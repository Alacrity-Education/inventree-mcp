# WP4 frontend report (build pipeline, shared components, pages)

Scope of this round: `frontend/`, build output in `inventree_mcp/static/`, `.gitignore`. No Python file and nothing under `dev/` was touched. Nothing was added to git.

## What was built

- `frontend/`: `package.json` (`@inventreedb/ui` pinned `~1.5.0`, React 19, Mantine 9.2 as in the host, Vite 7, vitest 3, biome 2.0.0), `package-lock.json`, `vite.config.ts` (externals react, react-dom, ReactDom, @lingui/core, @lingui/react, @mantine/core, @mantine/notifications; two output sets, plain `[name].js` and hashed `[name]-[hash].js`, both into `../inventree_mcp/static/`; entries Groups, Connections, Panel), `vitest.config.ts`, tsconfig*.json, `biome.json` (own root), `README.md`, `scripts/check-bundles.mjs`. No lingui/translations.
- `.gitignore`: `frontend/node_modules`, `frontend/dist` appended. `frontend/.gitignore` additionally ignores node_modules, dist, tsbuildinfo.
- `src/lib/api.ts`: typed wrappers over `context.api` (types RuleSet, Group, GroupDetail, Effective, Connection, McpStatus; functions listGroups, getGroup, patchCoreRuleSet, getCurrentUserPk, listMcpRuleSets, patchMcpRuleSet, copyRuleSets, clearRuleSets, revokeGroupTokens, getEffective, listConnections, revokeConnection, getStatus; `isNotWired` = HTTP 404).
- `src/lib/RoleGrid.tsx`: props `rows, editable, onSave(rows), shadowRows?, title?`. Same table, View/Change/Add/Delete column order, Save/Reset buttons, `*` edited marker and label-sorted rows as core's RoleTable (without mutating state). `onSave` receives only the edited rows. Shadow flag: a cell checked in `rows` but not checked in the same-named `shadowRows` row gets a yellow warning icon with tooltip "No effect: MCP access never exceeds interactive access".
- `src/lib/McpRolesEditor.tsx` (MCP Roles grid plus Copy from roles / Clear buttons, shared by the groups page and the panel), `Feedback.tsx` (ApiErrorAlert, ConfirmButton modal, notifications), `useAsync.ts`, `useGroups.ts`, `useVisible.ts` (IntersectionObserver for lazy rows), `summary.ts`.
- `src/Groups.tsx` `renderGroupsPage`: staff only; group list left (core `user/group/`), selected group right with "Revoke MCP tokens for this group" (confirmation), accordions Roles (core, editable, PATCH per edited ruleset), MCP Roles (plugin API, with shadow flags against the interactive roles), Members (username, effective MCP access badge with tooltip, connection count; both loaded when the row scrolls into view).
- `src/Connections.tsx` `renderConnectionsPage`: status card, "Your MCP permissions" (read-only RoleGrid from `effective`), connections table with confirmed revoke, Connect card (URL plus copy, `claude mcp add --transport http inventree <url>` plus copy, three claude.ai steps). User pk from `context.user.userId()`, fallback `GET /api/user/me/`.
- `src/Panel.tsx` `renderMCPRolesPanel`: McpRolesEditor for `Number(context.id)`.
- Every entry calls `checkPluginVersion(context)` and returns an element (`<Component context=.../>`), so hooks live in real components (RemoteComponent calls the function during its own render).
- 404 from the plugin API renders a yellow "MCP API not available" alert (`ApiErrorAlert`) instead of crashing; the core "Roles" accordion still works.

## API assumptions (for the backend round to confirm)

Plugin base `/plugin/inventree-mcp/`; core base `/api/`. All requests via `context.api` (absolute paths, session or token auth).
- `GET /api/user/group/` list of `{pk, name}` (plain array or `{results}`); `GET /api/user/group/<pk>/?role_detail=true&user_detail=true` gives `roles` (RuleSet shape) and `users` (`[{pk, username, ...}]`); `PATCH /api/user/ruleset/<pk>/` with the four `can_*` flags.
- `GET api/rulesets/?group=<pk>` returns a list (or `{results}`) of `{pk, name, label, group, can_*}`; `PATCH api/rulesets/<pk>/` with the four flags; `POST api/rulesets/copy/` and `api/rulesets/clear/` with `{"group": pk}` (response body ignored); `POST api/rulesets/revoke/` with `{"group": pk}` returns `{"users": n, "tokens": m}`.
- `GET api/effective/<user_pk>/` as in WP3: uses `effective`, `superuser`, `may_connect`, `reason`. Role maps carry no labels, so labels are derived from the role name ("part_category" becomes "Part Category"); core labels may differ slightly.
- `GET api/connections/` list of `{pk, client_name, client_host?, client_trusted, created, expires, scopes, last_used?}`; `DELETE api/connections/<pk>/`. For the members table the page calls `GET api/connections/?user=<pk>` and counts the list (WP3: staff may pass `?user=`). If that is not supported it shows "n/a".
- `GET api/status/` as in WP3; `mcp_url` is used for the Connect card, fallback `<origin>/plugin/inventree-mcp/mcp/`.
- Per-member loading means 2 requests per visible member (effective plus connections).

## Backend wiring must provide

Static files are in `inventree_mcp/static/` (core resolves hashed names from plain ones):
- route `groups` (staff): `self.plugin_static_file("Groups.js:renderGroupsPage")`; nav item "MCP Access", icon `ti:plug-connected`
- route `connections`: `self.plugin_static_file("Connections.js:renderConnectionsPage")`; nav item "MCP"
- group panel (target_model == "group", staff): `self.plugin_static_file("Panel.js:renderMCPRolesPanel")`, passes the group pk as `context.id`
- The built files import shared chunks relatively (`./assets/useAsync-<hash>.js`, `./assets/McpRolesEditor-<hash>.js`); the whole `static/` tree including `assets/` must be served/collected (same as the template). `static/.vite/manifest.json` and `*.map` are also generated; add `.map` to git or ignore as preferred. `MANIFEST.in` should include `inventree_mcp/static` recursively (not checked: Python side out of scope).
- Endpoint `POST api/rulesets/revoke/` and the `?user=` support on `api/connections/`.

## Outputs

`npm ci && npm run build` (fresh node_modules), final run:
```
vite v7.3.7 building client environment for production...
../inventree_mcp/static/Panel.js                            0.88 kB
../inventree_mcp/static/Connections.js                      5.90 kB
../inventree_mcp/static/Groups.js                           5.91 kB
../inventree_mcp/static/assets/useAsync-8mDyhcEX.js        50.04 kB (shared chunk, includes @inventreedb/ui checkPluginVersion)
../inventree_mcp/static/Panel-D3guLdDZ.js / Connections-BPfrj92s.js / Groups-BbPbCfOU.js (hashed copies)
built in 912ms
```
`tsc -b`: clean. `npm run lint` (biome check): "Checked 15 files. No fixes applied."

Tests (`npm test`, vitest 3 + jsdom + testing-library; the externals plugin is not used in tests, React/Mantine resolve from node_modules, so no fight with the externals config):
```
 ✓ src/lib/Feedback.test.tsx (2 tests)   (404 alert, other error)
 ✓ src/lib/RoleGrid.test.tsx (4 tests)   (sorted rows, toggle + onSave with the edited row, read-only, shadow flag)
 Test Files  2 passed (2) / Tests  6 passed (6)
```

Bundle-load check (`npm run check:bundles`: imports each built file in node with host globals window.React, MantineCore, MantineNotifications, Lingui stubbed, asserts the export, and server-renders the element in a MantineProvider with a never-answering API stub):
```
ok Groups-BbPbCfOU.js: exports [renderGroupsPage], renders 3562 bytes of HTML
ok Groups.js: exports [renderGroupsPage], renders 3562 bytes of HTML
ok Connections-BPfrj92s.js: exports [renderConnectionsPage], renders 8170 bytes of HTML
ok Connections.js: exports [renderConnectionsPage], renders 8170 bytes of HTML
ok Panel-D3guLdDZ.js: exports [renderMCPRolesPanel], renders 1816 bytes of HTML
ok Panel.js: exports [renderMCPRolesPanel], renders 1816 bytes of HTML
```

## Not verified / notes

- Not run inside a real InvenTree (backend not wired; no browser). The panel was not temporarily mounted from the groups page; it shares McpRolesEditor with the groups page and renders in the node check.
- npm 12 blocks esbuild's postinstall script (warning only); build works.
- `npm audit` reports 6 vulnerabilities in dev dependencies (not addressed).
- Panel "MCP Roles" shows nothing else (no Roles accordion), as the core drawer already has its own.
