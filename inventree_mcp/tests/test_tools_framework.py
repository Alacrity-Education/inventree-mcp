"""The tool framework: tools/_common.py helpers, annotations, ACCESS, coercion, error texts."""

from __future__ import annotations

import json
import unittest
from typing import ClassVar
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import override_settings
from InvenTree.unit_test import InvenTreeTestCase
from mcp.server.mcpserver.exceptions import ToolError
from part.api import PartDetail, PartList
from plugin import registry
from stock.api import StockList

from .. import context, proxy, tool_visibility
from ..mcp_server import mcp
from ..proxy import call_view, error_message
from ..tools import _common
from ..tools._common import (
    ACCESS,
    CATEGORY_FIELDS,
    DEFAULTS,
    LOCATION_FIELDS,
    MAX_LIMIT,
    PART_FIELDS,
    STOCK_ITEM_FIELDS,
    access,
    changes,
    clean,
    compact,
    fetch,
    listing,
    text,
    tool,
)
from .test_transport import MCPToolTestCase

#: The read tools of T1, with their titles.
READ_TOOLS = {
    "search_parts": "Search parts",
    "get_part": "Get part",
    "list_parts": "List parts",
    "search_part_categories": "Search part categories",
    "list_part_categories": "List part categories",
    "get_stock": "List stock items",
    "get_stock_item": "Get stock item",
    "search_stock_locations": "Search stock locations",
    "get_stock_location": "Get stock location",
    "list_stock_locations": "List stock locations",
}
#: Tools without an InvenTree view, hence without an ACCESS entry.
NO_ACCESS = {"search_part_images"}


def registered_tools() -> dict:
    return {t.name: t for t in async_to_sync(mcp.list_tools)()}


class ToolDecoratorTest(unittest.TestCase):
    """tool() is the one place titles and annotations are set."""

    def _register(self, name: str, **kwargs):
        @tool(name, title="Probe", **kwargs)
        async def probe(value: int = 0) -> dict:
            """First line
            second line.
            """
            return {"value": value}

        self.addCleanup(mcp.remove_tool, name)
        return registered_tools()[name]

    def test_read_tool_sets_title_and_all_four_hints(self):
        info = self._register("_probe_read", read_only=True)
        self.assertEqual(info.title, "Probe")
        self.assertEqual(info.description, "First line second line.")
        hints = info.annotations
        self.assertEqual(hints.title, "Probe")
        self.assertIs(hints.read_only_hint, True)
        self.assertIs(hints.destructive_hint, False)
        self.assertIs(hints.idempotent_hint, True)
        self.assertIs(hints.open_world_hint, False)
        self.assertIsNone(info.output_schema)  # unstructured: no output schema

    def test_write_tool_hints(self):
        info = self._register(
            "_probe_write", read_only=False, destructive=False, idempotent=True
        )
        hints = info.annotations
        self.assertIs(hints.read_only_hint, False)
        self.assertIs(hints.destructive_hint, False)
        self.assertIs(hints.idempotent_hint, True)
        self.assertIs(hints.open_world_hint, False)

    def test_write_tool_defaults_are_the_mcp_defaults(self):
        hints = self._register("_probe_delete", read_only=False).annotations
        self.assertIs(hints.destructive_hint, True)
        self.assertIs(hints.idempotent_hint, False)

    def test_open_world(self):
        hints = self._register(
            "_probe_open", read_only=True, open_world=True
        ).annotations
        self.assertIs(hints.open_world_hint, True)


class RegistryTest(unittest.TestCase):
    """The registered tools, their annotations and their ACCESS entries."""

    def test_read_tools_are_registered_with_titles_and_read_hints(self):
        tools = registered_tools()
        for name, title in READ_TOOLS.items():
            with self.subTest(tool=name):
                self.assertIn(name, tools)
                info = tools[name]
                self.assertEqual(info.title, title)
                self.assertIs(info.annotations.read_only_hint, True)
                self.assertIs(info.annotations.destructive_hint, False)
                self.assertIs(info.annotations.idempotent_hint, True)
                self.assertIs(info.annotations.open_world_hint, False)
                self.assertTrue(info.description)
                self.assertNotIn("\n", info.description)

    def test_every_tool_has_an_access_entry(self):
        """A tool added without access() fails here (search_part_images is exempt)."""
        registered = set(registered_tools())
        missing = registered - set(ACCESS) - NO_ACCESS
        self.assertEqual(missing, set(), "tools without an access() declaration")
        unknown = set(ACCESS) - registered
        self.assertEqual(unknown, set(), "access() entries without a tool")
        self.assertEqual(set(ACCESS) & NO_ACCESS, set())

    def test_access_entries_resolve_and_use_http_methods(self):
        for name, (loader, method) in ACCESS.items():
            with self.subTest(tool=name):
                self.assertIsNotNone(loader())
                self.assertIn(method, {"GET", "POST", "PATCH", "PUT", "DELETE"})
        for name in READ_TOOLS:
            self.assertEqual(ACCESS[name][1], "GET")

    def test_go_defaults_in_the_input_schemas(self):
        tools = registered_tools()
        expected = {
            "search_parts": 25,
            "list_parts": 50,
            "search_part_categories": 25,
            "list_part_categories": 100,
            "get_stock": 50,
            "search_stock_locations": 25,
            "list_stock_locations": 100,
        }
        for name, default in expected.items():
            with self.subTest(tool=name):
                schema = tools[name].input_schema
                self.assertEqual(schema["properties"]["limit"]["default"], default)
        self.assertEqual(tools["get_part"].input_schema["required"], ["id"])
        self.assertEqual(tools["search_parts"].input_schema["required"], ["search"])
        self.assertEqual(
            set(tools["get_stock"].input_schema["properties"]),
            {"part", "location", "limit", "offset"},
        )


class HelperTest(unittest.TestCase):
    """compact(), clean(), text() and listing()'s limit handling."""

    def test_compact_keeps_order_and_fills_none(self):
        obj = {"name": "R1", "pk": 3, "extra": True}
        result = compact(obj, ("pk", "name", "description"))
        self.assertEqual(list(result), ["pk", "name", "description"])
        self.assertEqual(result, {"pk": 3, "name": "R1", "description": None})

    def test_compact_reduces_part_detail(self):
        obj = {
            "pk": 1,
            "part_detail": {"pk": 2, "name": "R", "full_name": "R | A", "IPN": "x"},
        }
        result = compact(obj, ("pk", "part_detail"))
        self.assertEqual(
            result["part_detail"], {"pk": 2, "name": "R", "full_name": "R | A"}
        )
        self.assertIsNone(compact({"pk": 1}, ("part_detail",))["part_detail"])

    def test_field_sets_are_the_contract(self):
        """The four tuples, copied literally from docs/plans/tools/00-overview.md section 3."""
        self.assertEqual(
            PART_FIELDS,
            (
                "pk", "name", "full_name", "description", "category", "category_name",
                "active", "in_stock", "total_in_stock", "minimum_stock", "units", "IPN",
                "keywords", "link", "purchaseable", "assembly", "component", "trackable",
                "virtual", "image", "thumbnail", "tags",
            ),
        )  # fmt: skip
        self.assertEqual(
            STOCK_ITEM_FIELDS,
            (
                "pk", "part", "quantity", "serial", "batch", "location", "in_stock",
                "status", "status_text", "notes", "updated", "tags", "part_detail",
            ),
        )  # fmt: skip
        self.assertEqual(
            LOCATION_FIELDS,
            (
                "pk", "name", "description", "parent", "pathstring", "level", "items",
                "sublocations", "structural", "external", "icon", "tags",
            ),
        )  # fmt: skip
        self.assertEqual(
            CATEGORY_FIELDS,
            (
                "pk", "name", "description", "parent", "pathstring", "level",
                "part_count", "subcategories", "starred", "structural", "icon",
                "default_location",
            ),
        )  # fmt: skip
        self.assertEqual(DEFAULTS, {"search": 25, "list": 50, "tree": 100})
        self.assertEqual(MAX_LIMIT, 500)

    def test_clean_omission_rules(self):
        payload = clean(
            name="R1",
            description="",
            category=0,
            parent=0,
            location=0,
            default_location=0,
            part=0,
            minimum_stock=0,
            active=False,
            purchaseable=None,
            quantity=0,
            offset=0,
        )
        self.assertEqual(
            payload, {"name": "R1", "active": False, "quantity": 0, "offset": 0}
        )

    def test_clean_keeps_set_values(self):
        payload = clean(category=4, parent=2, minimum_stock=5, virtual=True, IPN="X")
        self.assertEqual(
            payload,
            {
                "category": 4,
                "parent": 2,
                "minimum_stock": 5,
                "virtual": True,
                "IPN": "X",
            },
        )

    def test_text(self):
        self.assertEqual(
            text("Part 12 deleted successfully."), "Part 12 deleted successfully."
        )

    def test_changes_cleans_and_refuses_an_empty_payload(self):
        self.assertEqual(
            changes(name="R2", description="", category=0, active=False),
            {"name": "R2", "active": False},
        )
        for empty in ({}, {"name": "", "category": 0, "minimum_stock": 0, "IPN": None}):
            with self.subTest(kwargs=empty), self.assertRaises(ToolError) as cm:
                changes(**empty)
            self.assertEqual(str(cm.exception), "no fields to update")

    def _fetch(self, response, **kwargs):
        calls = []

        async def fake_call_view(view_cls, method, path, **kw):
            calls.append((view_cls, method, path, kw))
            return response

        with patch.object(
            _common, "call_view", new=AsyncMock(side_effect=fake_call_view)
        ):
            result = async_to_sync(fetch)(
                lambda: PartDetail, fields=("pk", "name"), **kwargs
            )
        return result, calls

    def test_fetch_calls_the_view_and_compacts(self):
        result, calls = self._fetch(
            {"pk": 4, "name": "R", "extra": 1},
            method="PATCH",
            path="/api/part/4/",
            data={"name": "R"},
            pk=4,
        )
        self.assertEqual(result, {"pk": 4, "name": "R"})
        self.assertEqual(
            calls,
            [
                (
                    PartDetail,
                    "PATCH",
                    "/api/part/4/",
                    {"data": {"name": "R"}, "query_params": None, "pk": 4},
                )
            ],
        )

    def test_fetch_reduces_a_list_response_to_its_first_item(self):
        result, _calls = self._fetch(
            [{"pk": 7, "name": "first"}, {"pk": 8, "name": "second"}],
            method="POST",
            path="/api/stock/",
            data={"part": 1, "quantity": 2},
        )
        self.assertEqual(result, {"pk": 7, "name": "first"})

    def test_fetch_refuses_an_empty_list_response(self):
        with self.assertRaises(ToolError) as cm:
            self._fetch([], method="POST", path="/api/stock/", data={"part": 1})
        self.assertEqual(str(cm.exception), "no stock items returned")

    def _listing_query(self, limit: int, default: int, **params) -> dict:
        calls = []

        async def fake_call_view(view_cls, method, path, *, query_params=None, **kw):
            calls.append((method, path, query_params))
            return {
                "count": 1,
                "next": "http://x/?offset=1",
                "previous": None,
                "results": [{"pk": 1, "name": "A", "other": 1}],
            }

        with patch.object(
            _common, "call_view", new=AsyncMock(side_effect=fake_call_view)
        ):
            result = async_to_sync(listing)(
                lambda: PartList,
                "/api/part/",
                params,
                limit=limit,
                default=default,
                fields=("pk", "name"),
            )
        self.assertEqual(result, {"count": 1, "results": [{"pk": 1, "name": "A"}]})
        [(method, path, query)] = calls
        self.assertEqual((method, path), ("GET", "/api/part/"))
        return query

    def test_listing_defaults_and_clamps_the_limit(self):
        self.assertEqual(self._listing_query(0, 25)["limit"], 25)
        self.assertEqual(self._listing_query(-3, DEFAULTS["tree"])["limit"], 100)
        self.assertEqual(self._listing_query(7, 25)["limit"], 7)
        self.assertEqual(self._listing_query(MAX_LIMIT + 1, 25)["limit"], MAX_LIMIT)

    def test_listing_cleans_params(self):
        query = self._listing_query(5, 50, category=0, search="", offset=-4)
        self.assertEqual(query, {"limit": 5, "offset": 0})
        query = self._listing_query(5, 50, category=3, offset=10)
        self.assertEqual(query, {"category": 3, "limit": 5, "offset": 10})


class ErrorMessageTest(unittest.TestCase):
    """proxy.error_message(): the Go server's error texts."""

    def test_status_texts(self):
        self.assertEqual(
            error_message(401, {"detail": "x"}), "authentication failed (401)"
        )
        self.assertEqual(
            error_message(403, {"detail": "nope"}),
            "permission denied (403): your MCP roles or your InvenTree permissions "
            "do not allow this",
        )
        self.assertEqual(
            error_message(404, {"detail": "No Part matches the given query."}),
            "not found (404): resource does not exist",
        )

    def test_validation_lines(self):
        body = {
            "name": ["This field is required."],
            "quantity": ["Must be positive", "Must be a number"],
            "non_field_errors": ["Part is locked"],
            "items": [{}, {"quantity": ["Quantity must be greater than zero"]}],
            "detail": "Bad",
        }
        self.assertEqual(
            error_message(400, body),
            "validation failed (400): name: This field is required.\n"
            "quantity: Must be positive; Must be a number\n"
            "Part is locked\n"
            "items[1].quantity: Quantity must be greater than zero\n"
            "Bad",
        )

    def test_validation_of_a_list_body(self):
        self.assertEqual(
            error_message(400, ["Stock item is serialized"]),
            "validation failed (400): Stock item is serialized",
        )

    def test_other_status_and_truncation(self):
        self.assertEqual(error_message(500, ""), "API error 500 (empty response)")
        self.assertEqual(
            error_message(405, {"detail": 'Method "PUT" not allowed.'}),
            'API error 405: {"detail": "Method \\"PUT\\" not allowed."}',
        )
        message = error_message(502, "x" * 2000)
        self.assertEqual(message, "API error 502: " + "x" * 500 + "...")
        message = error_message(400, {"name": ["y" * 2000]})
        self.assertTrue(message.endswith("..."))
        self.assertLessEqual(len(message), len("validation failed (400): ") + 503)

    def test_credentials_are_redacted(self):
        secret = "0123456789abcdef0123456789abcdef"
        message = error_message(
            500, f"upstream said Token {secret} and Bearer {secret[::-1]}."
        )
        self.assertNotIn(secret, message)
        self.assertNotIn(secret[::-1], message)
        self.assertIn("Token [REDACTED]", message)
        self.assertIn("Bearer [REDACTED]", message)

    def test_ordinary_words_are_not_redacted(self):
        """Review finding 4: validation text mentioning "Basic"/"token" stays intact."""
        message = error_message(
            400, {"name": ["Basic units are not valid; token expired"]}
        )
        self.assertEqual(
            message,
            "validation failed (400): name: Basic units are not valid; token expired",
        )
        self.assertEqual(
            error_message(400, {"units": ["Basic units are not valid"]}),
            "validation failed (400): units: Basic units are not valid",
        )


class ViewSetActionsTest(unittest.TestCase):
    """Review finding 3: proxy._viewset_actions() maps every tool method on a ViewSet."""

    def test_viewset_mapping(self):
        from rest_framework.viewsets import GenericViewSet

        class FakeViewSet(GenericViewSet):
            pass

        actions = proxy._viewset_actions
        self.assertEqual(actions(FakeViewSet, "GET", {}), {"get": "list"})
        self.assertEqual(actions(FakeViewSet, "POST", {}), {"post": "create"})
        self.assertEqual(actions(FakeViewSet, "GET", {"pk": 1}), {"get": "retrieve"})
        self.assertEqual(
            actions(FakeViewSet, "PATCH", {"pk": 1}), {"patch": "partial_update"}
        )
        self.assertEqual(
            actions(FakeViewSet, "DELETE", {"pk": 1}), {"delete": "destroy"}
        )
        self.assertIsNone(actions(StockList, "POST", {}))


@override_settings(PLUGIN_TESTING_SETUP=True)
class CallViewErrorTest(InvenTreeTestCase):
    """The texts call_view() raises for real InvenTree failures, and the read-only gate."""

    roles: ClassVar[list[str]] = ["part.view", "stock.view", "stock.add"]

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        registry.reload_plugins(full_reload=True, collect=True)
        registry.set_plugin_state("inventree-mcp", True)
        cls.outsider = get_user_model().objects.create_user(
            username="no-roles", password="no-roles-password"
        )

    def bind(self, user) -> None:
        # Called inside the async tests (their own context): unbind by value.
        context.set_current_user(user)
        self.addCleanup(context.set_current_user, None)

    async def test_403_for_a_user_without_the_role(self):
        self.bind(self.outsider)
        with self.assertRaises(ToolError) as cm:
            await call_view(PartList, "GET", "/api/part/", query_params={"limit": 1})
        self.assertEqual(
            str(cm.exception),
            "permission denied (403): your MCP roles or your InvenTree permissions "
            "do not allow this",
        )

    async def test_404_for_a_missing_pk(self):
        self.bind(self.user)
        with self.assertRaises(ToolError) as cm:
            await call_view(PartDetail, "GET", "/api/part/999999/", pk=999_999)
        self.assertEqual(str(cm.exception), "not found (404): resource does not exist")

    async def test_400_for_an_invalid_payload(self):
        self.bind(self.user)

        def _disable_read_only():
            plugin = registry.get_plugin("inventree-mcp")
            plugin.set_setting("MCP_READ_ONLY", False)
            return plugin

        plugin = await sync_to_async(_disable_read_only)()
        self.addCleanup(plugin.set_setting, "MCP_READ_ONLY", True)

        with self.assertRaises(ToolError) as cm:
            await call_view(StockList, "POST", "/api/stock/", data={"quantity": "x"})
        message = str(cm.exception)
        self.assertTrue(message.startswith("validation failed (400): "), message)
        self.assertIn("part: Valid part must be supplied", message)

    async def test_read_only_gate_text(self):
        self.bind(self.user)
        with self.assertRaises(ToolError) as cm:
            await call_view(StockList, "POST", "/api/stock/", data={"part": 1})
        self.assertEqual(
            str(cm.exception),
            "This MCP server is running in read-only mode; write actions are disabled. "
            "An administrator must disable the 'Read Only' plugin setting to allow them.",
        )

    async def test_user_has_access_uses_the_method(self):
        self.bind(self.user)
        self.assertTrue(await proxy.user_has_access(StockList, "GET"))
        self.assertTrue(await proxy.user_has_access(StockList, "POST"))
        self.assertFalse(await proxy.user_has_access(PartList, "POST"))


@override_settings(PLUGIN_TESTING_SETUP=True)
class WireTest(MCPToolTestCase):
    """Argument coercion and error texts over the real MCP endpoint."""

    roles: ClassVar[list[str]] = ["part.view", "stock.view"]
    fixtures: ClassVar[list[str]] = ["category", "part", "location"]

    def _probe(self):
        seen = {}

        @tool("_coercion_probe", title="Probe", read_only=True)
        async def probe(
            id: int = 0, limit: int = 0, active: bool | None = None
        ) -> dict:
            """Coercion probe."""
            seen.update(id=id, limit=limit, active=active)
            return {"ok": True}

        self.addCleanup(mcp.remove_tool, "_coercion_probe")
        return seen

    def test_string_arguments_are_coerced(self):
        seen = self._probe()
        self.assertEqual(
            self.tool_json(
                "_coercion_probe", {"id": "1", "limit": "5", "active": "true"}
            ),
            {"ok": True},
        )
        self.assertEqual(seen, {"id": 1, "limit": 5, "active": True})
        self.assertIs(type(seen["id"]), int)
        self.assertIs(type(seen["limit"]), int)
        self.assertIs(seen["active"], True)

    def test_string_id_and_limit_reach_the_real_tools(self):
        part = self.tool_json("get_part", {"id": "1"})
        self.assertEqual(part["pk"], 1)
        listed = self.tool_json("list_parts", {"limit": "5"})
        self.assertEqual(len(listed["results"]), 5)

    def test_invalid_argument_is_a_tool_error(self):
        message = self.tool_error("get_part", {"id": "one"})
        self.assertIn("get_part", message)

    def test_404_text_over_the_wire(self):
        message = self.tool_error("get_part", {"id": 999_999})
        self.assertEqual(
            message,
            "Error executing tool get_part: not found (404): resource does not exist",
        )

    def test_403_text_over_the_wire(self):
        message = self.tool_error("list_stock_locations")
        self.assertIn("permission denied (403)", message)

    def test_results_are_indented_json_text(self):
        result = self.call_tool("get_part", {"id": 1})
        self.assertNotIn("structuredContent", result)
        [content] = result["content"]
        self.assertEqual(content["type"], "text")
        self.assertEqual(
            content["text"],
            json.dumps(json.loads(content["text"]), indent=2, ensure_ascii=False),
        )


@override_settings(PLUGIN_TESTING_SETUP=True)
class VisibilityTest(InvenTreeTestCase):
    """tool_visibility.visible_tool_names() and call_view()'s fail-closed edges.

    Restores the tool-independent cases of the old suite (no bound identity, anonymous
    identity, unresolvable view, call_view(None), no bound user) and checks that
    visibility uses each tool's declared HTTP method (ACCESS).
    """

    roles: ClassVar[list[str]] = ["part.view", "stock.view"]

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        registry.reload_plugins(full_reload=True, collect=True)
        registry.set_plugin_state("inventree-mcp", True)

    def bind(self, user) -> None:
        context.set_current_user(user)
        self.addCleanup(context.set_current_user, None)

    async def all_names(self) -> set[str]:
        return {t.name for t in await mcp.list_tools()}

    async def test_a_no_bound_identity_returns_every_tool_unfiltered(self):
        context.set_current_user(None)
        names = await self.all_names()
        self.assertEqual(await tool_visibility.visible_tool_names(names), names)

    async def test_b_anonymous_identity_sees_only_tools_without_access(self):
        self.bind(AnonymousUser())
        names = await self.all_names()
        self.assertEqual(
            await tool_visibility.visible_tool_names(names), names - set(ACCESS)
        )

    async def test_c_unresolvable_view_hides_its_tool_without_crashing(self):
        self.bind(self.user)
        with patch.dict(ACCESS, {"list_parts": (lambda: None, "GET")}):
            visible = await tool_visibility.visible_tool_names(await self.all_names())
        self.assertNotIn("list_parts", visible)
        self.assertIn("get_part", visible)

    def _register_post_probe(self) -> None:
        @tool("_probe_post", title="Probe", read_only=False, destructive=False)
        async def probe() -> str:
            """POST probe."""
            return "ok"

        access("_probe_post", lambda: StockList, "POST")
        self.addCleanup(mcp.remove_tool, "_probe_post")
        self.addCleanup(ACCESS.pop, "_probe_post", None)

    async def test_d_visibility_uses_the_declared_method(self):
        self._register_post_probe()
        self.bind(self.user)  # stock.view only
        visible = await tool_visibility.visible_tool_names(await self.all_names())
        self.assertIn("get_stock", visible)
        self.assertNotIn("_probe_post", visible)

        writer = await sync_to_async(self._stock_writer)()
        self.bind(writer)
        visible = await tool_visibility.visible_tool_names(await self.all_names())
        self.assertIn("_probe_post", visible)
        self.assertIn("get_stock", visible)

    def _stock_writer(self):
        from django.contrib.auth.models import Group

        group = Group.objects.create(name="mcp-stock-writers")
        # assignRole() saves the rulesets (a queryset update skips the permission sync).
        self.assignRole("stock.view", group=group)
        self.assignRole("stock.add", group=group)
        writer = get_user_model().objects.create_user(
            username="stock-writer", password="stock-writer-password"
        )
        writer.groups.add(group)
        return writer

    async def test_e_call_view_without_a_view_class_is_unavailable(self):
        self.bind(self.user)
        with self.assertRaises(ToolError) as cm:
            await call_view(None, "GET", "/api/part/")
        self.assertIn("unavailable", str(cm.exception))

    async def test_f_call_view_without_a_bound_user_fails_closed(self):
        context.set_current_user(None)
        with self.assertRaises(PermissionError):
            await call_view(PartList, "GET", "/api/part/", query_params={"limit": 1})
