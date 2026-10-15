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

URL_NAMESPACE = "plugin:inventree-mcp"
PRM_MARKER = "/.well-known/oauth-protected-resource"
DOCUMENTATION_URL = "https://github.com/inventree/inventree-mcp"
METADATA_MAX_AGE_SECONDS = 300


def _absolute(request: HttpRequest, name: str) -> str:
    return request.build_absolute_uri(reverse(f"{URL_NAMESPACE}:{name}"))


def issuer(request: HttpRequest) -> str:
    """The façade's issuer: absolute origin + /plugin/inventree-mcp, no trailing slash."""
    prm_url = _absolute(request, "oauth-protected-resource")
    return prm_url[: -len(PRM_MARKER)]


def resource(request: HttpRequest) -> str:
    """The protected resource identifier: absolute URL of the MCP endpoint (trailing slash kept)."""
    return _absolute(request, "mcp-endpoint")


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def challenge_header(request: HttpRequest, *, invalid_token: bool) -> str:
    """Value for the ``WWW-Authenticate`` header of a 401 from the MCP endpoint.

    ``resource_metadata`` and ``scope`` are only advertised while the façade
    is on; ``error="invalid_token"`` is added when a credential was presented.
    """
    params = [("realm", "InvenTree")]

    if oauth_enabled():
        settings_inject.ensure_applied()
        params += [
            ("resource_metadata", _absolute(request, "oauth-protected-resource")),
            ("scope", " ".join(policy.mcp_scopes_available())),
        ]

    if invalid_token:
        params.append(("error", "invalid_token"))

    return "Bearer " + ", ".join(f'{key}="{_quote(value)}"' for key, value in params)


class _MetadataView(FacadeViewMixin, View):
    """Anonymous JSON metadata document, readable cross-origin and cacheable."""

    http_method_names = ["get", "head", "options"]

    def document(self, request: HttpRequest) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError

    def _with_headers(self, response):
        response["Access-Control-Allow-Origin"] = "*"
        response["Cache-Control"] = f"max-age={METADATA_MAX_AGE_SECONDS}"
        return response

    def get(self, request: HttpRequest, *args, **kwargs) -> JsonResponse:
        """Serve the metadata document."""
        return self._with_headers(JsonResponse(self.document(request)))

    def options(self, request: HttpRequest, *args, **kwargs):
        """Answer CORS preflights (e.g. a client adding MCP-Protocol-Version)."""
        response = super().options(request, *args, **kwargs)
        response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response["Access-Control-Allow-Headers"] = "*"
        return self._with_headers(response)


class ProtectedResourceMetadataView(_MetadataView):
    """RFC 9728 protected-resource metadata for the MCP endpoint."""

    def document(self, request: HttpRequest) -> dict:
        """Build the protected-resource metadata document."""
        return {
            "resource": resource(request),
            "authorization_servers": [issuer(request)],
            "scopes_supported": policy.mcp_scopes_available(),
            "bearer_methods_supported": ["header"],
            "resource_name": "InvenTree MCP",
            "resource_documentation": DOCUMENTATION_URL,
        }


class AuthorizationServerMetadataView(_MetadataView):
    """RFC 8414 authorization-server metadata for the façade (also served as openid-configuration)."""

    def document(self, request: HttpRequest) -> dict:
        """Build the authorization-server metadata document."""
        auth_methods = ["none", "client_secret_basic", "client_secret_post"]
        return {
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
        }
