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
import logging


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


_well_known_warning_logged = False


def warn_if_discovery_needs_proxy() -> None:
    """Log once per process when Claude-style discovery needs a reverse-proxy rewrite.

    Without core's WellKnownMixin (InvenTree 1.5.x) the plugin cannot answer
    the RFC 8414 path-insertion URL clients probe first
    (/.well-known/oauth-authorization-server/plugin/inventree-mcp), so Claude
    Code stops there unless the proxy rewrites it (see README).
    """
    global _well_known_warning_logged

    from .._well_known_compat import NATIVE_WELL_KNOWN

    if NATIVE_WELL_KNOWN or _well_known_warning_logged:
        return
    _well_known_warning_logged = True
    logging.getLogger("inventree").warning(
        "inventree-mcp: OAuth sign-in is on, but this InvenTree version cannot serve "
        "/.well-known/oauth-authorization-server/plugin/inventree-mcp; add the "
        "reverse-proxy rewrites from the plugin README (section 'Connect with OAuth') "
        "or MCP clients such as Claude Code cannot discover the authorization server"
    )


class FacadeViewMixin:
    """First base of every façade view: sync DOT settings, then 404 while the façade is off.

    Must come before DOT's view classes in the MRO, so it runs ahead of DOT's
    own dispatch (login redirect, DCR gate, ...). ensure_applied() runs before
    the OAUTH_ENABLED check, so a request that answers 404 also restores the
    DOT settings InvenTree configured once the façade was switched off.
    """

    def dispatch(self, request, *args, **kwargs):
        """Make sure DOT runs with the current settings, then gate on OAUTH_ENABLED."""
        from django.http import JsonResponse

        from . import settings_inject

        settings_inject.ensure_applied()

        if not oauth_enabled():
            return JsonResponse({"error": "not_found"}, status=404)

        warn_if_discovery_needs_proxy()
        return super().dispatch(request, *args, **kwargs)
