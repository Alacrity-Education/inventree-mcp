"""The allauth SSO provider MCP sign-in and the membership re-check use.

Shared by the login bridge (which provider to send the browser to) and
membership.py (whose SocialAccount / SocialToken to re-check).
"""

from __future__ import annotations

from ..settings import get_plugin_setting

OIDC_PROVIDER = "openid_connect"


def sso_app(request=None):
    """The allauth SocialApp used for MCP sign-in, or None.

    SSO_PROVIDER_ID when set, else the single configured ``openid_connect``
    app; None when SSO login is disabled (LOGIN_ENABLE_SSO) or ambiguous.
    *request* may be None (re-checks run without one).
    """
    from allauth.socialaccount.adapter import get_adapter
    from common.settings import get_global_setting

    if not get_global_setting("LOGIN_ENABLE_SSO", create=False):
        return None

    adapter = get_adapter()
    configured = get_plugin_setting("SSO_PROVIDER_ID", "")
    if configured:
        apps = [
            app
            for app in adapter.list_apps(request)
            if (app.provider_id or app.provider) == configured
        ]
    else:
        apps = adapter.list_apps(request, provider=OIDC_PROVIDER)

    return apps[0] if len(apps) == 1 else None


def provider_id_of(app) -> str:
    """The id allauth stores in ``SocialAccount.provider`` for accounts of *app*."""
    return app.provider_id or app.provider


def sso_provider_id(request=None) -> str | None:
    """allauth provider id for MCP sign-in (see sso_app()); None when there is none."""
    app = sso_app(request)
    return provider_id_of(app) if app else None
