# T4: documentation, live e2e, beta configuration (Sonnet)

Read first: `docs/plans/tools/00-overview.md`, the three reports T1..T3 and `T23-review.md`, `README.md`,
`docs/OPERATIONS.md`, `AGENTS.md`, `dev/README.md`, `dev/oauth_smoke.py` and `dev/permissions_e2e.py` (the scripted
MCP client patterns to reuse: API token via /api/user/token/, JSON-RPC bodies, SSO login helper), and
`deploy/beta/configure_inventree.py`.

## Deliverables

1. `README.md`: replace every description of the old tools with the new tool reference (the 26 tools grouped by
   resource: one line each with the inputs and what it does; the workflow guidance from the Go README/CLAUDE.md: search
   before create, category hierarchy, descriptions from part numbers), the write-tool requirements (`MCP_READ_ONLY` off,
   MCP add/change/delete roles on the group, the user's interactive roles, `INVENTREE_DOWNLOAD_FROM_URL` for images,
   `GOOGLE_API_KEY`/`GOOGLE_CSE_ID` for image search), remove `describe_filters`/`make_web_link` mentions, keep the OAuth
   and permission sections accurate. Credit the tool design to syntaxerr66/inventree-mcp with a link.
2. `docs/OPERATIONS.md`: settings table rows for the two Google settings and the changed `MCP_READ_ONLY` meaning; a
   failure-modes row for "write refused" (read-only, missing MCP role, missing interactive role, download-from-URL off);
   section 7 note that tool-call logging now includes write payloads when `MCP_LOG_TOOL_CALLS` is on.
3. `AGENTS.md`: the new module layout, the `_common.py` rules (tool(), access(), listing(), compact(), clean()), the test
   files, and that the Go server is the reference for tool semantics.
4. `dev/tools_e2e.py`: a scripted end-to-end run against the dev stack (http://localhost:8080): obtain an API token
   (admin/admin), switch `MCP_READ_ONLY` off and grant the admin's group all MCP roles (through the plugin API or
   `dev/manage.sh`/`dev/shell.sh` like the other scripts do), then: search_part_categories -> create_part_category (under
   an existing one) -> create_part -> add_stock -> stock_add_quantity -> stock_remove_quantity -> create_stock_location ->
   stock_transfer -> get_stock (verify) -> delete_stock_item -> delete_part -> delete_part_category -> delete_stock_location,
   printing one line per step, restoring the settings in a `finally`, exit 1 on any failure. Also one negative step: with
   `MCP_READ_ONLY` back on, a write answers the read-only text. Document it in `dev/README.md` and `AGENTS.md`.
5. `deploy/beta/configure_inventree.py` and `.env.example`: a new `MCP_WRITE=1` switch (default 1 in `.env.example`,
   documented) that sets the plugin setting `MCP_READ_ONLY` to False and gives the groups in `MCP_GROUPS` all four MCP
   flags on part, part_category, stock and stock_location (keep view on everything else); `MCP_WRITE=0` keeps read-only.
   Do not run the beta yourself; the orchestrator does.
6. `docs/plans/tools/T4-report.md`: what changed, the e2e output (paste the step lines), commands run.

## Boundaries

Only the files above. No git. Lint `dev/*.py` with `uvx ruff@0.16.0 check --isolated dev/*.py`. The e2e must pass on the
dev stack before you report.
