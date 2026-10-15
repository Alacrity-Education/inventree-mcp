# WP2: membership re-check against Authentik (guide §3, §5, §7, §8)

Read first: `docs/plans/00-overview.md`, `docs/plans/WP1-report.md` (hook points left by WP1), `KINA/docs/MCP-AUTH-GUIDE.md` sections 3, 5, 7, 8, 9 and 11, `KINA/docs/DESIGN.md` section 7.1 (outcome table), and:
- InvenTree core: `InvenTree/sso.py` (`ensure_sso_groups`, what it reads from `SocialAccount.extra_data`), `InvenTree/apps.py` (where that signal is connected), `common/setting/system.py` (`SSO_GROUP_KEY`, `SSO_GROUP_MAP`, `SSO_REMOVE_GROUPS`, `LOGIN_ENABLE_SSO_GROUP_SYNC`), `InvenTree/tasks.py` (`offload_task`), `plugin/base/integration/ScheduleMixin.py`.
- allauth (`vendor/django-allauth-65.19.6`): `socialaccount/models.py` (`SocialAccount`, `SocialToken`: `token` = access token, `token_secret` = refresh token, `expires_at`, how settings-configured apps are referenced), `socialaccount/providers/openid_connect/{provider,views}.py` (`server_url`, `openid_config`, userinfo), `socialaccount/internal/jwtkit.py` (ID token verification against a JWKS; reuse it), `socialaccount/adapter.py` (`list_apps`, `get_app`), `account/signals.py` (`user_logged_in` carries `sociallogin` for SSO logins).
- plugin: `auth/membership.py` and `auth/policy.py` stubs, `auth/token.py` (call sites), `mcp_transport.py`.

Decided: the upstream refresh token is read from and written back to allauth's `SocialToken` (no second copy, no plugin-side encryption; documented deviation). Membership after a re-check is decided by core's group sync plus the plugin policy, never by comparing raw claim strings in the plugin.

## Scope

- `models.py`: `MCPUserState` exactly as in the overview; migration `0001_initial` generated inside the dev container (`dev/shell.sh` or `manage.py makemigrations inventree_mcp`), committed. `check_user_permission` classmethod: staff, or the row's own user for read.
- `auth/membership.py`: real implementations of every function in the overview interface, plus internals below.
- `auth/policy.py`: add `in_required_groups(user) -> bool` and make `may_connect` consult it (new setting `MCP_REQUIRED_GROUPS`, str, default empty = no requirement). Do not touch `effective_scopes`/`narrow` (WP3).
- `auth/signals.py`: login stamping. `auth/tasks.py`: `membership_sweep`. `mcp_transport.py`: blocked-user rejection and background re-check for non-OAuth credentials. `core.py`: settings `[2]` from the overview + `MCP_REQUIRED_GROUPS`, scheduled task registration, signal connection (in `__init__` or an `AppConfig.ready`-equivalent; InvenTree plugins typically connect signals in the plugin `__init__`, guarded against double connection with `dispatch_uid`).

## The re-check (`membership.recheck(user, *, force=False) -> Outcome`)

1. `MEMBERSHIP_ENFORCED` off → `Outcome.DISABLED`, no side effects. Not due and not `force` → `NOT_DUE`.
2. Serialise per user: `transaction.atomic()` + `MCPUserState.objects.select_for_update().get_or_create(user=user)`. Inside the lock re-read `membership_checked_at` and return `NOT_DUE` if another worker just did it.
3. Find the upstream credentials: the allauth app for `sso_provider_id()` (from WP1's `login_bridge.sso_provider_id`, move it to a shared helper `auth/sso.py` if both modules need it) and the user's `SocialAccount` for that provider and its `SocialToken`. No account or no `token_secret` → `NO_UPSTREAM_TOKEN` (set `last_recheck_error`, log a warning naming the cause once per user per hour, not per request).
4. Discovery: reuse allauth's OIDC adapter for the provider (`OpenIDConnectOAuth2Adapter(request=None, provider_id)`, its `openid_config`, `access_token_url`, `profile_url`), with explicit 5 s connect/read timeouts on every outbound call (allauth's `get_adapter().get_requests_session()` or `requests` directly, but always with `timeout=5`). Cache the discovery document in Django's cache for 10 minutes.
5. Redeem: `POST token_endpoint` with `grant_type=refresh_token`, `refresh_token=<token_secret>`, client auth as discovered (`client_secret_basic` preferred, `client_secret_post` fallback). Responses: 200 → continue; 400 with `error=invalid_grant` → `NOT_MEMBER` path "revoked upstream"; any other 4xx, 5xx, timeout, connection error, malformed JSON → `UNREACHABLE`.
6. Store the rotated tokens on the same `SocialToken` row (`token`, `token_secret` only if a new one came back, `expires_at`). Do this before verification so a verification failure does not lose the rotated token.
7. Verify the ID token with `jwtkit` against the provider's JWKS (issuer = discovery `issuer`, audience = app `client_id`); the `sub` must equal `SocialAccount.uid`; failure → `UNREACHABLE` (treat as unverifiable, per guide). Read the groups claim (`SSO_GROUP_KEY`) from the ID token; if absent, `GET userinfo` with the new access token (same timeout) and read it there; absent in both → empty list.
8. Sync groups through core: write the claim into `SocialAccount.extra_data["userinfo"][SSO_GROUP_KEY]` (create the `userinfo` dict if needed, keep other keys), save, then call `InvenTree.sso.ensure_sso_groups(None, SocialLogin(account=account))`. This applies `SSO_GROUP_MAP` and `SSO_REMOVE_GROUPS` exactly like a login does.
9. Decide: `ok, reason = policy.may_connect(user)` with a fresh `user` instance (groups changed). `ok` → `membership_checked_at = now`, clear `last_recheck_error`, `MEMBER`. Not ok with reason `no_mcp_permission` or `not_in_required_groups` → `block_user(user, reason)`, `NOT_MEMBER`. (Reason `blocked` cannot occur here because step 2 ran under the lock after `is_blocked` was false; `inactive` → `NOT_MEMBER` too.)

`block_user(user, reason)`: set `access_revoked_at=now`, `revoked_reason`, `revoke_all_mcp_tokens(user)` (WP1), delete the upstream `SocialToken` row for the provider, log at WARNING with user id and reason only. `unblock_user(user)`: clear both fields. `is_blocked(user)`: `access_revoked_at` set (cheap query, one row).

## Decision functions (used by WP1's token view; signatures fixed)

`refresh_grant_allowed(user) -> (bool, str)`:
- enforcement off → `(True, "")`; blocked → `(False, "invalid_grant")`.
- `recheck(user)` →
  - `MEMBER`, `NOT_DUE`, `DISABLED` → `(True, "")`
  - `NOT_MEMBER` → `(False, "invalid_grant")`
  - `UNREACHABLE` → allowed while `membership_checked_at` is younger than `MEMBERSHIP_GRACE_HOURS`, else `(False, "invalid_grant")`; nothing revoked, nothing rotated (the token view must not consume the refresh token when it refuses; WP1 returns before delegating to DOT, confirm this).
  - `NO_UPSTREAM_TOKEN` → allowed while the last SSO login (`MCPUserState.last_login_at`, falling back to `user.last_login`) is younger than `RELOGIN_INTERVAL_HOURS`, else `(False, "invalid_grant")`.

`token_grant_allowed(user)`: `(not is_blocked(user), "invalid_grant" if blocked else "")`. `policy.may_connect` already covers the rest at authorize time.

## Check points

- **Login** (`auth/signals.py`): on allauth's `user_logged_in` with a `sociallogin` for our provider: upsert `MCPUserState`, set `last_login_at=now`, `membership_checked_at=now` (core group sync ran in the same request), clear `access_revoked_at`/`revoked_reason`. Password logins only set `last_login_at`.
- **Refresh grant**: WP1's `token.py` already calls `refresh_grant_allowed`.
- **Bearer and token requests on `/mcp`** (`mcp_transport.py`, after authentication, before binding the identity): `is_blocked(user)` → 401 JSON-RPC error "Access revoked, sign in to InvenTree again" with the challenge header. For requests authenticated with an `ApiToken` or Basic (not OAuth2): if enforcement is on and `recheck_due(user)`, `offload_task("inventree_mcp.auth.tasks.recheck_user", user.pk, force_async=True, check_duplicates=True)`; never block the request. `recheck_due(user)` is a cheap read (`membership_checked_at` older than `MEMBERSHIP_RECHECK_INTERVAL_MINUTES` or missing).
- **Sweep** (`auth/tasks.py: membership_sweep`, hourly via `SCHEDULED_TASKS`): users that hold an unrevoked façade refresh token (through `clients.mcp_applications()`) or an active `ApiToken`, where `recheck_due`; call `recheck`. Log a one-line summary with counts only.

## Timings and settings (overview table): `MEMBERSHIP_RECHECK_INTERVAL_MINUTES=60`, `MEMBERSHIP_GRACE_HOURS=4`, `RELOGIN_INTERVAL_HOURS=24`, `MEMBERSHIP_ENFORCED=True`, `MCP_REQUIRED_GROUPS=""` (InvenTree group names, comma separated; a user must be in at least one when set).

## Privacy and logging (guide §7)

Log user id, provider id, issuer host and reason. Never the e-mail, name, claims, tokens, or the IdP error body beyond the `error` code. `last_recheck_error` holds a short code (`invalid_grant`, `timeout`, `http_5xx`, `bad_id_token`, `no_token`), not response text.

## Tests (`inventree_mcp/tests/test_membership.py`, plus additions to `test_auth_token.py`)

Mock the IdP at the HTTP boundary (patch the session/`requests` calls in `membership.py`); sign ID tokens with a test RSA key and serve its JWKS from the mocked discovery. Cases, each asserting state rows, tokens and the HTTP result where relevant:
1. member re-check: stamp updated, rotated refresh token stored on `SocialToken`, groups synced through core (a mapped group added, an unmapped one removed with `SSO_REMOVE_GROUPS`).
2. removed from the group (claim without it): blocked, façade tokens revoked, upstream token deleted, next refresh grant → 400 `invalid_grant`, MCP bearer → 401, authorize → 403 `blocked`; a new SSO login (signal) lifts the block.
3. IdP answers `invalid_grant`: same as 2.
4. IdP unreachable (timeout, 503, malformed JSON, bad signature): within grace → refresh succeeds and the same upstream token is kept; past grace → `invalid_grant`, nothing revoked, the DOT refresh token still works once the IdP is back.
5. no upstream token: allowed within `RELOGIN_INTERVAL_HOURS`, refused after; warning logged once.
6. not due → no outbound call (assert the mock was not hit); `force=True` calls anyway.
7. enforcement off → `DISABLED`, no calls, refresh allowed; `MCP_REQUIRED_GROUPS` respected when set, ignored when empty.
8. login signal: SSO login stamps and unblocks; password login only sets `last_login_at`.
9. non-OAuth credential on `/mcp` with a stale check offloads exactly one background task (patch `offload_task`), and a blocked user gets 401.
10. sweep picks only due users with live tokens.
11. serialisation: with the lock, two sequential `recheck` calls inside one interval produce one outbound call.

End-to-end against the real dev Authentik (`dev/membership_e2e.py`, adapted from KINA's `kina_authentik_e2e.py` scenarios): member connects through the façade and calls a tool; age the check (`UPDATE` on `membership_checked_at` through `dev/shell.sh` or psql); refresh triggers a real re-check (watch the Authentik token endpoint log or assert the rotated `token_secret` changed); remove `e2e-member` from `inventree-mcp` in Authentik (use the bootstrap API helper); age the check again; refresh → `invalid_grant`; MCP bearer → 401; add the user back; a new SSO login lifts the block. Include the script and its output in the report.

## Acceptance

Tests 1-11 green via `dev/test.sh inventree_mcp`; WP1 suites still green; e2e script output in the report; docs updated (`README.md` "Membership re-checks" section with the outcome table and the Authentik prerequisites: `offline_access`, refresh validity ≥ 30 days, group binding); report `docs/plans/WP2-report.md`; one commit `WP2: membership re-check`.
