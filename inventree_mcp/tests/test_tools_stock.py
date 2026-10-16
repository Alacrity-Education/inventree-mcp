"""Stock item tools over the real MCP endpoint.

T1 holds the read tool tests (get_stock, get_stock_item); the write tool tests (add_stock,
stock_add_quantity, stock_remove_quantity, stock_transfer, delete_stock_item) are added by T3.
"""

from __future__ import annotations

from typing import ClassVar

from django.test import override_settings
from stock.models import StockItem

from ..tools._common import STOCK_ITEM_FIELDS
from .test_transport import MCPToolTestCase

NOT_FOUND = "not found (404): resource does not exist"
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
