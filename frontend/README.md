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
`../inventree_mcp/static/`. **That directory is generated: never edit it by
hand, rebuild and commit the result** (the dev environment installs the plugin
from the checkout, so the built files must be in git).

React, ReactDOM, `@mantine/core`, `@mantine/notifications`, `@lingui/core` and
`@lingui/react` are externals provided by the InvenTree host (see
`vite.config.ts`); do not import other host-provided libraries without adding
them there.

## Entry points

| Source | Export | Used for |
|---|---|---|
| `src/Groups.tsx` | `renderGroupsPage` | route `groups` and nav item "MCP Access" (staff) |
| `src/Connections.tsx` | `renderConnectionsPage` | route `connections` and nav item "MCP" |
| `src/Panel.tsx` | `renderMCPRolesPanel` | group panel ("MCP Roles"), `context.id` = group pk |

Shared code is in `src/lib/` (`api.ts` typed API wrappers, `RoleGrid.tsx`,
`McpRolesEditor.tsx`, `useGroups.ts`, ...).

## Checks

```bash
npm run lint          # biome
npm test              # vitest (RoleGrid, error alert)
npm run build
npm run check:bundles # loads every built file with stub host globals and renders it
```
