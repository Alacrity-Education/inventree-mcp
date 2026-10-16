"""Part category tools over the real MCP endpoint.

T1 holds the read tool tests (search_part_categories, list_part_categories); the write tool
tests (create/update/delete_part_category) are added by T2.
"""

from __future__ import annotations

from typing import ClassVar

from django.test import override_settings
from part.models import PartCategory

from ..tools._common import CATEGORY_FIELDS
from .test_transport import MCPToolTestCase


@override_settings(PLUGIN_TESTING_SETUP=True)
class CategoryReadToolsTest(MCPToolTestCase):
    roles: ClassVar[list[str]] = ["part_category.view"]

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Created through the ORM (fixtures leave pathstring empty).
        cls.electronics = PartCategory.objects.create(name="MCP Electronics")
        cls.resistors = PartCategory.objects.create(
            name="MCP Resistors", parent=cls.electronics
        )
        PartCategory.objects.create(name="MCP Capacitors", parent=cls.electronics)
        ic = PartCategory.objects.create(name="MCP IC", parent=cls.electronics)
        PartCategory.objects.create(name="MCP MCU", parent=ic)

    def test_search_part_categories(self):
        body = self.tool_json("search_part_categories", {"search": "MCP Resist"})
        self.assertEqual(list(body), ["count", "results"])
        [resistors] = [c for c in body["results"] if c["name"] == "MCP Resistors"]
        self.assertEqual(tuple(resistors), CATEGORY_FIELDS)
        self.assertEqual(resistors["pk"], self.resistors.pk)
        self.assertEqual(resistors["pathstring"], "MCP Electronics/MCP Resistors")
        self.assertEqual(resistors["parent"], self.electronics.pk)
        self.assertEqual(resistors["level"], 1)

        limited = self.tool_json(
            "search_part_categories", {"search": "MCP", "limit": 2}
        )
        self.assertEqual(len(limited["results"]), 2)
        self.assertEqual(limited["count"], 5)

    def test_list_part_categories(self):
        body = self.tool_json("list_part_categories")
        total = PartCategory.objects.count()
        self.assertEqual(body["count"], total)
        self.assertEqual(len(body["results"]), min(total, 100))  # default limit 100
        self.assertEqual(tuple(body["results"][0]), CATEGORY_FIELDS)

        children = self.tool_json(
            "list_part_categories", {"parent": self.electronics.pk}
        )
        self.assertEqual(
            {c["name"] for c in children["results"]},
            {"MCP Resistors", "MCP Capacitors", "MCP IC"},
        )
        self.assertTrue(
            all(c["parent"] == self.electronics.pk for c in children["results"])
        )

        first = self.tool_json("list_part_categories", {"limit": 2})
        second = self.tool_json("list_part_categories", {"limit": 2, "offset": 2})
        self.assertEqual(len(first["results"]), 2)
        self.assertFalse(
            {c["pk"] for c in first["results"]} & {c["pk"] for c in second["results"]}
        )
