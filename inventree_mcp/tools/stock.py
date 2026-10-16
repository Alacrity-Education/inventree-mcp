"""Stock item tools: get_stock, get_stock_item, add_stock, stock_add_quantity,
stock_remove_quantity, stock_transfer, delete_stock_item.

Thin wrappers around stock.api.StockList / StockDetail (and StockAdd / StockRemove /
StockTransfer for T3) through proxy.call_view(). Both reads ask for ?part_detail=true so
every item carries its part's (pk, name, full_name). Names, inputs, defaults and
descriptions are the Go server's (docs/plans/tools/00-overview.md).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from ..proxy import call_view
from ..view_resolution import resolve_view
from ._common import (
    DEFAULTS,
    STOCK_ITEM_FIELDS,
    access,
    clean,
    fetch,
    listing,
    text,
    tool,
)


def _stock_list() -> type | None:
    return resolve_view("stock.api", "StockList")


def _stock_detail() -> type | None:
    return resolve_view("stock.api", "StockDetail")


def _stock_add() -> type | None:
    return resolve_view("stock.api", "StockAdd")


def _stock_remove() -> type | None:
    return resolve_view("stock.api", "StockRemove")


def _stock_transfer() -> type | None:
    return resolve_view("stock.api", "StockTransfer")


class StockAdjustment(BaseModel):
    """One stock item of an adjustment or transfer (the Go StockAdjustment)."""

    pk: Annotated[int, Field(description="Stock item ID")]
    quantity: Annotated[
        float,
        Field(description="Quantity to add (use stock_remove for subtraction)"),
    ]


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


@tool("add_stock", title="Add stock", read_only=False, destructive=False)
async def add_stock(
    part: Annotated[int, Field(description="Part ID to add stock for (required)")],
    quantity: Annotated[float, Field(description="Quantity to add (required)")],
    location: Annotated[
        int,
        Field(
            description="Location ID where stock will be stored. 0 or omit for no location."
        ),
    ] = 0,
    batch: Annotated[str, Field(description="Batch code")] = "",
    serial: Annotated[
        str, Field(description="Serial number (for trackable parts)")
    ] = "",
    notes: Annotated[str, Field(description="Notes about this stock item")] = "",
) -> dict:
    """Add stock by creating a new stock item. Requires a part ID and quantity. Optionally
    specify a location. Use search_parts to find the part ID and search_stock_locations to
    find the location ID first.
    """
    # StockList.create() drops "serial" and reads "serial_numbers" instead (InvenTree 1.5.7).
    # POST answers a list of the created items; fetch() returns the first.
    return await fetch(
        _stock_list,
        "POST",
        "/api/stock/",
        fields=STOCK_ITEM_FIELDS,
        data=clean(
            part=part,
            quantity=quantity,
            location=location,
            batch=batch,
            serial_numbers=serial,
            notes=notes,
        ),
    )


_ITEMS = Annotated[
    list[StockAdjustment],
    Field(description="List of stock items to adjust"),
]


@tool(
    "stock_add_quantity",
    title="Add stock quantity",
    read_only=False,
    destructive=False,
)
async def stock_add_quantity(
    items: _ITEMS,
    notes: Annotated[str, Field(description="Notes about this adjustment")] = "",
) -> str:
    """Add quantity to existing stock items. Use this to increase stock levels without
    creating new stock entries. Provide the stock item PK (not part ID) and the quantity to
    add.
    """
    await call_view(
        _stock_add(),
        "POST",
        "/api/stock/add/",
        data=clean(items=[i.model_dump() for i in items], notes=notes),
    )
    return text("Stock quantity updated successfully.")


@tool(
    "stock_remove_quantity",
    title="Remove stock quantity",
    read_only=False,
    destructive=True,
)
async def stock_remove_quantity(
    items: _ITEMS,
    notes: Annotated[str, Field(description="Notes about this adjustment")] = "",
) -> str:
    """Remove quantity from existing stock items. Provide the stock item PK (not part ID)
    and the quantity to remove.
    """
    await call_view(
        _stock_remove(),
        "POST",
        "/api/stock/remove/",
        data=clean(items=[i.model_dump() for i in items], notes=notes),
    )
    return text("Stock quantity removed successfully.")


@tool("stock_transfer", title="Transfer stock", read_only=False, destructive=False)
async def stock_transfer(
    items: Annotated[
        list[StockAdjustment],
        Field(description="List of stock items to transfer (pk and quantity)"),
    ],
    location: Annotated[int, Field(description="Destination location ID")],
    notes: Annotated[str, Field(description="Notes about this transfer")] = "",
) -> str:
    """Transfer stock items to a different location. Moves the specified quantity of each
    stock item to the target location.
    """
    await call_view(
        _stock_transfer(),
        "POST",
        "/api/stock/transfer/",
        data=clean(
            items=[i.model_dump() for i in items], location=location, notes=notes
        ),
    )
    return text("Stock transferred successfully.")


@tool("delete_stock_item", title="Delete stock item", read_only=False, destructive=True)
async def delete_stock_item(
    id: Annotated[int, Field(description="The stock item ID (pk) to delete")],
) -> str:
    """Delete a stock item. This is destructive and cannot be undone."""
    await call_view(_stock_detail(), "DELETE", f"/api/stock/{id}/", pk=id)
    return text(f"Stock item {id} deleted successfully.")


access("get_stock", _stock_list, "GET")
access("get_stock_item", _stock_detail, "GET")
access("add_stock", _stock_list, "POST")
access("stock_add_quantity", _stock_add, "POST")
access("stock_remove_quantity", _stock_remove, "POST")
access("stock_transfer", _stock_transfer, "POST")
access("delete_stock_item", _stock_detail, "DELETE")
