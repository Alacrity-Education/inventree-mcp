# InvenTree MCP - frontend

React and Mantine code for the plugin's UI: the "MCP Access" admin page, the
"MCP" connections page and the "MCP Roles" group panel. It follows the
InvenTree plugin-creator frontend template and is built with Vite against
`@inventreedb/ui` 1.5.x.

## Build

```bash
cd frontend
npm ci && npm run build
```

The build writes `Groups.js`, `Connections.js` and `Panel.js` (plain names) plus
hashed copies (`Groups-<hash>.js`, ...) and shared chunks into
`../inventree_mcp/static/`. **That directory is generated and git-ignored
(`inventree_mcp/.gitignore`): never edit it by hand.** It is built by
`dev/up.sh` (when stale), by CI, and must be built before `python -m build` so
the wheel contains it. A checkout without a build runs fine but has no plugin
pages (`tests/test_ui.py::UiFeatureMethodsTest::test_bundles_are_built` fails).

React, ReactDOM, `@mantine/core`, `@mantine/notifications`, `@lingui/core` and
`@lingui/react` are externals provided by the InvenTree host (see
`vite.config.ts`); do not import other host-provided libraries without adding
them there.

## Host version and `@inventreedb/ui`

- `@inventreedb/ui` is pinned to `~1.5.0` because 1.5.x is the newest line
  published to npm; there is no 1.6 package yet. Move the pin when it appears.
- The pages and nav items need **InvenTree 1.6 or later** (plugin routes,
  `get_ui_routes`; unreleased, the `latest` image). On 1.5.x the backend omits
  them and the bundles are never requested. They also need the global setting
  "Enable interface integration".
- On a 1.6 host the browser console logs "Plugin version mismatch!"
  (`checkPluginVersion`, informational) on each page load, because the
  plugin library is 1.5.x. It is harmless and goes away with the 1.6 package.

## Entry points

| Source | Export | Used for |
|---|---|---|
| `src/Groups.tsx` | `renderGroupsPage` | route `groups` and nav item "MCP Access" (staff with the admin role's change permission) |
| `src/Connections.tsx` | `renderConnectionsPage` | route `connections` and nav item "MCP" |
| `src/Panel.tsx` | `renderMCPRolesPanel` | group panel ("MCP Roles"), `context.id` = group pk |

Shared code is in `src/lib/` (`api.ts` typed API wrappers, `RoleGrid.tsx`,
`McpRolesEditor.tsx`, `useGroups.ts`, ...).

## Checks

```bash
npm run lint          # biome
npm test              # vitest (RoleGrid, error alert, save paths, revoke flows, roleMapToRows)
npm run build
npm run check:bundles # loads every built file with stub host globals and renders it
```
