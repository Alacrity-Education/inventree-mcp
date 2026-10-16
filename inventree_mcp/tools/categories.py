"""Part category tools: search_part_categories, list_part_categories (T1); writes follow (T2).

Thin wrappers around part.api.CategoryList / CategoryDetail through proxy.call_view(). Names,
inputs, defaults and descriptions are the Go server's (docs/plans/tools/00-overview.md).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..view_resolution import resolve_view
from ._common import CATEGORY_FIELDS, DEFAULTS, access, listing, tool


def _category_list() -> type | None:
    return resolve_view("part.api", "CategoryList")


def _category_detail() -> type | None:
    return resolve_view("part.api", "CategoryDetail")


@tool("search_part_categories", title="Search part categories", read_only=True)
async def search_part_categories(
    search: Annotated[
        str, Field(description="Search query to find categories by name")
    ],
    limit: Annotated[
        int, Field(description="Maximum number of results (default 25)")
    ] = DEFAULTS["search"],
) -> dict:
    """Search for part categories by name. Use this to find the right category when
    creating parts. Returns categories with their full path (e.g., 'Electronic
    Components/Resistors/Through Hole').
    """
    return await listing(
        _category_list,
        "/api/part/category/",
        {"search": search},
        limit=limit,
        default=DEFAULTS["search"],
        fields=CATEGORY_FIELDS,
    )


@tool("list_part_categories", title="List part categories", read_only=True)
async def list_part_categories(
    parent: Annotated[
        int,
        Field(description="Filter by parent category ID. 0 or omit to list all."),
    ] = 0,
    limit: Annotated[
        int, Field(description="Maximum number of results (default 100)")
    ] = DEFAULTS["tree"],
    offset: Annotated[int, Field(description="Offset for pagination")] = 0,
) -> dict:
    """List all part categories, optionally filtered by parent category. Shows the category
    hierarchy with pathstrings and part counts.
    """
    return await listing(
        _category_list,
        "/api/part/category/",
        {"parent": parent, "offset": offset},
        limit=limit,
        default=DEFAULTS["tree"],
        fields=CATEGORY_FIELDS,
    )


access("search_part_categories", _category_list, "GET")
access("list_part_categories", _category_list, "GET")
