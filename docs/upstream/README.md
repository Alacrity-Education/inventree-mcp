# Upstream patches for InvenTree core

Five small patches against InvenTree core `master` (base commit `2f139f94b`, 2026-10-15), prepared but **not submitted** to inventree/InvenTree. The stacked branch is pushed to the fork `Alacrity-Education/InvenTree` (`upstream-patches`), where core's QC, Frontend, Import/Export and Migration workflows run on it. Patches 1 to 4 each retire a workaround in the `inventree-mcp` plugin; patch 5 fixes a core regression the plugin's CI found on `latest`. They are independent and can be proposed as five separate PRs (per-patch branches `upstream/<name>` in the worktree, PR texts in `pr/`).

Source: local branch `upstream-patches` in the worktree `/home/alex.lucaci/Projects/inventree/.worktrees/upstream-patches` (clone `/home/alex.lucaci/Projects/inventree/InvenTree`, branch `master` untouched). Patch files (from `git format-patch master..upstream-patches`) are in this directory:

- `0001-Allow-OAUTH2_PROVIDER-options-to-be-set-via-config.y.patch`
- `0002-Preserve-next-through-account_login-and-honour-it-in.patch`
- `0003-Render-plugin-panels-in-the-group-and-user-drawers-e.patch`
- `0004-Accept-any-one-role-s-scope-for-tables-shared-by-sev.patch`
- `0005-Expire-model-meta-caches-after-a-plugin-registry-rel.patch`

Each applies cleanly (`git apply --check` and `git am`) on a fresh checkout of `master` at `2f139f94b`. Note: the plan named the files `0001-oauth2-provider-config-override.patch` etc.; format-patch derives names from the commit subjects.

---

## 1. OAUTH2_PROVIDER overrides from config

**Motivation.** `OAUTH2_PROVIDER` in `settings.py` is a fixed dict. Deployments cannot set django-oauth-toolkit options (`DCR_ENABLED`, `CIMD_ENABLED`, token lifetimes, `ALLOW_LOCALHOST_LOOPBACK`, ...) without a core change.

**What it changes.**
- `InvenTree/config.py`: new `merge_oauth2_provider_settings(base, overrides)`. Returns a new dict, `overrides` win, and logs only the sorted key names (never values, which could be secrets).
- `InvenTree/settings.py`: after the hard-coded dict, `OAUTH2_PROVIDER = merge_oauth2_provider_settings(OAUTH2_PROVIDER, get_setting('INVENTREE_OAUTH2_PROVIDER', 'oauth2_provider', None, typecast=dict))`. `SCOPES`, `OIDC_ENABLED`, `OIDC_RSA_PRIVATE_KEY` and `PKCE_REQUIRED` stay as in core unless explicitly overridden. The env var is JSON.
- `config_template.yaml`: commented example. `docs/docs/start/config.md`: new "OAuth2 Provider Options" section with the `configsetting` table row.
- Test: `InvenTree/tests.py::test_oauth2_provider_override` (merge precedence, no mutation of the base, key-only logging, JSON env via `get_setting`).

**Retiring the plugin workaround.** `auth/settings_inject.py` (runtime injection of DOT settings, `ensure_applied()` on each request) becomes unnecessary: the plugin docs instead tell admins to set `INVENTREE_OAUTH2_PROVIDER='{"DCR_ENABLED": true, "CIMD_ENABLED": true, ...}'`. Keep the injector as a fallback for older InvenTree versions (gate on a version check), then delete it when the minimum supported version includes the patch.

**Verification performed.**
- `uvx ruff@0.16.0 check` on `config.py`, `settings.py`, `tests.py`: pass.
- The merge helper and `get_setting(..., typecast=dict)` JSON path were exercised in a standalone Python run (merge result, key-only log line).
- NOT run: the Django test suite (no InvenTree Python environment available here), so `test_oauth2_provider_override` is written but unexecuted. Also not run: `mkdocs` build for the new docs section.

---

## 2. Preserve `next` through `account_login` and honour it in the React login

**Motivation.** django-oauth-toolkit's authorize view (and anything using `LoginRequiredMixin`) sends anonymous browsers to `/accounts/login/?next=...`. InvenTree maps that route (`account_login`) to a `RedirectView` that drops the query string, and the React login only honours router state, so the OAuth flow cannot resume after login.

**What it changes.**
- `InvenTree/urls.py`: `RedirectView.as_view(..., query_string=True)` on `account_login`. Test `test_auth.py::TestAuth::test_account_login_preserves_next`.
- `src/frontend/src/functions/auth.tsx`:
  - `sanitizeNextPath()`: accepts only same-origin paths; rejects absolute URLs to other hosts, `//` and `/\` prefixes, scheme URLs, and values that normalize to a protocol-relative path (e.g. `/..//evil.com`).
  - `getNextPath(redirect)`: reads `next` from the current URL (`/web/login?next=`, `/web/logged-in?next=`) or from the router state's `queryParams` (the `/accounts/login/?next=` -> `/web/?next=` -> `ProtectedRoute` -> `/logged-in` path already carries the query string there).
  - `followRedirect()` (the existing mechanism, reused by password login, `checkLoginState`, the `?login=&password=` flow and the MFA path) now prefers a valid `next`: paths under `/<base>/` (default `/web/`) go through the router, anything else uses `window.location.assign`.
  - `keepNextState()` keeps `next` when navigating between `/logged-in`, `/login` and `/provider-signup`.
  - `ProviderLogin(provider, process, redirect)`: the SSO `callback_url` becomes `/web/logged-in?next=...` when a `next` exists (login process only). `SSOButton` passes the router state.
- `LoggedIn.tsx`: preserves `next` when bouncing to `/login` after an SSO error. `Login.tsx` needed no change: its `followRedirect` calls pick up `next` automatically.

**Retiring the plugin workaround.** The login bridge's manual mode (the plugin-served login page that stashes the authorize URL and resumes it itself) and the forced-SSO assumption (that the only way to resume is a single SSO provider redirect started by the bridge) can go. With this patch the plugin just redirects an anonymous authorize request to `/accounts/login/?next=<authorize url>` for any login method (password, SSO, MFA). Keep the manual mode behind a capability check until the minimum InvenTree version includes the patch.

**Verification performed.**
- `uvx ruff@0.16.0 check` on `urls.py`, `test_auth.py`: pass.
- Frontend: dependencies installed with `npm install --legacy-peer-deps` (see note below); `npx tsc -p tsconfig.json --noEmit`: pass; `npx @biomejs/biome@1.9.4 check` (version pinned in `.pre-commit-config.yaml`) on the changed files: pass.
- The sanitizer logic was run in node against `//evil.com`, `/\evil.com`, `https://evil.com`, `javascript:`, `/..//evil.com`, tab-injected and normal paths; the `/..//evil.com` case was found and fixed.
- NOT run: Django tests, Playwright UI tests, a manual browser run. The end-to-end behaviour (authorize -> login -> back to authorize) still needs a browser check against the dev stack.

Caveat to mention in the PR: when frontend and backend are on different origins (vite dev server), a `next` outside `/web/` resolves against the frontend origin.

---

## 3. Plugin panels in the group and user drawers; `RoleTable` in the UI library

**Motivation.** `GroupDrawer` and `UserDrawer` (settings -> Users / Groups) have no plugin injection point, although the backend already accepts `target_model` `group` / `user` for UI features. The MCP plugin needs a second permission table (MCP roles) inside the group editor.

**What it changes.**
- `GroupTable.tsx` / `UserTable.tsx`: `usePluginPanels({ model: ModelType.group | ModelType.user, id, instance, reloadFunc: refreshInstance })` (called before the early returns, so hook order is stable) and each panel is rendered as an extra `Accordion.Item` after the existing ones, using `panel.content`, which is the same `PluginPanelContent` element `PanelGroup` renders.
- `RoleTable` moved from `src/components/items/` to `lib/components/RoleTable.tsx` and exported from `lib/index.ts` (with `RuleSet`). New optional props: `endpoint` (default `ApiEndpoints.ruleset_list`), `onSave` (override; receives the edited rulesets) and `api` (Axios instance for the default save). `api` is needed because the library must not import the app's `../../App`; core call sites (`GroupDrawer`, `GroupDetail`) pass `api`. Plugins pass `context.api`. Behaviour for core is unchanged. The two `Trans` usages became `t` template strings (the library only uses `@lingui/core/macro`).
- `lib` `CHANGELOG.md` entry.

**Retiring the plugin workaround.** The plugin's standalone groups page (MCP roles per group) becomes optional: the `Panel.js:renderMCPRolesPanel` entry point lights up inside the group drawer once the plugin's `get_ui_panels` returns it for `target_model == 'group'`. The plugin can then pass its own `endpoint` / `onSave` to the exported `RoleTable`. Keep the standalone page for InvenTree versions without the patch.

**Verification performed.**
- `npx tsc -p tsconfig.json --noEmit` and `npx tsc -p tsconfig.lib.json --noEmit`: pass. `npx @biomejs/biome@1.9.4 check` on changed files: pass (import ordering fixed with `--write`).
- NOT run: `npm run lib` build, Playwright tests, visual check of the drawers. Locale `.po` files still reference the old `RoleTable.tsx` path; they are regenerated by `lingui extract` and were not touched.

---

## 4. Any one role's scope for tables shared by several rulesets (`map_scope`)

**Motivation.** Several tables belong to more than one ruleset: `part_part` to *part* and *build*, `stock_stockitem` to *stock* and *build*, `company_company` / `company_contact` / `company_address` to *purchase_order*, *sales_order* and *return_order*, `company_supplierpart` / `company_manufacturerpart` to *part* and *purchase_order*, and a few more. The role check for a request (`users.permissions.check_user_permission`) accepts **any** of the table's roles. The OAuth2 scope check required **all** of them: `InvenTree/permissions.py` `map_scope(roles=...)` returned

```python
return [[get_granular_scope(action, table) for table in roles]]
```

one alternative holding every role's scope (an AND; the loop variable was even named `table` while it holds role names). A token issued with exactly the scopes the *Parts* role grants (`r:view:part`) could therefore not list parts unless it also held `r:view:build`. Found in the WP3 review of the plugin (decision 1).

**What it changes.**
- `InvenTree/permissions.py` `map_scope()`: `return [[get_granular_scope(action, role)] for role in roles]`, one alternative per role, which `TokenMatchesOASRequirements` treats as OR. The docstring says so. The `only_read`, `map_read` and `OPTIONS` paths are unchanged.
- `map_scope(roles=...)` has **three** callers on `master`, and the patch changes all three:
  1. `InvenTreeRoleScopeMixin.get_required_alternate_scopes` (`roles=` the rulesets of the view's table): every endpoint of a multi-role table. This is the intended fix.
  2. `DataImporterPermission.get_required_alternate_scopes` (`roles=_roles`, all eleven roles, `map_read=SAFE_METHODS`, `override_all_actions='change'`): the data importer endpoints `importer/api.py` (`DataImportSessionList` / `DataImportSessionDetail` at `/api/importer/session/` and `/api/importer/session/<pk>/`, `DataImportSessionAcceptRows` at `.../accept_rows/`, `DataImportColumnMappingList` / `Detail` at `/api/importer/column-mapping/`, `DataImportRowList` / `Detail` at `/api/importer/row/`). Reads stay `g:read`. Writes (POST, PUT, PATCH, DELETE) needed the `r:change:<role>` scope of **all eleven** roles; after the patch **any one** `r:change:<role>` passes the scope check.
  3. `ContentTypePermission.get_required_alternate_scopes` (`roles=_roles`): `GenericMetadataView` and `SimpleGenericMetadataView` (`InvenTree/api.py`), i.e. `/api/metadata/<model>/<pk>/` and `/api/metadata/<model>/<lookup_field>/<lookup_value>/`, which the per-model `.../metadata/` paths (`meta_path()`, 34 uses) redirect to. Every method needed the scope of all eleven roles (`r:view:*` for GET, `r:change:*` for PUT/PATCH); after the patch any one role's scope passes.
  
  For 2 and 3 the object-level checks still run unchanged (`DataImporterPermission.has_object_permission`: `check_user_permission(user, model, 'change')` for writes; `ContentTypePermission.has_object_permission`: `obj.check_permission` or `check_user_permission`). Those check the *user's roles*, not the token's scopes. So after the patch a token holding one role's scope (e.g. `r:view:part`) can read the metadata of an object of another role (e.g. a stock item) when the user holds that other role interactively, and a token with one `r:change:<role>` scope can drive an import into any model the user may change. Before the patch these endpoints were effectively closed to every token that did not carry all eleven roles' scopes. This is the same "any one role" rule the role check applies, but it is a relaxation of the token boundary on two more endpoint families and must be stated in the PR. (Maintainers may prefer to keep the all-roles requirement for these two classes by giving them their own `map_scope` variant; the plugin does not depend on either endpoint family.)
- Tests: `InvenTree/tests.py::OAuth2ScopeMapTest` (DOT's `TokenMatchesOASRequirements` with in-memory tokens):
  - `test_map_scope_any_role`: every method of a two-role table, a single role unchanged, `OPTIONS` still `g:read`;
  - `test_multi_role_table_accepts_any_role_scope`: `PartList`'s required scopes contain `[r:view:part]` and `[r:view:build]`; a token with either scope passes, `r:view:stock` is refused;
  - `test_content_type_permission_accepts_any_role_scope`: one alternative per role for GET and PATCH; `r:view:stock` passes GET, `r:change:part` passes PATCH, `g:read` alone and a view scope on PATCH are refused;
  - `test_data_importer_permission_accepts_any_role_scope`: GET needs `g:read`; every write method lists `r:change:<role>` per role; `r:change:part` passes POST, `r:change:sales_order` passes DELETE, `r:add:part` and `g:read` are refused for writes.
- API version: `InvenTree/api_version.py` bumped to **v558** with a text entry. The OpenAPI security requirements of all these endpoints change from one requirement holding every role's scope to one requirement per role (`InvenTree/schema.py` turns each alternative into a requirement). Core's `qc_checks.yaml` diffs the exported schema against the published schema of the current API version and fails without a bump. The entry links `https://github.com/inventree/InvenTree/pull/XXXXX`: replace the number when the PR is opened, and renumber if `master` has moved past 556.

**Retiring the plugin workaround.** `oauth2_bridge._any_role_alternatives` (called only from `scoped_view_class`) splits every all-role-scope alternative into one alternative per scope, which is exactly what the patched `map_scope` returns. Once the minimum supported InvenTree version includes the patch, drop `_any_role_alternatives` and use `get_required_alternate_scopes(...)` as it is (until then it is a no-op on patched cores: an alternative of one scope is left as it is). Its test `test_policy.TransportNarrowingTest.test_multi_role_tables_accept_any_role_scope` keeps passing either way. The other two bridge workarounds (core bugs 1 and 2 in the `oauth2_bridge.py` docstring: the raw `TokenMatchesOASRequirements()` in `check_oauth2_authentication` and the broken `super().has_permission` fallback) are not covered by these patches.

**Verification performed.**
- `uvx ruff@0.16.0 check` on `permissions.py`, `tests.py` and `api_version.py`: pass; `ruff format --diff` shows no change in the added code (only the pre-existing differences noted below).
- The four tests of `OAuth2ScopeMapTest` were run in the dev stack's InvenTree 1.5.7 server container (`manage.py shell`, the patched `map_scope` executed into `InvenTree.permissions`; `map_scope`, `DataImporterPermission`, `ContentTypePermission` and `_roles` are identical in 1.5.7 and `master`): `Ran 4 tests ... OK`. Against the unpatched function all four fail.
- The four patches apply in sequence onto `master` at `9b8fdeb06` (`git apply --cached` into a scratch index) and the result is tree-identical to `upstream-patches`; each also passes `git apply --check` on its own.
- NOT run: core's test suite (`invoke dev.test` for `InvenTree`, `importer`, `users`) and the schema export diff; both need a `master` development environment. Run them before proposing the patch.

---

## 5. Expire model meta caches after a plugin registry reload

**Motivation.** Found by the plugin's CI on `inventree/inventree:latest` (master after PR #13007, 2026-10-13): two tests deleting a `Group` failed with `IntegrityError ... inventree_mcp_mcpruleset ... violates foreign key constraint` at the end of the transaction. After a registry reload, `Group._meta.get_fields()` no longer lists the plugin's foreign key (`mcp_rule_sets`), so Django's deletion collector never cascades into the plugin's table and the deferred database constraint fails the commit. `apps.clear_cache()` brings the relation back.

**Cause.** `reload_plugins()` re-populates the apps registry (`registry._reload_apps`) while `apps.ready` is False, so the `Apps.clear_cache()` calls inside `populate()` skip the per-model cache expiry. Before #13007 this did not matter: `AppMixin._reregister_contrib_apps()` reloaded the plugin's models module *after* the registry was ready, and the `register_model()` calls expired the caches. Since #13007 the plugin package is purged from `sys.modules` on every collect, so the models module is imported inside `populate()` instead, `app_config.models` is already filled and `_reregister_contrib_apps()` reloads nothing: core models keep relation trees built before the reload.

**What it changes.**
- `plugin/registry.py` `_reload_apps()`: `apps.clear_cache()` after the registry is populated again (both the `populate` and the `set_installed_apps` paths).
- Test `plugin/test_plugin.py::RegistryTests::test_reload_expires_model_meta_caches`: the relation tree cached on `Group` before a `reload_plugins(full_reload=True, force_reload=True, collect=True)` is not the one in place afterwards.

**Plugin side.** `auth/signals.py` no longer relies on the cascade at all: the Group and User `pre_delete` receivers delete the plugin's rows themselves (`tests/test_policy.py::DeletionCleanupTest` hides the relation the way the stale cache does). The plugin's test package also disables the purge for the test run (`tests/__init__.py`), because every `unittest.mock.patch` target misses once the test modules and the code under test hold different module objects; that is a test-ergonomics concern worth raising with the PR (core's own regression test for #13007 relies on the purge, so a plain `PLUGIN_TESTING` gate is not the answer).

**Verification performed.** The regression test and `plugin.test_plugin` were run in the `inventree/inventree:latest` image (`dev/test-image.sh`), with the fix (pass) and without (the new test fails). Also NOT run: the rest of core's suite; the fork's QC workflow runs it on the stacked branch.

---

## Environment notes (all patches)

- The frontend uses `yarn.lock`; `yarn`/`corepack` are not installed here and `npm ci` is impossible (no `package-lock.json`). `npm install` fails with an ERESOLVE peer conflict (`@codecov/vite-plugin` vs `vite@7`), so `--legacy-peer-deps` was used; resolved versions may differ slightly from `yarn.lock`. `yarn.lock` and `package-lock.json` were not committed.
- Python: `ruff@0.16.0` flags pre-existing formatting differences in `urls.py` and `tests.py` (the repo uses a different ruff pin), so only `ruff check` was used and no reformatting was applied.

## Upstream PR checklist (per patch)

- [ ] Open a discussion/issue first (InvenTree prefers this for new settings and UI features); link the plugin use case.
- [ ] Target `master`; one PR per patch; PR template filled in; a title in the project's style.
- [ ] Docs updated: patch 1 has `docs/docs/start/config.md` and `config_template.yaml`; patch 2 needs a short note on `next` handling in the authentication/SSO docs; patch 3 needs a note in the plugin UI panel docs (`docs/docs/plugins/mixins/ui.md`) that `group` and `user` are valid panel targets.
- [ ] Tests: patch 4 `OAuth2ScopeMapTest`; patch 1 `test_oauth2_provider_override`; patch 2 `test_account_login_preserves_next` plus a Playwright test for the `next` flow (login with `/web/?next=/api-doc/` style target); patch 3 a Playwright test with a mock plugin panel in the group drawer (the `inventree-plugin-creator` demo plugin can provide it). Run `invoke dev.test` for the touched backend apps in a proper environment.
- [ ] `pre-commit run --all-files` (ruff, biome) and the frontend `yarn build`/`lib` build.
- [ ] Run `invoke dev.translate` / `lingui extract` only if maintainers ask; otherwise leave locales to the translation workflow.
- [ ] API version bump: patch 4 bumps to v558 (schema security requirements change); fill in the PR number in `api_version.py` and renumber if `master` has moved. Patches 1 to 3 and 5 need none. Frontend library version/changelog: patch 3 adds an entry.
- [ ] Patch 4: list the importer and generic metadata endpoints (above) in the PR text as a deliberate relaxation, or narrow the patch to `InvenTreeRoleScopeMixin` if maintainers object.
- [ ] Squash/reword the commits if requested; remove the `Co-Authored-By` trailers if the project policy requires.
