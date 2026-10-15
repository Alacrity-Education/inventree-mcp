"""OAuth façade, discovery and policy hooks for the MCP endpoint.

Everything here sits on top of django-oauth-toolkit (DOT): OAuth state lives in
DOT's models, PKCE, code issuance, client authentication and token rotation are
DOT/oauthlib. The façade views subclass DOT's views and only add the MCP
specific policy (who may connect, which scopes, consent skipping, CORS).

This module must stay importable on every supported InvenTree version, so it
only holds the feature detection and the "is the façade switched on" check.
"""

from __future__ import annotations

import functools
import importlib.util


@functools.cache
def facade_supported() -> bool:
    """True when the installed DOT provides what the façade builds on (DOT >= 3.4)."""
    try:
        import oauth2_provider.views as dot_views
    except ImportError:  # pragma: no cover - DOT is a hard InvenTree dependency
        return False

    return hasattr(dot_views, "DynamicClientRegistrationView") and (
        importlib.util.find_spec("oauth2_provider.cimd") is not None
    )


def oauth_enabled() -> bool:
    """True when the façade is supported and the OAUTH_ENABLED plugin setting is on."""
    from ..settings import get_plugin_setting

    return facade_supported() and get_plugin_setting("OAUTH_ENABLED", False)


class FacadeViewMixin:
    """First base of every façade view: 404 while the façade is off, else sync DOT settings.

    Must come before DOT's view classes in the MRO, so it runs ahead of DOT's
    own dispatch (login redirect, DCR gate, ...).
    """

    def dispatch(self, request, *args, **kwargs):
        """Gate on OAUTH_ENABLED, then make sure DOT runs with the current settings."""
        from django.http import JsonResponse

        from . import settings_inject

        if not oauth_enabled():
            return JsonResponse({"error": "not_found"}, status=404)

        settings_inject.ensure_applied()
        return super().dispatch(request, *args, **kwargs)
