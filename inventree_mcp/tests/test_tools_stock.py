"""Stock item tools over the real MCP endpoint.

T1 holds the read tool tests (get_stock, get_stock_item); the write tool tests (add_stock,
stock_add_quantity, stock_remove_quantity, stock_transfer, delete_stock_item) are added by T3.
"""

from __future__ import annotations

from typing import ClassVar

from django.test import override_settings
from part.models import Part
from stock.models import StockItem, StockLocation

from ..tools._common import STOCK_ITEM_FIELDS
from .test_transport import (
    DENIED,
    NOT_FOUND,
    READ_ONLY,
    MCPToolTestCase,
    WriteToolTestCase,
)

STOCK_FIXTURES = [
    "category",
    "part",
    "test_templates",
    "bom",
    "company",
    "location",
    "supplier_part",
    "stock",
]


@override_settings(PLUGIN_TESTING_SETUP=True)
class StockReadToolsTest(MCPToolTestCase):
    # part_detail is only serialized for users who may view parts.
    roles: ClassVar[list[str]] = ["stock.view", "part.view"]
    fixtures: ClassVar[list[str]] = STOCK_FIXTURES

    def test_get_stock(self):
        body = self.tool_json("get_stock")
        self.assertEqual(list(body), ["count", "results"])
        self.assertGreater(body["count"], 0)
        self.assertEqual(len(body["results"]), min(body["count"], 50))
        item = body["results"][0]
        self.assertEqual(tuple(item), STOCK_ITEM_FIELDS)
        self.assertEqual(tuple(item["part_detail"]), ("pk", "name", "full_name"))
        self.assertEqual(item["part_detail"]["pk"], item["part"])

        # Item 1 (part 1, location 3) and item 2 (part 1, location 2).
        by_part = self.tool_json("get_stock", {"part": 1})
        self.assertEqual({i["pk"] for i in by_part["results"]}, {1, 2})
        by_both = self.tool_json("get_stock", {"part": 1, "location": 3})
        self.assertEqual([i["pk"] for i in by_both["results"]], [1])

        first = self.tool_json("get_stock", {"limit": 2})
        second = self.tool_json("get_stock", {"limit": 2, "offset": 2})
        self.assertEqual(len(first["results"]), 2)
        self.assertEqual(first["count"], body["count"])
        self.assertFalse(
            {i["pk"] for i in first["results"]} & {i["pk"] for i in second["results"]}
        )

    def test_get_stock_item(self):
        item = StockItem.objects.get(pk=1)
        body = self.tool_json("get_stock_item", {"id": 1})
        self.assertEqual(tuple(body), STOCK_ITEM_FIELDS)
        self.assertEqual(body["pk"], 1)
        self.assertEqual(body["part"], item.part_id)
        self.assertEqual(body["location"], 3)
        self.assertEqual(body["quantity"], 4000)
        self.assertEqual(
            body["part_detail"],
            {
                "pk": item.part_id,
                "name": item.part.name,
                "full_name": item.part.full_name,
            },
        )

        self.assertTrue(
            self.tool_error("get_stock_item", {"id": 999_999}).endswith(NOT_FOUND)
        )


#: title, destructiveHint, idempotentHint (openWorldHint is false for all).
STOCK_WRITE_TOOLS = {
    "add_stock": ("Add stock", False, False),
    "stock_add_quantity": ("Add stock quantity", False, False),
    "stock_remove_quantity": ("Remove stock quantity", True, False),
    "stock_transfer": ("Transfer stock", False, False),
    "delete_stock_item": ("Delete stock item", True, False),
}

# Valid arguments per write tool (stock item 1 / location 1 exist in the fixtures).
STOCK_WRITE_CALLS = {
    "add_stock": {"part": 1, "quantity": 1},
    "stock_add_quantity": {"items": [{"pk": 1, "quantity": 1}]},
    "stock_remove_quantity": {"items": [{"pk": 1, "quantity": 1}]},
    "stock_transfer": {"items": [{"pk": 1, "quantity": 1}], "location": 1},
    "delete_stock_item": {"id": 1},
}


class StockWriteToolsTest(WriteToolTestCase):
    roles: ClassVar[list[str]] = [
        "stock.view",
        "stock.add",
        "stock.change",
        "stock.delete",
        "part.view",
    ]
    fixtures: ClassVar[list[str]] = STOCK_FIXTURES

    def setUp(self):
        super().setUp()
        self.part = Part.objects.get(pk=1)
        self.loc_a = StockLocation.objects.create(name="MCP Shelf A")
        self.loc_b = StockLocation.objects.create(name="MCP Shelf B")
        self.item = StockItem.objects.create(
            part=self.part, quantity=100, location=self.loc_a
        )

    def read(self, pk: int) -> StockItem:
        return StockItem.objects.get(pk=pk)

    def test_tools_list_titles_and_annotations(self):
        tools = self.list_tools()
        for name, (title, destructive, idempotent) in STOCK_WRITE_TOOLS.items():
            tool = tools[name]
            self.assertEqual(tool["title"], title)
            hints = tool["annotations"]
            self.assertFalse(hints["readOnlyHint"], name)
            self.assertEqual(hints["destructiveHint"], destructive, name)
            self.assertEqual(hints["idempotentHint"], idempotent, name)
            self.assertFalse(hints["openWorldHint"], name)
            self.assertNotIn("outputSchema", tool)
        items_schema = tools["stock_transfer"]["inputSchema"]
        self.assertEqual(set(items_schema["required"]), {"items", "location"})
        self.assertIn("items", tools["stock_add_quantity"]["inputSchema"]["required"])

    def test_add_stock(self):
        body = self.tool_json(
            "add_stock",
            {
                "part": self.part.pk,
                "quantity": "12.5",  # coerced
                "location": self.loc_a.pk,
                "batch": "B-MCP",
                "notes": "added by test",
            },
        )
        self.assertEqual(tuple(body), STOCK_ITEM_FIELDS)
        self.assertEqual(body["part"], self.part.pk)
        self.assertEqual(body["quantity"], 12.5)
        self.assertEqual(body["location"], self.loc_a.pk)
        self.assertEqual(body["batch"], "B-MCP")
        self.assertEqual(body["notes"], "added by test")
        item = self.read(body["pk"])
        self.assertEqual(item.quantity, 12.5)
        # Read back through the read tool.
        again = self.tool_json("get_stock_item", {"id": body["pk"]})
        self.assertEqual(again["quantity"], 12.5)

    def test_add_stock_minimal_omits_empty_fields(self):
        body = self.tool_json(
            "add_stock",
            {"part": self.part.pk, "quantity": 3, "location": 0, "batch": ""},
        )
        item = self.read(body["pk"])
        self.assertEqual(item.quantity, 3)
        self.assertEqual(item.batch, "")

    def test_add_stock_serial(self):
        # StockList.create() ignores "serial"; the tool sends serial_numbers.
        part = Part.objects.create(
            name="MCP Serial Part", description="trackable", trackable=True
        )
        body = self.tool_json(
            "add_stock",
            {
                "part": part.pk,
                "quantity": 1,
                "serial": "SN-77",
                "location": self.loc_a.pk,
            },
        )
        self.assertEqual(body["serial"], "SN-77")
        self.assertEqual(body["quantity"], 1)
        self.assertEqual(self.read(body["pk"]).serial, "SN-77")

    def test_add_stock_validation(self):
        error = self.tool_error("add_stock", {"part": 999_999, "quantity": 1})
        self.assertIn(
            "validation failed (400): part: Valid part must be supplied", error
        )
        error = self.tool_error("add_stock", {"quantity": 1})
        self.assertIn("part", error)  # missing argument: SDK validation error
        error = self.tool_error(
            "add_stock", {"part": self.part.pk, "quantity": 1, "location": 999_999}
        )
        self.assertIn("validation failed (400): ", error)
        self.assertIn("location", error)

    def test_stock_add_quantity(self):
        text = self.call_tool(
            "stock_add_quantity",
            {"items": [{"pk": self.item.pk, "quantity": "25"}], "notes": "plus"},
        )
        self.assertFalse(text.get("isError"), text)
        self.assertEqual(
            text["content"][0]["text"], "Stock quantity updated successfully."
        )
        self.assertEqual(self.read(self.item.pk).quantity, 125)
        self.assertEqual(
            self.tool_json("get_stock_item", {"id": self.item.pk})["quantity"], 125
        )

    def test_stock_remove_quantity(self):
        result = self.call_tool(
            "stock_remove_quantity",
            {"items": [{"pk": self.item.pk, "quantity": 40}]},
        )
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(
            result["content"][0]["text"], "Stock quantity removed successfully."
        )
        self.assertEqual(self.read(self.item.pk).quantity, 60)

    def test_stock_remove_more_than_available_clamps_to_zero(self):
        # InvenTree (1.5.7) does not reject this: take_stock() removes min(quantity, stock),
        # and the emptied item is deleted (the part's delete_on_deplete default).
        result = self.call_tool(
            "stock_remove_quantity",
            {"items": [{"pk": self.item.pk, "quantity": 1000}]},
        )
        self.assertFalse(result.get("isError"), result)
        self.assertFalse(StockItem.objects.filter(pk=self.item.pk).exists())

    def test_stock_remove_negative_quantity_fails(self):
        error = self.tool_error(
            "stock_remove_quantity",
            {"items": [{"pk": self.item.pk, "quantity": -5}]},
        )
        self.assertIn("validation failed (400): items[0].quantity: ", error)
        self.assertEqual(self.read(self.item.pk).quantity, 100)

    def test_stock_transfer(self):
        result = self.call_tool(
            "stock_transfer",
            {
                "items": [{"pk": self.item.pk, "quantity": 100}],
                "location": self.loc_b.pk,
                "notes": "move",
            },
        )
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(
            result["content"][0]["text"], "Stock transferred successfully."
        )
        moved = self.tool_json("get_stock_item", {"id": self.item.pk})
        self.assertEqual(moved["location"], self.loc_b.pk)
        self.assertEqual(moved["quantity"], 100)

    def test_stock_transfer_partial_splits_the_item(self):
        count = StockItem.objects.filter(part=self.part).count()
        result = self.call_tool(
            "stock_transfer",
            {
                "items": [{"pk": self.item.pk, "quantity": 30}],
                "location": self.loc_b.pk,
            },
        )
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(self.read(self.item.pk).quantity, 70)
        self.assertEqual(StockItem.objects.filter(part=self.part).count(), count + 1)
        split = StockItem.objects.get(part=self.part, location=self.loc_b)
        self.assertEqual(split.quantity, 30)

    def test_adjustment_validation_errors(self):
        # Missing location: SDK argument validation.
        error = self.tool_error(
            "stock_transfer", {"items": [{"pk": self.item.pk, "quantity": 1}]}
        )
        self.assertIn("location", error)
        # Unknown destination location: InvenTree's 400.
        error = self.tool_error(
            "stock_transfer",
            {"items": [{"pk": self.item.pk, "quantity": 1}], "location": 999_999},
        )
        self.assertIn("validation failed (400): location: ", error)
        # Unknown stock item pk.
        error = self.tool_error(
            "stock_add_quantity", {"items": [{"pk": 999_999, "quantity": 1}]}
        )
        self.assertIn("validation failed (400): ", error)
        self.assertIn("items[0].pk", error)
        # Empty list.
        error = self.tool_error("stock_remove_quantity", {"items": []})
        self.assertIn("validation failed (400): ", error)
        self.assertIn("items", error)
        # Wrong shape.
        error = self.tool_error("stock_add_quantity", {"items": [{"pk": "x"}]})
        self.assertIn("stock_add_quantity", error)

    def test_delete_stock_item(self):
        result = self.call_tool("delete_stock_item", {"id": str(self.item.pk)})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(
            result["content"][0]["text"],
            f"Stock item {self.item.pk} deleted successfully.",
        )
        self.assertFalse(StockItem.objects.filter(pk=self.item.pk).exists())
        self.assertTrue(
            self.tool_error("delete_stock_item", {"id": self.item.pk}).endswith(
                NOT_FOUND
            )
        )

    def test_delete_404(self):
        self.assertTrue(
            self.tool_error("delete_stock_item", {"id": 999_999}).endswith(NOT_FOUND)
        )

    def test_tools_need_the_mcp_write_role(self):
        """InvenTree roles present, MCP role missing: 403, filter off and on.

        The three adjustments are POSTs on StockAdd / StockRemove / StockTransfer, which
        InvenTree maps to the stock *add* role (overview 6c).
        """
        adjust = (["stock.view", "stock.add"], ["stock.view"])
        cases = {
            "add_stock": (["stock.view", "stock.add"], ["stock.view"]),
            "stock_add_quantity": adjust,
            "stock_remove_quantity": adjust,
            "stock_transfer": adjust,
            "delete_stock_item": (
                ["stock.view", "stock.add", "stock.change", "stock.delete"],
                ["stock.view", "stock.change"],
            ),
        }
        count = StockItem.objects.count()
        for name, (interactive, mcp) in cases.items():
            with self.subTest(tool=name):
                self.assert_denied_without_mcp_role(
                    name, STOCK_WRITE_CALLS[name], interactive=interactive, mcp=mcp
                )
        self.assertEqual(StockItem.objects.count(), count)
        item = StockItem.objects.get(pk=1)
        self.assertEqual((item.quantity, item.location_id), (4000, 3))


class StockWriteGatesTest(WriteToolTestCase):
    """Read-only gate and missing InvenTree roles (MCP roles present)."""

    roles: ClassVar[list[str]] = ["stock.view"]
    fixtures: ClassVar[list[str]] = STOCK_FIXTURES

    def test_read_only_gate(self):
        self.set_setting("MCP_READ_ONLY", True)
        for name, arguments in STOCK_WRITE_CALLS.items():
            with self.subTest(tool=name):
                self.assertEqual(
                    self.tool_error(name, arguments),
                    f"Error executing tool {name}: {READ_ONLY}",
                )
        self.assertTrue(StockItem.objects.filter(pk=1).exists())
        # The write tools are still listed.
        self.assertTrue(set(STOCK_WRITE_TOOLS) <= self.list_tool_names())

    def test_missing_inventree_role_is_forbidden(self):
        for name, arguments in STOCK_WRITE_CALLS.items():
            with self.subTest(tool=name):
                self.assertTrue(
                    self.tool_error(name, arguments).endswith(DENIED),
                    name,
                )
        self.assertTrue(StockItem.objects.filter(pk=1).exists())
