# T2+T3 review (Opus, 2026-10-16)

**Ready for T4: yes, after findings 1-4. The image decision (finding 1) must come before T4.** T4 writes the README, the e2e
script and the beta configuration around `set_part_image`. Either the download fix (section "Image fix requirements") or
the stop-gap from finding 1 must land first. Findings 2-4 are small; they can run in parallel with T4.

Verified with: `git diff 03a4e1d 27a6611`. I checked every new tool against `internal/tools/{parts,categories,stock,locations}.go`
and `internal/imagesearch/google.go`: descriptions, parameter texts, defaults, payload rules and result texts. I checked
InvenTree's 1.5.7 tag and master in `/home/alex.lucaci/Projects/inventree/InvenTree`: `permissions.py`, `stock/api.py`,
`part/models.py`, `part/serializers.py`, `helpers_model.py`, commit 99358eb4e. `dev/test.sh` per label: parts 22,
categories 13, images 11, stock 19, locations 15, all OK; whole suite Ran 376 OK (skipped=3). Ruff: all three clean.

## Major

1. **`set_part_image` and `image_url` report success and do nothing** (`parts.py:157,208,237-264`; `images.py:98-104`).
   - On 1.5.7 (API 530) and later, the PATCH answers 200 with `image: null`. The client is told that the image was set.
   - The `search_part_images` description sends models straight to these tools.
   - T2 kept the contract and pinned the behaviour in `test_set_part_image_with_a_real_patch` (`test_tools_parts.py:244-263`).
     That is an honest record, but the user-facing result is wrong.
   - InvenTree removed the field deliberately (commit 99358eb4e, "Close massive security hole entirely"). The
     `INVENTREE_DOWNLOAD_FROM_URL`, `..._MAX_SIZE` and `..._USER_AGENT` settings are gone too. So the overview's
     "error text names INVENTREE_DOWNLOAD_FROM_URL" (section 4) cannot happen; amend it.
   - Stop-gap if the fix is deferred: when `inventreeApiVersion() >= 489` and `image_url` is set, raise
     `ToolError("image_url is not supported by this InvenTree version (remote_image was removed at API 489)")`.
     Do this in `set_part_image` and in `create_part`/`update_part` before any call. Change the pinned test to expect
     that error.
2. **`delete_part` changes data when the delete fails, and deletes stock silently** (`parts.py:228-234`).
   - (a) The tool PATCHes `active: false` and then DELETEs. If the DELETE fails, the part stays deactivated. Causes:
     a 403 (change but not delete; finding 6 of the T2 report), a 400 "used in an assembly"
     (`PART_ALLOW_DELETE_FROM_ASSEMBLY` off), or a 400 "locked".
     Fix: wrap the DELETE. On any ToolError, if the tool deactivated the part, PATCH `{"active": True}` back, then re-raise.
     Add a test that uses a part that is a BOM `sub_part` (the delete is refused and the part is still active) and
     tighten `test_tools_parts.py:337-338`, which asserts only `name != "Nope"`.
   - (b) `StockItem.part` is `on_delete=CASCADE` (`stock/models.py`, 1.5.7). Deleting a part with stock deletes all of
     its stock items. The verbatim description says "The part must have no stock items."
     Fix (preferred, makes the contract text true and costs no extra call): the tool already GETs the part, so
     `if part.get("stock_item_count"): raise ToolError(f"part {id} has stock items: delete or move them first")`
     before the PATCH. `stock_item_count` is a PartSerializer field on 1.5.7 and master.
     Alternative: keep Go behaviour and have T4 document the cascade. Do not leave it undocumented.
3. **Test scaffolding duplicated by T3** (`test_tools_stock.py:11-32,126-128,372-390`; `test_tools_locations.py:19-28,122-124`).
   - T3 redefines `READ_ONLY_TEXT`, `FORBIDDEN`, `NOT_FOUND` (T2 has `READ_ONLY`, `DENIED`, `NOT_FOUND`), repeats
     the `MCP_READ_ONLY` `setUp` of `WriteToolTestCase`, and re-implements `make_token` + `error_as` inline.
   - Change: move `WriteToolTestCase` and the three text constants from `test_tools_parts.py:25-77` into
     `tests/test_transport.py`, next to `MCPToolTestCase`, which is their base. (Not `oauth_helpers.py`: these helpers
     are about tool calls, not OAuth.) The five tool test modules import from there.
   - Rebase `StockWriteToolsTest` and `LocationWriteToolsTest` on `WriteToolTestCase`. Replace
     `test_missing_mcp_role_is_forbidden` with `assert_denied_without_mcp_role`.
4. **Permission tests are incomplete, in opposite directions:**
   - T2 has no test for a user with the MCP role but without the InvenTree role (parts, categories). Add one per
     module, like T3's `test_missing_inventree_role_is_forbidden`: a class with only `part.view` (or `part_category.view`),
     every write tool answering the 403 text, nothing changed.
   - T3 checks a missing MCP role only for `add_stock`, and only with the filter off. Nothing covers the other four stock
     tools or the three location tools. Add cases through `assert_denied_without_mcp_role` (filter off and on), with:
     - add_stock: interactive `stock.view` + `stock.add`, MCP `stock.view`;
     - the three adjustments: interactive `stock.view` + `stock.add` (see finding 5), MCP `stock.view`;
     - delete_stock_item: all stock roles, MCP `stock.view` + `stock.change`;
     - locations: per verb, as in the categories test.

## Minor

5. **Overview section 4: wrong role for `stock_add_quantity`/`stock_remove_quantity`/`stock_transfer`** (`stock/change`).
   - StockAdd, StockRemove and StockTransfer are `CreateAPI` views without `role_required`. `RolePermission` maps
     POST to `add`, so InvenTree and the MCP scope both require `stock.add` (`r:add:stock`). ACCESS is right; amend the
     table, and T4's README role table and beta grants must say `add`.
6. `images.py:61`: `response.json().get("items")` raises an unhandled AttributeError when the JSON body is not an object.
   Fix: `body = response.json()` and then `items = (body.get("items") if isinstance(body, dict) else None) or []`.
   Keep it inside the `try` block.
7. `stock.py:163,185,207`: `[i.model_dump() for i in items]` is repeated 3 times. Add a module-level
   `def _items(items): return [i.model_dump() for i in items]`.
8. Test texts that do not match what they assert:
   - `test_tools_categories.py:177-183`: the docstring says the flags "default to false", but the tool sends them
     explicitly. The name says "keeps children and parts", but it checks no part. Put a part in `root` and assert that
     it moves to `parent=None`.
   - `test_tools_stock.py:97-103,141-146`: the `idempotent_ignored` column is dead. Drop it, or assert `idempotentHint`
     from it as locations does.
   - `test_tools_parts.py:340-341`: drop `setting_cleanup_reset` and inline the line, as categories does.
   - `test_tools_stock.py:209`: no 404 case in `add_stock`; rename it to `test_add_stock_validation`.
9. `delete_part_category` and `delete_stock_location` re-parent their contents (InvenTree, with both flags false; tested
   in `test_delete_part_category_keeps_children_and_parts` and `test_delete_does_not_enforce_empty_on_1_5_7`). The
   verbatim descriptions say "must have no parts or sub-categories" / "must be empty".
   - Acceptable as it is (nothing is lost); T4 documents it. Optional, as in 2b: GET first and refuse when
     `part_count`/`subcategories` or `items`/`sublocations` is non-zero, which makes the descriptions true.
10. Differences from the Go payloads (accepted; record them in the overview section 6b):
   - `clean()` drops `part=0` (`add_stock`) and `location=0` (`stock_transfer`), where Go sends 0. The 400 reads
     "This field is required" instead of an invalid-pk message.
   - A negative `minimum_stock` is sent on create, where Go sends only `> 0`. InvenTree validates it.
   - `delete_part` GETs first (spec), so it also needs `part.view`.

## Report items for the orchestrator

| item | verdict |
|---|---|
| T2-1 `remote_image` gone at API 489 | Needs action before T4: finding 1, then the fix below. |
| T2-2 / T3-1 delete bodies on tree nodes | Correct. Both send `false/false` (`categories.py:188`, `locations.py:191`), and both are tested against the real view. Amend overview 6b for categories as was done for locations. |
| T2-3 delete_part partial on 403 | Needs action: finding 2a. Not a privilege escalation, because the same user may deactivate through `update_part`. But it changes data on a refused call, and the same happens for the 400 cases. |
| T2-4 parts with stock deletable | Needs action: finding 2b (cascade deletes stock). |
| T3-2 over-removal clamps and deletes the emptied item | Acceptable. Pinned by `test_stock_remove_more_than_available_clamps_to_zero`; already in 6b for T4's e2e script. |
| T3-3 partial transfer splits the item | Acceptable; tested. |
| T1-review minor 8 (`search_part_images` in READ_TOOLS, the exact 26-name test) | Still open; it is the orchestrator's. |

## Confirmed correct

- **Names, titles and annotations.** All 16 write tools plus `search_part_images` match section 4 and the Go
  `DestructiveHint`s. The update tools are idempotent and the rest are not. `search_part_images` is readOnly + openWorld.
  No `outputSchema`.
- **Descriptions and parameter texts.** They are verbatim Go. The only edit is "environment variables" -> "plugin
  settings" in `search_part_images`, which its test asserts.
- **Inputs.** Required inputs and defaults match: `bool | None` for the tri-state booleans, `""`/0 for the omitted ones.
  `StockAdjustment` gives the `items` schema explicitly. String coercion (`"12.5"`, `"true"`, `"2"`) is tested.
- **Payloads.** Every payload goes through `clean()`, and every update payload through `changes()` ("no fields to
  update"). `image_url` -> `remote_image` (per contract). `serial` -> `serial_numbers`, verified with a trackable part.
  `add_stock` goes through `fetch()`, which takes the first item of the list.
- **Results.** Results come from `fetch()`/`compact()` with the right FIELDS tuple. Text results are the exact Go
  strings ("Part N deleted successfully.", "Stock quantity updated successfully.", etc.).
- **`search_part_images`.**
  - Uses `requests` with a 10 s timeout, `searchType=image`, and `num` handled as Go does (<=0 -> 5, clamped to 1..10).
  - Checks "unconfigured" before "query is required". Answers the no-hit text, and the result keys are exact.
  - Settings are read through `plugin_setting` (`GOOGLE_API_KEY` is protected) and the request runs in a thread.
  - The key is scrubbed raw and `quote_plus`-encoded from exception text. The exception is raised `from None`, and the
    status error carries no URL. Tests cover all three exception types and the 500/undecodable paths.
  - No ACCESS entry; the module docstring says why. It is visible to a user without part roles with the filter on (tested).
- **Security and permissions.**
  - Every InvenTree call goes through `call_view`/`fetch` with the right method. There is no ORM in the tools.
  - The `MCP_READ_ONLY` gate is untouched. Tests show every write tool answers the gate text and stays listed.
  - ACCESS: create = POST list; update and set_part_image = PATCH detail; deletes = DELETE detail (delete_part: DELETE);
    adjustments = POST on StockAdd/StockRemove/StockTransfer.
- **Tests that pin real InvenTree behaviour.** Over-removal clamp, re-parenting on location/category delete, the
  transfer split, the no-op `remote_image` (API-version branched), and the read-back of quantities through ORM and
  `get_stock_item`.

## Image fix requirements

1. **Gate.** Add a plugin setting, e.g. `MCP_ALLOW_IMAGE_DOWNLOAD` (bool, default False).
   - Without it, `image_url` answers a ToolError naming the setting. InvenTree removed this as a security hole, so
     enabling it must be the admin's explicit choice; T4 documents it in README/OPERATIONS.
2. **Authorise before any fetch.**
   - Before downloading, check `MCP_READ_ONLY` (same text as `call_view`) and `proxy.user_has_access(PartDetail,
     "PATCH")` (create: `PartList`, "POST"). Otherwise a read-only or unauthorised user makes the server fetch URLs.
3. **Download with InvenTree's helper.** Use `InvenTree.helpers_model.download_image_from_url(url, timeout=..., max_size=...)`.
   - It is present on 1.5.7 and master, with the same signature. It does the URL validation,
     `validate_url_no_ssrf`, `ssrf_safe_context` (DNS rebinding), manual redirect checks, a Content-Length/size cap
     (default 1 MB) and PIL validation, and it returns a PIL image.
   - Import it lazily (missing -> "image download is not available on this InvenTree version"); run it in a thread.
   - Map `ValidationError`/`requests` errors/`ValueError`/`TypeError` to `ToolError("image download failed: ...")`.
     Never echo response bodies.
4. **Re-encode.** Re-encode the PIL image into `BytesIO` in its own format (PNG fallback). Wrap it as
   `SimpleUploadedFile(f"part_{id}.{ext}", data, content_type)`.
5. **Upload through `call_view`.** `proxy.py` needs a multipart variant. Today non-GET requests are always
   `format="json"`, so add e.g. `files=` / `format="multipart"`.
   - PATCH `PartDetail` with `{"image": file}` (`PartSerializer.image` is writable on 1.5.7 and master); the result is
     `compact(PART_FIELDS)` with `image`/`thumbnail` set.
6. **Wire the tools.**
   - `set_part_image`: download, then PATCH. `update_part`: the other fields as JSON PATCH, then the image PATCH.
   - `create_part`: POST, then the image PATCH. On an image failure, raise "part N created, but setting the image
     failed: ...", so the model does not retry the create.
7. **Tests.**
   - Mock `download_image_from_url` to return a small PIL image and assert that `Part.image` is set. Cover: setting
     off -> error; read-only or no `part.change` -> no download call; helper ValidationError -> the error text.
     Replace the API-489 branch test.
