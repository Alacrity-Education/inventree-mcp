# T1 report: framework, deletions, read tools, salvaged tests

Date: 2026-10-16. Base: `main` at 37e6ac5 (+ the plan files). Nothing staged or committed.

## What was built

- **Deleted** (section 1): `tools/{attachments,bom,build_orders,categories,companies,discovery,locations,parameters,parts,project_codes,purchase_orders,return_orders,sales_orders,stock,stock_history,supplier_parts}.py`
  (the old categories/locations/parts/stock modules were then rewritten), `output_schemas.py`, `schema_introspection.py`,
  `filter_introspection.py`, `test_mcp.py`.
- **`tools/_common.py`**: the section 3 interface. `DEFAULTS`, `MAX_LIMIT = 500`, `tool()`, `ACCESS` / `access()`,
  `listing()`, `compact()`, `clean()`, `text()`, the four field tuples, plus `clamp_limit()` (public helper, used by
  `listing()`).
  - `tool()` calls `mcp.tool(name, title, description, annotations, structured_output=False)`. It also sets
    `ToolAnnotations.title`, for older clients. It always sets all four hints. Unset hints default to: read tools
    destructive=False, idempotent=True; write tools destructive=True, idempotent=False (the MCP defaults). T2/T3 pass
    explicit values per the table anyway.
  - The description is the function docstring with its whitespace collapsed to single spaces. The docstrings are the Go
    descriptions verbatim, wrapped for the line length.
  - `structured_output=False`: no `outputSchema`, and a dict result is sent as indented JSON text
    (`pydantic_core.to_json(indent=2)`), like the Go server's `MarshalIndent`. `text()` results are plain text.
  - `listing()` sends `params` through `clean()`, clamps the limit (<= 0 becomes the default, the cap is 500), raises a
    negative offset to 0, and returns only `{"count", "results"}`.
  - `clean()` drops None, `""` and 0 for `category`, `parent`, `location`, `default_location`, `part`, `minimum_stock`.
    It keeps False, and it keeps 0 for any other key (e.g. `quantity`, `offset`). `part` is in the set so that
    `get_stock(part=0)` means "all".
  - `compact()` keeps the given fields in order, fills missing ones with None and reduces `part_detail` to
    (pk, name, full_name).
- **`proxy.py`**:
  - `error_message(status, body)` produces the Go-style texts of section 5:
    - a 400 body becomes `field: message` lines, with lists joined by `"; "`, nested fields written as
      `items[1].quantity`, and `non_field_errors` / `detail` / `__all__` lines without a prefix;
    - details are cut at 500 chars + `...`;
    - `Token|Bearer|Basic <value>` is redacted;
    - an empty body gives `API error N (empty response)`.
  - Non-JSON error bodies (HTML 500s) are now handled; before, they raised `json.JSONDecodeError`.
  - `call_view`'s signature, the MCP_READ_ONLY gate (same text) and `user_has_access` are unchanged.
  - The module docstring documents the texts.
- **`tool_visibility.py`** reads `ACCESS` and checks `user_has_access(view, method)` with the declared method. Results
  are cached per (view class, method) within one tools/list. A tool without an entry is always listed. The
  MCP_FILTER_TOOL_LIST behaviour is unchanged.
- **Read tools** (10, see "Spec errors" 1):
  - `parts.py`: search_parts, get_part, list_parts.
  - `categories.py`: search_part_categories, list_part_categories.
  - `stock.py`: get_stock, get_stock_item. Both send `part_detail=true`.
  - `locations.py`: search_stock_locations, get_stock_location, list_stock_locations.

  Names, titles, R annotations, Go inputs, defaults and parameter descriptions are as in section 4 (`Annotated[...,
  Field(description=<Go jsonschema text>)]`). Each module ends with its `access(...)` lines.
- **Other modules**:
  - `tools/images.py` is a docstring-only module.
  - `tools/__init__.py` has a new docstring.
  - `mcp_server.py` has `INSTRUCTIONS`: the CLAUDE.md workflow guidance plus "writes need Read Only off + MCP write
    roles". It imports the five modules and `tool_visibility` / `tool_logging`. The text already names the T2/T3 tools.
- **`core.py`**:
  - New settings `GOOGLE_API_KEY` (`"protected": True`, default "") and `GOOGLE_CSE_ID` (default "").
  - The MCP_READ_ONLY description now lists the write tools it blocks.
  - `PLUGIN_VERSION = "0.5.0"`.
- **`server_card.py`**: reviewed. It mentions no tools (the description is "MCP server for InvenTree") and its docstring
  is still accurate, so it is unchanged. A test now asserts the card has no `tools` key.

## Tests

- **`tests/test_transport.py`** (32 tests):
  - Salvaged: MCPTransportTest (incl. the two tools/list filter tests), ToolLoggingTest, PluginSettingsTest,
    ServerCardTest, WellKnownCompatTest, ViewResolutionTest, ResolveViewAnyTest.
  - The MCP_READ_ONLY tests are now `ReadOnlyGateTest`: `call_view(StockList, "POST")`.
  - New tests: the Google settings, and every view of section 3 resolving.
  - The transport helper used by the other tool tests lives here: `MCPClientMixin` (`mcp_post`, `list_tool_names`,
    `list_tools`, `call_tool`, `tool_json`, `tool_error`) and `MCPToolTestCase` (plugin active, group with every MCP
    role, API token, `set_setting()` with cleanup).
  - Adaptations:
    - ToolLogging used `list_project_codes` / `get_project_code`; it now uses `list_parts` / `get_part` with the
      `part.view` role.
    - The filter test asserts that no ACCESS-gated tool is visible to a zero-role user.
- **`tests/test_tools_framework.py`** (32 tests):
  - `tool()` sets the title and the four hints, and publishes no output schema;
  - the read tools are registered with their titles and R hints, and with the Go limit defaults in the input schema;
  - ACCESS completeness: every registered tool except search_part_images has an entry, and the failure message names
    the missing tools;
  - `listing()` clamps and defaults the limit and cleans its params; `compact()`; `clean()`; `text()`;
  - `error_message()` unit tests (status texts, field lines, list bodies, truncation, redaction);
  - `call_view` 403 (user without the role), 404 (missing pk), 400 (POST StockList with MCP_READ_ONLY off), and the
    read-only gate text;
  - `user_has_access` honours the method;
  - coercion over the wire through a temporary probe tool: `{"id": "1", "limit": "5", "active": "true"}` reaches it as
    `1, 5, True`; `get_part {"id": "1"}` and `list_parts {"limit": "5"}` work; an invalid `id` gives a tool error;
  - 403 and 404 texts over the wire; results are indented JSON text.
- **`test_tools_parts.py` (3), `test_tools_categories.py` (2), `test_tools_stock.py` (2), `test_tools_locations.py` (3)**:
  - one test per tool, with a header comment saying T2/T3 add the write tests;
  - they assert the exact compact key set, the `{count, results}` envelope, the search hit, the pagination (limit, and
    offsets that do not overlap), `part_detail` on stock items, and the 404 text for a missing id.
  - Parts and stock use the core fixtures. Categories and locations create their trees through the ORM, because the
    fixtures leave `pathstring` empty.
- **Updated**:
  - `test_policy.py` and `test_auth_flow.py`: `list_stock_items` -> `get_stock`, `list_locations` ->
    `list_stock_locations`.
  - `_raw_api_token` + `API_TOKEN_V2_MIN_API_VERSION` moved to `tests/oauth_helpers.py`. Three modules import it from
    there: `test_policy.py`, `test_auth_flow.py` **and `test_membership.py`**, which the spec did not name.
  - `test_ui.py` and `test_membership.py`: comment references only.
  - `test_policy.test_paginated_list_uses_the_request_host` used to assert `body["next"]`, which list_parts no longer
    returns. It now asserts a successful 1-item page with `count > 1` and no `next` key. That still proves the request
    host is carried: the view builds its next link from the host, and with "testserver" outside ALLOWED_HOSTS InvenTree
    answers 500 DisallowedHost. I saw that answer in a dev shell call without request meta.

### Commands and results (dev stack, InvenTree 1.5.7, API 530)

| command | result |
|---|---|
| `dev/test.sh inventree_mcp.tests.test_transport` | Ran 32, OK |
| `dev/test.sh inventree_mcp.tests.test_tools_framework` | Ran 32, OK |
| `dev/test.sh inventree_mcp.tests.test_tools_parts` | Ran 3, OK |
| `dev/test.sh inventree_mcp.tests.test_tools_categories` | Ran 2, OK |
| `dev/test.sh inventree_mcp.tests.test_tools_stock` | Ran 2, OK |
| `dev/test.sh inventree_mcp.tests.test_tools_locations` | Ran 3, OK |
| `dev/test.sh` (whole package) | **Ran 294 tests in 609 s, OK (skipped=3)** |
| `uvx ruff@0.16.0 check --preview inventree_mcp` | All checks passed |
| `uvx ruff@0.16.0 format --preview inventree_mcp` | clean (`--check`: 38 files already formatted) |
| `uvx ruff@0.16.0 check --preview --isolated inventree_mcp/tests` | All checks passed (after `--fix` sorted the `_raw_api_token` imports) |

I also formatted the new test files with `ruff format --preview --isolated`.

**Live check**:
1. I restarted the server with `dev/compose.sh restart inventree-server inventree-worker`.
2. A script got an API token from `/api/user/token/` (admin/admin), then sent initialize, tools/list and tools/call to
   `http://localhost:8080/plugin/inventree-mcp/mcp/`.
3. tools/list returned the 10 read tools with their titles, `readOnlyHint=true destructiveHint=false
   idempotentHint=true openWorldHint=false`, and no `outputSchema`.
4. `search_parts {"search": "resistor", "limit": "2"}` returned compact JSON (count 48).
5. `get_stock {"limit": 1}` carried `part_detail` {pk, name, full_name}.
6. `get_part {"id": 999999}` returned `isError` with "Error executing tool get_part: not found (404): resource does
   not exist".

## View classes (all resolve; all are plain DRF generic views, not ViewSets)

| | 1.5.7 (dev, API 530) | beta image `inventree-mcp-beta:local` (1.6.0 dev, API 558) | `inventree/inventree:latest` (1.6.0 dev, API 557) |
|---|---|---|---|
| part.api PartList, PartDetail, CategoryList, CategoryDetail | yes | yes | yes |
| stock.api StockList, StockDetail, StockLocationList, StockLocationDetail | yes | yes | yes |
| stock.api **StockAdd, StockRemove, StockTransfer** (T3) | **yes** (subclasses of `StockAdjustView`) | **yes** | **yes** |

On 1.5.7 I checked by importing the classes in `dev/shell.sh`; on the two other images I grepped `class X(` in
`part/api.py` and `stock/api.py`. `ViewResolutionTest.test_resolves_every_view_the_tools_use` guards this.

## mcp SDK facts (dev stack: mcp 2.3.0)

- The signature is `MCPServer.tool(self, name=None, title=None, description=None, annotations: ToolAnnotations | None =
  None, icons=None, meta=None, structured_output: bool | None = None)`. `add_tool(fn, ...)` takes the same keywords.
- `mcp.types.ToolAnnotations` fields are `title, read_only_hint, destructive_hint, idempotent_hint, open_world_hint`
  (snake_case in Python, camelCase `readOnlyHint` etc. on the wire).
- Argument validation is pydantic lax mode after `pre_parse_json`, so `"1"`/`"5"` become ints and `"true"` becomes
  True. No coercion middleware is needed (tested).
- A `ToolError` raised by a tool reaches the client as `CallToolResult(isError=True)`. Its text is prefixed by the
  SDK: `"Error executing tool <name>: <our text>"`. Validation errors also become ToolErrors with that prefix.
- `structured_output=False` publishes no output schema, and the content is `pydantic_core.to_json(result, indent=2)`.
  A `str` result is sent as-is.
- `mcp.remove_tool(name)` exists. The tests use it to add and remove probe tools.

## Decisions (spec silent)

1. Unstructured results (no `outputSchema`, JSON text), to match "no output schema is published" and the Go output.
2. All four annotation hints are always explicit, and the title is also set in `ToolAnnotations.title`. The defaults
   for write tools are covered in "What was built".
3. Descriptions come from the docstrings with whitespace collapsed, because `tool()`'s interface has no description
   argument.
4. `clean()`'s zero-is-unset set also includes `part`, for `get_stock`'s "0 or omit to list all".
5. `listing()` also accepts a bare-list response, in case a view is unpaginated.
6. 400 lines are joined with `\n` after the `validation failed (400): ` prefix; a field's list messages are joined with
   `; `. Redaction of `Token/Bearer/Basic <value>` applies to all error details.
7. The well-known/compat tests keep their original early `return` on old cores instead of `skipTest`, so the skip
   count stays at the expected 3.

## Spec errors / gaps found (for the orchestrator to amend)

1. **"The 11 read tools"**: deliverable 4 lists 10 tools. The 11th R tool in section 4's table is `search_part_images`,
   which is T2's. T1 registers and lists 10 tools.
2. **Section 6 vs T1 spec**: section 6 gives `test_tools_parts/categories.py` to T2 and `test_tools_stock/locations.py`
   to T3, but T1 deliverable 7 tells T1 to create them with the read tests. I followed T1; T2/T3 append to them.
3. **`_raw_api_token` had three importers**, not two: `test_membership.py` also imported it from `test_mcp`. I updated
   all three.
4. **`part_detail` depends on part view permission**: InvenTree 1.5.7's `check_field_permission` drops `part_detail`
   (None) unless the user may view parts. A stock-only MCP role therefore gets `part_detail: null`. The stock tests use
   the roles `stock.view` + `part.view`.
5. **`tags`** is not in 1.5.7's part, stock or location list/detail output, so it is always `null` (and categories have
   no `tags` field at all). This matches "missing keys become None" and the Go structs, but the field carries no data
   on 1.5.7.
6. **Go `get_stock` / `get_stock_item` do not send `part_detail=true`.** The overview's addition is an improvement over
   Go, not a port.
7. **Error text prefix**: on the wire, every ToolError text starts with the SDK's `Error executing tool <name>: `, so
   "starts like the Go server's" holds for the ToolError message, not for the wire text.
8. **Leftovers outside T1's ownership**:
   - `inventree_mcp/expand_introspection.py` is now dead code: discovery.py was its only user, and section 1 does not
     list it. Delete it.
   - The docstrings of `tool_logging.py` (mentions `test_mcp.py`, `output_schemas.py`) and `view_resolution.py`
     (mentions `tools/discovery.py`'s `RESOURCE_LOADERS`) are stale; both files are "untouched" per section 1.
   - `pyproject.toml` still has `omit = ["*/test_mcp.py"]` in its coverage config.
   - T4 territory: `dev/permissions_e2e.py`, `dev/e2e/run.py` (use `list_stock_items`, `list_locations`,
     `get_location`), `dev/README.md` (references `inventree_mcp.test_mcp`), and AGENTS.md (file map: `tools/`,
     `discovery.py`, `schema_introspection.py`, `filter_introspection.py`, `test_mcp.py`; Testing section).
9. The MCP `initialize` result reports `serverInfo.version: ""`: `MCPServer` is created without a version. This is out
   of T1's scope; `MCPServer(..., version=PLUGIN_VERSION)` could be considered if the SDK accepts it.

## Review fixes (T1-review.md)

| finding | change |
|---|---|
| Major 1: shared write helpers | `tools/_common.py`: `changes(**kwargs)` returns `clean(**kwargs)` and raises `ToolError("no fields to update")` when nothing is left. `fetch(view_loader, method, path, *, fields, data=None, query_params=None, **view_kwargs)` calls `call_view()` and then `compact()`; a list response is reduced to its first item, and an empty list raises `ToolError("no stock items returned")`. `get_part`, `get_stock_item` and `get_stock_location` now use `fetch()` (the tool modules no longer import `call_view`/`compact`). Tests in `HelperTest`: `changes` (cleaned payload, two empty cases), `fetch` (arguments passed through + compacted; list -> first item; empty list -> ToolError), with `_common.call_view` patched. For T2/T3: use `changes()`, `fetch()`, `text()` and `call_view(loader(), "DELETE", path, pk=id)`. |
| Major 2: lost coverage | `VisibilityTest` in `test_tools_framework.py`, cases a-f: (a) no bound identity -> every name; (b) `AnonymousUser` -> `all - set(ACCESS)`; (c) `patch.dict(ACCESS, {"list_parts": (lambda: None, "GET")})` hides list_parts and keeps get_part; (d) probe tool `_probe_post` with `access(..., StockList, "POST")` is hidden for stock.view and visible for a user with stock.view + stock.add, so visibility uses the declared method (tool and ACCESS key cleaned up); (e) `call_view(None, ...)` -> ToolError containing "unavailable"; (f) `call_view` with no bound user -> `PermissionError`. The writer's roles are set with core's `assignRole()`, because a queryset `update()` on RuleSet skips the permission sync. |
| Minor 3: ViewSet POST | `proxy._viewset_actions`: without a pk, GET -> `list` and POST -> `create`. `ViewSetActionsTest` covers every mapping on a fake `GenericViewSet`, and checks that plain views get None. |
| Minor 4: redaction | The pattern is now `\b(Token|Bearer|Basic) [A-Za-z0-9._~+/=-]{16,}` (case-sensitive, credential-shaped), and the module docstring is updated. New test `test_ordinary_words_are_not_redacted` uses the review's example "Basic units are not valid; token expired" unchanged. The redaction test now uses 32-character secrets. |
| Minor 5: server version | `MCPServer(name=..., instructions=INSTRUCTIONS, version=PLUGIN_VERSION)`. `test_api_token_auth_succeeds` asserts `serverInfo.version == PLUGIN_VERSION`. |
| Minor 6: contract field tuples | `test_field_sets_are_the_contract` asserts the four tuples literally (copied from overview section 3), plus `DEFAULTS` and `MAX_LIMIT`. |
| Minor 7: leftovers | Deleted `inventree_mcp/expand_introspection.py`; the only remaining mention is in AGENTS.md (T4). Docstrings fixed: `tool_logging.py` (tests/test_transport.py; "approach tool_visibility.py already uses") and `view_resolution.py` (view loaders via the tool modules and ACCESS; "call sites"). Removed `omit = ["*/test_mcp.py"]` from `pyproject.toml`. `run_tests.sh` now defaults to `inventree_mcp`, with examples on `inventree_mcp.tests.test_transport`. Not done (T4's, as the review lists): AGENTS.md, dev/README.md, dev/permissions_e2e.py, dev/e2e/run.py. |
| Minor 8 | For the orchestrator after T2/T3 (search_part_images visible assertion, exact 26-names test): not done, as instructed. |

### Reruns after the fixes

| command | result |
|---|---|
| `dev/test.sh inventree_mcp.tests.test_tools_framework` | Ran 44, OK |
| `dev/test.sh inventree_mcp.tests.test_transport` | Ran 32, OK |
| `dev/test.sh inventree_mcp.tests.test_tools_parts` / `_categories` / `_stock` / `_locations` | 3 / 2 / 2 / 3, OK |
| `dev/test.sh inventree_mcp.tests.test_policy` | Ran 34, OK |
| `uvx ruff@0.16.0 check --preview inventree_mcp`, `format --preview` (`--check` clean), `check --preview --isolated inventree_mcp/tests` | All checks passed |
| `dev/test.sh` (whole package) | **Ran 306 tests in 626 s, OK (skipped=3)** (was 294: +12 for the new framework tests) |
