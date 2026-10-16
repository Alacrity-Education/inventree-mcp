# T3 report: stock and location write tools

Date: 2026-10-16. Base: main at 03a4e1d. Nothing staged or committed.

## What was built

- `tools/stock.py` (appended): `add_stock`, `stock_add_quantity`, `stock_remove_quantity`, `stock_transfer`,
  `delete_stock_item`, with `StockAdjustment` (pydantic model: `pk: int`, `quantity: float`, Go field texts) so the
  `items` schema is explicit. Descriptions, parameter texts, titles and annotations are the Go/overview ones. Success
  texts are the Go texts. ACCESS: add_stock -> StockList POST; the three adjustments -> StockAdd / StockRemove /
  StockTransfer POST; delete_stock_item -> StockDetail DELETE.
- `tools/locations.py` (appended): `create_stock_location` (POST StockLocationList, `structural: bool | None`),
  `update_stock_location` (PATCH, `changes()` gives "no fields to update"; idempotent=true), `delete_stock_location`
  (DELETE). ACCESS: POST / PATCH / DELETE on the list and detail views.
- Only `_common.py` helpers are used: `fetch()` (first item of the list the StockList POST answers), `clean()`,
  `changes()`, `text()`, `tool()`, `access()`; deletes and adjustments call `call_view(loader(), ...)` directly.

## The `serial` finding

InvenTree 1.5.7's `StockList.create()` pops `serial` and discards it ("We use 'serial_numbers' for item creation").
`add_stock` keeps the input name `serial` and sends `serial_numbers`. Verified with a trackable part: the item comes
back with `serial == "SN-77"` and quantity 1. The response is always a list, `fetch()` returns the first item.

## Spec errors / differences found on the dev stack (1.5.7)

1. **`delete_stock_location` needs a body.** `StockLocationDetail.destroy()` validates `LocationDeleteSerializer` and
   answers 400 "delete_stock_items: This field is required. delete_sub_locations: This field is required." for a bare
   DELETE (which is what the Go server and the overview's call shape send). The tool sends
   `{"delete_stock_items": false, "delete_sub_locations": false}`. This is also the safe choice: stock and
   sub-locations are moved up to the parent, never deleted. So InvenTree does not enforce "must be empty"; the Go
   description (verbatim, as required) says it must be. Tested (`test_delete_does_not_enforce_empty_on_1_5_7`).
   Not checked on 1.6; the serializer is the same code path in the vendored InvenTree checkout.
2. **Removing more than the quantity is not an error.** `StockItem.take_stock()` removes `min(quantity, stock)`, and the
   emptied item is deleted (delete_on_deplete). The spec's "remove below zero fails with the 400 text" is therefore
   not testable; the tests assert the clamp (item gone) and instead assert the 400 for a negative quantity
   (`items[0].quantity: Ensure this value is greater than or equal to 0.`), an unknown pk (`items[0].pk`), an empty
   list ("A list of stock items must be provided") and an unknown destination location.
3. A partial transfer splits the item (the source keeps the remainder, a new item holds the moved quantity); tested.

## Tests

`test_tools_stock.py` (19 tests, 17 new) and `test_tools_locations.py` (15 tests, 12 new), on 1.5.7 with
MCP_READ_ONLY off (`set_setting`) and all MCP roles granted (`MCPToolTestCase`):

- tools/list titles, the four hints, no outputSchema, required inputs;
- happy paths with the state read back through the ORM and `get_stock_item` / `get_stock_location` (add with/without
  optional fields, serial, add/remove/transfer incl. split, delete, create with parent and structural, "32"/"true"
  string coercion, update incl. move, "no fields to update", delete);
- 400 texts, 404 texts, the read-only gate text for all 8 write tools (and that they stay listed), the 403 text for a
  user lacking the InvenTree role, and for a user with InvenTree roles but without the MCP role (add_stock).

Commands (dev stack): `dev/test.sh inventree_mcp.tests.test_tools_stock` Ran 19 OK;
`dev/test.sh inventree_mcp.tests.test_tools_locations` Ran 15 OK. Ruff check/format (preview) on `inventree_mcp` and
`check --isolated` on the tests: clean. Note: running two test labels at once (T2 in parallel) caused a Postgres
deadlock in `build_default_values` once; a rerun was green.

## Requests for shared code / for others

- Overview section 4 / 6a: amend the delete_stock_location row ("DELETE StockDetail" body flags) and the "remove below
  zero fails" expectation for T4's e2e script (`dev/tools_e2e.py` must not expect an error there).
- Consider (T4/orchestrator) a README note that `delete_stock_location` never deletes content, it re-parents it.
