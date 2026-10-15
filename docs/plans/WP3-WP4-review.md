# WP3 review fixes and WP4 (GUI): review

Reviewed: `git diff bd04e4c..HEAD` on `feature/oauth-facade` (HEAD `b7c814f` "WP3 review fixes and WP4 backend wiring", plus `42e4524` "ci: …" in the range), the frontend under `frontend/` (committed in `fd2bff3`), the working tree, and the four patches in `docs/upstream/`. References: DOT 3.4.0, InvenTree core master `9b8fdeb06` (1.6.0 dev), the dev stack (InvenTree 1.5.7, API 530). Read-only review. The only file written is this one. Live probes created a DOT application, an access token and an API token, and changed the MCP roles of "MCP Users" with `queryset.update()` (no revocation signal). All of these were removed or restored afterwards.

## Verdict for A (WP3 fixes)

**Fixes confirmed. One residual in the upstream material.**

- F1 to F10 and the nit are implemented as the report describes.
- F1 and F2 were confirmed live through Caddy (see "Test and probe results").
- `may_connect` keeps its signature and semantics.
- The full suite passes, and so do `permissions_e2e.py` and `oauth_smoke.py`.

Residual: the new upstream patch 4 changes more than its README says (finding 4). That must be corrected before the patch is proposed. The plugin is not affected.

## Verdict for B (WP4)

**Accept with fixes.**

The backend wiring, the visibility rules, the missing-build behaviour and the frontend logic are sound. The pages worked live on `latest` (per the report and its screenshots).

One blocker:

- `frontend/src/lib/` (all shared frontend code: `api.ts`, `RoleGrid.tsx`, `McpRolesEditor.tsx`, the hooks, both vitest files) has never been committed. It is caught by the Python template's `lib/` rule in `.gitignore:17`.
- As a result, a fresh clone cannot build the frontend, `dev/up.sh` dies on a fresh clone, and CI's frontend job fails. Together with finding 2, the new CI job is red.

The other findings are documentation contradictions, wording of the shadow flag, log noise and test-script side effects.

## Findings

### 1. Blocker: `frontend/src/lib/` is git-ignored and missing from every commit

**Where.** `.gitignore:17` (`lib/`, from the Python template). `git check-ignore -v frontend/src/lib/api.ts` reports `.gitignore:17:lib/`. `git ls-files | grep -c lib/` gives `0`.

**Effect.** A fresh clone of HEAD, built with the existing `node_modules`:

```
src/Groups.tsx(44,27): error TS2307: Cannot find module './lib/useGroups' ...
src/Panel.tsx(6,26): error TS2307: Cannot find module './lib/api' ...
```

- `dev/up.sh:61` then dies with "frontend build failed" on any fresh checkout.
- The CI "Frontend (lint, test, build)" step fails. `integration-tests` `needs: ci`, so it never runs.
- The WP4 backend round's edits to `frontend/src/lib/{api,summary}.ts` (the `label` support) exist only in this working tree.

**Fix.**

1. Anchor the Python rule (`/lib/`), or add `!frontend/src/lib/` after it.
2. `git add frontend/src/lib`.
3. Verify with a clean clone: `npm ci && npm run build && npm test`.

### 2. Major (CI): the new dev-script lint step fails on a pre-existing error

**Where.** `.github/workflows/ci.yaml` "Style Checks" now runs `ruff check --isolated dev/*.py dev/authentik/*.py`. This fails with `UP035` at `dev/authentik/sso_smoke.py:31` (`from typing import Callable`): "Found 1 error". The WP3 report mentions it as pre-existing, but the commit in this range made it CI-blocking.

**Fix.** `from collections.abc import Callable`.

### 3. Major (docs): "bundles are committed" versus "bundles are git-ignored"

The amended spec (`WP4-gui.md`, Frontend project) and `inventree_mcp/.gitignore` (`static`) make the bundles build output. Three places still say the opposite:

- `.gitignore:169`: "built output lives in inventree_mcp/static/ and is committed".
- `frontend/README.md:17-19`: "never edit it by hand, rebuild and commit the result (the dev environment installs the plugin from the checkout, so the built files must be in git)".
- `docs/plans/WP4-gui.md:44` (Acceptance): "built assets committed".

**Fix.** Reword all three to "generated, git-ignored; built by `dev/up.sh` / CI / before `python -m build`".

### 4. Major (upstream patch 4): wrong scope-of-change claim, and the schema check will fail

`docs/upstream/README.md` §4 says "`map_scope(roles=...)` has a single caller (`InvenTreeRoleScopeMixin.get_required_alternate_scopes`)". That is false on master:

- `InvenTree/permissions.py:444-450`: `DataImporterPermission.get_required_alternate_scopes` calls `map_scope(roles=_roles, map_read=SAFE_METHODS, override_all_actions='change')`.
- `InvenTree/permissions.py:483-485`: `ContentTypePermission` calls `map_scope(roles=_roles)`.
- `_roles` is all 11 roles (`users/oauth2_scopes.py:12`).

**Effect of the patch on these two classes.**

- Before the patch, a write to the data importer (`importer/api.py:34`, `:74`) needed all eleven `r:change:*` scopes.
- `GenericMetadataView` (`InvenTree/api.py:908`) needed all eleven scopes for its method.
- After the patch, any single scope suffices.

The object-level checks still run (`check_user_permission(..., 'change')`, `obj.check_permission`). This is consistent with the patch's rationale, but it is an additional relaxation on two more endpoint families that the README and the tests do not mention.

**Schema check.** The README says "no API version bump is needed". Core's `qc_checks.yaml:233-234` runs `diff -u … schema.yml api.yaml … || exit 2` against the published schema of the *current* API version. The changed security requirements of every multi-role endpoint (`InvenTree/schema.py:38-44`) will therefore fail that job unless `INVENTREE_API_VERSION` is bumped with a text entry.

`git apply --check` passes for all four patches (see below). The test code compiles against master's imports (`timedelta`, `timezone` at `tests.py:5`, `:24`).

**Fix.**

- Correct §4.
- List the importer and metadata endpoints in the PR text.
- Add an assertion for `ContentTypePermission` (one `r:view:<role>` now passes).
- Add an `api_version.py` bump (v557) to the patch.

### 5. Minor: AGENTS.md misses the WP4 deliverables

The spec requires "`AGENTS.md`: frontend build and the rule that `inventree_mcp/static/` is generated, never edited by hand". `grep -i "frontend\|static\|npm\|test_ui" AGENTS.md` finds nothing.

The file map also lacks:

- the UI methods in the `core.py` entry (`AGENTS.md`, `core.py` bullet);
- `frontend/`;
- `tests/test_ui.py`;
- `dev/ui_smoke.py`.

`AGENTS.md:186` still says "CI installs the latest ruff". CI pins 0.16.0 since `42e4524`.

### 6. Minor: log noise on every UI feature request

- `core.py:328-331`: on every 1.5.x instance (the supported stable line), each `GET /api/plugins/ui/features/navigation/` logs INFO "this InvenTree has no plugin routes, no navigation items". The web UI calls this on every page load.
- `core.py:288-291`: a missing build logs a WARNING per feature call (routes and panels).

**Fix.** Log each once per process, for example with a module-level flag or `functools.cache` on the message key.

### 7. Minor: the shadow flag says "No effect", which is not always true

**Where.**

- `RoleGrid.tsx:98-101` compares an MCP cell with the *same group's* interactive row.
- `Groups.tsx:210` and `Panel.tsx:321` pass `interactiveRows={g.roles}`.

MCP access is limited by the *member's* interactive roles across all of their groups, per table (WP3 F4). A flagged cell does have an effect in two cases:

- **Another group.** The member holds the interactive role through another group.
- **A shared table.** Example: MCP *Build Orders: view* on a group without interactive Build Orders opens parts and stock for members with interactive *Parts* or *Stock* view.

The same inaccuracy appears in three places:

- the tooltip (`RoleGrid.tsx:18-19`);
- the page text "MCP access of a user is the intersection of the group's interactive roles and its MCP roles" (`Groups.tsx:244-247`);
- the README GUI bullet ("a warning mark on flags that grant more than the interactive role and therefore have no effect").

**Fix.**

- Tooltip: "This group grants no interactive <flag> on this role. It takes effect only for members who hold it through another group, or for tables this role shares with another role."
- Page text: "a member's MCP access is their interactive roles (all groups) ∩ the MCP roles of their groups, per table".

Keep the flag itself; it is useful.

### 8. Minor: "MCP Access" visibility is `is_staff`, but the API requires staff plus `admin.change`

`core.py:300-302` (`_is_staff`) gates the `groups` route, the "MCP Access" nav item and the group panel on `user.is_staff`. Every endpoint the page calls needs `IsStaffAdmin`: staff and `check_user_role(user, "admin", "change")` (`auth/api.py:71-89`).

A staff user without the admin role sees the page, and every plugin call answers 403. This matches the spec's wording but not its intent.

**Fix.** Use the same predicate as `IsStaffAdmin` (factor it into a helper used by both). Add a `test_ui` case for staff without the admin role.

### 9. Minor: README GUI section

- **Version.** "InvenTree newer than 1.5.7, which introduced plugin pages" is inaccurate:
  - `d33d77d67` (#12900) is in no tag; master is 1.6.0 dev.
  - A 1.5.8 patch release would not have routes.
  - Say "InvenTree 1.6.0 or later (not yet released; the `latest` image)".
  - `dev/README.md` "Frontend" has the same wording.
- **Group panel.** "registered … as the entry point for a future core group drawer" does not say that it needs `docs/upstream/` patch 3 (`0003-Render-plugin-panels-in-the-group-and-user-drawers…`). Name it.
- **Interface setting.** Nothing says the pages and nav items appear only with the global setting "Enable interface integration" (`ENABLE_PLUGINS_INTERFACE`). See `plugin/base/ui/api.py:29`; the dev stack sets it in `configure_inventree.py:23`.
- **Shadow flag.** See finding 7.

### 10. Minor: `dev/up.sh` frontend step

- **Fails instead of warning.** `up.sh:59` and `:61` `die` when npm is missing or the build fails. The backend handles a missing build (warning, no UI features), and the default 1.5.7 stack cannot show the pages at all. A missing node should warn and continue, with the warning naming the one test that will fail (`test_ui.UiFeatureMethodsTest.test_bundles_are_built`).
- **Staleness check.** It covers `frontend/src`, `package.json` and `vite.config.ts`, but not `package-lock.json`, `tsconfig*.json` or `biome.json`. Add them.
- **Rebuild cost.** `npm ci` runs on every rebuild, which is a full reinstall over the network. Run it only when `node_modules` is missing or older than `package-lock.json`.
- **Idempotency.** Idempotent otherwise: a failed `tsc` leaves `Groups.js` stale, so the next run retries.

### 11. Minor: `dev/ui_smoke.py` side effects on the shared stack

- **(a) Every run revokes members' MCP tokens.** On the dev default, `MCP Users` has view-only MCP roles, so Parts/Delete starts `False`. The UI save is an addition. The restore PATCH (`ui_smoke.py:122-128`) is a reduction (True → False), and its `post_save` signal revokes the MCP tokens of every member of "MCP Users" (admin and e2e-member). This interferes with any concurrent e2e run.
- **(b) It leaks an API token per run.** `ui-smoke-<timestamp>` (`:35`) is never deleted. `permissions_e2e.py` deletes its own token.
- **(c) It creates "MCP Users"** when the group is missing (`:62`).
- **(d) It overwrites the committed screenshots** in `docs/plans/screenshots/` on every run.
- **(e) It does not reuse the existing helpers.** It re-implements an API-token client instead of `membership_e2e.create_api_token` / `delete_api_tokens`. The comment "restore (through the UI's own API call …)" is misleading: the restore uses the API token, not the UI.

**Fix.**

- Toggle a flag on a scratch group the script creates and deletes (with no members), or restore through `membership_e2e.shell()` with `queryset.update()`.
- Delete the token in `finally`.
- Write screenshots only with `--screenshots`.

### 12. Minor: Groups page state after actions

- **Stale members table.** After an MCP roles save, Copy or Clear, the Members table keeps the old effective badges. `McpRolesEditor` supports `onChanged`, but `Groups.tsx:210` does not pass it. Pass a callback that bumps the `MembersTable` key, as revoke does.
- **`queued` ignored.** The revoke response's `queued` is dropped: `RevokeResult` in `api.ts` has no `queued`, and `Groups.tsx:170` always says "Revoked N token(s)". Above 20 members the count is "live at queueing time" and the work is still pending. Show "queued" in that case.

### 13. Nit: Connect card

`Connections.tsx:258-282`:

- **Fallback URL.** The card is shown while `api/status/` is loading, using a fallback URL (`window.location.origin/...`).
- **OAuth off.** The card is also shown when OAuth is disabled, with only a grey note underneath. Without OAuth, `claude mcp add --transport http inventree <url>` cannot authenticate.
- **Fix.** Render the card after the status is loaded. When `oauth_enabled` is false, show the API-token variant (`--header "Authorization: Token <your token>"`) instead.

The URL itself comes from `discovery.resource(request)`, so it is correct behind a proxy and not hard-coded. Only `mcp_url` from the server is used once loaded.

### 14. Nit: development wording in shipped UI

`Feedback.tsx:12-21`: "The plugin backend is probably not wired up yet". Replace it with "The MCP plugin API is not available (plugin disabled, URL integration off, or an older plugin version)".

### 15. Nit: `@inventreedb/ui ~1.5.0`

The pages run only on hosts of 1.6 or later. There, `checkPluginVersion` logs "Plugin version mismatch!" (console info) on every page load. It is only informational. Move to the 1.6 lib when it is published, and record the pin rationale in `frontend/README.md`.

### 16. Nit: remaining double computation (F7 follow-up)

- `effective_document` (`auth/api.py:411-414`) and `StatusView.get` (`:535`, `:545`) still call `policy.narrow(None, user)` and `policy.may_connect(user)` separately. That is two passes of `in_required_groups` and the ruleset query; use `connect_scopes()`.
- `ensure_rulesets_for(Group.objects.all())` (`:288`) loads full `Group` rows only to read `pk`.

### 17. Nit: test and spec-proof gaps

- No test covers the write branch of `_call_view_sync` carrying `request_meta` (`proxy.py`, the `format="json", **meta` call). `MCP_READ_ONLY` hides it in every existing test.
- vitest covers `RoleGrid` and `Feedback` only. There are no tests for:
  - the two save paths (core `user/ruleset/<pk>/` PATCH vs plugin `api/rulesets/<pk>/` PATCH);
  - the Connections revoke flow;
  - `roleMapToRows` labels.
- WP4 spec proofs not performed (declared in the report):
  - the panel rendered in a browser;
  - "e2e-member revokes a connection on the page, then the refresh grant fails".
- The commit mixes the WP3 fix-up and WP4. The spec asked for "one commit `WP4: …`".

### Checked and correct (no finding)

**`core.py` scope.** Beyond the UI methods, the only changes are:

- `from __future__ import annotations`;
- `Path`;
- the `UserInterfaceMixin` import and base;
- `UI_ROUTES_SUPPORTED`.

**Routes and navigation.**

- `groups` is staff only; `connections` is for any authenticated user.
- Navigation: "MCP Access" (staff) and "MCP" (everyone), both `ti:plug-connected`.
- `options.url` = `plugin/inventree-mcp/<page>`, which matches master's `Header.tsx:278` (`/${nav.options.options.url}`).
- The route `options.path` matches `PluginRoutes.tsx` (`${plugin_name}/${path}`; `plugin_name` = slug, `plugin/base/ui/api.py:50`).

**Group panel.** Only for `target_model == "group"` and staff. Sources are resolved through `plugin_static_file(..., check_exists=False)`, which resolves the hashed copy through `.vite/manifest.json` (`plugin.py:590-628`); the manifest ships in the wheel.

**Missing build.** `_ui_source` checks the package's `static/<file>`, warns and omits the feature. There is no exception path. Core would catch one per plugin anyway (`plugin/base/ui/api.py:36-44`), but it would write an Error Log entry on every call, so the guard is justified.

**`UI_ROUTES_SUPPORTED`.**

- It probes `hasattr(UserInterfaceMixin, "get_ui_routes")` on core's mixin class, not on the plugin. The plugin's own override does not affect it.
- On 1.5.7 the mixin has no `get_ui_routes`, and its `get_ui_features` map has no `'route'`. Verified in the container: the mixin source lists `get_ui_navigation_items` but no `get_ui_routes`.
- On master, `mixins.py:107-112` maps `'route': self.get_ui_routes` and `:237` defines it.
- A false positive on 1.5.x is impossible.
- A false negative on 1.6+ would need core to rename the method, which would break the routes anyway.
- It is feature detection, as hard rule 7 requires.
- Routes and panels are deliberately not gated; 1.5.7 never asks for them.

**Frontend API shapes.** `api.ts` matches `auth/api.py`:

- RuleSet fields come from core's `RuleSetSerializer`. List endpoints are unpaginated, and `asList` tolerates both shapes.
- `Effective`, `Connection` (incl. `access_expires`) and `McpStatus` match their serializers.
- Copy, clear and revoke use the `{"group": pk}` body.
- `?user=` is used for member connections.

**Save paths.** Interactive rows are PATCHed to core `/api/user/ruleset/<pk>/`, as the core drawer does. MCP rows are PATCHed to `/plugin/inventree-mcp/api/rulesets/<pk>/`. Only edited rows are sent. A failure keeps the edits (`RoleGrid.tsx:84-96`) and shows a notification.

**Revoke flow and secrets.**

- A connection revoke goes through a confirmation modal, then DELETE, then a reload.
- No token values, secrets or e-mail addresses are rendered. The page shows the scope strings, the client name (React-escaped), the client host and the username.

**Patch 3** passes `model: ModelType.group`, so `target_model == "group"` matches `get_ui_panels`.

## Part A detail (file:line evidence)

### 1. F1: every OAuth token on `/mcp` is cut to the current MCP scopes

**Code.**

- `mcp_transport.py:366-394`: one `policy.connect_scopes(user)` per request. Then:
  - OAuth: `narrowed_oauth_token(drf_request.auth, user, scopes)` (`:271-287`), an *unsaved* `AccessToken` with scope = token scopes ∩ MCP scopes and the token's own `expires`;
  - otherwise: `scoped_token_for(user, scopes)`.
- An empty scope gives 403 "No MCP permission" (`:392-394`).
- The presented row is never written. The narrowed object has no `application` and no `token`, so it cannot be used as a credential.
- `drf_request.auth` is used nowhere else after this point. `get_current_oauth2_token()` is read only by `proxy.py:78` and `:145`.

**Tests.** `test_policy.py`:

- `test_non_facade_oauth_token_is_cut_to_the_mcp_roles`: a `MANUAL` application, interactive stock view, MCP parts only. The test asserts the stored scope is unchanged.
- `test_oauth_token_without_any_mcp_scope_gets_403`.
- `test_leaving_the_group_takes_effect_at_the_next_request`: 403 after `groups.remove`, refresh token not revoked.

**Live.** All passed; see "Test and probe results":

- a manual application;
- a token `g:read r:view:stock r:view:part` for e2e-member;
- MCP parts only;
- group removal → 403;
- the stored scope unchanged.

### 2. F2: `request_meta` propagation

**What is passed.** `mcp_transport.request_meta` (`:289-304`) passes exactly four keys:

- `HTTP_HOST` from `get_host()`, already validated and `USE_X_FORWARDED_HOST`-aware;
- `wsgi.url_scheme` from `is_secure()`;
- `REMOTE_ADDR`;
- `HTTP_ACCEPT_LANGUAGE`, only when sent.

No `Authorization`, cookies, CSRF or `X-Forwarded-*`.

**Binding.** `context.py` stores it as a `MappingProxyType` in the frozen `_Identity`. `get_current_request_meta()` returns a copy.

**Both factory calls.** `proxy.py` passes `**meta` to the GET and to the write call (`_call_view_sync`), and to `_factory.generic` in `_user_has_access_sync`.

**Inner request scheme.** With `SECURE_PROXY_SSL_HEADER` set but the header absent on the inner request, Django's `HttpRequest.scheme` falls back to `wsgi.url_scheme`, so https is preserved.

**Regression test.** `test_paginated_list_uses_the_request_host`:

- It uses `@override_settings(ALLOWED_HOSTS=["mcp.example"])`. This *replaces* the `testserver` entry that `setup_test_environment` appends.
- An inner request with the factory's default `SERVER_NAME=testserver` / `HTTP_HOST=testserver` therefore raises `DisallowedHost` in `build_absolute_uri` for the `next` link.
- With the fix, the host is `mcp.example`, which is allowed. The test asserts one row and `next` starting with `http://mcp.example/`, for an API token and for a façade OAuth token.
- So the test fails without the fix. This was read and reasoned, not re-run with the fix removed (no code changes allowed). The WP3 report records the failing run (`DisallowedHost … 'testserver'`).

**Live.** Through Caddy: `list_parts {"limit":1}` → a row and `next=http://localhost:8080/api/part/?limit=1&offset=1`.

### 3. F4 to F9 and the nit

**F4.**

- `auth/api.py`: `EFFECTIVE_NOTE` (`:383-391`) is returned as `note`.
- The serializer field is added, with help text.
- `README.md`: "per table" and the example.
- The `policy.py` docstring is updated.
- Test: `test_api.py` `EffectiveApiTest.test_document`.

**F5.**

- `test_api.py`: asserts 401 with `a:staff` on the token.
- `test_staff_endpoints_require_a_staff_on_an_oauth_token` calls `MCPRuleSetDetail` directly: 403 without `a:staff` (row unchanged), 200 with it.
- README and `api.py` docstring reworded.

**F6.**

- `membership.mcp_token_querysets` (`membership.py:821-841`) is used by `revoke_all_mcp_tokens` and by `signals.live_mcp_token_count`.
- `revoke_users` now delegates inline to `tasks.revoke_mcp_tokens(user_ids, why)`.
- The duplicate loop and log are gone.
- Behaviour change: expired access tokens are no longer revoked or deleted here. That is DOT `cleartokens`' job, and the docstring says so.

**F7.**

- `policy.connect_scopes` (`policy.py:188-206`) runs anonymous, inactive, blocked, `in_required_groups`, then `_granted_scopes`. That is the original `may_connect` order and reason set.
- `may_connect` returns `(ok, reason)` from it, so the signature and semantics are unchanged. `membership._classify` still sees the same reasons.
- `effective_scopes` = `set(_granted_scopes)` after the same identity checks, so the result is identical. `g:read` is first, consistent with `mcp_scopes_available()` order.
- `scoped_token_for(user, scopes=None)` stays backward compatible.

**F8.** `signals.ensure_rulesets_for`: one SELECT of `(group_id, name)` and one `bulk_create(ignore_conflicts=True)`. `ensure_rulesets` delegates to it.

**F9.**

- `models.py`: `choices` dropped, with a comment.
- `0002_mcpruleset.py`: the `CreateModel` has no choices. That is state only, so no new migration.
- `label` still uses `RULESET_CHOICES`.

**Nit.** `signals._flags()` returns `MCPRuleSet.RULE_OPTIONS`.

**F10.** `permissions_e2e.py` `real_tool_calls()` requires `isError` false, rows, and `next` on the proxy base, plus `get_part`, for the OAuth token and for the API token. See its output below.

**Duplication.** No new duplication in the Python changes (details under Duplication).

### 4. Upstream patch 4

**Content.**

- `map_scope` returns one alternative per role.
- Docstring updated.
- `OAuth2ScopeMapTest` has two tests:
  - `map_scope` per method;
  - DOT `TokenMatchesOASRequirements` on `PartList`'s required scopes.
- README §4 added, and the checklist updated.

**`git apply --check`** against `/home/alex.lucaci/Projects/inventree/InvenTree` (`master`, `9b8fdeb06`): OK for 0001, 0002, 0003 and 0004.

**Issues.** See finding 4: the README's caller claim, the importer and metadata relaxation, and the schema-diff job.

## Duplication

- **Removed by the fixes:**
  - the token querysets (F6);
  - the revoke loop and log (F6);
  - the third spelling of the flags (nit);
  - per-group ensure (F8).
- **Shared rather than duplicated:**
  - `connect_scopes` / `_granted_scopes` are shared by `may_connect`, `effective_scopes` and the transport;
  - `narrowed_oauth_token` and `scoped_token_for` both build unsaved DOT tokens through `get_access_token_model()`; that is two short constructors, acceptable.
- **Frontend.** `McpRolesEditor` is shared by the groups page and the panel. `RoleGrid` serves all three grids.
- **Remaining:**
  - `effective_document` / `StatusView` recompute what `connect_scopes` returns (finding 16);
  - `dev/ui_smoke.py` re-implements the API-token helpers of `membership_e2e.py` (finding 11);
  - `ui_smoke.py`'s `Api.flag` / `group_pk` result unwrapping duplicates `api.ts`'s `asList` idea, which is acceptable in a test script.
- **No re-implemented DOT or core logic.**
  - The UI wiring uses `plugin_static_file` and core's feature API.
  - The frontend reuses `context.api`, `checkPluginVersion` and Mantine.
  - The core ruleset is saved through core's own endpoint.

## Test and probe results (quoted)

`dev/test.sh inventree_mcp` (dev stack, InvenTree 1.5.7):

```
Ran 332 tests in 322.035s
OK (skipped=3)
```

`python3 dev/permissions_e2e.py` (tool-call lines):

```
[A] tools/call list_stock_items -> isError=True 'Error executing tool list_stock_items: You do not have permission to perform this action.'
[A] OAuth token: tools/call list_parts {'limit': 1} -> isError=False, count=438, rows=1, next='http://localhost:8080/api/part/'
[A] OAuth token: tools/call get_part {'part_id': 82} -> isError=False, pk=82
[B] API token: tools/call list_parts {'limit': 1} -> isError=False, count=438, rows=1, next='http://localhost:8080/api/part/'
[B] API token: tools/call get_part {'part_id': 82} -> isError=False, pk=82
...
OK: MCP permission set end to end
```

F1 live probe. Setup through `dev/shell.sh`:

- a `registration_source=manual` application;
- an access token `g:read r:view:stock r:view:part` for e2e-member;
- the "MCP Users" MCP roles reduced to Parts view with `update()`;
- e2e-member's MCP scopes `['g:read', 'r:view:part']`;
- interactive stock view and part view both `True`.

The calls went through Caddy, `http://localhost:8080/plugin/inventree-mcp/mcp/`:

```
tools/list -> 200 ; 16 tools; list_parts True ; list_stock_items False
tools/call list_stock_items -> 200 isError= True 'Error executing tool list_stock_items: You do not have permission to perform this action.'
tools/call list_parts -> 200 isError= False rows= 1 next= http://localhost:8080/api/part/?limit=1&offset=1
direct /api/stock/?limit=1 with the same token -> 200        (interactive API untouched)
after removing e2e-member from "MCP Users": {"jsonrpc": "2.0", "error": {"code": -32000, "message": "No MCP permission"}, "id": null} -> HTTP 403
stored token scope after the calls: 'g:read r:view:stock r:view:part'
```

Restored afterwards:

- the group membership;
- the original flags;
- the application deleted (cascading the token);
- the API token deleted.

e2e-member's scopes are back to the 12 view scopes.

F2 live probe, with admin's API token:

```
list_parts {"limit":1}: isError= False count= 438 rows= 1 next= http://localhost:8080/api/part/?limit=1&offset=1
list_locations {"limit":1}: isError= False next= http://localhost:8080/api/stock/location/?limit=1&offset=1
```

`python3 dev/oauth_smoke.py --sso e2e-member` (tail):

```
[12] tools/list with the pre-refresh access token -> 401 (revoked by rotation)
[14] refresh with the just-revoked refresh token -> 400 invalid_grant
OK: OAuth façade flow completed (SSO as e2e-member)
```

Frontend, working tree (which includes the untracked `src/lib/`):

```
npm run lint: Checked 15 files in 5ms. No fixes applied.
npm test:     Test Files  2 passed (2)   Tests  6 passed (6)
npm run check:bundles: ok Groups(-DIzd31oa).js / Connections(-Cb2nTftL).js / Panel(-Cm_6DKwB).js (6 ok)
```

Fresh clone of HEAD (with the existing `node_modules`):

```
npm run build -> error TS2307: Cannot find module './lib/api' (and every other ./lib import)
```

Wheel built from that clone with the working tree's `static/` copied in: 17 static files, including `inventree_mcp/static/Groups.js`, `.vite/manifest.json` and 8 `.map` files. `MANIFEST.in` is correct.

Lint:

```
uvx ruff@0.16.0 check --preview inventree_mcp           -> All checks passed!
uvx ruff@0.16.0 format --preview --check inventree_mcp  -> 52 files already formatted
uvx ruff@0.16.0 check --preview --isolated inventree_mcp/tests -> All checks passed!
uvx ruff@0.16.0 check --isolated dev/*.py dev/authentik/*.py -> UP035 dev/authentik/sso_smoke.py:31, Found 1 error.
```

Upstream: `git apply --check` of 0001 to 0004 on InvenTree `master` `9b8fdeb06`: all OK.

Screenshots (`docs/plans/screenshots/*.png`):

- Group list on the left; the selected group with Roles, MCP Roles and Members accordions.
- Save/Reset aligned right, as in core's RoleTable.
- Amber checkboxes with a warning icon on Parts View/Change/Delete in a group without interactive Parts.
- The connections page with the Status, permissions grid, Connections and Connect cards. The Connect card is rendered although OAuth is "DISABLED" (finding 13).

## Next-stage readiness (what WP5 hardening must include)

1. **Repository integrity.**
   - Fix finding 1 and prove it from a fresh clone: `git clone` → `dev/up.sh` (frontend step) → `npm ci && npm run build && npm test` → `python -m build` with the wheel check.
   - CI green on both matrix legs (findings 1 and 2).
2. **A browser e2e on a routes-capable InvenTree (`latest`, or 1.6 when released).**
   - Covers the spec proofs not yet done: e2e-member opens `/web/plugin/inventree-mcp/connections` after an OAuth connection, revokes it, and the refresh grant then gives `invalid_grant`.
   - Covers the "MCP Roles" panel in the group drawer with patch 3 applied.
   - The run must have no shared-state side effects (finding 11).
3. **Real tool calls in every e2e,** through the proxy, for OAuth, API token and a non-façade OAuth application:
   - a paginated list and a detail call;
   - a refused cross-role call;
   - a write call with `MCP_READ_ONLY` off, to exercise the meta-carrying write branch (finding 17).
4. **Upstream patch 4.** Corrected README (finding 4), an API version bump, and a `ContentTypePermission` / importer assertion. Run core's `invoke dev.test` for `InvenTree`, `importer` and `users`, and the schema export, before proposing it.
5. **Visibility.**
   - "MCP Access" gating aligned with `IsStaffAdmin` (finding 8).
   - A `test_ui` case for staff without the admin role.
   - A 1.5.x test asserting that no INFO/WARNING is logged per request (finding 6).
6. **Docs.**
   - README GUI: 1.6+, patch 3, `ENABLE_PLUGINS_INTERFACE`, shadow-flag wording.
   - The AGENTS.md frontend rule and file map.
   - The three "committed bundles" statements (findings 3, 5, 7, 9).
   - `docs/OPERATIONS.md` (being written concurrently) should repeat the version and setting prerequisites.
7. **Release decision.** `MIN_VERSION` stays 1.5.0. The release notes must state that the GUI needs 1.6+ and that 1.5.x admins manage MCP roles through the plugin API only. A 1.5.x admin otherwise has no UI for the MCP roles, and new groups get no MCP access by default. Consider documenting the curl recipes for `api/rulesets/` for 1.5.x.
