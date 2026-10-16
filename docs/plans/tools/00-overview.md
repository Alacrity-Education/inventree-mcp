# Tools rewrite: the syntaxerr66 tool set inside the plugin (binding contract)

Decision (2026-10-16): the plugin's 56 read-only list/get tools are replaced by the 26 tools of
https://github.com/syntaxerr66/inventree-mcp (Go, stdio; vendored read-only at
`/home/alex.lucaci/Projects/inventree/vendor/syntaxerr66-inventree-mcp`, files `internal/tools/{parts,stock,locations,categories}.go`,
`internal/client/client.go`, `internal/coerce/coerce.go`, `internal/imagesearch/google.go`, `README.md`, `CLAUDE.md`).
No backward compatibility: tool names, inputs and outputs follow the Go server. What stays is the plugin architecture:
the Streamable HTTP transport, the OAuth façade and membership re-check, the MCP permission set (per-group MCP roles as
scopes), the in-process proxy (`proxy.call_view`) through InvenTree's own DRF views, tool visibility, tool logging,
the pages. Version becomes 0.5.0.

Read first: this file, `docs/plans/00-overview.md` (the auth/permission architecture), `AGENTS.md`, the Go sources above.

## 1. What is removed

- `inventree_mcp/tools/*` (all 16 modules, 56 tools), `tools/discovery.py` (describe_filters, make_web_link, RESOURCE_LOADERS).
- `inventree_mcp/output_schemas.py`, `schema_introspection.py`, `filter_introspection.py` (serializer-derived output
  schemas and filter introspection: the new tools return compact, fixed shapes; no output schema is published).
- `inventree_mcp/test_mcp.py` (upstream's 2539-line suite of the old tools). Its transport, logging, settings, server-card,
  view-resolution, well-known and MCP_READ_ONLY tests are salvaged into `tests/test_transport.py` (same assertions,
  adapted to the new tool names); everything that tested the old tools, filters or output schemas is dropped.
- README sections describing the old tools, `describe_filters` and `make_web_link`.

What stays untouched: `auth/*`, `models.py`, migrations, `frontend/`, `static/`, `mcp_transport.py`, `context.py`,
`oauth2_bridge.py`, `settings.py`, `view_resolution.py`, `tool_logging.py`, `core.py` (except settings/version), `dev/*`
(except the new e2e script), `deploy/*` (except the beta configuration).

## 2. Module layout (binding)

```
inventree_mcp/
  mcp_server.py        MCPServer instance, new instructions text; imports tools.{parts,categories,stock,locations,images}
  tool_visibility.py   unchanged behaviour; its map comes from tools._common.ACCESS (no RESOURCE_LOADERS)
  proxy.py             call_view() as today + the error texts of section 5; MCP_READ_ONLY gate unchanged
  tools/_common.py     helpers of section 3 (the only shared tool code; no duplicates in tool modules)
  tools/parts.py       search_parts, get_part, list_parts (T1); create_part, update_part, delete_part, set_part_image (T2)
  tools/categories.py  search_part_categories, list_part_categories (T1); create/update/delete_part_category (T2)
  tools/stock.py       get_stock, get_stock_item (T1); add_stock, stock_add_quantity, stock_remove_quantity, stock_transfer, delete_stock_item (T3)
  tools/locations.py   search_stock_locations, get_stock_location, list_stock_locations (T1); create/update/delete_stock_location (T3)
  tools/images.py      search_part_images (T2; Google Custom Search through the plugin settings)
tests/
  test_transport.py    salvaged transport/logging/settings/card/view-resolution tests (T1)
  test_tools_framework.py  section 3 helpers, annotations, ACCESS completeness, coercion, error texts, read-only gate (T1)
  test_tools_parts.py / test_tools_categories.py (T2), test_tools_stock.py / test_tools_locations.py (T3)
dev/tools_e2e.py       live round trip of the write tools on the dev stack (T4)
```

## 3. Shared helpers: `tools/_common.py` (interface, T1 implements)

```python
DEFAULTS = {"search": 25, "list": 50, "tree": 100}   # Go defaults: search 25; list_parts/get_stock 50; locations/categories 100
MAX_LIMIT = 500                                      # clamp; InvenTree pages this large are fine

def tool(name: str, *, title: str, read_only: bool, destructive: bool | None = None,
         idempotent: bool | None = None, open_world: bool = False):
    """@mcp.tool(name=..., title=..., annotations=ToolAnnotations(readOnlyHint=read_only, destructiveHint=...,
    idempotentHint=..., openWorldHint=open_world)) - the one place annotations are set. Verify the mcp 2.x
    MCPServer.tool() keyword names at implementation time (it accepts `title` and `annotations`)."""

ACCESS: dict[str, tuple[Callable[[], type | None], str]] = {}
def access(tool_name: str, view_loader: Callable[[], type | None], method: str) -> None:
    """Declare the InvenTree view + HTTP method whose permission decides tool visibility (tool_visibility reads it).
    Every tool except search_part_images registers exactly one entry; a test enforces it."""

async def listing(view_loader, path: str, params: dict, *, limit: int, default: int, fields: tuple[str, ...]) -> dict:
    """GET through call_view with limit/offset (clamped, default when <= 0), returns {"count": n, "results": [compact...]}
    (next/previous dropped, as the Go server does)."""

def compact(obj: dict, fields: tuple[str, ...]) -> dict:
    """Keep only *fields*, in that order; missing keys become None. Nested: STOCK_ITEM_FIELDS' part_detail keeps (pk, name, full_name)."""

def clean(**kwargs) -> dict:
    """Payload builder with the Go omission rules: drop None; drop "" strings; drop 0 for the fields the Go code
    treats "0 or omit" as unset (ids such as category/parent/location/default_location, and minimum_stock on create);
    booleans are sent only when given (None = omitted). Callers pass only what the tool accepts."""

def text(message: str) -> str          # plain-text results ("Part 12 deleted successfully.")

PART_FIELDS = ("pk","name","full_name","description","category","category_name","active","in_stock","total_in_stock",
               "minimum_stock","units","IPN","keywords","link","purchaseable","assembly","component","trackable","virtual",
               "image","thumbnail","tags")
STOCK_ITEM_FIELDS = ("pk","part","quantity","serial","batch","location","in_stock","status","status_text","notes",
                     "updated","tags","part_detail")      # part_detail -> {pk,name,full_name}; the view needs ?part_detail=true
LOCATION_FIELDS = ("pk","name","description","parent","pathstring","level","items","sublocations","structural",
                   "external","icon","tags")
CATEGORY_FIELDS = ("pk","name","description","parent","pathstring","level","part_count","subcategories","starred",
                   "structural","icon","default_location")
```

View loaders (in the tool modules, through `view_resolution.resolve_view` / `resolve_view_any`; verify the class names on
the dev stack 1.5.7 and the beta 1.6 image, both must resolve):
`part.api.PartList`, `PartDetail`, `CategoryList`, `CategoryDetail`; `stock.api.StockList`, `StockDetail`,
`StockLocationList`, `StockLocationDetail`, `StockAdd`, `StockRemove`, `StockTransfer`.

## 4. The 26 tools (binding)

Inputs are MCP arguments (pydantic; `"32"` must coerce to 32, `"true"` to True: rely on the SDK's lax validation and
test it). Defaults and omission rules are the Go server's. `id` arguments are database pks. Descriptions are the Go
descriptions verbatim (they carry the workflow guidance the user wants), with "environment variables" replaced by
"plugin settings" where applicable. Role column = InvenTree role of the view (used by MCP roles + core).

| tool | title | annotations (readOnly/destructive/idempotent/openWorld) | inputs | InvenTree call | role/action | output |
|---|---|---|---|---|---|---|
| search_parts | Search parts | R | search:str (req), limit:int=25 | GET PartList ?search&limit | part/view | listing(PART_FIELDS) |
| get_part | Get part | R | id:int | GET PartDetail pk | part/view | compact(PART_FIELDS) |
| list_parts | List parts | R | category:int=0, limit:int=50, offset:int=0 | GET PartList ?category&limit&offset | part/view | listing |
| create_part | Create part | W, destructive=false | name (req), description, category, IPN, keywords, units, minimum_stock:int, purchaseable/component/assembly/trackable/virtual: bool\|None, image_url | POST PartList (image_url -> `remote_image`) | part/add | compact(PART_FIELDS) |
| update_part | Update part | W, destructive=false, idempotent=true | id, name, description, category, active:bool\|None, IPN, keywords, units, minimum_stock, image_url | PATCH PartDetail pk; error "no fields to update" when payload empty | part/change | compact |
| delete_part | Delete part | W, destructive=true | id | PATCH PartDetail {active:false} then DELETE PartDetail pk | part/change + part/delete (ACCESS: delete) | text "Part N deleted successfully." |
| set_part_image | Set part image | W, destructive=false, idempotent=true | id, image_url (req) | PATCH PartDetail {remote_image} | part/change | compact |
| search_part_images | Search part images | R, openWorld=true | query (req), num:int=5 (1..10) | Google Custom Search (settings GOOGLE_API_KEY, GOOGLE_CSE_ID), 10 s timeout, no InvenTree call | none (no ACCESS entry) | {"query","count","results":[{title,link,thumbnail_url,context_url,width,height}]} or text "No images found for query: ..." |
| search_part_categories | Search part categories | R | search (req), limit=25 | GET CategoryList ?search | part_category/view | listing(CATEGORY_FIELDS) |
| list_part_categories | List part categories | R | parent:int=0, limit=100, offset=0 | GET CategoryList ?parent | part_category/view | listing |
| create_part_category | Create part category | W, destructive=false | name (req), description, parent, default_location, structural:bool\|None | POST CategoryList | part_category/add | compact(CATEGORY_FIELDS) |
| update_part_category | Update part category | W, destructive=false, idempotent=true | id, name, description, parent, default_location | PATCH CategoryDetail | part_category/change | compact |
| delete_part_category | Delete part category | W, destructive=true | id | DELETE CategoryDetail | part_category/delete | text "Category N deleted successfully." |
| get_stock | List stock items | R | part:int=0, location:int=0, limit=50, offset=0 | GET StockList ?part&location&part_detail=true | stock/view | listing(STOCK_ITEM_FIELDS) |
| get_stock_item | Get stock item | R | id | GET StockDetail pk ?part_detail=true | stock/view | compact(STOCK_ITEM_FIELDS) |
| add_stock | Add stock | W, destructive=false | part (req), quantity:float (req), location, batch, serial, notes | POST StockList {part, quantity, location?, batch?, serial?, notes?}; the view may answer a list -> return the first item | stock/add | compact(STOCK_ITEM_FIELDS) |
| stock_add_quantity | Add stock quantity | W, destructive=false | items:[{pk:int, quantity:float}] (req), notes | POST StockAdd {items, notes?} | stock/change | text "Stock quantity updated successfully." |
| stock_remove_quantity | Remove stock quantity | W, destructive=true | items, notes | POST StockRemove | stock/change | text "Stock quantity removed successfully." |
| stock_transfer | Transfer stock | W, destructive=false | items (req), location:int (req), notes | POST StockTransfer {items, location, notes?} | stock/change | text "Stock transferred successfully." |
| delete_stock_item | Delete stock item | W, destructive=true | id | DELETE StockDetail | stock/delete | text "Stock item N deleted successfully." |
| search_stock_locations | Search stock locations | R | search (req), limit=25 | GET StockLocationList ?search | stock_location/view | listing(LOCATION_FIELDS) |
| get_stock_location | Get stock location | R | id | GET StockLocationDetail | stock_location/view | compact |
| list_stock_locations | List stock locations | R | parent=0, limit=100, offset=0 | GET StockLocationList ?parent | stock_location/view | listing |
| create_stock_location | Create stock location | W, destructive=false | name (req), description, parent, structural:bool\|None | POST StockLocationList | stock_location/add | compact |
| update_stock_location | Update stock location | W, destructive=false, idempotent=true | id, name, description, parent | PATCH StockLocationDetail; "no fields to update" when empty | stock_location/change | compact |
| delete_stock_location | Delete stock location | W, destructive=true | id | DELETE StockLocationDetail | stock_location/delete | text "Location N deleted successfully." |

R = readOnlyHint true, destructive false, idempotent true. W = readOnlyHint false. openWorld false unless stated.
The `serial` input of add_stock: verify on the dev stack whether InvenTree's create serializer takes `serial` or
`serial_numbers`; send what works, keep the input name `serial`. `image_url` needs InvenTree's global setting
`INVENTREE_DOWNLOAD_FROM_URL`; when the API rejects `remote_image` because it is off, the error text must say so.

## 5. Errors and gates (binding)

- Every InvenTree failure surfaces as a ToolError whose text starts like the Go server's (`proxy.py`, T1):
  401 "authentication failed (401)"; 403 "permission denied (403): your MCP roles or your InvenTree permissions do not
  allow this"; 404 "not found (404): resource does not exist"; 400 "validation failed (400): " followed by `field: message`
  lines built from InvenTree's error body (lists joined, `non_field_errors` and `detail` without a prefix); anything else
  "API error <status>: <body>" with the body truncated to 500 characters. Tokens and secrets never appear in messages.
- The read-only gate stays in `call_view` (`MCP_READ_ONLY`, default True): a write tool answers the existing ToolError
  ("This MCP server is running in read-only mode ...") and the write tools are still listed (MCP_FILTER_TOOL_LIST).
- MCP roles: write tools need the matching `r:add|change|delete:<role>` scope (they exist only when MCP_READ_ONLY is off),
  plus the user's interactive role. `delete_part` needs change and delete.
- `tool_visibility` uses ACCESS (view + method) so a user without `r:delete:part` does not see delete_part when the
  filter is on.
- Settings added (core.py): `GOOGLE_API_KEY` (protected), `GOOGLE_CSE_ID`; both empty by default.
- Server instructions (mcp_server.py): rewritten for the new tools; it carries the Go CLAUDE.md workflow guidance
  (search before create; categories: list the hierarchy, pick the deepest fitting one, create under the right parent;
  part descriptions from part numbers) and states that writes need the Read Only setting off and MCP write roles.

## 6. Stages, agents, ownership

| stage | agent | owns | deliverable |
|---|---|---|---|
| T1 framework + read tools | Opus | everything in section 2 marked T1, the deletions, `proxy.py`, `tool_visibility.py`, `mcp_server.py`, `server_card.py`, `core.py`, `tools/_common.py`, `tools/images.py` (empty module), `tests/test_transport.py`, `tests/test_tools_framework.py`, the tool-name updates in `tests/test_policy.py`, `tests/test_auth_flow.py`, `tests/test_ui.py` | `docs/plans/tools/T1-report.md` |
| Opus review of T1 | Opus | none (report only) | `docs/plans/tools/T1-review.md`; fixes by the orchestrator or T1's agent |
| T2 parts + categories writes, images | Sonnet | append to `tools/parts.py`, `tools/categories.py`; `tools/images.py`; `tests/test_tools_parts.py`, `tests/test_tools_categories.py` | `T2-report.md` |
| T3 stock + locations writes | Sonnet (parallel with T2) | append to `tools/stock.py`, `tools/locations.py`; `tests/test_tools_stock.py`, `tests/test_tools_locations.py` | `T3-report.md` |
| Opus review of T2+T3 | Opus | none | `T23-review.md` |
| T4 docs, e2e, beta | Sonnet | `README.md`, `docs/OPERATIONS.md`, `AGENTS.md`, `dev/tools_e2e.py`, `dev/README.md`, `deploy/beta/configure_inventree.py` (+`.env.example`) | `T4-report.md` |
| final Opus review + release 0.5.0 | Opus / orchestrator | | `final-review.md` |

Rules for every agent: no edits outside the owned files; no duplicated helper code (use `_common.py`); no `git add`/`commit`;
`uvx ruff@0.16.0 check --preview inventree_mcp && uvx ruff@0.16.0 format --preview inventree_mcp` clean; tests run with
`dev/test.sh <label>` on the dev stack (InvenTree 1.5.7, admin/admin, demo data) and, for the final check,
`dev/test-image.sh up latest && dev/test-image.sh test` (InvenTree master); each stage ends with a report listing what
was verified and how.

## 6a. Corrections recorded after T1 (binding for T2/T3/T4)

- Ten read tools, not eleven: `search_part_images` counts as a T2 tool (its only "read" is Google).
- T1 created the four `tests/test_tools_*.py` files with the read tests; T2/T3 append their write tests to them.
- `_raw_api_token` and `API_TOKEN_V2_MIN_API_VERSION` live in `tests/oauth_helpers.py`.
- `part_detail` on stock items is `null` unless the caller may view parts (InvenTree 1.5.7); `tags` is `null` in the
  API output on 1.5.7. Both are InvenTree behaviour, not tool bugs; tests assert the key, not a value.
- Every error text a client sees is prefixed by the SDK with `Error executing tool <name>: `; tests match on the
  plugin's part of the text.
- The SDK (mcp 2.3.0) coerces `"1"` to 1 and `"true"` to True itself; `ToolAnnotations` fields are snake_case in Python.
- `test_policy`'s request-host test now checks that a paged call succeeds (the `next` link is no longer returned).

## 7. Acceptance

- `tools/list` returns exactly the 26 names of section 4 with titles and annotations as specified.
- Every tool works on the dev stack through the proxy with an API token and with an OAuth token (dev/tools_e2e.py):
  category -> part -> stock item -> add/remove/transfer -> deletes, with `MCP_READ_ONLY` off and MCP write roles granted.
- With `MCP_READ_ONLY` on, every write tool answers the read-only ToolError; with the MCP filter on, a user without the
  write roles does not see the write tools.
- Full suite green on 1.5.7 and on the `latest` image; CI green; version 0.5.0 released; the beta runs it with
  `MCP_READ_ONLY` off and ItAdmins holding all four MCP roles on part, part_category, stock and stock_location.
