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
- `settings.py` - `get_plugin_setting()` for this plugin's own settings (`REQUIRE_AUTH`,
  `MCP_READ_ONLY`).
- `mcp_server.py` - the `MCPServer` instance; imports `tools/*` for their `@mcp.tool()` side effect.
- `mcp_transport.py` - Django view bridging Streamable HTTP onto the MCP server.
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
- `models.py`, `migrations/` - plugin models (empty until WP2; `AppMixin` needs them to exist).
- `templates/inventree_mcp/` - server-rendered OAuth pages: `base.html`, `consent.html`,
  `login_bridge.html`, `error.html`.
- `auth/__init__.py` - `facade_supported()` (DOT >= 3.4 feature detection), `oauth_enabled()`,
  `FacadeViewMixin` (404 while `OAUTH_ENABLED` is off, then `settings_inject.ensure_applied()`).
- `auth/urls.py` - façade URL patterns (`oauth-*`, `.well-known/*` names under
  `plugin:inventree-mcp:`).
- `auth/discovery.py` - protected-resource and authorization-server metadata views, `issuer()`,
  `resource()`, `challenge_header()` (the `WWW-Authenticate` value used by `mcp_transport.py`).
- `auth/settings_inject.py` - merges CIMD, lifetimes and refresh-token reuse protection into
  `settings.OAUTH2_PROVIDER` at runtime (`apply()`, `ensure_applied()`); restores InvenTree's
  values while the façade is off.
- `auth/cors.py` - `trusted_origins()`, `TrustedOriginCORSMixin` for register/token/revoke.
- `auth/clients.py` - `MCPRegistrationView` (DOT's DCR helpers + rate limit),
  `is_trusted_client()`, `is_loopback_redirect()`, `mcp_applications()`.
- `auth/authorize.py` - `MCPAuthorizationView` (DOT `AuthorizationView` + policy, consent skip,
  error page), `render_error()`.
- `auth/login_bridge.py` - `MCPLoginBridgeView`, `sso_provider_id()`: SSO via allauth headless and
  back to the authorize URL.
- `auth/token.py` - `MCPTokenView` (membership gate + scope re-narrowing before DOT),
  `MCPRevokeView`.
- `auth/policy.py` - `mcp_scopes_available()`, `effective_scopes()`, `may_connect()`, `narrow()`
  (stub bodies until WP3).
- `auth/membership.py` - membership re-check API (stubs until WP2) and `revoke_all_mcp_tokens()`.
- `auth/tasks.py` - `cleanup_stale_clients()` (daily, via `ScheduleMixin`).
- `tests/` - OAuth façade tests (`test_auth_*.py`, `test_settings_inject.py`, shared
  `oauth_helpers.py`); `test_mcp.py` holds the original suite.
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

`--preview` matches `.pre-commit-config.yaml`, which pins `ruff==0.12.0`.
