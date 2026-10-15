# T3: write tools for stock and locations (Sonnet)

Read first: `docs/plans/tools/00-overview.md` (binding: sections 3, 4, 5), `docs/plans/tools/T1-report.md` (resolved
view names: StockAdd / StockRemove / StockTransfer and the detail views), `inventree_mcp/tools/_common.py`,
`inventree_mcp/tools/stock.py` and `tools/locations.py` (append to them), the Go sources
`vendor/syntaxerr66-inventree-mcp/internal/tools/{stock,locations}.go`.

## Deliverables

1. `tools/stock.py`: append `add_stock`, `stock_add_quantity`, `stock_remove_quantity`, `stock_transfer`,
   `delete_stock_item` per the table. `items` is a list of `{pk: int, quantity: float}` (a pydantic model or TypedDict
   so the schema is explicit). `add_stock`'s `serial`: verify on the dev stack what the StockList POST serializer accepts
   (`serial_numbers` on current InvenTree); keep the input name `serial` and send the working field; when the view
   answers a list, return the first item compacted. The adjustment tools answer the Go texts on success. ACCESS entries:
   add_stock -> POST StockList, the three adjustments -> POST their view, delete -> DELETE StockDetail.
2. `tools/locations.py`: append `create_stock_location`, `update_stock_location`, `delete_stock_location` per the table.
3. Tests appended to `tests/test_tools_stock.py` and `tests/test_tools_locations.py` (T1 created them with the read
   tests), on the dev stack with `MCP_READ_ONLY` off and MCP roles granted (see `tests/oauth_helpers.grant_mcp` and
   `tests/test_policy.py`): per tool the happy path (quantities verified by reading the item back through `get_stock_item`
   or the ORM; transfer moves the quantity to the target location; remove below zero fails with the 400 text; delete
   answers the Go text), validation 400 texts, 404, the permission text for a user lacking the MCP role, the read-only
   gate text, and a `tools/list` check of titles/annotations.
4. `docs/plans/tools/T3-report.md`: what was built, the `serial` finding, test counts and commands, spec errors.

## Boundaries

Only the files above; shared code requests go into the report, not into `_common.py`/`proxy.py`. No git. Lint and tests
as in the overview; finish with `dev/test.sh inventree_mcp.tests.test_tools_stock` and `... test_tools_locations` green.
T2 works in parallel on parts/categories/images: never touch their files.
