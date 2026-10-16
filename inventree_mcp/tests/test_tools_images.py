"""Part images over the real MCP endpoint.

search_part_images with requests.get mocked; the image download behind image_url
(create_part, update_part) and set_part_image with InvenTree's download_image_from_url mocked
(one test runs the real helper's private-address refusal, which needs no network).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from typing import ClassVar
from unittest.mock import MagicMock, patch

import requests
from django.core.exceptions import ValidationError
from django.test import override_settings
from part.models import Part
from PIL import Image

from ..tools.images import (
    IMAGE_DISABLED,
    IMAGE_MAX_BYTES,
    IMAGE_TIMEOUT_SECONDS,
    IMAGE_UNAVAILABLE,
    IMAGE_USER_AGENT,
    NOT_CONFIGURED,
    SEARCH_URL,
)
from .test_transport import DENIED, NOT_FOUND, READ_ONLY, WriteToolTestCase

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
            {"cx": "cse-1", "q": "R 2k2 0805", "searchType": "image", "num": 5},
        )
        self.assertEqual(kwargs["headers"], {"X-Goog-Api-Key": API_KEY})  # never in the URL

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
        # A JSON body that is not an object counts as no hits.
        for body in ({}, {"items": []}, ["unexpected"], "text"):
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


DOWNLOAD = "InvenTree.helpers_model.download_image_from_url"
IMAGE_URL = "https://img.test/part.png"
GO_SET_PART_IMAGE = (
    "Set or replace the image for an existing part by providing an image URL. InvenTree "
    "downloads the image from the URL server-side. Use this after search_part_images to "
    "attach a product photo to a part."
)


def _image(mode: str = "RGB") -> Image.Image:
    return Image.new(mode, (8, 8))


class PartImageDownloadTest(WriteToolTestCase):
    """image_url / set_part_image: the plugin downloads and uploads the image (overview 6c)."""

    roles: ClassVar[list[str]] = ["part.view", "part.add", "part.change"]
    fixtures: ClassVar[list[str]] = ["category", "part", "location"]

    def setUp(self):
        super().setUp()
        media = tempfile.mkdtemp(prefix="mcp-media-")
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        media_settings = override_settings(MEDIA_ROOT=media)
        media_settings.enable()
        self.addCleanup(media_settings.disable)
        self.part = Part.objects.get(name="R_2K2_0805")

    def enable(self) -> None:
        self.set_setting("IMAGE_DOWNLOAD_ENABLED", True)

    def test_setting_defaults_off(self):
        from ..core import InvenTreeMCP

        setting = InvenTreeMCP.SETTINGS["IMAGE_DOWNLOAD_ENABLED"]
        self.assertIs(setting["default"], False)
        self.assertIn("InvenTree removed", setting["description"])

    def test_disabled_by_default(self):
        self.assertEqual(
            IMAGE_DISABLED,
            "setting a part image from a URL is disabled: an administrator must enable "
            "the plugin setting IMAGE_DOWNLOAD_ENABLED",
        )
        calls = {
            "set_part_image": {"id": self.part.pk, "image_url": IMAGE_URL},
            "update_part": {"id": self.part.pk, "name": "Changed", "image_url": IMAGE_URL},
            "create_part": {"name": "MCP With Image", "image_url": IMAGE_URL},
        }
        with patch(DOWNLOAD) as download:
            for name, arguments in calls.items():
                with self.subTest(tool=name):
                    message = self.tool_error(name, arguments)
                    self.assertTrue(message.endswith(IMAGE_DISABLED), message)
        download.assert_not_called()
        self.part.refresh_from_db()
        self.assertEqual(self.part.name, "R_2K2_0805")  # nothing changed first
        self.assertFalse(Part.objects.filter(name="MCP With Image").exists())

    def test_read_only_answers_before_anything_else(self):
        self.enable()
        self.set_setting("MCP_READ_ONLY", True)
        with patch(DOWNLOAD) as download:
            message = self.tool_error(
                "set_part_image", {"id": self.part.pk, "image_url": IMAGE_URL}
            )
        self.assertTrue(message.endswith(READ_ONLY), message)
        download.assert_not_called()

    def test_no_download_without_the_change_permission(self):
        self.enable()
        token = self.make_token("img-viewer", ["part.view"], ["all"])
        with patch(DOWNLOAD) as download:
            for name, arguments in {
                "set_part_image": {"id": self.part.pk, "image_url": IMAGE_URL},
                "update_part": {"id": self.part.pk, "image_url": IMAGE_URL},
            }.items():
                with self.subTest(tool=name, missing="InvenTree role"):
                    self.assertTrue(self.error_as(token, name, arguments).endswith(DENIED))
            token = self.make_token("img-no-mcp", ["part.view", "part.change"], ["part"])
            with self.subTest(missing="MCP role"):
                message = self.error_as(
                    token, "set_part_image", {"id": self.part.pk, "image_url": IMAGE_URL}
                )
                self.assertTrue(message.endswith(DENIED), message)
        # (create_part checks the same PATCH permission before its POST; a user with add
        # but without change does not exist: InvenTree's and the MCP rulesets imply change
        # from add.)
        download.assert_not_called()
        self.assertFalse(self.part.image)

    def test_unknown_part_is_404_without_a_download(self):
        self.enable()
        with patch(DOWNLOAD) as download:
            message = self.tool_error(
                "set_part_image", {"id": 999_999, "image_url": IMAGE_URL}
            )
        self.assertTrue(message.endswith(NOT_FOUND), message)
        download.assert_not_called()

    def test_set_part_image(self):
        self.enable()
        with patch(DOWNLOAD, return_value=_image("RGB")) as download:
            body = self.tool_json(
                "set_part_image", {"id": self.part.pk, "image_url": IMAGE_URL}
            )
        download.assert_called_once_with(
            IMAGE_URL,
            timeout=IMAGE_TIMEOUT_SECONDS,
            user_agent=IMAGE_USER_AGENT,
            max_size=IMAGE_MAX_BYTES,
        )
        self.part.refresh_from_db()
        self.assertTrue(self.part.image)
        self.assertRegex(self.part.image.name, rf"part_{self.part.pk}[^/]*\.jpg$")
        self.assertTrue(body["image"].endswith(self.part.image.name.rsplit("/", 1)[-1]))
        self.assertTrue(body["thumbnail"])
        with Image.open(self.part.image.path) as stored:
            self.assertEqual((stored.format, stored.size), ("JPEG", (8, 8)))

    def test_transparent_images_stay_png(self):
        self.enable()
        with patch(DOWNLOAD, return_value=_image("RGBA")):
            self.tool_json("set_part_image", {"id": self.part.pk, "image_url": IMAGE_URL})
        self.part.refresh_from_db()
        self.assertRegex(self.part.image.name, rf"part_{self.part.pk}[^/]*\.png$")
        with Image.open(self.part.image.path) as stored:
            self.assertEqual(stored.format, "PNG")

    def test_update_part_with_fields_and_image(self):
        self.enable()
        with patch(DOWNLOAD, return_value=_image()):
            body = self.tool_json(
                "update_part",
                {"id": self.part.pk, "keywords": "with-image", "image_url": IMAGE_URL},
            )
        self.assertEqual(body["keywords"], "with-image")
        self.assertTrue(body["image"])
        self.part.refresh_from_db()
        self.assertEqual(self.part.keywords, "with-image")
        self.assertTrue(self.part.image)

    def test_update_part_with_only_the_image(self):
        self.enable()
        with patch(DOWNLOAD, return_value=_image()):
            body = self.tool_json(
                "update_part", {"id": self.part.pk, "image_url": IMAGE_URL}
            )
        self.assertTrue(body["image"])

    def test_create_part_with_image(self):
        self.enable()
        with patch(DOWNLOAD, return_value=_image()):
            body = self.tool_json(
                "create_part",
                {"name": "MCP Pictured", "category": 2, "image_url": IMAGE_URL},
            )
        part = Part.objects.get(pk=body["pk"])
        self.assertEqual(part.name, "MCP Pictured")
        self.assertTrue(part.image)
        self.assertTrue(body["image"])

    def test_create_part_keeps_the_part_when_the_image_fails(self):
        self.enable()
        with patch(DOWNLOAD, side_effect=ValueError("Image size is too large")):
            message = self.tool_error(
                "create_part", {"name": "MCP Half Done", "image_url": IMAGE_URL}
            )
        part = Part.objects.get(name="MCP Half Done")
        self.assertTrue(
            message.endswith(
                f"part {part.pk} was created, but setting its image failed: "
                "image download failed: Image size is too large"
            ),
            message,
        )
        self.assertFalse(part.image)

    def test_download_errors(self):
        self.enable()
        cases = [
            (ValidationError("Enter a valid URL."), "Enter a valid URL."),
            (ValueError("Image download exceeded maximum size"), "exceeded maximum"),
            (TypeError("Supplied URL is not a valid image file"), "not a valid image"),
            (requests.Timeout("read timed out"), "read timed out"),
            (requests.HTTPError("Server responded with invalid status code: 404"), "404"),
            (Exception("Connection error: refused"), "Connection error: refused"),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__), patch(DOWNLOAD, side_effect=error):
                message = self.tool_error(
                    "set_part_image", {"id": self.part.pk, "image_url": IMAGE_URL}
                )
                self.assertIn("image download failed: ", message)
                self.assertIn(expected, message)
        self.part.refresh_from_db()
        self.assertFalse(self.part.image)

    def test_errors_never_carry_the_url_credentials(self):
        self.enable()
        url = "https://bob:hunter2-secret@img.test/a.png?token=tok-0123456789"
        error = Exception(
            f"Exception occurred: HTTPSConnectionPool(host='img.test'): Max retries "
            f"exceeded with url: /a.png?token=tok-0123456789 ({url})"
        )
        with patch(DOWNLOAD, side_effect=error):
            message = self.tool_error(
                "set_part_image", {"id": self.part.pk, "image_url": url}
            )
        self.assertIn("image download failed: ", message)
        for secret in ("hunter2-secret", "tok-0123456789", "bob:"):
            self.assertNotIn(secret, message)

    def test_the_real_helper_refuses_private_addresses(self):
        self.enable()
        message = self.tool_error(
            "set_part_image", {"id": self.part.pk, "image_url": "http://127.0.0.1/a.png"}
        )
        self.assertIn("image download failed: ", message)
        self.part.refresh_from_db()
        self.assertFalse(self.part.image)

    def test_helper_missing(self):
        self.enable()
        with patch.dict(sys.modules, {"InvenTree.helpers_model": None}):
            message = self.tool_error(
                "set_part_image", {"id": self.part.pk, "image_url": IMAGE_URL}
            )
        self.assertTrue(message.endswith(IMAGE_UNAVAILABLE), message)

    def test_descriptions_name_the_setting(self):
        tools = self.list_tools()
        self.assertEqual(
            tools["set_part_image"]["description"],
            GO_SET_PART_IMAGE + " Requires the plugin setting IMAGE_DOWNLOAD_ENABLED.",
        )
        for name in ("create_part", "update_part"):
            self.assertTrue(
                tools[name]["description"].endswith(
                    "Setting an image from image_url requires the plugin setting "
                    "IMAGE_DOWNLOAD_ENABLED."
                ),
                name,
            )
