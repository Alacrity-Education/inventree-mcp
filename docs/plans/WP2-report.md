# WP2 report: membership re-check against Authentik

Microplan: `docs/plans/WP2-membership-recheck.md`. Branch `feature/oauth-facade`, on top of WP1 and its review fixes. Nothing is staged or committed; the orchestrator commits.

## What was built

| File | Content |
|---|---|
| `inventree_mcp/models.py` | `MCPUserState`, with the fields from the overview (`user` one-to-one, `related_name="mcp_state"`, `membership_checked_at`, `access_revoked_at`, `revoked_reason`, `last_recheck_error`, `last_login_at`). `check_user_permission(user, permission, obj=None)`: active staff may do anything; a user may `view` their own row when the row is passed in. Core calls it without a row, so in that case only staff pass. |
| `inventree_mcp/migrations/0001_initial.py` | Generated in the container with `dev/manage.sh makemigrations inventree_mcp`. It is owned by the host user. The live instance applied it at restart (`invoke update`). |
| `inventree_mcp/auth/sso.py` (new) | `sso_app(request=None)`, `sso_provider_id(request=None)`, `provider_id_of(app)`, `OIDC_PROVIDER`. Moved out of `login_bridge.py` because the bridge and the re-check both need it. `login_bridge` imports `sso_provider_id` from here, so the existing patch target `inventree_mcp.auth.login_bridge.sso_provider_id` still works. |
| `inventree_mcp/auth/membership.py` | The real implementations. Public: `Outcome`, `is_blocked`, `token_grant_allowed`, `refresh_grant_allowed`, `recheck(user, *, force=False)`, `block_user`, `unblock_user`, `revoke_all_mcp_tokens` (unchanged from WP1), plus `recheck_due(user)`, `enforced()` and `models_available()`. Details below. |
| `inventree_mcp/auth/policy.py` | `in_required_groups(user)` checks `MCP_REQUIRED_GROUPS` (comma separated InvenTree group names, case-insensitive, empty means no requirement). `may_connect` now runs: anonymous, inactive, blocked, then not in the required groups (`no_mcp_permission`). `effective_scopes` and `narrow` are untouched. |
| `inventree_mcp/auth/signals.py` (new) | `on_user_logged_in` is a receiver on allauth's `user_logged_in`. An SSO login through the MCP sign-in provider sets `last_login_at` and `membership_checked_at`, and clears `access_revoked_at`, `revoked_reason` and `last_recheck_error`. Any other login sets only `last_login_at`. The receiver never breaks a login. `connect_signals()` uses a `dispatch_uid`. |
| `inventree_mcp/auth/tasks.py` | `recheck_user(user_id)` is the background re-check. `membership_sweep()` runs hourly; see "Check points". |
| `inventree_mcp/auth/token.py` | The reorder and R1 (see "WP1 residual items"). |
| `inventree_mcp/auth/settings_inject.py` | R3: a `request_started` receiver (`_before_request`). |
| `inventree_mcp/mcp_transport.py` | After authentication and before the identity is bound: a blocked user gets `401` with the JSON-RPC message "Access revoked, sign in to InvenTree again." and the challenge header. An authenticated request that is not OAuth2 (ApiToken or Basic) queues a background re-check when one is due. |
| `inventree_mcp/core.py` | Adds the `[2]` settings with validators: `MEMBERSHIP_ENFORCED`, `MEMBERSHIP_RECHECK_INTERVAL_MINUTES` (≥ 1), `MEMBERSHIP_GRACE_HOURS` (≥ 0), `RELOGIN_INTERVAL_HOURS` (≥ 1) and `MCP_REQUIRED_GROUPS`. Adds `SCHEDULED_TASKS["membership_sweep"]` (`H`) and calls `signals.connect_signals()` in `__init__`. |
| `pyproject.toml` | `migrations` is added to ruff's `exclude`. The generated migration otherwise fails RUF012 (mutable class attributes). |
| `inventree_mcp/tests/test_membership.py` (new) | Spec tests 1 to 11 plus extras, 27 tests in total. See "Tests". |
| `inventree_mcp/tests/test_auth_token.py` | Two new tests: R1, and "dead tokens never reach the membership gate". |
| `inventree_mcp/tests/test_settings_inject.py` | One new test for R3. |
| `inventree_mcp/tests/test_auth_authorize.py` | Import and docstring now point to `auth/sso.py`. |
| `dev/membership_e2e.py` (new) | The end-to-end script against the real dev Authentik. |
| `README.md`, `AGENTS.md` | README: new "Membership re-checks" section with the check-point table, the outcome table and the Authentik prerequisites (`offline_access`, refresh validity ≥ 30 days, groups claim and group sync, group binding, `MCP_REQUIRED_GROUPS`). README also gets the five new settings and the app integration and migrate note. AGENTS.md: file map (`sso.py`, `signals.py`, `membership.py`, `tasks.py`, `models.py`, transport, tests) and the e2e command. |

### How the re-check works (`membership.recheck`)

1. If `MEMBERSHIP_ENFORCED` is off, or the plugin's Django app is not installed, return `DISABLED`. If the check is not due and `force` is not set, return `NOT_DUE`. Both are cheap reads.
2. Inside `transaction.atomic()`, run `MCPUserState.objects.select_for_update().get_or_create(user=user)`. Under the lock:
   - a blocked row returns `NOT_MEMBER` with no outbound call;
   - a row that another worker has just checked returns `NOT_DUE`.
3. Look up the upstream credentials:
   - `sso.sso_app(None)` gives the app;
   - its `SocialAccount` is matched on `provider = provider_id`;
   - its `SocialToken` is matched on `app=app`, or `app IS NULL` for settings-backed apps, as allauth's `_store_token` stores them.

   No app, a provider that is not `openid_connect`, no account, or an empty `token_secret` gives `NO_UPSTREAM_TOKEN`. The cause is stored as a code (`no_provider`, `unsupported_provider`, `no_token`). A warning is logged at most once per user per hour (`cache.add` marker).
4. Discovery uses allauth's `OpenIDConnectOAuth2Adapter(None, provider_id)`:
   - the plugin fetches `provider.server_url` itself, with `timeout=5`;
   - the document is cached in Django's cache for 10 minutes;
   - it is then seeded into the adapter's `_openid_config` memo, so `access_token_url`, `profile_url` and `basic_auth` are allauth's own.
5. Redeem: `POST token_endpoint` with `grant_type=refresh_token`. Client authentication follows `adapter.basic_auth`. The outcomes are:
   - 200 with an `access_token`: continue;
   - 400 with `error=invalid_grant`: `NOT_MEMBER` (revoked upstream);
   - any other status, a timeout, a connection error or malformed JSON: `UNREACHABLE`, with codes `http_4xx`, `http_5xx`, `timeout`, `connection_error`, `bad_response`, `discovery_*` or `bad_discovery`.
6. The rotated tokens are stored at once on the same `SocialToken` row, through `adapter.parse_token()`: `token`, `token_secret` (only when a new one came back) and `expires_at`. This happens outside the savepoint of step 7, so a later failure cannot roll it back.
7. In a savepoint:
   - The ID token is verified with `jwtkit.verify_and_decode`: JWKS from discovery, `issuer` from discovery, `audience` = the app's `client_id`.
   - The subject must equal `SocialAccount.uid` (allauth's `uid_field`, default `sub`).
   - Failures give `UNREACHABLE` (`bad_id_token`, `subject_mismatch`).
   - The groups claim (`SSO_GROUP_KEY`) is read from the ID token, or else from userinfo with the new access token. Userinfo must carry the same subject. A list or a single string is accepted; if absent, the list is empty.
   - The claim is written to `SocialAccount.extra_data["userinfo"][key]`, then `InvenTree.sso.ensure_sso_groups(None, SocialLogin(account=account))` runs.
   - Finally, `policy.may_connect(<fresh user>)` decides.
8. The outcome:
   - `MEMBER`: stamp `membership_checked_at` and clear `last_recheck_error`.
   - A refusal reason: `block_user(user, reason)` and `NOT_MEMBER`. The reason is `no_mcp_permission`, `inactive`, or `invalid_grant` for an upstream revocation.
   - Unreachable: store the code and leave `membership_checked_at` as it was.
   - An unexpected exception is treated as `UNREACHABLE` with code `error`. Its type name is logged at WARNING and the traceback at DEBUG only.

`block_user` runs in one transaction. It stamps `access_revoked_at` and `revoked_reason`, calls `revoke_all_mcp_tokens(user)` (DOT's `revoke()`), deletes the provider's `SocialToken` rows, and logs one WARNING line with the user id, the reason and the count. `unblock_user` clears both fields. `is_blocked` is a single indexed `exists()`. It is `False` while enforcement is off, because the overview calls `MEMBERSHIP_ENFORCED` the master switch for re-checks and blocking.

### Decision functions

- `refresh_grant_allowed(user)` follows the spec table exactly:
  - enforcement off: `(True, "")`;
  - blocked: `invalid_grant`;
  - `recheck()` returns `MEMBER`, `NOT_DUE` or `DISABLED`: allowed;
  - `NOT_MEMBER`: `invalid_grant`;
  - `UNREACHABLE`: allowed while `membership_checked_at` is younger than `MEMBERSHIP_GRACE_HOURS`;
  - `NO_UPSTREAM_TOKEN`: allowed while `MCPUserState.last_login_at`, or else `user.last_login`, is younger than `RELOGIN_INTERVAL_HOURS`.

  An exception escaping `recheck()`, for example a failure to take the lock, counts as `UNREACHABLE`, so the grace rule decides.
- `token_grant_allowed(user)` returns `(not blocked, "invalid_grant" if blocked else "")`.
- I confirmed that WP1's token view refuses before delegating to DOT. A refused refresh is answered from `_check_refresh_grant` before `super().post()`, so nothing is rotated or revoked unless `recheck()` itself blocked the user. The live e2e (scenario C) and `UnreachableTest` both show this.

### Check points

- **Login:** `auth/signals.py`, as above.
- **Refresh grant:** `token.py` calls `membership.refresh_grant_allowed` (WP1's call site).
- **`/mcp`:** a blocked user gets `401`. For ApiToken and Basic credentials, `_queue_membership_recheck()` runs when `recheck_due(user)`. It queues `offload_task("inventree_mcp.auth.tasks.recheck_user", user.pk, force_async=True, check_duplicates=True)`. A 60 s cache marker per user keeps a burst of requests from queueing and scanning the queue again. Any error is logged and swallowed, and the request never waits.
- **Sweep:** `tasks.membership_sweep()` takes users who hold an unrevoked refresh token of a façade application (`clients.mcp_applications()`, only when the façade is supported) or an active `ApiToken` (not revoked, expiry empty or in the future). It skips blocked users (an `Exists` on `MCPUserState`), checks `recheck_due` per user, and calls `recheck` for those that are due. It logs one line with counts, for example `membership sweep: member=1, no_upstream_token=1`. One user's failure does not stop the sweep. With enforcement off it does nothing.

### WP1 residual items handled here

- **R1:** in `_check_refresh_grant`, a revoked row inside the grace window with no successor access token now calls `_revoke_family()` before it is refused. That mirrors DOT's `validate_refresh_token`: when `REFRESH_TOKEN_REUSE_PROTECTION` is on and the row has a `token_family`, every unrevoked family member gets `revoke()`. For a plain revocation this is a no-op. Test: `test_stale_token_replayed_in_grace_revokes_the_family` (RT1 → RT2 → RT3, then RT1 is replayed: 400, RT3 is revoked, RT3's access token gets 401).
- **Reorder:** dead tokens are sorted out before the membership gate:
  - a revoked row past the grace window goes straight to DOT, which refuses it and revokes the family;
  - a revoked row inside the window without a successor is refused (and R1 applies);
  - only a rotation inside the window (a successor exists) goes through `refresh_grant_allowed` and then to DOT, which re-serves the pair.

  Every existing token test stays green, including `test_membership_gate_runs_for_rotated_tokens_too`. New test: `test_dead_tokens_never_reach_the_membership_gate`. It covers one token revoked inside the window and one rotated token past it, and asserts that neither `refresh_grant_allowed` nor `recheck` was called.
- **R3:** `settings_inject.connect_signals()` also connects Django's `request_started` to `_before_request()` (`ensure_applied()`, exceptions swallowed, `dispatch_uid`). The 5 s memo is kept. So a worker that only serves `/api/` or `/o/` also follows setting changes, and deactivating the plugin restores DOT's values in web processes. README updated. Test: `test_any_request_resyncs_after_the_memo_expires`. The value is changed without a signal, the memo expires, and `GET /api/` brings DOT to the new lifetime.

## Deviations from the spec, with reasons

1. **The required-groups refusal reason is `no_mcp_permission`, not `not_in_required_groups`.** The overview's binding reason set for `may_connect` is `{"anonymous", "inactive", "blocked", "no_mcp_permission"}`. `authorize.py` already has the right sentence for it ("None of your groups grant MCP access"). So `revoked_reason` reads `no_mcp_permission` for a lost group.
2. **Client authentication at the upstream token endpoint uses allauth's `adapter.basic_auth`, not "basic preferred".** This is the same choice allauth makes for the login's code exchange. It honours the app setting `token_auth_method`; otherwise it uses `client_secret_post` when the provider lists it and `client_secret_basic` only when post is not offered. Reusing it keeps the login and the re-check consistent, per hard rule 1. Authentik offers both, so post is used.
3. **The discovery document is fetched by the plugin and then seeded into allauth's adapter** (`adapter._openid_config`, the adapter's own memo attribute). allauth's `openid_config` fetches on every adapter instance, does not cache, and uses allauth's timeout. Seeding gives the spec's explicit 5 s timeout and the 10-minute cache, while keeping allauth's endpoint and auth-method logic. This depends on a private attribute name, which is commented.
4. **JWKS timeout:** the JWKS request is made inside `jwtkit.verify_and_decode`, through allauth's `get_requests_session()`. Its timeout is allauth's `SOCIALACCOUNT_REQUESTS_TIMEOUT`. That defaults to 5 s, and InvenTree does not set it. Passing an explicit value would mean re-implementing `jwtkit.fetch_key`. Test 1 asserts that every recorded outbound call, the JWKS fetch included, had a 5 s timeout.
5. **A refresh response without an ID token is not counted as unreachable.** The subject and the groups are then taken from userinfo, called with the new access token, and the subject must match. Some providers do not reissue ID tokens on refresh. Without this, every refresh for such providers would end in `invalid_grant` once the grace period ran out. This matches DESIGN §7.1 ("from userinfo with the new access token (same subject)").
6. **`is_blocked()` is `False` while `MEMBERSHIP_ENFORCED` is off** (master switch). Blocks stay in the table and apply again when enforcement is turned back on.
7. **A blocked user's `recheck()` returns `NOT_MEMBER` without an outbound call.** The upstream token was deleted at the block anyway, and only an SSO login lifts a block.
8. **The login receiver is connected to allauth's `user_logged_in` only.** That covers all InvenTree logins (password and SSO), which go through allauth. Logins that bypass allauth, such as the Django admin form or the test client's `force_login`, do not stamp. `refresh_grant_allowed` falls back to `user.last_login` for them, as the spec allows.
9. **A 60 s per-user cache marker in front of `offload_task`** in the transport, in addition to `check_duplicates=True`. Core's duplicate check iterates the whole django-q queue, and it would otherwise run on every API-token request until the worker picks the task up.
10. **The sweep excludes blocked users through `Exists(MCPUserState…)` rather than `mcp_state__…`.** See "What WP3 must know".

Not a deviation, but worth knowing: when no SSO provider is resolvable, `recheck` answers `NO_UPSTREAM_TOKEN` (`no_provider`), as the spec says. Installations where MCP users sign in only with local passwords therefore enter the 24-hour re-login mode. README and the setting description say to turn `MEMBERSHIP_ENFORCED` off there.

## Tests

`inventree_mcp/tests/test_membership.py` fakes the identity provider at the HTTP boundary. `requests.Session.request` is replaced by `FakeIdP`, so both `membership.py`'s calls and jwtkit's JWKS fetch reach it. ID tokens are signed with a per-test RSA key whose JWKS the fake serves, and the SSO app is settings-backed, as in the dev stack. Each spec case maps to tests as follows:

1. `MemberRecheckTest`:
   - `test_member_recheck_rotates_and_syncs_groups`: stamp, rotated tokens stored on the same `SocialToken` row, mapped `Users` added and unmapped `Stale` removed by core's sync, every call with a 5 s timeout;
   - `test_refresh_grant_runs_the_recheck_when_due`;
   - `test_groups_from_userinfo_when_the_id_token_lacks_them`.
2. `NotMemberTest.test_removed_from_group_is_blocked_until_the_next_sso_login`:
   - the refresh gets 400 `invalid_grant`;
   - blocked, façade tokens revoked, upstream token deleted;
   - the next refresh gets 400 with no provider call;
   - MCP bearer 401 with the message and challenge;
   - authorize 403 `blocked`;
   - an SSO login (the signal) lifts the block, and authorize and MCP work again.
3. `NotMemberTest.test_upstream_invalid_grant_blocks`, plus `test_inactive_user_is_blocked`.
4. `UnreachableTest`, with subtests for timeout, connection error, 503, malformed JSON, bad signature and wrong subject:
   - within grace the refresh succeeds and the same upstream token is kept (rotated and kept when the token was already redeemed);
   - past grace: 400 `invalid_grant`, nothing blocked or revoked, the access token still works, and the same DOT refresh token works once the provider is back;
   - `test_without_id_token_userinfo_confirms_the_subject`.
5. `NoUpstreamTokenTest`: allowed within the re-login interval and refused after; fallback to `user.last_login`; the warning is logged once over three re-checks.
6. `DueAndEnforcementTest.test_not_due_makes_no_call_and_force_does`, plus `test_never_checked_is_due`.
7. `DueAndEnforcementTest`:
   - `test_enforcement_off`: `DISABLED`, no calls, refresh allowed, an existing block ignored;
   - `test_required_groups`: case-insensitive, authorize 403 `no_mcp_permission`, ignored when empty;
   - `test_required_groups_apply_with_enforcement_off`.
8. `LoginSignalTest`: an SSO login stamps and unblocks; a password login, or a login through another provider, only sets `last_login_at`. The real `user_logged_in.send(...)` is used, with a session-backed request for allauth's other receivers.
9. `TransportTest`:
   - a stale check queues exactly one `offload_task` over two requests, and the request makes no provider call;
   - a fresh check, or an OAuth token, queues nothing;
   - `recheck_user` runs;
   - a blocked API-token user gets 401 with `error="invalid_token"` in the challenge.
10. `SweepTest`: only due users with a live façade refresh token or an active API token are re-checked. Excluded: users that are not due, have no tokens, are blocked, or hold only a revoked API token. Nothing happens when not enforced.
11. `MemberRecheckTest.test_second_recheck_in_one_interval_makes_no_call`: two re-checks make one call. A caller that read "due" before the lock re-reads under it and makes no call. This is the in-lock re-read.

Extra: `ModelPermissionTest`.

Command output (inside the dev instance, InvenTree 1.5.7, DOT 3.4.0, allauth 65.18.0):

```
$ dev/test.sh inventree_mcp
Ran 258 tests in 213.827s
OK (skipped=3)            (the WellKnownRedirectTest skips: no /.well-known/ plugin index on 1.5.7, as in WP1)

$ dev/test.sh inventree_mcp.test_mcp
Ran 132 tests in 33.059s
OK

$ dev/test.sh inventree_mcp.tests.test_membership
Ran 27 tests in 70.745s
OK

$ dev/test.sh inventree_mcp.tests.test_auth_token
Ran 20 tests in 19.055s
OK

$ dev/test.sh inventree_mcp.tests.test_auth_flow
Ran 1 test in 6.803s
OK

$ dev/test.sh inventree_mcp.tests.test_settings_inject
Ran 9 tests in 12.559s
OK
```

Lint:

```
$ uvx ruff@0.16.0 check --preview inventree_mcp                          -> All checks passed!
$ uvx ruff@0.16.0 format --preview --check inventree_mcp                 -> 51 files already formatted
$ uvx ruff@0.16.0 check --preview --isolated inventree_mcp/tests         -> All checks passed!
$ uvx ruff@0.16.0 format --preview --isolated --check inventree_mcp/tests -> 9 files already formatted
$ uvx ruff@0.16.0 check --preview --isolated dev/membership_e2e.py       -> All checks passed!
$ uvx ruff@0.16.0 check [--preview] .   and   uvx ruff@0.17.0 check [--preview] .   -> All checks passed!
```

## End to end against the dev Authentik

`dev/compose.sh restart inventree-server inventree-worker`, then `python3 dev/membership_e2e.py`. The script sets `OAUTH_ENABLED`, `MEMBERSHIP_ENFORCED` and `MCP_REQUIRED_GROUPS="MCP Users"` through `dev/shell.sh`. At the end it restores `MCP_REQUIRED_GROUPS`, e2e-member's Authentik groups and Authentik itself, and deletes its API token. It reuses `oauth_smoke.py`'s helpers (`sso_login_through_bridge`, `parse_forms`, `refresh`, `mcp_tools_list`, `step`, `expect`) and `dev/authentik`'s helpers (`Browser`, `absolute`, `sso_login`, `set_user_groups`, `user_groups`, `api_from_env`, `public_url`). It never prints tokens. The upstream token is compared by a hash, which is never printed. Scenario C stops and starts `authentik-server`; `--skip-outage` skips it.

```
[0] settings: MCP_REQUIRED_GROUPS='MCP Users', re-check interval 60 min, grace 4 h; Authentik groups of e2e-member: ['inventree-mcp', 'inventree-users']
[6] bridge form: POST /api/auth/v1/auth/provider/redirect provider=authentik process=login callback_url=http://localhost:8080/plugin/inventree-mcp/oauth/authorize/
[6] provider redirect -> 302 to http://authentik.localtest.me:19000/application/o/authorize/
[6] Authentik as 'e2e-member' -> back on /plugin/inventree-mcp/oauth/authorize/ (page, 200)
[A] connected through the façade: expires_in=3600 scopes=12
[A] tools/list -> 200
[A] state: checked 2 s ago, blocked=False reason='' last_error='' upstream token=present groups=['MCP Users', 'Users'] live MCP refresh tokens=2
[B] last check aged to 2 h, refresh -> 200; upstream refresh token rotated: True
[B] state: checked 2 s ago, blocked=False reason='' last_error='' upstream token=present groups=['MCP Users', 'Users'] live MCP refresh tokens=2
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
[F] Authentik groups of e2e-member: ['inventree-mcp', 'inventree-users']
[6] bridge form: POST /api/auth/v1/auth/provider/redirect provider=authentik process=login callback_url=http://localhost:8080/plugin/inventree-mcp/oauth/authorize/
[6] provider redirect -> 302 to http://authentik.localtest.me:19000/application/o/authorize/
[6] Authentik as 'e2e-member' -> back on /plugin/inventree-mcp/oauth/authorize/ (page, 200)
[F] connected through the façade: expires_in=3600 scopes=12
[F] tools/list -> 200
[F] state: checked 2 s ago, blocked=False reason='' last_error='' upstream token=present groups=['MCP Users', 'Users'] live MCP refresh tokens=1
OK: membership re-check end to end
```

(The two live refresh tokens in A and B include one from an earlier run's scenario F.)

This confirms what DESIGN §7.1 reports. After e2e-member is removed from `inventree-mcp`, Authentik still answers the refresh grant, with a `groups` claim that lacks the group. Core's sync then removes `MCP Users`, and the plugin's policy blocks the user.

Other live checks after the final restart:

- **Background re-check through the real django-q worker:** an ApiToken request to `/mcp/` with a check aged 2 h gets 200, then the worker logs:
  ```
  INFO Process-… processing … 'inventree_mcp.auth.tasks.recheck_user' [inventree]
  INFO inventree-mcp: membership re-check for user id 9: member (issuer authentik.localtest.me)
  INFO Processed 'inventree_mcp.auth.tasks.recheck_user' (…)
  ```
- **Sweep, in `dev/shell.sh`:** `membership sweep: member=1, no_upstream_token=1` (e2e-member, and admin with a password login holding a façade token), then on a second run `no_upstream_token=1, not_due=1`.
- **Schedules:** both `inventree_mcp.auth.tasks.membership_sweep` and `cleanup_stale_clients` are registered in django-q's `Schedule`.
- **WP1 smoke tests still pass:** `python3 dev/oauth_smoke.py --sso e2e-member` and `python3 dev/oauth_smoke.py` both print `OK: OAuth façade flow completed`.
- **Log lines written by the plugin during the run:**
  ```
  inventree-mcp: membership re-check for user id 9: member (issuer authentik.localtest.me)
  inventree-mcp: membership re-check for user id 9 inconclusive (connection_error), issuer authentik.localtest.me
  inventree-mcp: refresh refused for user id 9: identity provider unreachable past the grace period
  inventree-mcp: MCP access blocked for user id 9 (no_mcp_permission), 1 tokens revoked
  inventree-mcp: SSO login lifted the MCP block of user id 9
  inventree-mcp: authorization denied (no_mcp_permission) for user id 9
  ```

## Known limitations

- **Core's group sync logs the username.** `InvenTree.sso.ensure_sso_groups` logs "Removing group MCP Users from e2e-member" and "Adding group … to user …" at INFO. The re-check calls it, exactly as a login does. With SSO usernames that are e-mail addresses, this puts an address in the log. That is core's logging; the plugin's own lines carry only ids, codes and the issuer host. A fix belongs upstream: log `user.pk` in `sso.py`.
- **No group sync, no group-based block.** Without `LOGIN_ENABLE_SSO_GROUP_SYNC` (and a `SSO_GROUP_MAP` that maps the MCP group, plus `MCP_REQUIRED_GROUPS` or WP3's rulesets), a re-check only notices a revoked, expired or disabled upstream grant, not a removed group. README says so. Until WP3, `MCP_REQUIRED_GROUPS` is the only membership criterion; the WP1 stub `effective_scopes` gives every active user all scopes.
- **Local changes reach users without an upstream token late.** Removing a user from an `MCP_REQUIRED_GROUPS` group inside InvenTree applies to refreshes at the next due re-check, at most `MEMBERSHIP_RECHECK_INTERVAL_MINUTES` later. For users without an upstream token, `NO_UPSTREAM_TOKEN` never evaluates the policy, so it applies only at the re-login after `RELOGIN_INTERVAL_HOURS`; authorize applies it immediately. WP3's narrowing at every refresh closes this for rulesets.
- **API tokens stay usable while the provider is down.** The background re-check never denies a request: an ApiToken or Basic user is cut off only by a block. KINA additionally denies static tokens once the grace period has passed with the provider unreachable; the spec does not ask for that.
- **R2 (the race between the pre-check and DOT's locked save) is not addressed.** It was not assigned.
- **The login stamp does not check the token.** An SSO login stamps `membership_checked_at` even when Authentik issued no refresh token (no `offline_access`). The next due re-check reports `NO_UPSTREAM_TOKEN` with the warning.
- **The membership tests skip on installs without allauth's OIDC provider.** `test_membership.py` skips when `allauth.socialaccount.providers.openid_connect` is not in `INSTALLED_APPS`, that is, when InvenTree has no `social_backends`; the re-check needs allauth's provider registry. The dev stack has it. CI currently runs only `inventree_mcp.test_mcp`, so this does not change CI. WP5 should set `INVENTREE_SOCIAL_BACKENDS=openid_connect` when it adds `inventree_mcp.tests` to CI.
- **The upstream token is stored in clear, as allauth stores it.** It stays in allauth's `SocialToken.token_secret`, with no plugin-side encryption. This was decided in the spec and is documented.
- **The row lock is held across the outbound calls**, up to four calls of 5 s each. Concurrent refreshes of the same user wait rather than redeem twice. That is intended, but a slow provider delays that user's parallel refreshes.

## What WP3 must know

- **Where `policy.may_connect` is consulted:**
  - `authorize.py` `get()` and `form_valid()` (WP1);
  - `membership._check_upstream()` after the group sync, on a fresh user instance. Any `(False, reason)` there **blocks** the user (`block_user(user, reason)`), revokes all MCP tokens and deletes the upstream token.

  So if WP3's `may_connect` returns `no_mcp_permission` for "no MCP ruleset", a re-check will block such users until their next SSO login. That is right for a lost SSO group. WP3 should decide whether a ruleset an admin removed locally should also block, or only narrow. Narrowing to nothing at refresh already revokes the presented token.
- **Where `policy.effective_scopes` and `narrow` are consulted:** unchanged from WP1. `narrow` runs in `authorize.validate_authorization_request`, `authorize.form_valid` and `token._check_refresh_grant`, the last after the membership gate and only for live (unrevoked) refresh tokens. The membership code never calls `effective_scopes`.
- **`may_connect` order:** anonymous, inactive, blocked (`membership.is_blocked`), `in_required_groups` (`no_mcp_permission`). WP3 should add its ruleset check after these and keep the reason set from the overview.
- **Signals in place:**
  - allauth `user_logged_in` → `auth.signals.on_user_logged_in` (`dispatch_uid="inventree_mcp.auth.signals.user_logged_in"`);
  - `settings_inject`: `post_save` of `PluginSetting` and `PluginConfig` (memo invalidation), django-q `pre_execute`, and now Django `request_started`.

  All are connected from the plugin's `__init__` with `dispatch_uid`. WP3's ruleset-change revocation belongs in `auth/signals.py` next to the login receiver, connected in the same `connect_signals()`.
- **Do not query through `mcp_state` from `User`** (`User.objects.filter(mcp_state__…)`). InvenTree registers plugin apps after the `User` model's reverse relations are built. In running processes, such a lookup raises `FieldError: Cannot resolve keyword 'mcp_state'`. The test runner hides this, because there the lookup works. The attribute `user.mcp_state` does work. Query `MCPUserState.objects.filter(user=…)` or use `Exists(...)`, as `tasks.membership_sweep` does. The same will apply to `MCPRuleSet` and `group.mcp_rule_sets`: keep `MCPRuleSet.objects.filter(group=…)` in queries.
- **Models and migrations:** `0001_initial` exists; WP3 adds `0002_…` with `dev/manage.sh makemigrations inventree_mcp`. `migrations/` is excluded from ruff. Models need app integration (`ENABLE_PLUGINS_APP`); `membership.models_available()` guards the code paths that run without it.
- **API for WP3/WP4:** `membership.is_blocked`, `recheck_due`, `block_user`, `unblock_user` and `MCPUserState`'s fields are what an admin "connections / status" endpoint needs. `unblock_user` is the admin's manual unblock. `check_user_permission` gives staff full access and the owner read access when the row is passed.
- **Test fixtures:** `tests/test_membership.py` provides `FakeIdP`, `PROVIDERS` and the `MembershipTestCase` base (SSO account, upstream token, group map, `MCP_REQUIRED_GROUPS="MCP Users"`). WP3 tests that need the full SSO path can reuse them.

## Review fixes applied

Applied after `docs/plans/WP2-review.md`, following the decisions recorded in `docs/plans/00-overview.md` ("Decisions recorded after reviews" and the `Outcome.NO_MCP_PERMISSION` note). Binding signatures are unchanged. The only interface addition is the approved `Outcome.NO_MCP_PERMISSION`. Every outbound call keeps its 5 s timeout. An unreachable provider still never rotates or revokes anything. Nothing is staged or committed.

| Finding | What changed (file, function) | Verified by |
|---|---|---|
| **F1** required groups on `/mcp` | `mcp_transport.py` `MCPView.dispatch` now calls `policy.may_connect(user)` for every credential type: `blocked` → 401 "Access revoked" with the challenge; `inactive` / `no_mcp_permission` → 403 JSON-RPC "No MCP permission" with no challenge. Only allowed users queue the background re-check. | `TransportTest.test_user_outside_required_groups_gets_403`: an API token and an OAuth token both get 403, nothing is queued, `/api/user/me/` still works. Live: e2e `[E] after an admin unblock: tools/list with the API token -> 403 'No MCP permission'`. |
| **WP3 guidance**: identity vs authorisation | `membership.py`: `_check_upstream` returns `(verdict, reason)`. The new `_classify(fresh)` decides: inactive → identity refusal `inactive`; `not policy.in_required_groups` → identity refusal `not_in_required_groups` (`NOT_IN_REQUIRED_GROUPS`); otherwise `policy.may_connect` decides, where `no_mcp_permission` is an authorisation refusal and any other reason is an identity refusal. Upstream `invalid_grant` still blocks. In `recheck`, an identity refusal → `block_user`, `NOT_MEMBER`. An authorisation refusal stamps `membership_checked_at`, clears `last_recheck_error`, calls `revoke_all_mcp_tokens` only (no block, upstream token kept), logs "member without MCP permission" and returns the new `Outcome.NO_MCP_PERMISSION`. `refresh_grant_allowed` maps that to `(False, "invalid_grant")`. The sweep counts it by `.value`. | `NoMcpPermissionTest.test_member_without_mcp_permission_is_revoked_not_blocked`: refused, not blocked, upstream token rotated and kept, stamp fresh, MCP tokens revoked, log line present, sweep counts `{"no_mcp_permission": 1}`. `NotMemberTest`: a lost group now stores `not_in_required_groups`. Live: e2e `[D] ... blocked=True reason='not_in_required_groups'`. |
| `effective_scopes` | `policy.py` `effective_scopes(user)` returns an empty set when `not in_required_groups(user)`. The stub semantics are otherwise unchanged. | `test_auth_token.TokenEndpointTest.test_refresh_outside_required_groups_is_refused`: narrowing refuses and revokes. `LoginSignalTest.test_sso_login_of_an_unblocked_non_member_blocks_nothing` asserts the empty set. |
| **F2** (minimum) sweep vs django-q timeout | `tasks.py` `membership_sweep(user_ids=None)`: candidate ids are collected first. No new re-check starts after `SWEEP_BUDGET_SECONDS = 60`. The remaining ids go to `_queue_continuation` (`offload_task("inventree_mcp.auth.tasks.membership_sweep", remaining, force_async=True, check_duplicates=True)`), counted as `deferred`. At least one user is re-checked per run, so a continuation always makes progress. A continuation re-checks only its ids that are still unblocked and still due. README ("The hourly sweep runs as one background task …") and this report document the 90 s `background.timeout` interaction: one re-check can take about 20 s (four calls with a 5 s timeout), so 60 + 20 stays under 90. | `SweepTest.test_sweep_stops_at_its_time_budget_and_queues_the_rest`: a fake clock; one user is re-checked, `{"member": 1, "deferred": 2}`, the exact continuation call is asserted, and the continuation re-checks exactly the two deferred users. Live: `dev/shell.sh` → `membership sweep: no_upstream_token=1, not_due=1`. |
| **F3** back-off | `membership.py` `_check_upstream`: after `timeout`, `connection_error` or any `*http_5xx` (`_starts_backoff`), a per-provider cache marker `_backoff_key(provider_id)` is set for `PROVIDER_BACKOFF_SECONDS = 60`. While it is present, `_check_upstream` raises `_Unreachable(<cached code>)` without any call, and grace decides as before. The marker is checked before the call and set only on a real failure, so the back-off does not extend itself. | `UnreachableTest.test_provider_backoff_after_an_outage` (subtests timeout / connection_error / 503): three further re-checks make no call and keep the code; after the marker expires the provider is asked again (MEMBER). `test_no_backoff_for_answers_of_a_reachable_provider` (bad signature → no back-off). The existing unreachable subtests clear the cache between modes. |
| **F4** login lifts blocks | `signals.py` `on_user_logged_in`: an SSO login always stamps `membership_checked_at`, `last_login_at` and clears `last_recheck_error`. It lifts the block only when `user.is_active and policy.in_required_groups(user)`. Otherwise the block stays and an INFO line ("kept the MCP block") is logged. No new block is set at login. | `LoginSignalTest.test_sso_login_of_a_non_member_keeps_the_block`, `test_sso_login_of_an_inactive_user_keeps_the_block`, `test_sso_login_of_an_unblocked_non_member_blocks_nothing`; the existing stamp-and-unblock and password-login tests still pass. Live: e2e `[E] state: checked 2 s ago, blocked=True reason='not_in_required_groups'`. |
| **F5** queue churn | `membership.recheck`: after a `NO_UPSTREAM_TOKEN` outcome, it sets the transport's marker `recheck_queued_key(pk)` (shared helper, also used by `_queue_membership_recheck`) for `MEMBERSHIP_RECHECK_INTERVAL_MINUTES * 60`. Because this is set inside `recheck`, it covers the background task, the sweep and the refresh path. README states that the back-off, queue, warning and discovery markers are per process without InvenTree's global cache. | `TransportTest.test_no_upstream_token_holds_the_queue_marker_for_the_interval`: after `recheck_user` → `no_upstream_token`, an API-token request on a due user queues nothing. |
| **F6** blocks while enforcement is off | `membership.is_blocked` no longer depends on `enforced()`, only on `models_available()`. `refresh_grant_allowed` checks the block before the "off" short-cut; otherwise "off" behaves as before (`(True, "")`, `recheck` → `DISABLED`, no new blocks). The README settings row and the `MEMBERSHIP_ENFORCED` description now say existing blocks still apply. | `DueAndEnforcementTest.test_enforcement_off`: `DISABLED`, not due, blocked; refresh 400 and `/mcp` 401; after `unblock_user` the refresh gives 200; no provider call. |
| **F7** long-lived credentials | `membership.py`: the predicates are extracted once as `within_grace(state)` and `within_relogin(user, state)`. `refresh_grant_allowed` uses them, and so does the new `long_lived_credential_allowed(user)`, which judges by the last recorded outcome. A non-empty `last_recheck_error` outside `NO_TOKEN_CODES` (that is, unreachable) past grace → `unreachable_past_grace`. A no-token code (`no_token`, and also `no_provider` / `unsupported_provider`, which are the same `NO_UPSTREAM_TOKEN` outcome) past the re-login interval → `relogin_required`. It is always allowed while not enforced. `mcp_transport.py`: for non-OAuth credentials, the re-check is queued first (so recovery happens once the provider is back), then a refusal → 401 with a clear message and the realm-only challenge `Bearer realm="InvenTree"`. `/api/` is not touched. | `TransportTest.test_unreachable_past_grace_denies_long_lived_credentials` (API token and Basic: 401 with the realm-only challenge; `/api/user/me/` 200; within grace, or with the error cleared: 200; a re-check still queued) and `test_no_upstream_token_past_relogin_denies_long_lived_credentials` (401, `/api/` 200, a fresh login → 200, enforcement off → 200). Live: e2e scenario G. |
| **F8** (optional) | `membership._adapter_class()` builds, on first use, a `CachedConfigAdapter(OpenIDConnectOAuth2Adapter)` whose `openid_config` property returns the cached discovery document. `_discovery(session, app)` only fetches and caches. The private `_openid_config` is no longer touched. | Every re-check test, plus live e2e B (a real rotation through allauth's endpoint and auth-method logic). |
| **F9** userinfo stub | `membership._sync_groups`: a missing `userinfo` is seeded from `extra_data["id_token"]` (or `{"sub": uid}`). | `MemberRecheckTest.test_group_sync_seeds_userinfo_from_the_id_token`. |
| **F10** missing app or table | `membership.warn_if_unenforceable()`, called from `InvenTreeMCP.__init__`, logs one WARNING per process when `MEMBERSHIP_ENFORCED` is on and the app will not be installed. It uses the same condition as core's `AppMixin`: `PLUGIN_TESTING` or `ENABLE_PLUGINS_APP`; at `__init__` the app is not installed yet, so `apps.is_installed` cannot be used. Request-path row reads (`is_blocked`, `recheck_due`, `_state` → `long_lived_credential_allowed` / `refresh_grant_allowed`) go through `_read()`, which runs the query in its own `transaction.atomic()`. On `DatabaseError` it logs once and returns the safe default (not blocked, not due, no state). README notes both. | `MissingTableTest.test_reads_fail_safe_and_log_once`: the table is really renamed inside the test transaction. All reads fail safe, `/mcp` with an API token gives 200, one warning is logged, and the connection stays usable. `test_warning_at_load_without_app_integration`: logged once, and not logged with `PLUGIN_TESTING`. |
| **F11** dev duplication | `dev/oauth_smoke.py` now owns `register(agent, asm, client_name)`, `authorize_url(asm, client_id, **extra)` (random state, PKCE) and `approve_and_exchange(..., **extra)`, which checks that the state comes back. Its `main()` uses them (steps 4, 5, 7-8, 9b). `dev/membership_e2e.py` imports them and its copies are deleted. Both scripts were formatted with `ruff format --isolated`, which reflowed their pre-existing long lines. | Live: both smoke modes and the e2e pass (below). |
| **F12** test gaps | 1. SSO login of a non-member (F4 tests) and an API-token user outside the required groups (F1 test). 2. `test_upstream_invalid_grant_blocks` now mints a fresh `make_token_pair` after the block and asserts 401 "Access revoked". 3. The sweep test adds an expired `ApiToken` and a refresh token of a non-façade (`MANUAL`) application; both are excluded and the counts are unchanged. | `SweepTest.test_sweep_picks_only_due_users_with_live_tokens`, `NotMemberTest.test_upstream_invalid_grant_blocks`. |
| Near-duplicate | The login receiver uses `membership._locked_state(user)` instead of its own `select_for_update().get_or_create`. | `LoginSignalTest`. |
| F13, F14 | Informational, no change. | |

E2E scenarios changed with the decisions:

- D: now expects `not_in_required_groups`.
- E: now expects the block to survive the SSO login. It then shows the F1 403 after an admin `unblock_user`.
- G is new and shows F7 live: 401 with the realm-only challenge, `/api/` unaffected, and recovery through the queued background re-check.

AGENTS.md and README were updated for all of the above.

### Verification

```
$ dev/test.sh inventree_mcp
Ran 273 tests in 235.738s
OK (skipped=3)                (the same three WellKnownRedirectTest skips as before)

$ dev/test.sh inventree_mcp.test_mcp
Ran 132 tests in 33.449s
OK

$ dev/test.sh inventree_mcp.tests.test_membership
Ran 41 tests in 90.739s
OK

$ uvx ruff@0.16.0 check --preview inventree_mcp                            -> All checks passed!
$ uvx ruff@0.16.0 format --preview --check inventree_mcp                   -> 51 files already formatted
$ uvx ruff@0.16.0 check --preview --isolated inventree_mcp/tests           -> All checks passed!
$ uvx ruff@0.16.0 format --preview --isolated --check inventree_mcp/tests  -> 9 files already formatted
$ uvx ruff@0.16.0 check --isolated dev/membership_e2e.py dev/oauth_smoke.py         -> All checks passed!
$ uvx ruff@0.16.0 format --isolated --check dev/membership_e2e.py dev/oauth_smoke.py -> 2 files already formatted
```

After `dev/compose.sh restart inventree-server inventree-worker`, `python3 dev/membership_e2e.py` (including the Authentik outage):

```
[A] connected through the façade: expires_in=3600 scopes=12
[A] tools/list -> 200
[B] last check aged to 2 h, refresh -> 200; upstream refresh token rotated: True
[B] refresh again within the interval -> 200; upstream token unchanged: True
[C] Authentik down, last check 2 h ago (grace 4 h): refresh -> 200; last_error='connection_error'
[C] Authentik down, last check 5 h ago: refresh -> 400 invalid_grant; blocked=False live MCP refresh tokens=2
[C] tools/list with the current access token -> 200 (nothing revoked)
[C] Authentik back: the same refresh token -> 200 (attempt 11); checked 2 s ago
[D] refresh -> 400 invalid_grant
[D] state: checked 7203 s ago, blocked=True reason='not_in_required_groups' last_error='' upstream token=none groups=['Users'] live MCP refresh tokens=0
[D] tools/list with the last access token -> 401
[D] tools/list with an InvenTree API token of the blocked user -> 401 'Access revoked, sign in to InvenTree again.'
[E] state: checked 2 s ago, blocked=True reason='not_in_required_groups' last_error='' upstream token=present groups=['Users'] live MCP refresh tokens=0
[E] SSO login while outside the group: authorize -> 403 ('Access revoked' shown: True)
[E] after an admin unblock: tools/list with the API token -> 403 'No MCP permission'
[E] after an admin unblock: authorize -> 403 ('No MCP access' shown: True)
[F] connected through the façade: expires_in=3600 scopes=12
[F] tools/list -> 200
[F] tools/list with the API token -> 200
[G] last re-check unreachable, last success 5 h ago (grace 4 h): API token on /mcp -> 401 'MCP access suspended: group membership could not be confirmed with the identity provider for too long. Try again later or sign in to InvenTree again.'; WWW-Authenticate: Bearer realm="InvenTree"
[G] the same API token on /api/user/me/ -> 200
[G] after the queued background re-check: API token on /mcp -> 200 (attempt 1); checked 5 s ago, last_error=''
OK: membership re-check end to end
```

The "attempt 11" in C is Authentik's start-up time, now possibly combined with the 60 s provider back-off. The loop allows 12 attempts at 5 s.

`python3 dev/oauth_smoke.py --sso e2e-member`:

```
[14] refresh with the just-revoked refresh token -> 400 invalid_grant
OK: OAuth façade flow completed (SSO as e2e-member)
```

`python3 dev/oauth_smoke.py` (password mode): `OK: OAuth façade flow completed (password login)`.

Live sweep in `dev/shell.sh` (the running process, not the test runner): `membership sweep: no_upstream_token=1, not_due=1`.

### Deferred

- **F2 proper (WP5):** the lease-based restructuring. Under `select_for_update` set a short lease and commit; do the outbound calls and `_store_tokens` in autocommit; then run the group sync, decision and stamp in a short second transaction. Until then:
  - the rotated upstream token is durable only at the end of the re-check's transaction;
  - the row lock is held across the outbound calls, so the login receiver can wait on it;
  - only the sweep's time budget guards against django-q's 90 s kill.

  The back-off (F3) shortens outage-time lock holds to a cache read.
- **F12 item 3:** no concurrent (multi-connection) test of the row lock. The sequential re-read test stays, as the spec asks.
- **Per-process cache markers without Redis:** documented, not changed.
- **F7 also applies to service accounts.** An API-token-only user with no upstream token and no interactive login inside `RELOGIN_INTERVAL_HOURS` is refused on `/mcp` once a background re-check has recorded `no_token` / `no_provider`. That follows the recorded decision ("same windows as refresh grants"). README tells such installations to turn `MEMBERSHIP_ENFORCED` off or give the account an upstream token.
