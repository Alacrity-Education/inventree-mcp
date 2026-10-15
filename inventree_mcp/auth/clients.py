"""Client registration for the façade (RFC 7591) and client classification helpers.

Claude identifies itself either by registering (DCR, this module's view) or by
using an https URL as its client_id (CIMD, resolved by DOT itself during
authorize/token once settings_inject enabled CIMD for the trusted hosts).
"""

from __future__ import annotations

import logging
import math
import time
from urllib.parse import urlparse

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.http.request import validate_host
from django.views import View
from oauth2_provider.models import get_application_model
from oauth2_provider.views import DynamicClientRegistrationView

# DOT's registration helpers are private names; we reuse them instead of
# copying their bodies (metadata parsing and validation, registration token,
# response shape). test_dot_helpers_present fails loudly if a DOT upgrade
# renames or removes them.
from oauth2_provider.views.dynamic_client_registration import (
    _application_to_response,
    _build_application_kwargs,
    _error_response,
    _issue_registration_token,
    _parse_metadata,
    _validation_error_description,
)

from ..settings import get_plugin_setting
from . import FacadeViewMixin
from .cors import TrustedOriginCORSMixin, trusted_origins

logger = logging.getLogger("inventree")

DEFAULT_TRUSTED_HOSTS = "claude.ai,claude.com,.anthropic.com"
DEFAULT_CLIENT_NAME = "MCP client"
RATE_LIMIT_WINDOW_SECONDS = 60
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def trusted_client_hosts() -> list[str]:
    """CIMD host allowlist from TRUSTED_CLIENT_HOSTS (Django ALLOWED_HOSTS syntax)."""
    value = get_plugin_setting("TRUSTED_CLIENT_HOSTS", DEFAULT_TRUSTED_HOSTS)
    return [host.strip() for host in value.split(",") if host.strip()]


def mcp_applications():
    """Applications created through the façade: registered by DCR or resolved by CIMD."""
    Application = get_application_model()
    return Application.objects.filter(
        registration_source__in=[
            Application.RegistrationSource.DCR,
            Application.RegistrationSource.CIMD,
        ]
    )


def is_trusted_client(application) -> bool:
    """A CIMD application whose client_id URL host matches TRUSTED_CLIENT_HOSTS."""
    if application.registration_source != application.RegistrationSource.CIMD:
        return False
    host = urlparse(application.client_id).hostname
    return bool(host) and validate_host(host, trusted_client_hosts())


def is_loopback_redirect(uri: str) -> bool:
    """An http redirect URI on the local machine (any local program can listen there)."""
    parsed = urlparse(uri or "")
    return parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS


def _rate_limited(request: HttpRequest) -> int:
    """Count this registration; return the seconds to wait when over the limit, else 0.

    Fixed one-minute windows aligned to the clock, one counter per client IP
    in Django's cache. REMOTE_ADDR already reflects InvenTree's proxy settings.
    """
    limit = get_plugin_setting("REGISTER_RATE_LIMIT_PER_MINUTE", 30)
    if limit <= 0:
        return 0

    key = f"inventree_mcp:dcr:{request.META.get('REMOTE_ADDR', 'unknown')}"
    remaining = RATE_LIMIT_WINDOW_SECONDS - (time.time() % RATE_LIMIT_WINDOW_SECONDS)

    cache.add(key, 0, timeout=math.ceil(remaining))
    try:
        count = cache.incr(key)
    except ValueError:  # expired between add() and incr()
        cache.set(key, 1, timeout=math.ceil(remaining))
        count = 1

    return math.ceil(remaining) if count > limit else 0


class _UngatedRegistrationView(DynamicClientRegistrationView):
    def dispatch(self, request, *args, **kwargs):
        # Skip DOT's DCR_ENABLED gate: it guards DOT's own /o/register/, which
        # stays off. The façade's gate is FacadeViewMixin (OAUTH_ENABLED).
        return View.dispatch(self, request, *args, **kwargs)


class MCPRegistrationView(
    FacadeViewMixin, TrustedOriginCORSMixin, _UngatedRegistrationView
):
    """RFC 7591 registration for MCP clients: anonymous, rate limited per IP."""

    def post(self, request: HttpRequest, *args, **kwargs) -> JsonResponse:
        """Register a client the way DOT does, with façade defaults (no owner, CORS origins)."""
        retry_after = _rate_limited(request)
        if retry_after:
            response = JsonResponse({"error": "too_many_requests"}, status=429)
            response["Retry-After"] = str(retry_after)
            return response

        data, error = _parse_metadata(request.body)
        if error:
            return error

        app_kwargs, error = _build_application_kwargs(data)
        if error:
            return error

        Application = get_application_model()
        if (
            app_kwargs["authorization_grant_type"]
            != Application.GRANT_AUTHORIZATION_CODE
        ):
            # The façade only serves the authorization_code (+ refresh_token) flow.
            return _error_response(
                "invalid_client_metadata",
                "grant_types must be ['authorization_code', 'refresh_token']",
            )

        app_kwargs["name"] = app_kwargs.get("name") or DEFAULT_CLIENT_NAME
        application = Application(
            user=None,
            registration_source=Application.RegistrationSource.DCR,
            allowed_origins=" ".join(trusted_origins()),
            skip_authorization=False,
            **app_kwargs,
        )

        # Capture the raw secret before save() hashes it (as DOT does).
        raw_secret = (
            application.client_secret
            if application.client_type == Application.CLIENT_CONFIDENTIAL
            else None
        )

        try:
            application.full_clean()
        except ValidationError as exc:
            return _error_response(
                "invalid_client_metadata", _validation_error_description(exc)
            )

        with transaction.atomic():
            application.save()
            registration_token = _issue_registration_token(application, None)

        data = _application_to_response(application, registration_token, request)
        data["client_id_issued_at"] = int(application.created.timestamp())
        data["client_secret_expires_at"] = 0
        if raw_secret:
            data["client_secret"] = raw_secret

        logger.info(
            "inventree-mcp: registered MCP client %s (%s)",
            application.pk,
            application.client_type,
        )
        return JsonResponse(data, status=201)
