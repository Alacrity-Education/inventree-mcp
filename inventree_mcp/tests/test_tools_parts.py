"""Part tools over the real MCP endpoint.

T1 holds the read tool tests (search_parts, get_part, list_parts); the write tool tests
(create_part, update_part, delete_part, set_part_image, search_part_images) are added by T2.
"""

from __future__ import annotations

from typing import ClassVar

from django.test import override_settings
from part.models import Part

from ..tools._common import PART_FIELDS
from .test_transport import MCPToolTestCase

NOT_FOUND = "not found (404): resource does not exist"


@override_settings(PLUGIN_TESTING_SETUP=True)
class PartReadToolsTest(MCPToolTestCase):
    roles: ClassVar[list[str]] = ["part.view"]
    fixtures: ClassVar[list[str]] = ["category", "part", "location"]

    def test_search_parts(self):
        body = self.tool_json("search_parts", {"search": "R_2K2"})
        self.assertEqual(list(body), ["count", "results"])
        self.assertEqual(body["count"], len(body["results"]))
        names = [part["name"] for part in body["results"]]
        self.assertIn("R_2K2_0805", names)
        for part in body["results"]:
            self.assertEqual(tuple(part), PART_FIELDS)

        limited = self.tool_json("search_parts", {"search": "", "limit": 2})
        self.assertEqual(len(limited["results"]), 2)
        self.assertEqual(limited["count"], Part.objects.count())

    def test_get_part(self):
        part = Part.objects.get(name="R_2K2_0805")
        body = self.tool_json("get_part", {"id": part.pk})
        self.assertEqual(tuple(body), PART_FIELDS)
        self.assertEqual(body["pk"], part.pk)
        self.assertEqual(body["name"], "R_2K2_0805")
        self.assertEqual(body["category"], part.category_id)

        self.assertTrue(
            self.tool_error("get_part", {"id": 999_999}).endswith(NOT_FOUND)
        )

    def test_list_parts(self):
        body = self.tool_json("list_parts")
        self.assertEqual(list(body), ["count", "results"])
        total = Part.objects.count()
        self.assertEqual(body["count"], total)
        self.assertEqual(len(body["results"]), min(total, 50))
        self.assertEqual(tuple(body["results"][0]), PART_FIELDS)

        first = self.tool_json("list_parts", {"limit": 3, "offset": 0})
        second = self.tool_json("list_parts", {"limit": 3, "offset": 3})
        self.assertEqual(len(first["results"]), 3)
        self.assertEqual(first["count"], total)
        self.assertFalse(
            {p["pk"] for p in first["results"]} & {p["pk"] for p in second["results"]}
        )

        # category filters (InvenTree includes sub-categories); 0 lists all.
        resistors = self.tool_json("list_parts", {"category": 2})
        self.assertTrue(resistors["results"])
        self.assertTrue(all(p["category"] == 2 for p in resistors["results"]))
        self.assertEqual(self.tool_json("list_parts", {"category": 0})["count"], total)
