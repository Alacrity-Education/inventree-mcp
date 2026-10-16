# T2 report: part and category writes, search_part_images

Date: 2026-10-16. Nothing staged or committed.

## What was built

- `tools/parts.py`: `create_part`, `update_part`, `delete_part`, `set_part_image` (Go descriptions verbatim as docstrings,
  inputs and defaults per the table, annotations through `tool()`, payloads through `clean()` / `changes()`, results
  through `fetch()` / `text()`). `image_url` is sent as `remote_image`. ACCESS: create POST PartList, update and
  set_part_image PATCH PartDetail, delete DELETE PartDetail. `delete_part` reads the part, PATCHes `{active: false}` only
  if it is active, then DELETEs; all through `call_view`.
- `tools/categories.py`: `create_part_category`, `update_part_category`, `delete_part_category` (ACCESS POST/PATCH/DELETE).
- `tools/images.py`: `search_part_images` (requests, 10 s timeout, `searchType=image`, `num` clamped 1..10, 0 or less ->
  5; credentials via `plugin_setting`; no ACCESS entry; readOnly + openWorld). The request runs in `asyncio.to_thread`.
  Exception texts are scrubbed of the key (raw and URL-encoded); the HTTP-status error carries no URL.
- Tests: `test_tools_parts.py` 22 (was 3), `test_tools_categories.py` 13 (was 2), `test_tools_images.py` 11 (new).
  `WriteToolTestCase` (in `test_tools_parts.py`, also imported by the two other modules) sets MCP_READ_ONLY off and offers
  `make_token`, `error_as`, `tool_text` and `assert_denied_without_mcp_role` (403 text, filter off and on, tools/list).

## Commands (dev stack, InvenTree 1.5.7, API 530)

`dev/test.sh inventree_mcp.tests.test_tools_parts` Ran 22 OK; `..._categories` Ran 13 OK; `..._images` Ran 11 OK.
`uvx ruff@0.16.0 check --preview inventree_mcp`, `format --preview` (--check) and `check --preview --isolated
inventree_mcp/tests`: clean.

## Findings that need the orchestrator (important)

1. **`remote_image` no longer exists in InvenTree's Part API** (API v489, 2026-05-18: "Removes the remote_image field from
   the Part API endpoint"; also Company). On 1.5.7 (API 530) and the 1.6 images, a PATCH/POST with `remote_image` is
   silently ignored: HTTP 200, `image` stays null. So `image_url` on create_part/update_part and the whole of
   `set_part_image` are no-ops on current InvenTree, and the overview's "error text must say INVENTREE_DOWNLOAD_FROM_URL"
   cannot happen (the field is not validated at all). I kept the contract (send `remote_image`) and pinned the observed
   behaviour in `test_set_part_image_with_a_real_patch` (error naming the setting below API 489, no-op from 489). Options for
   you: (a) accept it and say so in the tool descriptions/README, (b) download the image in the plugin and set it, which needs
   multipart PATCH support in `call_view` (it sends JSON only) plus SSRF protection for the URL. I did not change anything.
2. **Category DELETE needs a body on InvenTree >= API 491**: the view validates `delete_child_categories` and `delete_parts`
   (both required booleans) before it even looks up the pk, so a bare DELETE answered "validation failed (400):
   delete_child_categories: This field is required." `delete_part_category` therefore sends both as false (children and
   parts move to the parent; same as older versions). **T3's `delete_stock_location` almost certainly has the same
   requirement** (API 491 added a delete serializer for locations too): check its DELETE body.
3. `delete_part` for a user holding MCP/InvenTree change but not delete deactivates the part, then answers 403 on the
   DELETE (the Go order, as the spec says). Tool visibility hides it from such a user when the filter is on. Pinned in the
   permission test comment.
4. InvenTree 1.5.7 deleted a part that had a stock item without complaint (I tried it); the Go description says "must have
   no stock items". The description is verbatim; no tool-side check added.

## Decisions

- `search_part_images`: no-hit text path and key handling as above. Errors read "image search failed: image search request
  failed: <ExceptionType>: <scrubbed text>" / "... returned status N" / "... decoding image search response failed",
  mirroring the Go wrapping. Unconfigured check comes before the empty-query check, as in Go.
- A 0 `minimum_stock` and 0 ids are dropped by `clean()` (Go: `> 0` on create, `!= 0` on update; identical except negative
  values, which are sent on create too).
- Tests patch `inventree_mcp.tools._common.call_view` for payload assertions on `fetch()` tools and
  `inventree_mcp.tools.parts.call_view` (wrapped) for delete_part's call sequence.
- The partner of the overview's "26 tools exactly" and "search_part_images visible" assertions (T1 review minor 8) is left to
  you; `test_visible_to_a_user_without_part_roles_when_the_filter_is_on` already covers the visibility part.

## Requests for the orchestrator

- Decide finding 1. If you want an upload path, `call_view` needs a multipart variant (proxy.py).
- Tell T3 about finding 2 (and apply the same to anything else that DELETEs a tree node).

## Fix round (after T23-review.md; 2026-10-16)

Base: main at 27a6611 (plus the orchestrator's uncommitted `00-overview.md` amendment, untouched). Nothing staged or
committed. Work list: T23-review.md majors 1-4 and minors 6-9, T1-review minor 8, overview 6c.

### What changed

- **Image upload (major 1, overview 6c).** New plugin setting `IMAGE_DOWNLOAD_ENABLED` (`core.py`, bool, default off;
  the description says InvenTree removed URL downloads and this re-enables them for MCP under InvenTree's own checks).
  `tools/images.py` holds the one implementation: `ensure_image_allowed()` (read-only gate, then the setting, then
  `require_access(PartDetail, PATCH)`, then for an existing part a GET so an unknown id is a 404) runs before any change
  and any network access; `upload_part_image()` downloads with `InvenTree.helpers_model.download_image_from_url`
  (lazy import; timeout 10 s, 1 MB cap, user agent `inventree-mcp/<version>`) in a worker thread
  (`sync_to_async(thread_sensitive=False)`), re-encodes (PNG when the image has alpha / a palette, else JPEG q90) into
  `part_<pk>.<ext>` and PATCHes `image` as multipart through `call_view(..., files=...)`. Failures (Django
  `ValidationError`, `ValueError`, `TypeError`, requests exceptions, and the plain `Exception` InvenTree raises for
  connection errors) become `image download failed: <text>` with the URL itself, its query, fragment, user name,
  password and any `user:pass@` scrubbed, cut at 300 characters. `create_part` POSTs first; when only the image fails
  the text is `part N was created, but setting its image failed: ...`. `update_part` PATCHes the other fields as JSON
  first (an image-only update is allowed, not "no fields to update"). The three descriptions keep the Go text plus one
  appended sentence naming `IMAGE_DOWNLOAD_ENABLED`. `remote_image` is no longer sent.
- **`proxy.py`.** `call_view(..., files=None)`: with files the request is `format="multipart"` (data beside the files);
  the JSON path is unchanged. The read-only gate is `ensure_writable(method)` with the text in `READ_ONLY_TEXT`
  (same text, now reusable).
- **`tools/_common.py`.** `require_access(view_loader, method)`: read-only gate, then `proxy.user_has_access()`,
  ToolError with the 403 text; used by the image helper and the stock adjustments (below).
- **`delete_part` (major 2).** Refuses a part with stock: `part N has K stock item(s): delete its stock first`
  (`stock_item_count` from the read; when absent, the `count` of `GET StockList ?part=N&limit=1`). Deactivates, deletes,
  and when the DELETE fails after its own deactivation PATCHes `active: true` back and re-raises the DELETE's error
  (a failing reactivation is swallowed so the original error is reported).
- **Stock adjustments, found in this round (security).** The new MCP-role tests showed that a user with the
  interactive `stock.add` role but only the MCP `stock.view` role could run `stock_add_quantity`,
  `stock_remove_quantity` and `stock_transfer`. Cause (InvenTree 1.5.7): `InvenTreeTokenMatchesOASRequirements`
  resolves StockAdd / StockRemove / StockTransfer to `g:read` for every method (checked with
  `get_required_alternate_scopes`; StockList POST gives `r:add:stock` / `r:add:build`), so the proxied view enforces
  the interactive role but not the MCP scope. Fix inside the tool files: the adjustments call
  `require_access(StockList, POST)` before their own POST, and their ACCESS entries are `StockList POST` (same
  interactive role, correct scope), so visibility matches. Recommendation for the orchestrator: a generic fix in
  `oauth2_bridge.scoped_view_class()` (derive the scope from the view's model when core answers only `g:read` for a
  write method) and an upstream report; then the tool-side check can go.
- **Docs of delete semantics (minor 9).** `tools/categories.py` and `tools/locations.py` module docstrings say the
  deletes move contents to the parent (the tool docstrings are the Go descriptions verbatim, so the note is in the module
  docstring and the existing comment); tests assert it with a nested tree.
- **`images.py` JSON guard (minor 6)** and **`stock._items()` (minor 7)** as the review proposed.
- **Tests.** `WriteToolTestCase` and `NOT_FOUND` / `DENIED` / `READ_ONLY` live in `tests/test_transport.py`; all five
  tool modules import them (no local constants, `setUp`, token helpers). `WriteToolTestCase` gained
  `assert_denied_without_inventree_role()` and `assert_denied()`; both denial helpers check filter off and on and leave
  the filter off (so `setting_cleanup_reset` and the inline resets are gone). Permission tests per write tool: MCP role
  missing and InvenTree role missing (parts, categories new; stock and locations got the MCP-missing cases with the
  roles the review lists). `test_tools_framework.ToolSetContractTest`: `tools/list` is exactly the 26 names with titles
  and all four hints; ACCESS equals the literal view/method table; and the role/action column is checked by granting
  one InvenTree role/action per user (every MCP role, filter on) and comparing the visible tools.
  Renamed/fixed: `test_add_stock_validation`, the stock idempotent column is asserted,
  `test_delete_part_category_moves_children_and_parts_to_the_parent`, `test_delete_moves_contents_to_the_parent`.
  Image tests mock `download_image_from_url` with a real PIL image and write to a temporary `MEDIA_ROOT`; one test
  runs the real helper against `http://127.0.0.1/` (refused as a private address, no network needed).

### Decisions

- Check order for image calls: read-only first (every write tool must answer the read-only text, overview section 7),
  then the setting, then the permission. `create_part` checks before its POST, so a refused image creates nothing.
- InvenTree's `RuleSet.save()` implies view from change/add/delete and change from add/delete, and the MCP rulesets do
  the same; so "add without change" users do not exist and the contract test compares against the implied set. The
  role column of the overview is confirmed: the adjustments appear for `stock.add` and not for `stock.change`; no tool
  needs `stock.change`.
- 1 MB download cap and 10 s timeout as constants (InvenTree's former defaults); no extra settings.
- The image file name is `part_<pk>.<ext>`; InvenTree's storage may add a suffix when the name exists.

### Commands and counts (dev stack, InvenTree 1.5.7, API 530; one label at a time)

`dev/test.sh inventree_mcp.tests.<module>`: test_tools_parts 21 OK, test_tools_categories 14 OK, test_tools_images 27
OK, test_tools_stock 19 OK, test_tools_locations 16 OK, test_tools_framework 47 OK, test_transport 32 OK.
Whole suite `dev/test.sh`: Ran 396 tests, OK (skipped=3). (A last tidy of `tools/images.py`, which passes `part_id` into `_download` and catches re-encode errors, came after that run; the images and parts modules were rerun afterwards and pass.)
`uvx ruff@0.16.0 check --preview inventree_mcp`, `uvx ruff@0.16.0 format --preview inventree_mcp` and
`uvx ruff@0.16.0 check --preview --isolated inventree_mcp/tests`: clean.
Django-side checks with `dev/shell.sh`: `download_image_from_url` signature `(remote_url, timeout=2.5, user_agent='',
max_size=None)` with the SSRF guard on 1.5.7; `PartSerializer.image` writable, `remote_image` absent,
`stock_item_count` present; a multipart PATCH of `image` on PartDetail answers 200 with `image` and `thumbnail` set.

### For T4 / the orchestrator

- README / OPERATIONS: document `IMAGE_DOWNLOAD_ENABLED` (off by default, why) and that category / location deletes
  re-parent their contents; `delete_part` refuses parts with stock.
- The adjustment-tool ACCESS change (StockList POST) and the `oauth2_bridge` recommendation above.
- The overview's section 4 still says `set_part_image` sends `{remote_image}`; 6c supersedes it.
