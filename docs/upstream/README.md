# Upstream patches for InvenTree core

Three small patches against InvenTree core `master` (base commit `9b8fdeb06`), prepared but **not submitted**. Each retires a workaround in the `inventree-mcp` plugin. They are independent and can be proposed as three separate PRs.

Source: local branch `upstream-patches` in the worktree `/home/alex.lucaci/Projects/inventree/.worktrees/upstream-patches` (clone `/home/alex.lucaci/Projects/inventree/InvenTree`, branch `master` untouched). Patch files (from `git format-patch master..upstream-patches`) are in this directory:

- `0001-Allow-OAUTH2_PROVIDER-options-to-be-set-via-config.y.patch`
- `0002-Preserve-next-through-account_login-and-honour-it-in.patch`
- `0003-Render-plugin-panels-in-the-group-and-user-drawers-e.patch`

Each applies cleanly (`git apply --check` and `git am`) on a fresh checkout of `master` at `9b8fdeb06`. Note: the plan named the files `0001-oauth2-provider-config-override.patch` etc.; format-patch derives names from the commit subjects.

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

## Environment notes (all patches)

- The frontend uses `yarn.lock`; `yarn`/`corepack` are not installed here and `npm ci` is impossible (no `package-lock.json`). `npm install` fails with an ERESOLVE peer conflict (`@codecov/vite-plugin` vs `vite@7`), so `--legacy-peer-deps` was used; resolved versions may differ slightly from `yarn.lock`. `yarn.lock` and `package-lock.json` were not committed.
- Python: `ruff@0.16.0` flags pre-existing formatting differences in `urls.py` and `tests.py` (the repo uses a different ruff pin), so only `ruff check` was used and no reformatting was applied.

## Upstream PR checklist (per patch)

- [ ] Open a discussion/issue first (InvenTree prefers this for new settings and UI features); link the plugin use case.
- [ ] Target `master`; one PR per patch; PR template filled in; a title in the project's style.
- [ ] Docs updated: patch 1 has `docs/docs/start/config.md` and `config_template.yaml`; patch 2 needs a short note on `next` handling in the authentication/SSO docs; patch 3 needs a note in the plugin UI panel docs (`docs/docs/plugins/mixins/ui.md`) that `group` and `user` are valid panel targets.
- [ ] Tests: patch 1 `test_oauth2_provider_override`; patch 2 `test_account_login_preserves_next` plus a Playwright test for the `next` flow (login with `/web/?next=/api-doc/` style target); patch 3 a Playwright test with a mock plugin panel in the group drawer (the `inventree-plugin-creator` demo plugin can provide it). Run `invoke dev.test` for the touched backend apps in a proper environment.
- [ ] `pre-commit run --all-files` (ruff, biome) and the frontend `yarn build`/`lib` build.
- [ ] Run `invoke dev.translate` / `lingui extract` only if maintainers ask; otherwise leave locales to the translation workflow.
- [ ] API version bump: not needed (no API change). Frontend library version/changelog: patch 3 adds an entry.
- [ ] Squash/reword the commits if requested; remove the `Co-Authored-By` trailers if the project policy requires.
