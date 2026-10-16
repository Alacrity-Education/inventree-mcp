"""Shared helpers for the MCP tools (the only shared tool code; tool modules hold no duplicates).

- tool(): registers a tool with its title and the four MCP annotation hints, the one place
  annotations are set. Tools are unstructured (no output schema is published): a dict result
  reaches the client as indented JSON text, as the Go server returns it.
- ACCESS / access(): the InvenTree view and HTTP method whose permission decides a tool's
  visibility (tool_visibility.py reads it). Every tool except search_part_images declares one.
- listing(): a paginated GET through proxy.call_view(), reduced to {"count", "results"}.
- compact(): a fixed, ordered field selection of an InvenTree object.
- fetch(): one call through proxy.call_view(), reduced with compact() (get, create, update).
- clean(): a payload / query builder with the Go server's omission rules.
- changes(): clean() for the update tools; "no fields to update" when nothing is left.
- text(): a plain-text result.

The tool set, names, inputs, defaults and descriptions follow the Go server
github.com/syntaxerr66/inventree-mcp (see docs/plans/tools/00-overview.md).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..mcp_server import mcp
from ..proxy import call_view

#: Go defaults: search 25; list_parts / get_stock 50; the location and category lists 100.
DEFAULTS = {"search": 25, "list": 50, "tree": 100}
#: Upper bound of a page; larger requests are clamped.
MAX_LIMIT = 500

#: Fields the Go server treats as unset when 0 ("0 or omit"): ids, and minimum_stock.
_ZERO_IS_UNSET = frozenset({
    "category",
    "parent",
    "location",
    "default_location",
    "part",
    "minimum_stock",
})

#: Nested objects kept by compact(), with their own field selection.
_NESTED_FIELDS: dict[str, tuple[str, ...]] = {
    "part_detail": ("pk", "name", "full_name"),
}

PART_FIELDS = (
    "pk",
    "name",
    "full_name",
    "description",
    "category",
    "category_name",
    "active",
    "in_stock",
    "total_in_stock",
    "minimum_stock",
    "units",
    "IPN",
    "keywords",
    "link",
    "purchaseable",
    "assembly",
    "component",
    "trackable",
    "virtual",
    "image",
    "thumbnail",
    "tags",
)
#: part_detail is reduced to (pk, name, full_name); the view needs ?part_detail=true.
STOCK_ITEM_FIELDS = (
    "pk",
    "part",
    "quantity",
    "serial",
    "batch",
    "location",
    "in_stock",
    "status",
    "status_text",
    "notes",
    "updated",
    "tags",
    "part_detail",
)
LOCATION_FIELDS = (
    "pk",
    "name",
    "description",
    "parent",
    "pathstring",
    "level",
    "items",
    "sublocations",
    "structural",
    "external",
    "icon",
    "tags",
)
CATEGORY_FIELDS = (
    "pk",
    "name",
    "description",
    "parent",
    "pathstring",
    "level",
    "part_count",
    "subcategories",
    "starred",
    "structural",
    "icon",
    "default_location",
)

ViewLoader = Callable[[], type | None]

#: tool name -> (view loader, HTTP method) whose permission decides the tool's visibility.
ACCESS: dict[str, tuple[ViewLoader, str]] = {}


def tool(
    name: str,
    *,
    title: str,
    read_only: bool,
    destructive: bool | None = None,
    idempotent: bool | None = None,
    open_world: bool = False,
) -> Callable:
    """Register the decorated coroutine as the MCP tool *name*.

    Sets the title (on the tool and in its annotations) and all four hints. Unset hints
    take the tool kind's value: a read tool is non-destructive and idempotent; a write tool
    is non-idempotent and, unless stated, destructive (the MCP defaults). The description
    is the function's docstring with its whitespace collapsed (the Go descriptions are one
    line; the docstrings are wrapped for the line length).
    """
    if destructive is None:
        destructive = not read_only
    if idempotent is None:
        idempotent = read_only
    annotations = ToolAnnotations(
        title=title,
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    )

    def decorator(fn: Callable) -> Callable:
        description = " ".join((inspect.getdoc(fn) or "").split())
        return mcp.tool(
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            structured_output=False,
        )(fn)

    return decorator


def access(tool_name: str, view_loader: ViewLoader, method: str) -> None:
    """Declare the view + HTTP method whose permission decides *tool_name*'s visibility."""
    ACCESS[tool_name] = (view_loader, method.upper())


def clamp_limit(limit: int, default: int) -> int:
    """*default* for limit <= 0, else *limit* capped at MAX_LIMIT."""
    if limit <= 0:
        return default
    return min(limit, MAX_LIMIT)


async def listing(
    view_loader: ViewLoader,
    path: str,
    params: dict[str, Any],
    *,
    limit: int,
    default: int,
    fields: tuple[str, ...],
) -> dict:
    """GET a list view with limit/offset; returns {"count": n, "results": [compact...]}.

    *params* are passed through clean() (so 0 ids and empty strings are omitted); a negative
    offset becomes 0. next/previous are dropped, as the Go server does.
    """
    query = clean(**params)
    query["limit"] = clamp_limit(limit, default)
    query["offset"] = max(int(query.get("offset", 0)), 0)
    body = await call_view(view_loader(), "GET", path, query_params=query)
    if isinstance(body, list):  # an unpaginated view answers a bare list
        return {"count": len(body), "results": [compact(row, fields) for row in body]}
    return {
        "count": body.get("count", 0),
        "results": [compact(row, fields) for row in body.get("results", [])],
    }


async def fetch(
    view_loader: ViewLoader,
    method: str,
    path: str,
    *,
    fields: tuple[str, ...],
    data: dict[str, Any] | None = None,
    query_params: dict[str, Any] | None = None,
    **view_kwargs: Any,
) -> dict:
    """call_view() then compact(*fields*): the get, create, update and set-image tools.

    A list response (POST StockList answers the created items as a list) is reduced to its
    first item; an empty list raises ToolError("no stock items returned").
    """
    body = await call_view(
        view_loader(),
        method,
        path,
        data=data,
        query_params=query_params,
        **view_kwargs,
    )
    if isinstance(body, list):
        if not body:
            raise ToolError("no stock items returned")
        body = body[0]
    return compact(body, fields)


def compact(obj: dict, fields: tuple[str, ...]) -> dict:
    """Only *fields* of *obj*, in that order; missing keys become None.

    part_detail keeps (pk, name, full_name).
    """
    result = {}
    for field in fields:
        value = obj.get(field)
        nested = _NESTED_FIELDS.get(field)
        if nested is not None and isinstance(value, dict):
            value = {key: value.get(key) for key in nested}
        result[field] = value
    return result


def clean(**kwargs: Any) -> dict:
    """The keyword arguments without the values the Go server treats as unset.

    Dropped: None; "" strings; 0 for ids (category, parent, location, default_location,
    part) and minimum_stock. Booleans are kept, False included (None means "omitted").
    """
    payload = {}
    for key, value in kwargs.items():
        if value is None or (isinstance(value, str) and value == ""):
            continue
        if (
            key in _ZERO_IS_UNSET
            and not isinstance(value, bool)
            and isinstance(value, int | float)
            and value == 0
        ):
            continue
        payload[key] = value
    return payload


def changes(**kwargs: Any) -> dict:
    """clean(**kwargs); ToolError("no fields to update") when nothing is left (the Go update tools)."""
    payload = clean(**kwargs)
    if not payload:
        raise ToolError("no fields to update")
    return payload


def text(message: str) -> str:
    """A plain-text tool result ("Part 12 deleted successfully.")."""
    return message
