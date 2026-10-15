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

- `context.py` - contextvar carrying the authenticated Django user into tool execution.
- `proxy.py` - `call_view()`, the permission boundary described above.
- `settings.py` - `get_plugin_setting(key, default)` for this plugin's own settings (coerced to
  `type(default)`, fail-safe; read from the database while the registry is still loading),
  `plugin_setting(key)` / `plugin_setting_list(key)` with the defaults declared in `core.SETTINGS`.
- `mcp_server.py` - the `MCPServer` instance; imports `tools/*` for their `@mcp.tool()` side effect.
- `mcp_transport.py` - Django view bridging Streamable HTTP onto the MCP server; 401 with the OAuth
  challenge; `policy.may_connect()` for every credential (blocked: 401, inactive / outside
  `MCP_REQUIRED_GROUPS`: 403); for API-token and Basic requests a background re-check and
  `membership.long_lived_credential_allowed()` (401 past the grace / re-login window).
- `oauth2_bridge.py` - lets `call_view()` present an OAuth2-authenticated request's real token to
  the proxied view, and works around upstream InvenTree OAuth2 scope-enforcement bugs.
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
  DOT settings at load.
- `models.py`, `migrations/` - plugin models: `MCPUserState` (per-user membership check stamp and
  MCP block). Generate migrations inside the dev container: `dev/manage.sh makemigrations
  inventree_mcp` (fixes file ownership); `migrations/` is excluded from ruff.
- `templates/inventree_mcp/` - server-rendered OAuth pages: `base.html`, `consent.html`,
  `login_bridge.html`, `error.html`.
- `auth/__init__.py` - `facade_supported()` (DOT >= 3.4 feature detection), `oauth_enabled()`,
  `FacadeViewMixin` (`settings_inject.ensure_applied()`, then 404 while `OAUTH_ENABLED` is off),
  the one-time warning when InvenTree 1.5.x needs the proxy rewrites.
- `auth/urls.py` - façade URL patterns (`oauth-*`, `.well-known/*` names under
  `plugin:inventree-mcp:`).
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
- `auth/clients.py` - `MCPRegistrationView` (DOT's DCR helpers + rate limit),
  `is_trusted_client()`, `is_loopback_redirect()`, `mcp_applications()`.
- `auth/authorize.py` - `MCPAuthorizationView` (DOT `AuthorizationView` + policy, consent skip,
  error page), `render_error()`.
- `auth/login_bridge.py` - `MCPLoginBridgeView`: SSO via allauth headless and back to the authorize
  URL.
- `auth/sso.py` - `sso_app()`, `sso_provider_id()`: the allauth app used for MCP sign-in and the
  membership re-check (`SSO_PROVIDER_ID` or the single `openid_connect` app).
- `auth/token.py` - `MCPTokenView` (dead refresh tokens sorted out first, then the membership gate,
  then scope re-narrowing, all before DOT), `MCPRevokeView`.
- `auth/policy.py` - `mcp_scopes_available()`, `effective_scopes()` (empty outside
  `MCP_REQUIRED_GROUPS`), `may_connect()` (blocked, `in_required_groups()` / `MCP_REQUIRED_GROUPS`),
  `narrow()` (`effective_scopes`/`narrow` are stub bodies until WP3).
- `auth/membership.py` - membership re-check against the SSO provider: `recheck()`,
  `refresh_grant_allowed()`, `token_grant_allowed()`, `recheck_due()`, `is_blocked()`,
  `block_user()`, `unblock_user()`, `revoke_all_mcp_tokens()`. Reuses allauth (the upstream refresh
  token stays in `SocialToken`, `OpenIDConnectOAuth2Adapter` for endpoints and client auth, jwtkit for
  the ID token) and core's `InvenTree.sso.ensure_sso_groups` for the group sync; never compare raw
  claim strings. Identity refusals (inactive, outside `MCP_REQUIRED_GROUPS`, upstream
  `invalid_grant`) block; a `no_mcp_permission` from `policy.may_connect()` only revokes the MCP
  tokens (`Outcome.NO_MCP_PERMISSION`). The grace and re-login predicates (`within_grace()`,
  `within_relogin()`) are shared by `refresh_grant_allowed()` and `long_lived_credential_allowed()`.
  Every outbound call has a 5 s timeout, a provider is left alone for 60 s after a timeout /
  connection error / 5xx; re-checks are serialised by a row lock on `MCPUserState`.
- `auth/signals.py` - allauth `user_logged_in`: SSO logins stamp `membership_checked_at` and lift a
  block only when the user is active and in `MCP_REQUIRED_GROUPS`, other logins record
  `last_login_at` (connected from the plugin's `__init__`).
- `auth/tasks.py` - `cleanup_stale_clients()` (daily), `membership_sweep()` (hourly, 60 s budget,
  then a continuation task with the remaining user ids), both via `ScheduleMixin`; `recheck_user()` (queued by `mcp_transport.py` for API-token/Basic requests).
- `tests/` - OAuth façade tests (`test_auth_*.py`, `test_settings_inject.py`, shared
  `oauth_helpers.py`), membership re-checks (`test_membership.py`, a fake OIDC provider at the
  `requests` boundary); `test_mcp.py` holds the original suite.
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
dev/oauth_smoke.py                          # live OAuth flow against http://localhost:8080
dev/membership_e2e.py                       # live membership re-checks against the dev Authentik
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

CI (`.github/workflows/ci.yaml`) runs `ruff check` + a build check, then a matrix job against the
real `inventree/inventree:stable` and `:latest` Docker images with Postgres.

## Lint/format

```
ruff check --preview inventree_mcp
ruff format --preview inventree_mcp
```

`--preview` matches `.pre-commit-config.yaml`, which pins ruff `v0.16.0` (CI installs the latest
ruff). Run the pinned version with `uvx ruff@0.16.0 check --preview inventree_mcp` and
`uvx ruff@0.16.0 format --preview inventree_mcp`. `tests/` is excluded by the repo config; check it
with `--isolated`.
