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
