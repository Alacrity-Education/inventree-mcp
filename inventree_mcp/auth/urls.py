"""URL patterns of the OAuth façade and the MCP permission API, appended by core.py when facade_supported().

All façade views are auth_exempt (InvenTree's AuthRequiredMiddleware would
answer 401 for anything under /plugin/ otherwise; they decide for
themselves), and the machine endpoints are csrf_exempt (clients post without
cookies). The authorize view keeps CSRF protection: its consent form is a
session form.

The API views under api/ (auth/api.py, WP3) are plain DRF views: InvenTree's
middleware and DRF authentication apply as for /api/ (session with CSRF for
the web UI, API token), and they are served whether or not OAUTH_ENABLED is on
(the MCP roles also narrow API tokens).
"""

from __future__ import annotations

from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from InvenTree.permissions import auth_exempt

from .api import (
    ConnectionDetail,
    ConnectionList,
    EffectiveView,
    MCPRuleSetClear,
    MCPRuleSetCopy,
    MCPRuleSetDetail,
    MCPRuleSetList,
    MCPRuleSetRevoke,
    StatusView,
)
from .authorize import MCPAuthorizationView
from .clients import MCPRegistrationView
from .discovery import AuthorizationServerMetadataView, ProtectedResourceMetadataView
from .login_bridge import MCPLoginBridgeView
from .token import MCPRevokeView, MCPTokenView


def _machine(view_class):
    return csrf_exempt(auth_exempt(view_class.as_view()))


def _browser(view_class):
    return auth_exempt(view_class.as_view())


urlpatterns = [
    path(
        ".well-known/oauth-protected-resource",
        _machine(ProtectedResourceMetadataView),
        name="oauth-protected-resource",
    ),
    path(
        ".well-known/oauth-authorization-server",
        _machine(AuthorizationServerMetadataView),
        name="oauth-authorization-server",
    ),
    path(
        ".well-known/openid-configuration",
        _machine(AuthorizationServerMetadataView),
        name="openid-configuration",
    ),
    path("oauth/register/", _machine(MCPRegistrationView), name="oauth-register"),
    path("oauth/authorize/", _browser(MCPAuthorizationView), name="oauth-authorize"),
    path("oauth/token/", _machine(MCPTokenView), name="oauth-token"),
    path("oauth/revoke/", _machine(MCPRevokeView), name="oauth-revoke"),
    path("oauth/login/", _browser(MCPLoginBridgeView), name="oauth-login"),
    # MCP permission API (WP3)
    path("api/rulesets/", MCPRuleSetList.as_view(), name="api-ruleset-list"),
    path("api/rulesets/copy/", MCPRuleSetCopy.as_view(), name="api-ruleset-copy"),
    path("api/rulesets/clear/", MCPRuleSetClear.as_view(), name="api-ruleset-clear"),
    path("api/rulesets/revoke/", MCPRuleSetRevoke.as_view(), name="api-ruleset-revoke"),
    path(
        "api/rulesets/<int:pk>/",
        MCPRuleSetDetail.as_view(),
        name="api-ruleset-detail",
    ),
    path("api/effective/<int:user_pk>/", EffectiveView.as_view(), name="api-effective"),
    path("api/connections/", ConnectionList.as_view(), name="api-connection-list"),
    path(
        "api/connections/<int:pk>/",
        ConnectionDetail.as_view(),
        name="api-connection-detail",
    ),
    path("api/status/", StatusView.as_view(), name="api-status"),
]
