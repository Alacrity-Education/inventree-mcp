"""Background tasks of the auth package.

Scheduled through ScheduleMixin in core.py: cleanup_stale_clients (daily),
membership_sweep (hourly). Queued on demand: recheck_user (mcp_transport.py),
revoke_mcp_tokens (signals.revoke_users, for groups above REVOKE_SYNC_LIMIT members).
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.db.models import Exists, OuterRef
from django.utils import timezone

from ..settings import get_plugin_setting
from . import facade_supported

logger = logging.getLogger("inventree")

#: The sweep starts no new re-check after this many seconds and queues the rest
#: as a continuation task. django-q kills a task after InvenTree's
#: background.timeout (90 s by default) and one re-check can take up to about
#: 20 s (four outbound calls with a 5 s timeout), so 60 s leaves headroom.
SWEEP_BUDGET_SECONDS = 60
_clock = time.monotonic


def cleanup_stale_clients() -> int:
    """Delete DCR applications that nobody uses any more; return how many were deleted.

    Claude registers a new client on every fresh connection, so DCR rows pile
    up. A DCR application is deleted when its ``updated`` timestamp is older
    than UNUSED_CLIENT_RETENTION_DAYS and it holds no unexpired access token
    and no unrevoked refresh token. The registration access token DOT issues
    with every DCR client (scope DCR_REGISTRATION_SCOPE, expiring after the
    injected DCR_REGISTRATION_TOKEN_EXPIRE_SECONDS) does not count as use. CIMD and manually created applications are never touched.

    The worker process already runs with the façade's DOT settings: the
    django-q pre_execute receiver (settings_inject.connect_signals()) syncs
    them before every task, including InvenTree's daily DOT maintenance
    (clear_expired). ensure_applied() here covers a direct call.
    """
    if not facade_supported():
        return 0

    from oauth2_provider.models import (
        get_access_token_model,
        get_application_model,
        get_refresh_token_model,
    )
    from oauth2_provider.settings import oauth2_settings

    from . import settings_inject

    settings_inject.ensure_applied()

    Application = get_application_model()
    now = timezone.now()
    days = get_plugin_setting("UNUSED_CLIENT_RETENTION_DAYS", 90)

    live_access_tokens = (
        get_access_token_model()
        .objects.filter(application=OuterRef("pk"), expires__gt=now)
        .exclude(scope=oauth2_settings.DCR_REGISTRATION_SCOPE)
    )
    live_refresh_tokens = get_refresh_token_model().objects.filter(
        application=OuterRef("pk"), revoked__isnull=True
    )

    stale = Application.objects.filter(
        registration_source=Application.RegistrationSource.DCR,
        updated__lt=now - timedelta(days=days),
    ).filter(~Exists(live_access_tokens), ~Exists(live_refresh_tokens))

    count = stale.count()
    if count:
        stale.delete()
    logger.info("inventree-mcp: deleted %s stale DCR clients", count)
    return count


def recheck_user(user_id: int) -> str:
    """Background membership re-check of one user (queued by mcp_transport.py); returns the outcome."""
    from django.contrib.auth import get_user_model

    from . import membership

    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None:
        return "missing"
    return membership.recheck(user).value


def revoke_mcp_tokens(user_ids: list[int], why: str = "MCP permissions reduced") -> int:
    """Revoke the MCP tokens of *user_ids*; returns the token count.

    Queued by signals.revoke_users for groups above REVOKE_SYNC_LIMIT members
    and called inline by it for smaller ones. Logs counts and *why* only.
    """
    from django.contrib.auth import get_user_model

    from .membership import revoke_all_mcp_tokens

    count = sum(
        revoke_all_mcp_tokens(user)
        for user in get_user_model().objects.filter(pk__in=user_ids)
    )
    logger.info(
        "inventree-mcp: %s; revoked %s MCP tokens of %s users",
        why,
        count,
        len(user_ids),
    )
    return count


def membership_sweep(user_ids: list[int] | None = None) -> dict[str, int]:
    """Re-check users who hold MCP access but are due (hourly, via ScheduleMixin).

    Candidates are users with an unrevoked refresh token of a façade
    application (clients.mcp_applications()) or an active InvenTree API
    token, who are not blocked and whose last check is older than
    MEMBERSHIP_RECHECK_INTERVAL_MINUTES. Logs one line with counts only.

    Time budget: no new re-check starts after SWEEP_BUDGET_SECONDS; the
    remaining candidates are queued as a continuation of this task
    (*user_ids*), which re-checks only those (still due, not blocked) users.
    At least one user is re-checked per run, so a continuation always makes
    progress. A user whose re-check is in progress elsewhere (lease held)
    counts as not_due.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    from . import membership

    counts: dict[str, int] = {}
    if not membership.enforced():
        logger.info("inventree-mcp: membership sweep skipped (not enforced)")
        return counts

    from users.models import ApiToken

    from ..models import MCPUserState

    # Not "mcp_state__..." from User: InvenTree registers plugin apps after the
    # User model's reverse relations are built, so that lookup is unavailable
    # in running processes (it only works in the test runner).
    blocked = MCPUserState.objects.filter(
        user=OuterRef("pk"), access_revoked_at__isnull=False
    )
    users = get_user_model().objects.exclude(Exists(blocked))

    if user_ids is None:
        today = timezone.now().date()
        holds_access = Exists(
            ApiToken.objects.filter(user=OuterRef("pk"), revoked=False).filter(
                Q(expiry__isnull=True) | Q(expiry__gte=today)
            )
        )
        if facade_supported():
            from oauth2_provider.models import get_refresh_token_model

            from .clients import mcp_applications

            holds_access |= Exists(
                get_refresh_token_model().objects.filter(
                    user=OuterRef("pk"),
                    revoked__isnull=True,
                    application__in=mcp_applications(),
                )
            )
        users = users.filter(holds_access)
    else:
        users = users.filter(pk__in=user_ids)

    pks = list(users.order_by("pk").values_list("pk", flat=True))
    started = _clock()

    for index, pk in enumerate(pks):
        if index and _clock() - started >= SWEEP_BUDGET_SECONDS:
            remaining = pks[index:]
            counts["deferred"] = len(remaining)
            _queue_continuation(remaining)
            break
        user = get_user_model().objects.filter(pk=pk).first()
        if user is None:
            continue
        if not membership.recheck_due(user):
            counts["not_due"] = counts.get("not_due", 0) + 1
            continue
        try:
            outcome = membership.recheck(user).value
        except Exception:  # noqa: BLE001 - one user's failure must not stop the sweep
            outcome = "error"
        counts[outcome] = counts.get(outcome, 0) + 1

    logger.info(
        "inventree-mcp: membership sweep: %s",
        ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        or "no users",
    )
    return counts


def _queue_continuation(user_ids: list[int]) -> None:
    """Queue membership_sweep for the users the time budget left out."""
    from InvenTree.tasks import offload_task

    try:
        offload_task(
            "inventree_mcp.auth.tasks.membership_sweep",
            user_ids,
            force_async=True,
            check_duplicates=True,
        )
    except Exception:  # the next hourly sweep picks them up
        logger.warning(
            "inventree-mcp: could not queue the membership sweep continuation "
            "(%s users left for the next sweep)",
            len(user_ids),
            exc_info=True,
        )
