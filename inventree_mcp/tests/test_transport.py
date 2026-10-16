"""Transport, logging, settings, server card, view resolution and read-only gate tests.

Salvaged from the original suite (test_mcp.py, removed with the old tools in 0.5.0), with
the same assertions adapted to the new tool names. Also home of the transport helper
(MCPClientMixin, MCPToolTestCase) the tool tests use to call tools over the real HTTP
endpoint.
"""

from __future__ import annotations

import datetime
import importlib
import json
import unittest
from typing import Any, ClassVar
from unittest.mock import patch

from asgiref.sync import async_to_sync, sync_to_async
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from InvenTree.unit_test import InvenTreeTestCase
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types.version import SUPPORTED_PROTOCOL_VERSIONS
from oauth2_provider.models import AccessToken, Application
from part.api import PartList
from plugin import registry
from stock.api import StockList
from users.models import ApiToken

from .. import PLUGIN_VERSION, context, view_resolution
from .. import settings as plugin_settings
from ..mcp_server import mcp
from ..proxy import call_view
from ..server_card import SERVER_CARD_SCHEMA, build_server_card
from ..settings import get_plugin_setting
from ..tools._common import ACCESS
from .oauth_helpers import _raw_api_token, grant_mcp

MCP_URL = "/plugin/inventree-mcp/mcp/"


class MCPClientMixin:
    """Call the real MCP endpoint (Client(enforce_csrf_checks=True), API token auth).

    Needs ``self.api_token`` (a raw API token).
    """

    URL = MCP_URL
    api_token: ClassVar[str]

    def mcp_post(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: int = 1,
        client: Client | None = None,
        **headers: Any,
    ) -> Any:
        """POST one JSON-RPC request; API token auth unless *headers* carry another."""
        headers.setdefault("HTTP_AUTHORIZATION", f"Token {self.api_token}")
        return (client or Client(enforce_csrf_checks=True)).post(
            self.URL,
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }),
            content_type="application/json",
            HTTP_ACCEPT="application/json, text/event-stream",
            **headers,
        )

    def list_tool_names(self, **headers: Any) -> set[str]:
        """Names from tools/list."""
        response = self.mcp_post("tools/list", request_id=2, **headers)
        self.assertEqual(response.status_code, 200, response.content)
        return {tool["name"] for tool in response.json()["result"]["tools"]}

    def list_tools(self, **headers: Any) -> dict[str, dict]:
        """The tools/list entries by name."""
        response = self.mcp_post("tools/list", request_id=2, **headers)
        self.assertEqual(response.status_code, 200, response.content)
        return {tool["name"]: tool for tool in response.json()["result"]["tools"]}

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, **headers: Any
    ) -> dict:
        """tools/call; returns the JSON-RPC result (a CallToolResult)."""
        response = self.mcp_post(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            request_id=3,
            **headers,
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["result"]

    def tool_json(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """A successful tool call's JSON text, parsed."""
        result = self.call_tool(name, arguments)
        self.assertFalse(result.get("isError"), result)
        return json.loads(result["content"][0]["text"])

    def tool_error(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """A failed tool call's error text."""
        result = self.call_tool(name, arguments)
        self.assertTrue(result.get("isError"), result)
        return result["content"][0]["text"]


@override_settings(PLUGIN_TESTING_SETUP=True)
class MCPToolTestCase(MCPClientMixin, InvenTreeTestCase):
    """Plugin active, the test group holding every MCP role, an API token for cls.user.

    Subclasses set ``roles`` (the interactive roles) and ``fixtures``.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.api_token = _raw_api_token(
            ApiToken.objects.create(user=cls.user, name="mcp-tool-test-token")
        )
        registry.reload_plugins(full_reload=True, collect=True)
        registry.set_plugin_state("inventree-mcp", True)
        # MCP access needs MCP roles (WP3); the interactive roles still decide.
        grant_mcp(cls.group, "all")

    def set_setting(self, key: str, value: Any) -> None:
        """Set a plugin setting for this test (restored to its default afterwards)."""
        plugin = registry.get_plugin("inventree-mcp")
        previous = plugin.get_setting(key)
        plugin.set_setting(key, value)
        self.addCleanup(plugin.set_setting, key, previous)


@override_settings(PLUGIN_TESTING_SETUP=True)
class MCPTransportTest(MCPClientMixin, InvenTreeTestCase):
    """HTTP-level regression tests for mcp_transport.py.

    Everything here was originally verified by hand (curl + the real mcp
    client SDK) while tracking down three separate bugs - see AGENTS.md's
    OAuth2 section (auth_exempt requirement, SessionAuthentication/CSRF
    interaction, request.body caching).

    Must use Client(enforce_csrf_checks=True): Django's test Client disables
    CSRF checking entirely by default, which would silently mask the
    SessionAuthentication/CSRF bug this class specifically guards against.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.api_token = _raw_api_token(
            ApiToken.objects.create(user=cls.user, name="transport-test-token")
        )

        app, _ = Application.objects.get_or_create(
            name="mcp-transport-test-app",
            defaults={
                "client_type": "confidential",
                "authorization_grant_type": "client-credentials",
                "user": cls.user,
            },
        )
        cls.oauth2_token = AccessToken.objects.create(
            user=cls.user,
            application=app,
            token="mcp-transport-test-token",
            expires=timezone.now() + datetime.timedelta(hours=1),
            scope="g:read",
        ).token

        registry.reload_plugins(full_reload=True, collect=True)
        registry.set_plugin_state("inventree-mcp", True)
        # MCP access needs MCP roles (WP3); the interactive roles still decide.
        grant_mcp(cls.group, "all")

    def _post(self, client: Client | None = None, **headers) -> Any:
        """An initialize request with exactly *headers* (no default credential)."""
        headers.setdefault("HTTP_AUTHORIZATION", None)
        if headers["HTTP_AUTHORIZATION"] is None:
            del headers["HTTP_AUTHORIZATION"]
        return (client or Client(enforce_csrf_checks=True)).post(
            self.URL,
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0.1"},
                },
            }),
            content_type="application/json",
            HTTP_ACCEPT="application/json, text/event-stream",
            **headers,
        )

    def test_unauthenticated_request_rejected_by_default(self):
        """REQUIRE_AUTH defaults to True - no credential must be cleanly rejected, not crash."""
        response = self._post()

        self.assertEqual(response.status_code, 401)
        self.assertIn("error", json.loads(response.content))

    def test_invalid_credential_rejected(self):
        """A malformed/unknown token must be rejected cleanly, not crash the view."""
        response = self._post(HTTP_AUTHORIZATION="Token not-a-real-token")

        self.assertEqual(response.status_code, 401)

    def test_session_only_auth_is_rejected(self):
        """SessionAuthentication is deliberately excluded (see AGENTS.md) - a logged-in
        browser session alone, with no Token/Basic/OAuth2 credential, must not
        authenticate this endpoint.
        """
        client = Client(enforce_csrf_checks=True)
        client.login(username=self.username, password=self.password)

        response = self._post(client=client)

        self.assertEqual(response.status_code, 401)

    def test_api_token_auth_succeeds(self):
        response = self._post(HTTP_AUTHORIZATION=f"Token {self.api_token}")

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(body["result"]["serverInfo"]["name"], "InvenTree MCP")
        self.assertEqual(body["result"]["serverInfo"]["version"], PLUGIN_VERSION)

    def test_oauth2_bearer_auth_succeeds(self):
        """Regression guard for three separate bugs at once - see class docstring.

        Any one of them regressing changes this specific outcome:
        - auth_exempt missing -> 401 before dispatch() ever runs.
        - SessionAuthentication not excluded -> 403 "CSRF Failed: CSRF cookie not set."
        - request.body not cached before initialize_request() -> 500 RawPostDataException.
        """
        response = self._post(HTTP_AUTHORIZATION=f"Bearer {self.oauth2_token}")

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(body["result"]["serverInfo"]["name"], "InvenTree MCP")

    def test_hop_by_hop_response_headers_are_stripped(self):
        """Regression test for _HOP_BY_HOP_HEADERS in mcp_transport.py.

        The ASGI session manager can emit hop-by-hop headers (RFC 7230 6.1) -
        e.g. "Connection" - as part of its response, which WSGI servers like
        the stdlib `runserver` reject outright since only the server itself
        is allowed to control connection handling. Patches
        StreamableHTTPSessionManager.handle_request directly (rather than
        relying on the real MCP protocol to happen to emit one).
        """

        async def fake_handle_request(self, scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"connection", b"keep-alive"),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"jsonrpc": "2.0", "id": 1, "result": {}}',
            })

        with patch(
            "mcp.server.streamable_http_manager.StreamableHTTPSessionManager"
            ".handle_request",
            new=fake_handle_request,
        ):
            response = self._post(HTTP_AUTHORIZATION=f"Token {self.api_token}")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Connection", response)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_tools_list_over_http_is_filtered_by_permission(self):
        """End-to-end regression test for tool_visibility.apply()'s wiring into
        the real low-level server: the override must reach a real client's
        tools/list request over the wire.

        cls.user has zero roles here (MCPTransportTest.roles is never set -
        see UserMixin's default), so only the tools without an ACCESS entry
        (no InvenTree view) may appear once MCP_FILTER_TOOL_LIST is on (off
        by default, see the next test).
        """
        registry.get_plugin("inventree-mcp").set_setting("MCP_FILTER_TOOL_LIST", True)
        self.addCleanup(
            registry.get_plugin("inventree-mcp").set_setting,
            "MCP_FILTER_TOOL_LIST",
            False,
        )
        names = self.list_tool_names()

        self.assertNotIn("list_parts", names)
        self.assertNotIn("get_stock", names)
        self.assertNotIn("search_stock_locations", names)
        self.assertEqual(names & set(ACCESS), set())

    def test_tools_list_over_http_shows_every_tool_by_default(self):
        """MCP_FILTER_TOOL_LIST off (the default): a zero-role caller still sees
        every registered tool; the call itself is refused with a permission
        error.
        """
        registry.get_plugin("inventree-mcp").set_setting("MCP_FILTER_TOOL_LIST", False)
        names = self.list_tool_names()

        registered = {tool.name for tool in async_to_sync(mcp.list_tools)()}
        self.assertEqual(names, registered)
        self.assertIn("list_parts", names)
        self.assertIn("get_stock", names)

        error = self.tool_error("list_parts")
        self.assertIn("permission denied (403)", error)


@override_settings(PLUGIN_TESTING_SETUP=True)
class ToolLoggingTest(MCPToolTestCase):
    """Verify MCP_LOG_TOOL_CALLS gates tool_logging.apply()'s real HTTP-level hook.

    tool_logging.apply() re-registers the low-level CallToolRequest handler,
    which mcp.call_tool() deliberately bypasses - see tool_logging.py's
    module docstring - so this goes over the wire.
    """

    roles: ClassVar[list[str]] = ["part.view"]

    def _enable_logging(self) -> None:
        self.set_setting("MCP_LOG_TOOL_CALLS", True)

    def test_tool_call_is_not_logged_by_default(self):
        """MCP_LOG_TOOL_CALLS defaults to False - logging must be fully opt-in."""
        with patch("inventree_mcp.tool_logging.logger") as mock_logger:
            result = self.call_tool("list_parts", {"limit": 1})

        self.assertFalse(result.get("isError"), result)
        mock_logger.info.assert_not_called()

    def test_successful_tool_call_is_logged_when_enabled(self):
        self._enable_logging()

        with patch("inventree_mcp.tool_logging.logger") as mock_logger:
            result = self.call_tool("list_parts", {"limit": 1})

        self.assertFalse(result.get("isError"), result)
        self.assertEqual(mock_logger.info.call_count, 2)

        start_args = mock_logger.info.call_args_list[0].args
        self.assertIn("list_parts", start_args)
        self.assertIn(self.username, start_args)

        end_args = mock_logger.info.call_args_list[1].args
        self.assertIn("succeeded", end_args[0])
        self.assertIn("list_parts", end_args)
        self.assertIn(self.username, end_args)

    def test_failed_tool_call_is_logged_when_enabled(self):
        """A ToolError (e.g. not found) must still be logged, not swallowed silently."""
        self._enable_logging()

        with patch("inventree_mcp.tool_logging.logger") as mock_logger:
            result = self.call_tool("get_part", {"id": 999_999})

        self.assertTrue(result["isError"])

        # Only the start line goes through .info - the completion line for a
        # failed call is logged at .warning instead, a deliberate severity
        # distinction so it can be filtered/alerted on separately.
        self.assertEqual(mock_logger.info.call_count, 1)
        self.assertEqual(mock_logger.warning.call_count, 1)

        end_args = mock_logger.warning.call_args_list[0].args
        self.assertIn("failed", end_args[0])
        self.assertIn("get_part", end_args)


@override_settings(PLUGIN_TESTING_SETUP=True)
class ReadOnlyGateTest(InvenTreeTestCase):
    """MCP_READ_ONLY (default on) blocks every non-GET call_view(), whatever the roles."""

    roles: ClassVar[list[str]] = ["stock.view", "stock.add", "stock.change"]

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        registry.reload_plugins(full_reload=True, collect=True)
        registry.set_plugin_state("inventree-mcp", True)

    def setUp(self):
        super().setUp()
        token = context.set_current_user(self.user)
        self.addCleanup(context.reset_current_user, token)

    async def test_read_only_setting_blocks_writes_by_default(self):
        """Even a fully-permissioned user cannot write while MCP_READ_ONLY is on (the default)."""
        with self.assertRaises(ToolError) as cm:
            await call_view(StockList, "POST", "/api/stock/", data={})

        self.assertIn("read-only", str(cm.exception).lower())

    async def test_read_only_setting_can_be_disabled(self):
        """Disabling MCP_READ_ONLY lets a write reach the real view (and its own validation)."""

        def _disable_read_only():
            plugin = registry.get_plugin("inventree-mcp")
            plugin.set_setting("MCP_READ_ONLY", False)
            return plugin

        # registry/setting lookups are sync Django ORM work - must be bridged
        # the same way proxy.call_view() bridges tool calls (see AGENTS.md).
        plugin = await sync_to_async(_disable_read_only)()
        self.addCleanup(plugin.set_setting, "MCP_READ_ONLY", True)

        with self.assertRaises(ToolError) as cm:
            await call_view(StockList, "POST", "/api/stock/", data={})

        self.assertNotIn("read-only", str(cm.exception).lower())
        self.assertTrue(str(cm.exception).startswith("validation failed (400): "))


class PluginSettingsTest(InvenTreeTestCase):
    """Verify get_plugin_setting() fails safe - both REQUIRE_AUTH and MCP_READ_ONLY
    depend on this for their fail-*closed* (i.e. restrictive) behaviour, so "can't
    resolve the setting" must never quietly mean "unrestricted".

    The mocks target the module object imported above (patch.object), not the
    dotted path: after a plugin registry reload on InvenTree 1.6+ (core PR
    #13007) the dotted path names a re-imported copy of the module, while
    get_plugin_setting() keeps using the one it was defined in.
    """

    def test_returns_default_when_plugin_unresolvable(self):
        with patch.object(plugin_settings, "_get_plugin_instance", return_value=None):
            self.assertTrue(get_plugin_setting("REQUIRE_AUTH"))
            self.assertTrue(get_plugin_setting("MCP_READ_ONLY"))
            # The default parameter itself must be respected, not hardcoded True.
            self.assertFalse(get_plugin_setting("REQUIRE_AUTH", default=False))

    def test_returns_default_when_setting_lookup_raises(self):
        class _BrokenPlugin:
            def get_setting(self, key):
                raise RuntimeError("simulated failure")

        with patch.object(
            plugin_settings, "_get_plugin_instance", return_value=_BrokenPlugin()
        ):
            self.assertTrue(get_plugin_setting("REQUIRE_AUTH"))

    def test_returns_real_value_when_plugin_resolvable(self):
        with patch.object(plugin_settings, "_get_plugin_instance") as mock_get:
            mock_get.return_value.get_setting.return_value = False
            self.assertFalse(get_plugin_setting("MCP_READ_ONLY"))

    def test_google_settings_default_empty_and_key_protected(self):
        from ..core import InvenTreeMCP

        self.assertEqual(InvenTreeMCP.SETTINGS["GOOGLE_API_KEY"]["default"], "")
        self.assertTrue(InvenTreeMCP.SETTINGS["GOOGLE_API_KEY"]["protected"])
        self.assertEqual(InvenTreeMCP.SETTINGS["GOOGLE_CSE_ID"]["default"], "")


@override_settings(PLUGIN_TESTING_SETUP=True)
class ServerCardTest(InvenTreeTestCase):
    """Regression tests for the MCP Server Card well-known endpoint (server_card.py).

    Implements SEP-2127 (https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2127)

    Tests that the card is reachable and correctly shaped, and - separately, since
    the two are independent failure modes - that it's actually wired into
    WellKnownMixin (core.py's get_well_known_urls())
    """

    URL = "/plugin/inventree-mcp/server-card/"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        registry.reload_plugins(full_reload=True, collect=True)
        registry.set_plugin_state("inventree-mcp", True)

    def test_server_card_is_reachable_without_authentication(self):
        """Discovery metadata only, no InvenTree data - unlike the real MCP
        endpoint (see MCPTransportTest.test_unauthenticated_request_rejected_by_default),
        this must not require a credential.
        """
        response = Client().get(self.URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_server_card_matches_the_sep_2127_shape(self):
        response = Client().get(self.URL)
        card = response.json()

        self.assertEqual(card["$schema"], SERVER_CARD_SCHEMA)
        # Reverse-DNS namespace + "/" + server name, per the schema's pattern.
        self.assertRegex(card["name"], r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")
        self.assertEqual(card["version"], PLUGIN_VERSION)
        # Tools are dynamic (per caller): the card never lists them.
        self.assertNotIn("tools", card)

        [remote] = card["remotes"]
        self.assertEqual(remote["type"], "streamable-http")
        self.assertTrue(remote["url"].endswith("/plugin/inventree-mcp/mcp/"))
        self.assertEqual(
            remote["supportedProtocolVersions"], list(SUPPORTED_PROTOCOL_VERSIONS)
        )

    def test_build_server_card_resolves_a_real_absolute_mcp_url(self):
        """Direct unit test of build_server_card() - the request-scoped absolute
        URL building is the one part the HTTP-level tests above don't isolate.
        """
        response = Client().get(self.URL)
        card = build_server_card(response.wsgi_request)

        self.assertTrue(card["remotes"][0]["url"].startswith("http"))
        self.assertIn("/plugin/inventree-mcp/mcp/", card["remotes"][0]["url"])

    @override_settings(
        SITE_URL="http://testserver", CSRF_TRUSTED_ORIGINS=["http://testserver"]
    )
    def test_server_card_is_discoverable_via_the_well_known_index(self):
        """End-to-end regression test for InvenTreeMCP.get_well_known_urls() -
        proves the card is reachable via InvenTree's aggregated /.well-known/
        index (plugin/urls.py's wellknownindexview), the same mechanism the
        built-in InvenTreeWellKnown plugin uses for passkey-endpoints.

        The SITE_URL/CSRF_TRUSTED_ORIGINS override matches
        InvenTreeWellKnownTest.test_well_known_urls in InvenTree core -
        wellknownindexview's request.build_absolute_uri() rejects an
        untrusted host otherwise.
        """

        import django.urls.exceptions

        try:
            response = Client().get(reverse("well-known:index"))
        except django.urls.exceptions.NoReverseMatch:
            # Exit early, this version of the InvenTree server does not have the well-known index.
            return

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("mcp-server-card", data["well_known_urls"])
        self.assertTrue(data["well_known_urls"]["mcp-server-card"].endswith(self.URL))


class WellKnownCompatTest(unittest.TestCase):
    """Regression test for _well_known_compat.py's WellKnownMixin fallback.

    WellKnownMixin was added in inventree/InvenTree#12698 - InvenTreeMCP must
    still import and load cleanly on InvenTree versions that don't provide it.
    Simulates that by deleting the real mixin from plugin.mixins and
    reloading the compat shim, rather than actually downgrading InvenTree.
    """

    def test_falls_back_to_a_blank_mixin_when_plugin_mixins_lacks_it(self):
        import plugin.mixins as plugin_mixins

        from .. import _well_known_compat

        try:
            real_mixin = plugin_mixins.WellKnownMixin
        except AttributeError:
            # Exit early - the WellKnownMixin does not exist in this version of plugin.mixins
            return

        del plugin_mixins.WellKnownMixin
        try:
            reloaded = importlib.reload(_well_known_compat)

            self.assertIsNot(reloaded.WellKnownMixin, real_mixin)
            self.assertEqual(reloaded.WellKnownMixin().get_well_known_urls(), [])
        finally:
            plugin_mixins.WellKnownMixin = real_mixin
            importlib.reload(_well_known_compat)


class ViewResolutionTest(unittest.TestCase):
    """resolve_view() must tolerate a missing/renamed view class - the case a
    mismatched InvenTree core version (relative to this plugin) produces -
    instead of letting ImportError/AttributeError escape, and must not
    re-attempt (or re-log) an import it already knows fails.
    """

    def setUp(self):
        super().setUp()
        # _unresolved is a module-level cache that deliberately persists for
        # the life of the process (see resolve_view's docstring) - tests
        # must isolate their own entries from it explicitly rather than
        # relying on it emptying itself between tests.
        before = set(view_resolution._unresolved)
        self.addCleanup(view_resolution._unresolved.clear)
        self.addCleanup(view_resolution._unresolved.update, before)

    def test_resolves_a_real_view_class(self):
        self.assertIs(view_resolution.resolve_view("part.api", "PartList"), PartList)

    def test_resolves_every_view_the_tools_use(self):
        """The view classes of section 3 of the tools plan (T3 needs StockAdd/Remove/Transfer)."""
        for module_path, names in {
            "part.api": ("PartList", "PartDetail", "CategoryList", "CategoryDetail"),
            "stock.api": (
                "StockList",
                "StockDetail",
                "StockLocationList",
                "StockLocationDetail",
                "StockAdd",
                "StockRemove",
                "StockTransfer",
            ),
        }.items():
            for name in names:
                with self.subTest(view=f"{module_path}.{name}"):
                    self.assertIsNotNone(
                        view_resolution.resolve_view(module_path, name)
                    )

    def test_returns_none_for_a_class_that_does_not_exist(self):
        self.assertIsNone(view_resolution.resolve_view("part.api", "NotARealViewClass"))

    def test_returns_none_for_a_module_that_does_not_exist(self):
        self.assertIsNone(view_resolution.resolve_view("not.a.real.module", "Whatever"))

    def test_caches_a_failure_without_re_importing(self):
        with patch.object(
            view_resolution, "import_module", side_effect=ImportError("boom")
        ) as mock_import:
            self.assertIsNone(view_resolution.resolve_view("some.fake.module", "X"))
            self.assertIsNone(view_resolution.resolve_view("some.fake.module", "X"))

        mock_import.assert_called_once()


class ResolveViewAnyTest(unittest.TestCase):
    """resolve_view_any() must fall back across a renamed-between-core-versions class."""

    def setUp(self):
        super().setUp()
        # Same isolation reasoning as ViewResolutionTest.setUp - this cache
        # persists for the life of the process (see resolve_view_any's
        # docstring), so tests must not leak entries into each other.
        before = set(view_resolution._unresolved_any)
        self.addCleanup(view_resolution._unresolved_any.clear)
        self.addCleanup(view_resolution._unresolved_any.update, before)

    def test_resolves_the_first_candidate_that_exists(self):
        self.assertIs(
            view_resolution.resolve_view_any(
                "part.api", ["PartList", "NotARealViewClass"]
            ),
            PartList,
        )

    def test_falls_back_to_a_later_candidate(self):
        self.assertIs(
            view_resolution.resolve_view_any(
                "part.api", ["NotARealViewClass", "PartList"]
            ),
            PartList,
        )

    def test_returns_none_when_no_candidate_exists(self):
        self.assertIsNone(
            view_resolution.resolve_view_any(
                "part.api", ["NotARealViewClass", "AlsoNotReal"]
            )
        )

    def test_returns_none_for_a_module_that_does_not_exist(self):
        self.assertIsNone(
            view_resolution.resolve_view_any("not.a.real.module", ["Whatever"])
        )

    def test_caches_a_total_failure_without_re_importing(self):
        with patch.object(
            view_resolution, "import_module", side_effect=ImportError("boom")
        ) as mock_import:
            self.assertIsNone(
                view_resolution.resolve_view_any("some.fake.module", ["X", "Y"])
            )
            self.assertIsNone(
                view_resolution.resolve_view_any("some.fake.module", ["X", "Y"])
            )

        mock_import.assert_called_once()
