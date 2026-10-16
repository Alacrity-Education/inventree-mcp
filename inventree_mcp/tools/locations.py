"""Stock location tools: search_stock_locations, get_stock_location, list_stock_locations (T1);
the location write tools follow (T3).

Thin wrappers around stock.api.StockLocationList / StockLocationDetail through
proxy.call_view(). Names, inputs, defaults and descriptions are the Go server's
(docs/plans/tools/00-overview.md).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..view_resolution import resolve_view
from ._common import DEFAULTS, LOCATION_FIELDS, access, fetch, listing, tool


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


access("search_stock_locations", _location_list, "GET")
access("get_stock_location", _location_detail, "GET")
access("list_stock_locations", _location_list, "GET")
