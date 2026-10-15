"""MCP server for InvenTree"""

import logging
from typing import ClassVar

from django.urls import reverse_lazy
from plugin import InvenTreePlugin
from plugin.mixins import AppMixin, ScheduleMixin, SettingsMixin, UrlsMixin

from . import PLUGIN_VERSION
from ._well_known_compat import WellKnownMixin
from .auth import facade_supported

logger = logging.getLogger("inventree")


class InvenTreeMCP(
    WellKnownMixin, AppMixin, ScheduleMixin, SettingsMixin, UrlsMixin, InvenTreePlugin
):
    """InvenTreeMCP - custom InvenTree plugin."""

    # Plugin metadata
    TITLE = "InvenTree MCP"
    NAME = "InvenTreeMCP"
    SLUG = "inventree-mcp"
    DESCRIPTION = "MCP server for InvenTree"
    VERSION = PLUGIN_VERSION

    # Additional project information
    AUTHOR = "Oliver Walters"
    WEBSITE = "https://github.com/inventree/inventree-mcp"
    LICENSE = "MIT"

    # Supported InvenTree versions
    MIN_VERSION = "1.5.0"

    # Plugin settings (from SettingsMixin)
    # Ref: https://docs.inventree.org/en/latest/plugins/mixins/settings/
    SETTINGS: ClassVar[dict] = {
        "REQUIRE_AUTH": {
            "name": "Require Authentication",
            "description": "Reject unauthenticated requests to the MCP endpoint. Disable only for local testing.",
            "validator": bool,
            "default": True,
        },
        "MCP_READ_ONLY": {
            "name": "Read Only",
            "description": "Block all write actions via the MCP endpoint, regardless of the calling user's permissions.",
            "validator": bool,
            "default": True,
        },
        "MCP_LOG_TOOL_CALLS": {
            "name": "Log Tool Calls",
            "description": "Log every MCP tool call (tool name, arguments, calling user, and outcome) to the 'inventree' logger for debugging.",
            "validator": bool,
            "default": False,
        },
        "OAUTH_ENABLED": {
            "name": "Enable OAuth Sign-in",
            "description": "Serve OAuth discovery and the OAuth endpoints under /plugin/inventree-mcp/, so MCP clients such as Claude can connect by signing in through the browser. Changes DOT token lifetimes for the whole instance (see the lifetime settings).",
            "validator": bool,
            "default": False,
        },
        "TRUSTED_CLIENT_HOSTS": {
            "name": "Trusted Client Hosts",
            "description": "Comma separated hosts whose client ID metadata documents (https client_id URLs) are accepted, in Django ALLOWED_HOSTS syntax ('.example.com' includes subdomains).",
            "default": "claude.ai,claude.com,.anthropic.com",
        },
        "TRUSTED_CLIENT_ORIGINS": {
            "name": "Trusted Client Origins",
            "description": "Comma separated browser origins allowed to call the OAuth register, token and revoke endpoints (CORS).",
            "default": "https://claude.ai,https://claude.com",
        },
        "AUTO_APPROVE_TRUSTED_CLIENTS": {
            "name": "Auto-approve Trusted Clients",
            "description": "Skip the consent page for clients from a trusted host, unless they redirect to a loopback address.",
            "validator": bool,
            "default": True,
        },
        "ACCESS_TOKEN_LIFETIME_SECONDS": {
            "name": "Access Token Lifetime (seconds)",
            "description": "Lifetime of OAuth access tokens. Applies to every OAuth application on this instance.",
            "validator": int,
            "default": 3600,
        },
        "REFRESH_TOKEN_LIFETIME_DAYS": {
            "name": "Refresh Token Lifetime (days)",
            "description": "Idle lifetime of OAuth refresh tokens. Applies to every OAuth application on this instance.",
            "validator": int,
            "default": 30,
        },
        "AUTH_CODE_LIFETIME_SECONDS": {
            "name": "Authorization Code Lifetime (seconds)",
            "description": "Lifetime of OAuth authorization codes. Applies to every OAuth application on this instance.",
            "validator": int,
            "default": 600,
        },
        "ALLOW_LOCALHOST_LOOPBACK": {
            "name": "Allow localhost Loopback",
            "description": "Treat http://localhost redirect URIs like 127.0.0.1 (any port), as used by Claude Code.",
            "validator": bool,
            "default": True,
        },
        "REGISTER_RATE_LIMIT_PER_MINUTE": {
            "name": "Registration Rate Limit",
            "description": "Maximum client registrations per minute and client IP (0 disables the limit).",
            "validator": int,
            "default": 30,
        },
        "UNUSED_CLIENT_RETENTION_DAYS": {
            "name": "Unused Client Retention (days)",
            "description": "Dynamically registered clients without a live token are deleted after this many days.",
            "validator": int,
            "default": 90,
        },
        "OAUTH_FORCE_SSO": {
            "name": "Force SSO for MCP Sign-in",
            "description": "Send the browser straight to the single sign-on provider when an MCP client connects.",
            "validator": bool,
            "default": True,
        },
        "SSO_PROVIDER_ID": {
            "name": "SSO Provider ID",
            "description": "allauth provider id used for MCP sign-in. Empty selects the single configured OpenID Connect provider.",
            "default": "",
        },
    }

    # Scheduled tasks (from ScheduleMixin)
    # Ref: https://docs.inventree.org/en/latest/plugins/mixins/schedule/
    SCHEDULED_TASKS: ClassVar[dict] = {
        "cleanup_stale_clients": {
            "func": "inventree_mcp.auth.tasks.cleanup_stale_clients",
            "schedule": "D",
        },
    }

    def __init__(self):
        """Initialise the plugin and inject the OAuth façade's DOT settings."""
        super().__init__()

        if facade_supported():
            try:
                from .auth import settings_inject

                settings_inject.apply()
            except Exception:  # noqa: BLE001 - the database may not be ready yet; views re-apply on first use
                logger.debug(
                    "inventree-mcp: DOT settings not injected at load", exc_info=True
                )
        else:
            logger.warning(
                "inventree-mcp: django-oauth-toolkit < 3.4, the OAuth façade is not available"
            )

    # Custom URL endpoints (from UrlsMixin)
    # Ref: https://docs.inventree.org/en/latest/plugins/mixins/urls/
    def setup_urls(self):
        """Configure custom URL endpoints for this plugin."""
        from .mcp_transport import urlpatterns as mcp_urlpatterns
        from .server_card import urlpatterns as server_card_urlpatterns

        urlpatterns = mcp_urlpatterns + server_card_urlpatterns

        if facade_supported():
            from .auth.urls import urlpatterns as auth_urlpatterns

            urlpatterns += auth_urlpatterns

        return urlpatterns

    # Well-known URLs (from WellKnownMixin, if the running InvenTree provides it)
    # Ref: https://github.com/inventree/inventree-mcp/issues/33
    def get_well_known_urls(self, request=None):
        """Advertise the MCP Server Card and, with the OAuth façade, its metadata documents.

        Core answers /.well-known/<name> and /.well-known/<name>/<anything>
        with a redirect, which covers RFC 9728's and RFC 8414's path-insertion
        forms (e.g. /.well-known/oauth-protected-resource/plugin/inventree-mcp/mcp/).
        """
        urls = [("mcp-server-card", reverse_lazy(f"plugin:{self.slug}:server-card"))]

        if facade_supported():
            urls += [
                (
                    "oauth-protected-resource",
                    reverse_lazy(f"plugin:{self.slug}:oauth-protected-resource"),
                ),
                (
                    "oauth-authorization-server",
                    reverse_lazy(f"plugin:{self.slug}:oauth-authorization-server"),
                ),
            ]

        return urls
