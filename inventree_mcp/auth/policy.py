"""Which MCP scopes a user may hold, and whether they may connect at all.

effective_scopes() is a WP1 stub body (every active user in
MCP_REQUIRED_GROUPS gets every advertised MCP scope); WP3 replaces it with the
MCP ruleset. The signatures and the call sites in authorize.py, token.py,
discovery.py and mcp_transport.py stay as they are. may_connect() also applies
the membership block (WP2) and MCP_REQUIRED_GROUPS (in_required_groups()), so
authorize, refresh narrowing and the MCP endpoint agree without separate checks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q

from ..settings import get_plugin_setting, plugin_setting_list
from . import membership

if TYPE_CHECKING:
    from collections.abc import Iterable

GENERAL_READ_SCOPE = "g:read"
WRITE_PERMISSIONS = ("add", "change", "delete")


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
    MCP_REQUIRED_GROUPS. Stub otherwise: everything advertised (WP3: the
    MCP ruleset).
    """
    if user is None or not user.is_authenticated or not user.is_active:
        return set()
    if not in_required_groups(user):
        return set()
    return set(mcp_scopes_available())


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
    "no_mcp_permission" (not in MCP_REQUIRED_GROUPS; WP3 adds "no MCP ruleset").
    """
    if user is None or not user.is_authenticated:
        return False, "anonymous"
    if not user.is_active:
        return False, "inactive"
    if membership.is_blocked(user):
        return False, "blocked"
    if not in_required_groups(user):
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
