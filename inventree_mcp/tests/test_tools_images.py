"""search_part_images over the real MCP endpoint, with requests.get mocked."""

from __future__ import annotations

import json
from typing import ClassVar
from unittest.mock import MagicMock, patch

import requests
from django.test import override_settings

from ..tools.images import NOT_CONFIGURED, SEARCH_URL
from .test_tools_parts import WriteToolTestCase

API_KEY = "AIza-secret-key-1234567890"
GET = "inventree_mcp.tools.images.requests.get"


def _response(status: int = 200, body=None, *, bad_json: bool = False) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    if bad_json:
        response.json.side_effect = ValueError("no json")
    else:
        response.json.return_value = body if body is not None else {}
    return response


def _item(n: int) -> dict:
    return {
        "title": f"Title {n}",
        "link": f"https://img.test/{n}.jpg",
        "image": {
            "thumbnailLink": f"https://img.test/t{n}.jpg",
            "contextLink": f"https://page.test/{n}",
            "width": 100 + n,
            "height": 200 + n,
        },
    }


@override_settings(PLUGIN_TESTING_SETUP=True)
class SearchPartImagesTest(WriteToolTestCase):
    roles: ClassVar[list[str]] = ["part.view"]

    def configure(self) -> None:
        self.set_setting("GOOGLE_API_KEY", API_KEY)
        self.set_setting("GOOGLE_CSE_ID", "cse-1")

    def test_unconfigured(self):
        for key, cse in [("", ""), (API_KEY, ""), ("", "cse-1")]:
            with self.subTest(key=bool(key), cse=bool(cse)):
                self.set_setting("GOOGLE_API_KEY", key)
                self.set_setting("GOOGLE_CSE_ID", cse)
                with patch(GET) as get:
                    message = self.tool_error("search_part_images", {"query": "x"})
                self.assertTrue(message.endswith(NOT_CONFIGURED), message)
                get.assert_not_called()
        self.assertEqual(
            NOT_CONFIGURED,
            "image search is not configured: set the plugin settings "
            "GOOGLE_API_KEY and GOOGLE_CSE_ID",
        )

    def test_empty_query(self):
        self.configure()
        with patch(GET) as get:
            message = self.tool_error("search_part_images", {"query": ""})
        self.assertTrue(message.endswith("query is required"), message)
        get.assert_not_called()

    def test_result_shape_and_request(self):
        self.configure()
        with patch(
            GET, return_value=_response(body={"items": [_item(1), _item(2)]})
        ) as get:
            body = self.tool_json("search_part_images", {"query": "R 2k2 0805"})
        self.assertEqual(list(body), ["query", "count", "results"])
        self.assertEqual(body["query"], "R 2k2 0805")
        self.assertEqual(body["count"], 2)
        self.assertEqual(
            body["results"][0],
            {
                "title": "Title 1",
                "link": "https://img.test/1.jpg",
                "thumbnail_url": "https://img.test/t1.jpg",
                "context_url": "https://page.test/1",
                "width": 101,
                "height": 201,
            },
        )
        (url,), kwargs = get.call_args
        self.assertEqual(url, SEARCH_URL)
        self.assertEqual(kwargs["timeout"], 10)
        self.assertEqual(
            kwargs["params"],
            {
                "key": API_KEY,
                "cx": "cse-1",
                "q": "R 2k2 0805",
                "searchType": "image",
                "num": 5,
            },
        )

    def test_num_is_clamped(self):
        self.configure()
        for given, sent in [
            (1, 1),
            (10, 10),
            (11, 10),
            (100, 10),
            (0, 5),
            (-3, 5),
            ("7", 7),
        ]:
            with self.subTest(num=given), patch(GET, return_value=_response()) as get:
                self.call_tool("search_part_images", {"query": "x", "num": given})
            self.assertEqual(get.call_args.kwargs["params"]["num"], sent)

    def test_no_images(self):
        self.configure()
        for body in ({}, {"items": []}):
            with patch(GET, return_value=_response(body=body)):
                result = self.call_tool("search_part_images", {"query": "nothing here"})
            self.assertFalse(result.get("isError"), result)
            self.assertEqual(
                result["content"][0]["text"], "No images found for query: nothing here"
            )

    def test_api_error_status(self):
        self.configure()
        with patch(GET, return_value=_response(status=403)):
            message = self.tool_error("search_part_images", {"query": "x"})
        self.assertTrue(
            message.endswith("image search failed: image search returned status 403"),
            message,
        )

    def test_undecodable_response(self):
        self.configure()
        with patch(GET, return_value=_response(bad_json=True)):
            message = self.tool_error("search_part_images", {"query": "x"})
        self.assertIn("image search failed", message)

    def test_the_api_key_never_appears_in_errors(self):
        self.configure()
        url = f"{SEARCH_URL}?key={API_KEY}&cx=cse-1&q=x"
        errors = [
            requests.ConnectionError(
                f"HTTPSConnectionPool: Max retries for url: {url}"
            ),
            requests.Timeout(f"timed out: {url}"),
            requests.RequestException(f"key={API_KEY}"),
        ]
        for error in errors:
            with (
                self.subTest(error=type(error).__name__),
                patch(GET, side_effect=error),
            ):
                result = self.call_tool("search_part_images", {"query": "x"})
            text = result["content"][0]["text"]
            self.assertTrue(result["isError"], result)
            self.assertIn("image search failed", text)
            self.assertNotIn(API_KEY, text)
            self.assertIn("***", text)
        for response in (_response(status=500), _response(bad_json=True)):
            with patch(GET, return_value=response):
                self.assertNotIn(
                    API_KEY, self.tool_error("search_part_images", {"query": "x"})
                )

    def test_listed_with_title_and_annotations(self):
        entry = self.list_tools()["search_part_images"]
        self.assertEqual(entry["title"], "Search part images")
        self.assertEqual(
            entry["annotations"],
            {
                "title": "Search part images",
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": True,
            },
        )
        self.assertNotIn("outputSchema", entry)
        self.assertIn("GOOGLE_API_KEY", entry["description"])
        self.assertNotIn("environment variables", entry["description"])

    def test_visible_to_a_user_without_part_roles_when_the_filter_is_on(self):
        self.set_setting("MCP_FILTER_TOOL_LIST", True)
        token = self.make_token("img-no-part", [], ["stock_location.view"])
        names = self.list_tool_names(HTTP_AUTHORIZATION=f"Token {token}")
        self.assertIn("search_part_images", names)
        self.assertNotIn("search_parts", names)

    def test_result_is_json_text(self):
        self.configure()
        with patch(GET, return_value=_response(body={"items": [_item(1)]})):
            result = self.call_tool("search_part_images", {"query": "x"})
        self.assertEqual(json.loads(result["content"][0]["text"])["count"], 1)
