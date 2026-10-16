# T4 report: documentation, live e2e, beta configuration

Date: 2026-10-16. Base: main at 7e9ec83. Nothing staged or committed. No file under `inventree_mcp/` touched.

## What changed

- `README.md`: intro rewritten for the 26 tools (no output schema, describe_filters or make_web_link text, no old tool
  names); new "Tools" section: workflow guidance, one table per resource with the inputs read from the code, "Write
  requirements" (`MCP_READ_ONLY` off, MCP add for creates and the three stock adjustments, change for updates and
  `set_part_image`, delete for deletes, `delete_part` needs change + delete, the interactive roles, why the adjustments
  need stock add), the image paragraph (`IMAGE_DOWNLOAD_ENABLED`, Google settings), "Delete semantics", "Credits"
  (syntaxerr66/inventree-mcp, linked). Plugin settings: `MCP_READ_ONLY` reworded, `IMAGE_DOWNLOAD_ENABLED`,
  `GOOGLE_API_KEY`, `GOOGLE_CSE_ID` added (texts match `core.py`). Development section lists `dev/tools_e2e.py`.
- `docs/OPERATIONS.md`: settings rows (`MCP_READ_ONLY` changed, the three new ones); section 6 "A write is refused"
  (table: read-only, 403 for MCP or interactive role, `IMAGE_DOWNLOAD_ENABLED`, `image download failed`, part with stock,
  400) with the note that InvenTree's StockAdd/StockRemove/StockTransfer declare only a general read scope and the
  plugin pre-checks the stock add role; section 7: tool-call logging includes write payloads; checklist row 3 no
  longer promises a `next` link.
- `AGENTS.md`: new "Tool set rule" (Go server as reference, overview 6a to 6c) and `tools/_common.py` rules, module
  layout, tool_visibility / tool_logging / view_resolution entries, test files and the scaffolding in
  `tests/test_transport.py`, `dev/tools_e2e.py`, lint line for the dev scripts; every `test_mcp` mention fixed.
- `dev/README.md`: `test_mcp` fixes, `dev/tools_e2e.py` in the command list, layout table and a new "Tools end-to-end
  run" section.
- `dev/tools_e2e.py` (new, executable): see output below.
- `dev/permissions_e2e.py`, `dev/e2e/run.py`: new tool names (`get_stock`, `list_stock_locations`, ..., `get_part`
  takes `id`), `STOCK_TOOLS` set, the `next` link assertion dropped (listings no longer return it).
- `deploy/beta/configure_inventree.py`, `.env.example`: `MCP_WRITE` (default 1): `MCP_READ_ONLY` False and all four MCP
  flags on part, part_category, stock, stock_location for the groups of `MCP_GROUPS`; `MCP_WRITE=0` sets
  `MCP_READ_ONLY` True and grants nothing beyond view. Also fixed an existing ruff I001 in that file (import order).
  Not run.

## Decisions and deviations

- `deploy/beta/up.sh` (one `-e "MCP_WRITE=${MCP_WRITE:-1}"` added to the `configure` call) and `deploy/beta/README.md`
  (one paragraph) were edited although the spec names only `configure_inventree.py` and `.env.example`: without the
  forward, `.env` could never reach the script, so the switch would not work. `bash -n` is clean; not run.
- `MCP_WRITE=0` does not remove flags a group already holds; it sets `MCP_READ_ONLY` True, which already strips every
  write scope.
- Pre-existing problem found in the dev scripts: since 0.4.2 `MCP_FILTER_TOOL_LIST` defaults to off, so the tool list
  assertions of `permissions_e2e.py` and `e2e/run.py` scenario 6 (parts-only list) were already failing. They now switch
  the setting on for their run and restore it (`permissions_e2e.py` via `set_tool_filter()`, `e2e/run.py` through its
  managed settings).
- The dev server had to be restarted (`dev/compose.sh restart inventree-server inventree-worker`): it still ran the T1
  code (`tools/list` answered 10 tools).
- `tools_e2e.py` waits for settings changes to reach the web process by retrying while the answer is the read-only text.
- The OAuth-token variant of the e2e (overview section 7) is not in `tools_e2e.py`; it uses an API token as the spec says.
  OAuth tokens run through the same scoped-token path (`permissions_e2e.py` covers OAuth reads).
- Install snippet in README still names 0.4.2 (release is the orchestrator's).

## Commands and results

- `uvx ruff@0.16.0 check --isolated dev/*.py dev/e2e/*.py deploy/beta/*.py`: All checks passed.
- `python3 dev/tools_e2e.py` (dev stack, 1.5.7, http://localhost:8080), exit 0, output:

```
[01] PASS setup: MCP_READ_ONLY off (was True), MCP roles of admin's groups granted
[02] PASS API token of admin obtained
[03] PASS tools/list lists 26 tools
[04] PASS search_part_categories -> 28 found, parent category 23
[05] PASS create_part_category -> pk 31 under 23
[06] PASS create_part -> pk 927 in category 31
[07] PASS add_stock -> stock item 1390 quantity 10
[08] PASS stock_add_quantity -> 'Stock quantity updated successfully.'; quantity 15
[09] PASS stock_remove_quantity -> 'Stock quantity removed successfully.'; quantity 12
[10] PASS create_stock_location -> pk 40
[11] PASS stock_transfer -> 'Stock transferred successfully.'
[12] PASS get_stock -> 2 items totalling 12; 4 in location 40
[13] PASS delete_stock_item 1390 -> 'Stock item 1390 deleted successfully.'
[14] PASS delete_stock_item 1391 -> 'Stock item 1391 deleted successfully.'
[15] PASS delete_part -> 'Part 927 deleted successfully.'
[16] PASS delete_part_category -> 'Category 31 deleted successfully.'
[17] PASS delete_stock_location -> 'Location 40 deleted successfully.'
[18] PASS MCP_READ_ONLY on: create_part_category answers 'Error executing tool create_part_category: This MCP server is running in read-only mode; w'
restored: MCP_READ_ONLY and the MCP roles of admin's groups
OK: 18 steps
```

  Afterwards verified through `dev/shell.sh`: `MCP_READ_ONLY` True, MCP part/stock flags back to view only, no
  `tools-e2e` API token left.
- `python3 dev/permissions_e2e.py`: OK ("MCP permission set end to end"; tools/list 4 tools with the filter on,
  `get_stock` refused, `list_parts` + `get_part` with OAuth and API token).
- `python3 dev/e2e/run.py --only 2 --only 6`: 2 passed, 0 failed. The other eight scenarios were not rerun (the change
  is limited to the tool names and the filter setting used by scenarios 2 and 6; scenario 4 stops Authentik).
- Not run: `deploy/beta/up.sh` and `configure_inventree.py` (the orchestrator runs the beta); no unit tests (no code or
  tests changed).
