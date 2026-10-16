"""MCP server for InvenTree"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

from django.core.validators import MinValueValidator
from django.urls import reverse_lazy
from plugin import InvenTreePlugin
from plugin.mixins import (
    AppMixin,
    ScheduleMixin,
    SettingsMixin,
    UrlsMixin,
    UserInterfaceMixin,
)

from . import PLUGIN_VERSION
from ._well_known_compat import WellKnownMixin
from .auth import facade_supported, oauth_enabled

logger = logging.getLogger("inventree")

# Plugin pages (get_ui_routes) arrived after InvenTree 1.5.7 (core PR #12900). Without them the navigation
# entries would link to pages that do not exist, so they are only offered when the host supports routes.
UI_ROUTES_SUPPORTED = hasattr(UserInterfaceMixin, "get_ui_routes")

#: (message, args) already logged by _log_once() in this process.
_logged_once: set[tuple] = set()


def _log_once(level: int, message: str, *args) -> None:
    """Log *message* once per process: the UI feature API calls the UI methods on every page load."""
    key = (message, *args)
    if key in _logged_once:
        return
    _logged_once.add(key)
    logger.log(level, message, *args)


class InvenTreeMCP(
    WellKnownMixin,
    AppMixin,
    ScheduleMixin,
    SettingsMixin,
    UrlsMixin,
    UserInterfaceMixin,
    InvenTreePlugin,
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
            "description": "Block every write tool (create_part, update_part, delete_part, set_part_image, create/update/delete_part_category, add_stock, stock_add_quantity, stock_remove_quantity, stock_transfer, delete_stock_item, create/update/delete_stock_location), regardless of the calling user's permissions and MCP roles. While on, MCP write roles are not granted.",
            "validator": bool,
            "default": True,
        },
        "MCP_LOG_TOOL_CALLS": {
            "name": "Log Tool Calls",
            "description": "Log every MCP tool call (tool name, arguments, calling user, and outcome) to the 'inventree' logger for debugging.",
            "validator": bool,
            "default": False,
        },
        "MCP_FILTER_TOOL_LIST": {
            "name": "Hide Unavailable Tools",
            "description": "List only the tools the calling user may use (tools/list). Off: every tool is listed, and a call the user may not make fails with a permission error.",
            "validator": bool,
            "default": False,
        },
        "OAUTH_ENABLED": {
            "name": "Enable OAuth Sign-in",
            "description": "Serve OAuth discovery and the OAuth endpoints under /plugin/inventree-mcp/, so MCP clients such as Claude can connect by signing in through the browser. Changes DOT token lifetimes for the whole instance (see the lifetime settings). InvenTree 1.5.x also needs the reverse-proxy rewrites from the plugin README for /.well-known/ discovery.",
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
            "validator": [int, MinValueValidator(1)],
            "default": 3600,
        },
        "REFRESH_TOKEN_LIFETIME_DAYS": {
            "name": "Refresh Token Lifetime (days)",
            "description": "Idle lifetime of OAuth refresh tokens. Applies to every OAuth application on this instance.",
            "validator": [int, MinValueValidator(1)],
            "default": 30,
        },
        "AUTH_CODE_LIFETIME_SECONDS": {
            "name": "Authorization Code Lifetime (seconds)",
            "description": "Lifetime of OAuth authorization codes. Applies to every OAuth application on this instance.",
            "validator": [int, MinValueValidator(1)],
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
            "validator": [int, MinValueValidator(0)],
            "default": 30,
        },
        "TOKEN_RATE_LIMIT_PER_MINUTE": {
            "name": "Token Rate Limit",
            "description": "Maximum authorization code exchanges per minute and client IP at the OAuth token endpoint (0 disables the limit). Refresh grants are not limited.",
            "validator": [int, MinValueValidator(0)],
            "default": 60,
        },
        "UNUSED_CLIENT_RETENTION_DAYS": {
            "name": "Unused Client Retention (days)",
            "description": "Dynamically registered clients without a live token are deleted after this many days.",
            "validator": [int, MinValueValidator(1)],
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
            "description": "allauth provider id used for MCP sign-in and membership re-checks. Empty selects the single configured OpenID Connect provider.",
            "default": "",
        },
        "MEMBERSHIP_ENFORCED": {
            "name": "Enforce SSO Membership",
            "description": "Re-check MCP users' group membership at the SSO provider and block users who lost it. Users without an upstream refresh token (password logins, no offline_access) must sign in again after the re-login interval. Off: no re-checks and no new blocks; existing blocks still apply.",
            "validator": bool,
            "default": True,
        },
        "MEMBERSHIP_RECHECK_INTERVAL_MINUTES": {
            "name": "Membership Re-check Interval (minutes)",
            "description": "How old the last membership check may be before a token refresh re-checks at the SSO provider.",
            "validator": [int, MinValueValidator(1)],
            "default": 60,
        },
        "MEMBERSHIP_GRACE_HOURS": {
            "name": "Membership Grace (hours)",
            "description": "While the SSO provider is unreachable, refreshes keep working for this long after the last successful check.",
            "validator": [int, MinValueValidator(0)],
            "default": 4,
        },
        "RELOGIN_INTERVAL_HOURS": {
            "name": "Re-login Interval (hours)",
            "description": "Without an upstream refresh token, refreshes keep working for this long after the last login to InvenTree.",
            "validator": [int, MinValueValidator(1)],
            "default": 24,
        },
        "MCP_REQUIRED_GROUPS": {
            "name": "Required Groups",
            "description": "Comma separated InvenTree group names; when set, a user must be in at least one of them to use MCP.",
            "default": "",
        },
        "MCP_SUPERUSER_FULL_ACCESS": {
            "name": "Superusers Bypass MCP Roles",
            "description": "Superusers get every MCP scope regardless of the MCP roles of their groups. Off: superusers are limited by the MCP roles like everybody else.",
            "validator": bool,
            "default": False,
        },
        "GOOGLE_API_KEY": {
            "name": "Google API Key",
            "description": "Google Cloud API key with the Custom Search API enabled, for the search_part_images tool. Empty: the tool reports that image search is not configured.",
            "default": "",
            "protected": True,
        },
        "GOOGLE_CSE_ID": {
            "name": "Google Custom Search Engine ID",
            "description": "ID of a Google Programmable Search Engine configured for image search, for the search_part_images tool.",
            "default": "",
        },
        "MCP_ALLOW_BASIC_AUTH": {
            "name": "Allow Basic Authentication",
            "description": "Accept username and password (HTTP Basic) on the MCP endpoint. Off: only OAuth and InvenTree API tokens.",
            "validator": bool,
            "default": False,
        },
    }

    # Scheduled tasks (from ScheduleMixin)
    # Ref: https://docs.inventree.org/en/latest/plugins/mixins/schedule/
    SCHEDULED_TASKS: ClassVar[dict] = {
        "cleanup_stale_clients": {
            "func": "inventree_mcp.auth.tasks.cleanup_stale_clients",
            "schedule": "D",
        },
        "membership_sweep": {
            "func": "inventree_mcp.auth.tasks.membership_sweep",
            "schedule": "H",
        },
    }

    def __init__(self):
        """Initialise the plugin and inject the OAuth façade's DOT settings.

        Runs in every process that loads the plugin registry (web, worker,
        management commands). The signal receivers keep the settings in sync
        afterwards, including in django-q worker processes (see settings_inject),
        and stamp logins for the membership re-check (auth/signals.py).
        """
        super().__init__()

        from .auth import membership, signals

        signals.connect_signals()
        membership.warn_if_unenforceable()

        if facade_supported():
            from .auth import settings_inject

            settings_inject.connect_signals()
            try:
                settings_inject.apply()
            except Exception:
                # The database may not be ready yet (e.g. during migrations);
                # views, the challenge and the task hook re-apply on first use.
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

        # The redirect patterns are built at URL load (request=None) and must
        # cover a façade switched on later; the /.well-known/ index (a request)
        # lists the documents only while they are served.
        if facade_supported() and (request is None or oauth_enabled()):
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

    # User interface (from UserInterfaceMixin); the bundles are built from frontend/ into static/
    # Ref: https://docs.inventree.org/en/latest/plugins/mixins/ui/
    def _ui_source(self, source: str) -> str | None:
        """URL of a built bundle ("File.js:exportedFunction"), or None (with a warning) when it is missing.

        A missing build must not break the UI feature API, which serves every plugin.
        """
        file_name = source.partition(":")[0]
        if not (Path(__file__).parent / "static" / file_name).is_file():
            _log_once(
                logging.WARNING,
                "inventree-mcp: %s is missing, run 'npm ci && npm run build' in frontend/",
                file_name,
            )
            return None
        try:
            return self.plugin_static_file(source, check_exists=False)
        except Exception:
            logger.warning("inventree-mcp: cannot resolve %s", source, exc_info=True)
            return None

    @staticmethod
    def _is_staff_admin(request) -> bool:
        """The condition of the plugin's staff API (auth.api.IsStaffAdmin), for its UI entries."""
        from .auth.policy import is_staff_admin

        return is_staff_admin(getattr(request, "user", None))

    def get_ui_routes(self, request, context, **kwargs):
        """Pages: the group permission editor (staff) and the personal connections page."""
        routes = []
        groups = self._ui_source("Groups.js:renderGroupsPage")
        if groups and self._is_staff_admin(request):
            routes.append({
                "key": "mcp-groups-route",
                "title": "MCP Access",
                "source": groups,
                "options": {"path": "groups"},
            })
        connections = self._ui_source("Connections.js:renderConnectionsPage")
        if connections and request.user.is_authenticated:
            routes.append({
                "key": "mcp-connections-route",
                "title": "MCP",
                "source": connections,
                "options": {"path": "connections"},
            })
        return routes

    def get_ui_navigation_items(self, request, context, **kwargs):
        """Navigation entries for the two pages (only where the host serves plugin routes)."""
        items = []
        if not UI_ROUTES_SUPPORTED:
            _log_once(
                logging.INFO,
                "inventree-mcp: this InvenTree has no plugin routes, no navigation items",
            )
            return items
        if self._is_staff_admin(request):
            items.append({
                "key": "mcp-groups",
                "title": "MCP Access",
                "icon": "ti:plug-connected",
                "options": {"url": f"plugin/{self.SLUG}/groups"},
            })
        if request.user.is_authenticated:
            items.append({
                "key": "mcp-connections",
                "title": "MCP",
                "icon": "ti:plug-connected",
                "options": {"url": f"plugin/{self.SLUG}/connections"},
            })
        return items

    def get_ui_panels(self, request, context, **kwargs):
        """The "MCP Roles" panel on a group (entry point for the core group drawer)."""
        context = context or {}
        if context.get("target_model") != "group" or not self._is_staff_admin(request):
            return []
        source = self._ui_source("Panel.js:renderMCPRolesPanel")
        if not source:
            return []
        return [
            {
                "key": "mcp-roles",
                "title": "MCP Roles",
                "icon": "ti:plug-connected",
                "source": source,
            }
        ]
