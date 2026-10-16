"""Part category tools over the real MCP endpoint.

T1 holds the read tool tests (search_part_categories, list_part_categories); T2 added the
write tool tests (create/update/delete_part_category).
"""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import patch

from django.test import override_settings
from part.models import Part, PartCategory

from ..tools._common import CATEGORY_FIELDS
from .test_transport import (
    NOT_FOUND,
    READ_ONLY,
    MCPToolTestCase,
    WriteToolTestCase,
)


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


CATEGORY_ROLES = [
    "part_category.view",
    "part_category.add",
    "part_category.change",
    "part_category.delete",
]


class CategoryWriteToolsTest(WriteToolTestCase):
    roles: ClassVar[list[str]] = [*CATEGORY_ROLES, "stock_location.view"]

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.root = PartCategory.objects.create(name="MCP Root")
        cls.child = PartCategory.objects.create(name="MCP Child", parent=cls.root)

    def test_create_part_category(self):
        body = self.tool_json(
            "create_part_category",
            {
                "name": "MCP New",
                "description": "created by a test",
                "parent": str(self.root.pk),
                "structural": "true",
            },
        )
        self.assertEqual(tuple(body), CATEGORY_FIELDS)
        self.assertEqual(body["parent"], self.root.pk)
        self.assertEqual(body["pathstring"], "MCP Root/MCP New")
        self.assertEqual(body["description"], "created by a test")
        self.assertTrue(body["structural"])
        self.assertTrue(PartCategory.objects.filter(pk=body["pk"]).exists())

    def test_create_part_category_top_level_omits_unset_fields(self):
        with patch("inventree_mcp.tools._common.call_view") as call:
            call.return_value = {}
            self.call_tool(
                "create_part_category",
                {"name": "Top", "parent": 0, "default_location": 0, "description": ""},
            )
        (_, method, path), kwargs = call.call_args
        self.assertEqual((method, path), ("POST", "/api/part/category/"))
        self.assertEqual(kwargs["data"], {"name": "Top"})

    def test_create_part_category_validation_error(self):
        message = self.tool_error(
            "create_part_category", {"name": "Bad", "parent": 999_999}
        )
        self.assertIn("validation failed (400): ", message)
        self.assertIn("parent: ", message)
        message = self.tool_error(
            "create_part_category", {"name": self.child.name, "parent": self.root.pk}
        )
        self.assertIn("validation failed (400): ", message)

    def test_update_part_category_changes_only_the_given_fields(self):
        body = self.tool_json(
            "update_part_category", {"id": self.child.pk, "description": "new text"}
        )
        self.assertEqual(tuple(body), CATEGORY_FIELDS)
        self.assertEqual(body["description"], "new text")
        self.assertEqual(body["name"], "MCP Child")
        self.assertEqual(body["parent"], self.root.pk)

        other = PartCategory.objects.create(name="MCP Other")
        body = self.tool_json(
            "update_part_category", {"id": self.child.pk, "parent": other.pk}
        )
        self.assertEqual(body["parent"], other.pk)
        self.assertEqual(body["pathstring"], "MCP Other/MCP Child")
        self.assertEqual(body["description"], "new text")

    def test_update_part_category_errors(self):
        message = self.tool_error(
            "update_part_category", {"id": self.child.pk, "name": "", "parent": 0}
        )
        self.assertTrue(message.endswith("no fields to update"), message)
        self.assertTrue(
            self.tool_error(
                "update_part_category", {"id": 999_999, "name": "x"}
            ).endswith(NOT_FOUND)
        )
        message = self.tool_error(
            "update_part_category", {"id": self.root.pk, "parent": self.child.pk}
        )
        self.assertIn("validation failed (400): ", message)

    def test_delete_part_category(self):
        category = PartCategory.objects.create(name="MCP Doomed")
        self.assertEqual(
            self.tool_text("delete_part_category", {"id": str(category.pk)}),
            f"Category {category.pk} deleted successfully.",
        )
        self.assertFalse(PartCategory.objects.filter(pk=category.pk).exists())

    def test_delete_part_category_404(self):
        self.assertTrue(
            self.tool_error("delete_part_category", {"id": 999_999}).endswith(NOT_FOUND)
        )

    def test_delete_part_category_moves_children_and_parts_to_the_parent(self):
        """The tool sends delete_child_categories / delete_parts false: InvenTree moves the
        category's sub-categories and parts to its parent instead of deleting them."""
        middle = PartCategory.objects.create(name="MCP Middle", parent=self.root)
        grandchild = PartCategory.objects.create(name="MCP Grandchild", parent=middle)
        nested = Part.objects.create(name="MCP Nested Part", description="n", category=middle)
        top_part = Part.objects.create(
            name="MCP Root Part", description="r", category=self.root
        )

        self.tool_text("delete_part_category", {"id": middle.pk})
        self.assertFalse(PartCategory.objects.filter(pk=middle.pk).exists())
        grandchild.refresh_from_db()
        nested.refresh_from_db()
        self.assertEqual(grandchild.parent_id, self.root.pk)
        self.assertEqual(nested.category_id, self.root.pk)

        # A top-level category: its contents become top-level / uncategorised.
        self.tool_text("delete_part_category", {"id": self.root.pk})
        for category in (self.child, grandchild):
            category.refresh_from_db()
            self.assertIsNone(category.parent_id)
        for part in (nested, top_part):
            part.refresh_from_db()
            self.assertIsNone(part.category_id)

    def test_tools_need_the_mcp_write_role(self):
        cases = [
            (
                "create_part_category",
                {"name": "Nope"},
                ["part_category.view", "part_category.add"],
                ["part_category.view"],
            ),
            (
                "update_part_category",
                {"id": self.child.pk, "name": "Nope"},
                ["part_category.view", "part_category.change"],
                ["part_category.view"],
            ),
            (
                "delete_part_category",
                {"id": self.child.pk},
                CATEGORY_ROLES,
                ["part_category.view", "part_category.change"],
            ),
        ]
        for name, arguments, interactive, mcp in cases:
            with self.subTest(tool=name):
                self.assert_denied_without_mcp_role(
                    name, arguments, interactive=interactive, mcp=mcp
                )
        self.assertTrue(PartCategory.objects.filter(pk=self.child.pk).exists())
        self.assertEqual(PartCategory.objects.get(pk=self.child.pk).name, "MCP Child")
        self.assertFalse(PartCategory.objects.filter(name="Nope").exists())

    def test_tools_need_the_inventree_role(self):
        """Every MCP role, only part_category.view in InvenTree: 403, filter off and on."""
        cases = {
            "create_part_category": {"name": "Nope"},
            "update_part_category": {"id": self.child.pk, "name": "Nope"},
            "delete_part_category": {"id": self.child.pk},
        }
        for name, arguments in cases.items():
            with self.subTest(tool=name):
                self.assert_denied_without_inventree_role(
                    name, arguments, interactive=["part_category.view"]
                )
        self.assertEqual(PartCategory.objects.get(pk=self.child.pk).name, "MCP Child")
        self.assertFalse(PartCategory.objects.filter(name="Nope").exists())

    def test_tools_are_listed_with_titles_and_annotations(self):
        tools = self.list_tools()
        expected = {
            "create_part_category": ("Create part category", False, False),
            "update_part_category": ("Update part category", False, True),
            "delete_part_category": ("Delete part category", True, False),
        }
        for name, (title, destructive, idempotent) in expected.items():
            with self.subTest(tool=name):
                self.assertEqual(tools[name]["title"], title)
                self.assertEqual(
                    tools[name]["annotations"],
                    {
                        "title": title,
                        "readOnlyHint": False,
                        "destructiveHint": destructive,
                        "idempotentHint": idempotent,
                        "openWorldHint": False,
                    },
                )
                self.assertNotIn("outputSchema", tools[name])


class CategoryWriteReadOnlyTest(WriteToolTestCase):
    roles: ClassVar[list[str]] = [*CATEGORY_ROLES]

    def test_every_write_tool_answers_the_read_only_text(self):
        self.set_setting("MCP_READ_ONLY", True)
        category = PartCategory.objects.create(name="MCP Kept")
        calls = {
            "create_part_category": {"name": "Blocked"},
            "update_part_category": {"id": category.pk, "name": "Blocked"},
            "delete_part_category": {"id": category.pk},
        }
        for name, arguments in calls.items():
            with self.subTest(tool=name):
                self.assertTrue(self.tool_error(name, arguments).endswith(READ_ONLY))
                self.assertIn(name, self.list_tool_names())
        self.assertEqual(PartCategory.objects.get(pk=category.pk).name, "MCP Kept")
        self.assertFalse(PartCategory.objects.filter(name="Blocked").exists())
