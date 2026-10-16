"""Part category tools: search_part_categories, list_part_categories (T1); create/update/delete_part_category (T2).

Thin wrappers around part.api.CategoryList / CategoryDetail through proxy.call_view(). Names,
inputs, defaults and descriptions are the Go server's (docs/plans/tools/00-overview.md).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..proxy import call_view
from ..view_resolution import resolve_view
from ._common import (
    CATEGORY_FIELDS,
    DEFAULTS,
    access,
    changes,
    clean,
    fetch,
    listing,
    text,
    tool,
)


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


@tool(
    "create_part_category",
    title="Create part category",
    read_only=False,
    destructive=False,
)
async def create_part_category(
    name: Annotated[str, Field(description="Category name (required)")],
    description: Annotated[str, Field(description="Category description")] = "",
    parent: Annotated[
        int,
        Field(description="Parent category ID. 0 or omit for top-level category."),
    ] = 0,
    default_location: Annotated[
        int,
        Field(
            description="Default stock location ID for parts in this category. "
            "0 or omit for none."
        ),
    ] = 0,
    structural: Annotated[
        bool | None,
        Field(
            description="If true, parts cannot be directly assigned to this category "
            "(only to sub-categories)"
        ),
    ] = None,
) -> dict:
    """Create a new part category. Categories organize parts into a hierarchy (e.g.,
    Electronic Components > Resistors > Through Hole). Always search for existing categories
    first to avoid duplicates.
    """
    payload = clean(
        name=name,
        description=description,
        parent=parent,
        default_location=default_location,
        structural=structural,
    )
    return await fetch(
        _category_list,
        "POST",
        "/api/part/category/",
        fields=CATEGORY_FIELDS,
        data=payload,
    )


@tool(
    "update_part_category",
    title="Update part category",
    read_only=False,
    destructive=False,
    idempotent=True,
)
async def update_part_category(
    id: Annotated[int, Field(description="The category ID (pk) to update")],
    name: Annotated[str, Field(description="New category name")] = "",
    description: Annotated[str, Field(description="New category description")] = "",
    parent: Annotated[
        int,
        Field(description="New parent category ID. 0 or omit to leave unchanged."),
    ] = 0,
    default_location: Annotated[
        int,
        Field(
            description="New default stock location ID. 0 or omit to leave unchanged."
        ),
    ] = 0,
) -> dict:
    """Update an existing part category's fields. Only provided fields are changed. Use this
    to rename categories, change descriptions, or move a category under a different parent.
    """
    payload = changes(
        name=name,
        description=description,
        parent=parent,
        default_location=default_location,
    )
    return await fetch(
        _category_detail,
        "PATCH",
        f"/api/part/category/{id}/",
        fields=CATEGORY_FIELDS,
        data=payload,
        pk=id,
    )


@tool(
    "delete_part_category",
    title="Delete part category",
    read_only=False,
    destructive=True,
)
async def delete_part_category(
    id: Annotated[int, Field(description="The category ID (pk) to delete")],
) -> str:
    """Delete a part category. The category must have no parts or sub-categories."""
    # InvenTree 1.5+ (API 491) requires both flags in the DELETE body; false keeps the
    # parts and sub-categories (they move up to the parent), as before.
    await call_view(
        _category_detail(),
        "DELETE",
        f"/api/part/category/{id}/",
        data={"delete_parts": False, "delete_child_categories": False},
        pk=id,
    )
    return text(f"Category {id} deleted successfully.")


access("search_part_categories", _category_list, "GET")
access("list_part_categories", _category_list, "GET")
access("create_part_category", _category_list, "POST")
access("update_part_category", _category_detail, "PATCH")
access("delete_part_category", _category_detail, "DELETE")
