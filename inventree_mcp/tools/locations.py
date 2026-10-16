"""Stock location tools: search_stock_locations, get_stock_location, list_stock_locations,
create_stock_location, update_stock_location, delete_stock_location.

Thin wrappers around stock.api.StockLocationList / StockLocationDetail through
proxy.call_view(). Names, inputs, defaults and descriptions are the Go server's
(docs/plans/tools/00-overview.md).

delete_stock_location never deletes contents: it sends delete_stock_items and
delete_sub_locations as false, and InvenTree moves the location's stock items and
sub-locations to its parent (no location / top level when it has none). The Go description
("must be empty") is kept verbatim but is not enforced.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..proxy import call_view
from ..view_resolution import resolve_view
from ._common import (
    DEFAULTS,
    LOCATION_FIELDS,
    access,
    changes,
    clean,
    fetch,
    listing,
    text,
    tool,
)


def _location_list() -> type | None:
    return resolve_view("stock.api", "StockLocationList")


def _location_detail() -> type | None:
    return resolve_view("stock.api", "StockLocationDetail")


@tool("search_stock_locations", title="Search stock locations", read_only=True)
async def search_stock_locations(
    search: Annotated[
        str,
        Field(
            description="Search query to find locations by name or description. "
            "Supports partial/fuzzy matching (e.g. 'green' finds 'Green 1')."
        ),
    ],
    limit: Annotated[
        int, Field(description="Maximum number of results (default 25)")
    ] = DEFAULTS["search"],
) -> dict:
    """Search for stock locations by name or description. Use this to find locations when
    the user gives an approximate name (e.g., 'green box 2' or 'office'). Returns matching
    locations with their full path (e.g., 'Office/Green 1'). The pathstring field shows the
    hierarchical location path.
    """
    return await listing(
        _location_list,
        "/api/stock/location/",
        {"search": search},
        limit=limit,
        default=DEFAULTS["search"],
        fields=LOCATION_FIELDS,
    )


@tool("get_stock_location", title="Get stock location", read_only=True)
async def get_stock_location(
    id: Annotated[int, Field(description="The location ID (pk) to retrieve")],
) -> dict:
    """Get detailed information about a specific stock location by its ID, including its
    parent path and number of items.
    """
    return await fetch(
        _location_detail,
        "GET",
        f"/api/stock/location/{id}/",
        fields=LOCATION_FIELDS,
        pk=id,
    )


@tool("list_stock_locations", title="List stock locations", read_only=True)
async def list_stock_locations(
    parent: Annotated[
        int,
        Field(description="Filter by parent location ID. 0 or omit to list all."),
    ] = 0,
    limit: Annotated[
        int, Field(description="Maximum number of results (default 100)")
    ] = DEFAULTS["tree"],
    offset: Annotated[int, Field(description="Offset for pagination")] = 0,
) -> dict:
    """List all stock locations, optionally filtered by parent location. Returns the full
    location hierarchy with pathstrings. Use this to see all available locations and their
    structure.
    """
    return await listing(
        _location_list,
        "/api/stock/location/",
        {"parent": parent, "offset": offset},
        limit=limit,
        default=DEFAULTS["tree"],
        fields=LOCATION_FIELDS,
    )


@tool(
    "create_stock_location",
    title="Create stock location",
    read_only=False,
    destructive=False,
)
async def create_stock_location(
    name: Annotated[str, Field(description="Location name (required)")],
    description: Annotated[str, Field(description="Location description")] = "",
    parent: Annotated[
        int,
        Field(description="Parent location ID. 0 or omit for top-level location."),
    ] = 0,
    structural: Annotated[
        bool | None,
        Field(
            description="If true, stock cannot be directly stored here "
            "(only in sub-locations)"
        ),
    ] = None,
) -> dict:
    """Create a new stock location. Locations can be nested (e.g., Office > Green 1).
    Provide a parent ID to create a sub-location. Always search for existing locations
    first to avoid duplicates.
    """
    return await fetch(
        _location_list,
        "POST",
        "/api/stock/location/",
        fields=LOCATION_FIELDS,
        data=clean(
            name=name, description=description, parent=parent, structural=structural
        ),
    )


@tool(
    "update_stock_location",
    title="Update stock location",
    read_only=False,
    destructive=False,
    idempotent=True,
)
async def update_stock_location(
    id: Annotated[int, Field(description="The location ID (pk) to update")],
    name: Annotated[str, Field(description="New location name")] = "",
    description: Annotated[str, Field(description="New location description")] = "",
    parent: Annotated[
        int,
        Field(description="New parent location ID. 0 or omit to leave unchanged."),
    ] = 0,
) -> dict:
    """Update an existing stock location's fields. Only provided fields are changed. Use
    this to rename locations, change descriptions, or move a location under a different
    parent.
    """
    return await fetch(
        _location_detail,
        "PATCH",
        f"/api/stock/location/{id}/",
        fields=LOCATION_FIELDS,
        data=changes(name=name, description=description, parent=parent),
        pk=id,
    )


@tool(
    "delete_stock_location",
    title="Delete stock location",
    read_only=False,
    destructive=True,
)
async def delete_stock_location(
    id: Annotated[int, Field(description="The location ID (pk) to delete")],
) -> str:
    """Delete a stock location. This is destructive and cannot be undone. The location must
    be empty (no stock items or sub-locations).
    """
    # StockLocationDetail.destroy() requires both flags in the body (400 "This field is
    # required" otherwise). False keeps sub-locations and stock: they move to the parent.
    await call_view(
        _location_detail(),
        "DELETE",
        f"/api/stock/location/{id}/",
        data={"delete_stock_items": False, "delete_sub_locations": False},
        pk=id,
    )
    return text(f"Location {id} deleted successfully.")


access("search_stock_locations", _location_list, "GET")
access("get_stock_location", _location_detail, "GET")
access("list_stock_locations", _location_list, "GET")
access("create_stock_location", _location_list, "POST")
access("update_stock_location", _location_detail, "PATCH")
access("delete_stock_location", _location_detail, "DELETE")
