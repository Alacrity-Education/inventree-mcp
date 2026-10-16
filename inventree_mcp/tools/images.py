"""Part images: search_part_images (T2) and the image download behind image_url (fix round).

search_part_images: Google Custom Search through the plugin settings GOOGLE_API_KEY and
GOOGLE_CSE_ID; no InvenTree call, so the tool has no ACCESS entry and every user who may use
the MCP endpoint sees it (docs/plans/tools/00-overview.md). The API key never reaches an error
text or a log: exception texts are scrubbed of it before they are raised.

image_url (create_part, update_part, set_part_image): InvenTree removed remote_image from the
Part API (API 489, a security fix), so the plugin downloads the image itself, only when the
plugin setting IMAGE_DOWNLOAD_ENABLED is on (overview 6c). ensure_image_allowed() runs before
any call and any network access: the read-only gate, the setting and the caller's permission
to change parts. upload_part_image() downloads with InvenTree's own
helpers_model.download_image_from_url (URL validation, SSRF guard incl. DNS rebinding,
redirect checks, size cap, image validation) in a worker thread, re-encodes the image and
PATCHes the part's image field as multipart through proxy.call_view(). Error texts never carry
the URL's credentials, query or response bodies.
"""

from __future__ import annotations

import asyncio
import io
import re
from typing import Annotated, Any
from urllib.parse import quote_plus, urlsplit

import requests
from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import PLUGIN_VERSION
from ..proxy import call_view, ensure_writable
from ..settings import plugin_setting
from ._common import ViewLoader, require_access, text, tool

SEARCH_URL = "https://www.googleapis.com/customsearch/v1"
TIMEOUT_SECONDS = 10
NOT_CONFIGURED = (
    "image search is not configured: set the plugin settings GOOGLE_API_KEY "
    "and GOOGLE_CSE_ID"
)


def _scrub(message: str, api_key: str) -> str:
    """*message* without the API key (raw or URL-encoded)."""
    for secret in {api_key, quote_plus(api_key)}:
        message = message.replace(secret, "***")
    return message


def _search(api_key: str, cse_id: str, query: str, num: int) -> list[dict]:
    """One Google Custom Search image query (blocking); the result rows of the tool."""
    params = {"cx": cse_id, "q": query, "searchType": "image", "num": num}
    # The key travels in a header, not in the query string: HTTP client debug
    # logs record request URLs, never headers.
    headers = {"X-Goog-Api-Key": api_key}
    try:
        response = requests.get(
            SEARCH_URL, params=params, headers=headers, timeout=TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise ToolError(
            _scrub(
                f"image search failed: image search request failed: {type(exc).__name__}: {exc}",
                api_key,
            )
        ) from None
    if response.status_code != 200:
        raise ToolError(
            f"image search failed: image search returned status {response.status_code}"
        )
    try:
        body = response.json()
        items = (body.get("items") if isinstance(body, dict) else None) or []
    except ValueError:
        raise ToolError(
            "image search failed: decoding image search response failed"
        ) from None
    results = []
    for item in items:
        image = item.get("image") or {}
        results.append({
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "thumbnail_url": image.get("thumbnailLink", ""),
            "context_url": image.get("contextLink", ""),
            "width": image.get("width", 0),
            "height": image.get("height", 0),
        })
    return results


@tool(
    "search_part_images",
    title="Search part images",
    read_only=True,
    open_world=True,
)
async def search_part_images(
    query: Annotated[
        str,
        Field(
            description="Search query for finding product images (e.g. part name, "
            "manufacturer part number)"
        ),
    ],
    num: Annotated[
        int, Field(description="Number of results to return (1-10, default 5)")
    ] = 5,
) -> dict | str:
    """Search for product images using Google Custom Search. Returns image URLs that can be
    used with create_part (image_url field) or set_part_image. Decision logic: If the top
    result clearly matches the part (e.g. exact product photo from manufacturer or
    distributor), use it directly. If unsure, present all results to the user and let them
    choose. Tip: include the manufacturer name or 'datasheet' in the query for better
    results. Requires the plugin settings GOOGLE_API_KEY and GOOGLE_CSE_ID to be configured.
    """
    api_key = await sync_to_async(plugin_setting)("GOOGLE_API_KEY")
    cse_id = await sync_to_async(plugin_setting)("GOOGLE_CSE_ID")
    if not api_key or not cse_id:
        raise ToolError(NOT_CONFIGURED)
    if not query:
        raise ToolError("query is required")
    if num <= 0:
        num = 5
    num = min(max(num, 1), 10)
    results = await asyncio.to_thread(_search, api_key, cse_id, query, num)
    if not results:
        return text("No images found for query: " + query)
    return {"query": query, "count": len(results), "results": results}


#: The setting that enables image_url / set_part_image.
IMAGE_SETTING = "IMAGE_DOWNLOAD_ENABLED"
IMAGE_DISABLED = (
    "setting a part image from a URL is disabled: an administrator must enable the "
    f"plugin setting {IMAGE_SETTING}"
)
IMAGE_UNAVAILABLE = "image download is not available on this InvenTree version"
#: Download limits (InvenTree's former INVENTREE_DOWNLOAD_* defaults: 1 MB).
IMAGE_TIMEOUT_SECONDS = 10
IMAGE_MAX_BYTES = 1024 * 1024
IMAGE_USER_AGENT = f"inventree-mcp/{PLUGIN_VERSION}"
#: Longest download error detail passed on.
_MAX_DETAIL = 300
#: user:password@ in any URL of an error text.
_USERINFO = re.compile(r"(?<=://)[^/@\s]+@")


def _scrub_url(message: str, url: str) -> str:
    """*message* without the URL's credentials and query (and without any user:password@)."""
    parts = urlsplit(url)
    secrets = {
        url,
        parts.query,
        parts.fragment,
        parts.username or "",
        parts.password or "",
    }
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        message = message.replace(secret, "***")
    message = _USERINFO.sub("***@", message).strip()
    if len(message) > _MAX_DETAIL:
        message = message[:_MAX_DETAIL] + "..."
    return message


async def ensure_image_allowed(
    view_loader: ViewLoader, part_id: int | None = None
) -> None:
    """Refuse image_url before any change and any download.

    In this order: MCP_READ_ONLY on -> the read-only text (as every write tool answers);
    IMAGE_DOWNLOAD_ENABLED off -> IMAGE_DISABLED; the caller may not PATCH
    *view_loader*'s view (PartDetail) -> the 403 text; with *part_id* (an existing part),
    a GET of it -> the 404 text for an unknown part. Nothing has been downloaded or
    changed when this raises.
    """
    await sync_to_async(ensure_writable)("PATCH")
    if not await sync_to_async(plugin_setting)(IMAGE_SETTING):
        raise ToolError(IMAGE_DISABLED)
    await require_access(view_loader, "PATCH")
    if part_id is not None:
        await call_view(view_loader(), "GET", f"/api/part/{part_id}/", pk=part_id)


def _download(url: str, part_id: int) -> SimpleUploadedFile:
    """Download *url* with InvenTree's helper and re-encode it (blocking).

    Returns the upload part_<part_id>.png (alpha or palette) or part_<part_id>.jpg.
    """
    try:
        from InvenTree.helpers_model import download_image_from_url
    except ImportError:
        raise ToolError(IMAGE_UNAVAILABLE) from None
    try:
        image = download_image_from_url(
            url,
            timeout=IMAGE_TIMEOUT_SECONDS,
            user_agent=IMAGE_USER_AGENT,
            max_size=IMAGE_MAX_BYTES,
        )
    except ValidationError as exc:
        raise ToolError(
            "image download failed: " + _scrub_url("; ".join(exc.messages), url)
        ) from None
    except Exception as exc:  # noqa: BLE001 - InvenTree wraps network errors in Exception
        # ValueError (size, redirects, private address), TypeError (not an image),
        # requests exceptions and the plain Exception InvenTree raises for connection errors.
        raise ToolError(
            f"image download failed: {_scrub_url(str(exc), url) or type(exc).__name__}"
        ) from None
    has_alpha = image.mode in {"RGBA", "LA", "P", "PA"} or "transparency" in image.info
    buffer = io.BytesIO()
    try:
        if has_alpha:
            image.save(buffer, format="PNG")
            extension, content_type = "png", "image/png"
        else:
            image.convert("RGB").save(buffer, format="JPEG", quality=90)
            extension, content_type = "jpg", "image/jpeg"
    except (OSError, ValueError):
        raise ToolError(
            "image download failed: the image could not be re-encoded"
        ) from None
    return SimpleUploadedFile(
        f"part_{part_id}.{extension}", buffer.getvalue(), content_type=content_type
    )


async def upload_part_image(
    view_loader: ViewLoader, part_id: int, image_url: str
) -> Any:
    """Download *image_url* and PATCH it as part *part_id*'s image; the PATCH response.

    Call ensure_image_allowed() first. The download runs in a worker thread (not the
    request's thread: it is network I/O, no ORM work).
    """
    upload = await sync_to_async(_download, thread_sensitive=False)(image_url, part_id)
    return await call_view(
        view_loader(),
        "PATCH",
        f"/api/part/{part_id}/",
        files={"image": upload},
        pk=part_id,
    )
