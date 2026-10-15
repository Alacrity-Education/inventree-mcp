"""Signal receivers of the auth package: login stamps (WP2) and the MCP rulesets (WP3).

MCP rulesets (models.MCPRuleSet):

- every group gets one row per core role: ``post_save`` on ``Group`` (new
  groups), migration 0002 (existing ones) and ``ensure_rulesets()``, which the
  API also calls so a role added by a core upgrade appears;
- a reduction revokes: a flag going from true to false (``pre_save`` snapshot,
  ``post_save`` comparison), a deleted row that granted something, or a deleted
  group whose rows granted something revokes the MCP tokens of every member of
  the group (``membership.revoke_all_mcp_tokens``, never a block). More than
  REVOKE_SYNC_LIMIT members go to a background task. Additions revoke
  nothing: the next authorize or refresh picks them up.

Login stamps (WP2):

An SSO login through the MCP sign-in provider proves membership the same way a
re-check does: allauth already ran core's group sync (``ensure_sso_groups`` on
``social_account_updated`` / the SocialAccount ``post_save``) before it sends
``user_logged_in``. So it always sets ``membership_checked_at`` and
``last_login_at``, and lifts a block only when the identity checks pass (the
user is active and in MCP_REQUIRED_GROUPS); otherwise the block stays.
Other logins (password) only record ``last_login_at``, which the degraded
"no upstream refresh token" mode reads.

Deletions (``pre_delete`` on ``Group`` and on the user model): the plugin's
rows are deleted here, not left to ``on_delete=CASCADE``. After a plugin
registry reload on InvenTree 1.6+ (core PR #13007 purges and re-imports the
plugin's modules) core models keep stale relation caches and no longer know
the plugin's foreign keys, so the cascade is never collected and the
database constraint fails the whole transaction.

connect_signals() is called from the plugin's ``__init__`` and is idempotent
(``dispatch_uid``: the receivers connected first stay connected when
InvenTree's registry re-imports this module).
"""

from __future__ import annotations

import logging

from django.db import DatabaseError, transaction
from django.utils import timezone

logger = logging.getLogger("inventree")

#: Up to this many group members are revoked in the request; more go to a background task.
REVOKE_SYNC_LIMIT = 20


def _flags() -> tuple[str, ...]:
    """MCPRuleSet.RULE_OPTIONS (imported late: the plugin's app may not be installed)."""
    from ..models import MCPRuleSet

    return MCPRuleSet.RULE_OPTIONS


#: The ruleset model as a lazy sender: connected once the plugin's app is installed.
_RULESET_MODEL = "inventree_mcp.MCPRuleSet"

#: Groups whose deletion is in progress (their rows' post_delete must not revoke again).
_deleting_groups: set[int] = set()


def on_user_logged_in(sender, request=None, user=None, sociallogin=None, **kwargs):
    """allauth ``user_logged_in``: stamp MCPUserState; an SSO login may also unblock."""
    if user is None or not getattr(user, "pk", None):
        return
    try:
        from . import policy
        from .membership import _locked_state, models_available
        from .sso import sso_provider_id

        if not models_available():
            return

        provider = getattr(getattr(sociallogin, "account", None), "provider", None)
        through_sso = provider is not None and provider == sso_provider_id(request)
        now = timezone.now()
        lifted = kept = False

        with transaction.atomic():
            state = _locked_state(user)
            state.last_login_at = now
            fields = ["last_login_at"]
            if through_sso:
                state.membership_checked_at = now
                state.last_recheck_error = ""
                fields += ["membership_checked_at", "last_recheck_error"]
                if state.access_revoked_at is not None:
                    if user.is_active and policy.in_required_groups(user):
                        state.access_revoked_at = None
                        state.revoked_reason = ""
                        fields += ["access_revoked_at", "revoked_reason"]
                        lifted = True
                    else:
                        kept = True
            state.save(update_fields=fields)

        if lifted:
            logger.info(
                "inventree-mcp: SSO login lifted the MCP block of user id %s", user.pk
            )
        elif kept:
            logger.info(
                "inventree-mcp: SSO login of user id %s kept the MCP block "
                "(identity checks failed)",
                user.pk,
            )
    except Exception:  # never break a login over the stamp
        logger.warning(
            "inventree-mcp: could not record the login of user id %s",
            user.pk,
            exc_info=True,
        )


# -- MCP rulesets ---------------------------------------------------------------


def ensure_rulesets(group) -> int:
    """Create the missing MCPRuleSet rows (all flags false) of *group*; returns how many."""
    return ensure_rulesets_for([group.pk])


def ensure_rulesets_for(group_ids) -> int:
    """ensure_rulesets() for several groups, by pk: one SELECT, at most one bulk INSERT.

    *group_ids* is any iterable of group pks (e.g. a ``values_list("pk",
    flat=True)`` queryset, so no Group rows are loaded).
    """
    from users.ruleset import RULESET_NAMES

    from ..models import MCPRuleSet

    group_ids = list(group_ids)
    if not group_ids:
        return 0
    existing = set(
        MCPRuleSet.objects.filter(group_id__in=group_ids).values_list(
            "group_id", "name"
        )
    )
    missing = [
        MCPRuleSet(group_id=group_id, name=str(name))
        for group_id in group_ids
        for name in RULESET_NAMES
        if (group_id, str(name)) not in existing
    ]
    if missing:
        MCPRuleSet.objects.bulk_create(missing, ignore_conflicts=True)
    return len(missing)


def grants_anything(rows) -> bool:
    """Whether any of *rows* (MCPRuleSet instances or flag dicts) has a true flag."""
    for row in rows:
        get = row.get if isinstance(row, dict) else lambda f, r=row: getattr(r, f)
        if any(get(flag) for flag in _flags()):
            return True
    return False


def live_mcp_token_count(user_ids) -> int:
    """Live façade tokens of *user_ids* (unrevoked refresh plus unexpired access tokens)."""
    from .membership import mcp_token_querysets

    refresh, access = mcp_token_querysets(user_ids)
    return refresh.count() + access.count()


def revoke_users(user_ids, why: str) -> tuple[int, int]:
    """Revoke the MCP tokens of *user_ids*: (users, tokens).

    Up to REVOKE_SYNC_LIMIT users synchronously (``tasks.revoke_mcp_tokens``
    called inline); more are handed to that task (offload_task runs it inline
    when no worker is running), and *tokens* is then the number of live
    tokens at queueing time. Logs counts only.
    """
    from .tasks import revoke_mcp_tokens

    user_ids = sorted(set(user_ids))
    if not user_ids:
        return 0, 0

    if len(user_ids) > REVOKE_SYNC_LIMIT:
        from InvenTree.tasks import offload_task

        tokens = live_mcp_token_count(user_ids)
        offload_task(
            "inventree_mcp.auth.tasks.revoke_mcp_tokens",
            user_ids,
            check_duplicates=False,
        )
        logger.info(
            "inventree-mcp: %s; MCP tokens of %s users queued for revocation (%s live)",
            why,
            len(user_ids),
            tokens,
        )
        return len(user_ids), tokens

    return len(user_ids), revoke_mcp_tokens(user_ids, why)


def revoke_group_members(
    group, why: str = "MCP tokens revoked for a group"
) -> tuple[int, int]:
    """Revoke the MCP tokens of every member of *group*: (users, tokens)."""
    user_ids = list(group.user_set.values_list("pk", flat=True))
    return revoke_users(user_ids, f"{why} (group id {group.pk})")


def on_group_saved(sender, instance=None, created=False, raw=False, **kwargs):
    """Group ``post_save``: a new group gets its MCPRuleSet rows (core does the same for RuleSet)."""
    if raw or not created or instance is None:
        return
    from .membership import models_available

    if not models_available():
        return
    try:
        with transaction.atomic():
            ensure_rulesets(instance)
    except DatabaseError:  # never break a group save over the plugin's table
        logger.warning(
            "inventree-mcp: could not create the MCP rulesets of group id %s "
            "(run `invoke migrate`)",
            instance.pk,
            exc_info=True,
        )


def on_group_deleting(sender, instance=None, **kwargs):
    """Group ``pre_delete``: revoke the members' MCP tokens when the group granted MCP access.

    Runs before the cascade (the members are still known); the rows'
    own ``post_delete`` then sees the group marked and does nothing.
    """
    if instance is None or instance.pk is None:
        return
    from .membership import models_available

    if not models_available():
        return
    from ..models import MCPRuleSet

    try:
        with transaction.atomic():
            granted = grants_anything(
                MCPRuleSet.objects.filter(group=instance).values(*_flags())
            )
    except DatabaseError:
        return
    _deleting_groups.add(instance.pk)
    if granted:
        revoke_group_members(instance, "group with MCP permissions deleted")
    # Not left to the cascade (see the module docstring); the rows' post_delete
    # sees the marker above and revokes nothing more.
    MCPRuleSet.objects.filter(group_id=instance.pk).delete()


def on_group_deleted(sender, instance=None, **kwargs):
    """Group ``post_delete``: forget the in-progress marker."""
    if instance is not None:
        _deleting_groups.discard(instance.pk)


def on_user_deleting(sender, instance=None, **kwargs):
    """User ``pre_delete``: drop the user's MCPUserState row (not left to the cascade, see the module docstring)."""
    if instance is None or instance.pk is None:
        return
    from .membership import models_available

    if not models_available():
        return
    from ..models import MCPUserState

    MCPUserState.objects.filter(user_id=instance.pk).delete()


def _is_ruleset(instance) -> bool:
    """Whether *instance* is an MCPRuleSet row (the receivers below check, see connect_signals)."""
    return instance is not None and instance._meta.label == _RULESET_MODEL


def on_ruleset_saving(sender, instance=None, raw=False, **kwargs):
    """MCPRuleSet ``pre_save``: remember the stored flags for the reduction check."""
    if raw or not _is_ruleset(instance) or instance.pk is None:
        return
    instance._mcp_previous_flags = (
        type(instance).objects.filter(pk=instance.pk).values(*_flags()).first()
    )


def on_ruleset_saved(sender, instance=None, created=False, raw=False, **kwargs):
    """MCPRuleSet ``post_save``: a flag that went from true to false revokes the group's tokens."""
    if raw or created or not _is_ruleset(instance):
        return
    previous = getattr(instance, "_mcp_previous_flags", None)
    if not previous:
        return
    instance._mcp_previous_flags = None
    if any(previous[flag] and not getattr(instance, flag) for flag in _flags()):
        revoke_group_members(instance.group, "MCP permissions of a group reduced")


def on_ruleset_deleted(sender, instance=None, **kwargs):
    """MCPRuleSet ``post_delete``: a deleted row that granted something revokes too."""
    if not _is_ruleset(instance) or instance.group_id in _deleting_groups:
        return
    if grants_anything([instance]):
        from django.contrib.auth.models import Group

        group = Group.objects.filter(pk=instance.group_id).first()
        if group is not None:
            revoke_group_members(group, "MCP permissions of a group deleted")


def connect_signals() -> None:
    """Connect the login and ruleset receivers (idempotent).

    The ruleset receivers are connected to the model by name: every call (the
    plugin's ``__init__``, so every registry reload) binds the class currently
    registered, because InvenTree re-executes the plugin's models module on
    each reload. Earlier classes die, and Django matches senders by ``id()``,
    so a later class (of any model) can reuse a dead class's id and the stale
    entry then fires for its rows: the receivers check the instance's model
    themselves (``_is_ruleset``).
    """
    from allauth.account.signals import user_logged_in
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group
    from django.db.models.signals import post_delete, post_save, pre_delete, pre_save

    user_logged_in.connect(
        on_user_logged_in, dispatch_uid="inventree_mcp.auth.signals.user_logged_in"
    )
    post_save.connect(
        on_group_saved,
        sender=Group,
        dispatch_uid="inventree_mcp.auth.signals.group_saved",
    )
    pre_delete.connect(
        on_group_deleting,
        sender=Group,
        dispatch_uid="inventree_mcp.auth.signals.group_deleting",
    )
    post_delete.connect(
        on_group_deleted,
        sender=Group,
        dispatch_uid="inventree_mcp.auth.signals.group_deleted",
    )
    pre_delete.connect(
        on_user_deleting,
        sender=get_user_model(),
        dispatch_uid="inventree_mcp.auth.signals.user_deleting",
    )
    pre_save.connect(
        on_ruleset_saving,
        sender=_RULESET_MODEL,
        dispatch_uid="inventree_mcp.auth.signals.ruleset_saving",
    )
    post_save.connect(
        on_ruleset_saved,
        sender=_RULESET_MODEL,
        dispatch_uid="inventree_mcp.auth.signals.ruleset_saved",
    )
    post_delete.connect(
        on_ruleset_deleted,
        sender=_RULESET_MODEL,
        dispatch_uid="inventree_mcp.auth.signals.ruleset_deleted",
    )
