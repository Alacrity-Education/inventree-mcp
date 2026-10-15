# T2: write tools for parts and categories, and search_part_images (Sonnet)

Read first: `docs/plans/tools/00-overview.md` (binding: sections 3, 4, 5), `docs/plans/tools/T1-report.md` (what T1
built, the resolved view names, SDK facts), `inventree_mcp/tools/_common.py` (use it; add nothing shared elsewhere),
`inventree_mcp/tools/parts.py` and `tools/categories.py` (append to them), the Go sources
`vendor/syntaxerr66-inventree-mcp/internal/tools/{parts,categories}.go` and `internal/imagesearch/google.go`.

## Deliverables

1. `tools/parts.py`: append `create_part`, `update_part`, `delete_part`, `set_part_image` exactly per the table
   (inputs, defaults, descriptions verbatim, annotations through `_common.tool`, payloads through `_common.clean`,
   outputs `compact(PART_FIELDS)`, text results for delete). `image_url` is sent as `remote_image`. `update_part` and
   `set_part_image` with an empty payload raise ToolError "no fields to update" / "image_url is required" (Go texts).
   `delete_part`: PATCH `{active: false}` first (skip the PATCH if the part is already inactive: read it first), then DELETE;
   both go through `call_view`. ACCESS entries: create -> POST PartList, update/set_part_image -> PATCH PartDetail,
   delete -> DELETE PartDetail.
2. `tools/categories.py`: append `create_part_category`, `update_part_category`, `delete_part_category` likewise.
3. `tools/images.py`: `search_part_images` per the table: Google Custom Search (`requests`, 10 s timeout, `num` clamped
   1..10, `searchType=image`), credentials from the plugin settings `GOOGLE_API_KEY` and `GOOGLE_CSE_ID` through
   `inventree_mcp.settings.plugin_setting`; unconfigured -> ToolError "image search is not configured: set the plugin
   settings GOOGLE_API_KEY and GOOGLE_CSE_ID"; empty query -> "query is required"; no hits -> text "No images found for
   query: ..."; result keys exactly title, link, thumbnail_url, context_url, width, height. The API key must never appear
   in errors or logs (strip it from exception text). No ACCESS entry (documented in the module docstring); annotations
   readOnly=true, openWorld=true.
4. Tests, appended to `tests/test_tools_parts.py` and `tests/test_tools_categories.py` (T1 created them with the read
   tests) and a new `tests/test_tools_images.py`:
   - Run on the dev stack with `MCP_READ_ONLY` off and the test group granted the MCP roles the tool needs (see
     `tests/oauth_helpers.grant_mcp` and how `tests/test_policy.py` sets `MCP_READ_ONLY` and roles); use a user with the
     interactive roles (`roles = [...]` on the test class, as InvenTreeTestCase supports).
   - Per write tool: the happy path (create returns the compact shape with the new pk; update changes only the given
     fields; delete answers the Go text and the object is gone, delete_part on an active part deactivates first), the
     validation error text (400 with `field: message`), the 404 text, the permission text (a user whose group lacks the
     MCP role; both filter on and off), and the read-only gate text with `MCP_READ_ONLY` on.
   - `image_url` -> `remote_image` is asserted by inspecting the payload (patch `call_view` with `unittest.mock`) rather
     than by downloading anything; one test runs the real PATCH with `INVENTREE_DOWNLOAD_FROM_URL` off and asserts the
     error text mentions that setting.
   - search_part_images: `requests.get` is mocked (`unittest.mock.patch("inventree_mcp.tools.images.requests.get")`);
     unconfigured, empty query, clamping, result shape, API error, "no images" path, key never in the error text.
   - `tools/list` over the transport shows the new tools with their titles and annotations.
5. `docs/plans/tools/T2-report.md`: what was built, test counts and commands, decisions (e.g. the `serial`-style
   verification you did for `remote_image`), anything the overview got wrong.

## Boundaries

Only the files above. `_common.py`, `proxy.py`, `tool_visibility.py`, `mcp_server.py` belong to T1 (done); if you
need a change there, write it in your report under "Requests for the orchestrator" instead of editing. No git. Lint
and tests as in the overview; finish with `dev/test.sh inventree_mcp.tests.test_tools_parts`,
`... test_tools_categories`, `... test_tools_images` green.
