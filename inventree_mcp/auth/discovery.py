"""OAuth discovery: protected-resource metadata, authorization-server metadata, 401 challenge.

Claude finds the authorization server by following the ``resource_metadata``
URL in the MCP endpoint's ``WWW-Authenticate`` challenge (RFC 9728), then
reads the authorization-server metadata (RFC 8414) of the issuer named there.
The issuer is the plugin root, ``<origin>/plugin/inventree-mcp``, so DOT's own
``/o/`` endpoints keep serving other OAuth applications untouched.

All URLs are absolute and built with ``request.build_absolute_uri``, so they
follow InvenTree's ``USE_X_FORWARDED_HOST`` / ``SECURE_PROXY_SSL_HEADER``.
"""

from __future__ import annotations

from django.http import HttpRequest, JsonResponse
from django.urls import reverse
from django.views import View

from . import FacadeViewMixin, oauth_enabled, policy, settings_inject

try:
    from oauth2_provider.views import OAuthProtectedResourceMetadataView
except ImportError:  # pragma: no cover - DOT < 3.4: the façade URLs are not registered
    OAuthProtectedResourceMetadataView = View

URL_NAMESPACE = "plugin:inventree-mcp"
PRM_MARKER = "/.well-known/oauth-protected-resource"
DOCUMENTATION_URL = "https://github.com/inventree/inventree-mcp"
METADATA_MAX_AGE_SECONDS = 300
REALM = "InvenTree"


def _absolute(request: HttpRequest, name: str) -> str:
    return request.build_absolute_uri(reverse(f"{URL_NAMESPACE}:{name}"))


def issuer(request: HttpRequest) -> str:
    """The façade's issuer: absolute origin + /plugin/inventree-mcp, no trailing slash."""
    prm_url = _absolute(request, "oauth-protected-resource")
    return prm_url[: -len(PRM_MARKER)]


def resource(request: HttpRequest) -> str:
    """The protected resource identifier: absolute URL of the MCP endpoint (trailing slash kept)."""
    return _absolute(request, "mcp-endpoint")


def challenge_header(request: HttpRequest, *, invalid_token: bool) -> str:
    """Value for the ``WWW-Authenticate`` header of a 401 from the MCP endpoint.

    ``resource_metadata`` and ``scope`` are only advertised while the façade
    is on; ``error="invalid_token"`` is added when a credential was presented.
    The header is built by DOT's ``build_bearer_challenge`` (DOT >= 3.4, only
    imported when the façade is supported); without the façade it is the bare
    ``Bearer realm="InvenTree"``.
    """
    settings_inject.ensure_applied()

    if not oauth_enabled():
        return f'Bearer realm="{REALM}"' + (
            ', error="invalid_token"' if invalid_token else ""
        )

    from oauth2_provider.www_authenticate import build_bearer_challenge

    oauth2_error = {"scope": " ".join(policy.mcp_scopes_available())}
    if invalid_token:
        oauth2_error["error"] = "invalid_token"
    return build_bearer_challenge(
        request,
        oauth2_error=oauth2_error,
        realm=REALM,
        resource_metadata_url=_absolute(request, "oauth-protected-resource"),
    )


class _MetadataResponseMixin:
    """Anonymous JSON metadata document, readable cross-origin and cacheable."""

    http_method_names = ("get", "head", "options")

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        """Add the CORS and caching headers to every answer."""
        response = super().dispatch(request, *args, **kwargs)
        response["Access-Control-Allow-Origin"] = "*"
        response["Cache-Control"] = f"max-age={METADATA_MAX_AGE_SECONDS}"
        return response

    def options(self, request: HttpRequest, *args, **kwargs):
        """Answer CORS preflights (e.g. a client adding MCP-Protocol-Version)."""
        response = super().options(request, *args, **kwargs)
        response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response["Access-Control-Allow-Headers"] = "*"
        return response


class ProtectedResourceMetadataView(
    FacadeViewMixin, _MetadataResponseMixin, OAuthProtectedResourceMetadataView
):
    """RFC 9728 protected-resource metadata for the MCP endpoint (DOT's view, per resource)."""

    def get_resource(self, request: HttpRequest) -> str:
        """The MCP endpoint's absolute URL."""
        return resource(request)

    def get_authorization_servers(self, request: HttpRequest) -> list[str]:
        """The façade's issuer."""
        return [issuer(request)]

    def get_scopes_supported(self) -> list[str]:
        """The MCP scopes, in their stable advertised order."""
        return policy.mcp_scopes_available()

    def get_resource_name(self) -> str:
        """Human-readable name of the resource."""
        return "InvenTree MCP"

    def get_resource_documentation(self) -> str:
        """The plugin's documentation."""
        return DOCUMENTATION_URL


class AuthorizationServerMetadataView(FacadeViewMixin, _MetadataResponseMixin, View):
    """RFC 8414 authorization-server metadata for the façade (also served as openid-configuration).

    Built here rather than by subclassing DOT's OAuthServerMetadataView, which
    hard-codes its own ``oauth2_provider:`` URL names and DCR_ENABLED.
    """

    def get(self, request: HttpRequest, *args, **kwargs) -> JsonResponse:
        """Serve the authorization-server metadata document."""
        auth_methods = ["none", "client_secret_basic", "client_secret_post"]
        return JsonResponse({
            "issuer": issuer(request),
            "authorization_endpoint": _absolute(request, "oauth-authorize"),
            "token_endpoint": _absolute(request, "oauth-token"),
            "registration_endpoint": _absolute(request, "oauth-register"),
            "revocation_endpoint": _absolute(request, "oauth-revoke"),
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": auth_methods,
            "revocation_endpoint_auth_methods_supported": auth_methods,
            "scopes_supported": policy.mcp_scopes_available(),
            "client_id_metadata_document_supported": True,
            "service_documentation": DOCUMENTATION_URL,
        })
