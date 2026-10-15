"""Signal receivers of the auth package: login stamps for the membership re-check (WP2).

An SSO login through the MCP sign-in provider proves membership the same way a
re-check does: allauth already ran core's group sync (``ensure_sso_groups`` on
``social_account_updated`` / the SocialAccount ``post_save``) before it sends
``user_logged_in``. So it sets ``membership_checked_at`` and lifts a block.
Other logins (password) only record ``last_login_at``, which the degraded
"no upstream refresh token" mode reads.

connect_signals() is called from the plugin's ``__init__`` and is idempotent
(``dispatch_uid``); this module is never reloaded by InvenTree's registry.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger("inventree")


def on_user_logged_in(sender, request=None, user=None, sociallogin=None, **kwargs):
    """allauth ``user_logged_in``: stamp MCPUserState; an SSO login also unblocks."""
    if user is None or not getattr(user, "pk", None):
        return
    try:
        from ..models import MCPUserState
        from .membership import models_available
        from .sso import sso_provider_id

        if not models_available():
            return

        provider = getattr(getattr(sociallogin, "account", None), "provider", None)
        through_sso = provider is not None and provider == sso_provider_id(request)
        now = timezone.now()

        with transaction.atomic():
            state, _created = MCPUserState.objects.select_for_update().get_or_create(
                user=user
            )
            was_blocked = state.access_revoked_at is not None
            state.last_login_at = now
            fields = ["last_login_at"]
            if through_sso:
                state.membership_checked_at = now
                state.access_revoked_at = None
                state.revoked_reason = ""
                state.last_recheck_error = ""
                fields += [
                    "membership_checked_at",
                    "access_revoked_at",
                    "revoked_reason",
                    "last_recheck_error",
                ]
            state.save(update_fields=fields)

        if through_sso and was_blocked:
            logger.info(
                "inventree-mcp: SSO login lifted the MCP block of user id %s", user.pk
            )
    except Exception:  # never break a login over the stamp
        logger.warning(
            "inventree-mcp: could not record the login of user id %s",
            user.pk,
            exc_info=True,
        )


def connect_signals() -> None:
    """Connect the login receiver (idempotent)."""
    from allauth.account.signals import user_logged_in

    user_logged_in.connect(
        on_user_logged_in, dispatch_uid="inventree_mcp.auth.signals.user_logged_in"
    )
