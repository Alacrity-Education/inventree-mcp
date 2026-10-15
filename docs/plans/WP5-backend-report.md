# WP5 backend hardening: report

Branch `feature/oauth-facade`, based on HEAD `3f33d59`. Nothing is staged or committed in the plugin repository. The upstream worktree (`.worktrees/upstream-patches`, branch `upstream-patches`) has an amended patch-4 commit, `808c179b0` (it replaces `cc50ad2bb`).

Sources: `docs/plans/WP3-WP4-review.md` (findings 4, 5, 6, 8, 16, 17; readiness items 4 and 5) and `docs/plans/WP2-review.md` (F2 "Proper", F12 item 3).

## 1. WP2 F2 proper: the re-check lease

**Lease choice: a database column, not a cache `add`.**

- InvenTree's cache is optional (`INVENTREE_CACHE_ENABLED`). Without Redis every process has its own LocMemCache.
- A cache lease would then not stop a gunicorn worker and the django-q worker from redeeming the same upstream refresh token at the same time. The loser's `invalid_grant` would block the user.
- So the lease is `MCPUserState.recheck_lease_until` (DateTimeField, null), added by `migrations/0003_mcpuserstate_recheck_lease.py` (generated in the container). `RECHECK_LEASE_SECONDS = 60`.

**Code (`inventree_mcp/auth/membership.py`).** `recheck(user, *, force=False)` keeps its signature and now runs three steps.

1. **`_take_lease()`.** A short `transaction.atomic()` with `_locked_state()` (`select_for_update`):
   - re-reads the row;
   - blocked → `NOT_MEMBER`;
   - not due → `NOT_DUE`;
   - a live lease → `NOT_DUE`, logged at DEBUG as "in progress", with no outbound call (also with `force=True`);
   - otherwise it sets `recheck_lease_until = now + 60 s` and commits.
2. **`_check_upstream()` → `_redeem_and_verify()`, in autocommit.** Discovery, redeem, `_store_tokens()`, ID-token verification (JWKS), the subject check and userinfo.
   - The rotated upstream token is committed the moment `_store_tokens()` saves it.
   - `_check_upstream()` now returns `(account, groups)`. The provider back-off is unchanged.
3. **`_recheck_under_lease()`.** A second short transaction under the row lock: `_sync_groups()` and `_classify()` in a savepoint (a failure there is inconclusive, as before), then the block, or the stamp and the revocation.
   - The lease is released in `finally` (`_release_lease()`, compare-and-clear on the lease value).
   - A lease left by a killed process expires after 60 s.

Helpers:

- `_inconclusive()`: one place for the UNREACHABLE recording and logging.
- `_record_error(user, code)`: an `update()` on the row.

Every outcome and side effect matches the outcome table:

- NO_UPSTREAM_TOKEN: error recorded, the hourly warning, the queue marker.
- `invalid_grant`: block.
- Unreachable or unexpected error: error recorded and WARNING.
- IDENTITY_REFUSED: block.
- NO_MCP_PERMISSION: stamp and revoke.
- MEMBER: stamp.

The sweep's budget and the back-off are untouched. `tasks.membership_sweep`'s docstring now says that a user whose lease is held counts as `not_due`.

**One behavioural consequence (accepted by the spec's test b).** A refresh grant that arrives while another process holds the user's lease is allowed (`NOT_DUE`) and does not wait for the in-flight re-check.

- If that re-check then blocks the user, `is_blocked()` refuses the next refresh, and `/mcp` answers 401 for any surviving token.
- If it ends in NO_MCP_PERMISSION, `/mcp` answers 403.

The login receiver (`signals.on_user_logged_in`) only ever waits for steps 1 and 3 now.

**Tests (`tests/test_membership.py`, new class `LeaseTest`; `FakeIdP.on_call` hook added):**

- (a) `test_rotated_token_survives_a_failure_after_the_redeem`: `bad_signature` gives UNREACHABLE; a fresh `SocialToken.objects.get()` has `upstream-rt-1`; the lease is released.
- (a) `test_rotated_token_survives_a_killed_process`: `_store_tokens` is wrapped to raise `_ProcessDied(BaseException)` right after storing.
  - A fresh query has the rotated token and access token, the stamp is unchanged and the lease is released.
  - The next re-check redeems `upstream-rt-1`.
  - Against the old code this test fails: `'upstream-rt-0' != 'upstream-rt-1'`.
- (b) `test_live_lease_means_in_progress_without_a_call`: with a live lease, `recheck()`, `recheck(force=True)` and `refresh_grant_allowed()` all return NOT_DUE / allowed with zero outbound calls, and the foreign lease is left alone.
- (b) `test_expired_lease_is_taken_over`: an expired lease does not block the next re-check.
- (c) `test_no_transaction_is_open_during_the_outbound_calls`: inside every mocked HTTP call, `len(connection.atomic_blocks)` equals the depth before `recheck()`.
  - `in_atomic_block` cannot be used here: it is always true inside Django's TestCase.
  - The committed lease is visible during the redeem, and `sso_login()` (the login receiver) runs inside the mocked token call.
  - Fails against the old code.
- (d) The 42 existing membership tests pass unchanged (47 in the module now).

**Optional F12 item 3 (a two-thread unit test): not done.**

- It needs a `TransactionTestCase`, because TestCase's wrapping transaction hides committed rows from a second connection.
- A `TransactionTestCase` flushes the shared `--keepdb` test database, and the plugin and OAuth fixtures would have to be rebuilt outside `setUpTestData`.
- That risks the rest of the suite. It was replaced by a live two-connection probe in the dev server:
  - a scratch user (`lease-probe-scratch`, deleted afterwards);
  - `_check_upstream` was wrapped to sleep 2 s;
  - thread A ran `recheck(force=True)`;
  - 0.5 s later, thread B ran `recheck(force=True)` and thread C ran the login lock transaction.

  > `results: {'C-login-lock': ('stamped', 0.02), 'B': ('not_due', 0.02), 'A': ('no_upstream_token', 2.04)}`
  > `outbound calls by: ['A']`
  > `lease after: None`

## 2. Token endpoint rate limit

- **`inventree_mcp/auth/ratelimit.py` (new).** The single fixed-window limiter: `cache_key()`, `retry_after(request, scope, setting)` and `too_many_requests()` (429, `{"error": "too_many_requests"}`, `Retry-After`, `Cache-Control: no-store`).
  - It is the body of the old `clients._rate_limited`, moved, not copied.
  - The cache key format is unchanged (`inventree_mcp:dcr:<ip>`; new `inventree_mcp:token:<ip>`).
- **`auth/clients.py`.** `_rate_limited` and its imports were removed; `MCPRegistrationView.post` uses `ratelimit`.
- **`auth/token.py`.** `MCPTokenView.post` applies the limit to `grant_type=authorization_code` only, before the membership hook. Refresh grants are unlimited.
- **`core.py`.** New setting `TOKEN_RATE_LIMIT_PER_MINUTE` (int, default 60, `MinValueValidator(0)`, 0 disables).
- **Tests.**
  - `test_auth_token.TokenEndpointTest.test_code_exchange_rate_limit`, with a fixed clock:
    - two guesses get 400 and the third exchange gets 429 with `Retry-After: 20`;
    - another IP still gets 200;
    - three refreshes get 200;
    - with 0, the limit is off.
  - `oauth_helpers.OAuthFacadeTestMixin.setUp` resets both counters.
  - The existing DCR tests pass unchanged.

## 3. Finding 8: one staff-admin predicate

- `auth/policy.py`: new `is_staff_admin(user)`: active, staff, and `check_user_role(user, "admin", "change")`.
- `auth/api.py`: `IsStaffAdmin.has_permission` calls it, and the duplicate body and import are gone.
- `core.py`: `_is_staff` is replaced by `_is_staff_admin(request)` (calls `policy.is_staff_admin`). It gates the `groups` route, the "MCP Access" nav item and the group panel.
- `tests/test_ui.py`:
  - `UiTestCase.roles = ("admin.change",)`;
  - new user `staff_only`;
  - `test_staff_without_admin_role_sees_no_staff_entries` (routes, nav and panel);
  - `test_staff_without_admin_role_session` (the UI feature API).

## 4. Finding 6: log once per process

- `core.py`: a module-level `_logged_once` set and `_log_once(level, message, *args)`.
- Used for the "no plugin routes" INFO (`get_ui_navigation_items`) and the per-bundle "is missing" WARNING (`_ui_source`).
- Tests:
  - `test_no_route_support_is_logged_once_per_process`: three navigation calls give one INFO line;
  - `test_missing_build_does_not_raise`: two rounds of routes and panel give exactly three WARNINGs, one per bundle.

## 5. Finding 16

- `auth/api.py`: `effective_document()` and `StatusView.get()` use `policy.connect_scopes()`, which is one pass instead of `narrow()` plus `may_connect()`.
  - Consequence: `scopes` is now empty whenever `may_connect` is false (for example a blocked user), as on the MCP endpoint.
  - The serializers' `scopes` help text says so.
  - New test `test_api.ConnectionsAndStatusApiTest.test_blocked_user_has_no_scopes`.
- `auth/signals.py`: `ensure_rulesets_for(group_ids)` takes pks. `ensure_rulesets(group)` passes `[group.pk]`, and `MCPRuleSetList.get` passes `Group.objects.values_list("pk", flat=True)`.

## 6. Finding 17 (backend)

`tests/test_policy.TransportNarrowingTest.test_write_call_carries_the_request_meta` covers the write branch:

- Setup: `MCP_READ_ONLY` off, an OAuth token with `r:change:part`, and `request_meta` with host `mcp.example` under `ALLOWED_HOSTS=["mcp.example"]`.
- It calls `proxy._call_view_sync(PartDetail, "PATCH", …)` with the factory wrapped.
- Asserts: the part is changed, `factory.patch` was called once with `format="json"`, and the four meta keys were passed as given.

## 7. Finding 5: AGENTS.md

- The `core.py` entry now lists the UI methods, `UI_ROUTES_SUPPORTED`, `_is_staff_admin`, `_ui_source` and `_log_once`.
- New file-map entries: `static/` (generated, git-ignored), `frontend/`, `auth/ratelimit.py`, the `test_ui.py` description and `dev/ui_smoke.py`.
- Updated file-map entries: `models.py` (the lease, `0003`), `clients.py`, `token.py`, `policy.py` (`is_staff_admin`), `membership.py` (the lease, replacing "serialised by a row lock") and `signals.py` (`ensure_rulesets_for(group_ids)`).
- `ui_smoke.py` is added to the testing commands.
- The CI paragraph describes the frontend and wheel steps.
- New "Frontend" section:
  - the build pipeline and `npm ci && npm run build`;
  - `static/` is generated and never edited by hand;
  - host externals;
  - pages need 1.6+;
  - the `lib/` gitignore lesson, with `git check-ignore -v`.
- The ruff note now says CI pins 0.16.0 (`RUFF_VERSION`).

## 8. Finding 4 / readiness item 4: upstream patch 4

**Worktree commit amended (`808c179b0`):**

- `InvenTree/api_version.py`: v557 with a text entry. The PR URL is the placeholder `pull/XXXXX`, to fill in when the PR is opened.
- `InvenTree/tests.py::OAuth2ScopeMapTest`:
  - shared helper `token_passes()`;
  - new `test_content_type_permission_accepts_any_role_scope`;
  - new `test_data_importer_permission_accepts_any_role_scope`;
  - the two existing tests now use the helper.
- The commit message names the three callers of `map_scope(roles=...)` and the version bump.

**`docs/upstream/README.md` §4 corrected:**

- `map_scope(roles=...)` has three callers: `InvenTreeRoleScopeMixin`, `DataImporterPermission` and `ContentTypePermission`.
- Endpoints whose requirement loosens:
  - the importer session, accept_rows, column-mapping and row endpoints (writes);
  - `GenericMetadataView` and `SimpleGenericMetadataView` at `/api/metadata/…`, and the 34 per-model `.../metadata/` redirects.
- The object checks are user-role based, so the token boundary loosens on these endpoints.
- Also changed: the tests list, the API-version paragraph (core's schema diff in `qc_checks.yaml` needs the bump), the verification results and the PR checklist.

**Re-export and checks:**

- The old 0004 was removed, then `git format-patch master..upstream-patches -o docs/upstream/` was run. Only 0004 changed.
- `git apply --check` against `/home/alex.lucaci/Projects/inventree/InvenTree` (master, untouched): `OK` for each of 0001 to 0004.
- Applied in sequence into a scratch index: `sequential tree: db4ed0eed9…` equals `branch tree: db4ed0eed9…`.
- `uvx ruff@0.16.0 check` on `permissions.py`, `tests.py` and `api_version.py`: `All checks passed!`. The `format --diff` hunks that remain are in code the patch does not touch.

**Core tests in the dev container (1.5.7).** `map_scope`, both permission classes and `_roles` are identical to master (same md5 for `map_scope`).

- The four tests were run through `manage.py shell` with the patched `map_scope` exec'd into `InvenTree.permissions`. Patched: `Ran 4 tests … MODE patched OK`. Unpatched: all four FAIL.
- Not run: core's `invoke dev.test` for `InvenTree`, `importer` and `users`, and the schema export diff. Both need a master development environment, which does not exist here.

## 9. Repository integrity: wheel

In a scratch copy (tracked and untracked non-ignored files plus the built `inventree_mcp/static/`), `python -m build` (build 1.6.0) gave:

> `Successfully built inventree_mcp-0.3.0.tar.gz and inventree_mcp-0.3.0-py3-none-any.whl`

The wheel holds 104 files:

- `templates/inventree_mcp/consent.html` and `base.html`: True; 4 template files in total.
- `static/Groups.js`, `Connections.js`, `Panel.js`: True; 17 static files in total.
- `migrations/0003_…` and `auth/ratelimit.py`: True.

The sdist contains 19 static files and 5 template files.

## Verification (quoted)

- `uvx ruff@0.16.0 check --preview inventree_mcp`: `All checks passed!`
- `uvx ruff@0.16.0 format --preview --check inventree_mcp`: `53 files already formatted`
- `uvx ruff@0.16.0 check --preview --isolated inventree_mcp/tests`: `All checks passed!`
- `dev/test.sh inventree_mcp`: `Ran 343 tests in 334.673s` / `OK (skipped=3)`
- `dev/test.sh inventree_mcp.test_mcp`: `Ran 132 tests in 31.632s` / `OK`
- After `dev/compose.sh restart inventree-server inventree-worker`, `migrate` printed `Applying inventree_mcp.0003_mcpuserstate_recheck_lease... OK`. Then:
  - `python3 dev/membership_e2e.py --skip-outage`: `OK: membership re-check end to end`
  - `python3 dev/oauth_smoke.py --sso e2e-member`: `OK: OAuth façade flow completed (SSO as e2e-member)`
  - `python3 dev/permissions_e2e.py`: `OK: MCP permission set end to end`
- Dev DB after the e2e runs: `recheck_lease_until` is NULL for every `MCPUserState` row, so all leases were released.

## README deltas (not applied; README.md belongs to another agent)

1. **Prerequisites, proxy bullet (around line 224).** Change "the registration rate limit (`REGISTER_RATE_LIMIT_PER_MINUTE`) is per client IP" to "the registration and token rate limits (`REGISTER_RATE_LIMIT_PER_MINUTE`, `TOKEN_RATE_LIMIT_PER_MINUTE`) are per client IP".
2. **Plugin settings table, after the `REGISTER_RATE_LIMIT_PER_MINUTE` row.** Add:

   `| TOKEN_RATE_LIMIT_PER_MINUTE | 60 | Authorization code exchanges per minute and client IP at the OAuth token endpoint (0 disables); refresh grants are not limited; 429 with Retry-After. |`
3. **Membership re-checks, around lines 425-426.** Replace "A re-check holds a database row lock for the user while it talks to the provider; replacing that with a short lease is planned hardening." with:

   > A re-check holds no database lock while it talks to the provider. It takes a 60-second lease on the user's `MCPUserState` row (`recheck_lease_until`), stores the rotated upstream refresh token the moment it arrives, and applies the outcome in a second short transaction. A second re-check of the same user while the lease is held returns at once without calling the provider. A lease left by a killed worker expires after 60 seconds. Logins never wait for a re-check.
4. **Installation and upgrade note (lines 216-219).** "run `invoke migrate` after installing or upgrading" now matters for this release: migration `0003` adds `MCPUserState.recheck_lease_until`.
5. **GUI section, line 449.** Change "**MCP Access** (… staff only)" to "staff with the *admin* role's change permission (the condition of the plugin's staff API; staff without it see neither the page, its navigation entry nor the *MCP Roles* group panel)". Apply the same wording wherever the panel is described as "staff".
6. **API table, rows `api/effective/` and `api/status/`.** Add after `scopes`: "(empty whenever `may_connect` is false, e.g. for a blocked user)".
7. **Troubleshooting, line 484.** Change "`429` on registration | `REGISTER_RATE_LIMIT_PER_MINUTE` per client IP …" to "`429` on registration or the code exchange | `REGISTER_RATE_LIMIT_PER_MINUTE` / `TOKEN_RATE_LIMIT_PER_MINUTE` per client IP; check that the proxy replaces `X-Forwarded-For`".
8. **Troubleshooting, line 485.** "No *MCP* / *MCP Access* pages": add "or (*MCP Access* only) the user lacks the admin role's change permission".
9. **Logs (optional).** The "no plugin routes" INFO and the "bundle is missing" WARNING appear once per process, not per page load.

Deltas for files owned by other agents, for their attention:

- `docs/OPERATIONS.md` lines 73 and 99: add the token rate limit next to the registration one.
- `frontend/README.md`, entry-points table: "MCP Access" is "(staff admins: staff + admin.change)", not "(staff)".
- `docs/plans/00-overview.md` settings table: add `TOKEN_RATE_LIMIT_PER_MINUTE`.

## Deferred

- **WP2 F12 item 3, the two-thread unit test.** Not added, because it needs a `TransactionTestCase` that flushes the shared keepdb test database (see item 1). The live two-connection probe above covers the behaviour.
- **Core's `invoke dev.test` (InvenTree, importer, users) and the schema export diff for patch 4.** No master development environment is available. Both are listed in the README checklist as must-run before proposing.
- **Patch 4's PR number in `api_version.py`.** Placeholder `XXXXX` until the PR exists. Renumber if master moves past v556.
- **A live 429 probe of the token endpoint.** Skipped on purpose. Behind Caddy every local client shares one counter, so it would have blocked the e2e agent's code exchanges for up to a minute. The unit test covers it.
