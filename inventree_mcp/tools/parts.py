"""Part tools: search_parts, get_part, list_parts (T1); the part write tools follow (T2).

Thin wrappers around part.api.PartList / PartDetail through proxy.call_view(), so permissions,
filtering and serialization are InvenTree's own. Names, inputs, defaults and descriptions are
the Go server's (docs/plans/tools/00-overview.md, section 4).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..view_resolution import resolve_view
from ._common import DEFAULTS, PART_FIELDS, access, fetch, listing, tool


def _part_list() -> type | None:
    return resolve_view("part.api", "PartList")


def _part_detail() -> type | None:
    return resolve_view("part.api", "PartDetail")


@tool("search_parts", title="Search parts", read_only=True)
async def search_parts(
    search: Annotated[
        str, Field(description="Search query to find parts by name or keyword")
    ],
    limit: Annotated[
        int, Field(description="Maximum number of results to return (default 25)")
    ] = DEFAULTS["search"],
) -> dict:
    """Search for parts by name, keyword, or description. Use this to find existing parts
    before creating new ones. Returns matching parts with their IDs, names, categories, and
    stock levels.
    """
    return await listing(
        _part_list,
        "/api/part/",
        {"search": search},
        limit=limit,
        default=DEFAULTS["search"],
        fields=PART_FIELDS,
    )


@tool("get_part", title="Get part", read_only=True)
async def get_part(
    id: Annotated[int, Field(description="The part ID (pk) to retrieve")],
) -> dict:
    """Get detailed information about a specific part by its ID."""
    return await fetch(
        _part_detail, "GET", f"/api/part/{id}/", fields=PART_FIELDS, pk=id
    )


@tool("list_parts", title="List parts", read_only=True)
async def list_parts(
    category: Annotated[
        int, Field(description="Filter by category ID. 0 or omit to list all.")
    ] = 0,
    limit: Annotated[
        int, Field(description="Maximum number of results (default 50)")
    ] = DEFAULTS["list"],
    offset: Annotated[int, Field(description="Offset for pagination")] = 0,
) -> dict:
    """List all parts, optionally filtered by category. Use search_parts for finding
    specific parts by name.
    """
    return await listing(
        _part_list,
        "/api/part/",
        {"category": category, "offset": offset},
        limit=limit,
        default=DEFAULTS["list"],
        fields=PART_FIELDS,
    )


access("search_parts", _part_list, "GET")
access("get_part", _part_detail, "GET")
access("list_parts", _part_list, "GET")
