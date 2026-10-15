# WP1 report: discovery and the OAuth façade

Microplan: `docs/plans/WP1-discovery-oauth-facade.md`. Branch `feature/oauth-facade`, not committed (the orchestrator commits after review).

## What was built

| File | Content |
|---|---|
| `inventree_mcp/settings.py` | `get_plugin_setting(key, default=True)` now coerces to `type(default)` (`bool` via `str2bool` for strings, `int`, `str`) and falls back to `default` when the plugin is unresolvable, the value is `None`, the lookup raises or coercion fails. Existing callers unchanged. |
| `inventree_mcp/mcp_transport.py` | Both 401 paths (`APIException` and `REQUIRE_AUTH`) go through `_auth_error_response()`, which keeps the JSON-RPC body and adds `WWW-Authenticate: discovery.challenge_header(request, invalid_token=<Authorization header present>)`. A slash-less alias route `mcp` (name `mcp-endpoint-noslash`) was added; see deviations. |
| `inventree_mcp/core.py` | `AppMixin` and `ScheduleMixin`, the 12 WP1 settings with validators and descriptions, `SCHEDULED_TASKS["cleanup_stale_clients"]` (daily), `__init__` calls `settings_inject.apply()` in a try/except, `setup_urls()` appends `auth.urls` only when `auth.facade_supported()`, and `get_well_known_urls()` adds `oauth-protected-resource` and `oauth-authorization-server` when the façade is supported. If DOT is older than 3.4, a warning is logged and the façade URLs are not registered. |
| `inventree_mcp/models.py`, `migrations/__init__.py` | Empty, so the Django app loads under `AppMixin`. |
| `auth/__init__.py` | `facade_supported()` (feature detection, cached), `oauth_enabled()`, `FacadeViewMixin` (first base of every façade view: JSON 404 while `OAUTH_ENABLED` is off, otherwise `settings_inject.ensure_applied()`). |
| `auth/urls.py` | The 8 URL names from the overview. Machine endpoints are `csrf_exempt(auth_exempt(...))`; authorize and the login bridge are `auth_exempt` only. |
| `auth/discovery.py` | `issuer()`, `resource()`, `challenge_header()`, `ProtectedResourceMetadataView`, `AuthorizationServerMetadataView` (also served as `openid-configuration`). Responses carry `ACAO: *` and `Cache-Control: max-age=300`, and OPTIONS is answered. Endpoint URLs come from `reverse()` plus `build_absolute_uri()`. |
| `auth/settings_inject.py` | `apply()`, `ensure_applied()`. Merges into `settings.OAUTH2_PROVIDER` in place, calls `oauth2_settings.reload()`, and drops every cached `_oauthlib_core` on `OAuthLibMixin` subclasses, because DOT bakes `token_expires_in` into the cached server and `reload()` alone does not reach it. Logs the effective values at INFO. |
| `auth/cors.py` | `trusted_origins()`, `TrustedOriginCORSMixin`. |
| `auth/clients.py` | `MCPRegistrationView`, `is_trusted_client()`, `is_loopback_redirect()`, `mcp_applications()`, `trusted_client_hosts()`. Registration reuses DOT's private helpers `_parse_metadata`, `_build_application_kwargs`, `_issue_registration_token`, `_application_to_response`, `_error_response` and `_validation_error_description`, with explicit imports and a comment. `test_dot_helpers_present` guards them. `_issue_registration_token`/`_application_to_response` are chained, so the code works with both the 3.4.0 (model) and 3.4.1 (raw string) return types. |
| `auth/authorize.py` | `MCPAuthorizationView(FacadeViewMixin, AuthorizationView)`, `render_error()`, `REASONS`, `human_duration()`. |
| `auth/login_bridge.py` | `MCPLoginBridgeView`, `sso_provider_id()`. Posts to `reverse("browser:socialaccount:redirect_to_provider")`, which is `/api/auth/v1/auth/provider/redirect` (verified in the container). |
| `auth/token.py` | `MCPTokenView`, `MCPRevokeView`. |
| `auth/policy.py` | `mcp_scopes_available()` (real: `g:read` + `r:view:<role>` in `RULESET_NAMES` order, plus add/change/delete when `MCP_READ_ONLY` is off, filtered to InvenTree's `SCOPES`). `effective_scopes`, `may_connect` and `narrow` match the overview, with stub bodies. |
| `auth/membership.py` | `Outcome` and the stubs as specified. `revoke_all_mcp_tokens()` is real: `RefreshToken.revoke()` for every unrevoked refresh token of a façade app, then `AccessToken.revoke()` for that user's remaining access tokens of façade apps. It returns the total number revoked. |
| `auth/tasks.py` | `cleanup_stale_clients()`. |
| `templates/inventree_mcp/` | `base.html` (self-contained, inline CSS, light and dark), `consent.html` (DOT's hidden fields, client, host and trust badge, user, scope labels, lifetimes, loopback warning, Deny and Approve with `name="allow"`), `login_bridge.html` (auto-submitting SSO form with a fallback button, or manual mode), `error.html`. |
| `tests/` | `oauth_helpers.py` (shared fixtures, not collected), `test_auth_discovery.py`, `test_auth_clients.py`, `test_auth_authorize.py`, `test_auth_token.py`, `test_auth_flow.py`, `test_settings_inject.py`: 69 tests. |
| `dev/oauth_smoke.py` | Live flow script, standard library only. Tokens are never printed. |
| `README.md`, `AGENTS.md` | "Connect with OAuth" section, settings table, 1.5.x proxy note; `auth/` file map, the DOT no-duplication rule, `dev/test.sh` usage. |

## Deviations from the microplan, with reasons

1. **DOT settings are injected only while `OAUTH_ENABLED` is on.** When it is off, `desired_values()` is `{}` and values injected earlier are restored to what InvenTree configured; a key that was originally absent is removed again. The spec calls `apply()` unconditionally. Doing that would silently change DOT's instance-wide lifetimes (access tokens from 10 h to 1 h, refresh tokens from never-expiring to 30 d) for every existing plugin user who never enables OAuth. `apply()` is still called from `__init__` and from every façade view.
2. **`DCR_REGISTRATION_TOKEN_EXPIRE_SECONDS=86400` is injected**, a key beyond the overview's list. DOT's default dates the registration access token issued with every DCR client to year 9999. With PostgreSQL and a connection time zone east of UTC (the dev instance uses `Europe/Bucharest`), reading that row fails with `DataError: timestamp too large (after year 10K)`. That broke the cascade delete in `cleanup_stale_clients`, and it would break any admin listing of tokens. The token has no use here anyway: its RFC 7592 management URI is DOT's `/o/register/<id>/`, which stays disabled. `DCR_ENABLED` and `OIDC_ISS_ENDPOINT` are still never injected.
3. **Scope narrowing on GET happens in an override of `validate_authorization_request()`**, which returns `policy.narrow(scopes, user)`, rather than as a separate statement in `get()`. DOT's own `get()`, called in step 6, re-validates the request and renders the consent form's hidden `scope` field from that result. Narrowing only inside `get()` would have shown and posted the un-narrowed scopes. The behaviour is the same as the spec's step 4, and `get()` still renders `no_mcp_permission` when the result is empty.
4. **PKCE S256 is enforced by the façade.** InvenTree 1.5.7 sets `PKCE_REQUIRED=False` (master sets `True`), so DOT does not reject a missing challenge there. `get()` sends an `invalid_request` redirect to the validated redirect URI through DOT's `error_response`, and `form_valid()` refuses a tampered `code_challenge_method` with a 400 error page. Code verification itself stays oauthlib's.
5. **Extra policy re-checks on the consent POST.** `form_valid()` also re-runs `policy.may_connect()` before narrowing. A Deny goes straight to DOT, so it still redirects with `access_denied`.
6. **Hardening not spelled out in the spec:**
   - DCR accepts only the `authorization_code` grant (+ `refresh_token`). Other grants get `invalid_client_metadata`.
   - The token endpoint answers `unsupported_grant_type` for anything other than `authorization_code` and `refresh_token`.
   - Consent is not skipped when the request carries `prompt=...`, so DOT handles it.
7. **Refresh scope source.** If the client sends `scope` on refresh, that list is narrowed. Otherwise the original scopes are taken from DOT's own `validator.get_original_scopes()`, which also covers its fallback when the access token row is gone. If no original scopes can be determined, the request goes to DOT unchanged. Narrowing an empty list would yield every effective scope, and oauthlib would then reject that as `invalid_scope`.
8. **`client_id_issued_at` and `client_secret_expires_at: 0` are added** to DOT's registration response, which emits neither. RFC 7591 and the KINA guide expect them. `name` defaults to "MCP client". `allowed_origins` comes from `TRUSTED_CLIENT_ORIGINS`.
9. **The `ScheduleMixin` path is absolute**: `inventree_mcp.auth.tasks.cleanup_stale_clients`. Dotted paths are global imports in `ScheduleMixin`; there is no form relative to the plugin. The task also calls `ensure_applied()`, so the worker process carries the façade's refresh lifetime into InvenTree's daily DOT `clear_expired()`.
10. **Slash-less MCP route alias** `mcp` → the same view (name `mcp-endpoint-noslash`). Claude Code also POSTs to `/plugin/inventree-mcp/mcp`. Without the alias that request missed the view and got InvenTree's bare `401 Unauthorized` without the challenge, as observed in the proxy log.
11. **The login bridge's invalid `next` uses the reason code `invalid_request`**, with its own sentence in `REASONS`. The spec lists no code for that case.
12. **Challenge header formatting is done in `discovery.py`.** The format is `Bearer realm="InvenTree", resource_metadata="…", scope="…"[, error="invalid_token"]`, with quoting. It does not use DOT's `www_authenticate.build_bearer_challenge`, because that helper is DOT ≥ 3.4 only and `mcp_transport.py` must keep working on older DOT.

## Test results (inside the dev instance, InvenTree 1.5.7, DOT 3.4.0)

```
$ dev/test.sh inventree_mcp
Ran 201 tests in 52.450s
OK (skipped=3)

$ dev/test.sh inventree_mcp.test_mcp
Ran 132 tests in 15.422s
OK

$ dev/test.sh inventree_mcp.tests          (the 69 new tests)
OK (skipped=3)
```

The 3 skips are `WellKnownRedirectTest`: "this InvenTree version has no /.well-known/ plugin index". The `/.well-known/<name>` plugin redirects exist only on InvenTree master (`WellKnownMixin`). The same test file runs them on master.

Lint (ruff 0.12.12, as AGENTS.md specifies; `.pre-commit-config.yaml` says v0.16.0):

```
$ ruff format --preview --check inventree_mcp   -> 50 files already formatted
$ ruff check --preview inventree_mcp            -> Found 2 errors.
```

Both errors are pre-existing E402s in `inventree_mcp/mcp_server.py`, untouched by WP1 and reproduced on an unmodified checkout of HEAD. `tests/` is excluded by the repo's ruff config. It was checked with `--isolated` and is clean.

## Smoke test (acceptance 2)

`OAUTH_ENABLED` was enabled in the dev instance through the Django shell (`dev/shell.sh`), then:

```
$ python3 dev/oauth_smoke.py
[1] POST /mcp/ without token -> 401; WWW-Authenticate: Bearer realm="InvenTree", resource_metadata="http://localhost:8080/plugin/inventree-mcp/.well-known/oauth-protected-reso...
[2] PRM -> 200; resource=http://localhost:8080/plugin/inventree-mcp/mcp/ authorization_servers=['http://localhost:8080/plugin/inventree-mcp']
[3] AS metadata -> 200; issuer=http://localhost:8080/plugin/inventree-mcp token_endpoint=http://localhost:8080/plugin/inventree-mcp/oauth/token/
[4] register -> 201; client_id=LBvfGf9QE2UGbc9c7JOOAHsXBVu1QxokLxRHI9vf
[5] authorize (anonymous) -> 302 to /plugin/inventree-mcp/oauth/login/
[5] login bridge -> 200 (sso mode), csrftoken cookie set: True
[6] allauth headless login as 'admin' -> 200
[7] authorize (signed in) -> 200, consent page shown: True
[7] approve -> 302 to http://127.0.0.1:43123/callback (code: True, state ok: True)
[8] token -> 200; token_type=Bearer expires_in=3600 access_token=<30 chars> refresh_token=<30 chars> scopes=12
[9] tools/list -> 200; 56 tools, e.g. ['describe_filters', 'make_web_link', 'list_attachments', 'get_attachment']
[10] refresh -> 200; expires_in=3600 refresh token rotated: True
[11] tools/list with refreshed token -> 200; 56 tools
[12] tools/list with the pre-refresh access token -> 401 (revoked by rotation)
OK: OAuth façade flow completed
```

The bridge rendered in SSO mode against the dev Authentik: `<form action="/api/auth/v1/auth/provider/redirect">`, `provider=authentik`, `callback_url=<absolute authorize URL>`. The script uses the admin password login for the session, as the task allowed. The full Authentik round trip through the bridge was not driven by script.

`claude mcp add --transport http inventree-dev-wp1 http://localhost:8080/plugin/inventree-mcp/mcp/`, then `claude mcp list`:

- **Directly against the dev proxy (InvenTree 1.5.7): `✘ Failed to connect — JSON Parse error: Unrecognized token '<'`.** The proxy log shows Claude Code gets the 401 challenge, fetches the PRM (200), then fetches the RFC 8414 path-insertion URL `/.well-known/oauth-authorization-server/plugin/inventree-mcp`. On 1.5.7 that URL is not plugin-servable: `AuthRequiredMiddleware` sends it to `/accounts/login/` and on to `/web`, which returns HTML with status 200. Claude Code tries to parse that HTML and stops. On InvenTree master, `WellKnownMixin` redirects this exact path to the plugin document; the plugin advertises it and the redirect tests cover it.
- **Through a throwaway Caddy on port 8081 with two rewrites** (the ones now in README under "InvenTree 1.5.x only"): `inventree-dev-wp1: http://localhost:8081/plugin/inventree-mcp/mcp/ (HTTP) - ! Needs authentication`. That is the expected acceptance result. The temporary container and the `claude mcp` entry were removed afterwards. `dev/Caddyfile` was not modified; the dev-environment owner should add the same two `rewrite` lines so the dev stack passes this check directly.

`OAUTH_ENABLED` is left on in the dev instance. The server container was restarted once to load the new route.

## Known limitations

- **InvenTree 1.5.x needs the reverse-proxy rewrite above** for Claude clients. A plugin cannot serve root `/.well-known/` paths there. The `openid-configuration` alias under the issuer covers clients that try it, but Claude Code gives up after the HTML 200.
- **Browser CORS preflights to `/plugin/` are answered by InvenTree's CORS middleware** (django-cors-headers, `CORS_URLS_REGEX` includes `/plugin/`) before any view runs. So `TrustedOriginCORSMixin` only governs simple requests and the actual responses, such as the form-encoded token POST. Registration from a browser (JSON → preflight) therefore needs `https://claude.ai` and `https://claude.com` in InvenTree's `cors.whitelist`, which the README documents. The mixin's tests call the view directly for that reason.
- **A refresh token stays usable for 120 s after being revoked or rotated.** DOT's `REFRESH_TOKEN_GRACE_PERIOD_SECONDS` (120, as the overview prescribes) also applies to tokens revoked through `/oauth/revoke/` or by reuse protection. After that window the token is refused and reuse revokes the whole family; the tests show both.
- **The `registration_client_uri` in the DCR response points at DOT's `/o/register/<id>/`**, which answers 404 because `DCR_ENABLED` stays off. This is DOT's shape and is kept as specified. RFC 7592 client management is not offered.
- **Access tokens issued with a `resource` parameter (as Claude sends) are audience-bound by DOT to the MCP URL.** They are rejected on `/api/...`; that is intended. Tokens without `resource` also work on the REST API (tested).
- **Lifetimes are instance-wide while `OAUTH_ENABLED` is on**, a DOT limitation.
- **The bridge's manual mode cannot resume the flow.** Core drops `next` on its login page, so the user has to reconnect from the client.
- **`ensure_applied()` reads about 8 plugin settings** (a DB query each) per façade request and per 401 challenge. That is acceptable for these low-traffic endpoints.
- **`sso_provider_id()` requires exactly one matching allauth app** when `SSO_PROVIDER_ID` is empty; with several, it returns `None` and the bridge uses manual mode.

## What WP2 and WP3 must know

Call sites (replace bodies in `policy.py` and `membership.py`, not these calls):

- **`auth/token.py`, `MCPTokenView.post()`** dispatches on `grant_type` before `super().post()`:
  - `_check_code_grant()` loads the `Grant` by `code` (and `application__client_id` when posted), then calls **`membership.token_grant_allowed(grant.user)`**. `(False, code)` → `400 {"error": code or "invalid_grant"}`.
  - `_check_refresh_grant()` loads the `RefreshToken` the way DOT's validator does (checksum, unrevoked first). Revoked or unknown tokens go straight to DOT (grace period, reuse protection). Otherwise:
    1. **`membership.refresh_grant_allowed(rt.user)`** runs. This is where WP2 runs `recheck()` when due. A refusal returns `invalid_grant` before DOT runs: nothing is rotated or revoked.
    2. **`policy.narrow(requested, rt.user)`** runs, where `requested` is the posted `scope` or DOT's original scopes. Empty → `rt.revoke()` + `invalid_grant`; otherwise the narrowed `scope` is written into a copied `request.POST`.
  - WP3's "reduced ruleset takes effect at refresh" works through `policy.effective_scopes()`.
- **`auth/authorize.py`, `MCPAuthorizationView`:**
  - `get()` first calls **`policy.may_connect(request.user)`**. WP2's `membership.is_blocked()` feeds it with reason `blocked`; WP3's "no MCP ruleset" should return `(False, "no_mcp_permission")` from there.
  - `validate_authorization_request()` applies **`policy.narrow(scopes, user)`**.
  - `form_valid()` re-runs `may_connect` and `narrow` on the posted form.
  - All refusals go through `render_denied(reason)` → `error.html`, 403. `REASONS` in `authorize.py` holds the sentences; add new codes there.
- **`policy.mcp_scopes_available()`** is already real and is used by the PRM, the AS metadata and the challenge's `scope`. WP3 should keep it as the advertised list.
- **`membership.revoke_all_mcp_tokens(user)`** is real: it covers DCR and CIMD applications via `clients.mcp_applications()` and returns the number of tokens revoked. CIMD applications created through DOT's own `/o/` endpoints also count as MCP applications, since CIMD is enabled instance-wide while the façade is on.
- `membership.block_user()` raises `NotImplementedError`; `unblock_user()` is a no-op.
- **`auth/tasks.py`** is the place for `membership_sweep` (WP2); add it to `SCHEDULED_TASKS` in `core.py` with the absolute dotted path.
- **`sso_provider_id(request)` in `login_bridge.py`** resolves the allauth provider and accepts `request=None` for use without a request. WP2's re-check should reuse it to find the `SocialApp`/`SocialToken`.
- **Models:** `models.py` and `migrations/` exist and are empty, and `AppMixin` is active. WP2 and WP3 add `MCPUserState` and `MCPRuleSet` plus migrations there.
- **Tests:** `tests/oauth_helpers.OAuthFacadeTestMixin` enables the plugin and `OAUTH_ENABLED`, restores DOT's settings in `tearDownClass`, and provides `register()`, `obtain_code()`, `exchange_code()`, `refresh()` and `mcp_call()`. `tests/test_auth_flow.py` is the end-to-end regression test to keep green.
