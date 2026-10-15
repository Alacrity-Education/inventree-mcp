# WP5 frontend and dev-script hardening: report

Scope: `frontend/**` (rebuilt into the git-ignored `inventree_mcp/static/`), `dev/up.sh`, `dev/ui_smoke.py`, `frontend/README.md`, `.gitignore` (comment), `docs/plans/WP4-gui.md`, the four screenshots. No README.md, dev/README.md, Python under `inventree_mcp/`, `dev/e2e/` or AGENTS.md was touched. Nothing was added to git.

## Items

1. **Finding 7 (shadow-flag wording).** `RoleGrid.tsx` `SHADOW_TOOLTIP` now reads: "This group grants no interactive access for this flag. It takes effect only for members who hold it through another group, or for tables this role shares with another role." (multiline tooltip, max width 320). The doc comment of `shadowRows` was corrected. `Groups.tsx` page text: "A member's MCP access is their interactive roles (across all their groups) intersected with the MCP roles of their groups, per table. A warning mark on an MCP flag means this group grants no interactive access for it: the flag takes effect only for members who hold the role through another group, or for tables shared with another role." The flag itself is kept.
2. **Finding 12.** `Groups.tsx`: one `refreshMembers` callback bumps the `MembersTable` key. It is passed as `onChanged` to `McpRolesEditor` (MCP save, Copy, Clear) and also called after the core roles save (that changes effective access too) and after a revoke. `RevokeResult` has `queued?: boolean`; the revoke notification is "Revocation queued for N members" (title "Queued") when `queued`, else "Revoked N token(s) of M user(s)".
3. **Finding 13.** `Connections.tsx`: the Connect card renders only once `api/status/` loaded, with `status.mcp_url` (no origin fallback). With OAuth on: URL, `claude mcp add --transport http inventree <url>`, claude.ai steps. With OAuth off: `claude mcp add --transport http inventree <url> --header "Authorization: Token <your-api-token>"`, no claude.ai steps, and the sentence "OAuth sign-in is off on this server, so clients authenticate with an InvenTree API token. Create one in your account settings and replace <your-api-token> in the command." The grey "OAuth connections are disabled" alert was removed.
4. **Finding 14.** `Feedback.tsx` 404 alert: "The MCP permission API is not available on this server. The plugin may be disabled, URL integration may be off, or the installed plugin version may be older than this page." Dev wording in comments removed.
5. **Finding 15.** `frontend/README.md` new section "Host version and `@inventreedb/ui`": the pin is `~1.5.0` because 1.5.x is the newest published line (no 1.6 package yet; move when it appears); the pages need InvenTree 1.6+ (plugin routes) and "Enable interface integration"; a 1.6 host logs "Plugin version mismatch!" from `checkPluginVersion` on each page load, informational.
6. **Finding 3.** Reworded in `.gitignore` (comment), `frontend/README.md` (generated, git-ignored via `inventree_mcp/.gitignore`, built by `dev/up.sh`, CI and before `python -m build`; a checkout without a build fails `test_bundles_are_built`) and the `docs/plans/WP4-gui.md` acceptance line.
7. **Finding 10.** `dev/up.sh` step 1b: missing npm, a failed `npm ci` or a failed build now only warn, naming `inventree_mcp.tests.test_ui.UiFeatureMethodsTest.test_bundles_are_built` (the stack continues). Staleness covers `src`, `package.json`, `package-lock.json`, `vite.config.ts`, `biome.json`, `tsconfig*.json`. `npm ci` runs only when `node_modules` is missing or older than `package-lock.json`; otherwise only `npm run build`. `bash -n dev/up.sh` clean. The script was not run end to end (it would restart or reconfigure the shared stack); only the frontend step was reviewed.
8. **Finding 11.** `dev/ui_smoke.py` rewritten: creates a scratch group `ui-smoke-<timestamp>` (no members, so a flag change cannot revoke any member's tokens), toggles Parts/Delete in the "MCP Roles" grid in the browser, verifies through the plugin API, and deletes the group in `finally`. The admin API token it creates is revoked in `finally` (`DELETE /api/user/tokens/<id>/`; InvenTree keeps the row flagged revoked). It never creates "MCP Users". Screenshots only with `--screenshots`. Docstring and comments corrected (no more "restore through the UI"). If the host does not offer the plugin navigation items (1.5.x) it runs the API part, prints "SKIPPED browser steps" and exits with status 3. `ruff check --isolated` (0.16.0) is clean (the CI lint step covers `dev/*.py`).
   **Deviation:** `membership_e2e.create_api_token` / `delete_api_tokens` are not reused. They create and delete the token named `membership-e2e` of **e2e-member** (not staff, so it cannot administer groups), and deleting it would break the concurrent e2e agent. The script needs an admin token, obtained from `/api/user/token/` with basic auth as before.
   **Screenshots retaken.** I reproduced an isolated `latest` stack (compose project `wp5-latest`, files in the scratchpad, Postgres, Redis, server and Caddy on 127.0.0.1:8081, no demo data, no Authentik; the shared stack was not touched) and ran `ui_smoke.py --base-url http://localhost:8081 --screenshots` from a playwright venv: output `after save in the browser: Parts delete = True`, `connections page: status card rendered`, `ui_smoke: ok`, scratch group deleted, token revoked. The four PNGs in `docs/plans/screenshots/` are the new ones (scratch group instead of "MCP Users"; the connections shot shows the API-token Connect variant because OAuth is off there). The stack was removed (`down -v`, data deleted). One earlier run on this stack timed out at the login wait right after the server restart; a repeat passed (the script does not retry).
9. **Finding 17 (frontend).** New vitest files, `src/pages.test.tsx` and `src/lib/api.test.ts` (6 tests became 17):
   - core ruleset save: Groups page, toggle in the interactive grid, Save: `PATCH /api/user/ruleset/<pk>/` with the four flags only;
   - plugin ruleset save: toggle in the MCP grid: `PATCH /plugin/inventree-mcp/api/rulesets/<pk>/`;
   - revoke result: "Revocation queued for 25 members" and "Revoked 3 token(s) of 2 user(s)";
   - Connections revoke: confirm modal, nothing deleted before confirming, `DELETE .../connections/<pk>/`, list reloaded (empty state shown, two list GETs);
   - Connect card: waits for the status; OAuth command; API-token command and the "OAuth sign-in is off" sentence;
   - `roleMapToRows` (API label, humanized fallback, label map, undefined) and both patch helpers against a mocked `api`.
   `@inventreedb/ui` and `@mantine/notifications` are mocked in `pages.test.tsx` (`checkPluginVersion` no-op, captured `notifications.show`).
10. **Readiness item 2 (browser e2e on latest).** Not built, as instructed; requirements below.

## Verification

```
npm run lint:           Checked 17 files in 4ms. No fixes applied.
npm test:               Test Files 4 passed (4); Tests 17 passed (17)
npm run build:          tsc -b clean; vite built; Groups/Connections/Panel (+ hashed copies) in inventree_mcp/static/
npm run check:bundles:  6 x ok (Groups, Connections, Panel, plain and hashed names)
bash -n dev/up.sh:      ok
uvx ruff@0.16.0 check --isolated dev/ui_smoke.py: All checks passed
python3 dev/ui_smoke.py (shared 1.5.7 stack, http://localhost:8080):
  scratch group 'ui-smoke-...' pk=12: Parts delete = False
  SKIPPED browser steps: this InvenTree does not offer the plugin pages (needs 1.6 or later)
  deleted scratch group pk=12 / revoked the script's API token / exit status 3
python3 dev/ui_smoke.py --screenshots on the isolated latest stack (8081): ui_smoke: ok
```

On the shared stack my probes created a few admin API tokens (`wp5-*`, `chk`, `ui-smoke-*`); all are revoked (rows remain, flagged revoked; the DELETE endpoint revokes). No group, role or e2e-member token was touched. Note: `dev/data/static/plugins/inventree-mcp/*` on the shared stack is root-owned (collected by the container), not by me.

## README deltas (README.md and dev/README.md are not mine)

README.md, GUI section:
- Shadow flag bullet: replace "a warning mark on flags that grant more than the interactive role and therefore have no effect" with "a warning mark on an MCP flag whose group grants no interactive access for it; the flag only takes effect for members who hold that role through another group, or for tables shared with another role (a member's MCP access is their interactive roles across all groups intersected with the MCP roles of their groups, per table)".
- Connect card: "The Connect card shows the OAuth command (and the claude.ai steps) when OAuth is enabled, and otherwise the API-token variant `claude mcp add --transport http inventree <url> --header "Authorization: Token <your-api-token>"` with a note that OAuth sign-in is off."
- Revocation: "Revoking a group's MCP tokens reports 'Revocation queued for N members' above 20 members (the work runs in the background)."
- Finding 9 items (not mine to edit, listed for the README owner): version wording "InvenTree 1.6.0 or later (not yet released; the `latest` image)"; the group panel needs `docs/upstream/` patch 3; pages and nav items need the global setting "Enable interface integration" (`ENABLE_PLUGINS_INTERFACE`).
- Any "bundles are committed" sentence becomes "generated and git-ignored; built by `dev/up.sh`, CI and before `python -m build`".

dev/README.md:
- "Frontend": version wording as above (1.6.0 or later, not "newer than 1.5.7").
- `ui_smoke.py` table row and the "Frontend" paragraph: "creates and deletes a scratch group and revokes its own admin API token; screenshots only with `--screenshots`; on a host without plugin routes it runs the API part and exits with status 3 (skipped)". Run line: `/tmp/pw/bin/python dev/ui_smoke.py [--base-url http://localhost:8080] [--screenshots]`.
- `up.sh`: "the frontend step warns and continues when npm is missing or the build fails (then `test_ui.UiFeatureMethodsTest.test_bundles_are_built` fails); `npm ci` runs only when `node_modules` is missing or older than `package-lock.json`; the staleness check includes `package-lock.json`, `tsconfig*.json` and `biome.json`".

## What a browser e2e on 1.6 needs (readiness item 2, for scheduling)

The spec proofs still open: e2e-member opens `/web/plugin/inventree-mcp/connections` after an OAuth connection, revokes it, and the refresh grant then answers `invalid_grant`; and the "MCP Roles" group panel with patch 3.
- A routes-capable stack with Authentik: `INVENTREE_TAG=latest` and `DEMO_DATASET_REF=WORKTREE` in `dev/.env`, then `dev/down.sh -v && dev/up.sh` (a full second environment; the shared 1.5.7 stack cannot be switched while other agents use it). My isolated stack has no Authentik, no SSO config, port 8081 and `INVENTREE_SITE_URL` 8081; Authentik's issuer URL and the OIDC redirect are bound to `localhost:8080` and `authentik.localtest.me:19000` in `dev/authentik/bootstrap.py` and `config.yaml.tmpl`, so a second Authentik cannot run beside the shared one (same ports, same hostnames). The latest stack therefore has to replace the shared one, or take its own ports and issuer.
- `/etc/hosts` entry `127.0.0.1 authentik.localtest.me` (already needed for SSO).
- The e2e flow (reuse `dev/e2e` / `membership_e2e.connect()` for the OAuth connect as e2e-member, `oauth_smoke.sso_login_through_bridge`), then a playwright session logged in as e2e-member through Authentik SSO (the web UI login button for the OIDC provider, or a session cookie obtained with the allauth browser flow), open `/web/plugin/inventree-mcp/connections`, click Revoke and confirm in the modal, then POST the refresh token to the token endpoint and assert 400 `invalid_grant`.
- Group panel: apply `docs/upstream/` patch 3 to the core checkout used for the image (the stock `latest` image does not request plugin group panels), then open a group drawer as admin and check "MCP Roles".
- Isolation: a scratch group and a scratch Authentik user, not "MCP Users" membership changes; revoke/cleanup in `finally`; never run it concurrently with `membership_e2e.py`.
- Known flake to handle: the first login right after a server restart can time out; wait for `/api/` health plus the login form before filling.

## Deferred

- `ConnectCard` and `connectCommand` stay private to `Connections.tsx` (exporting extra names from a page entry changes the bundle's API); they are tested through the page.
- No test for the members table refresh (key bump) itself; only the save and revoke paths are asserted.
- `dev/up.sh` was only syntax-checked and reviewed, not executed against the shared stack.
