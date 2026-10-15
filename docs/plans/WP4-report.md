# WP4 report: GUI backend wiring, dev support, live proof

Nothing was added to git. Files touched: `inventree_mcp/core.py`, `inventree_mcp/tests/test_ui.py` (new), `dev/up.sh`, `dev/README.md`, `dev/ui_smoke.py` (new), `frontend/src/lib/{api,summary}.ts`, `README.md` (new "GUI" section appended), `docs/plans/screenshots/` (4 PNGs), this report. The rebuilt bundles are in the git-ignored `inventree_mcp/static/`.

## Key finding: plugin pages need InvenTree newer than 1.5.7

The dev stack runs `inventree/inventree:1.5.7` (stable). Its `UserInterfaceMixin` has no `get_ui_routes` and its feature API answers `route` with "Invalid feature type" (routes arrived in core PR #12900, present in `inventree/inventree:latest`, API 556). On 1.5.7 the pages cannot be reached at all. Consequences handled:
- `core.UI_ROUTES_SUPPORTED = hasattr(UserInterfaceMixin, "get_ui_routes")`; the navigation items are only returned when it is true (otherwise the nav entries would link to nothing). Routes and panels need no gating (ignored / harmless on old hosts).
- The live proof was done on an isolated second stack on `latest` (postgres, redis, server, caddy on 127.0.0.1:8081, scratch directory, removed afterwards), not on the shared 1.5.7 dev stack. To use `latest` in the dev stack: `INVENTREE_TAG=latest` and `DEMO_DATASET_REF=WORKTREE` in `dev/.env`, then `dev/down.sh -v && dev/up.sh` (documented in `dev/README.md`). Not done to the shared stack because another agent was running tests on it.
- Decision for you: MIN_VERSION stays 1.5.0 (API and MCP work there); the GUI needs a release with #12900.

## What was built

1. `core.py`: `UserInterfaceMixin` in the bases. `get_ui_routes`: `groups` (staff, source `Groups.js:renderGroupsPage`) and `connections` (authenticated, `Connections.js:renderConnectionsPage`), as `{key, title, source, options: {path}}` (the sample's shape). `get_ui_navigation_items`: "MCP Access" (`ti:plug-connected`, staff, `plugin/inventree-mcp/groups`) and "MCP" (everyone, `.../connections`). `get_ui_panels`: "MCP Roles" (`Panel.js:renderMCPRolesPanel`) only for `context["target_model"] == "group"` and staff. `plugin_static_file(check_exists=True)` only logs an error for a missing file and never raises, but it still returns a URL to a nonexistent file; `_ui_source()` therefore checks `inventree_mcp/static/<file>` itself, logs a warning ("run npm ci && npm run build") and leaves the feature out, so a missing build cannot break the feature API (which serves every plugin).
2. `dev/up.sh`: step 1b builds the frontend (`npm ci && npm run build`) when `static/Groups.js` is missing or any file under `frontend/src`, `package.json` or `vite.config.ts` is newer, before any container starts. Static tree: the registry copies `inventree_mcp/static/` (including `assets/`) to `dev/data/static/plugins/inventree-mcp/` when it loads; on the `latest` stack after `restart inventree-server` `http://localhost:8081/static/plugins/inventree-mcp/Groups.js` answered 200; on the 1.5.7 stack `http://localhost:8080/static/plugins/inventree-mcp/{Groups,Connections,Panel}.js` answer 200 with files identical (md5) to the build, after the registry reloads of the test runs (I did not restart the shared stack). `dev/README.md` gained a "Frontend" section (rebuild, refresh, `latest` instructions, the playwright venv recipe). `plugin_dev` hot reload: not attempted (no `npm run dev` script in the frontend, and the stable stack has no routes to load into).
3. Frontend: `RoleFlags.label?` added; `roleMapToRows` and `summarizeRoles` use the API's `label`, `humanize()` stays as fallback. `npm run build`, `npm run lint` (clean after `biome --write` on one file), `npm test` (6 passed), `npm run check:bundles` (6 ok) all clean. No other change was needed: the pages worked live unmodified.
4. `tests/test_ui.py` (10 tests): methods for staff, non-staff, anonymous, non-group and missing target models, no-build behaviour (warning, no exception), navigation with and without route support; the feature API (`GET /api/plugins/ui/features/<type>/`, needs the global setting `ENABLE_PLUGINS_INTERFACE`, set in the test) for a staff session and a non-staff session (non-staff: no `groups` route, only "MCP", no group panel). On 1.5.7 the API tests assert empty routes/navigation (the capability flag), on `latest` the full lists. Both pass.
5. `dev/ui_smoke.py` (python + playwright; the host python is externally managed, so a venv: `python3 -m venv /tmp/pw && /tmp/pw/bin/pip install playwright && /tmp/pw/bin/playwright install chromium`; chromium installed fine) with `--base-url`.

## Live verification (isolated `latest` stack, http://localhost:8081, headless chromium)

Feature API as admin: two routes with hashed sources (`/static/plugins/inventree-mcp/Groups-DIzd31oa.js:renderGroupsPage`, ...) and two navigation items; the top bar shows "MCP Access" and "MCP". `dev/ui_smoke.py` output:
```
group 'MCP Users' pk=1: Parts delete = False
after save: Parts delete = True
restored
connections page: status card rendered
ui_smoke: ok
```
It logs in as admin, opens `/web/plugin/inventree-mcp/groups`, selects "MCP Users", ticks Parts/Delete in the MCP Roles grid, presses Save (toast "MCP roles updated"), confirms through `GET /plugin/inventree-mcp/api/rulesets/?group=1` that it persisted, restores it, and opens `/connections` (status card with "MCP OAuth", "You can connect", the permissions grid with the API labels, "Connect" card). Screenshots: `docs/plans/screenshots/groups-1-selected.png`, `groups-2-edited.png`, `groups-3-saved.png` (shows the amber "no effect" marks on Parts View/Change/Delete because that group has no interactive Parts role), `connections.png`.

Not verified: the "revoke a connection as e2e-member after an OAuth connection and see the refresh grant fail" proof from the WP4 spec (the isolated stack has no Authentik/SSO and OAuth was disabled there; that path is covered by WP3's tests); the group panel in a browser (core requests no group panels); the pages on the 1.5.7 stack (impossible, see above).

## Tests and lint

- `uvx ruff@0.16.0 check --preview` / `format --preview --check` on `core.py`: clean; on `tests/test_ui.py` with `--isolated` (repo config excludes tests/): clean.
- `dev/test.sh inventree_mcp.tests.test_ui`: `Ran 10 tests ... OK`; same on the `latest` image (route branch).
- `dev/test.sh inventree_mcp` (full, at the end): `Ran 332 tests in 413.900s  OK (skipped=3)`.

## Known limitations

- GUI needs InvenTree with plugin routes (> 1.5.7); on 1.5.7 no pages or nav entries by design.
- The bundles must be built before packaging (`inventree_mcp/static/` is git-ignored); a missing build only logs a warning. `MANIFEST.in`/wheel inclusion of `static/` was not checked (outside the owned files).
- Static refresh in the dev stack needs a server restart (or a registry reload); the copy under `dev/data/static/` keeps stale hashed files.
- `npm audit` reports dev-dependency vulnerabilities (unchanged from the frontend round).
- The smoke script creates the "MCP Users" group when missing and issues a named API token (`ui-smoke-<timestamp>`) per run.
