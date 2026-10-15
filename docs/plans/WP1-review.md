# WP1 review: discovery and OAuth façade

Commit `6375ace` on `feature/oauth-facade` (diff `1bf973b..6375ace`). Read-only review. Spec: `docs/plans/WP1-discovery-oauth-facade.md`. Binding interfaces: `docs/plans/00-overview.md`. Implementer report: `docs/plans/WP1-report.md`. I ran the test suites and `dev/oauth_smoke.py`, and I ran live probes against `http://localhost:8080`. The probe scripts are in the session scratchpad. They are not part of the repo.

## Verdict

**Accept with fixes.** Structure, call sites, URL names, settings keys and signatures match the overview. The DOT reuse is mostly right. Four things must be fixed before WP2 starts:

- **Blocker:** revocation can be undone with the revoked refresh token (F1).
- **Major:** `approval_prompt=auto` skips consent for loopback and DCR clients (F2).
- **Major:** DOT settings are injected lazily per process and never at start-up (F3). Refresh-token expiry is therefore not enforced, and switching the façade off does not restore DOT's settings.
- **Major:** ruff fails under the pinned and the CI versions (F4).

None of these needs a redesign.

## Findings

### F1 (blocker): a refresh token revoked within the last 120 s still mints a new token pair, and the membership gate is skipped

- **Where:** `inventree_mcp/auth/token.py:102-105` returns `None` (delegates to DOT) for a refresh-token row that is already revoked. `token.py:117-121` delegates unchanged when no original scopes are found.
- **What DOT does with it** (`oauth2_validators.py:1208-1224` and `:984-1015`): inside `REFRESH_TOKEN_GRACE_PERIOD_SECONDS` a revoked row still validates. A revocation (as opposed to a rotation) leaves no successor access token behind. So `_save_bearer_token` creates a **new** access token and a new refresh token, and `get_original_scopes()` returns `[]`.
- **Verified live:**
  1. `POST /oauth/revoke/` with the refresh token returns 200, and the old access token gets 401.
  2. `POST /oauth/token/ grant_type=refresh_token` with the revoked token returns **200** with `scope=''`.
  3. That new access token gets **200 on `tools/list`** (10 tools) and **200 on `/api/part/`**.
  4. A second refresh with the new refresh token gets 400. This only happens because `scope_to_list('')` is `['']`, so `narrow()` returns `[]` and the empty-scope branch revokes it.
- **Net effect:** after `/oauth/revoke/` or `membership.revoke_all_mcp_tokens()`, any client that refreshes within 120 s gets an unscoped access token for a full `ACCESS_TOKEN_LIFETIME_SECONDS`. That includes WP2's `block_user()`. Claude refreshes automatically on a 401, so this path is the normal one, not an attack. Because the row is revoked, `membership.refresh_grant_allowed()` is never called, so WP2 cannot close the gap from inside the function body.
- **Hidden by a test:** `tests/test_auth_token.py:179` (`test_revoke_endpoint_revokes`) calls `_expire_grace_period()` before it checks the refusal.
- **Report:** `WP1-report.md` "Known limitations" mentions the 120 s window. It does not mention that a fresh, unscoped token family is issued.

**Fix**, in `_check_refresh_grant`:

1. Run `membership.refresh_grant_allowed(rt.user)` for revoked rows too.
2. For a revoked row, delegate to DOT only when it is a rotation inside the grace window, that is when `AccessToken.objects.filter(source_refresh_token=rt).exists()`. DOT then re-serves the existing pair. Otherwise answer `invalid_grant`.
3. Replace `if not requested: return None` with `invalid_grant`, and filter empty strings out of `scope_to_list()`.
4. Rewrite `test_revoke_endpoint_revokes` so it asserts a 400 immediately after revocation, with no `_expire_grace_period()`. Add the same assertion to `test_auth_flow.py`.
5. Consider reporting the DOT behaviour upstream: grace applies to revoked tokens, not only rotated ones.

### F2 (major): `approval_prompt=auto` skips consent for any client with a live token, including loopback DCR clients

- **Where:** `auth/authorize.py:164` falls through to `AuthorizationView.get()`. DOT honours `request.GET["approval_prompt"] == "auto"` (`views/base.py:257-292`) and issues a code without consent whenever the user holds an unexpired token for the same application and scopes.
- **Verified live:** a DCR client registered with `http://127.0.0.1/callback` and approved once was then authorized again with `approval_prompt=auto`. The result was a 302 straight to `http://127.0.0.1:43123/callback?code=…`, with no consent page.
- **Why it matters:** this contradicts spec §6 and the README: consent is skipped only for trusted CIMD clients with non-loopback redirects. Loopback ports are wildcarded (`ALLOW_LOCALHOST_LOOPBACK`), and DCR client IDs are public. A local program that knows a consented client's `client_id` can obtain a code silently by opening the browser.
- **Not declared** in the report.

**Fix:** in `MCPAuthorizationView.get()`, before `super().get()`, drop `approval_prompt` from a copy of `request.GET`, or refuse `auto` unless `_may_skip_consent()` is true. Add a test.

### F3 (major): DOT settings injection is per process and lazy. It never happens at start-up and is never undone when the façade is switched off

- **Start-up is a no-op.** `core.py:142-147` calls `settings_inject.apply()` in `__init__`. At that moment `registry.get_plugin("inventree-mcp")` is still `None`, so `get_plugin_setting("OAUTH_ENABLED", False)` returns `False` and `apply()` injects `{}`.
  - Verified with a fresh `manage.py shell` in the worker container while `OAUTH_ENABLED` is on: `REFRESH_TOKEN_EXPIRE_SECONDS=None`, `ACCESS_TOKEN_EXPIRE_SECONDS=36000`, `CIMD_ENABLED=False`.
- **Consequences:**
  - InvenTree's daily `run_oauth_maintenance` (`InvenTree/tasks.py:1286-1294`) runs `clear_expired()` in a django-q worker process. Unless `cleanup_stale_clients` happened to run earlier in that same process, `REFRESH_TOKEN_EXPIRE_SECONDS` is `None`, and DOT enforces refresh expiry only in `clear_expired()` (`models.py:997-1040`). In practice the 30-day refresh lifetime is not enforced. With `REFRESH_TOKEN_REUSE_PROTECTION` on, revoked rows are never purged either.
  - DOT's own `/o/` endpoints issue 10 h or 1 h tokens, and accept or reject CIMD client IDs, depending on whether the gunicorn worker that serves the request has served a façade request before.
- **Switching off does not restore.** `auth/__init__.py:51-54` returns 404 before `ensure_applied()`, and `discovery.py:54-55` calls `ensure_applied()` only while the façade is enabled. After an admin turns `OAUTH_ENABLED` off, the web processes keep `CIMD_ENABLED=True` and the façade lifetimes until they restart. That contradicts report deviation 1 ("values injected earlier are restored").

**Fix:**

1. Call `ensure_applied()` unconditionally, before the enabled check, in `FacadeViewMixin.dispatch` and in `challenge_header`.
2. Apply in workers through a django-q `pre_execute` signal receiver, connected once from the plugin. Alternatively, make `desired_values()` read `PluginSetting` directly when the registry has no instance yet, so the `__init__` call works.
3. Add a test that resets `settings_inject._last_applied` and the DOT settings, then checks that the first scheduled-task execution (or a fresh process) sees the façade's values.

### F4 (major, definition of done): ruff is not clean under the versions that matter

- **Results:**
  - `uvx ruff@0.16.0 check --preview inventree_mcp` (the `.pre-commit-config.yaml` pin) reports 3 errors, all in WP1 code:
    - `auth/discovery.py:70` RUF012 (`http_method_names` list)
    - `auth/login_bridge.py:97` RUF012
    - `core.py:147` RUF100 (unused `noqa: BLE001`)
  - CI (`.github/workflows/ci.yaml:31-35`: `pip install -U ruff`, then `ruff check`; ruff 0.17.0 today) reports the same three. CI would fail.
  - `ruff@0.12.12` reports only the two pre-existing E402s in `mcp_server.py`.
  - `ruff format --preview --check` is clean under both versions.
  - `tests/` checked with `--isolated` under 0.16.0: one RUF100 at `tests/test_auth_authorize.py:45`.
- **Which to trust:** `.pre-commit-config.yaml` (v0.16.0) and CI. `AGENTS.md:121` ("pins `ruff==0.12.0`") is stale, and the report followed it.

**Fix:** make both `http_method_names` tuples, drop the `noqa` at `core.py:147` and at `test_auth_authorize.py:45`, and correct `AGENTS.md:121`.

### F5 (minor): the protected-resource metadata view rebuilds a DOT view that is meant to be subclassed

- **Where:** `auth/discovery.py:92-104` builds the RFC 9728 document by hand.
- **DOT equivalent:** DOT 3.4.0 ships `OAuthProtectedResourceMetadataView` (`views/metadata.py:159-232`). It has `get_resource`, `get_authorization_servers`, `get_scopes_supported`, `get_resource_name` and `get_resource_documentation` hooks, which exist for the per-resource case.
- **Not declared.** The spec described the document, but hard rule 1 applies.

**Fix:** `class ProtectedResourceMetadataView(FacadeViewMixin, OAuthProtectedResourceMetadataView)`, overriding those five getters. Keep `Cache-Control` and `OPTIONS` in a small mixin. The AS metadata view is correctly hand-built: DOT's `OAuthServerMetadataView` hard-codes `oauth2_provider:` URL names and `DCR_ENABLED`.

### F6 (minor): the challenge header is formatted by hand

- **Where:** `auth/discovery.py:42-64` re-implements `oauth2_provider.www_authenticate.build_bearer_challenge`.
- **The declared justification does not hold.** Report deviation 12 says the helper is "DOT ≥ 3.4 only". But `resource_metadata` and `scope` are added only under `oauth_enabled()`, which already requires `facade_supported()`, so a lazy import inside that branch is safe on older DOT.
- **What the local `_quote` misses:** it does not strip CR/LF the way DOT's does (`www_authenticate.py:20-29`).

**Fix:**

```python
build_bearer_challenge(request, oauth2_error={"error": "invalid_token"} if invalid_token else {"scope": ...}, realm="InvenTree", resource_metadata_url=...)
```

Merge `scope` into the dict in both cases. Keep the bare `Bearer realm="InvenTree"` fallback.

### F7 (minor): registration rate limit can be bypassed with a spoofed `X-Forwarded-For`

- **Where:** `auth/clients.py:90` keys on `REMOTE_ADDR`, as the spec says.
- **Why that is spoofable:** InvenTree's `x_forwarded_for.middleware.XForwardedForMiddleware` overwrites `REMOTE_ADDR` with the **first** `X-Forwarded-For` entry, which the client controls. That holds unless the proxy replaces the header: Caddy without `trusted_proxies` replaces it, nginx's `$proxy_add_x_forwarded_for` does not. It also holds whenever gunicorn is reachable directly.
- **Mitigation already present:** `cleanup_stale_clients` limits the damage.

**Fix:** document the proxy requirement in README. Optionally key on `REMOTE_ADDR` together with the last `X-Forwarded-For` hop.

### F8 (minor): token-endpoint policy runs before client authentication, for any DOT application

- **Where:** `auth/token.py:64-138` runs the membership gate, and on empty narrowing `refresh_token.revoke()` (`token.py:124-125`), before DOT authenticates the client. The lookups are not limited to `mcp_applications()`.
- **Effects:**
  - Anyone holding a code or refresh token, without the client secret, can learn the user's block status from the error text.
  - The same person can revoke a refresh token of a manually created `/o/` application by posting it to the façade token endpoint, because its scopes, such as `a:staff`, narrow to `[]`.
- **Severity:** low, since the holder already has the token, but it also goes beyond the RFC 7009 client-authentication model.

**Fix:** return `None` (let DOT answer) unless `rt.application` or `grant.application` is in `clients.mcp_applications()`. Alternatively, call `self.authenticate_client(request)` (from `OAuthLibMixin`) first and refuse with `invalid_client` on failure.

### F9 (minor): consent page gives no trust signal for DCR clients

- **Where:** `templates/inventree_mcp/consent.html:2-11`. The heading shows the client-chosen `client_name` (for example "Claude"), and the trust badge appears only when `client_host` is set (CIMD clients).

**Fix:** add `client_registered_dynamically` to the context and show a "self-registered, not verified" badge.

### F10 (minor): `resource` values are not restricted to this server

- **Where:** `auth/authorize.py:144-147` and DOT's `get()` accept any absolute URI. Tokens bound to foreign audiences are issued.
- **Verified:** `resource=https://other.example/api/` produced a token that gets 401 on both MCP and `/api/`, so it is harmless here, but RFC 8707 expects `invalid_target`.

**Fix:** accept only `discovery.resource(request)` and its slash-less form.

### F11 (minor): `Vary` header is overwritten

- **Where:** `auth/cors.py:47` does `response["Vary"] = "Origin"`, which replaces any `Vary` the view set.

**Fix:** `django.utils.cache.patch_vary_headers(response, ["Origin"])`.

### F12 (minor): defaults and comma-list parsing are duplicated

- **Defaults:** the `TRUSTED_CLIENT_HOSTS` default appears three times (`core.py:68`, `auth/clients.py:43`, `auth/settings_inject.py:59`). The `TRUSTED_CLIENT_ORIGINS` default appears twice (`core.py`, `auth/cors.py:14`).
- **Parsing:** comma-list parsing appears three times (`settings_inject._split_hosts`, `clients.trusted_client_hosts`, `cors.trusted_origins`).

**Fix:** have `settings_inject.desired_values()` call `clients.trusted_client_hosts()`, add a `_csv(key)` helper in `settings.py`, and take defaults from `InvenTreeMCP.SETTINGS[key]["default"]`.

### F13 (minor): integer settings have no lower bound

- **Where:** `core.py:83-113` uses `"validator": int`. Values of 0 or below are accepted, so `ACCESS_TOKEN_LIFETIME_SECONDS=0` issues dead tokens and `UNUSED_CLIENT_RETENTION_DAYS=0` deletes every idle DCR client daily.

**Fix:** `[int, MinValueValidator(1)]`, and `MinValueValidator(0)` for the rate limit.

### F14 (minor): stale docstring

- **Where:** `auth/tasks.py:23-25` says the DCR registration token is "never expiring". `settings_inject.py:82` now injects 86400 s.

### F15 (minor): the well-known index advertises 404 documents when the façade is off

- **Where:** `core.py:183` gates on `facade_supported()` only. On InvenTree master, `/.well-known/` then lists `oauth-*` documents that answer 404 while `OAUTH_ENABLED` is off. This is spec-compliant: the redirect patterns are built at URL load with `request=None`.

**Fix (optional):** when `request is not None` (the index view), also require `oauth_enabled()`.

### F16 (minor): `ensure_applied()` cost, including on anonymous 401s

- **Where:** `discovery.py:54-55` calls it, and through it `desired_values()` (7 setting reads), on every unauthenticated MCP 401. `mcp_scopes_available()` adds one more read.
- **Cost:** cache hits when InvenTree's global cache is configured, DB reads otherwise. Anyone can trigger it.

**Fix (optional):** memoise `desired_values()` for a few seconds per process, or invalidate on `PluginSetting` `post_save`. F3's fix should be done together with this one.

### F17 (minor, informational)

- **Line endings:** `core.py` was converted from CRLF to LF (`git show 1bf973b:inventree_mcp/core.py` is CRLF), so the diff is a whole-file rewrite. Mention this in the commit body or keep the conversion separate.
- **Disabled façade:** `FacadeViewMixin` answers JSON 404 to the browser views (authorize, login bridge). Acceptable.
- **Alias document:** the `openid-configuration` alias serves an RFC 8414 document without the OIDC-required fields (`jwks_uri`, `subject_types_supported`, `id_token_signing_alg_values_supported`). Fine for MCP clients; a strict OIDC client would reject it. Document this.
- **README gap:** WP2 needs *app integration* (`ENABLE_PLUGINS_APP`) for `AppMixin` models. The README lists only URL and schedule integration.

## Question-by-question

### 1. Spec compliance

| Spec § | Status | Notes |
|---|---|---|
| 1 Challenge | ✓ | `mcp_transport.py:160-174`, both 401 paths (`:209`, `:216`). Format and quoting as specified. Slash-less alias `:248` (declared dev. 10, justified). |
| 2 Discovery | ✓ | `discovery.py`. All fields, `*` CORS, `max-age=300`, 404 when off, `reverse()`-built URLs. F5 (PRM should subclass DOT). The extra `OPTIONS` handler is harmless and undeclared. |
| 3 Settings injection | ✗ partially | Keys ✓, `DCR_ENABLED` and `OIDC_ISS_ENDPOINT` never injected ✓, oauthlib core reset ✓ (good catch). `apply()` in `__init__` is a no-op, and restore-on-disable is not reached (F3). Extra key `DCR_REGISTRATION_TOKEN_EXPIRE_SECONDS` (declared dev. 2, justified). |
| 4 CORS | ✓ | `cors.py`. F11. Preflight is in practice answered by django-cors-headers (declared limitation, correct). |
| 5 Registration | ✓ | `clients.py:110-180`. Gate bypass `:103-107`. Rate limit `:80-100` (F7). Helpers imported, not copied, with guard test `test_auth_clients.py:27-40`. Extra fields (declared dev. 8) and grant restriction (dev. 6), justified. Cleanup task `tasks.py` ✓. |
| 6 Authorize | ✗ partially | Order, narrowing, `form_valid`, `error_response` and `render_denied` ✓. Narrowing moved into `validate_authorization_request` (dev. 3, necessary). S256 enforcement (dev. 4, necessary on 1.5.7). `may_connect` re-run on POST (dev. 5). `prompt` disables skip (dev. 6). The consent-skip rule is violated through `approval_prompt=auto` (F2, undeclared). |
| 7 Login bridge | ✓ | `login_bridge.py`. Probed `next` values: `/\evil…`, `https:/evil…` and `javascript:` → 400; same-host absolute URL and a fragment pass, both harmless. `ensure_csrf_cookie` ✓. Reason `invalid_request` added (dev. 11). |
| 8 Token, revoke | ✗ | Hooks in place before `super().post()`. The revoked-row and empty-scope pass-through break revocation (F1). Unsupported grant types refused (dev. 6). `revoke_all_mcp_tokens` calls `RefreshToken.revoke()` and `AccessToken.revoke()` ✓. |
| 9 Wiring | ✓ | `AppMixin` and `ScheduleMixin`, 12 settings with defaults exactly as the overview, feature detection `auth/__init__.py:18-28`, well-known entries `core.py:174-195`, absolute task path (dev. 9, correct: `ScheduleMixin` imports globally). `settings.get_plugin_setting` coerces and fails safe ✓. |
| 10 Docs | ✓ | README "Connect with OAuth" and settings table, AGENTS file map and rule. `AGENTS.md:121` ruff pin is stale (F4). |

**Binding interfaces:**

- **`policy.py`:** `mcp_scopes_available() -> list[str]` (`:22`), `effective_scopes(user) -> set[str]` (`:44`), `may_connect(user) -> tuple[bool, str]` (`:51`), `narrow(requested, user) -> list[str]` (`:62`). Signatures ✓. Stub semantics as in the overview ✓.
- **`membership.py`:** `Outcome` with the six members ✓, `is_blocked`, `token_grant_allowed`, `refresh_grant_allowed`, `recheck(user, *, force=False)`, `block_user` (raises), `unblock_user`, `revoke_all_mcp_tokens` (real) ✓.
- **`clients.py` helpers ✓. `discovery.py` helpers ✓. `settings_inject.apply() -> dict`, `ensure_applied() -> None` ✓,** but not "cheap" (F16).
- **URL names:** all 8 ✓, plus `mcp-endpoint-noslash`.
- **Setting keys and defaults:** ✓.
- **Issuer and resource:** forms ✓.

**Undeclared deviations:** the `__init__` no-op (F3), `approval_prompt` (F2), the metadata `OPTIONS` handler (harmless), and the hand-built PRM (F5).

### 2. Security of the OAuth logic

- **PKCE:** S256 required on GET (`authorize.py:134-135`), redirecting `invalid_request` to the validated URI. Verified live: `plain` → 302 `error=invalid_request`. Required again on the consent POST (`:196-197`). Verifier checking stays in oauthlib/DOT ✓. The consent-skip path is behind the same check ✓.
- **Redirect URIs:** exact matching and loopback port wildcarding are DOT's (`redirect_uri_allowed`, `ALLOW_LOCALHOST_LOOPBACK`) ✓. `is_loopback_redirect` (`clients.py:74-77`): http only, `localhost`, `127.0.0.1`, `::1` ✓.
- **Login bridge `next`:** same host, scheme rule and exact authorize path (`login_bridge.py:82-91`) ✓. No open redirect found.
- **CSRF posture:**
  - Metadata: `csrf_exempt`, GET only.
  - Register: `csrf_exempt`, anonymous, and always `user=None` (`clients.py:143-149`), so a cross-site POST cannot bind a user.
  - Token and revoke: `csrf_exempt`, as DOT's own views already are. Authentication is client credentials or PKCE or token possession, not cookies.
  - Authorize: **not** exempt, so the consent POST is CSRF-protected ✓.
  - Login bridge: GET only, and the form it auto-submits carries allauth's CSRF token ✓.
- **Consent skip:** in the façade code, only for trusted CIMD clients with non-loopback redirects and no `prompt` ✓. But DOT's `approval_prompt=auto` bypasses that rule (F2). `application.skip_authorization` is False for DCR clients (`clients.py:147`).
- **Scope narrowing:**
  - GET ✓, through `validate_authorization_request`.
  - Consent POST ✓ (`form_valid`). The tampered hidden field is covered by `test_tampered_scope_is_narrowed`.
  - Refresh ✓, except the empty or unknown original-scope pass-through (F1).
- **Refusals in the token view:** membership refusals return before DOT, so nothing is consumed or rotated. Covered by `test_refresh_refused_by_membership`. The empty-narrow branch revokes, as the spec requires, but before client authentication (F8).
- **CORS:** trusted origins only on register, token and revoke ✓. `*` on metadata, as the spec requires.
- **Rate limiter:** one-minute fixed window aligned to the clock, key `inventree_mcp:dcr:<REMOTE_ADDR>`, 429 with `Retry-After` ✓. Can be bypassed with `X-Forwarded-For` behind an appending proxy (F7).
- **`resource`:** audience binding works. Verified: a token bound to the MCP URL gets 200 on `/mcp/` and `/mcp`, and 401 on `/api/part/`. Foreign resources are accepted at issue time (F10).
- **Invalid clients:** `FatalClientError` renders `error.html` and never redirects (`authorize.py:236-257`). Covered for unknown client, unregistered redirect and untrusted CIMD host.
- **Leaks:**
  - Plugin log calls (`clients.py:175`, `authorize.py:157/247/261`, `token.py:76/110/126`, `membership.py:84`, `tasks.py:66`, `settings_inject.py:137`) log only primary keys, reason codes and non-secret setting values ✓.
  - Templates render no token, code or secret. `consent.html:15` shows the redirect URI, and `:25` shows the signed-in user's own username (with SSO this may be their e-mail, shown only to themselves). That is acceptable.
  - Error bodies contain fixed strings or oauthlib descriptions.
  - Outside the plugin: DOT logs the success URL including `code` at DEBUG (`views/base.py:176`).

### 3. Reuse and duplication

See the Duplication section.

### 4. InvenTree 1.5.x discovery

See the 1.5.x discovery recommendation section.

### 5. Test quality

See Test results.

- **Strengths:**
  - The suites assert PKCE (wrong verifier, missing PKCE, tampered method), exact redirect validation, the error page never redirecting, consent skip for trusted CIMD and loopback consent, tampered-scope narrowing, a refusal not rotating, rotation, reuse protection, `revoke_all_mcp_tokens` scope, CORS for trusted and untrusted origins, and 404 when off.
  - Mocks replace the policy hooks, not the façade logic. That is correct for call-site tests.
- **Gaps:**
  - `test_auth_token.py:163-182` masks F1 (see F1).
  - No tests for: `approval_prompt=auto` (F2); the real `sso_provider_id()`/`_sso_app()` (`LOGIN_ENABLE_SSO` off, `SSO_PROVIDER_ID`, ambiguity), which is always mocked (`test_auth_authorize.py:236`, `:256`, `:267`); login bridge 404 when off; `plain` PKCE on GET; a tampered `client_id` or `redirect_uri` on the consent POST; injection in a fresh process (F3).
  - `test_inactive_user_gets_403` and `test_cors_for_trusted_origin_only` call the view directly. Both are justified in their docstrings.
- **Skips:** the three `WellKnownRedirectTest` skips are justified, because `well-known:index` does not exist on 1.5.7. They are never executed in this environment. CI's `:latest` matrix job is the only place they run.
- **`test_auth_flow.py`:** covers 401 → PRM → AS metadata → DCR → authorize (bridge redirect) → consent → token with `resource` → MCP → refresh → MCP. It asserts only that the refreshed **access** token differs. It does not check that the refresh token rotated, that the old access token is refused, or revocation.
- **Recommended additions to `test_auth_flow.py`:**
  1. Assert `refreshed["refresh_token"] != tokens["refresh_token"]` and that the old access token gets 401.
  2. `POST /oauth/revoke/`, then assert the MCP endpoint gives 401 and that a refresh with the revoked token gives 400 immediately. The last check currently fails (F1), which is the point.

### 6. WP2 and WP3 compatibility

The hook points exist as the report states:

- `token.py:50-62` dispatches before `super().post()`.
- `_check_code_grant` → `membership.token_grant_allowed` (`:74`).
- `_check_refresh_grant` → `membership.refresh_grant_allowed` (`:108`), then `policy.narrow` (`:123`).
- `authorize.py:121` calls `may_connect` first. `:109-117` narrows in `validate_authorization_request`, and `:185-206` narrows in `form_valid`.
- Membership stubs exist.
- `models.py` holds a docstring only, `migrations/__init__.py` exists, `AppMixin` is active, and `ScheduleMixin` is in place for `membership_sweep`.

**Call sites WP2/WP3 would otherwise have to change** (fix in WP1):

- **`token.py:102-105`** skips `refresh_grant_allowed` for revoked rows. WP2's block-then-refresh path depends on this (F1).
- **`token.py:120-121`** skips `policy.narrow` when the original scopes are unknown. WP3 narrowing would not apply (F1).
- **`authorize.py:164`:** the `approval_prompt=auto` path issues codes without the consent re-check (F2). `may_connect` and `narrow` still apply there, so WP3 is unaffected. Consent policy is.
- **`settings_inject` in workers (F3):** WP2's `membership_sweep` runs in django-q. It must not rely on injected DOT settings until F3 is fixed.

Nothing else needs to move. WP2 must enable app integration, and must generate `0001_initial` in the container.

### 7. Code quality

- **ruff:** F4.
- **Docstrings:** present on every public function and class. One stale docstring (F14).
- **Dead code:**
  - `REASONS["anonymous"]` and `REASONS["sso_unavailable"]` are unreachable. `LoginRequiredMixin` redirects anonymous users before `get()`, and nothing renders `sso_unavailable`. The spec asked for both, so keep them.
  - `_MetadataView.document` is an abstract stub, which is fine.
- **`FacadeViewMixin`:** first in every MRO (`authorize.py:103`, `clients.py:110`, `token.py:43`, `:149`, `login_bridge.py:94`, `discovery.py:67`), so the 404 precedes CORS preflight handling and DOT's login redirect ✓. It skips `ensure_applied()` when disabled (F3).
- **`settings_inject` across gunicorn and django-q:** per-process module state guarded by a lock, plus the oauthlib core reset. Correct within a process. Wrong across processes and at start-up (F3).
- **`ensure_applied()` cost:** F16.

## Duplication

| Place | Re-implements | Verdict | Refactor |
|---|---|---|---|
| `clients.py:123-173` (`MCPRegistrationView.post`) | DOT `DynamicClientRegistrationView.post` (`dynamic_client_registration.py:282-325`), statement for statement, minus the permission check, plus façade fields | Acceptable: the spec mandates it, the helpers are called rather than copied, and `test_dot_helpers_present` guards them | Optional: inject `DCR_REGISTRATION_PERMISSION_CLASSES=(<façade permission that allows only the façade URL>,)`, call `super().post()`, then post-process the `Application` (`user=None`, `allowed_origins`, name) and the JSON. That removes the copied sequence, at the cost of one more instance-wide setting. |
| `token.py:94-101` | Refresh-token lookup from `OAuth2Validator.validate_refresh_token` (`oauth2_validators.py:1195-1203`) | Acceptable (the spec requires it). DOT's method has side effects (family revocation) and cannot be called for a read. | None beyond the existing comment |
| `authorize.py:144-147` | RFC 8707 resource validation from `AuthorizationView.get` (`base.py:225-248`) | Acceptable (the spec tolerated it). Marked with a comment. | — |
| `discovery.py:42-64` | `www_authenticate.build_bearer_challenge` | Not acceptable as justified | F6 |
| `discovery.py:92-104` | `OAuthProtectedResourceMetadataView` | Not acceptable under rule 1 | F5 |
| `token.py:35-40` `_oauth_error` | DOT `_error_response` shape | Trivial, acceptable | — |
| `settings_inject.py:40-41,57-61`, `clients.py:49-52`, `cors.py:18-21` | Comma-list parsing and defaults ×3 | Not acceptable (drift risk on the allowlist) | F12 |
| `tests/test_auth_flow.py:238-246` `_mcp`, `test_auth_discovery.py:156-163` `_post` | `OAuthFacadeTestMixin.mcp_call` (`oauth_helpers.py:195-203`), differing only in an optional token | Minor | Give `mcp_call` an optional token and use it in both |
| `{k: v for k, v in page.context["form"].initial.items() if v is not None}` in `oauth_helpers.py:149`, `test_auth_authorize.py:97`, `test_auth_flow.py:316` | Same form harvesting ×3 | Minor | Add `consent_form_data(page)` to the helpers |
| `test_auth_token.py:245-256` `_pair`, `test_auth_clients.py:190-206` `_token` | Token-row factories | Minor | Add `make_token_pair(app, user, *, expired, refresh)` to `oauth_helpers.py` |
| `test_auth_flow.py:270-300` builds the DCR body and authorize URL by hand | `register()` and `authorize_url()` | Acceptable: the flow test deliberately uses URLs from discovery documents | — |
| `dev/oauth_smoke.py:37-87` (`NoRedirect`, `Session`) | `dev/authentik/sso_smoke.py:44-73` (`NoRedirect`, `Browser`) at `6375ace` | Not acceptable: about 35 duplicated lines, and the smoke script never drives the SSO bridge | Move the HTTP client into `dev/_http.py` (stdlib). Give `oauth_smoke.py` an `--sso USER` mode that reuses `sso_smoke.browse()`/`run_flow()` from the bridge's form (POST `provider/redirect` with `callback_url` = authorize URL), so bridge → Authentik → authorize is exercised. Coordinate with the agent currently editing `dev/` (`sso_smoke.py` is modified in the working tree). |

## 1.5.x discovery recommendation

**Facts (verified with curl on the dev stack):**

- `/.well-known/oauth-authorization-server/plugin/inventree-mcp` gets a 302 to `/accounts/login/?next=…`, which ends in 200 HTML.
- The same happens for `/.well-known/openid-configuration/plugin/inventree-mcp` and the bare `/.well-known/oauth-authorization-server`.
- `/plugin/inventree-mcp/.well-known/oauth-authorization-server` and `…/openid-configuration` get 200 JSON.

**In-plugin alternatives, evaluated:**

- **A different issuer layout:** none works. RFC 8414 path insertion always places `/.well-known/…` at the origin root, whatever the issuer path is, including an issuer with no path. On 1.5.x every root `/.well-known/*` path is caught by `AuthRequiredMiddleware` and redirected to login. No issuer path makes the middleware return a non-2xx answer that would let Claude Code fall through to the path-appended candidate.
- **An alternative advertisement:** RFC 9728 has no field that names the AS metadata URL directly. `authorization_servers` takes issuers only. The `openid-configuration` alias under the issuer is already served, but Claude Code aborts before it gets there.
- **Patching core at runtime:** a plugin could insert a pattern into `InvenTree.urls.urlpatterns` or extend `middleware.paths_own_security`. That is a monkeypatch of core, fragile across registry reloads, against the decided "no core changes", and unnecessary on master. Rejected.

**Recommendation:** the reverse-proxy rewrite is the right answer for 1.5.x, and upgrading to an InvenTree release with `WellKnownMixin` (#12698) is the permanent fix. Improvements:

1. Ship the rewrites for Caddy and nginx (and Traefik if cheap) in README. Add the `openid-configuration` path-insertion form: Claude Code stops at the first candidate, but other clients probe it. The PRM rewrite is not needed by Claude, which uses the `resource_metadata` from the challenge, but it is harmless.
2. Make the misconfiguration visible. When `OAUTH_ENABLED` is on and `_well_known_compat` fell back to the blank mixin, log a WARNING at first façade use with the rewrite hint, and mention it in the `OAUTH_ENABLED` setting description.
3. Add the two `rewrite` lines to `dev/Caddyfile`, so that acceptance 2 (`claude mcp list` → "Needs authentication") passes on the dev stack directly. This is the dev-environment owner's file. The current working copy does not contain them.

## Test results

```
$ dev/test.sh inventree_mcp            (-v 2)
Ran 201 tests in 54.672s
OK (skipped=3)
  skipped: WellKnownRedirectTest.{test_authorization_server_path_insertion_redirects_to_the_plugin,
           test_index_lists_the_oauth_documents, test_protected_resource_path_insertion_redirects_to_the_plugin}
           'this InvenTree version has no /.well-known/ plugin index'

$ dev/test.sh inventree_mcp.tests.test_auth_flow
Ran 1 test in 2.892s
OK

$ python3 dev/oauth_smoke.py
...
[10] refresh -> 200; expires_in=3600 refresh token rotated: True
[11] tools/list with refreshed token -> 200; 56 tools
[12] tools/list with the pre-refresh access token -> 401 (revoked by rotation)
OK: OAuth façade flow completed

$ uvx ruff@0.16.0 check --preview inventree_mcp      -> Found 3 errors (discovery.py:70, login_bridge.py:97 RUF012; core.py:147 RUF100)
$ uvx ruff@0.12.12 check --preview inventree_mcp     -> Found 2 errors (pre-existing E402, mcp_server.py:51-52)
$ uvx ruff check   (0.17.0, as CI)                   -> the same 3 WP1 errors (+3 in uncommitted dev/ files)
$ ruff format --preview --check inventree_mcp        -> 50 files already formatted (0.16.0 and 0.12.12)
```

**Live probes:**

| Probe | Result | Verdict |
|---|---|---|
| Refresh with a revoked refresh token, within the grace period | 200, `scope=''`, new token works on MCP and `/api/part/` | F1 |
| `approval_prompt=auto` on a consented loopback DCR client | code issued without consent | F2 |
| Fresh worker process with `OAUTH_ENABLED` on | `REFRESH_TOKEN_EXPIRE_SECONDS=None`, `ACCESS_TOKEN_EXPIRE_SECONDS=36000`, `CIMD_ENABLED=False` | F3 |
| PKCE `plain` on GET | `invalid_request` redirect | ✓ |
| Bad `next` values on the login bridge | 400 | ✓ |
| Audience binding | correct | ✓ |
| DCR `registration_access_token` | rejected on MCP (401), `/api/` (403) and the management URI (404) | ✓ |

## Next-stage readiness

WP2 is **blocked** on F1, because WP2's blocking relies on revocation that a refresh within 120 s undoes, and the gate is skipped for revoked rows. WP2 is also blocked on F3, because WP2's sweep runs in workers that lack the injected settings.

WP3 is not blocked once F1 is fixed: its narrowing must also run when the original scopes are unknown.

Required before WP2:

- F1, F2, F3 and F4.
- The flow-test additions from Q5, including the immediate post-revocation refresh assertion.

Recommended in the same fix-up commit: F5, F6, F12, F14, the test-helper consolidation, and the README proxy notes (F7, F17, 1.5.x item 1).

Can wait: F8 to F11, F13, F15 and F16.
