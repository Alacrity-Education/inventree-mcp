"""Part tools over the real MCP endpoint.

T1 holds the read tool tests (search_parts, get_part, list_parts); T2 added the write tool
tests (create_part, update_part, delete_part, set_part_image). The image download behind
image_url / set_part_image is tested in test_tools_images.py.
"""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import patch

from common.models import InvenTreeSetting
from django.test import override_settings
from part.models import BomItem, Part, PartCategory
from stock.models import StockItem

from ..proxy import call_view
from ..tools._common import PART_FIELDS
from .test_transport import (
    NOT_FOUND,
    READ_ONLY,
    MCPToolTestCase,
    WriteToolTestCase,
)

DOWNLOAD = "InvenTree.helpers_model.download_image_from_url"

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


PART_ROLES = ["part.view", "part.add", "part.change", "part.delete"]


class PartWriteToolsTest(WriteToolTestCase):
    # stock.view: delete_part counts stock through the stock list when the part read has
    # no stock_item_count.
    roles: ClassVar[list[str]] = [*PART_ROLES, "part_category.view", "stock.view"]
    fixtures: ClassVar[list[str]] = ["category", "part", "location"]

    def test_create_part(self):
        body = self.tool_json(
            "create_part",
            {
                "name": "MCP Widget",
                "description": "A widget",
                "category": 2,
                "IPN": "MCP-1",
                "keywords": "widget mcp",
                "units": "pcs",
                "minimum_stock": "5",
                "assembly": "true",
                "virtual": False,
            },
        )
        self.assertEqual(tuple(body), PART_FIELDS)
        part = Part.objects.get(pk=body["pk"])
        self.assertEqual(body["name"], "MCP Widget")
        self.assertEqual(body["category"], 2)
        self.assertEqual(body["IPN"], "MCP-1")
        self.assertEqual(body["minimum_stock"], 5)
        self.assertTrue(body["assembly"])
        self.assertFalse(body["virtual"])
        self.assertEqual((part.units, part.keywords), ("pcs", "widget mcp"))

    def test_create_part_minimal_omits_unset_fields(self):
        with patch("inventree_mcp.tools._common.call_view") as call:
            call.return_value = {}
            self.call_tool("create_part", {"name": "x", "category": 0})
        (_, method, path), kwargs = call.call_args
        self.assertEqual((method, path), ("POST", "/api/part/"))
        self.assertEqual(kwargs["data"], {"name": "x"})

    def test_create_part_validation_error(self):
        message = self.tool_error("create_part", {"name": "Bad", "category": 999_999})
        self.assertIn("validation failed (400): ", message)
        self.assertIn("category: ", message)
        self.assertFalse(Part.objects.filter(name="Bad").exists())

    def test_update_part_changes_only_the_given_fields(self):
        part = Part.objects.get(name="R_2K2_0805")
        before = (part.description, part.category_id, part.IPN)
        body = self.tool_json(
            "update_part", {"id": part.pk, "keywords": "mcp-key", "minimum_stock": "3"}
        )
        self.assertEqual(tuple(body), PART_FIELDS)
        self.assertEqual(body["keywords"], "mcp-key")
        self.assertEqual(body["minimum_stock"], 3)
        part.refresh_from_db()
        self.assertEqual((part.description, part.category_id, part.IPN), before)

        body = self.tool_json("update_part", {"id": part.pk, "active": False})
        self.assertFalse(body["active"])
        self.assertEqual(body["keywords"], "mcp-key")

    def test_update_part_without_fields(self):
        message = self.tool_error("update_part", {"id": 1, "name": "", "category": 0})
        self.assertTrue(message.endswith("no fields to update"), message)

    def test_update_part_errors(self):
        self.assertTrue(
            self.tool_error("update_part", {"id": 999_999, "name": "x"}).endswith(
                NOT_FOUND
            )
        )
        part = Part.objects.get(name="R_2K2_0805")
        message = self.tool_error("update_part", {"id": part.pk, "category": 999_999})
        self.assertIn("validation failed (400): category: ", message)

    def test_set_part_image_requires_a_url(self):
        message = self.tool_error("set_part_image", {"id": 1, "image_url": ""})
        self.assertTrue(message.endswith("image_url is required"), message)

    def test_delete_part_deactivates_active_part_first(self):
        part = Part.objects.create(
            name="MCP Doomed", description="d", category_id=2, active=True
        )
        with patch("inventree_mcp.tools.parts.call_view", wraps=call_view) as call:
            text = self.tool_text("delete_part", {"id": part.pk})
        self.assertEqual(text, f"Part {part.pk} deleted successfully.")
        self.assertEqual(
            [c.args[1] for c in call.call_args_list], ["GET", "PATCH", "DELETE"]
        )
        self.assertEqual(call.call_args_list[1].kwargs["data"], {"active": False})
        self.assertFalse(Part.objects.filter(pk=part.pk).exists())

    def test_delete_part_skips_the_patch_when_inactive(self):
        part = Part.objects.create(
            name="MCP Inactive", description="d", category_id=2, active=False
        )
        with patch("inventree_mcp.tools.parts.call_view", wraps=call_view) as call:
            self.tool_text("delete_part", {"id": part.pk})
        self.assertEqual([c.args[1] for c in call.call_args_list], ["GET", "DELETE"])
        self.assertFalse(Part.objects.filter(pk=part.pk).exists())

    def test_delete_part_errors(self):
        self.assertTrue(
            self.tool_error("delete_part", {"id": 999_999}).endswith(NOT_FOUND)
        )

    def test_delete_part_refuses_a_part_with_stock(self):
        """InvenTree would delete the stock with the part (CASCADE); the tool refuses."""
        part = Part.objects.create(
            name="MCP Stocked", description="d", category_id=2, active=True
        )
        item = StockItem.objects.create(part=part, quantity=5)
        with patch("inventree_mcp.tools.parts.call_view", wraps=call_view) as call:
            message = self.tool_error("delete_part", {"id": part.pk})
        self.assertTrue(
            message.endswith(
                f"part {part.pk} has 1 stock item(s): delete its stock first"
            ),
            message,
        )
        self.assertEqual([c.args[1] for c in call.call_args_list], ["GET"])
        part.refresh_from_db()
        self.assertTrue(part.active)
        self.assertTrue(StockItem.objects.filter(pk=item.pk).exists())

    def test_delete_part_counts_stock_when_the_read_lacks_the_count(self):
        part = Part.objects.create(
            name="MCP Stocked 2", description="d", category_id=2, active=True
        )
        StockItem.objects.create(part=part, quantity=1)
        StockItem.objects.create(part=part, quantity=2)

        async def without_count(view_cls, method, path, **kwargs):
            body = await call_view(view_cls, method, path, **kwargs)
            if method == "GET" and path == f"/api/part/{part.pk}/":
                body.pop("stock_item_count")
            return body

        with patch(
            "inventree_mcp.tools.parts.call_view", side_effect=without_count
        ) as call:
            message = self.tool_error("delete_part", {"id": part.pk})
        self.assertTrue(message.endswith("has 2 stock item(s): delete its stock first"))
        (_, method, path), kwargs = call.call_args_list[1]
        self.assertEqual((method, path), ("GET", "/api/stock/"))
        self.assertEqual(kwargs["query_params"], {"part": part.pk, "limit": 1})
        self.assertTrue(Part.objects.get(pk=part.pk).active)

    def test_delete_part_reactivates_when_the_delete_fails(self):
        """A BOM sub-part cannot be deleted (PART_ALLOW_DELETE_FROM_ASSEMBLY off): the
        DELETE answers 400 and the tool undoes its deactivation."""
        InvenTreeSetting.set_setting("PART_ALLOW_DELETE_FROM_ASSEMBLY", False)
        assembly = Part.objects.create(
            name="MCP Assembly", description="a", category_id=2, assembly=True
        )
        sub_part = Part.objects.create(
            name="MCP Sub Part", description="s", category_id=2, component=True
        )
        BomItem.objects.create(part=assembly, sub_part=sub_part, quantity=1)
        with patch("inventree_mcp.tools.parts.call_view", wraps=call_view) as call:
            message = self.tool_error("delete_part", {"id": sub_part.pk})
        self.assertIn("validation failed (400): ", message)
        self.assertEqual(
            [c.args[1] for c in call.call_args_list],
            ["GET", "PATCH", "DELETE", "PATCH"],
        )
        self.assertEqual(call.call_args_list[3].kwargs["data"], {"active": True})
        sub_part.refresh_from_db()
        self.assertTrue(sub_part.active)

    def test_tools_need_the_mcp_write_role(self):
        """MCP role missing (InvenTree roles present): 403, filter off and on."""
        self.set_setting("IMAGE_DOWNLOAD_ENABLED", True)
        part = Part.objects.get(name="R_2K2_0805")
        cases = [
            ("create_part", {"name": "Nope"}, ["part.view", "part.add"], ["part.view"]),
            (
                "update_part",
                {"id": part.pk, "name": "Nope"},
                ["part.view", "part.change"],
                ["part.view"],
            ),
            (
                "set_part_image",
                {"id": part.pk, "image_url": "https://x.test/a.png"},
                ["part.view", "part.change"],
                ["part.view"],
            ),
            (
                "delete_part",
                {"id": part.pk},
                [*PART_ROLES],
                ["part.view", "part.change"],  # change but not delete
            ),
        ]
        with patch(DOWNLOAD) as download:
            for name, arguments, interactive, mcp in cases:
                with self.subTest(tool=name):
                    self.assert_denied_without_mcp_role(
                        name, arguments, interactive=interactive, mcp=mcp
                    )
        download.assert_not_called()
        # delete_part (change but not delete) deactivated the part, was refused on the
        # DELETE and reactivated it.
        part.refresh_from_db()
        self.assertTrue(part.active)
        self.assertNotEqual(part.name, "Nope")
        self.assertFalse(Part.objects.filter(name="Nope").exists())

    def test_tools_need_the_inventree_role(self):
        """Every MCP role, InvenTree role missing: 403, filter off and on."""
        self.set_setting("IMAGE_DOWNLOAD_ENABLED", True)
        part = Part.objects.get(name="R_2K2_0805")
        cases = {
            "create_part": {"name": "Nope"},
            "update_part": {"id": part.pk, "name": "Nope"},
            "set_part_image": {"id": part.pk, "image_url": "https://x.test/a.png"},
            "delete_part": {"id": part.pk},
        }
        with patch(DOWNLOAD) as download:
            for name, arguments in cases.items():
                with self.subTest(tool=name):
                    self.assert_denied_without_inventree_role(
                        name, arguments, interactive=["part.view"]
                    )
        download.assert_not_called()
        part.refresh_from_db()
        self.assertTrue(part.active)
        self.assertNotEqual(part.name, "Nope")
        self.assertFalse(Part.objects.filter(name="Nope").exists())

    def test_tools_are_listed_with_titles_and_annotations(self):
        tools = self.list_tools()
        expected = {
            "create_part": ("Create part", False, False, False),
            "update_part": ("Update part", False, False, True),
            "delete_part": ("Delete part", False, True, False),
            "set_part_image": ("Set part image", False, False, True),
        }
        for name, (title, read_only, destructive, idempotent) in expected.items():
            with self.subTest(tool=name):
                entry = tools[name]
                self.assertEqual(entry["title"], title)
                self.assertEqual(
                    entry["annotations"],
                    {
                        "title": title,
                        "readOnlyHint": read_only,
                        "destructiveHint": destructive,
                        "idempotentHint": idempotent,
                        "openWorldHint": False,
                    },
                )
                self.assertNotIn("outputSchema", entry)

    def test_string_arguments_are_coerced(self):
        body = self.tool_json(
            "create_part", {"name": "MCP Coerced", "category": "2", "trackable": "true"}
        )
        self.assertEqual(body["category"], 2)
        self.assertTrue(body["trackable"])
        self.assertEqual(
            self.tool_text("delete_part", {"id": str(body["pk"])}),
            f"Part {body['pk']} deleted successfully.",
        )


class PartWriteReadOnlyTest(WriteToolTestCase):
    roles: ClassVar[list[str]] = [*PART_ROLES]
    fixtures: ClassVar[list[str]] = ["category", "part", "location"]

    def test_every_write_tool_answers_the_read_only_text(self):
        self.set_setting("MCP_READ_ONLY", True)
        part = Part.objects.get(name="R_2K2_0805")
        calls = {
            "create_part": {"name": "Blocked"},
            "update_part": {"id": part.pk, "name": "Blocked"},
            "set_part_image": {"id": part.pk, "image_url": "https://x.test/a.png"},
            "delete_part": {"id": part.pk},
        }
        for name, arguments in calls.items():
            with self.subTest(tool=name):
                self.assertTrue(self.tool_error(name, arguments).endswith(READ_ONLY))
                self.assertIn(name, self.list_tool_names())  # still listed
        part.refresh_from_db()
        self.assertNotEqual(part.name, "Blocked")
        self.assertTrue(part.active)
        self.assertFalse(Part.objects.filter(name="Blocked").exists())
        self.assertFalse(PartCategory.objects.filter(name="Blocked").exists())
