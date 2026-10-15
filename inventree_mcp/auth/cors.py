"""CORS for the façade's machine endpoints (register, token, revoke).

Claude's web app calls these from the browser. Only origins listed in the
TRUSTED_CLIENT_ORIGINS plugin setting get an ``Access-Control-Allow-Origin``;
the metadata documents use ``*`` instead (see discovery.py).
"""

from __future__ import annotations

from django.http import HttpResponse

from ..settings import get_plugin_setting

DEFAULT_TRUSTED_ORIGINS = "https://claude.ai,https://claude.com"
PREFLIGHT_MAX_AGE_SECONDS = 3600


def trusted_origins() -> list[str]:
    """Origins allowed to call the façade endpoints, from TRUSTED_CLIENT_ORIGINS."""
    value = get_plugin_setting("TRUSTED_CLIENT_ORIGINS", DEFAULT_TRUSTED_ORIGINS)
    return [origin.strip().rstrip("/") for origin in value.split(",") if origin.strip()]


class TrustedOriginCORSMixin:
    """Answer preflights and tag responses for trusted origins only."""

    def _trusted_origin(self, request) -> str | None:
        origin = request.headers.get("Origin")
        return origin if origin and origin in trusted_origins() else None

    def dispatch(self, request, *args, **kwargs):
        """Short-circuit trusted preflights; add the CORS header to every other response."""
        origin = self._trusted_origin(request)

        if origin and request.method == "OPTIONS":
            response = HttpResponse(status=204)
            response["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            response["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            response["Access-Control-Max-Age"] = str(PREFLIGHT_MAX_AGE_SECONDS)
        else:
            response = super().dispatch(request, *args, **kwargs)

        # DOT's token backend already sets the header when the client's
        # Application.allowed_origins matches; do not send it twice.
        if origin and "Access-Control-Allow-Origin" not in response:
            response["Access-Control-Allow-Origin"] = origin
            response["Vary"] = "Origin"
        return response
