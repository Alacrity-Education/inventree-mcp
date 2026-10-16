# T1 review (Opus, 2026-10-16)

**Ready for T2/T3: no.** Apply finding 1 first. It is about 30 lines in `tools/_common.py`, and T2 and T3 run in parallel
without write access to that file. The other findings can be fixed in parallel with T2/T3.

Verified with: `dev/test.sh` on test_tools_framework (32 OK), test_transport (32 OK), the four test_tools_* (3+2+2+3 OK),
test_policy (34 OK), test_auth_flow (2 OK), test_membership (50 OK), test_ui (13 OK); both ruff checks and `format --check` clean; a
dev-shell comparison of every registered tool's description, parameter names/descriptions, `required` and defaults against
the Go structs and `Description` strings of `internal/tools/*.go` (all 10 match exactly); `git show 63f3c6b:inventree_mcp/test_mcp.py`
diffed against `tests/test_transport.py`.

## Major

1. **`tools/_common.py`: shared write helpers are missing, so T2 and T3 would duplicate them.**
   - `update_part` (T2), `update_part_category` (T2) and `update_stock_location` (T3) all need the Go error
     "no fields to update" when the cleaned payload is empty.
   - Get, create, update and set-image all follow `compact(await call_view(loader(), method, path, ...), FIELDS)`.
     That is 3 copies today (`parts.py:55-56`, `stock.py:216-223`, `locations.py:290-293`) and about 7 more in T2/T3.
   - Add to `_common.py` (and import `ToolError` there):
     ```python
     def changes(**kwargs) -> dict:
         """clean(**kwargs); ToolError("no fields to update") when nothing is left (the Go update tools)."""
     async def fetch(view_loader, method, path, *, fields, data=None, query_params=None, **view_kwargs) -> dict:
         """call_view() then compact(); a list response (POST StockList) is reduced to its first item,
         ToolError("no stock items returned") when the list is empty."""
     ```
   - Rewrite `get_part`, `get_stock_item` and `get_stock_location` on `fetch()`.
   - Test both in `HelperTest` (patched `_common.call_view`, as `_listing_query` does).
   - Tell T2 and T3 to use `changes()`, `fetch()`, `text()` and `call_view(..., "DELETE", ..., pk=id)`.
   - Not shared, so out of scope: the `items` model (T3 only) and the `remote_image` error text (T2 only).
2. **Lost coverage: tool_visibility and call_view edge cases that do not depend on the old tools were dropped.**
   - These old tests were not salvaged:
     - `test_mcp.py:1289` `test_no_bound_user_returns_every_tool_unfiltered`;
     - `:1301` `test_unauthenticated_bound_identity_sees_only_tools_needing_no_auth`;
     - `:1321` `test_unavailable_resource_hides_its_tools_without_crashing`;
     - `:1518` `CallViewImportSafetyTest` (`call_view(None, ...)` gives the "unavailable" ToolError);
     - `:586` `test_no_bound_user_fails_closed`.
   - Nothing tests that `visible_tool_names()` uses the *declared* method, which is T1 deliverable 2.
     `CallViewErrorTest.test_user_has_access_uses_the_method` covers only `proxy.user_has_access`.
   - Add a `VisibilityTest(InvenTreeTestCase)` to `tests/test_tools_framework.py` with:
     - (a) no bound identity: `visible_tool_names(all) == all`;
     - (b) `AnonymousUser` bound: the result `== all - set(ACCESS)`;
     - (c) `patch.dict(ACCESS, {"list_parts": (lambda: None, "GET")})`: `list_parts` is hidden and `get_part` is
       still visible for a `part.view` user;
     - (d) a probe tool registered with `tool(...)` plus `access("_probe_post", lambda: StockList, "POST")` (clean up
       both the tool and the ACCESS key): hidden for `stock.view`, visible for `stock.view` + `stock.add`;
     - (e) `call_view(None, "GET", "/api/part/")` raises a ToolError containing "unavailable";
     - (f) `call_view(PartList, "GET", ...)` with no bound user raises `PermissionError`.
   - The OAuth scope-narrowing tests (`test_mcp.py:664,680,1158,1180`) do not need salvaging:
     `test_policy.TransportNarrowingTest` covers them (`test_non_facade_oauth_token_is_cut_to_the_mcp_roles`,
     `test_tool_visibility_reflects_the_narrowed_set`).

## Minor

3. **`proxy.py:153-158, 180-183`: `_viewset_actions` has no POST mapping.**
   - A POST without a pk on a ViewSet raises `KeyError('POST')`. All 11 tool views are plain generics on 1.5.7, beta
     1.6 and latest (report table), so nothing breaks today. But core is converting endpoints (PurchaseOrder already).
   - Change: `if "pk" not in view_kwargs and method in ("GET", "POST"): action = "list" if method == "GET" else "create"`.
4. **`proxy.py:57`: the credential regex is case-insensitive and matches any following word.**
   - Verified: `"name: Basic units are not valid; token expired"` becomes
     `"name: Basic [REDACTED] are not valid; token [REDACTED]"`, so ordinary validation text gets mangled.
   - Change to `re.compile(r"\b(Token|Bearer|Basic) [A-Za-z0-9._~+/=-]{16,}")` (case-sensitive, credential-shaped).
   - Add an `ErrorMessageTest` case asserting that `"Basic units are not valid"` is unchanged.
5. **`mcp_server.py:58`: `serverInfo.version` is empty.**
   - `MCPServer.__init__` accepts `version: str = ""` (checked on mcp 2.3.0).
   - Change: `from . import PLUGIN_VERSION` and `MCPServer(name="InvenTree MCP", instructions=INSTRUCTIONS, version=PLUGIN_VERSION)`.
   - Assert `body["result"]["serverInfo"]["version"] == PLUGIN_VERSION` in `test_transport.py`
     `test_api_token_auth_succeeds`.
6. **`tests/test_tools_framework.py:185-189`: the section 3 field tuples are only spot-checked.**
   - The checks are `len(PART_FIELDS) == 22`, the last stock field, and one member each for the other two.
   - The read tests compare keys against the same constants, so a drift from the contract passes silently.
   - Change: assert the four tuples literally, copied from overview section 3. They currently match.
7. **Dead code and stale references.** None are imports of deleted modules (grep of the package, `dev/`, `deploy/`, `docs/`,
   `.github/`, root). Outside T1's ownership, so the orchestrator fixes these now:
   - delete `inventree_mcp/expand_introspection.py` (no importer left);
   - `tool_logging.py:10` "see test_mcp.py": change to "see tests/test_transport.py";
   - `tool_logging.py:16` "output_schemas.py and tool_visibility.py": change to "tool_visibility.py";
   - `view_resolution.py:6-7`: drop "(and from tools/discovery.py's RESOURCE_LOADERS)" and say "through the tool modules' view
     loaders";
   - `view_resolution.py:14`: change "~50 call sites" to "call sites";
   - `pyproject.toml:62`: delete `omit = ["*/test_mcp.py"]`;
   - **`run_tests.sh:11-12,19`** (not named in the T1 report): the default target `inventree_mcp.test_mcp` no longer exists, so
     `./run_tests.sh` with no argument fails. Change the default to `inventree_mcp` and the examples to
     `inventree_mcp.tests.test_transport.MCPTransportTest`.

   Give these to T4 with exact lines:
   - `AGENTS.md:58-65` (tools/discovery/introspection file map), `:172`, `:187-209`, `:225`;
   - `dev/README.md:12-13,42,49`;
   - `dev/permissions_e2e.py:45,281-285` and `dev/e2e/run.py:655-677` (`list_stock_items`, `list_locations`,
     `get_location`; use `get_stock`, `list_stock_locations`, `get_stock_location`).
8. **`tests/test_transport.py:287-308`: the filter test lost its positive assertion that ungated tools are visible.**
   - That is unavoidable while there are no ungated tools.
   - After T2, the orchestrator (T2 does not own this file) should:
     - add `self.assertIn("search_part_images", names)`;
     - add `search_part_images` to `READ_TOOLS` with `open_world_hint=True` (`test_tools_framework.py:40-51`; today the loop
       asserts `open_world_hint False` for every READ_TOOLS entry, so give it its own expectation).
   - After T3, the orchestrator should add a test that pins the exact 26 names, titles and the four hints of section 4.
     This is acceptance item 1, and no test covers it yet.

## T1 report items for the orchestrator

| item | verdict |
|---|---|
| 10 read tools, not 11 | Acceptable. Amend overview section 6 and T1 deliverable 4: search_part_images is T2's. |
| T1 created `test_tools_{parts,categories,stock,locations}.py` | Acceptable. Amend overview section 6: T2/T3 *append* to them. |
| `_raw_api_token` had 3 importers (incl. test_membership) | Acceptable. The move to `oauth_helpers.py` is correct. |
| `part_detail` is null without part view | Acceptable: this is InvenTree's own field permission, not a bypass. No code change. T4 documents it (README; instructions could say "stock roles plus part view for part names"). |
| `tags` always null | Acceptable, and it matches Go (Go does not request tags either). Verified on 1.5.7: `tags` is an OptionalField (`?tags=true`) on the **detail** views (returns `[]`). On the **list** views `tags` is a filter: `?tags=true` gave count 0 instead of 438, so it must never be sent there. Optional: send `tags=true` on the three detail GETs. Not required. |
| changed `test_paginated_list_uses_the_request_host` | Acceptable. With `count > 1` and `limit 1` the view builds the next link from the request host, and a lost meta raises DisallowedHost, which fails the call. The F17/meta tests (`test_policy.py:374-437`) cover the factory kwargs directly. |
| `serverInfo.version` empty | Needs action: finding 5 (one line). |
| Go get_stock sends no `part_detail` | Acceptable: a deliberate improvement in the overview. |
| SDK prefix "Error executing tool <name>: " | Acceptable. Amend section 5's wording to "the ToolError message starts with". The tests assert both forms. |
| leftovers outside T1 | Need action: finding 7 (orchestrator part now, T4 part listed). |

## Confirmed correct

- **Deletions.** Section 1 complete. No import of a deleted module remains anywhere. `mcp_server.py` imports exactly the five
  modules plus tool_logging and tool_visibility. Import order is safe: `mcp` is defined before `_common` imports it.
- **Tool contract.** Names, titles and R annotations (read true, destructive false, idempotent true, openWorld false) for all
  10 tools. Inputs, types, `required` and defaults (25/50/100, 0 ids, offset 0) match the table and Go. Descriptions and
  parameter descriptions are verbatim Go (checked programmatically). No `outputSchema`; the result is indented JSON text
  like Go's `MarshalIndent`.
- **Envelope and field sets.** `listing()` returns only `{count, results}`, clamps (<=0 gives the default, cap 500), raises a
  negative offset to 0, and sends params through `clean()`. `compact()` keeps order, fills None and reduces `part_detail`
  to (pk, name, full_name). The four field tuples equal section 3. On 1.5.7 only `tags` (part, stock, location) is absent
  from the serializers.
- **`clean()`.** Drops None, `""`, and 0 for category/parent/location/default_location/part/minimum_stock. Keeps False and
  other zeros. This matches Go: minimum_stock 0 is "unset" in both create and update.
- **Error texts.** The 401/403/404/400/other texts are exactly section 5. 400 bodies become `field: message` lines
  (non_field_errors/detail unprefixed, nested `items[1].quantity`). Truncation is at 500. A non-JSON body no longer crashes.
  Credentials cannot reach a message: the 401/403/404 texts are fixed, and other bodies are InvenTree's response to the
  same user, which never echoes request headers (a redaction pass also runs). The 400 shaping only reformats the body
  the same user would get from the REST API, so it exposes no other users' data.
- **Write gates.** The `MCP_READ_ONLY` gate is unchanged in `_call_view_sync` (same text) and is tested for POST in
  `ReadOnlyGateTest` and `CallViewErrorTest`.
- **Tool code.** Tools contain no ORM access, and every tool goes through `call_view` (or `listing`, which calls it).
- **Visibility.** `tool_visibility` reads `ACCESS`, passes the declared method, caches per (view, method), and hides a tool
  whose view does not resolve. The ACCESS completeness test fails for a newly registered tool without `access()`, and
  for a stale entry.
- **Salvaged tests.** MCPTransportTest, ToolLoggingTest, PluginSettingsTest, ServerCardTest, WellKnownCompatTest,
  ViewResolutionTest, ResolveViewAnyTest and the two read-only tests keep their original assertions. Only tool names,
  helpers and docstrings changed. The read-only tests moved from PartList to StockList, and one assertion was added for
  the 400 text.
- **Spec item 7 coverage.** Annotations, ACCESS completeness, listing clamp/default, compact, clean, wire coercion
  (`"1"`, `"5"`, `"true"`), the 403/404/400 texts and the gate text are all covered. The read tests assert exact key
  sets, the envelope, search hits, non-overlapping pages, `part_detail` and the 404 text.
- **Settings and views.** `GOOGLE_API_KEY` (protected) and `GOOGLE_CSE_ID` default ""; MCP_READ_ONLY text updated; version
  0.5.0; `server_card.py` tools-free and tested; StockAdd/StockRemove/StockTransfer resolve (`ViewResolutionTest`).
- **T2/T3 test base.** `MCPToolTestCase` grants every MCP role; `set_setting("MCP_READ_ONLY", False)` enables writes.
