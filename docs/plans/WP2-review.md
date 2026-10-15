# WP2 review: membership re-check against the identity provider

Reviewed commit `7081bad` on `feature/oauth-facade` (`git diff 94bbd50..7081bad`, frontend files excluded). This was a read-only review against the spec `docs/plans/WP2-membership-recheck.md`, the overview, KINA `MCP-AUTH-GUIDE.md` §3/5/7/8/9/11 and `DESIGN.md` §7.1, allauth 65.19.6, InvenTree core and DOT 3.4.0. I ran the test suite, the end-to-end script and the WP1 smoke test against the dev stack. The working tree was clean before and after.

## Verdict

**Accept with fixes.** The re-check works as the spec describes:

- It reuses allauth's adapter, jwtkit and `SocialToken`, and core's `ensure_sso_groups`.
- The decision functions match the guide's outcome table.
- The WP1 residual items are handled: R1, R3 and the gate reordering.
- All tests and the real-Authentik e2e pass.

Two majors must be fixed before WP3 builds on this code:

- **F1:** `MCP_REQUIRED_GROUPS` is not enforced for API-token and Basic requests on `/mcp`.
- **F2:** the rotated upstream refresh token is only committed at the end of a transaction. That transaction stays open across the outbound calls, and the hourly sweep runs inside django-q's 90 s kill timeout. A killed sweep can lose a user's upstream token, and that user is then blocked.

The rest are minors. None of them needs a redesign.

## Findings

### F1 (major): `MCP_REQUIRED_GROUPS` is not enforced for API-token and Basic credentials on `/mcp`

**Where:** `inventree_mcp/mcp_transport.py:260-266`. It checks only `membership.is_blocked(user)`. `policy.may_connect` / `in_required_groups` is called only from `authorize.py:132,231` and `membership.py:337`.

**Effect:** a user who is not in a required group but holds an ApiToken (or Basic, once allowed) can use MCP:

- **With an upstream token:** for up to `MEMBERSHIP_RECHECK_INTERVAL_MINUTES`, until a background re-check blocks them.
- **Without an upstream token** (password-only user): indefinitely. `NO_UPSTREAM_TOKEN` (`membership.py:227-230`) never evaluates the policy, so no block is ever set.
- **With `MEMBERSHIP_ENFORCED` off:** indefinitely, because `in_required_groups` is enforced at authorize only.

The setting's description (`core.py:152-156`: "a user must be in at least one of them to use MCP") and the README settings row ("to connect and to keep the connection") therefore overstate it.

E2E scenario E shows the precondition. After an SSO login outside the group, `blocked=False` and `checked 2 s ago`, so an API token of that user passes `/mcp` until the next due re-check. WP3's planned 403 for empty effective scopes covers rulesets, not `MCP_REQUIRED_GROUPS`, unless WP3 folds the groups into `effective_scopes`.

**Fix:**

- In `MCPView.dispatch`, replace the `is_blocked` check with `ok, reason = policy.may_connect(user)`:
  - `blocked` → the existing 401 with the challenge;
  - `no_mcp_permission` / `inactive` → 403 JSON-RPC "No MCP permission" (the credential is valid; this matches WP3's plan).
- Keep `_queue_membership_recheck` for allowed users only.
- Add a test: an API-token user outside `MCP_REQUIRED_GROUPS` gets 403.

### F2 (major): the rotated upstream token is not durable until the whole re-check commits, and the lock-holding transaction spans every outbound call

**Where:** `membership.py:217` opens `transaction.atomic()`. It holds the `select_for_update` row lock (`:110`) across `_discovery`, `_redeem`, the JWKS fetch inside jwtkit, and userinfo (`:320-338`). `_store_tokens` (`:325`) is "outside the savepoint", but it is still inside that outer transaction. So the rotated `token_secret` is committed only when `recheck()` returns.

The spec's intent for step 6 ("store before verification so a failure does not lose it") holds only for failures that raise. It does not hold if the process dies between the redeem and the commit.

**When the process dies:** `membership_sweep` (`tasks.py:135-143`) re-checks every due user serially in one django-q task. The task has InvenTree's default `background.timeout` of 90 s and `max_attempts` of 5 (`InvenTree/setting/worker.py:25-36`).

- With a healthy IdP, a re-check is about four HTTP round trips. A few hundred due users exceed 90 s, and django-q kills the worker mid-loop.
- If the kill lands after `_redeem` and before the commit, the old (spent) refresh token stays in `SocialToken`.
- Authentik answers `invalid_grant` for it at the next re-check. The user is then blocked and their MCP tokens are revoked until their next SSO login.

**Side effect:** a waiting `select_for_update` (no `nowait`) holds a gunicorn gthread thread and its DB connection for as long as the holder's outbound calls take. The login signal waits on the same row (`signals.py:41`).

**Fix:**

- **Minimum:**
  - Give the sweep a time budget, for example stop starting new re-checks after 60 s and `offload_task` a continuation.
  - Document the 90 s interaction in the report.
- **Proper:** commit the rotated token immediately with a lease instead of a lock held across the network:
  - under `select_for_update`, check and set a short lease (cache `add` keyed on the user with a 60 s TTL, or a lease timestamp column), then commit;
  - do the outbound calls and `_store_tokens` in autocommit;
  - do the group sync, the decision and the stamp in a short second transaction.

  That also removes the lock wait from the login path.

### F3 (minor): no back-off while the provider is unreachable

**Where:** `UNREACHABLE` leaves `membership_checked_at` untouched (`membership.py:239-251`), so the user stays due.

**Effect:**

- During an outage, every refresh of every due user retries the outbound call.
- When the provider times out rather than refusing connections, that costs at least 5 s, up to 10 s with connect plus read.
- Concurrent refreshes of one user queue on the row lock and each retries in turn.

The grace logic stays correct. The cost is worker time.

**Fix:** after `timeout` / `connection_error` / `http_5xx`, set a per-provider cache marker (about 60 s). While it is present, `_check_upstream` raises `_Unreachable(<cached code>)` without a call. Alternatively, use `select_for_update(nowait=True)` and treat lock contention as `UNREACHABLE`, so the grace window decides.

### F4 (minor): an SSO login lifts a block without evaluating the identity policy

**Where:** `signals.py:47-50` clears `access_revoked_at` and stamps `membership_checked_at` for any SSO login through the MCP provider. The spec asks for exactly that. Guide §3.2 says the opposite: a refusal at login blocks an existing user.

**Effect:** E2E scenario E shows `blocked=False reason=''` for a user outside the group. Authorize still refuses with 403. API tokens are exposed until F1 is fixed.

**Fix:**

- Core's group sync has run by the time `user_logged_in` fires (signals docstring; `InvenTree/sso.py:60-69` and the `social_account_updated` hook).
- So in the receiver, lift the block only when `policy.in_required_groups(user)` and `user.is_active`.
- Otherwise leave the row blocked, or block with reason `no_mcp_permission`.
- `membership_checked_at` can still be stamped.

Add a test for an SSO login of a non-member. This aligns with the WP3 rule below: identity-level refusals block.

### F5 (minor): queue churn for users who stay due

**Where:** `mcp_transport.py:193` throttles with a 60 s `cache.add` marker. For `NO_UPSTREAM_TOKEN` (password-only script users) and `UNREACHABLE`, the user never stops being due.

**Effect:**

- Every active API-token user queues one `recheck_user` per minute per process. `check_duplicates` scans the queue each time.
- Without Redis, InvenTree uses `LocMemCache` (`InvenTree/cache.py:141`). Then the 60 s marker, the hourly warning marker (`membership.py:275`) and the discovery cache are all per process. The "once per user per hour" warning becomes once per process.

**Fix:** after a `NO_UPSTREAM_TOKEN` outcome, set the transport marker for the full re-check interval, for example `tasks.recheck_user` sets `inventree_mcp:recheck_queued:<pk>` with `MEMBERSHIP_RECHECK_INTERVAL_MINUTES*60`. Also mention in the README that the markers are per process without the global cache.

### F6 (minor, deviation 6): enforcement off also stops honouring blocks

**Where:** `membership.py:130` returns `False` from `is_blocked` while not enforced.

**Comparison:** KINA DESIGN §7.1: "otherwise only `access_revoked_at` is honoured". The overview calls the setting the "master switch for re-checks and blocking", and the spec's `refresh_grant_allowed` returns `(True, "")` when off, so the implementer's reading is defensible.

**Risk:** an admin who switches enforcement off during an IdP outage silently re-admits every blocked user's API tokens on `/mcp`, and lets them re-authorize with a password session.

**Recommendation (orchestrator's decision):**

- Honour `access_revoked_at` regardless of the switch.
- Make "off" mean "no re-checks, no new blocks".
- Admins use `unblock_user` for manual lifts.
- If kept, the README sentence "Off: no re-checks, no blocks" should add "existing blocks are ignored while off".

### F7 (minor, declared in part): long-lived credentials are never cut off by the grace or re-login windows

**Where:** `mcp_transport.py:182-208` never denies. Guide §11 ("Static tokens ... die with the 24-hour rule") and DESIGN §7.1 ("Static tokens: denied once the grace period is over and the latest background re-check found the provider unreachable") both deny.

**What the report declares:** the unreachable half ("Known limitations": "API tokens stay usable while the provider is down"). It does not declare the no-upstream-token half. The spec does not ask for either.

**Fix:** decide in WP3 or WP5. If adopted, the transport applies the same `_grace_ok` / `_relogin_ok` predicates as `refresh_grant_allowed` (extract them from `membership.py:174-199` instead of duplicating), using the last recorded outcome.

### F8 (minor): discovery seeded into allauth's private `_openid_config`

**Where:** `membership.py:384-397` (deviation 3, declared).

**Assessment:** it works, because `openid_config` is a `hasattr` memo (`views.py:24-31`). It does depend on a private name. A subclass removes that dependency and keeps the same reuse:

```python
class _CachedConfigAdapter(OpenIDConnectOAuth2Adapter):
    @property
    def openid_config(self):
        return self._config
```

The subclass is constructed with the cached document. Low priority; justified as is.

### F9 (minor): the group sync can replace allauth's account data with a stub

**Where:** `membership.py:519-525`. When `extra_data["userinfo"]` is absent (the app has `fetch_userinfo: False`, so allauth stored only `id_token`), the code creates `{"sub": uid, <groups>}`.

**Effect:** allauth's `_pick_data` prefers `userinfo` (`providers/openid_connect/provider.py:20-25`). From then on, the account's display and e-mail data come from that stub.

**Fix:** seed from the existing data: `userinfo = dict(extra.get("id_token") or {"sub": account.uid})`. Other keys are already preserved when `userinfo` exists.

### F10 (minor): missing table and missing app integration

`models_available()` (`membership.py:87-91`) checks only `apps.is_installed`. Two cases are not handled:

- **App installed, `0001_initial` not yet applied:** `is_blocked` (`:134`) raises `ProgrammingError`. Authorize, the token endpoint and `/mcp` answer 500.
- **App integration off:** `MEMBERSHIP_ENFORCED=True` silently does nothing.

**Fix:**

- Log a WARNING once at plugin load when `MEMBERSHIP_ENFORCED` is on and the app is not installed.
- In `is_blocked`, catch `DatabaseError` (inside `transaction.atomic()` so PostgreSQL stays usable), log once, and return `False`. The README already says to run `invoke migrate`.

### F11 (minor): duplication between `dev/membership_e2e.py` and `dev/oauth_smoke.py`

**Where:**

- `membership_e2e.py:183-246`: `register()`, `authorize_url()`, `approve_and_exchange()`.
- `oauth_smoke.py:186-260`: the same request bodies (DCR body, PKCE pair, consent POST, code exchange) inline in `main()`.

**Fix:** move the three helpers into `oauth_smoke.py` and call them from both scripts. Everything else is already imported rather than copied (`step`, `expect`, `refresh`, `mcp_tools_list`, `parse_forms`, `sso_login_through_bridge`, `Browser`, `sso_login`, `set_user_groups`).

### F12 (minor): test gaps and one weak assertion

1. No test for an SSO login of a non-member (F4) or for an API-token user outside `MCP_REQUIRED_GROUPS` (F1).
2. `test_upstream_invalid_grant_blocks` (`test_membership.py:412`) asserts 401 on `/mcp` with `tokens["access_token"]`. `block_user` already deleted that token, so the assertion holds without the block check. `test_removed_from_group…` (`:381-387`) covers the real path with a fresh `make_token_pair`.
3. Test 11 (`:344-356`) is sequential. It simulates the race by patching `recheck_due`. That is what the spec asks, but no test exercises the lock under concurrency.
4. The sweep test does not cover an expired ApiToken or a refresh token of a non-façade application.

### F13 (informational): ID-token algorithm acceptance

jwtkit takes `algorithms=[header alg]` (`jwtkit.py:89-90`). The key comes from the JWK whose `kid` matches, built for the JWK's `alg` (default RS256, `:46-50`). PyJWT rejects an algorithm of a different key family; an HMAC-on-RSA-key attempt raises, and `membership.py:459-464` maps it to `bad_id_token`. `none` is impossible with `verify_signature=True`. The plugin does not restrict to discovery's `id_token_signing_alg_values_supported`. allauth's login does not either. Acceptable.

### F14 (informational): default `MEMBERSHIP_ENFORCED=True` with an empty `MCP_REQUIRED_GROUPS`

The guide says to enable enforcement only with a required group. The overview's default is binding. The README states the effect on password-only installations (24-hour re-login) and tells them to switch it off.

## Spec and guide compliance (question 1)

### Spec sections

| Spec item | Where | Status |
|---|---|---|
| Model and migration | `models.py:10-77`, `migrations/0001_initial.py` | Matches the overview: names, `OneToOneField(related_name="mcp_state", on_delete=CASCADE)`, `max_length` 64/255. Extras: `blank=True` and verbose names (harmless). Index: the one-to-one's unique index on `user_id` is all `is_blocked` needs; the overview specifies no other. `check_user_permission` at `models.py:67-77`. |
| `policy.in_required_groups` | `policy.py:55-67`, `:82-83` | Done. Reason `no_mcp_permission` instead of `not_in_required_groups` (deviation 1; see WP3 guidance). |
| Step 1 | `membership.py:212-215` | Done. |
| Step 2: lock and re-read | `:217-222`, `_locked_state :106-111` | Done. Blocked → `NOT_MEMBER` (deviation 7, declared, justified). |
| Step 3: credentials | `:304-317`, `_social_token :341-347` (app NULL for settings apps), warning `:273-285` | Done. The "once per hour" warning is per process without Redis (F5). |
| Step 4: discovery | `:376-397` | 10-minute cache, 5 s timeout. Deviation 3 is declared and acceptable (F8). |
| Step 5: redeem | `:400-431` | 400 `invalid_grant` → revoked; other statuses / timeout / malformed → unreachable. Client auth follows allauth's `basic_auth` (deviation 2, declared, justified: same as the login; Authentik lists post). |
| Step 6: store first | `:325`, `:434-441` | Ordered before verification, but only durable at the outer commit (F2). |
| Step 7: verify | `:444-464` jwtkit with `issuer` and `audience`; `sub` vs `account.uid` `:329-331`; userinfo fallback `:467-500` with a subject check `:498` | Done. Deviation 5 (no ID token on refresh → subject confirmed by userinfo) is declared and justified: OIDC Core §12.2 makes the ID token optional on refresh. The access token came straight from the token endpoint with client authentication over TLS, and the userinfo `sub` must match. Missing ID token and missing userinfo endpoint → `bad_id_token` (`:481-483`), so there is never an unverified pass. |
| Step 8: group sync | `:512-529` | Done (F9 for a corner). |
| Step 9: decide | `:337-338`, `:253-265` | Done. `inactive` blocks. |
| block / unblock / is_blocked | `:535-579`, `:128-136` | Done. Deviation 6 → F6. |
| Decision functions | `:142-199` | Done; see question 3. |
| Check points | `signals.py:24-77`, `token.py:160`, `mcp_transport.py:182-208,260-266`, `tasks.py:75-150` | Done (F1, F4). |
| Settings | `core.py:128-156` with validators; schedule `core.py:166-169`; signals `core.py:182-184` | Done. |
| Privacy | the plugin's own log lines carry only ids, codes and the issuer host (grep below) | Done. Core's `ensure_sso_groups` logs the username (declared limitation). |
| Tests 1-11, e2e, README, AGENTS | see question 6 | Done. |

### Guide outcome table (§5 / DESIGN §7.1)

| Outcome | Implementation | Verified by |
|---|---|---|
| Member | stamp `:257-259` | test 1; e2e B |
| Not a member | `block_user` `:253-255` → refresh `invalid_grant` `:171-172` | test 2; e2e D |
| Provider `invalid_grant` | `:231-238` → `block_user(user, "invalid_grant")` | test 3 |
| Unreachable within grace | `:177-181`, grace counted from `membership_checked_at` | test 4; e2e C (200, `connection_error`) |
| Unreachable past grace | `invalid_grant`, nothing revoked; the same DOT token works afterwards | test 4; e2e C (400, then 200 once Authentik was back) |
| No upstream token within / past the re-login interval | `:189-199`, `last_login_at` with fallback to `user.last_login` | test 5 |
| Enforcement off | `(True, "")`, `DISABLED`; blocks ignored | test 7. Diverges from DESIGN §7.1 (F6). Static tokens: F7. |

### Declared deviations

| # | Declared | Justified |
|---|---|---|
| 1 | yes | yes (overview reason set), but it forces WP3 to separate the identity check (below) |
| 2 | yes | yes |
| 3 | yes | yes (F8 nit) |
| 4 | yes | yes: allauth's `REQUESTS_TIMEOUT` defaults to 5, InvenTree does not set it, and test 1 asserts 5 on every call including JWKS |
| 5 | yes | yes |
| 6 | yes | defensible, but against the KINA reference (F6) |
| 7 | yes | yes |
| 8 | yes | yes |
| 9 | yes | yes (F5 for the residual churn) |
| 10 | yes | yes |

**Undeclared:**

- F1: required groups are not applied to long-lived credentials.
- F7: the no-token half.
- F2: the transaction scope makes step 6's durability weaker than the report's "a later failure cannot roll it back" (report line 48), for the case where the process is killed.

## Security and correctness of the re-check (question 2)

**The upstream refresh token is never logged or returned.**

- It is only passed in the POST body (`:404`).
- The debug traceback (`:242`) and `type(exc).__name__` (`:248`) contain no token.
- The e2e compares the token by hash only.

**Rotation:** stored on the same row before verification (`:325`). See F2 for durability.

**ID token:**

- Verified with `jwtkit.verify_and_decode`: JWKS from discovery, `issuer` from discovery, `audience=app.client_id`, `verify_exp`, and jti replay through the cache (`jwtkit.py:69-81`).
- The subject is compared with `SocialAccount.uid` using the app's `uid_field`.
- Algorithms: F13.

**Groups claim:**

- Read with `SSO_GROUP_KEY` (`:475`). A string or a list is accepted (`:503-509`).
- Written into `extra_data["userinfo"][key]` while keeping the other keys (`:519-527`; F9 for the stub case).
- `ensure_sso_groups(None, SocialLogin(account=account))` (`:529`) applies `SSO_GROUP_MAP` and `SSO_REMOVE_GROUPS` exactly as `InvenTree/sso.py:17-57` does at login.
- The account `save(update_fields=...)` fires core's `post_save` with `created=False`, which does nothing (`sso.py:60-69`), so the sync does not run twice.

**Serialisation:** `select_for_update` per user (`:110`), re-read under the lock (`:219-222`).

- Under load:
  - InvenTree's gunicorn runs gthread workers with 4 threads (`contrib/container/gunicorn.conf.py:18`). Each thread owns its DB connection.
  - Contention is per user, so it does not starve the pool across users.
  - But each waiter holds a thread for the full upstream time, with no lock timeout, and repeats the call after an unreachable result (F2, F3).
  - There is no `ATOMIC_REQUESTS`.
- The transaction is "idle in transaction" in PostgreSQL for the duration of the outbound calls.

**Discovery cache:** Django cache, 10 minutes, keyed on a SHA-256 of `server_url`. Per process under LocMem.

**Timeouts:**

- `_http` passes `timeout=5` explicitly (`:355`). That overrides the partial in allauth's session (`adapter.py:344-351`) and covers discovery, token and userinfo.
- The JWKS fetch uses allauth's session default of 5 (`jwtkit.py:59-60`).
- Test 1 asserts `{5}` over every recorded call (`test_membership.py:323`).

**Client authentication:** allauth's `basic_auth` (`views.py:33-40`). Authentik lists `client_secret_post`, so post is used.

**`block_user`** (`:535-553`):

- One transaction: stamp, `revoke_all_mcp_tokens` (DOT `revoke()`), delete the provider's `SocialToken` rows, one WARNING line with id, reason and count.
- When `LOGIN_ENABLE_SSO` is off, `sso_app` is `None` and the upstream token is not deleted (`:562-564`). That is minor, since no re-check runs then either.

**`unblock_user`** (`:571-579`) clears both fields. Only an SSO login unblocks automatically (`signals.py:47`, F4).

**Password login:** sets only `last_login_at` (`signals.py:44-46`). It does not unblock (test 8, `test_membership.py:603-615`). It does extend the re-login window for no-token users, as the guide's "last interactive login" implies.

**Missing `SocialToken` / `SocialAccount`:** `NO_UPSTREAM_TOKEN` with code `no_token` (`:314-317`).

**Missing table:** F10.

## Decision functions as used by `token.py` (question 3)

**`refresh_grant_allowed`** (`membership.py:149-199`) maps exactly:

| Condition | Result |
|---|---|
| off | allowed |
| blocked | `invalid_grant` |
| `MEMBER` / `NOT_DUE` / `DISABLED` | allowed |
| `NOT_MEMBER` | `invalid_grant` |
| `UNREACHABLE` | allowed while `membership_checked_at > now - grace`, else `invalid_grant`; a never-checked user is refused at once, as the guide expects after enabling |
| `NO_UPSTREAM_TOKEN` | allowed while `last_login_at or user.last_login` is newer than the re-login interval |

An exception escaping `recheck()` is treated as `UNREACHABLE` (`:160-168`).

**`token_grant_allowed`** (`:142-146`) is called at `token.py:100` for façade applications.

**Reordering** (`token.py:145-169`):

- A revoked row past the grace window goes to DOT without the gate (`:151-152`).
- A revoked row inside the window without a successor is refused after `_revoke_family` (`:153-157`).
- Only a rotation inside the window (`:158`) and live rows reach the gate (`:160`).
- A refusal returns before `super().post()` (`:87-89`), so nothing is rotated.

`test_dead_tokens_never_reach_the_membership_gate` (`test_auth_token.py:239-271`) is a real test:

- It wraps the gate and mocks `recheck`.
- It drives one revoked-in-grace token and one rotated-past-grace token through the HTTP endpoint.
- It asserts that neither was called.
- Under the pre-WP2 order (gate before the revoked branch) `gate.assert_not_called()` fails.

**R1:** `_revoke_family` (`token.py:212-234`) mirrors DOT's `validate_refresh_token` reuse protection. `test_stale_token_replayed_in_grace_revokes_the_family` asserts that RT3 is revoked, that its access token gets 401, and that RT3 then fails to refresh.

**R3:** the `request_started` receiver (`settings_inject.py:188-219`, `dispatch_uid`).

- Cost per request: one `time.monotonic()` and a dict comparison while the 5 s memo is valid. At most one plugin-settings read per process every 5 s. A write only happens when the values changed.
- Across gunicorn workers each process converges within 5 s of its next request. It is no longer tied to façade traffic.
- `_write` mutates `settings.OAUTH2_PROVIDER` under a module lock. Threads reading `oauth2_settings` during that window are a pre-existing WP1 property and occur only on a change.
- The receiver is connected only when `facade_supported()` (`core.py:186-189`), which is correct.
- The test (`test_settings_inject.py:107-128`) calls `connect_signals()` itself, the same weakness WP1's verification noted. It also checks the receiver registry.

## MCP endpoint, offload and sweep (question 4)

**Blocked users:** 401 with the challenge for every authenticator (Token, Basic, OAuth2), before the identity is bound (`mcp_transport.py:260-264`). The `error="invalid_token"` challenge is asserted in `test_membership.py:671`. Required groups are not applied (F1).

**Offload:** `_queue_membership_recheck` (`mcp_transport.py:182-208`):

- Runs only for non-OAuth credentials, only when `recheck_due`.
- Then a 60 s `cache.add` marker per user, then `offload_task(..., force_async=True, check_duplicates=True)`.
- Exceptions are swallowed, and the request never runs the re-check synchronously (`force_async`).

**Flooding:** bounded to one queue scan per user per 60 s per process. A user who stays due causes steady churn (F5). An anonymous caller cannot trigger it, because it requires an authenticated user.

**Sweep** (`tasks.py:87-150`):

- Candidates: active ApiTokens (not revoked, expiry empty or today or later) or unrevoked façade refresh tokens (`mcp_applications()`), excluding blocked users through `Exists` (`:130-133`), then `recheck_due` per user.
- Expired-but-unrevoked DOT refresh tokens are still counted until DOT's daily `clear_expired`. That is harmless.
- It logs counts only. With enforcement off it does nothing.
- Runtime limit: F2.

**Signals:**

- `user_logged_in` with `dispatch_uid` (`signals.py:72-77`), connected from `InvenTreeMCP.__init__` (`core.py:182-184`).
- InvenTree's registry reload re-imports only `inventree_mcp.core` (per the WP1 verification). `auth.signals` keeps its identity, so `dispatch_uid` prevents duplicates.
- The receiver stays connected after deactivation. It then only stamps rows, which is harmless.

## Reuse and duplication (question 5)

See "Duplication" below.

## Duplication

**Reused, not re-implemented:**

- allauth's `get_adapter().get_requests_session()`, `OpenIDConnectOAuth2Adapter` (`access_token_url`, `profile_url`, `basic_auth`, `parse_token`), `jwtkit.verify_and_decode` / `lookup_kid_jwk`, and `SocialToken` storage.
- Core's `ensure_sso_groups`.
- DOT's `RefreshToken.revoke()` / `AccessToken.revoke()`, and DOT's reuse-protection logic is mirrored rather than replaced.

**Discovery:** fetched by the plugin and then seeded into allauth's memo. This is acceptable as reuse: the endpoint and auth-method logic stays allauth's. A subclass would avoid the private attribute (F8).

**`sso.py`** (`auth/sso.py:14-49`) replaces `login_bridge._sso_app` / `sso_provider_id`. The bridge imports from it (`login_bridge.py:33`), and no copy remains.

**Within the package:**

- `membership.py`, `tasks.py` and `signals.py` share logic by calling each other: `tasks.recheck_user` → `membership.recheck`, and the sweep → `recheck_due` / `recheck`. No copy-paste.
- The one near-duplicate: the login receiver's get-or-create under lock (`signals.py:40-43`) and `membership._locked_state` (`membership.py:106-111`). The receiver could call `_locked_state`. Trivial.

**Tests:**

- `FakeIdP`, `PROVIDERS` and `MembershipTestCase` exist only in `tests/test_membership.py`.
- OAuth helpers come from the shared `tests/oauth_helpers.py`.

**Dev scripts:** `dev/membership_e2e.py` imports the flow helpers from `oauth_smoke.py` and `dev/authentik`. It duplicates the registration, PKCE and consent/exchange code that `oauth_smoke.py` keeps inline in `main()` (F11).

## WP3 guidance (question 7)

### Current structure

- `membership._check_upstream` returns `reason` from `policy.may_connect(fresh_user)` (`membership.py:337-338`).
- `recheck` calls `block_user(user, reason)` for any non-empty reason (`:253-255`).
- `in_required_groups` already returns the same reason string, `no_mcp_permission` (`policy.py:82-83`).
- So **the reason alone cannot tell "no MCP ruleset" (authorisation) from "not in `MCP_REQUIRED_GROUPS`" (identity)**.

The orchestrator's rule can still be implemented as a **body change in `membership.py` only**. No signature changes are needed, and `policy.in_required_groups` is already public. WP3 must:

1. **Split the decision in `_check_upstream`.** After `_sync_groups`, load `fresh = User.objects.get(pk=user.pk)` and decide in this order:
   - `not fresh.is_active` → identity refusal, reason `inactive`.
   - `not policy.in_required_groups(fresh)` → identity refusal. Store `revoked_reason="not_in_required_groups"`. That is the spec's original code. `revoked_reason` is a free field and not part of `may_connect`'s binding reason set, which can stay as the overview fixes it.
   - Otherwise `ok, reason = policy.may_connect(fresh)`. `reason == "no_mcp_permission"` → authorisation refusal. Any other refusal → identity refusal.

   Return the class of the result to `recheck`, for example `("block", reason)`, `("no_permission", "")` or `("member", "")`.
2. **Identity refusal:** `block_user(user, reason)`, `Outcome.NOT_MEMBER`, as today. Upstream `invalid_grant` stays a block (`:231-238`).
3. **Authorisation refusal:**
   - Call `revoke_all_mcp_tokens(user)` only. Do not call `block_user`, and do not call `_delete_upstream_token`.
   - Stamp `membership_checked_at = now` and clear `last_recheck_error` (the identity is confirmed).
   - Log `"membership re-check for user id %s: member without MCP permission"`.
   - **Do not return `MEMBER`.** `refresh_grant_allowed` would then allow the refresh. `token.py` would carry on with a refresh-token object whose DB row was just revoked, and safety would rest entirely on `policy.narrow()` returning empty (`token.py:180-186`). If `narrow` ever returned a scope while `may_connect` said `no_mcp_permission`, DOT would receive a revoked row inside the grace window without a successor. That is exactly the WP1 F1 mint path.
   - Return a refusing outcome instead:
     - either add `Outcome.NO_MCP_PERMISSION`. This is an additive enum member, so get the orchestrator's approval for the overview change. Map it in `refresh_grant_allowed` to `(False, "invalid_grant")`. `tasks.membership_sweep` counts `.value`, so it needs no change.
     - or, with no interface change, return `Outcome.NOT_MEMBER` without blocking.
   - In both cases subsequent refreshes within the interval get `NOT_DUE` → allowed → `narrow()` → empty → that token revoked and refused (`token.py:180-186`), which is consistent.
4. **Make `effective_scopes(user)` return an empty set when `not in_required_groups(user)`.** Then `narrow()` at refresh, WP3's transport 403 for non-OAuth credentials, and authorize all agree. That closes F1 even for users who never re-check (password-only, `NO_UPSTREAM_TOKEN`). The transport should call `policy.may_connect` (F1 fix) rather than only `is_blocked` or `effective_scopes`.
5. **Login signal (F4):** with the rule above, an SSO login lifts a block only when the identity checks pass (`is_active` and `in_required_groups`). Users who only lack a ruleset were never blocked, so a new ruleset takes effect at their next authorize with no login needed.
6. **Ruleset-change revocation** (WP3 §`MCPRuleSet` signals): call `revoke_all_mcp_tokens` only, never `block_user`, for the same reason.

### Caveat: do not query through `mcp_state` from `User`

Confirmed live in the dev server process:

```
User.objects.filter(mcp_state__isnull=False)  -> FieldError: Cannot resolve keyword 'mcp_state' into field.
user.mcp_state                                -> works
```

The same applies to `Group.mcp_rule_sets` in WP3. Use `MCPRuleSet.objects.filter(group__in=user.groups.all())` and `Exists(...)`, never `user.groups.filter(mcp_rule_sets__…)` or `Group.objects.filter(mcp_rule_sets__…)`. The test runner does not catch this, so add a dev-stack check, as WP2's report did.

### Model and migration

They match the overview: field names, `on_delete=CASCADE`, `related_name="mcp_state"`, and the unique index from the one-to-one. Extras: `blank=True` and verbose names. WP3's `0002_mcpruleset` depends on `("inventree_mcp", "0001_initial")` and `auth`. Generate it with `dev/manage.sh makemigrations inventree_mcp`. `migrations/` is excluded from ruff, as InvenTree itself does (`InvenTree/.pre-commit-config.yaml:8`), and is packaged through `packages.find`.

## Test and e2e results (question 6)

```
$ dev/test.sh inventree_mcp
Ran 258 tests in 215.077s
OK (skipped=3)
EXIT 0

$ dev/test.sh inventree_mcp.tests.test_membership -v 2
Ran 27 tests in 70.884s
OK            (all 27 ran; none skipped on the dev stack)
```

The 3 skips are WP1's `WellKnownRedirectTest` (no `/.well-known/` plugin index on 1.5.7). The module-level skip in `test_membership.py:177-179` (allauth `openid_connect` not installed) is justified, because the re-check needs allauth's provider registry. It means CI with stock images (no `INVENTREE_SOCIAL_BACKENDS`) gets no WP2 coverage. WP5 must set `INVENTREE_SOCIAL_BACKENDS=openid_connect`, as the report says.

**Test quality:**

- `FakeIdP` signs real RS256 tokens with a per-test RSA key and serves the matching JWKS (`test_membership.py:118-122`, `160-174`).
- It is patched at `requests.Session.request`, so jwtkit's own JWKS fetch is exercised.
- `bad_signature` signs with a different key.

Tests 1-11 assert what the spec lists:

| Test | Asserts |
|---|---|
| 1 | stamp, same `SocialToken` row rotated, mapped group added / unmapped removed, every timeout 5 |
| 2 | 400, the state row, revoked DOT tokens, deleted upstream token, no second outbound call, MCP 401 with challenge, authorize 403 `blocked`, unblock by SSO login |
| 3 | blocking on upstream `invalid_grant` (weak 401 assertion, F12) |
| 4 | six failure modes, within and past grace, the same DOT token works afterwards |
| 5 | re-login window, fallback, warning once |
| 6 | no call when not due, call with `force` |
| 7 | enforcement off, required groups |
| 8 | SSO login versus password login |
| 9 | exactly one offload; 401 for blocked |
| 10 | candidate selection |
| 11 | the sequential re-read under the lock |

No test is tautological. Gaps are in F12.

**Lint:**

```
$ uvx ruff@0.16.0 check --preview inventree_mcp                     -> All checks passed!
$ uvx ruff@0.16.0 format --preview --check inventree_mcp            -> 51 files already formatted
$ uvx ruff@0.16.0 check --preview --isolated inventree_mcp/tests    -> All checks passed!
$ uvx ruff@0.16.0 check --isolated dev/membership_e2e.py            -> All checks passed!
$ uvx ruff@0.16.0 check --preview --isolated inventree_mcp/migrations -> Found 2 errors (RUF012 in the generated migration; excluded by pyproject, as InvenTree does)
```

**Log grep:** every `logger.*` call in `auth/membership.py`, `auth/signals.py`, `auth/tasks.py`, `auth/token.py` and `mcp_transport.py` logs only user ids, reason codes, counts and the issuer host. None logs a username, e-mail, claim or token. Core's `ensure_sso_groups` logs `user` (the username) on group changes (`InvenTree/sso.py:49,56`). That is declared, and the fix belongs upstream.

**README accuracy:** accurate except for two points:

- the `MCP_REQUIRED_GROUPS` "keep the connection" claim for API tokens (F1);
- "Off: no re-checks, no blocks" should say that existing blocks are ignored (F6).

**E2E** (`python3 dev/membership_e2e.py`, with the Authentik outage):

```
[0] settings: MCP_REQUIRED_GROUPS='MCP Users', re-check interval 60 min, grace 4 h; Authentik groups of e2e-member: ['inventree-mcp', 'inventree-users']
[A] connected through the façade: expires_in=3600 scopes=12
[A] tools/list -> 200
[A] state: checked 2 s ago, blocked=False reason='' last_error='' upstream token=present groups=['MCP Users', 'Users'] live MCP refresh tokens=2
[B] last check aged to 2 h, refresh -> 200; upstream refresh token rotated: True
[B] refresh again within the interval -> 200; upstream token unchanged: True
[C] stopping authentik-server
[C] Authentik down, last check 2 h ago (grace 4 h): refresh -> 200; last_error='connection_error'
[C] Authentik down, last check 5 h ago: refresh -> 400 invalid_grant; blocked=False live MCP refresh tokens=2
[C] tools/list with the current access token -> 200 (nothing revoked)
[C] starting authentik-server
[C] Authentik back: the same refresh token -> 200 (attempt 1); checked 2 s ago
[D] Authentik groups of e2e-member: ['inventree-users']
[D] refresh -> 400 invalid_grant
[D] state: checked 7203 s ago, blocked=True reason='no_mcp_permission' last_error='' upstream token=none groups=['Users'] live MCP refresh tokens=0
[D] tools/list with the last access token -> 401
[D] tools/list with an InvenTree API token of the blocked user -> 401 'Access revoked, sign in to InvenTree again.'
[E] state: checked 2 s ago, blocked=False reason='' last_error='' upstream token=present groups=['Users'] live MCP refresh tokens=0
[E] SSO login while outside the group: authorize -> 403 ('No MCP access' shown: True)
[F] connected through the façade: expires_in=3600 scopes=12
[F] tools/list -> 200
OK: membership re-check end to end
EXIT 0
```

**WP1 smoke** (`python3 dev/oauth_smoke.py --sso e2e-member`):

```
[14] refresh with the just-revoked refresh token -> 400 invalid_grant
OK: OAuth façade flow completed (SSO as e2e-member)
```

## Next-stage readiness

WP3 can start now. Its work is mostly in `policy.py`, `api.py`, `mcp_transport.py` and the `MCPRuleSet` model. The interfaces it consumes are in place: `may_connect`, `is_blocked`, `revoke_all_mcp_tokens`, `block_user` / `unblock_user`, `recheck_due`, `MCPUserState`, `auth/signals.connect_signals`, and the `FakeIdP` / `MembershipTestCase` fixtures.

Before or with WP3:

1. **F1:** the transport calls `policy.may_connect`. WP3 touches the same lines, so it can take this fix.
2. **F2:** at least the sweep time budget. The lease restructuring can follow in WP5.
3. **The WP3 guidance above:** separate identity refusals from `no_mcp_permission` in `membership._check_upstream`, return a refusing outcome for the latter, and put `MCP_REQUIRED_GROUPS` into `effective_scopes`.
4. **F4** together with item 3, since both decide what counts as an identity refusal.
5. Orchestrator decisions on F6 (blocks while enforcement is off) and F7 (deny long-lived credentials after grace or re-login). Record them in the overview.

F3, F5 and F8-F12 can go to WP5 (hardening).
