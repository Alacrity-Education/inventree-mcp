# WP4: GUI (plugin pages first, core sidebar injection as the upstream target)

Read first: `docs/plans/00-overview.md`, the master plan "WP4" (both options and why B ships first), `docs/plans/WP3-report.md` (the API this UI consumes), and:
- InvenTree docs: `docs/docs/plugins/frontend.md`, `docs/docs/plugins/mixins/ui.md`, `docs/docs/plugins/creator.md`.
- Core sample: `src/backend/InvenTree/plugin/samples/integration/user_interface_sample.py` (`get_ui_routes`, `get_ui_navigation_items`, `get_ui_panels`, `plugin_static_file`) and `plugin/samples/static/plugins/sampleui/sample_route.js`.
- Frontend internals that render plugin code: `src/frontend/src/components/plugins/{PluginRoutes.tsx,RemoteComponent.tsx,PluginContext.tsx}`, `src/frontend/src/hooks/UseRemotePlugin.tsx`, `src/frontend/src/components/nav/Header.tsx` (navigation items), `src/frontend/lib/types/Plugins.tsx` (`InvenTreePluginContext`), `src/frontend/lib/index.ts` (what `@inventreedb/ui` exports).
- The look to reproduce: `src/frontend/src/components/items/RoleTable.tsx` and `src/frontend/src/tables/settings/GroupTable.tsx` (`GroupDrawer`).
- Build template: `/home/alex.lucaci/Projects/inventree/vendor/plugin-creator/plugin_creator/template/{{ cookiecutter.plugin_name }}/frontend/` (`vite.config.ts`, `package.json`, `src/Panel.tsx`): externalised libraries, two output sets (plain and hashed) into the package's `static/` directory.

Facts (verified): plugin routes mount at `/web/plugin/<plugin_name>/<path>` and the exported function receives `InvenTreePluginContext` and returns a React element; `window.React`, `ReactDOM`, `MantineCore`, `MantineNotifications`, `LinguiCore`, `LinguiReact` are provided by the host and must be externalised; `@inventreedb/ui` 1.5.0 is on npm; node 26 and npm 12 are installed on this machine. The core group drawer has no injection point, so option A needs a core PR (prepared in WP5 as a patch); this WP ships option B and the panel entry point A will use.

## Deliverables

### Frontend project (`frontend/`)

Copy the plugin creator's frontend setup (vite, externals, biome, tsconfig; no lingui/translations in this round) and adapt: entries `src/Groups.tsx`, `src/Connections.tsx`, `src/Panel.tsx`; output into `inventree_mcp/static/` (plain and hashed names, like the template). `npm run build` writes into `inventree_mcp/static/`, which stays git-ignored (upstream convention, `inventree_mcp/.gitignore`): the bundles are build output, not source. Consequences: `dev/up.sh` runs `npm ci && npm run build` when `inventree_mcp/static/Groups.js` is missing or older than `frontend/src` (node is on the host), CI builds the frontend before `python -m build`, and the PyPI/wheel build includes `static/` through `MANIFEST.in`. Document `npm ci && npm run build` in `frontend/README.md` and `AGENTS.md`. `.gitignore`: `frontend/node_modules`, `frontend/dist`. Pin `@inventreedb/ui` to the InvenTree minor in use (1.5.x), and call `checkPluginVersion(context)` at every entry point.

Shared code (`src/lib/`): `api.ts` (typed wrappers over `context.api` for the plugin API from WP3 and core `user/group/`, `user/ruleset/`), `RoleGrid.tsx` (one component for both tables: props `rows`, `editable`, `onSave(rows)`, `shadowRows?` to flag cells granted in MCP but not interactively, `title`), `useGroups.ts`. Styling with Mantine only, same checkbox grid and Save/Reset buttons as core's `RoleTable`.

### Pages

1. **MCP Access (admin)** – route `groups` → `/web/plugin/inventree-mcp/groups`, nav item "MCP Access" (icon `ti:plug-connected`), both only when `request.user.is_staff`. Layout: group list (left, from core `user/group/`), selected group (right) with two accordions: "Roles" (interactive, editable, saves to core `user/ruleset/<pk>/` PATCH, exactly what the core drawer does) and "MCP Roles" (editable, saves to plugin `api/rulesets/<pk>/` PATCH), plus buttons "Copy from roles" and "Clear" (plugin API), the shadow flag on cells where MCP grants more than interactive (tooltip: "No effect: MCP access never exceeds interactive access"), a "Members" table (username, effective MCP summary from `api/effective/<pk>/` loaded lazily per row, connection count) and a "Revoke MCP tokens for this group" button (new endpoint, see backend).
2. **MCP (every user)** – route `connections` → `/web/plugin/inventree-mcp/connections`, nav item "MCP" for everyone. Cards: status (`api/status/`: enabled, may_connect with reason, membership checked_at / blocked), "Your MCP permissions" (`RoleGrid` read-only from `api/effective/<me>/`, `effective` block), "Connections" (table from `api/connections/`: client, trusted badge, created, expires, scopes, revoke button with confirmation), "Connect" (MCP URL with copy button, `claude mcp add --transport http inventree <url>`, three-line claude.ai instructions).
3. **Group panel for option A** – export `renderMCPRolesPanel(context)` from `src/Panel.tsx` rendering "MCP Roles" for `context.id` (group pk); backend `get_ui_panels` returns it only when `context.get("target_model") == "group"` and the user is staff. Core does not request group panels today; this is the entry point the core PR (WP5) will light up. Verify it renders by temporarily calling the panel function from the groups page in dev, then remove that call.

### Backend (`core.py`, `auth/api.py`, `auth/urls.py`)

- `UserInterfaceMixin` on the plugin; `get_ui_routes`, `get_ui_navigation_items`, `get_ui_panels` as above, sources via `self.plugin_static_file("Groups.js:renderGroupsPage")` etc. (hashed file resolved by core).
- New endpoint `POST api/rulesets/revoke/` body `{"group": <pk>}` → `membership.revoke_all_mcp_tokens` for each member (offloaded above 20 members), staff only; returns `{"users": n, "tokens": m}`.
- `README.md`: screenshots are not required; describe both pages. `AGENTS.md`: frontend build and the rule that `inventree_mcp/static/` is generated, never edited by hand.

### Dev environment support

`dev/README.md` gains: how to rebuild the frontend and refresh the static files in the running container (find the invoke task that re-collects plugin static files, or restart the server container; document the one that works), and how to use InvenTree's `plugin_dev` config block with `npm run dev` for hot reload (optional, only if it works within an hour of trying).

## Tests

- Backend: `tests/test_ui.py`: `get_ui_routes`/`get_ui_navigation_items` return the expected entries for staff and nothing route-wise for non-staff where applicable; `get_ui_panels` returns the group panel only for `target_model == "group"` and staff; revoke endpoint permissions and counts.
- Frontend: `npm run lint` clean; a minimal `vitest` smoke test for `RoleGrid` (renders rows, toggles a cell, calls `onSave`) if it can be added without fighting the externals setup; otherwise document why not.
- Manual proof in the dev environment: load `/web/plugin/inventree-mcp/groups` as admin, change an MCP flag, save, confirm via `GET api/rulesets/?group=<pk>`; load `/web/plugin/inventree-mcp/connections` as `e2e-member` after an OAuth connection and revoke it; confirm the refresh grant then fails. Use a headless browser (`playwright` via `npx playwright`, chromium) for screenshots saved under `docs/plans/screenshots/` if feasible; otherwise describe what you verified through the API and the served JS.

## Acceptance

Both pages reachable and functional in the dev environment; nav items present; built assets committed; tests green; report `docs/plans/WP4-report.md`; one commit `WP4: MCP permissions and connections pages`.
