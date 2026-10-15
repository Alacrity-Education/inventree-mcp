"""Scheduled tasks of the OAuth façade (registered through ScheduleMixin in core.py)."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Exists, OuterRef
from django.utils import timezone

from ..settings import get_plugin_setting
from . import facade_supported

logger = logging.getLogger("inventree")


def cleanup_stale_clients() -> int:
    """Delete DCR applications that nobody uses any more; return how many were deleted.

    Claude registers a new client on every fresh connection, so DCR rows pile
    up. A DCR application is deleted when its ``updated`` timestamp is older
    than UNUSED_CLIENT_RETENTION_DAYS and it holds no unexpired access token
    and no unrevoked refresh token. The registration access token DOT issues
    with every DCR client (scope DCR_REGISTRATION_SCOPE, never expiring) does
    not count as use. CIMD and manually created applications are never touched.

    Also re-syncs the injected DOT settings in this (worker) process, so
    InvenTree's daily DOT maintenance task (clear_expired) sees the façade's
    refresh-token lifetime.
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
