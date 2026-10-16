"""Stock location tools over the real MCP endpoint.

T1 holds the read tool tests (search_stock_locations, get_stock_location,
list_stock_locations); the write tool tests (create/update/delete_stock_location) are added
by T3.
"""

from __future__ import annotations

from typing import ClassVar

from django.test import override_settings
from part.models import Part
from stock.models import StockItem, StockLocation

from ..tools._common import LOCATION_FIELDS
from .test_transport import MCPToolTestCase

NOT_FOUND = "not found (404): resource does not exist"
FORBIDDEN = (
    "permission denied (403): your MCP roles or your InvenTree permissions "
    "do not allow this"
)
READ_ONLY_TEXT = (
    "Error executing tool {name}: This MCP server is running in read-only mode; "
    "write actions are disabled. An administrator must disable the 'Read Only' "
    "plugin setting to allow them."
)
LOCATION_WRITE_TOOLS = {
    "create_stock_location": ("Create stock location", False, False),
    "update_stock_location": ("Update stock location", False, True),
    "delete_stock_location": ("Delete stock location", True, False),
}
LOCATION_WRITE_CALLS = {
    "create_stock_location": {"name": "MCP Gate"},
    "update_stock_location": {"id": 1, "name": "MCP Gate"},
    "delete_stock_location": {"id": 1},
}


@override_settings(PLUGIN_TESTING_SETUP=True)
class LocationReadToolsTest(MCPToolTestCase):
    roles: ClassVar[list[str]] = ["stock_location.view"]

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Created through the ORM (fixtures leave pathstring empty).
        cls.office = StockLocation.objects.create(name="MCP Office")
        cls.drawers = [
            StockLocation.objects.create(name=f"MCP Drawer_{n}", parent=cls.office)
            for n in (1, 2, 3)
        ]
        home = StockLocation.objects.create(name="MCP Home")
        StockLocation.objects.create(name="MCP Bathroom", parent=home)

    def test_search_stock_locations(self):
        body = self.tool_json("search_stock_locations", {"search": "mcp drawer"})
        self.assertEqual(list(body), ["count", "results"])
        self.assertEqual(
            {loc["name"] for loc in body["results"]},
            {"MCP Drawer_1", "MCP Drawer_2", "MCP Drawer_3"},
        )
        self.assertEqual(body["count"], 3)
        drawer = body["results"][0]
        self.assertEqual(tuple(drawer), LOCATION_FIELDS)
        self.assertTrue(drawer["pathstring"].startswith("MCP Office/MCP Drawer_"))

        limited = self.tool_json(
            "search_stock_locations", {"search": "mcp drawer", "limit": 1}
        )
        self.assertEqual(len(limited["results"]), 1)
        self.assertEqual(limited["count"], 3)

    def test_get_stock_location(self):
        drawer = self.drawers[0]
        body = self.tool_json("get_stock_location", {"id": drawer.pk})
        self.assertEqual(tuple(body), LOCATION_FIELDS)
        self.assertEqual(body["pk"], drawer.pk)
        self.assertEqual(body["name"], "MCP Drawer_1")
        self.assertEqual(body["parent"], self.office.pk)
        self.assertEqual(body["pathstring"], "MCP Office/MCP Drawer_1")
        self.assertEqual(body["level"], 1)

        self.assertTrue(
            self.tool_error("get_stock_location", {"id": 999_999}).endswith(NOT_FOUND)
        )

    def test_list_stock_locations(self):
        body = self.tool_json("list_stock_locations")
        total = StockLocation.objects.count()
        self.assertEqual(body["count"], total)
        self.assertEqual(len(body["results"]), min(total, 100))  # default limit 100
        self.assertEqual(tuple(body["results"][0]), LOCATION_FIELDS)

        children = self.tool_json("list_stock_locations", {"parent": self.office.pk})
        self.assertEqual(
            {loc["name"] for loc in children["results"]},
            {"MCP Drawer_1", "MCP Drawer_2", "MCP Drawer_3"},
        )
        self.assertEqual({loc["sublocations"] for loc in children["results"]}, {0})

        first = self.tool_json("list_stock_locations", {"limit": 3})
        second = self.tool_json("list_stock_locations", {"limit": 3, "offset": 3})
        self.assertEqual(len(first["results"]), 3)
        self.assertFalse(
            {loc["pk"] for loc in first["results"]}
            & {loc["pk"] for loc in second["results"]}
        )


@override_settings(PLUGIN_TESTING_SETUP=True)
class LocationWriteToolsTest(MCPToolTestCase):
    roles: ClassVar[list[str]] = [
        "stock_location.view",
        "stock_location.add",
        "stock_location.change",
        "stock_location.delete",
    ]

    def setUp(self):
        super().setUp()
        self.set_setting("MCP_READ_ONLY", False)
        self.parent = StockLocation.objects.create(name="MCP Parent")

    def test_tools_list_titles_and_annotations(self):
        tools = self.list_tools()
        for name, (title, destructive, idempotent) in LOCATION_WRITE_TOOLS.items():
            hints = tools[name]["annotations"]
            self.assertEqual(tools[name]["title"], title)
            self.assertFalse(hints["readOnlyHint"], name)
            self.assertEqual(hints["destructiveHint"], destructive, name)
            self.assertEqual(hints["idempotentHint"], idempotent, name)
            self.assertFalse(hints["openWorldHint"], name)
            self.assertNotIn("outputSchema", tools[name])
        self.assertEqual(
            tools["create_stock_location"]["inputSchema"]["required"], ["name"]
        )
        self.assertEqual(
            tools["update_stock_location"]["inputSchema"]["required"], ["id"]
        )

    def test_create_stock_location(self):
        body = self.tool_json(
            "create_stock_location",
            {
                "name": "MCP Child",
                "description": "made by test",
                "parent": str(self.parent.pk),
                "structural": "true",
            },
        )
        self.assertEqual(tuple(body), LOCATION_FIELDS)
        self.assertEqual(body["name"], "MCP Child")
        self.assertEqual(body["description"], "made by test")
        self.assertEqual(body["parent"], self.parent.pk)
        self.assertEqual(body["pathstring"], "MCP Parent/MCP Child")
        self.assertTrue(body["structural"])
        location = StockLocation.objects.get(pk=body["pk"])
        self.assertEqual(location.parent_id, self.parent.pk)
        self.assertTrue(location.structural)
        # Read back through the read tool.
        again = self.tool_json("get_stock_location", {"id": body["pk"]})
        self.assertEqual(again["pathstring"], "MCP Parent/MCP Child")

    def test_create_stock_location_omits_unset_fields(self):
        body = self.tool_json(
            "create_stock_location",
            {"name": "MCP Top", "description": "", "parent": 0},
        )
        self.assertIsNone(body["parent"])
        self.assertFalse(body["structural"])
        self.assertEqual(body["pathstring"], "MCP Top")

    def test_create_validation_errors(self):
        error = self.tool_error("create_stock_location", {"parent": self.parent.pk})
        self.assertIn("name", error)  # missing argument
        error = self.tool_error(
            "create_stock_location", {"name": "X", "parent": 999_999}
        )
        self.assertIn("validation failed (400): parent: ", error)
        # Duplicate name under the same parent.
        StockLocation.objects.create(name="MCP Dup", parent=self.parent)
        error = self.tool_error(
            "create_stock_location", {"name": "MCP Dup", "parent": self.parent.pk}
        )
        self.assertIn("validation failed (400): ", error)

    def test_update_stock_location(self):
        child = StockLocation.objects.create(name="MCP Old", parent=self.parent)
        other = StockLocation.objects.create(name="MCP Other")
        body = self.tool_json(
            "update_stock_location",
            {"id": child.pk, "name": "MCP New", "description": "renamed"},
        )
        self.assertEqual(tuple(body), LOCATION_FIELDS)
        self.assertEqual(body["name"], "MCP New")
        self.assertEqual(body["description"], "renamed")
        self.assertEqual(body["parent"], self.parent.pk)  # parent 0 = unchanged
        # Move under another parent.
        body = self.tool_json(
            "update_stock_location", {"id": child.pk, "parent": other.pk}
        )
        self.assertEqual(body["pathstring"], "MCP Other/MCP New")
        child.refresh_from_db()
        self.assertEqual((child.name, child.parent_id), ("MCP New", other.pk))

    def test_update_without_fields_fails(self):
        for arguments in (
            {"id": self.parent.pk},
            {"id": self.parent.pk, "name": "", "description": "", "parent": 0},
        ):
            self.assertTrue(
                self.tool_error("update_stock_location", arguments).endswith(
                    "no fields to update"
                )
            )

    def test_update_404_and_validation(self):
        self.assertTrue(
            self.tool_error(
                "update_stock_location", {"id": 999_999, "name": "Nope"}
            ).endswith(NOT_FOUND)
        )
        # A location cannot become its own parent.
        error = self.tool_error(
            "update_stock_location", {"id": self.parent.pk, "parent": self.parent.pk}
        )
        self.assertIn("validation failed (400): ", error)

    def test_delete_stock_location(self):
        location = StockLocation.objects.create(name="MCP Gone")
        result = self.call_tool("delete_stock_location", {"id": str(location.pk)})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(
            result["content"][0]["text"],
            f"Location {location.pk} deleted successfully.",
        )
        self.assertFalse(StockLocation.objects.filter(pk=location.pk).exists())
        self.assertTrue(
            self.tool_error("delete_stock_location", {"id": location.pk}).endswith(
                NOT_FOUND
            )
        )

    def test_delete_404(self):
        self.assertTrue(
            self.tool_error("delete_stock_location", {"id": 999_999}).endswith(
                NOT_FOUND
            )
        )

    def test_delete_does_not_enforce_empty_on_1_5_7(self):
        # The Go description says "must be empty", but InvenTree's delete moves sub-locations
        # and stock to the parent when both delete flags are false, which the tool sends.
        child = StockLocation.objects.create(name="MCP Kept", parent=self.parent)
        item = StockItem.objects.create(
            part=Part.objects.create(name="MCP Loc Part", description="x"),
            quantity=1,
            location=self.parent,
        )
        result = self.call_tool("delete_stock_location", {"id": self.parent.pk})
        self.assertFalse(result.get("isError"), result)
        child.refresh_from_db()
        item.refresh_from_db()
        self.assertIsNone(child.parent_id)
        self.assertIsNone(item.location_id)


@override_settings(PLUGIN_TESTING_SETUP=True)
class LocationWriteGatesTest(MCPToolTestCase):
    roles: ClassVar[list[str]] = ["stock_location.view"]

    def test_read_only_gate(self):
        # MCP_READ_ONLY is on by default.
        for name, arguments in LOCATION_WRITE_CALLS.items():
            with self.subTest(tool=name):
                self.assertEqual(
                    self.tool_error(name, arguments), READ_ONLY_TEXT.format(name=name)
                )
        self.assertTrue(set(LOCATION_WRITE_TOOLS) <= self.list_tool_names())

    def test_missing_inventree_role_is_forbidden(self):
        self.set_setting("MCP_READ_ONLY", False)
        location = StockLocation.objects.create(name="MCP Guarded")
        calls = {
            "create_stock_location": {"name": "MCP Gate"},
            "update_stock_location": {"id": location.pk, "name": "MCP Gate"},
            "delete_stock_location": {"id": location.pk},
        }
        for name, arguments in calls.items():
            with self.subTest(tool=name):
                self.assertTrue(self.tool_error(name, arguments).endswith(FORBIDDEN))
        location.refresh_from_db()
        self.assertEqual(location.name, "MCP Guarded")
