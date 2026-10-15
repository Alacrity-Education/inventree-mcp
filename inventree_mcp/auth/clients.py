"""Client registration for the façade (RFC 7591) and client classification helpers.

Claude identifies itself either by registering (DCR, this module's view) or by
using an https URL as its client_id (CIMD, resolved by DOT itself during
authorize/token once settings_inject enabled CIMD for the trusted hosts).
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.http.request import validate_host
from django.views import View
from oauth2_provider.cimd import (
    GRANT_TYPE_MAP,
    IGNORED_GRANT_TYPES,
    SafeMetadataFetcher,
)
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

from ..settings import plugin_setting_list
from . import FacadeViewMixin, ratelimit
from .cors import TrustedOriginCORSMixin, trusted_origins

logger = logging.getLogger("inventree")

DEFAULT_CLIENT_NAME = "MCP client"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def trusted_client_hosts() -> list[str]:
    """CIMD host allowlist from TRUSTED_CLIENT_HOSTS (Django ALLOWED_HOSTS syntax)."""
    return plugin_setting_list("TRUSTED_CLIENT_HOSTS")


class ClientMetadataFetcher(SafeMetadataFetcher):
    """DOT's metadata fetcher, keeping only the grant types DOT can map.

    claude.ai's client metadata document declares ``authorization_code``,
    ``refresh_token`` and ``urn:ietf:params:oauth:grant-type:jwt-bearer``.
    django-oauth-toolkit 3.4 ignores ``refresh_token`` but requires exactly one
    other grant type, so it refuses the document and the client shows up as
    unknown. RFC 7591 lets a server ignore grant types it does not support
    (KINA does the same): values DOT has no mapping for are dropped, but only
    when a mapped one remains, so a document without any usable grant type is
    still refused by DOT. Set as ``CIMD_METADATA_FETCHER`` (settings_inject).
    """

    def fetch(self, client_id):
        data, max_age = super().fetch(client_id)
        grants = data.get("grant_types")
        if isinstance(grants, list):
            known = [
                g for g in grants if g in GRANT_TYPE_MAP or g in IGNORED_GRANT_TYPES
            ]
            if len(known) != len(grants) and any(g in GRANT_TYPE_MAP for g in known):
                logger.info(
                    "inventree-mcp: client %s declares unsupported grant types %s, ignored",
                    client_id,
                    sorted(set(grants) - set(known)),
                )
                data = {**data, "grant_types": known}
        return data, max_age


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
        wait = ratelimit.retry_after(request, "dcr", "REGISTER_RATE_LIMIT_PER_MINUTE")
        if wait:
            return ratelimit.too_many_requests(wait)

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
