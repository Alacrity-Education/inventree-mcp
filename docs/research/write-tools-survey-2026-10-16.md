# Write tools: what upstream and the community ship (2026-10-16)

Two Sonnet audits run on 2026-10-16, kept verbatim. Question: does any InvenTree MCP implementation present modify/write tools, and how? Summary and decisions: see the closing section.

## 1. Upstream inventree/inventree-mcp (origin/main 0.3.0, PR #49, tags 0.1.0 to 0.3.0)

Upstream `origin/main` (c1dcdfa, 0.3.0) registers no write tools: all 56 tools are read-only. Upstream does anticipate write tools. It ships a `MCP_READ_ONLY` kill switch (default `True`), a non-GET branch in `proxy.call_view()`, and README text saying "Once write tools land...". No issue, PR or TODO proposes a write tool, and none carries a roadmap. No tool carries MCP annotations or a title. Upstream PR #49 adds authentication only. No version through 0.3.0 shipped a stdio server or CLI.

1. Tool inventory on origin/main (56 `@mcp.tool()` decorators across `inventree_mcp/tools/*.py`; the count is the same at 0.2.0, 0.2.1, 0.2.2 and 0.3.0)
- project_codes (2): list_project_codes, get_project_code
- locations (2): list_locations, get_location
- categories (2): list_categories, get_category
- parts (2): list_parts, get_part
- stock (2): list_stock_items, get_stock_item
- stock_history (4): list_stock_tracking, get_stock_tracking, list_stock_test_results, get_stock_test_result
- purchase_orders (4): list/get_purchase_order, list/get_purchase_order_line
- sales_orders (6): list/get_sales_order, list/get_sales_order_line, list/get_sales_order_allocation
- return_orders (4): list/get_return_order, list/get_return_order_line
- build_orders (6): list/get_build_order, list/get_build_line, list/get_build_item
- companies (6): list/get_company, list/get_contact, list/get_address
- supplier_parts (4): list/get_manufacturer_part, list/get_supplier_part
- bom (4): list/get_bom_item, list/get_bom_substitute
- parameters (4): list/get_parameter, list/get_parameter_template
- attachments (2): list_attachments, get_attachment
- discovery (2): describe_filters (`tools/discovery.py:173`), make_web_link (`tools/discovery.py:320`)
- Count: 54 list/get tools plus 2 helpers. The 0.1.0 tag had 55; make_web_link arrived in PR #35.
- Non-GET requests: none. Every `call_view(...)` in `tools/` is invoked with the literal "GET". `user_has_access` is also only called with "GET" (`tool_visibility.py:158`).
- The tool modules contain no create/update/delete/allocate/adjust code. The only "create" strings are docstrings in `tools/stock_history.py:3-8, 35-36`.

2. Scaffolding for write tools (all present, none used)
- `inventree_mcp/core.py:40-45`: MCP_READ_ONLY setting, name "Read Only", default True. Description: "Block all write actions via the MCP endpoint, regardless of the calling user's permissions."
- `inventree_mcp/proxy.py:77-81`: `if method != "GET" and get_plugin_setting("MCP_READ_ONLY"): raise ToolError("This MCP server is running in read-only mode; write actions are disabled. An administrator must disable the 'Read Only' plugin setting to allow them.")`
- `inventree_mcp/proxy.py:28-34`: `_DETAIL_ACTIONS` maps PUT, PATCH and DELETE to update, partial_update and destroy.
- `inventree_mcp/proxy.py:83-88`: the request is built with `data=data, format="json"` for non-GET methods.
- `inventree_mcp/proxy.py:126-216`: `user_has_access(view_cls, method="GET")` supports checking the permission for other HTTP methods. The docstring says "'GET' for every tool that exists today (all read-only)."
- `inventree_mcp/mcp_server.py:32-33`, the server instructions sent to clients: "Every tool here only reads data (no create/update/delete tools exist yet)."
- `README.md:18-22`: "Currently read-only, covering parts, ... project codes. Once write tools land, the `MCP_READ_ONLY` setting (see Configuration) will block them by default regardless of the calling user's permissions."
- `README.md:108-109`: "**Read Only** (`MCP_READ_ONLY`, default `True`): block all write actions via MCP, regardless of the calling user's permissions. A plugin-wide kill switch, independent of per-user roles."
- `AGENTS.md:15-17`: "any non-GET call raises `ToolError` unless an administrator has turned it off. This is a shared chokepoint - don't duplicate the check per-tool or bypass `call_view()`."
- `inventree_mcp/tools/stock_history.py:6-8`: "Neither can be created or edited via these tools even once write tools exist elsewhere in this plugin".
- `inventree_mcp/test_mcp.py:634-650`: tests that a write is blocked by default and reaches the real view when MCP_READ_ONLY is off.
- There is no CHANGELOG and no `docs/` directory. Issue #12 ("Documentation", open) is only a request to add the plugin to the InvenTree docs. No TODO or FIXME mentions writes.

3. Tool presentation
- Annotations: none. A grep of `inventree_mcp` for `ToolAnnotations`, `annotations=`, `title=` and `readOnlyHint` finds nothing outside `from __future__ import annotations`.
- Registration is a bare `@mcp.tool()` on an `MCPServer` (`mcp_server.py:24-26`), with no arguments.
- `output_schemas.apply()` (`output_schemas.py:145-147`) takes each tool from `mcp._tool_manager.get_tool(name)` and sets only `tool.fn_metadata.output_schema`. `tool_visibility.apply()` wraps `list_tools` (`tool_visibility.py:185-195`) to hide tools the caller's roles do not permit. `tool_logging.apply()` only logs calls.
- A client therefore sees, per tool: name, description (from the docstring), inputSchema, a derived outputSchema, and no annotations or title.
- A per-tool annotation hook would sit at `@mcp.tool()` or in a new post-registration pass like `output_schemas.apply()`. Upstream has none.
- Read-only status is conveyed only by the server-level `instructions` text (`mcp_server.py:32-33`) and the README. The Server Card (`server_card.py`) deliberately omits tools.

4. Upstream activity
- Repository: all 30 PRs are listed. Open: #49. Merged since 2026-08-27: #34/#35 (web link tool), #36-#39 (0.2.0/0.2.1 releases and docs), #40, #43 (csrf-exempt fix for issue #32), #44-#45 (0.2.2), #46 (well-known URL), #47 (0.3.0), #48. Earlier merged: #19 (permission-gated tool visibility), #27 (MCP v2 library). The history is large.
- No PR or issue mentions writes, mutations or create/update/delete tools.
- "Read-only" appears only in the code and README quoted above, not in any issue or PR title.
- Open issues: #42 "OIDC Support" ("authenticate directly via agent or use with an MCP middleware service layer"), #41 "Patch web_url into returned data", #24 "Allow plugins to extend available tools" (MCPMixin idea; matmair says plugins can use their own sub-plugin mixins), #13 and #12 (registry and docs).
- Closed issues: #33 (SEP-2127 well-known discovery), #32 (token auth 403), #28 (PyPI), #22 (tool logging), #21, #20, #17, #16 (hide tools outside permission scope), #8, #5, #3, #1.
- Issue #24 is the only extensibility item. It concerns letting other plugins add tools, not a write plan.
- PR #49 "Add OIDC bearer authentication" (author impuls42, opened 2026-09-25, 765 lines added, 0 deleted, closes #42). The branch `feature/oidc` is not on `origin`, so I read it via `refs/pull/49/head`. It is not merged.
  - It adds `inventree_mcp/oidc.py` (320 lines), `OIDCAuthentication` as one more DRF authentication class on `MCPView`, and `get_plugin_value` in `settings.py`.
  - It also changes `core.py` (+25), `mcp_transport.py` (+2: import and registration), README (+27), AGENTS.md (+2) and `test_mcp.py` (+379).
  - New settings: OIDC_ISSUER (empty means off), OIDC_AUDIENCE, OIDC_PROVIDER, optional OIDC_JWKS_URL and OIDC_CLIENT_USERS. Each can be overridden by an `INVENTREE_MCP_<KEY>` environment variable.
  - Token validation: `Authorization: Bearer <jwt>`, signature checked against the provider's JWKS (default: `jwks_uri` from `<issuer>/.well-known/openid-configuration`, https only), plus `iss`, `aud`, `exp` and `iat`. Asymmetric algorithms only (RS/PS/ES/EdDSA). `sub` is mapped to the user whose allauth SocialAccount for that provider has that uid.
  - It refuses machine tokens (`sub == azp`) unless mapped in OIDC_CLIENT_USERS, and it fails closed if the audience or provider is missing. Opaque InvenTree OAuth2 tokens still go to `ExtendedOAuth2Authentication`.
  - It registers no client and serves no discovery or protected-resource metadata. It only consumes the provider's discovery document.
  - It touches no tools and leaves `MCP_READ_ONLY` and `proxy.py` unchanged.
  - Review state: matmair says "the general approach looks sound; there are some SSRF vectors I would like to address but before that I would like to question if this really belongs in a plugin or in core". impuls42 offered to move it to core. Copilot left about 7 comments (cache locking, https enforcement, log level, over-broad exception suppression).
  - The PR description says stock changes made over MCP are attributed to the actual person. impuls42 runs this against "a fork of the older community plugin", so that setup is not upstream's read-only plugin. This is the author's own claim; I did not verify it.

5. The stdio question
- No stdio server or CLI exists in any tag.
  - Tags 0.1.0, 0.2.0, 0.2.1, 0.2.2 and 0.3.0 have no `__main__.py`, no `[project.scripts]` or console_scripts, and no `mcp.run(...)` or stdio code in `inventree_mcp/`.
  - `pyproject.toml` has only the `inventree_plugins` entry point (`pyproject.toml:36-37`: `InvenTreeMCP = "inventree_mcp.core:InvenTreeMCP"`).
  - The only transport is Streamable HTTP via a Django view (`mcp_transport.py`), served at `/plugin/inventree-mcp/mcp/`.
- What a stdio-only client was told to do (`README.md:67-70` on main; 0.1.0 uses the same wording with `"mcp-remote"` unpinned and no `@latest`): "For a client that only supports local (stdio) servers, bridge it with [`mcp-remote`](https://www.npmjs.com/package/mcp-remote)". It then gives an `npx -y mcp-remote@latest https://<your-inventree-server>/plugin/inventree-mcp/mcp/ --header "Authorization: Token <your-api-token>"` config (`README.md:72-87`). A Windows `npx.cmd` note and a `NODE_TLS_REJECT_UNAUTHORIZED=0` env note for self-signed certificates follow (`README.md:89-100`).

Open questions
- Whether upstream intends to add write tools is not stated anywhere I could find. The scaffolding implies it, but no issue, PR or roadmap text confirms it.
- The outcome of PR #49 is open: whether it lands in the plugin or moves to core, and whether the SSRF concerns are resolved.
- PR #49 was read only as a git ref, not as a merged state.

Implication for a fork that wants write tools: upstream has already built the plumbing. A fork would add tools that call `call_view()` with POST/PATCH/DELETE, and they would be blocked by default until MCP_READ_ONLY is switched off. Upstream has no annotation convention to follow, so a fork should define its own `readOnlyHint`/`destructiveHint` marking at registration. It should also change the `mcp_server.py:32-33` instructions text and the README claim, which would otherwise be false. The fork can stay close to upstream: auth, the read-only switch and the proxy are shared, and only the tool modules and annotations diverge. Upstream's changes to the proxy and settings, including PR #49's `settings.py` addition, are the likeliest merge conflicts.

Scratch copy of origin/main: /tmp/claude-2017/-home-alex-lucaci-Projects-inventree/bc5a60e8-e864-4ae5-8ca1-913eb8bb7be9/scratchpad/agent-upstream/main. I also created a local ref `refs/scratch/pr49` in the clone's `.git` to read the PR; it can be deleted with `git update-ref -d refs/scratch/pr49`.

## 2. Community InvenTree MCP servers

Seven repositories examined, plus one extra found by search. 28 of the 36 tools in the table below were counted from source; the other counts are inferred from docstrings or the shipped manifest. Nothing was run, modified or installed. Clones are in `/tmp/claude-2017/-home-alex-lucaci-Projects-inventree/bc5a60e8-e864-4ae5-8ca1-913eb8bb7be9/scratchpad/agent-community/` (`<owner>__<repo>`). All paths below are relative to each clone.

The second GitHub search ("inventree mcp server") returned nothing.

## 1. syntaxerr66/inventree-mcp
- **Identity:** Go (go-sdk), stdio only. Token from `INVENTREE_URL` / `INVENTREE_TOKEN` in env. 4 stars, last commit 2026-02-25, MIT (README). One integration test file, `internal/tools/tools_integration_test.go`.
- **Tools:** 26 in total, 15 of them writes.
  - Parts: `create_part` (POST /api/part/), `update_part` (PATCH /api/part/<id>/), `delete_part` (PATCH `active:false`, then DELETE /api/part/<id>/), `set_part_image` (PATCH).
  - Stock: `add_stock` (POST /api/stock/), `stock_add_quantity` (POST /api/stock/add/), `stock_remove_quantity` (POST /api/stock/remove/), `stock_transfer` (POST /api/stock/transfer/), `delete_stock_item` (DELETE /api/stock/<id>/).
  - Locations and categories: `create_`, `update_` and `delete_stock_location` (/api/stock/location/); `create_`, `update_` and `delete_part_category` (/api/part/category/).
- **Presentation:**
  - Create, add and transfer tools set `destructiveHint:false`. Deletes and `stock_remove_quantity` set `destructiveHint:true`. `search_part_images` sets `readOnlyHint`.
  - `update_*` and `set_part_image` have no annotations, and neither do the read tools. No `title` is set, and there is no gating.
  - Example description: "Create a new stock location. Locations can be nested ... Always search for existing locations first to avoid duplicates."
  - `create_part` embeds a numbered workflow in its description: search for duplicates, then list categories, and so on.
  - Errors are mapped to short strings by `internal/client/client.go`: 401 means "check INVENTREE_TOKEN", 403 means "permission denied", and other bodies are truncated to 500 characters. Token text is redacted.
- **Notable:**
  - Stock adjustments use the native `items:[{pk,quantity}]` batch shape.
  - `internal/coerce/coerce.go` is middleware that turns "32" into 32, because some clients send integers as strings.
  - `delete_part` hides InvenTree's deactivate-before-delete rule from the model.
  - There is no `count` operation.

## 2. syntaxerr66/inventree-mcp-plugin
- **Identity:** Python, runs inside InvenTree as a plugin and uses the Django ORM. Streamable HTTP, stateless (django-mcp-server). Auth is InvenTree DRF token, session or basic (`views.py`). 0 stars, last commit 2026-04-01, MIT, no tests.
- **Tools:** 36 in total, 24 of them writes.
  - Parts and categories: `create_part`, `update_part`, `delete_part`, `set_part_image`, and `create_`, `update_`, `delete_part_category`.
  - Stock: `add_stock`, `stock_add_quantity`, `stock_remove_quantity`, `stock_transfer`, `delete_stock_item`.
  - Locations: `create_`, `update_`, `delete_stock_location`.
  - Parameters and location types: `create_parameter_template`, `delete_parameter_template`, `set_part_parameter`, `bulk_set_part_parameters`, `delete_part_parameter`, `set_category_parameter`, `delete_category_parameter`, `create_location_type`, `delete_location_type`.
  - These call model methods directly, for example `add_stock`, `take_stock` and `move`, rather than REST.
- **Presentation:**
  - No MCP annotations. Descriptions are docstrings; `delete_stock_item` reads "Delete a stock item permanently."
  - Every tool starts with `check_permission(role, 'add'|'change'|'delete'|'view')` from `permissions.py`, which calls InvenTree's `check_user_role`. A denial returns a JSON `{"error": ...}`.
  - The permission check fails open if `check_user_role` cannot be imported.
  - `mcp_server.py` carries server-level `instructions`: search before create, ID 0 means "no filter", use the bulk tool for many parameters.
- **Notable:**
  - `stock_add_quantity`, `stock_remove_quantity` and `stock_transfer` take `items:[{pk,quantity}]`. They apply items one at a time, silently skip zero quantities, and abort at the first error without rolling back earlier items.
  - `bulk_set_part_parameters` runs in one `transaction.atomic()` and returns per-entry errors.
  - Compact serializers and tool consolidation were added to save context (latest commit).

## 3. munin92/inventree-mcp
- **Identity:** Python (fastmcp 4), Streamable HTTP via uvicorn (`inventree_mcp/server.py`).
  - Auth to InvenTree has three modes, in this order: an `X-Inventree-Token` header per request, a verified OIDC JWT mapped to an InvenTree user through a remote-login header, or a shared env token.
  - With OIDC on, it never falls back silently to the shared token.
  - 0 stars, last commit 2026-10-15, MIT per `pyproject.toml` (no LICENSE file). Has pytest + respx tests in `tests/`.
- **Tools:** 12 tools (`part`, `stock`, `purchase_order`, `sales_order`, `build_order`, `return_order`, `company`, `attachment`, `barcode`, `label`, `report`, `system`). Each takes an `operation` string, so there are well over 100 operations. Only `system` is read-only.
- **Write operations**, from the `match` statements in `inventree_mcp/tools/*.py`:
  - `stock`: create, update, delete, `transfer` (POST /api/stock/transfer/), `count` (POST /api/stock/<pk>/count/), `add` and `remove` (POST /api/stock/add|remove/), `merge`, `location_*`.
  - `part`: create, update, delete, category CRUD, `bom_add`, `bom_remove`, `bom_validate`, `parameter_set`, `parameter_delete`.
  - Orders: PO `issue` / `receive` / `complete` / `cancel` and `line_add` / `line_update`; SO `issue` / `ship` / `complete` / `cancel` and `shipment_create`; build `issue` / `complete` / `cancel` / `auto-allocate`; return orders likewise.
  - Others: `company` create / update / delete plus contacts and addresses; `attachment` delete; `barcode` link / unlink; `label` and `report` print.
- **Presentation:**
  - No annotations, no gating, no `title`. The server instructions are only "Each tool uses an 'operation' parameter".
  - The `Operations:` list is the whole contract. Example: "stock: Stock item & location management."
  - A delete or order cancel looks no different to a client than a list.
  - Errors use `resp.raise_for_status()`, so the InvenTree validation body is not passed to the model.
- **Notable:**
  - A single `data` dict is passed through unvalidated.
  - `add` and `remove` send `{**data, items:[{pk}]}`, which puts `quantity` at the top level instead of inside each item. This looks wrong against InvenTree's adjust serializers; verify before copying.
  - `count` uses `/api/stock/<pk>/count/`, which is not a documented route.
  - The adjustment vocabulary is the richest of the group: add, remove, count, transfer, merge.

## 4. davidvanlaatum/inventree-mcp
- **Identity:** Go (go-sdk v1.7), by far the largest at about 111k lines including tests.
  - Transports are stdio (env `INVENTREE_TOKEN`) and Streamable HTTP. HTTP uses MCP-owned OAuth 2.1: PKCE, CIMD `private_key_jwt`, encrypted token envelopes. A per-user InvenTree token is minted at setup.
  - HTTP production mode rejects raw tokens. There is also a stateless bearer "bootstrap" endpoint.
  - 0 stars, last commit 2026-10-14, 50 commits. Large unit tests plus a Testcontainers integration suite against real InvenTree 1.5.x. No LICENSE file found.
- **Tools:** 181 per `docs/tool-manifest.json`: 71 read-only and 110 with a write class (42 `write`, 26 `operational`, 25 `destructive`+`operational`/`destructive`, the rest upload-scoped).
- **Write tools, by group:**
  - Stock: `create_stock_item`, `adjust_stock_quantity` (POST /api/stock/add|remove/), `stocktake_adjustment` (/api/stock/count/), `transfer_stock_item` and `bulk_transfer_stock_items` (/api/stock/transfer/), `set_stock_status` and `bulk_set_stock_status` (/api/stock/change_status/), `deplete_stock_item`, `install_stock_item`, `uninstall_stock_item`, `assign_stock_serial`, `set_stock_serial`, `set_stock_delete_on_deplete`, `update_stock_item_provenance`, `restructure_stock_location`, `generate_stocktake`.
  - Purchase orders: `create_purchase_order`, `create_purchase_order_with_lines`, `issue_`, `hold_`, `resume_`, `complete_`, `cancel_purchase_order`, `receive_purchase_order_items` (POST /api/order/po/<id>/receive/), plus line, extra-line, project-code, contact and address tools.
  - Parts and catalog: part, category, parameter, supplier-part, manufacturer-part, price-break, relation, BOM-less catalog CRUD, and `bulk_update_*` (parts, companies, stock metadata and others).
  - Attachments, images, barcodes: `upload_attachment`, `set_primary_image`, `assign_barcode`.
  - Deletes: `delete_part`, `delete_part_category`, `delete_stock_location` and others.
- **Presentation:**
  - Every tool has title, icon and all four annotations, driven from one class table in `internal/tools/annotations.go`. Reads are `readOnly+idempotent`. Writes are `readOnly:false` with `destructive` and `idempotent` set per tool (for example `update_company` is idempotent, `delete_part` is destructive).
  - Descriptions start like "Plans or confirms one relative stock quantity adjustment with an audit reason." (`adjust_stock_quantity`) and "Previews and explicitly deletes one unreferenced ordinary part with read-back verification." (`delete_part`).
  - Gating: five OAuth scopes (`inventree.read`, `.write`, `.upload`, `.operational`, `.destructive`), additive, checked per tool by `GuardTool` in `internal/tools/auth_guard.go`. The scopes are also advertised in `_meta.securitySchemes`.
  - Gating: HTTP refuses to start with write tools unless per-tool OAuth is on (`internal/server/server.go`). Write tools register only when `EnableWriteTools` is set, but I found no operator flag that turns it off, since `cmd/inventree-mcp/main.go` hard-codes true.
  - Destructive and stock tools take `dry_run`, `confirm` and `plan_hash`.
  - Errors: `safeToolError` (`internal/tools/validation_errors.go`) converts InvenTree 400s into `field: message` lists. Network errors become "upstream request failed", and each call is classified into a closed outcome set for logs.
- **Notable:**
  - Stock adjustments are one item at a time, and every one needs a nonblank `reason`.
  - The vocabulary is `delta` (relative) versus `observed_quantity` (absolute stocktake), kept as separate tools.
  - `internal/tools/stock_adjustment_tools.go` implements a stateful plan store:
    - `dry_run` returns a plan bound to the item's current state.
    - It issues an opaque token valid 5 minutes and single use, bound to the caller and the action.
    - It rejects stale state, serialized stock, and anything that would silently trigger delete-on-deplete.
  - A bulk tool exists for most mutating record types, with a cap on items.
  - Several tools do a read-back after writing (and `deplete_stock_item` verifies not-found), and return `partial_failure` with a `recovery_plan` after a partial failure.
  - Client-side safety defaults are forced: `auto_pricing=false` and `merge_items=false` on PO lines.

## 5. yoo2001818/inventree-mcp
- **Identity:** TypeScript (`@modelcontextprotocol/sdk`), Streamable HTTP at `/mcp`. OAuth 2.1 + PKCE + DCR (built for ChatGPT). The user enters their InvenTree token during authorization and it is stored AES-256-GCM encrypted (`src/oauth.ts`, `src/crypto.ts`). 0 stars, last commit 2026-10-05, no LICENSE found. Tests in `test/` (OAuth, HTTP, domain).
- **Tools:** 27 by default, 28 with `ENABLE_RAW_WRITE`. The writes:
  - `create_inventory_plan`, `discard_inventory_plan`, `commit_inventory_plan` (the only tool that actually executes), `prepare_part_image_upload`, and `inventree_write` (optional escape hatch for POST/PATCH/PUT only; no DELETE).
  - The 32 typed actions live inside the plan, not as separate tools: `receive_stock` (POST /api/stock/ or /add/), `consume_stock` (/api/stock/remove/), `move_stock` (/api/stock/transfer/), `count_stock` (/api/stock/count/), `set_stock_status` (/change_status/), `receive_purchase_order` (/api/order/po/<id>/receive/), part, category, company, supplier-part and parameter creates and updates, `issue_` / `hold_` / `cancel_` / `complete_purchase_order`.
  - There is no delete action at all.
- **Presentation:**
  - All tools have a title and annotations. Reads are `readOnly:true` (a few omit `destructiveHint`). `create_inventory_plan` and `discard_` use `mutationAnnotations()`: `destructive:false`, `idempotent:true`. `commit_inventory_plan` is `destructive:true` (`src/write/shared.ts`).
  - Descriptions: "Validate and stage an entire ordered inventory workflow in one call..." and "After the user's single final confirmation, revalidate and execute all staged steps serially."
  - Gating: OAuth scopes `inventree.read` / `inventree.write` checked per call. The raw write tool is off by default and needs a literal `"I confirm this InvenTree mutation"` argument.
  - Errors go through `safely()` / `errorResult` and `DomainError` with `status:"conflict"` and `conflict_type` (`src/domainErrors.ts`).
- **Notable:**
  - The write flow is plan, review, then commit. The plan creator needs a caller-chosen `operation_id` idempotency key. The same key with a different plan returns a conflict, and the same key with the same plan returns the existing plan.
  - A plan's steps are validated at creation, and `commit` re-reads captured state digests. It fails with `stale_inventory` if anything changed. Plans last 30 minutes and commits are retry-safe, and aliases connect later steps to earlier outputs.
  - Duplicate checks run before creates.
  - The raw-write confirmation is a schema literal; per README and the `inventree_write` description, it is only enforced in the schema.
  - Stock vocabulary is receive / consume / move / count, not add / remove, and `receive_stock` records the supplier part.
  - Image bytes go through an expiring upload URL, never through the LLM context.

## 6. eljefedelrodeodeljefe/inventree-mcp-plugin
- **Identity:** Python, a Django plugin using the ORM (FastMCP). Streamable HTTP, stateless, at `/plugin/inventree-mcp/mcp/`. Auth is the InvenTree token, bearer, basic or session, with a `REQUIRE_AUTH` plugin setting. 5 stars, last commit 2026-02-26, MIT, mocked unit tests in `tests/` plus an integration script.
- **Tools:** 28 in total, 5 of them writes (modules under `inventree_mcp_plugin/tools/simple/`).
  - `create_part` (ORM create)
  - `update_part` (allowlist of 6 fields)
  - `delete_parts` (batch; deactivates, then deletes; skips locked parts or parts used in assemblies unless `delete_from_assemblies=true`; reports skipped parts)
  - `adjust_stock` (positive is `add_stock`, negative is `take_stock`)
  - `transfer_stock` (`item.move`)
- **Presentation:** Docstring descriptions with "Args:" blocks, e.g. "Adjust the quantity of a stock item (add or remove stock)." No annotations, no gating. The README says outright that role checks are bypassed, so any authenticated user has write access.
- **Notable:**
  - `delete_parts` documents the cascade (stock, builds and supplier parts are deleted) in its docstring.
  - A zero-quantity `adjust_stock` silently does nothing.
  - `user=None` means stock history is not attributed to a user.
  - `delete_parts` is the only tool that lists the cascade in its description.

## 7. hvkshetry/inventree-mcp (consolidated into puran-water/PuranOS-public)
- **Identity:** Python (official `mcp` FastMCP), stdio by default, SSE with the `sse` argument. Token in env `INVENTREE_TOKEN`. Wraps the official `inventree-python` library (`client.py`). 1 star, last commit 2026-03-10, MIT, no tests.
- **Tools:** 12 tools, advertised as 117 operations: `part`, `stock`, `build_order`, `purchase_order`, `sales_order`, `return_order`, `company`, `barcode`, `label`, `report`, `attachment`, `system`. All but `system` are mixed read/write.
  - Stock writes: `create`, `update`, `transfer`, `count`, `add`, `remove`, `create_location`, `upload_test_result`. These map to `inventree-python` `transferStock`, `countStock`, `addStock` and `removeStock`, which post to the stock endpoints.
  - Other writes by tool: `part` create / update / delete; `build_order` create / update / allocate / complete / cancel; `purchase_order` issue / receive / complete / cancel / hold and lines; `sales_order` and `return_order` similar, plus shipments; `company` create / update / delete; `barcode` assign / unassign; `attachment` upload / delete.
- **Presentation:**
  - All tools carry `title` plus all four hints inside the annotations dict. `destructiveHint` is true only for `part`, `company` and `attachment`. `stock` and the order tools are marked non-destructive although they cancel orders or remove stock. Every write tool sets `openWorldHint:true`. `system` is `readOnly`.
  - There is no gating. Errors from `_error` in `server.py` return `{"error", "hint"}` based on whether the message contains 404, 403, 400 or connect.
- **PuranOS:** only docs reference the InvenTree server, so the consolidated code is not easy to find. Not pursued.

## Extra from search: echo313unfolding/mcelroy-inventory
- Not an InvenTree MCP server as such. It is a shop-specific tool (UGSI McElroy fleet) with a hand-rolled JSON-RPC server over stdio or `--http` in `mcp_server/server.py`. It uses a username/password to get a token. Last commit 2026-10-13, 1 star, no tests, "internal tool" in the README.
- 11 tools, 2 writing: `adjust_stock` and `import_csv`.
  - `adjust_stock` takes a part search string, a location name and a signed quantity. It PATCHes quantity directly (/api/stock/<pk>/) or POSTs a new stock item; it does not use the stock add / remove endpoints.
  - `import_csv` defaults to `dry_run=true` and writes only when write mode is on.
- Gate: `enable_write_operations`, off by default (env `UGSI_ENABLE_WRITES`). The LLM can switch it on itself, because `set_config` accepts that key. That makes the gate ineffective.
- No annotations.

## Comparison

| repository | transport | auth | tools total | write tools | annotations | write gating |
|---|---|---|---|---|---|---|
| syntaxerr66/inventree-mcp | stdio | env token | 26 | 15 | partial (destructive on create/add/delete/remove only) | none |
| syntaxerr66/inventree-mcp-plugin | Streamable HTTP | InvenTree token/session | 36 | 24 | none | InvenTree role check per tool (fails open on import error) |
| munin92/inventree-mcp | Streamable HTTP | env token, per-request header, or OIDC JWT | 12 (100+ ops) | most ops | none | none |
| davidvanlaatum/inventree-mcp | stdio + Streamable HTTP | env token (stdio), OAuth 2.1 (HTTP) | 181 | 110 | full (4 hints + title + icon + scopes metadata) | 5 OAuth scopes, dry_run/confirm/plan_hash, write tools registered conditionally |
| yoo2001818/inventree-mcp | Streamable HTTP | OAuth 2.1, token stored encrypted | 27 (+1 optional) | 5 (+32 typed plan actions) | full (title + 4 hints) | plan/review/commit, `operation_id`, raw write off by default |
| eljefedelrodeodeljefe/...-plugin | Streamable HTTP | InvenTree token/session | 28 | 5 | none | none (README says roles bypassed) |
| hvkshetry/inventree-mcp | stdio + SSE | env token | 12 (117 ops) | most ops | title + 4 hints per tool (coarse) | none |
| mcelroy-inventory (extra) | stdio / HTTP | username/password | 11 | 2 | none | config flag the LLM can change itself |

## What is worth borrowing
1. **Server-held plan with state-bound confirmation token.** davidvanlaatum, `internal/tools/stock_adjustment_tools.go` (`stockPlanStore`). `dry_run` returns a plan; `confirm:true` plus `plan_hash` executes it. The token is single use, valid 5 minutes, and bound to caller, action and item state.
2. **Plan, review, commit with stale-state digests and idempotency.** yoo2001818, `src/mutationPlans.ts` and `src/write/planTools.ts`. One caller-chosen `operation_id`, one human confirmation, and a failure with `stale_inventory` if upstream data changed.
3. **Scope classes tied to hints.** davidvanlaatum, `internal/tools/annotations.go` plus `auth_guard.go`. One table gives read / write / operational / destructive classes, accurate annotations, and per-tool OAuth scopes; a test fails if a tool is misclassified. `docs/tool-manifest.json` is generated from it.
4. **Native stock vocabulary in separate tools, with a required audit reason.** davidvanlaatum separates `adjust_stock_quantity` (delta), `stocktake_adjustment` (absolute) and `transfer_stock_item`, and refuses no-ops, serialized stock and implicit depletion. These map to /api/stock/add|remove|count|transfer/ directly (munin92 also has `merge`).
5. **InvenTree's own role check per tool.** syntaxerr66 plugin, `inventree_mcp_plugin/permissions.py`. Map each write to a role and action (`stock` / `change`, `part` / `delete`) and return a JSON error. It should fail closed, not open.
6. **Per-user identity instead of a shared token, with no silent fallback.** munin92, `inventree_mcp/server.py` `get_client()` and `client.py` (the remote-login user replaces the token, never accompanies it).
7. **Batch with skip report and atomic bulk upsert.** eljefede `delete_parts` (`tools/combinatory/parts.py`: `deleted` / `skipped` with reasons, cascade documented, `delete_from_assemblies=false` default) and syntaxerr66 plugin `bulk_set_part_parameters` (one transaction, per-entry errors).
8. **Error shaping.** davidvanlaatum `safeToolError` turns 400s into `field: message` text and hides everything else. Use that instead of hvkshetry's substring-matched hints or raising raw HTTP errors as munin92 does.

**Anti-patterns to avoid:**
- Giving the model a tool that changes its own write gate (mcelroy).
- Marking order cancellation or stock removal as non-destructive (hvkshetry).
- Passing raw `data` dicts through to the API with no validation, one `operation` string selecting everything (munin92, hvkshetry).

## 3. Summary and what it means for this fork

- Upstream has zero write tools and never shipped a stdio server; stdio-only clients were told to bridge with `npx mcp-remote` and an API token, so the "stdio executable" exposed exactly the same 56 read-only tools. Upstream built the plumbing for writes (`MCP_READ_ONLY`, the non-GET branch of `proxy.call_view()`, `_DETAIL_ACTIONS`) and says "once write tools land", but no issue or PR proposes any.
- Upstream tools carry no MCP annotations (`readOnlyHint` etc.) and no title; ours inherit that. Adding `readOnlyHint: true` to every current tool is a cheap, correct improvement.
- Community servers do ship writes, from unguarded ORM calls (eljefedelrodeodeljefe) to a planned, confirmed, scope-gated design (davidvanlaatum: 181 tools, 110 writes, four hints and five OAuth scopes per tool; yoo2001818: plan/review/commit with idempotency keys and stale-state detection).
- If this fork adds write tools, the design that fits what we already have: one tool per operation using InvenTree's own stock endpoints (`add`, `remove`, `count`, `transfer`), each registered with accurate annotations, gated by `MCP_READ_ONLY`, by the MCP add/change/delete roles (already OAuth scopes `r:add|change|delete:<role>`) and by core's role check, with a `dry_run`/confirm step for destructive operations and InvenTree's 400 bodies shaped into field messages.
