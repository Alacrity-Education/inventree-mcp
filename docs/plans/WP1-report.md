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

## Review fixes applied

Applies `docs/plans/WP1-review.md` (F1 to F17, Duplication, 1.5.x discovery, Test quality, Next-stage readiness). Not committed.

### Findings

| Finding | What changed (file: function) |
|---|---|
| **F1** (blocker) | `auth/token.py`: `MCPTokenView._check_refresh_grant` now runs `membership.refresh_grant_allowed()` for revoked rows too. A revoked row goes to DOT only when it was rotated, i.e. a successor access token exists (`AccessToken.objects.filter(source_refresh_token=rt).exists()`), so DOT re-serves the pair it already issued. A revoked row with no successor inside the grace window gets `invalid_grant` (`_revoked_within_grace()` uses DOT's own `REFRESH_TOKEN_GRACE_PERIOD_SECONDS` test). One case still goes to DOT: a revoked row past the grace window. DOT refuses it there and applies reuse protection to the family. That keeps the family revocation when an old rotated token is replayed after its successor was itself rotated, because the successor access token is gone by then. Unknown original scopes now get `invalid_grant` instead of being passed through. New `_scope_list()` drops the `''` that `scope_to_list('')` yields. The test's `_expire_grace_period()` call that hid the bug is removed. |
| **F2** | `auth/authorize.py`: `MCPAuthorizationView.get` forces `approval_prompt=force` on a copy of `request.GET` before DOT's `get()`. This covers both the query parameter and an instance-wide `REQUEST_APPROVAL_PROMPT=auto`. Consent is now skipped only by `_may_skip_consent()`. |
| **F3** | `settings.py: get_plugin_setting` reads the stored value (`_stored_value()`: `PluginSetting` of an active `PluginConfig`) while the registry has not loaded the plugin yet. So `core.InvenTreeMCP.__init__ -> settings_inject.apply()` now injects at start-up in every process. `auth/__init__.py: FacadeViewMixin.dispatch` and `auth/discovery.py: challenge_header` call `ensure_applied()` before the enabled check, so turning the façade off restores DOT's values. `auth/settings_inject.py: connect_signals()` is called from the plugin's `__init__` and is idempotent through `dispatch_uid`. It connects a django-q `pre_execute` receiver (`_before_task` -> `ensure_applied()`), which runs in the worker process before every task, including InvenTree's daily `run_oauth_maintenance`. |
| **F4** | Both `http_method_names` are now tuples (`discovery._MetadataResponseMixin`, `login_bridge.MCPLoginBridgeView`). The `noqa` at `core.py` `__init__` and at `tests/test_auth_authorize.py` `assertErrorPage` is gone. `AGENTS.md` lint section: pin corrected to ruff v0.16.0 (CI installs the latest), with the `uvx` commands. ruff 0.17 (CI) flagged `delattr(cls, "_oauthlib_core")` in `settings_inject._reset_oauthlib_cores`; it is now `del cls._oauthlib_core`. CI runs `ruff check` at the repo root, so `dev/` must pass too. Its import-sort, `noqa` and `if-exp` findings are fixed in `dev/authentik/sso_smoke.py`, `dev/authentik/bootstrap.py` and `dev/configure_inventree.py`. |
| **F5** | `auth/discovery.py: ProtectedResourceMetadataView(FacadeViewMixin, _MetadataResponseMixin, OAuthProtectedResourceMetadataView)` overrides `get_resource`, `get_authorization_servers`, `get_scopes_supported`, `get_resource_name` and `get_resource_documentation`. `_MetadataResponseMixin` adds `Cache-Control` and the `OPTIONS` answer. The import is guarded, because `mcp_transport.py` imports `discovery` on any DOT version. The AS metadata view stays hand-built (docstring says why). |
| **F6** | `auth/discovery.py: challenge_header` uses DOT's `build_bearer_challenge(request, oauth2_error={"scope": …[, "error": "invalid_token"]}, realm="InvenTree", resource_metadata_url=…)`. It is imported lazily inside the enabled branch. The bare `Bearer realm="InvenTree"[, error="invalid_token"]` stays as the fallback when the façade is off. `_quote` is removed. DOT joins parameters with `,` (no space). |
| **F7** | README prerequisites: the proxy must replace `X-Forwarded-For` (Caddy does by default; nginx needs `$remote_addr`, not `$proxy_add_x_forwarded_for`), and gunicorn must not be reachable directly. The optional code change (keying on the last hop) was not made. |
| **F8** | `auth/token.py: _check_code_grant` / `_check_refresh_grant` return `None` (DOT alone) unless the grant's or refresh token's application is in `clients.mcp_applications()` (`_is_mcp_application()`). |
| **F9** | `authorize.get_context_data` adds `client_registered_dynamically`. `consent.html` shows a "self-registered, not verified" badge for DCR clients. |
| **F10** | `authorize.get` accepts only `discovery.resource(request)` and its slash-less form (`_allowed_resources()`). Anything else gets an `invalid_target` redirect to the validated redirect URI, with `state`. `form_valid` refuses a tampered hidden `resource` with a 400 error page, not a redirect, because the posted redirect URI is not validated at that point. |
| **F11** | `auth/cors.py: TrustedOriginCORSMixin.dispatch` uses `patch_vary_headers(response, ["Origin"])`. |
| **F12** | `settings.py`: `setting_default(key)` (from `InvenTreeMCP.SETTINGS`), `plugin_setting(key)`, `plugin_setting_list(key)`. Used by `clients.trusted_client_hosts`, `cors.trusted_origins` and `settings_inject.desired_values`, which now calls `clients.trusted_client_hosts()`. `DEFAULT_TRUSTED_HOSTS`, `DEFAULT_TRUSTED_ORIGINS` and `_split_hosts` are removed. |
| **F13** | `core.py SETTINGS`: `[int, MinValueValidator(1)]` for the three lifetimes and `UNUSED_CLIENT_RETENTION_DAYS`; `MinValueValidator(0)` for `REGISTER_RATE_LIMIT_PER_MINUTE`. |
| **F14** | `auth/tasks.py: cleanup_stale_clients` docstring now says the registration token expires (injected 86400 s) and that the worker sync comes from the `pre_execute` hook. |
| **F15** | `core.py: get_well_known_urls` lists the OAuth documents for the index (`request` given) only while `oauth_enabled()`. The redirect patterns (`request=None`) are unchanged. |
| **F16** | `settings_inject`: `desired_values()` is memoised per process for `DESIRED_VALUES_TTL_SECONDS` (5 s) in `_memoised_desired_values()`. `invalidate()` clears the memo on `post_save` of `plugin.PluginSetting` / `plugin.PluginConfig` (lazy senders, connected in `connect_signals()`). Anonymous 401s no longer read the settings on every request. |
| **F17** | Informational. README now documents that `openid-configuration` is an RFC 8414 alias without the OIDC-only fields, and that WP2 needs app integration (`ENABLE_PLUGINS_APP`). The CRLF to LF conversion of `core.py` from WP1 should be mentioned in the commit body; no other files were converted. |
| **1.5.x discovery** | README has a new subsection, "InvenTree 1.5.x: reverse-proxy rewrites for discovery". It gives Caddy and nginx rewrites for the `oauth-authorization-server`, `openid-configuration` and `oauth-protected-resource/.../mcp` path-insertion URLs, plus the alias caveat. `auth/__init__.py: warn_if_discovery_needs_proxy()` logs one WARNING per process at the first façade request when `OAUTH_ENABLED` is on and `_well_known_compat.NATIVE_WELL_KNOWN` is false (new flag). The `OAUTH_ENABLED` setting description and the README settings table mention the rewrites. |
| **Duplication (tests)** | `tests/oauth_helpers.py`: `mcp_call(access_token=None, method, path, **headers)` replaces `test_auth_flow._mcp` and `test_auth_discovery._post`. `consent_form_data(page)` replaces the three copies. `make_token_pair(app, user, *, expired, refresh, scope)` replaces `test_auth_token._pair` and `test_auth_clients._token`. `setUp` also calls `settings_inject.invalidate()`, because a test's rollback sends no signal. |
| **Duplication (dev)** | `dev/oauth_smoke.py` now uses `Browser`, `browse(stop=...)` and `absolute` from `dev/authentik/sso_smoke.py`; its own `Session`/`NoRedirect` are gone. A new `--sso USER` mode submits the login bridge's SSO form (POST `provider/redirect`, `callback_url` = authorize URL), drives Authentik with `browse()`, and lands back on the authorize URL with a session. The admin password mode is still the default. Both modes now also check the RFC 8414 path-insertion URL, `approval_prompt=auto` (consent still shown), revocation, and an immediate refusal of the revoked refresh token. |
| **dev/** | `dev/Caddyfile` has the rewrites from README. I added three lines: the `openid-configuration` one besides the two asked for. `dev/entrypoint.sh` has a `fix_checkout_ownership()` function. It runs after `pip install -e`, again after `invoke update`, and in a detached loop (every 10 s for 5 min) after start. The earlier single `find`/`chown` did not cover the later `plugins.txt` installs (`invoke update`, and InvenTree's registry at start-up): those wrote `inventree_mcp.egg-info/PKG-INFO` as root, and I reproduced this after a restart. |

### New and changed tests (69 -> 96 in `inventree_mcp/tests/`)

- `test_auth_token.py`:
  - `test_revoke_endpoint_revokes` now asserts a 400 `invalid_grant` immediately after `/oauth/revoke/`, with no `_expire_grace_period()`.
  - New: `test_refresh_after_revoke_all_mcp_tokens_is_refused` (the WP2 `block_user` path), `test_membership_gate_runs_for_rotated_tokens_too`, `test_unknown_original_scope_is_refused`, `test_empty_scope_parameter_keeps_the_original_scope`, `test_other_applications_are_left_to_dot` (F8).
  - The rotation test also asserts DOT's in-grace re-serve of the same pair.
- `test_auth_flow.py`: asserts refresh-token rotation and that the old access token gets 401. Then it revokes through `revocation_endpoint` and asserts that MCP answers 401 and that the revoked refresh token gets 400 immediately.
- `test_auth_authorize.py`:
  - `test_approval_prompt_auto_does_not_skip_consent` (a DCR client with an https redirect and a loopback DCR client, each holding a live token).
  - `test_resource_must_be_the_mcp_endpoint`, `test_tampered_resource_on_consent_is_refused`, `test_dynamically_registered_client_is_marked_unverified`, `test_tampered_redirect_uri_is_never_redirected_to`, `test_tampered_client_id_cannot_take_this_redirect_uri`, `test_plain_pkce_redirects_with_invalid_request`.
  - Login bridge `test_disabled_facade_answers_404`.
  - New `SsoProviderTest`: the real `sso_provider_id()` / `_sso_app()` against allauth's app list, with settings-backed apps through `override_settings(SOCIALACCOUNT_PROVIDERS=…)`. It covers a single app (with and without a request), `LOGIN_ENABLE_SSO` off, two apps being ambiguous, `SSO_PROVIDER_ID` choosing one, a configured id that does not exist, no provider, and the bridge using the real provider.
- `test_settings_inject.py`:
  - `test_disabled_facade_view_restores_dot_settings`, `test_desired_values_are_memoised_between_saves` (F16).
  - New `FreshProcessTest`. It resets `_last_applied` and DOT's settings, then covers plugin `__init__` while the registry holds no instance (`test_plugin_init_injects_while_the_registry_is_loading`), an inactive plugin injecting nothing, and `test_scheduled_task_sees_the_facade_values` (django-q `pre_execute`).
- `test_auth_discovery.py`: `test_disabled_facade_restores_dot_settings_through_the_challenge`, plus new `WellKnownAdvertisementTest` (F15).

The F1 tests were checked against the old behaviour: with the revoked-row branch short-circuited to DOT, `test_revoke_endpoint_revokes`, `test_refresh_after_revoke_all_mcp_tokens_is_refused` and `test_auth_flow` fail. With the fix they pass.

### Verification

Tests (InvenTree 1.5.7, DOT 3.4.0, after the final restart):

```
$ dev/test.sh inventree_mcp
Ran 228 tests in 72.345s
OK (skipped=3)          (the three WellKnownRedirectTest skips: no /.well-known/ plugin index on 1.5.7)

$ dev/test.sh inventree_mcp.test_mcp
Ran 132 tests in 19.611s
OK
```

Lint:

```
$ uvx ruff@0.16.0 check --preview inventree_mcp                 -> All checks passed!
$ uvx ruff@0.16.0 format --preview --check inventree_mcp        -> 50 files already formatted
$ uvx ruff@0.16.0 check --preview --isolated inventree_mcp/tests -> All checks passed!
$ uvx ruff@0.16.0 format --preview --isolated --check inventree_mcp/tests -> 8 files already formatted
$ uvx ruff@0.16.0 check [--preview]  (repo root, as CI)          -> All checks passed!
$ uvx ruff@0.17.0 check [--preview]  (repo root, CI's current)   -> All checks passed!
```

Rewritten discovery URL through the dev proxy, after `dev/compose.sh restart proxy`:

```
$ curl -i http://localhost:8080/.well-known/oauth-authorization-server/plugin/inventree-mcp
HTTP/1.1 200 OK
Access-Control-Allow-Origin: *
Cache-Control: max-age=300
Content-Type: application/json
Via: 1.1 Caddy
{"issuer": "http://localhost:8080/plugin/inventree-mcp", "authorization_endpoint": "http://localhost:8080/plugin/inventree-mcp/oauth/authorize/", ...}
/.well-known/openid-configuration/plugin/inventree-mcp           -> 200 application/json
/.well-known/oauth-protected-resource/plugin/inventree-mcp/mcp   -> 200 application/json
/.well-known/oauth-protected-resource/plugin/inventree-mcp/mcp/  -> 200 application/json
```

Fresh worker process (`dev/compose.sh exec -T inventree-worker python src/backend/InvenTree/manage.py shell -c "…"`, `OAUTH_ENABLED` on):

```
A fresh process, after plugin load (__init__): REFRESH_TOKEN_EXPIRE_SECONDS=2592000 ACCESS_TOKEN_EXPIRE_SECONDS=3600 CIMD_ENABLED=True REFRESH_TOKEN_REUSE_PROTECTION=True
B reset to InvenTree's DOT values: REFRESH_TOKEN_EXPIRE_SECONDS=None ACCESS_TOKEN_EXPIRE_SECONDS=36000 CIMD_ENABLED=False REFRESH_TOKEN_REUSE_PROTECTION=False
C after django-q pre_execute: REFRESH_TOKEN_EXPIRE_SECONDS=2592000 ACCESS_TOKEN_EXPIRE_SECONDS=3600 CIMD_ENABLED=True REFRESH_TOKEN_REUSE_PROTECTION=True
```

In the review, line A read `None / 36000 / False`. A real task executed by the running qcluster (`async_task('builtins.eval', …)` reading `oauth2_settings` inside the task):

```
WORKER task (pid, REFRESH_TOKEN_EXPIRE_SECONDS, ACCESS_TOKEN_EXPIRE_SECONDS, CIMD_ENABLED) = (149, 2592000, 3600, True) ; shell pid 434
```

The server log shows the 1.5.x hint once per process:

```
WARNING inventree-mcp: OAuth sign-in is on, but this InvenTree version cannot serve /.well-known/oauth-authorization-server/plugin/inventree-mcp; add the reverse-proxy rewrites from the plugin README …
```

Smoke test, password mode (`python3 dev/oauth_smoke.py`):

```
[1] POST /mcp/ without token -> 401; WWW-Authenticate: Bearer realm="InvenTree",scope="g:read r:view:admin r:view:part_category r:view:part r:view:bom r:view:stock_location r:...
[2] PRM -> 200; resource=http://localhost:8080/plugin/inventree-mcp/mcp/ authorization_servers=['http://localhost:8080/plugin/inventree-mcp']
[3] AS metadata at /.well-known/oauth-authorization-server/plugin/inventree-mcp -> 200 application/json
[3] AS metadata -> 200; issuer=http://localhost:8080/plugin/inventree-mcp token_endpoint=http://localhost:8080/plugin/inventree-mcp/oauth/token/
[4] register -> 201; client_id=<40 chars>
[5] authorize (anonymous) -> 302 to /plugin/inventree-mcp/oauth/login/
[5] login bridge -> 200 (sso mode), csrftoken cookie set: True
[6] allauth headless login as 'admin' -> 200
[7] authorize (signed in) -> 200, consent page shown: True
[7] approve -> 302 to http://127.0.0.1:43123/callback (code: True, state ok: True)
[8] token -> 200; token_type=Bearer expires_in=3600 access_token=<30 chars> refresh_token=<30 chars> scopes=12
[9] tools/list -> 200; 56 tools, e.g. ['describe_filters', 'make_web_link', 'list_attachments', 'get_attachment']
[9b] authorize with approval_prompt=auto -> 200 (consent shown again: True)
[10] refresh -> 200; expires_in=3600 refresh token rotated: True
[11] tools/list with refreshed token -> 200; 56 tools
[12] tools/list with the pre-refresh access token -> 401 (revoked by rotation)
[13] revoke refresh token -> 200
[13] tools/list with the revoked pair's access token -> 401
[14] refresh with the just-revoked refresh token -> 400 invalid_grant
OK: OAuth façade flow completed (password login)
```

Smoke test, SSO mode (`python3 dev/oauth_smoke.py --sso e2e-member`). Steps 1 to 5 and 7 to 14 are as above, all OK. The SSO part:

```
[6] bridge form: POST /api/auth/v1/auth/provider/redirect provider=authentik process=login callback_url=http://localhost:8080/plugin/inventree-mcp/oauth/authorize/
[6] provider redirect -> 302 to http://authentik.localtest.me:19000/application/o/authorize/
[6] Authentik as 'e2e-member' -> back on /plugin/inventree-mcp/oauth/authorize/ (page, 200)
[7] authorize (signed in) -> 200, consent page shown: True
...
[14] refresh with the just-revoked refresh token -> 400 invalid_grant
OK: OAuth façade flow completed (SSO as e2e-member)
```

`python3 dev/authentik/sso_smoke.py e2e-member` still passes after the import tidy-up (4 × PASS). After restarting the containers, nothing in the checkout outside `dev/data/` is owned by root. `inventree_mcp.egg-info/PKG-INFO` is `2017:2017`.

### Deferred or not done

- **F7, optional code part** (rate-limit key from the last `X-Forwarded-For` hop): not done. The README now documents the proxy requirement, and that is the fix the review asks for. Keying on a header hop the plugin cannot verify would add its own spoofing questions.
- **Duplication, optional refactor of `MCPRegistrationView.post`** (inject `DCR_REGISTRATION_PERMISSION_CLASSES` and call `super().post()`): not done. The review rates the current form acceptable, and the refactor would add one more instance-wide DOT setting.
- **F1 item 5** (report upstream to DOT that the grace period applies to revoked tokens, not only rotated ones): not done from here. It is worth filing with the WP6 upstream work.
- Nothing else was deferred. F8, F9, F10, F11, F13, F15 and F16 were each well under half an hour and are all done.
