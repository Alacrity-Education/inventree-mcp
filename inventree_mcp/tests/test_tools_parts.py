"""Part tools over the real MCP endpoint.

T1 holds the read tool tests (search_parts, get_part, list_parts); T2 added the write tool
tests (create_part, update_part, delete_part, set_part_image) and WriteToolTestCase, the base
of the category and image tests too.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import override_settings
from InvenTree.version import inventreeApiVersion
from part.models import Part, PartCategory
from users.models import ApiToken

from ..tools._common import PART_FIELDS
from .oauth_helpers import _raw_api_token, grant_mcp
from .test_transport import MCPToolTestCase

NOT_FOUND = "not found (404): resource does not exist"
DENIED = "permission denied (403): your MCP roles or your InvenTree permissions do not allow this"
READ_ONLY = (
    "This MCP server is running in read-only mode; write actions are disabled. "
    "An administrator must disable the 'Read Only' plugin setting to allow them."
)


class WriteToolTestCase(MCPToolTestCase):
    """MCPToolTestCase with MCP_READ_ONLY off and helpers for the write tool tests.

    cls.user (API token cls.api_token) holds the class's ``roles`` and every MCP role.
    """

    def setUp(self):
        super().setUp()
        self.set_setting("MCP_READ_ONLY", False)

    def make_token(self, username: str, interactive: list[str], mcp: list[str]) -> str:
        """An API token for a new user whose group has these interactive and MCP roles."""
        group = Group.objects.create(name=f"{username}-group")
        for role in interactive:
            self.assignRole(role, group=group)
        grant_mcp(group, *mcp)
        user = get_user_model().objects.create_user(username=username)
        user.groups.add(group)
        return _raw_api_token(ApiToken.objects.create(user=user, name=username))

    def error_as(self, token: str, name: str, arguments: dict[str, Any]) -> str:
        """A failed tool call's error text, called with *token*."""
        result = self.call_tool(name, arguments, HTTP_AUTHORIZATION=f"Token {token}")
        self.assertTrue(result.get("isError"), result)
        return result["content"][0]["text"]

    def tool_text(self, name: str, arguments: dict[str, Any]) -> str:
        """A successful tool call's plain text."""
        result = self.call_tool(name, arguments)
        self.assertFalse(result.get("isError"), result)
        return result["content"][0]["text"]

    def assert_denied_without_mcp_role(self, name, arguments, *, interactive, mcp):
        """The tool answers 403 for a user lacking the MCP role, filter off and on.

        Filter on: the tool is also missing from that user's tools/list.
        """
        token = self.make_token(f"no-mcp-{name}", interactive, mcp)
        headers = {"HTTP_AUTHORIZATION": f"Token {token}"}
        self.assertTrue(self.error_as(token, name, arguments).endswith(DENIED))
        self.assertIn(name, self.list_tool_names(**headers))  # filter off: listed
        self.set_setting("MCP_FILTER_TOOL_LIST", True)
        self.assertNotIn(name, self.list_tool_names(**headers))
        self.assertTrue(self.error_as(token, name, arguments).endswith(DENIED))
        self.assertIn(name, self.list_tool_names())  # the full-role user still sees it


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
    roles: ClassVar[list[str]] = [*PART_ROLES, "part_category.view"]
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

    def test_create_part_image_url_is_sent_as_remote_image(self):
        with patch("inventree_mcp.tools._common.call_view") as call:
            call.return_value = {}
            self.call_tool(
                "create_part", {"name": "x", "image_url": "https://example.com/a.png"}
            )
        self.assertEqual(
            call.call_args.kwargs["data"],
            {"name": "x", "remote_image": "https://example.com/a.png"},
        )

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

    def test_update_part_image_url_is_sent_as_remote_image(self):
        with patch("inventree_mcp.tools._common.call_view") as call:
            call.return_value = {}
            self.call_tool(
                "update_part", {"id": 7, "image_url": "https://example.com/a.png"}
            )
        (_, method, path), kwargs = call.call_args
        self.assertEqual((method, path, kwargs["pk"]), ("PATCH", "/api/part/7/", 7))
        self.assertEqual(kwargs["data"], {"remote_image": "https://example.com/a.png"})

    def test_set_part_image_payload(self):
        with patch("inventree_mcp.tools._common.call_view") as call:
            call.return_value = {}
            self.call_tool(
                "set_part_image", {"id": 7, "image_url": "https://example.com/a.png"}
            )
        (_, method, path), kwargs = call.call_args
        self.assertEqual((method, path), ("PATCH", "/api/part/7/"))
        self.assertEqual(kwargs["data"], {"remote_image": "https://example.com/a.png"})

    def test_set_part_image_requires_a_url(self):
        message = self.tool_error("set_part_image", {"id": 1, "image_url": ""})
        self.assertTrue(message.endswith("image_url is required"), message)

    def test_set_part_image_with_a_real_patch(self):
        """The real PATCH with INVENTREE_DOWNLOAD_FROM_URL off (the default).

        InvenTree before API 489 rejects remote_image naming that setting; from API 489 the
        field is gone from the Part API, so the PATCH succeeds and changes nothing (the
        dev stack, 1.5.7, is API 530). Both outcomes are pinned; see the T2 report.
        """
        part = Part.objects.get(name="R_2K2_0805")
        arguments = {"id": part.pk, "image_url": "https://example.com/a.png"}
        result = self.call_tool("set_part_image", arguments)
        if inventreeApiVersion() < 489:
            self.assertTrue(result["isError"], result)
            self.assertIn("INVENTREE_DOWNLOAD_FROM_URL", result["content"][0]["text"])
        else:
            self.assertFalse(result.get("isError"), result)
            body = json.loads(result["content"][0]["text"])
            self.assertEqual(tuple(body), PART_FIELDS)
            self.assertIsNone(body["image"])
            part.refresh_from_db()
            self.assertFalse(part.image)

    def test_set_part_image_404(self):
        self.assertTrue(
            self.tool_error(
                "set_part_image", {"id": 999_999, "image_url": "https://x.test/a.png"}
            ).endswith(NOT_FOUND)
        )

    def test_delete_part_deactivates_active_part_first(self):
        part = Part.objects.create(
            name="MCP Doomed", description="d", category_id=2, active=True
        )
        with patch(
            "inventree_mcp.tools.parts.call_view", wraps=self._wrapped_call_view()
        ) as call:
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
        with patch(
            "inventree_mcp.tools.parts.call_view", wraps=self._wrapped_call_view()
        ) as call:
            self.tool_text("delete_part", {"id": part.pk})
        self.assertEqual([c.args[1] for c in call.call_args_list], ["GET", "DELETE"])
        self.assertFalse(Part.objects.filter(pk=part.pk).exists())

    def _wrapped_call_view(self):
        from ..proxy import call_view

        return call_view

    def test_delete_part_errors(self):
        self.assertTrue(
            self.tool_error("delete_part", {"id": 999_999}).endswith(NOT_FOUND)
        )

    def test_tools_need_the_mcp_write_role(self):
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
        for name, arguments, interactive, mcp in cases:
            with self.subTest(tool=name):
                self.assert_denied_without_mcp_role(
                    name, arguments, interactive=interactive, mcp=mcp
                )
                self.setting_cleanup_reset()
        # delete_part with change but not delete deactivated the part, then was refused.
        self.assertNotEqual(Part.objects.get(pk=part.pk).name, "Nope")

    def setting_cleanup_reset(self):
        self.set_setting("MCP_FILTER_TOOL_LIST", False)

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
