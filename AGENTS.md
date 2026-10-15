# AGENTS.md

InvenTree plugin (entry point `inventree_mcp.core:InvenTreeMCP`) exposing an MCP (Model Context
Protocol) server over Streamable HTTP so MCP clients can query InvenTree data. Only runs installed
inside a real InvenTree instance - there is no standalone dev server for this repo.

## Core design rule

Tool code (`inventree_mcp/tools/*.py`) must never touch the Django ORM directly. Every tool calls
`proxy.call_view()`, which dispatches through the *real* InvenTree DRF view class as the
authenticated caller, so permission checks, filtering, and serialization all run exactly as they do
for the normal REST API. New tools should wrap an existing (or new) API view via `call_view()`
rather than reimplementing filtering/serialization by hand.

`call_view()` also enforces the `MCP_READ_ONLY` plugin setting (default `True`): any non-GET call
raises `ToolError` unless an administrator has turned it off. This is a shared chokepoint - don't
duplicate the check per-tool or bypass `call_view()`.

## OAuth façade rule

`inventree_mcp/auth/` turns the plugin into an OAuth authorization server for its MCP endpoint by
**subclassing and calling django-oauth-toolkit (DOT)**, never by re-implementing it. OAuth state
lives in DOT's models (`Application`, `Grant`, `AccessToken`, `RefreshToken`); PKCE, code issuance,
client authentication and token rotation are DOT/oauthlib. Do not copy DOT view bodies; override
the smallest hook and call `super()`. The policy entry points `policy.*` and `membership.*` are
called from fixed places (`authorize.py`, `token.py`, `discovery.py`); later work changes their
bodies, not their call sites. Never log or return tokens, secrets or e-mail addresses (log user ids
and reason codes). Every façade view is `auth_exempt` (InvenTree's `AuthRequiredMiddleware` 401s
`/plugin/` paths otherwise) and the machine endpoints are `csrf_exempt`.

## File map

- `context.py` - contextvar carrying the authenticated Django user into tool execution, with the
  token whose scopes bound the request and `request_meta` (the MCP request's resolved host, scheme,
  `REMOTE_ADDR`, `Accept-Language`; `get_current_request_meta()`).
- `proxy.py` - `call_view()`, the permission boundary described above. Its factory requests carry
  `get_current_request_meta()`, so paginated views build `next` links with the real host instead of
  `testserver` (which fails `ALLOWED_HOSTS` on a real instance).
- `settings.py` - `get_plugin_setting(key, default)` for this plugin's own settings (coerced to
  `type(default)`, fail-safe; read from the database while the registry is still loading),
  `plugin_setting(key)` / `plugin_setting_list(key)` with the defaults declared in `core.SETTINGS`.
- `mcp_server.py` - the `MCPServer` instance; imports `tools/*` for their `@mcp.tool()` side effect.
- `mcp_transport.py` - Django view bridging Streamable HTTP onto the MCP server; 401 with the OAuth
  challenge; `policy.may_connect()` for every credential (blocked: 401, inactive / outside
  `MCP_REQUIRED_GROUPS` / no MCP role: 403), computed once per request with the user's MCP scopes
  (`policy.connect_scopes()`). Every credential is cut to those scopes with an *unsaved* DOT
  `AccessToken`: an OAuth token of any application to its scopes ∩ them with its own expiry
  (`narrowed_oauth_token()`), API-token and Basic requests to them (`scoped_token_for()`, after a
  background re-check and `membership.long_lived_credential_allowed()`, 401 past the grace /
  re-login window); an empty scope is a 403. So proxied views enforce the current MCP roles like an
  OAuth token, and a group change applies at the next request. `request_meta()` is bound for the
  proxy. Basic only with `MCP_ALLOW_BASIC_AUTH` (`get_authenticators()`).
- `oauth2_bridge.py` - lets `call_view()` present the bound token (the transport's unsaved token:
  the narrowed OAuth2 token, or the scoped token for API-token / Basic requests) to the proxied
  view, and works around upstream InvenTree OAuth2 scope-enforcement bugs, including splitting
  multi-role scope requirements into "any one role's scope" (as core's role check accepts any one
  role; `_any_role_alternatives` retires with `docs/upstream/` patch 4).
- `tools/` - one module per resource, each a thin async wrapper around `call_view()`;
  `discovery.py` holds `describe_filters()`.
- `schema_introspection.py` / `output_schemas.py` - derive each tool's MCP `outputSchema` from the
  real DRF serializer.
- `filter_introspection.py` - derives each list tool's `filters` argument from a view's real
  `filterset_class`/`search_fields`/`ordering_fields`.
- `expand_introspection.py` - derives optional output-expansion flags (e.g. `part_detail`) from a
  view's real `output_options`.
- `core.py` - the `InvenTreePlugin` subclass: settings (incl. the OAuth ones), URL wiring (the
  OAuth façade only when `auth.facade_supported()`), well-known entries, scheduled tasks; injects
  DOT settings at load. UI (`UserInterfaceMixin`): `get_ui_routes()` (pages `groups` and
  `connections`), `get_ui_navigation_items()` (only when `UI_ROUTES_SUPPORTED`, i.e. the host has
  plugin routes, InvenTree 1.6+), `get_ui_panels()` (the "MCP Roles" panel for
  `target_model == "group"`). The groups page, its nav item and the panel are offered only to
  `_is_staff_admin()` (`policy.is_staff_admin()`, the condition of the plugin's staff API);
  `_ui_source()` returns None when a bundle is missing from `static/`. Both the missing-build
  WARNING and the "no plugin routes" INFO go through `_log_once()` (once per process: the web UI
  asks for UI features on every page load).
- `models.py`, `migrations/` - plugin models: `MCPUserState` (per-user membership check stamp,
  MCP block and the re-check lease `recheck_lease_until`, `0003`), `MCPRuleSet` (WP3: the MCP roles, core's `RuleSet` shape, one row per group and role;
  `0002` also creates the rows of existing groups). Never filter core models through the plugin's
  reverse relations (`Group.objects.filter(mcp_rule_sets__…)`, `User.objects.filter(mcp_state__…)`):
  they raise `FieldError` in running processes; query `MCPRuleSet.objects.filter(group__in=…)` /
  `MCPUserState.objects.filter(user=…)` or use `Exists`. Generate migrations inside the dev container: `dev/manage.sh makemigrations
  inventree_mcp` (fixes file ownership); `migrations/` is excluded from ruff.
- `templates/inventree_mcp/` - server-rendered OAuth pages: `base.html`, `consent.html`,
  `login_bridge.html`, `error.html`.
- `static/` - **generated** by the frontend build (`frontend/`), git-ignored
  (`inventree_mcp/.gitignore`); never edit it by hand. Shipped in the wheel.
- `frontend/` - React + Mantine sources of the plugin pages and the group panel (see "Frontend"
  below and `frontend/README.md`): `src/Groups.tsx`, `src/Connections.tsx`, `src/Panel.tsx`
  (entry points), shared code in `src/lib/` (`api.ts`, `RoleGrid.tsx`, `McpRolesEditor.tsx`,
  hooks, vitest files), `scripts/check-bundles.mjs`.
- `auth/__init__.py` - `facade_supported()` (DOT >= 3.4 feature detection), `oauth_enabled()`,
  `FacadeViewMixin` (`settings_inject.ensure_applied()`, then 404 while `OAUTH_ENABLED` is off),
  the one-time warning when InvenTree 1.5.x needs the proxy rewrites.
- `auth/urls.py` - façade URL patterns (`oauth-*`, `.well-known/*` names under
  `plugin:inventree-mcp:`) and the permission API (`api-*`).
- `auth/api.py` - DRF API of the MCP permission set (rulesets list / detail / copy / clear / revoke,
  effective, connections, status). Reuses core's `RuleSetSerializer`, `check_user_role`,
  `get_user_roles`, the fixed OAuth scope check from `oauth2_bridge.py` and DOT's `revoke()`; staff
  endpoints need staff + the admin role's change permission (InvenTree's middleware refuses OAuth
  tokens on these paths with 401; the views additionally require `a:staff`). The effective document
  is per role and carries a `note` on tables shared by several roles (access is per table).
- `auth/discovery.py` - protected-resource metadata (subclass of DOT's
  `OAuthProtectedResourceMetadataView`) and authorization-server metadata views, `issuer()`,
  `resource()`, `challenge_header()` (DOT's `build_bearer_challenge`; the `WWW-Authenticate` value
  used by `mcp_transport.py`).
- `auth/settings_inject.py` - merges CIMD, lifetimes and refresh-token reuse protection into
  `settings.OAUTH2_PROVIDER` at runtime (`apply()`, `ensure_applied()`); restores InvenTree's
  values while the façade is off. Applied at plugin load (settings read from the database), before
  every request (`request_started`) and task (`pre_execute`) via `connect_signals()`, and by every
  façade view and challenge; desired values memoised for a few seconds, dropped on `PluginSetting`
  saves.
- `auth/cors.py` - `trusted_origins()`, `TrustedOriginCORSMixin` for register/token/revoke.
- `auth/ratelimit.py` - the one fixed-window limiter per client IP (`retry_after()`,
  `too_many_requests()`: 429 with `Retry-After`), used by registration
  (`REGISTER_RATE_LIMIT_PER_MINUTE`) and the token endpoint's authorization_code grant
  (`TOKEN_RATE_LIMIT_PER_MINUTE`). Do not add a second limiter.
- `auth/clients.py` - `MCPRegistrationView` (DOT's DCR helpers + `ratelimit`),
  `is_trusted_client()`, `is_loopback_redirect()`, `mcp_applications()`.
- `auth/authorize.py` - `MCPAuthorizationView` (DOT `AuthorizationView` + policy, consent skip,
  error page), `render_error()`.
- `auth/login_bridge.py` - `MCPLoginBridgeView`: SSO via allauth headless and back to the authorize
  URL.
- `auth/sso.py` - `sso_app()`, `sso_provider_id()`: the allauth app used for MCP sign-in and the
  membership re-check (`SSO_PROVIDER_ID` or the single `openid_connect` app).
- `auth/token.py` - `MCPTokenView` (authorization_code grants rate limited per IP; dead refresh
  tokens sorted out first, then the membership gate, then scope re-narrowing, all before DOT;
  refresh grants are not rate limited), `MCPRevokeView`.
- `auth/policy.py` - `mcp_scopes_available()`, `effective_scopes()` (union of the groups' MCP
  rulesets as core's granular scopes, cut to the advertised ones so `MCP_READ_ONLY` strips writes,
  plus `g:read`; empty outside `MCP_REQUIRED_GROUPS`; everything for superusers with
  `MCP_SUPERUSER_FULL_ACCESS`), `may_connect()` (inactive, blocked, `in_required_groups()`, then
  `no_mcp_permission` when no scope is left), `connect_scopes()` (may_connect plus the scopes in one
  pass, for the transport and the status / effective API), `narrow()`, `mcp_roles()`,
  `is_staff_admin()` (staff + the admin role's change permission; `api.IsStaffAdmin` and the UI
  gating in `core.py`).
- `auth/membership.py` - membership re-check against the SSO provider: `recheck()`,
  `refresh_grant_allowed()`, `token_grant_allowed()`, `recheck_due()`, `is_blocked()`,
  `block_user()`, `unblock_user()`, `revoke_all_mcp_tokens()`, `mcp_token_querysets()` (the live
  façade tokens of a set of users; shared by revocation and `signals.live_mcp_token_count()`).
  Reuses allauth (the upstream refresh token stays in `SocialToken`, `OpenIDConnectOAuth2Adapter`
  for endpoints and client auth, jwtkit for the ID token) and core's `InvenTree.sso.ensure_sso_groups` for the group sync; never compare raw
  claim strings. Identity refusals (inactive, outside `MCP_REQUIRED_GROUPS`, upstream
  `invalid_grant`) block; a `no_mcp_permission` from `policy.may_connect()` only revokes the MCP
  tokens (`Outcome.NO_MCP_PERMISSION`). The grace and re-login predicates (`within_grace()`,
  `within_relogin()`) are shared by `refresh_grant_allowed()` and `long_lived_credential_allowed()`.
  Every outbound call has a 5 s timeout per socket operation and a 15 s total deadline (the body is
  streamed), a provider is left alone for 60 s after a timeout /
  connection error / 5xx. Re-checks are serialised by a lease, not by a lock held across the
  network: a short `select_for_update` transaction takes `MCPUserState.recheck_lease_until`
  (120 s) and commits; discovery, the lease re-check (`_assert_lease()`: lost means
  `_Unreachable("lease_lost")`, nothing redeemed), redeem, `_store_tokens()` (the rotated upstream token is durable
  at once), verification and userinfo run in autocommit; a second short transaction does the group
  sync, the classification, the stamp and the block / revocation; the lease is released in
  `finally`. A re-check finding a live lease returns `NOT_DUE` without calling out (internally
  `_LEASE_HELD`, so that `refresh_grant_allowed()` applies the grace / re-login windows to it). Never move an
  outbound call back inside a transaction.
- `auth/signals.py` - allauth `user_logged_in`: SSO logins stamp `membership_checked_at` and lift a
  block only when the user is active and in `MCP_REQUIRED_GROUPS`, other logins record
  `last_login_at`. MCP rulesets: `ensure_rulesets()` / `ensure_rulesets_for(group_ids)` (Group
  `post_save` and the API; takes pks; one SELECT and one bulk INSERT for any number of groups), revocation of
  the group's members on a reduction (flag true -> false, row or group deleted) through
  `membership.revoke_all_mcp_tokens()` only (never a block) through `tasks.revoke_mcp_tokens`
  (inline up to 20 members, a background task above). All connected from the plugin's `__init__`.
- `auth/tasks.py` - `cleanup_stale_clients()` (daily), `membership_sweep()` (hourly, 60 s budget,
  then a continuation task with the remaining user ids), both via `ScheduleMixin`; `recheck_user()` (queued by `mcp_transport.py` for API-token/Basic requests); `revoke_mcp_tokens()` (queued by a ruleset reduction of a large group).
- `tests/` - OAuth façade tests (`test_auth_*.py`, `test_settings_inject.py`, shared
  `oauth_helpers.py` with `grant_mcp()`; the façade mixin grants the test group every MCP role
  unless a class sets `mcp_roles`), membership re-checks (`test_membership.py`, a fake OIDC provider
  at the `requests` boundary), the MCP permission set (`test_policy.py`, `test_api.py`), the UI
  features (`test_ui.py`: routes, navigation and panel per user kind incl. staff without the admin
  role, the once-per-process logging, the UI feature API; needs the built bundles);
  `test_mcp.py` holds the original suite.
- `dev/ui_smoke.py` - live browser check of the plugin pages (playwright) against the dev stack,
  on a scratch group; needs a host with plugin routes (InvenTree 1.6+ / `latest`), exits 3 on
  1.5.x.
- `server_card.py` - serves an MCP Server Card (SEP-2127) at `plugin/inventree-mcp/server-card/`
  for discovery, advertised under `/.well-known/` via `core.py`'s `get_well_known_urls()`.
- `_well_known_compat.py` - `WellKnownMixin` import, falling back to a blank mixin on InvenTree
  versions predating `inventree/InvenTree#12698` (not yet in a "stable" release as of writing).

## Testing

With the docker dev environment (`dev/`, see `dev/README.md`), from this repo:

```
dev/up.sh                                   # once: InvenTree + Authentik + demo data
dev/test.sh                                 # whole package (test_mcp.py + tests/)
dev/test.sh inventree_mcp.tests.test_auth_flow
dev/test.sh inventree_mcp.test_mcp          # the original suite
dev/test-image.sh up latest && dev/test-image.sh test   # the suite in another InvenTree image (CI's `latest` = master)
dev/oauth_smoke.py                          # live OAuth flow against http://localhost:8080
dev/membership_e2e.py                       # live membership re-checks against the dev Authentik
dev/permissions_e2e.py                      # live MCP permission set (parts-only MCP role) as e2e-member
dev/ui_smoke.py                             # live plugin pages in a headless browser (1.6+ host)
```

Inside the InvenTree devcontainer instead:

```
# from this repo, inside the InvenTree devcontainer:
./run_tests.sh                                            # full suite, --keepdb
./run_tests.sh inventree_mcp.test_mcp.MCPTransportTest     # one class
```

Equivalent by hand:

```
pip install -e dev/InvenTreeMCP --no-deps
invoke dev.test -r inventree_mcp.test_mcp.MCPToolPermissionTest
```

CI (`.github/workflows/ci.yaml`) runs ruff, the frontend job (`npm ci`, lint, vitest, build,
`check:bundles`) and `python -m build` with a wheel check (templates and bundles inside), then a
matrix job against the real `inventree/inventree:stable` and `:latest` Docker images with
Postgres.

Mocks and InvenTree 1.6+: core (PR #13007) purges and re-imports the plugin's modules on every
registry collect, and the test runner collects for every test class. `tests/__init__.py` disables
that purge for the test run; without it every `unittest.mock.patch` target misses (the test
modules keep the old module objects, the code under test lazily imports the fresh ones). Core's
`AppMixin` still re-executes `models.py` on every reload on all versions, so a test module's model
class is never the registry's: compare models by `_meta.label`, not identity, and never rely on
`on_delete=CASCADE` from a core model into a plugin table (`auth/signals.py` deletes the plugin's
rows itself on `pre_delete`). In `test_mcp.py`, patch the module objects it imported
(`patch.object(view_resolution, ...)`), not dotted paths.

## Frontend

The plugin pages ("MCP Access" for staff admins, "MCP" for everybody) and the "MCP Roles" group
panel are React + Mantine in `frontend/`, built with Vite against `@inventreedb/ui`:

```
cd frontend
npm ci && npm run build      # writes ../inventree_mcp/static/ (Groups.js, Connections.js, Panel.js + chunks)
npm run lint && npm test     # biome, vitest
npm run check:bundles        # renders every built bundle with stub host globals
```

- `inventree_mcp/static/` is generated and git-ignored. Never edit it by hand; rebuild. Build it
  before `python -m build` (the wheel must contain it) and before `dev/test.sh` (`test_ui.py`
  checks the bundles exist). `dev/up.sh` rebuilds it when stale.
- React, ReactDOM, Mantine and Lingui are externals provided by the host (`vite.config.ts`).
- The backend decides visibility (`core.py`); the bundles only render. Pages and nav items need
  plugin routes (InvenTree 1.6+); on 1.5.x only the panel API exists and `UI_ROUTES_SUPPORTED` is
  False.
- Lesson: the Python `.gitignore` template's `lib/` rule silently ignored `frontend/src/lib/`
  (shared code never committed, fresh clones could not build). The rule is anchored (`/lib/`)
  now; after adding a directory, check `git status --ignored` / `git check-ignore -v <path>`.

## Lint/format

```
ruff check --preview inventree_mcp
ruff format --preview inventree_mcp
```

`--preview` matches `.pre-commit-config.yaml`, which pins ruff `v0.16.0`; CI pins the same
version (`RUFF_VERSION: "0.16.0"` in `ci.yaml`). Run the pinned version with `uvx ruff@0.16.0 check --preview inventree_mcp` and
`uvx ruff@0.16.0 format --preview inventree_mcp`. `tests/` is excluded by the repo config; check it
with `--isolated`.
