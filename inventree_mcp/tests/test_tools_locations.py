"""Stock location tools over the real MCP endpoint.

T1 holds the read tool tests (search_stock_locations, get_stock_location,
list_stock_locations); the write tool tests (create/update/delete_stock_location) are added
by T3.
"""

from __future__ import annotations

from typing import ClassVar

from django.test import override_settings
from stock.models import StockLocation

from ..tools._common import LOCATION_FIELDS
from .test_transport import MCPToolTestCase

NOT_FOUND = "not found (404): resource does not exist"


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
