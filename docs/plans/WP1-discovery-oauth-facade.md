# WP1: discovery and the OAuth façade (Claude connects through the browser, no hard-coded token)

Read first: `docs/plans/00-overview.md` (binding names, interfaces, settings), the master plan section "WP1", `KINA/docs/MCP-AUTH-GUIDE.md` sections 4 and 6, and these sources:
- plugin: `inventree_mcp/core.py`, `mcp_transport.py`, `settings.py`, `server_card.py`, `_well_known_compat.py`, `oauth2_bridge.py`, `test_mcp.py` (test base classes and the existing OAuth2 fixtures around lines 360-390 and 2020-2040).
- DOT 3.4 (`vendor/django-oauth-toolkit-3.4.0`, the version in InvenTree 1.5.x): `views/base.py` (`AuthorizationView`, `TokenView`, `RevokeTokenView`), `views/dynamic_client_registration.py`, `views/metadata.py`, `cimd.py`, `models.py` (`AbstractApplication`, `redirect_to_uri_allowed`), `oauth2_validators.py` (`validate_client_id`, `validate_refresh_token`), `settings.py`, `templates/oauth2_provider/authorize.html` (form fields the consent page must post).
- InvenTree core: `InvenTree/permissions.py` (`auth_exempt`), `InvenTree/middleware.py` (`AuthRequiredMiddleware`: `/plugin/` paths 401 unless `auth_exempt`), `InvenTree/urls.py` (allauth headless mounted at `/api/auth/`, `account_login` redirect that drops `next`), `users/oauth2_scopes.py`, `plugin/base/integration/WellKnownMixin.py`, `plugin/urls.py` (well-known redirects), `src/frontend/src/functions/auth.tsx` (`ProviderLogin`: the POST the login bridge must reproduce).
- allauth: `headless/socialaccount/views.py` (`RedirectToProviderView`), `headless/socialaccount/forms.py` (`callback_url` must pass `is_safe_url`).

Decided, do not re-open: plugin route; façade under `/plugin/inventree-mcp/oauth/`; issuer `<origin>/plugin/inventree-mcp`; DOT models and oauthlib do all token work; DOT's own `/o/` endpoints untouched; no core changes.

## Scope of WP1

Everything in the overview marked [1]: `auth/urls.py`, `discovery.py`, `settings_inject.py`, `cors.py`, `clients.py`, `authorize.py`, `token.py`, `login_bridge.py`, `policy.py` (stub), `membership.py` (stub + real `revoke_all_mcp_tokens`), `tasks.py` (`cleanup_stale_clients`), templates, the `WWW-Authenticate` challenge in `mcp_transport.py`, `core.py` wiring (`AppMixin`, `ScheduleMixin`, settings, well-known entries, feature detection), `settings.py` generalisation, tests, docs.

Out of scope: membership re-check logic (WP2), `MCPRuleSet` and scope narrowing beyond the stub (WP3), any React (WP4). Keep the call sites for `membership.*` and `policy.*` exactly as specified so later WPs only replace bodies.

## Behaviour to implement

### 1. Challenge on the MCP endpoint (`mcp_transport.py`)

Every response with status 401 from `MCPView.dispatch` (both the `APIException` path and the `REQUIRE_AUTH` path) gets `WWW-Authenticate: <discovery.challenge_header(request, invalid_token=<credential was presented>)>`. Header value: `Bearer realm="InvenTree"` plus, when `OAUTH_ENABLED`, `, resource_metadata="<absolute PRM url>", scope="<space-joined policy.mcp_scopes_available()>"`, plus `, error="invalid_token"` when a credential was presented. Keep the JSON-RPC body. Do not change anything else in the view.

### 2. Discovery (`auth/discovery.py`)

- `ProtectedResourceMetadataView` (GET, anonymous, `Access-Control-Allow-Origin: *`, `Cache-Control: max-age=300`): `{"resource": resource(request), "authorization_servers": [issuer(request)], "scopes_supported": policy.mcp_scopes_available(), "bearer_methods_supported": ["header"], "resource_name": "InvenTree MCP", "resource_documentation": "https://github.com/inventree/inventree-mcp"}`.
- `AuthorizationServerMetadataView` (same headers): `issuer`, `authorization_endpoint`, `token_endpoint`, `registration_endpoint`, `revocation_endpoint` (all absolute façade URLs), `response_types_supported: ["code"]`, `response_modes_supported: ["query"]`, `grant_types_supported: ["authorization_code", "refresh_token"]`, `code_challenge_methods_supported: ["S256"]`, `token_endpoint_auth_methods_supported: ["none", "client_secret_basic", "client_secret_post"]`, `revocation_endpoint_auth_methods_supported` same, `scopes_supported` as above, `client_id_metadata_document_supported: true`, `service_documentation`. Build the endpoint list from the URL names in the overview with `request.build_absolute_uri(reverse(...))`; do not hard-code paths. Serve the same document under `openid-configuration`.
- Both views return 404 when `OAUTH_ENABLED` is off.
- `issuer(request)`, `resource(request)`, `challenge_header(request, *, invalid_token)` as in the overview.

### 3. DOT settings injection (`auth/settings_inject.py`)

`apply()` merges the keys listed in the overview into `django.conf.settings.OAUTH2_PROVIDER` (the dict object DOT's `oauth2_settings.user_settings` holds; mutate in place, then `oauth2_settings.reload()`), logging the effective values once at INFO without secrets. Values come from plugin settings (`ACCESS_TOKEN_LIFETIME_SECONDS` etc.); `CIMD_REGISTRATION_PERMISSION_CLASSES=("oauth2_provider.cimd.HostAllowlistCIMDPermission",)`, `CIMD_ALLOWED_HOSTS` = `TRUSTED_CLIENT_HOSTS` split and stripped. `ensure_applied()` is called at the top of every façade view and of the challenge builder: it compares the desired dict with what was last applied (module-level cache) and re-applies only on change, so admins can tune lifetimes without a restart. `apply()` is also called from the plugin's `__init__` with a try/except (DB may not be ready; then defaults). Unit test: after `apply()`, `oauth2_settings.CIMD_ENABLED is True` and `oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS == <setting>`; changing the plugin setting and calling `ensure_applied()` updates it.

### 4. CORS (`auth/cors.py`)

`trusted_origins() -> list[str]` from `TRUSTED_CLIENT_ORIGINS`. `TrustedOriginCORSMixin` for class-based views: answers `OPTIONS` preflight with 204 and `Access-Control-Allow-Origin: <origin>`, `Access-Control-Allow-Methods: POST, OPTIONS`, `Access-Control-Allow-Headers: Authorization, Content-Type`, `Access-Control-Max-Age: 3600` when the request `Origin` is trusted; adds `Access-Control-Allow-Origin` to every other response for a trusted origin unless already present (DOT's token backend sets it when `Application.allowed_origins` matches; do not double up). Use this mixin on register, token and revoke. Metadata views use `*`.

### 5. Registration (`auth/clients.py`)

`MCPRegistrationView(TrustedOriginCORSMixin, oauth2_provider.views.DynamicClientRegistrationView)`, `csrf_exempt`, `auth_exempt`, 404 when `OAUTH_ENABLED` is off. Override `dispatch` so DOT's `DCR_ENABLED` gate is not consulted (we never inject `DCR_ENABLED`), and override `post`:
1. rate limit per client IP: fixed window counter in Django's cache, key `inventree_mcp:dcr:<ip>`, limit `REGISTER_RATE_LIMIT_PER_MINUTE` (0 disables), over limit → 429 JSON `{"error": "too_many_requests"}` with `Retry-After` seconds. Client IP: `request.META["REMOTE_ADDR"]` (InvenTree's proxy settings already rewrite it when `USE_X_FORWARDED_*` is on; do not parse `X-Forwarded-For` yourself).
2. registration itself: reuse DOT's module helpers `_parse_metadata`, `_build_application_kwargs`, `_issue_registration_token`, `_application_to_response` (they are private names; import them explicitly, add a comment and a unit test `test_dot_helpers_present` that fails loudly if a DOT upgrade removes them). Do **not** copy their bodies. Build the `Application` with `user=None`, `registration_source=DCR`, `name=client_name or "MCP client"`, `allowed_origins=" ".join(trusted_origins())`, `skip_authorization=False`. Keep DOT's response shape (201, `client_id`, `client_id_issued_at`, `client_secret_expires_at: 0`, echoed metadata, `client_secret` only for confidential methods, `registration_access_token` / `registration_client_uri` as DOT emits them). Validation errors → 400 `invalid_client_metadata` exactly as DOT.

Helpers: `is_trusted_client(application)`, `is_loopback_redirect(uri)`, `mcp_applications()` as in the overview. `cleanup_stale_clients()` in `auth/tasks.py`: delete DCR applications (never CIMD, never manual) whose `updated` (DOT bumps it) is older than `UNUSED_CLIENT_RETENTION_DAYS` and that have no unexpired, unrevoked access token and no unrevoked refresh token; return the count; register it in `core.py` `SCHEDULED_TASKS` daily.

### 6. Authorize (`auth/authorize.py`)

`MCPAuthorizationView(oauth2_provider.views.AuthorizationView)`, `auth_exempt` (it decides itself), `template_name = "inventree_mcp/consent.html"`, `login_url = reverse_lazy("plugin:inventree-mcp:oauth-login")` so DOT's `LoginRequiredMixin` sends anonymous browsers to the bridge with `?next=<full authorize URL>`. 404 when `OAUTH_ENABLED` is off.

`get()`:
1. `settings_inject.ensure_applied()`.
2. `ok, reason = policy.may_connect(request.user)`; if not ok → `render_denied(reason)` (403, `error.html`, never a redirect).
3. `scopes, credentials = self.validate_authorization_request(request)` (DOT; raises `OAuthToolkitError` for unknown client, bad redirect URI, missing PKCE; handle with `self.error_response(error, application=None)`, which must render the plugin's `error.html`, see below).
4. `scopes = policy.narrow(scopes, request.user)`; empty → `render_denied("no_mcp_permission")`.
5. Load the application (`get_application_model().objects.get(client_id=credentials["client_id"])`). If `AUTO_APPROVE_TRUSTED_CLIENTS` and `is_trusted_client(application)` and not `is_loopback_redirect(credentials["redirect_uri"])`: validate any `resource` query values with DOT's `is_valid_resource_uri` and put the list in `credentials["resource"]` (DOT's `get()` does this; the three lines are an accepted duplication, mark them with a comment), then `uri, headers, body, status = self.create_authorization_response(request=request, scopes=" ".join(scopes), credentials=credentials, allow=True)` and `return self.redirect(uri, application)`.
6. Otherwise `return super().get(request, *args, **kwargs)` (DOT renders the consent form with its hidden fields).

`get_context_data()`: on top of DOT's context add `scope_labels` (narrowed scopes → human labels from `oauth2_settings.SCOPES`), `client_host` (host of a CIMD `client_id`, else `None`), `client_trusted`, `loopback_redirect` (bool), `access_token_lifetime`, `refresh_token_lifetime`, `inventree_user`.

`form_valid(form)`: narrow `form.cleaned_data["scope"]` through `policy.narrow(...)` before calling `super().form_valid(form)` (the hidden field can be tampered with); empty → `render_denied("no_mcp_permission")`.

`error_response(error, application, **kwargs)`: when DOT would render its own template (fatal errors with no trusted redirect), render `inventree_mcp/error.html` with the error description instead; when DOT redirects (error with a valid `redirect_uri`), keep DOT's behaviour. Study `BaseAuthorizationView.error_response` and override minimally.

`render_denied(reason)`: `error.html`, 403, with the reason code and a sentence per code: `anonymous`, `inactive`, `blocked` ("Your access has been revoked. Sign in to InvenTree again."), `no_mcp_permission` ("None of your groups grant MCP access. Ask an administrator."), `client` ("This client is not registered or not trusted."), `sso_unavailable`.

### 7. Login bridge (`auth/login_bridge.py`)

`MCPLoginBridgeView` (GET, `auth_exempt`):
- reads `next`; it must pass `url_has_allowed_host_and_scheme` for the current host and its path must equal the authorize path (`reverse("plugin:inventree-mcp:oauth-authorize")`), otherwise 400 `error.html`.
- if `request.user.is_authenticated` → redirect to `next`.
- if `OAUTH_FORCE_SSO` and an SSO provider is available (`sso_provider_id(request)`: `SSO_PROVIDER_ID` or the single configured `openid_connect` app via allauth's adapter `list_apps`; the global setting `LOGIN_ENABLE_SSO` must be on): render `login_bridge.html`, decorated with `ensure_csrf_cookie`, containing a form that auto-submits (JS, with a visible "Continue with <provider name>" button as fallback) a POST to allauth's headless browser endpoint `provider/redirect` (resolve its URL with `reverse`; find the exact namespace by inspecting `InvenTree/urls.py` and allauth's `headless/urls.py`; the frontend posts to `/api/auth/v1/auth/provider/redirect`) with fields `provider`, `process=login`, `callback_url=<absolute next URL>`, `csrfmiddlewaretoken`. After the SSO round trip allauth lands on `callback_url`, which is the authorize URL, now with a session.
- otherwise render `login_bridge.html` in "manual" mode: a link to InvenTree's login page and the instruction to return to Claude and connect again after signing in (core drops `next`, documented limitation).

### 8. Token and revoke (`auth/token.py`)

`MCPTokenView(TrustedOriginCORSMixin, oauth2_provider.views.TokenView)`, `csrf_exempt`, `auth_exempt`, 404 when off. `post()`:
1. `ensure_applied()`.
2. `grant_type == "authorization_code"`: look up the `Grant` by `code` and `application__client_id` (do not consume it); if found, `ok, code = membership.token_grant_allowed(grant.user)`; refusal → 400 `{"error": "invalid_grant", "error_description": ...}`.
3. `grant_type == "refresh_token"`: look up the `RefreshToken` the same way DOT's validator does (`token_checksum = sha256(token)`, prefer the unrevoked row). If found and it has a user: `ok, code = membership.refresh_grant_allowed(rt.user)`; refusal → `invalid_grant`. Then `scopes = policy.narrow(rt.access_token.scope.split() if rt.access_token else [], rt.user)`; empty → revoke `rt` and its access token, `invalid_grant`. Otherwise copy `request.POST` to a mutable `QueryDict`, set `scope=" ".join(scopes)` (oauthlib accepts a subset of the original scopes on refresh) and assign it back.
4. `return super().post(request, *args, **kwargs)`; do not touch DOT's response except the CORS header from the mixin.

`MCPRevokeView(TrustedOriginCORSMixin, RevokeTokenView)`: `csrf_exempt`, `auth_exempt`, 404 when off, nothing else.

`membership.revoke_all_mcp_tokens(user)` (real in WP1): revoke (`revoked=now`) every unrevoked `RefreshToken` of `user` whose application is in `mcp_applications()`, and delete/expire its `AccessToken` the way DOT's `RefreshToken.revoke()` does (call that method; do not re-implement); return the count.

### 9. Wiring (`core.py`, `auth/urls.py`, `settings.py`)

- `core.py`: add `AppMixin` (templates and later models; create an empty `models.py` and `migrations/__init__.py` now so the app loads) and `ScheduleMixin`; add the [1] settings from the overview with validators (`bool`, `int`) and descriptions; `setup_urls()` appends `auth.urls.urlpatterns` only when feature detection passes; `get_well_known_urls()` adds the two OAuth entries (only when the façade is registered); call `settings_inject.apply()` in `__init__` guarded by try/except. `SCHEDULED_TASKS = {"cleanup_stale_clients": {"func": "auth.tasks.cleanup_stale_clients" ...}}` daily (check `ScheduleMixin` docs for the dotted path form relative to the plugin package).
- `auth/urls.py`: the names in the overview; wrap views with `csrf_exempt`/`auth_exempt` as the existing `mcp/` route does.
- `settings.py`: `get_plugin_setting(key, default)` returns `plugin.get_setting(key)` coerced to `type(default)` (`bool` via InvenTree's `str2bool` for strings, `int`, `str`), falling back to `default` on any error or when the plugin cannot be resolved. Keep the existing callers working.
- Templates: `base.html` (self-contained, small inline CSS, light/dark via `prefers-color-scheme`, no external assets, InvenTree name in the header), `consent.html` (DOT's hidden fields from `oauth2_provider/authorize.html` plus client name, host, trust badge, user, scope labels, lifetimes, loopback warning, Approve/Deny buttons where Approve has `name="allow"`), `login_bridge.html` (auto-submit form and manual mode), `error.html` (title, sentence, reason code, "Back to InvenTree" link).

### 10. Documentation

`README.md`: new "Connect with OAuth" section (claude.ai connector URL, `claude mcp add --transport http …`, what the user sees, Authentik/InvenTree prerequisites: SSO configured, `OAUTH_ENABLED`), settings table. `AGENTS.md`: file map for `auth/`, the "no DOT duplication" rule, how to run `dev/test.sh`.

## Tests (`inventree_mcp/tests/`, InvenTree `InvenTreeTestCase`/`InvenTreeAPITestCase` from `InvenTree.unit_test`, same style as `test_mcp.py`)

- `test_auth_discovery.py`: both documents (content, headers, 404 when disabled, issuer/resource honour `X-Forwarded-Proto/Host`), well-known redirects from `/.well-known/oauth-protected-resource/anything` and `/.well-known/oauth-authorization-server/plugin/inventree-mcp`, challenge header on the MCP endpoint with and without a presented credential and with the feature off.
- `test_auth_clients.py`: registration happy path (public client, `none`), confidential client gets a secret, invalid redirect URI → 400, rate limit → 429 with `Retry-After`, `allowed_origins` set, `cleanup_stale_clients` deletes only what it should, `test_dot_helpers_present`.
- `test_auth_authorize.py`: anonymous → redirect to the bridge with `next`; bridge renders the SSO form (mock `sso_provider_id`) or manual mode; invalid `next` → 400; signed-in user + registered client → consent page with narrowed scope labels; approve → 302 to `redirect_uri` with `code` and `state`; deny → `error=access_denied`; unknown client / unregistered redirect → error page, never a redirect; trusted CIMD client with https redirect skips consent (create the `Application` with `registration_source=CIMD` and an `https://claude.ai/...` client_id directly, no network); same client with a loopback redirect shows consent; tampered hidden `scope` is narrowed; inactive user → 403.
- `test_auth_token.py`: full PKCE code exchange through the façade yields access + refresh token with `expires_in == ACCESS_TOKEN_LIFETIME_SECONDS` and the narrowed scope; the access token works on the MCP endpoint (`tools/list` non-empty) and on `/api/part/`; refresh rotates (old refresh token rejected afterwards), narrowed scope re-applied, `membership.refresh_grant_allowed` returning `(False, "invalid_grant")` (mock) → 400 `invalid_grant`; revoke endpoint revokes; `revoke_all_mcp_tokens` revokes only façade applications' tokens; CORS preflight and header for a trusted origin, none for an untrusted one.
- `test_auth_flow.py`: one end-to-end test with Django's test client: 401 with challenge → PRM → AS metadata → register → authorize (session login) → consent approve → token → MCP call → refresh → MCP call. This is the regression test future WPs must keep green.
- `test_settings_inject.py`: as described in section 3.

## Acceptance

1. All tests above pass via `dev/test.sh inventree_mcp`; `dev/test.sh inventree_mcp.test_mcp` still passes.
2. In the running dev environment (`dev/up.sh`), with `OAUTH_ENABLED` turned on for the plugin: `claude mcp add --transport http inventree-dev http://localhost:8080/plugin/inventree-mcp/mcp/` then `claude mcp list` reports it as needing authentication (discovery works). If a browser is unavailable to you, prove the flow with a script instead: `dev/oauth_smoke.py` that performs register → authorize (using a session obtained through `dev/authentik/sso_smoke.py` from WP-dev, or admin basic login) → token → `tools/list` → refresh → `tools/list`, against `http://localhost:8080`. Include the script in the repo and its output in the report.
3. `ruff` clean, docs updated, one commit `WP1: discovery and OAuth façade`.

## Report back

Write `docs/plans/WP1-report.md`: what was built, deviations from this microplan with reasons, exact test command outputs (summary lines), the smoke script output, known limitations, and anything WP2/WP3 must know (for example the exact hook points in `token.py`).
