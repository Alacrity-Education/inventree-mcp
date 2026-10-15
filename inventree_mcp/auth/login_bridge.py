"""Login bridge: get an anonymous browser signed in and back to the authorize URL.

DOT sends anonymous users to ``login_url?next=<authorize URL>``. InvenTree's
own login page drops ``next`` (``/accounts/login/`` redirects to the SPA
without the query string), so the OAuth flow would not resume. The bridge
instead reproduces the POST the React login makes for SSO
(``ProviderLogin`` in ``src/frontend/src/functions/auth.tsx``) to allauth's
headless ``provider/redirect`` endpoint, with ``callback_url`` set to the
authorize URL: allauth sends the browser to the identity provider, creates the
session, runs InvenTree's group sync and lands back on the authorize URL.

Without a usable SSO provider (or with OAUTH_FORCE_SSO off) the bridge shows
a link to InvenTree's login page and asks the user to reconnect from the MCP
client after signing in.
"""

from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.csrf import ensure_csrf_cookie

from ..settings import get_plugin_setting
from . import FacadeViewMixin
from .authorize import render_error
from .sso import sso_provider_id

TEMPLATE = "inventree_mcp/login_bridge.html"
#: allauth headless (browser client) endpoint, mounted by InvenTree under /api/auth/.
PROVIDER_REDIRECT_URL_NAME = "browser:socialaccount:redirect_to_provider"


def _provider_name(request, provider_id: str) -> str:
    """Display name of the allauth provider, falling back to its id."""
    from allauth.socialaccount.adapter import get_adapter

    try:
        return get_adapter().get_provider(request, provider_id).name
    except Exception:  # noqa: BLE001 - cosmetic only
        return provider_id


def _is_valid_next(request, next_url: str) -> bool:
    """``next`` must be a same-host URL whose path is the façade's authorize endpoint."""
    return bool(next_url) and (
        url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
        and urlparse(next_url).path == reverse("plugin:inventree-mcp:oauth-authorize")
    )


class MCPLoginBridgeView(FacadeViewMixin, View):
    """``plugin:inventree-mcp:oauth-login``: sends the browser through SSO to ``next``."""

    http_method_names = ("get",)

    @method_decorator(ensure_csrf_cookie)
    def get(self, request, *args, **kwargs):
        """Redirect signed-in users, otherwise render the SSO form or the manual hint."""
        next_url = request.GET.get("next", "")
        if not _is_valid_next(request, next_url):
            return render_error(request, "invalid_request", status=400)

        if request.user.is_authenticated:
            return HttpResponseRedirect(next_url)

        context = {
            "mode": "manual",
            "login_url": f"/{settings.FRONTEND_URL_BASE}/login",
        }

        provider_id = (
            sso_provider_id(request)
            if get_plugin_setting("OAUTH_FORCE_SSO", True)
            else None
        )
        if provider_id:
            context.update({
                "mode": "sso",
                "provider_id": provider_id,
                "provider_name": _provider_name(request, provider_id),
                "redirect_url": reverse(PROVIDER_REDIRECT_URL_NAME),
                "callback_url": request.build_absolute_uri(next_url),
            })

        return render(request, TEMPLATE, context)
