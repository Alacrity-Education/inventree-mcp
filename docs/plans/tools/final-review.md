# Final review of the tools rewrite (Opus, 2026-10-16)

**Ship 0.5.0 to the beta: yes, after finding 1.** Finding 1 is packaging only: the beta build would otherwise install
0.4.2. Findings 2 to 9 are small and can ship in 0.5.0 or in 0.5.1. None of them is a security hole.

Reviewed: `git diff 63f3c6b 93f815d`, plus the uncommitted working-tree changes present during the review
(`proxy.py`, which renders index-keyed list errors from InvenTree master; one test each in `test_tools_framework.py` and
`test_tools_stock.py`; overview 6d). Those changes are correct and must be committed before the release is tagged.

How it was verified:
- `dev/test.sh` (1.5.7, working tree): Ran 396, OK (skipped=3).
- `python3 dev/tools_e2e.py`: OK, 18 steps.
- All four ruff commands: clean.
- A dev-shell dump of `mcp.list_tools()`, compared programmatically with the Go `Name`/`Description`/`jsonschema` strings.
- `required_alternate_scopes` printed for all 11 views through `scoped_view_class()`.
- A direct REST probe of StockAdd with a view-only token, inside a transaction that was rolled back.

## Findings
1. **Major (release): the beta would still deploy 0.4.2.**
   - `deploy/beta/image/inventree_mcp-0.4.2-py3-none-any.whl` is present. It is git-ignored and, per
     `image/Dockerfile:2-3,9`, *wins over* `PLUGIN_WHEEL`.
   - `deploy/beta/compose.yaml:12` and `deploy/beta/image/Dockerfile:6` default to the 0.4.2 URL.
   - `README.md:41` still installs 0.4.2, and `deploy/beta/README.md:4` says "0.4.0 baked in".
   - Change: after the 0.5.0 release is published, delete the local 0.4.2 wheel (or replace it with the 0.5.0 build).
     Set all three URLs to `.../releases/download/0.5.0/inventree_mcp-0.5.0-py3-none-any.whl`, and change
     `deploy/beta/README.md:4` to `0.5.0`.
   - Then run `deploy/beta/up.sh`. `configure_inventree.py` and `MCP_WRITE` have not been run yet (T4 report).
2. **Minor (wrong guidance to the model): `mcp_server.py:49-52`.**
   - The text says "InvenTree downloads it server-side when its "download from URL" setting is enabled". That setting
     was removed at API 489, and the plugin does the download.
   - Replace with: "...or set_part_image. The plugin downloads the image only when an administrator has enabled the
     plugin setting IMAGE_DOWNLOAD_ENABLED (off by default)."
   - The parameter texts "InvenTree downloads it server-side" (`tools/parts.py:141-142,215-216,306`) are Go verbatim,
     so they stay. Record them under finding 3.
3. **Minor (contract record): deviations not recorded in 6a to 6c.** Add to overview 6c, and to `AGENTS.md:28-29`, which
   says "verbatim; do not reword":
   - (a) The descriptions of `create_part`, `update_part` and `set_part_image` add one sentence naming
     `IMAGE_DOWNLOAD_ENABLED` (`parts.py:155-156,222-223,311`). These three plus `search_part_images` are the only
     description differences from Go; all parameter texts match.
   - (b) T23-review minor 10 was never recorded:
     - `clean()` drops `part=0` and `location=0` (Go sends 0);
     - a negative `minimum_stock` is sent on create;
     - `delete_part` needs part *view* (it GETs first), and it needs stock *view* when `stock_item_count` is absent.
4. **Minor (gate order): the read-only answer is not first in three cases.** Section 7 says every write tool answers the
   read-only text while `MCP_READ_ONLY` is on. With it on:
   - `delete_part` on a missing part answers 404;
   - `delete_part` on a part with stock answers "has K stock item(s)" (`parts.py:262-273`);
   - `update_part`, `update_part_category` and `update_stock_location` without fields answer "no fields to update"
     (`changes()`).

   Nothing is written in these cases, so this is not a bypass. Change: make
   `await sync_to_async(ensure_writable)("DELETE")` the first line of `delete_part`. In the three update tools, call
   `ensure_writable("PATCH")` before `changes()`. Or record the order in 6c.
5. **Minor (secret hygiene): the Google key can reach the logs at `INVENTREE_LOG_LEVEL=DEBUG`.**
   - InvenTree calls `logging.basicConfig(level=LOG_LEVEL)`, and urllib3 logs the request line at DEBUG, which includes
     `?key=...` (`tools/images.py:56-64`). Tool errors are scrubbed correctly.
   - Change: send the key as the header `X-goog-api-key` (Google's API-key header) and drop `"key"` from `params`. Keep
     `_scrub()`. Check against the live API once. Add a test that asserts `"key" not in params`.
6. **Leftover: `codecov.yml:2` ignores `inventree_mcp/test_mcp.py`, which no longer exists.**
   - Change: delete the `ignore:` block, or replace it with `- "inventree_mcp/tests/**"` to match the old intent (test
     code excluded).
   - The leftovers named in the T1 review are gone: `expand_introspection.py` is deleted, the `pyproject.toml` omit is
     removed, and the `run_tests.sh` default is `inventree_mcp`.
7. **Duplicate test.**
   - `tests/test_tools_framework.py:506` (`test_400_for_an_invalid_payload`) repeats
     `tests/test_transport.py:493` (`test_read_only_setting_can_be_disabled`): the same inline `_disable_read_only`
     and the same StockList POST 400 assertion.
   - Change: delete the framework copy and move its single extra assertion
     (`"part: Valid part must be supplied"`) into the transport test.
8. **Stale docstring: `tools/__init__.py:4-5`.** The helper list omits `fetch()`, `changes()` and `require_access()`.
   Change: list them, or point to `_common.py`'s module docstring.
9. **E2e gap: `dev/tools_e2e.py` never calls `update_part`, `update_part_category`, `update_stock_location` or
   `set_part_image`, and has no OAuth variant (overview section 7 asks for both token kinds).**
   - Unit tests cover all four tools and the OAuth path (`test_auth_flow`, `test_policy`), so this does not block.
   - Change: add one update call per resource after step 6. Add `set_part_image` only when `IMAGE_DOWNLOAD_ENABLED`
     is on.

Outside this diff (pre-existing core behaviour, for a follow-up):
- An `AccessToken` scoped `g:read r:view:stock` that belongs to a **superuser** gets 201 from core's own REST
  `POST /api/stock/` and `/api/stock/add/`. This was probed on dev 1.5.7 and rolled back. RolePermission lets
  superusers through, and core does not enforce the scope.
- The same token for a non-superuser holding interactive stock.add gets 403 on both.
- The MCP tools are not affected. Decide whether tokens issued by the MCP façade should be refused outside `/mcp`.

## Confirmed correct

- **Write path.**
  - Every InvenTree read and write goes through `call_view`/`fetch`/`listing`. There is no ORM in `tools/`.
  - The methods are right: POST list (creates and `add_stock`), PATCH detail (updates and the image), DELETE detail
    (deletes, with the false/false body on categories and locations), POST StockAdd/StockRemove/StockTransfer.
  - `delete_part` does GET, then the stock check, then PATCH inactive, then DELETE. It reactivates the part on failure.
- **Read-only gate.** `ensure_writable` runs inside `_call_view_sync` on every non-GET call.
  - The image path checks the gate before the setting, the permission and any download (`ensure_image_allowed`).
  - The adjustments check it inside `require_access` before their POST.
  - Nothing outside `call_view` writes. The only other outbound I/O is the Google GET, which is read-only.
- **Adjustment pre-check.**
  - StockAdd/StockRemove/StockTransfer resolve to `[['g:read']]` for every method, and every MCP token carries `g:read`
    (`policy._granted_scopes`).
  - `_adjust()` first calls `require_access(StockList, "POST")`. `user_has_access` runs `initial()` on
    `scoped_view_class(StockList)` with the bound (narrowed) token. That requires `r:add:stock` or `r:add:build` (the
    deliberate any-role split, the same rule as `add_stock`) plus the interactive role.
  - API-token and Basic requests also carry a scoped token (`mcp_transport.scoped_token_for`).
  - **A token holding only `r:view:stock` cannot reach StockAdd through any plugin path.** The three tools are the only
    callers, and the pre-check answers 403 before the POST. This is tested with the filter off and on, and the quantity
    is unchanged.
  - ACCESS for the three tools is StockList POST, so visibility matches.
- **Image download.**
  - `IMAGE_DOWNLOAD_ENABLED` defaults to False (tested).
  - The order is: read-only gate, then the setting, then PartDetail PATCH permission, then a GET of the part (404), and
    only then the download (tests assert the mock is not called).
  - `download_image_from_url` is used unchanged, with timeout 10 s and a 1 MB cap. Its URL validator, SSRF check,
    `ssrf_safe_context` and per-hop redirect checks are untouched. It does no DB work, so a non-thread-sensitive thread
    is safe.
  - The upload is `files={"image": <re-encoded PNG/JPEG>}` with no other data. The field name, filename and content are
    plugin-controlled, so no other field can be targeted.
  - Errors scrub the URL, its query, fragment and userinfo, and are cut at 300 characters. The Google key is scrubbed
    raw and URL-encoded, with `from None`.
- **Contract.**
  - `tools/list` is exactly the 26 names. Titles and the four hints match section 4.
  - Inputs, `required` and defaults match Go and the table, and all parameter texts are verbatim.
  - The 6a to 6c items are implemented as recorded: `serial`→`serial_numbers`, the delete bodies, the clamp, the add
    role, the image design, `delete_part` refusing parts with stock, and the shared scaffolding in `test_transport.py`.
- **Earlier reviews.**
  - T1-review findings 1 to 8 are fixed: `fetch`/`changes`, VisibilityTest a to f, POST→create, the case-sensitive
    credential regex plus its test, `serverInfo.version`, literal field tuples, the leftovers, and the 26-name pin.
    `search_part_images` appears in the framework table and is tested visible with the filter on (in
    `test_tools_images`).
  - T23-review findings 1 to 9 are fixed: the real image upload, `delete_part` reactivation and the stock refusal,
    scaffolding moved, both denial directions per tool, the role table, the JSON guard, `_items()`, and test names.
    Finding 10 was not recorded (finding 3b above).
- **Tests.** Every tool has a happy path, 400/404 texts, the read-only gate, both role denials (MCP and InvenTree, filter
  off and on), the 26-name pin, the image path with the download mocked (plus the real helper refusing 127.0.0.1), and
  the adjustment pre-check.
- **Docs.**
  - The README tool tables match the code for all 26 tools (inputs, defaults, required).
  - The README settings list and the OPERATIONS rows cover all 27 `core.py` settings. "15 write tools" is correct.
  - The beta `MCP_WRITE=1` updates only the `part`, `part_category`, `stock` and `stock_location` rows to all four flags
    and sets `MCP_READ_ONLY` False. Every other role keeps its existing view flag. `up.sh` forwards `MCP_WRITE`.
- **Release.** `PLUGIN_VERSION = "0.5.0"`. The release workflow builds the wheel from it.

## Release notes draft (0.5.0)

```
0.5.0: new tool set, with write tools
- Breaking: the 56 read-only list/get tools, describe_filters and make_web_link are removed. They are replaced by the
  26 tools of syntaxerr66/inventree-mcp (parts, part categories, stock items, stock locations), with the same names,
  inputs and descriptions and compact JSON results.
- New write tools: create/update/delete for parts, categories and locations; add_stock; stock_add_quantity,
  stock_remove_quantity, stock_transfer; delete_stock_item; set_part_image.
- Writes need MCP_READ_ONLY off (still on by default) plus the matching MCP role and interactive role (add, change,
  delete; the stock adjustments need stock add).
- Deletes are safe by design: deleting a category or location moves its contents to the parent, and delete_part
  refuses parts with stock.
- search_part_images (Google Custom Search): new settings GOOGLE_API_KEY (protected) and GOOGLE_CSE_ID.
- image_url / set_part_image: opt-in setting IMAGE_DOWNLOAD_ENABLED (off by default). Uses InvenTree's URL/SSRF/size
  checks.
- Errors read "permission denied (403)", "not found (404)" or "validation failed (400): field: message".
- Every tool carries a title and readOnly/destructive/idempotent/openWorld hints, and serverInfo reports the version.
```
