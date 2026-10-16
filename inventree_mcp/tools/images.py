"""Part image search: search_part_images (T2).

Google Custom Search through the plugin settings GOOGLE_API_KEY and GOOGLE_CSE_ID; no
InvenTree call, so the tool has no ACCESS entry and every user who may use the MCP endpoint
sees it (docs/plans/tools/00-overview.md). The API key never reaches an error text or a log:
exception texts are scrubbed of it before they are raised.
"""

from __future__ import annotations

import asyncio
from typing import Annotated
from urllib.parse import quote_plus

import requests
from asgiref.sync import sync_to_async
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..settings import plugin_setting
from ._common import text, tool

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
    params = {
        "key": api_key,
        "cx": cse_id,
        "q": query,
        "searchType": "image",
        "num": num,
    }
    try:
        response = requests.get(SEARCH_URL, params=params, timeout=TIMEOUT_SECONDS)
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
        items = response.json().get("items") or []
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
