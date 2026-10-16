"""Stock item tools: get_stock, get_stock_item (T1); the stock write tools follow (T3).

Thin wrappers around stock.api.StockList / StockDetail (and StockAdd / StockRemove /
StockTransfer for T3) through proxy.call_view(). Both reads ask for ?part_detail=true so
every item carries its part's (pk, name, full_name). Names, inputs, defaults and
descriptions are the Go server's (docs/plans/tools/00-overview.md).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..view_resolution import resolve_view
from ._common import DEFAULTS, STOCK_ITEM_FIELDS, access, fetch, listing, tool


def _stock_list() -> type | None:
    return resolve_view("stock.api", "StockList")


def _stock_detail() -> type | None:
    return resolve_view("stock.api", "StockDetail")


@tool("get_stock", title="List stock items", read_only=True)
async def get_stock(
    part: Annotated[
        int, Field(description="Filter by part ID. 0 or omit to list all.")
    ] = 0,
    location: Annotated[
        int, Field(description="Filter by location ID. 0 or omit to list all.")
    ] = 0,
    limit: Annotated[
        int, Field(description="Maximum number of results (default 50)")
    ] = DEFAULTS["list"],
    offset: Annotated[int, Field(description="Offset for pagination")] = 0,
) -> dict:
    """List stock items, optionally filtered by part ID and/or location ID. Returns stock
    quantities, locations, and status for each item.
    """
    return await listing(
        _stock_list,
        "/api/stock/",
        {"part": part, "location": location, "offset": offset, "part_detail": "true"},
        limit=limit,
        default=DEFAULTS["list"],
        fields=STOCK_ITEM_FIELDS,
    )


@tool("get_stock_item", title="Get stock item", read_only=True)
async def get_stock_item(
    id: Annotated[int, Field(description="The stock item ID (pk) to retrieve")],
) -> dict:
    """Get detailed information about a specific stock item by its ID."""
    return await fetch(
        _stock_detail,
        "GET",
        f"/api/stock/{id}/",
        fields=STOCK_ITEM_FIELDS,
        query_params={"part_detail": "true"},
        pk=id,
    )


access("get_stock", _stock_list, "GET")
access("get_stock_item", _stock_detail, "GET")
