"""Which MCP scopes a user may hold, and whether they may connect at all.

The second permission set (WP3): every group has one MCPRuleSet row per core
role with core's four flags. A user's MCP scopes are the union of the flags of
their groups' rows as core's granular scopes ("r:view:part", ...), cut down to
mcp_scopes_available() (so MCP_READ_ONLY strips the write scopes), plus "g:read"
when anything is left. Core enforces interactive roles AND token scopes on
every proxied call, so MCP access = interactive roles ∩ MCP roles; nothing here
re-checks a permission.

may_connect() adds the membership block (WP2), MCP_REQUIRED_GROUPS
(in_required_groups()) and "no MCP scope at all", so authorize, refresh
narrowing and the MCP endpoint agree without separate checks. The signatures
and the call sites in authorize.py, token.py, discovery.py and mcp_transport.py
are binding.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models import Q

from ..settings import get_plugin_setting, plugin_setting_list
from . import membership

logger = logging.getLogger("inventree")

if TYPE_CHECKING:
    from collections.abc import Iterable

GENERAL_READ_SCOPE = "g:read"
WRITE_PERMISSIONS = ("add", "change", "delete")
#: MCPRuleSet flag -> core permission name (users.ruleset.RULESET_PERMISSIONS order).
FLAG_PERMISSIONS = (
    ("can_view", "view"),
    ("can_add", "add"),
    ("can_change", "change"),
    ("can_delete", "delete"),
)


def mcp_scopes_available() -> list[str]:
    """Every scope the MCP endpoint advertises, in a stable order.

    "g:read", then "r:view:<role>" for every InvenTree role, then (only when
    MCP_READ_ONLY is off) "r:add|change|delete:<role>". Scopes InvenTree does
    not define are dropped, so the list always validates against DOT's
    SCOPES backend.
    """
    from oauth2_provider.settings import oauth2_settings
    from users.ruleset import RULESET_NAMES

    permissions = ["view"]
    if not get_plugin_setting("MCP_READ_ONLY", True):
        permissions += WRITE_PERMISSIONS

    candidates = [GENERAL_READ_SCOPE] + [
        f"r:{permission}:{role}" for permission in permissions for role in RULESET_NAMES
    ]
    defined = oauth2_settings.SCOPES
    return [scope for scope in candidates if scope in defined]


def effective_scopes(user) -> set[str]:
    """The MCP scopes *user* may hold.

    Empty for anonymous and inactive users and for users outside
    MCP_REQUIRED_GROUPS. Superusers get everything advertised when
    MCP_SUPERUSER_FULL_ACCESS is on. Otherwise the union of the MCP rulesets
    of the user's groups (one query), intersected with mcp_scopes_available(),
    plus "g:read" when the result is not empty.
    """
    if user is None or not user.is_authenticated or not user.is_active:
        return set()
    if not in_required_groups(user):
        return set()

    available = mcp_scopes_available()
    if user.is_superuser and get_plugin_setting("MCP_SUPERUSER_FULL_ACCESS", False):
        return set(available)

    scopes = ruleset_scopes(user) & set(available)
    if scopes and GENERAL_READ_SCOPE in available:
        scopes.add(GENERAL_READ_SCOPE)
    return scopes


def mcp_roles(user) -> dict[str, dict[str, bool]]:
    """The union of the MCP flags of *user*'s groups: {role: {view, add, change, delete}}.

    Every core role is present (all False when no group grants it). Reads
    MCPRuleSet directly (group__in), never Group.mcp_rule_sets (see models.py).
    """
    from users.ruleset import RULESET_NAMES

    roles = {
        str(name): dict.fromkeys((perm for _flag, perm in FLAG_PERMISSIONS), False)
        for name in RULESET_NAMES
    }
    for row in _ruleset_rows(user):
        flags = roles.setdefault(
            row[0], dict.fromkeys((perm for _flag, perm in FLAG_PERMISSIONS), False)
        )
        for (_flag, perm), value in zip(FLAG_PERMISSIONS, row[1:], strict=True):
            flags[perm] = flags[perm] or bool(value)
    return roles


def ruleset_scopes(user) -> set[str]:
    """Core's granular scopes for every true MCP flag of *user*'s groups (not narrowed)."""
    from users.oauth2_scopes import get_granular_scope

    return {
        get_granular_scope(perm, name)
        for name, flags in mcp_roles(user).items()
        for perm, value in flags.items()
        if value
    }


_no_app_logged = False


def _ruleset_rows(user) -> list[tuple]:
    """(name, can_view, can_add, can_change, can_delete) of the user's groups' MCP rulesets.

    Without the plugin's Django app (InvenTree's app integration off) or its
    table there are no MCP rulesets, so nobody gets an MCP scope (fail
    closed); logged once per process.
    """
    global _no_app_logged  # one warning per process
    if not membership.models_available():
        if not _no_app_logged:
            _no_app_logged = True
            logger.warning(
                "inventree-mcp: app integration (ENABLE_PLUGINS_APP) is off, so there "
                "are no MCP permissions and the MCP endpoint refuses every user"
            )
        return []
    from ..models import MCPRuleSet

    return membership._read(
        lambda: list(
            MCPRuleSet.objects.filter(group__in=user.groups.all()).values_list(
                "name", *(flag for flag, _perm in FLAG_PERMISSIONS)
            )
        ),
        [],
    )


def in_required_groups(user) -> bool:
    """Whether *user* is in one of the MCP_REQUIRED_GROUPS (always True when none are set).

    The names are InvenTree group names (as core's SSO group sync maps them),
    compared case-insensitively.
    """
    required = plugin_setting_list("MCP_REQUIRED_GROUPS")
    if not required:
        return True
    match = Q()
    for name in required:
        match |= Q(name__iexact=name)
    return user.groups.filter(match).exists()


def may_connect(user) -> tuple[bool, str]:
    """Whether *user* may connect an MCP client: (True, "") or (False, reason).

    Reasons: "anonymous", "inactive", "blocked" (membership block),
    "no_mcp_permission" (not in MCP_REQUIRED_GROUPS, or no MCP scope at all).
    The identity checks come first; membership._classify() relies on that
    order to tell identity refusals from a lacking MCP permission.
    """
    if user is None or not user.is_authenticated:
        return False, "anonymous"
    if not user.is_active:
        return False, "inactive"
    if membership.is_blocked(user):
        return False, "blocked"
    if not in_required_groups(user):
        return False, "no_mcp_permission"
    if not effective_scopes(user):
        return False, "no_mcp_permission"
    return True, ""


def narrow(requested: Iterable[str] | None, user) -> list[str]:
    """Reduce *requested* scopes to what *user* may hold.

    No requested scopes means "everything the user may hold", in advertised
    order; otherwise the intersection, in requested order, without duplicates.
    """
    effective = effective_scopes(user)
    requested = list(requested or [])

    if not requested:
        return [scope for scope in mcp_scopes_available() if scope in effective]

    return [
        scope
        for index, scope in enumerate(requested)
        if scope in effective and scope not in requested[:index]
    ]
