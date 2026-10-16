"""Part tools: search_parts, get_part, list_parts (T1); create_part, update_part, delete_part,
set_part_image (T2).

Thin wrappers around part.api.PartList / PartDetail through proxy.call_view(), so permissions,
filtering and serialization are InvenTree's own. Names, inputs, defaults and descriptions are
the Go server's (docs/plans/tools/00-overview.md, section 4).
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..proxy import call_view
from ..view_resolution import resolve_view
from ._common import (
    DEFAULTS,
    PART_FIELDS,
    access,
    changes,
    clean,
    fetch,
    listing,
    text,
    tool,
)


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


@tool("create_part", title="Create part", read_only=False, destructive=False)
async def create_part(
    name: Annotated[str, Field(description="Part name (required)")],
    description: Annotated[str, Field(description="Part description")] = "",
    category: Annotated[
        int,
        Field(description="Category ID for the part. 0 or omit for uncategorized."),
    ] = 0,
    IPN: Annotated[str, Field(description="Internal Part Number")] = "",
    keywords: Annotated[str, Field(description="Keywords for search")] = "",
    units: Annotated[str, Field(description="Units of measure")] = "",
    minimum_stock: Annotated[int, Field(description="Minimum stock level")] = 0,
    purchaseable: Annotated[
        bool | None,
        Field(description="Whether the part can be purchased (default true)"),
    ] = None,
    component: Annotated[
        bool | None,
        Field(description="Whether the part is a component (default true)"),
    ] = None,
    assembly: Annotated[
        bool | None, Field(description="Whether the part is an assembly")
    ] = None,
    trackable: Annotated[
        bool | None,
        Field(description="Whether the part is trackable by serial number"),
    ] = None,
    virtual: Annotated[
        bool | None, Field(description="Whether the part is virtual (not physical)")
    ] = None,
    image_url: Annotated[
        str,
        Field(
            description="URL of an image to attach to the part. InvenTree downloads it "
            "server-side."
        ),
    ] = "",
) -> dict:
    """Create a new part in InvenTree. Returns the created part with its ID. IMPORTANT
    workflow: (1) search_parts to check for duplicates, (2) list_part_categories to see the
    FULL category hierarchy (check pathstring fields to understand nesting), (3) find the
    deepest, most specific category that fits this part — categories can be nested many
    levels deep (e.g. Electronic Components/Resistors/Through Hole/1/8 Watt), so always
    prefer the most specific match, (4) if no suitable category exists, create one with
    create_part_category under the most appropriate parent — do NOT put a part in an
    unrelated category just because it exists, and do NOT create top-level categories when
    the part belongs under an existing parent, (5) create the part with the correct
    category ID. Parts should always have a category.
    """
    payload = clean(
        name=name,
        description=description,
        category=category,
        IPN=IPN,
        keywords=keywords,
        units=units,
        minimum_stock=minimum_stock,
        purchaseable=purchaseable,
        component=component,
        assembly=assembly,
        trackable=trackable,
        virtual=virtual,
        remote_image=image_url,
    )
    return await fetch(
        _part_list, "POST", "/api/part/", fields=PART_FIELDS, data=payload
    )


@tool(
    "update_part",
    title="Update part",
    read_only=False,
    destructive=False,
    idempotent=True,
)
async def update_part(
    id: Annotated[int, Field(description="The part ID (pk) to update")],
    name: Annotated[str, Field(description="New part name")] = "",
    description: Annotated[str, Field(description="New description")] = "",
    category: Annotated[
        int, Field(description="New category ID. 0 or omit to leave unchanged.")
    ] = 0,
    active: Annotated[
        bool | None, Field(description="Whether the part is active")
    ] = None,
    IPN: Annotated[str, Field(description="New Internal Part Number")] = "",
    keywords: Annotated[str, Field(description="New keywords")] = "",
    units: Annotated[str, Field(description="New units of measure")] = "",
    minimum_stock: Annotated[
        int,
        Field(description="New minimum stock level. 0 or omit to leave unchanged."),
    ] = 0,
    image_url: Annotated[
        str,
        Field(
            description="URL of an image to set for this part. InvenTree downloads it "
            "server-side."
        ),
    ] = "",
) -> dict:
    """Update an existing part's fields. Only provided fields are changed. Use this to rename
    parts, change categories, update descriptions, or deactivate parts.
    """
    payload = changes(
        name=name,
        description=description,
        category=category,
        active=active,
        IPN=IPN,
        keywords=keywords,
        units=units,
        minimum_stock=minimum_stock,
        remote_image=image_url,
    )
    return await fetch(
        _part_detail,
        "PATCH",
        f"/api/part/{id}/",
        fields=PART_FIELDS,
        data=payload,
        pk=id,
    )


@tool("delete_part", title="Delete part", read_only=False, destructive=True)
async def delete_part(
    id: Annotated[int, Field(description="The part ID (pk) to delete")],
) -> str:
    """Delete a part from InvenTree. This automatically deactivates the part first (required
    by InvenTree). The part must have no stock items. This is destructive and cannot be
    undone.
    """
    path = f"/api/part/{id}/"
    part = await call_view(_part_detail(), "GET", path, pk=id)
    if part.get("active"):
        # InvenTree only deletes inactive parts.
        await call_view(_part_detail(), "PATCH", path, data={"active": False}, pk=id)
    await call_view(_part_detail(), "DELETE", path, pk=id)
    return text(f"Part {id} deleted successfully.")


@tool(
    "set_part_image",
    title="Set part image",
    read_only=False,
    destructive=False,
    idempotent=True,
)
async def set_part_image(
    id: Annotated[int, Field(description="The part ID (pk) to set the image for")],
    image_url: Annotated[
        str,
        Field(description="URL of the image. InvenTree downloads it server-side."),
    ],
) -> dict:
    """Set or replace the image for an existing part by providing an image URL. InvenTree
    downloads the image from the URL server-side. Use this after search_part_images to
    attach a product photo to a part.
    """
    if not image_url:
        raise ToolError("image_url is required")
    return await fetch(
        _part_detail,
        "PATCH",
        f"/api/part/{id}/",
        fields=PART_FIELDS,
        data={"remote_image": image_url},
        pk=id,
    )


access("search_parts", _part_list, "GET")
access("get_part", _part_detail, "GET")
access("list_parts", _part_list, "GET")
access("create_part", _part_list, "POST")
access("update_part", _part_detail, "PATCH")
access("delete_part", _part_detail, "DELETE")
access("set_part_image", _part_detail, "PATCH")
