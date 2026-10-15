# T1: framework, deletions, the 11 read tools, salvaged tests (Opus)

Read first: `docs/plans/tools/00-overview.md` (binding), `AGENTS.md`, `inventree_mcp/proxy.py`, `tool_visibility.py`,
`mcp_server.py`, `tools/_common.py`, `tools/parts.py` (the old template), `test_mcp.py` (what to salvage), and the Go
sources (`vendor/syntaxerr66-inventree-mcp/internal/tools/*.go`, `client.go`, `coerce.go`). The mcp SDK installed in the dev
stack is mcp 2.x (`mcp.server.mcpserver.MCPServer`): check `MCPServer.tool()`'s signature for `title`/`annotations`
(`dev/shell.sh -c "import inspect, mcp.server.mcpserver as m; print(inspect.signature(m.MCPServer.tool))"`) and
`mcp.types.ToolAnnotations`.

## Deliverables

1. Deletions of section 1 of the overview (old tool modules, discovery, output_schemas, schema_introspection,
   filter_introspection, test_mcp.py). `git rm` is not yours: delete the files; the orchestrator commits.
2. `tools/_common.py` with exactly the interface of section 3 (names and semantics binding; add private helpers freely).
   `tool()` wraps `mcp.tool(...)` and is the only place annotations/titles are set. `ACCESS` + `access()` replace
   `discovery.RESOURCE_LOADERS`; `tool_visibility.py` reads `ACCESS` and checks `user_has_access(view, method)` with the
   declared method (today it hard-codes "GET"). Keep tool_visibility's MCP_FILTER_TOOL_LIST behaviour.
3. `proxy.py`: the error texts of section 5 (map DRF/HTTP status to the Go-style messages; shape 400 bodies into
   `field: message` lines; truncate at 500 chars; never include credentials). Keep `call_view`'s signature, the
   MCP_READ_ONLY gate and `user_has_access`. Document in the module docstring.
4. The 11 read tools in `tools/parts.py` (search_parts, get_part, list_parts), `tools/categories.py`
   (search_part_categories, list_part_categories), `tools/stock.py` (get_stock, get_stock_item), `tools/locations.py`
   (search_stock_locations, get_stock_location, list_stock_locations), exactly per the table in section 4: names,
   titles, annotations, inputs with the Go defaults, descriptions verbatim from the Go code, compact outputs. Each module
   ends with its `access(...)` declarations. `tools/images.py` exists as an empty module with a docstring (T2 fills it);
   `mcp_server.py` imports the five modules and carries the new instructions text (section 5, last bullet).
   `tools/__init__.py` docstring updated.
5. `server_card.py`: review its text for mentions of the old tools; keep it tools-free.
6. `core.py`: settings `GOOGLE_API_KEY` (protected: InvenTree plugin settings support `"protected": True`) and
   `GOOGLE_CSE_ID`, both default ""; `PLUGIN_VERSION = "0.5.0"` in `inventree_mcp/__init__.py`; the `MCP_READ_ONLY`
   description updated to name the write tools it blocks.
7. Tests:
   - `tests/test_transport.py`: salvage from `test_mcp.py` the classes MCPTransportTest (incl. the tools/list filter tests,
     with `list_parts` still a valid name and `get_stock` replacing `list_stock_items`), ToolLoggingTest, PluginSettingsTest,
     ServerCardTest, WellKnownCompatTest, ViewResolutionTest, ResolveViewAnyTest, and the MCP_READ_ONLY tests
     (`test_mcp.py:634-650` area) rewritten against `call_view` with a stock view and POST. Keep their intent and
     assertions; adapt imports (`from .oauth_helpers import grant_mcp`, `_raw_api_token` moves here and
     `tests/test_membership.py` + `tests/test_policy.py` import it from `.test_transport`... better: move
     `_raw_api_token` and `API_TOKEN_V2_MIN_API_VERSION` into `tests/oauth_helpers.py` and update the two imports).
   - `tests/test_tools_framework.py`: `tool()` sets title and the four hints; every registered tool has an ACCESS entry
     except search_part_images (list the names: the test must fail if a tool is added without one); `listing()` clamps
     and defaults; `compact()` keeps order and fills None; `clean()` omission rules (0 ids dropped, "" dropped, False
     booleans kept, None dropped); coercion over the wire (`tools/call` with `{"id": "1"}` and `{"limit": "5"}` reaches
     the tool as ints; `{"active": "true"}` becomes True) using the transport helper from test_transport; the error
     texts for 403 (a user without the role), 404 (missing pk), 400 (invalid payload on a POST through call_view with
     MCP_READ_ONLY off); the read-only gate text for a POST with the setting on.
   - Read tools: one test per tool in the four `tests/test_tools_*.py` files (T2/T3 will add the write tests to the same
     files: create the files with the read tests only, with a header comment saying so): demo data gives parts,
     categories, stock and locations; assert the compact field set (exact keys), the count/results envelope, the search
     hit, the pagination, `part_detail` present on stock items, and a 404 text for a missing id.
   - Update `tests/test_policy.py`, `tests/test_auth_flow.py`, `tests/test_ui.py` wherever they name old tools
     (`list_stock_items` -> `get_stock`; keep `list_parts`); run them.
8. `docs/plans/tools/T1-report.md`: what was built, the view class names that resolved on 1.5.7 (and whether `StockAdd` /
   `StockRemove` / `StockTransfer` resolve, for T3), the mcp SDK signature facts, test counts, anything the overview got
   wrong (the orchestrator amends the overview; do not change it yourself).

## Verification

`dev/test.sh inventree_mcp.tests.test_tools_framework`, `dev/test.sh inventree_mcp.tests.test_transport`, the four
`test_tools_*` modules, then the whole suite `dev/test.sh` (must be green: 3 skips are expected on 1.5.7). Lint as in
the overview. A live check: `tools/list` on the dev stack (http://localhost:8080, API token via
`/api/user/token/` with admin/admin; see `tests/oauth_helpers.mcp_body` for the JSON-RPC shape, or
`dev/oauth_smoke.py` for a scripted client) returns the 11 tools with titles and annotations.

## Boundaries

Do not implement write tools (T2/T3), do not touch `auth/`, `frontend/`, `dev/*.py`, `deploy/`, README/OPERATIONS (T4).
Do not run git add/commit. Ask nothing; decide, and record decisions in the report.
