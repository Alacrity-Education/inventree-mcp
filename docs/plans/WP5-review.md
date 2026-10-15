# WP5 hardening and end-to-end suite: final review

Reviewed: `git diff 3f33d59..HEAD -- inventree_mcp frontend dev .github docs/upstream` on `feature/oauth-facade`. The range holds `a12c4db`, `7c1e52d`, `e79d088`, `9db8d6a` and `3a86fc9`; `3a86fc9` is the docs agent's commit and adds the Caddy `Location` filter.

Inputs read: `docs/plans/WP5-backend-report.md`, `WP5-frontend-report.md`, `WP5-e2e-report.md`, `WP3-WP4-review.md`, `WP2-review.md` F2 and `00-overview.md`. In `00-overview.md`, "Decisions recorded after reviews" is at :181. The `Outcome` enum and the `recheck()` signature are at :131 and :140 and are unchanged. The overview has no separate "Outcome note" section.

References: DOT 3.4.0 (vendor), InvenTree master `9b8fdeb06`, and the worktree branch `upstream-patches`.

This was a read-only review. The only file written is this one.

## Verdict

**Ship-ready for a fork.**

- No blocker and no major finding.
- The lease, the shared rate limiter, the UI gating and the frontend hardening do what the reports say.
- Unit suite, frontend checks, upstream patches and the e2e run are green (quoted below).
- Findings 1 to 3 should be fixed before a wider production rollout. All are small.

## Findings

### 1. Minor (security): a refresh during another process's lease skips the grace and re-login windows

**Where.**

- `inventree_mcp/auth/membership.py:398-401`: `_take_lease` returns `NOT_DUE` when a live lease exists.
- `inventree_mcp/auth/membership.py:325`: `refresh_grant_allowed` treats `NOT_DUE` as allowed.

**Effect.**

- Suppose a user is overdue and their re-check is in flight elsewhere (the sweep, or a background `recheck_user`). That user's refresh grant is allowed without waiting.
- This is the documented behaviour change.
  - If the in-flight re-check blocks the user or revokes their MCP tokens, `/mcp` answers 401 or 403 on the next request (`policy.connect_scopes` checks `is_blocked` on every request). So that case is covered.
  - If the in-flight re-check ends UNREACHABLE past `MEMBERSHIP_GRACE_HOURS`, or NO_UPSTREAM_TOKEN past `RELOGIN_INTERVAL_HOURS`, the refresh still receives new tokens.
- The transport does not apply those windows to OAuth bearers, only to long-lived credentials (`long_lived_credential_allowed`).
- Exposure: one extra access-token lifetime (1 h). The next refresh re-checks and is refused.
- It needs a concurrent re-check, so it is rare. It is also a regression against the WP2 lock design, where the waiting refresh repeated the decision.

**Fix.** Keep "never wait", but apply the windows when a lease is found.

- In `refresh_grant_allowed`, after `NOT_DUE`, read `_state(user)`.
- If `recheck_lease_until` is in the future and `membership_checked_at` is older than the re-check interval, return `(True, "")` only when `within_grace(state)` (or `within_relogin` when `last_recheck_error in NO_TOKEN_CODES`). Otherwise return `(False, "invalid_grant")`.
- Add a `LeaseTest` case: a lease held and the last check 5 h old gives a refused refresh.
- This needs no change to the binding `Outcome` enum.
- Also record the lease semantics under "Decisions recorded after reviews" in `00-overview.md`.

### 2. Minor: an expired lease can be taken over while its holder is still calling out

**Where.**

- `membership.py:75` (`RECHECK_LEASE_SECONDS = 60`).
- `membership.py:398` (takeover of an expired lease).
- `membership.py:640` (`timeout=HTTP_TIMEOUT_SECONDS`). This is a per-socket-operation timeout, not a total deadline. The JWKS fetch inside jwtkit is an additional call.

**Effect.**

- The nominal worst case is about 4 to 5 calls × (connect 5 s + read 5 s), which is under 60 s.
- A slow-drip response or DNS stall can exceed 60 s. Then a second re-check takes the lease and redeems the same upstream refresh token.
- With Authentik's rotation, the loser gets `invalid_grant`, which blocks the user. This is the failure F2 set out to remove.
- The compare-and-clear release at `membership.py:412-414` is correct and does not cover this case.

**Fix.** Use one of:

- re-assert ownership immediately before `_redeem` (`MCPUserState.objects.filter(user_id=..., recheck_lease_until=lease).update(recheck_lease_until=now+60s)`, and if 0 rows are updated raise `_Unreachable("lease_lost")`);
- or give `_http` a total deadline derived from the lease.

Also raise `RECHECK_LEASE_SECONDS` to at least 2× the worst case.

### 3. Minor (CI): the bundle artifact loses `.vite/manifest.json`

**Where.** `.github/workflows/ci.yaml:58-63`.

**Effect.**

- `actions/upload-artifact@v4` (since 4.4.0) excludes hidden files by default. So `inventree_mcp/static/.vite/manifest.json` never reaches the `integration-tests` job.
- The job will not go red: `test_ui.test_bundles_are_built` checks only the plain `Groups.js`, `Connections.js` and `Panel.js`. But the integration environment differs from the wheel.
- The hashed-name resolution of `plugin_static_file` is not exercised in CI.
- The integration job also installs from the checkout (`pip install -e`), not from the wheel built in the `ci` job.

**Fix.** Add `include-hidden-files: true` to the upload step. Optionally install the built wheel in the integration job.

### 4. Nit (CI): the e2e runner is not linted in CI

**Where.** `ci.yaml:49` lints `dev/*.py dev/authentik/*.py`, which excludes `dev/e2e/run.py`. It passes locally (`uvx ruff@0.16.0 check --isolated dev/e2e`: "All checks passed!"; `format --check`: "1 file already formatted").

**Fix.** Add `dev/e2e/*.py`.

### 5. Minor (dev): `dev/up.sh` rebuilds the bundles but does not republish them to a running server

**Where.** `dev/up.sh:72-89` (the build) versus `:104-161` (`need_restart` is only set for config or settings changes).

**Effect.**

- The server collects `inventree_mcp/static/` into `dev/data/static/plugins/inventree-mcp/` only when it starts.
- After a rebuild on a running stack, the manifest in the package points to new hashed names that are not collected yet, so pages 404 until the next restart. This only matters on 1.6+.
- The `npm ci` heuristic compares the lock file with the `node_modules` directory mtime (`:79`). The mtime does not change for in-place package updates.

**Fix.**

- Set `need_restart=1` when a build ran and `server_was_running` (the variable is assigned at `:105`, after step 1b, so record it earlier), or run the static collection in the container.
- Compare against `node_modules/.package-lock.json`.

Side effects otherwise:

- npm missing or a failed build: a warning that names the failing test, and the stack continues.
- Idempotent: a failed build leaves the bundle stale or missing, so the next run retries.

### 6. Nit (dev): `dev/ui_smoke.py` cleanup and skip edge cases

**Where and effect.**

- `ui_smoke.py:144-147`: the admin token is created in `Api.__init__`. Its id is resolved by `find_token_id()` inside the `try`. If that listing fails, `token_id` stays `None` and the token is not revoked.
- `:122`: the persistence check is an `assert`, which `python -O` removes.
- `:153-155`: `pages_offered()` is also false on a 1.6 host with `ENABLE_PLUGINS_INTERFACE` off. The script then reports "needs 1.6 or later" and exits 3, which hides the misconfiguration.
- The scratch-group design itself is correct: no members, so no revocation, and it is deleted in `finally`.

**Fix.**

- Return the token id from the creating call, or list tokens before anything else.
- Replace the `assert` with an explicit failure.
- Distinguish "no route support" (version) from "interface integration off" (the `ENABLE_PLUGINS_INTERFACE` setting).

### 7. Nit (e2e): small output and restore gaps

- **Values printed around leaks.** `dev/e2e/run.py:912-919` prints a 260-character window of every leaking log line. Secrets are masked. The OAuth `state`, request ids and client IP are printed in clear. None of these is a credential, but the docstring says "Tokens, codes and secrets are never printed". Mask `state=` as well, or say so.
- **Restore gaps.**
  - `Restore._block` (`:1110`) only unblocks. A user who was blocked before the run stays unblocked after scenario 3's SSO login.
  - `membership_checked_at` of e2e-member is left aged or restamped.
- **Undeclared overlap.** `mcp_call` (`:165`) overlaps `oauth_smoke.mcp_tools_list` (`dev/oauth_smoke.py:92`) and `permissions_e2e.call_tool` (`dev/permissions_e2e.py:121`). It is short and generalises the Authorization header. Acceptable, but not listed in the report's "Reused" column.
- **Leftover clients.** Unrelated to `run.py`: the dev DB holds 40 leftover DCR applications from standalone runs of `membership_e2e.py` (21) and `oauth_smoke.py` (19). Those scripts do not clean up. `run.py` left none (no application created during my run remained).

### 8. Nit (frontend): the Members table reloads before a queued revocation has run

**Where.** `frontend/src/Groups.tsx:179`. After "Revocation queued for N members", the immediate reload shows the pre-revocation connection counts.

**Fix.** Skip the reload, or add "(pending)" when `queued`.

### 9. Nit: a 429 from the token endpoint hides `Retry-After` from browsers

**Where.** `auth/ratelimit.py:51-56` together with `auth/cors.py` (`TrustedOriginCORSMixin.dispatch`). No `Access-Control-Expose-Headers: Retry-After` is sent.

**Effect.** claude.ai exchanges the code server-side, so the effect is limited to browser-based clients.

**Fix.** Expose the header on 429 responses.

### 10. Nit (docs): binding model and operator wording

- `docs/plans/00-overview.md:167-173` (binding `MCPUserState` fields) lacks `recheck_lease_until`.
- `README.md:427-431` (docs agent's file) describes the lease but not its consequence for refresh grants (they proceed without waiting). Add one sentence; see finding 1.

## Answers to the review questions

### 1. The lease (`membership.py`, `models.py:58-66`, `migrations/0003_mcpuserstate_recheck_lease.py`)

**No transaction across outbound calls.**

- Step 1 is `transaction.atomic()` at `membership.py:391-405`, committed before `recheck()` continues (`:380`).
- Step 2 is `_check_upstream` → `_redeem_and_verify` (`:550-607`), called at `:430` outside any atomic block.
- Step 3 opens its transaction only after the network part (`:453`).
- Callers open no transaction:
  - InvenTree sets no `ATOMIC_REQUESTS` (grep of master `settings.py`);
  - DOT 3.4's `TokenView` has no atomic around `post`, and the plugin's hook runs before DOT;
  - allauth 65.18's login flows contain no `transaction.atomic`;
  - django-q tasks run in autocommit.
- `LeaseTest.test_no_transaction_is_open_during_the_outbound_calls` asserts this with `connection.atomic_blocks` depth inside every mocked call.

**Rotated token persisted immediately.**

- `_store_tokens` (`:601`, defined at `:743`) runs in autocommit, before verification.
- `test_rotated_token_survives_a_killed_process` raises a `BaseException` right after the store and finds the rotated token. Per the backend report, it fails against the old code.

**Lease taken and released.**

- Taken under `select_for_update` (`:392`, `_locked_state` at `:202-207`) and committed.
- Released in `finally` (`:385-386`), which also runs for `BaseException`.
- The release is a compare-and-clear on the lease value (`:412-414`), so a holder whose lease was taken over does not clear the new one.
- A `DatabaseError` during the release is logged, and the lease expires on its own.
- A SIGKILL skips `finally`, and the lease expires after 60 s. That is the intended design.

**Expired lease.** Taken over (`:398`, only `> now` counts as live). Covered by `test_expired_lease_is_taken_over`. The remaining edge case is finding 2.

**Concurrent refresh during a lease.**

- It returns `NOT_DUE` without a call (`test_live_lease_means_in_progress_without_a_call`).
- Block and NO_MCP_PERMISSION outcomes are still enforced by the transport on the next request.
- The grace and re-login windows are not enforced in that window: finding 1.

**Sweep budget and back-off.**

- Unchanged: `tasks.py` `membership_sweep` (budget loop, continuation) and `_check_upstream` back-off (`membership.py:575-584`).
- A leased user counts as `not_due` (docstring at `tasks.py:131-132`).

**Outcome table: unchanged.**

| Outcome | What happens | Where |
|---|---|---|
| NO_UPSTREAM_TOKEN | error recorded, warning at most hourly, queue marker | `:431-441` |
| `invalid_grant` | block | `:442-449` |
| unreachable or unexpected | `_inconclusive` | `:450-451`, `:492-505` |
| group sync failure | inconclusive, in a savepoint | `:455-459` |
| IDENTITY_REFUSED | block | `:462-464` |
| NO_MCP_PERMISSION | stamp, then revoke | `:467-480` |
| MEMBER | stamp, then log | `:467-469`, `:484-489` |

The only nuance: an `_Unreachable` raised by the group sync no longer starts the provider back-off. The sync is outside the wrapped call now, and the effect is negligible.

**Blocking and revoking in a short transaction.**

- `block_user` uses its own `atomic` (`:854-861`), either standalone (from step 2) or nested in step 3.
- The revocation in step 3 is inside the step-3 transaction.

**Login receiver deadlock.**

- `signals.on_user_logged_in` (`signals.py:74-89`) locks only the `MCPUserState` row, in a short transaction, after allauth's autocommit writes.
- Step 3 takes `MCPUserState`, then `SocialAccount`, groups and DOT token rows.
- DOT's refresh transaction never touches `MCPUserState`.
- With no cyclic lock order, no deadlock is possible.

**Migration.**

- `0003` is a single nullable `AddField` with no default and no data step.
- The help text matches the model.
- It was applied on the dev DB (backend report).
- After my e2e run, `recheck_lease_until` was NULL on every row ("0 0" from a read-only shell query: no live leases, no blocks).

### 2. Rate limiter (`auth/ratelimit.py`)

- **Window.** A fixed window aligned to the clock: `remaining = ceil(60 - t % 60)`, range 1 to 60 (`:39`). `cache.add` with that TTL, then `incr` (`:41-46`). With django-redis (InvenTree's backend, `InvenTree/cache.py:117`), `incr` is an atomic Lua `EXISTS`+`INCRBY`, so a key cannot lose its TTL at the boundary.
- **Key.** `inventree_mcp:<scope>:<REMOTE_ADDR>` (`:25`), unchanged for `dcr`; new `token` scope.
- **429.** Body `{"error":"too_many_requests"}`, `Retry-After`, `no-store` (`:51-56`).
- **Grants.** The limit applies only to `grant_type=authorization_code`, before the membership hook (`token.py:80-87`). Refresh grants are unlimited.
- **Setting.** `TOKEN_RATE_LIMIT_PER_MINUTE`: int, `MinValueValidator(0)`, default 60, 0 disables (`core.py:142-147`).
- **Spoofing.** `REMOTE_ADDR` is set by core's `x_forwarded_for` middleware from the first `X-Forwarded-For` entry (master `settings.py:374`). Spoofing is therefore possible exactly as README and OPERATIONS §3 document (the proxy must replace the header). There is no other bypass.
- **No duplicate limiter left.** `clients._rate_limited` was removed, and `grep` finds `incr`, the window arithmetic and the 429 body only in `ratelimit.py`.
- **Test.** `test_code_exchange_rate_limit` uses a fixed clock; `Retry-After: 20` is checked; the other IP, refresh and 0 cases are covered.
- **Residual: no Redis.** Without Redis (LocMemCache) each gunicorn worker counts separately.

### 3. UI gating, log-once, `connect_scopes`, `ensure_rulesets_for`, write-path test

- **Staff-admin gating.**
  - `policy.is_staff_admin` (`policy.py:46-61`) is the single predicate.
  - `api.IsStaffAdmin` delegates to it, and the duplicate body and import are gone.
  - `core._is_staff_admin` (`core.py:319-323`) gates the route (`:329`), the navigation item (`:355`) and the panel (`:374`).
  - Tests: `test_staff_without_admin_role_sees_no_staff_entries` and `test_staff_without_admin_role_session`.
- **Log-once.**
  - `_log_once` (`core.py:30-40`) keys on message and args, so there is one WARNING per missing bundle and one INFO for "no routes" per process. The key set is bounded (four keys).
  - The unexpected-error path `core.py:315` ("cannot resolve") still logs every time. That is acceptable: it is not an expected state.
- **`connect_scopes`.**
  - Used in `effective_document` (`api.py:415`) and `StatusView` (`api.py:537`).
  - For a blocked user, `scopes` and the `effective` grid are now empty. Before, `narrow()` did not check the block.
  - Nothing the pages need is lost:
    - `Connections.tsx` never reads `status.scopes`;
    - the status card shows `may_connect: no`, `reason`, and the `membership.blocked` badge with the reason (`Connections.tsx:72-82`);
    - the effective document still returns `roles` and `mcp_roles` (asserted in `test_blocked_user_has_no_scopes`).
  - An admin can no longer read "what a blocked member would regain" directly. That follows from `roles ∩ mcp_roles`, and the new value matches the MCP endpoint.
- **`ensure_rulesets_for(group_ids)`.** It takes pks (`signals.py:118-128`). All callers pass pks or call `ensure_rulesets(group)`, including `dev/configure_inventree.py:84`.
- **Write-path test.** `test_policy.test_write_call_carries_the_request_meta` exercises `_call_view_sync` PATCH with `MCP_READ_ONLY` off. It asserts the DB change, `format="json"` and the four meta keys on `factory.patch`.

### 4. Frontend

- **Shadow-flag wording.** `RoleGrid.tsx:18-19` (tooltip) and `Groups.tsx` page text now state the per-table, all-groups rule (WP3-WP4 finding 7).
- **Members refresh.** `Groups.tsx:151-153` bumps the key after a core save (`:161`), an MCP save, Copy or Clear (`onChanged`, wired in `McpRolesEditor.tsx:45,61`) and a revoke (`:179`).
- **Queued revoke.** `Groups.tsx:173-178` (see finding 8 for the reload timing).
- **Connect card.**
  - Rendered only after the status loads, with no origin fallback (`Connections.tsx:292-297`).
  - With OAuth on: the OAuth command and the claude.ai steps.
  - With OAuth off: `--header "Authorization: Token <your-api-token>"` and the explanation (`:205-266`).
- **No secrets rendered.** The only "token" strings are the placeholder and the revoke count.
- **vitest coverage is real.** `pages.test.tsx` renders the real pages with a URL-routed fake `api`, so the assertions are on actual requests and notifications:
  - core PATCH `/api/user/ruleset/11/` versus plugin PATCH `/plugin/inventree-mcp/api/rulesets/21/` with the four flags;
  - both revoke messages;
  - the Connections revoke: nothing deleted before confirming, DELETE, list reloaded;
  - both Connect variants, and that the card is absent before the status arrives.
- **`api.test.ts`.** Covers `roleMapToRows` and both patch helpers.
- **Gaps.** The members key bump is untested, and the reload assertion is `>= 2` GETs (weak but meaningful). Both are declared in the frontend report.

Results (quoted):

```
npm run lint:           Checked 17 files in 4ms. No fixes applied.
npm test:               Test Files  4 passed (4)   Tests  17 passed (17)
npm run build:          ✓ 6380 modules transformed.  ✓ built in 932ms
npm run check:bundles:  ok Groups-CI4zs_YK.js / Groups.js / Connections-CaGfFcB6.js / Connections.js /
                        Panel-DQklP5Jf.js / Panel.js (6 x ok, exports and render checked)
```

The only stderr output was Node's `ExperimentalWarning: localStorage is not available`, which is harmless.

### 5. `dev/up.sh` and `dev/ui_smoke.py`

- **`up.sh`.** See finding 5. The warn-and-continue behaviour, the staleness inputs and idempotency are as the report describes.
- **`ui_smoke.py`.** See finding 6.
  - Side effects are confined to a member-less scratch group and the script's own admin token. Both are removed in `finally` (`:159-171`).
  - Screenshots are written only with `--screenshots`.
  - The checkbox index `nth(3)` matches `RoleGrid` `COLUMNS` (View, Change, Add, Delete).
  - Not run here (not in the permitted list).

### 6. E2E runner (`dev/e2e/run.py`)

**Reuse.**

- Flows are imported from `oauth_smoke`, `membership_e2e`, `permissions_e2e`, `sso_smoke` and `bootstrap`.
- Secrets are harvested by wrapping `sso.Browser.request` once (`:130-157`); nothing is copied.
- Declared overlap: `bridge_attempt` (`:215-249`).
- Undeclared but minor: `mcp_call` (finding 7).

**State restoration.**

- `Restore` snapshots before any change (`:1191`). Settings are written inside the `try`, and the outer `finally` runs `restore()` (`:1198-1200`).
- Each restore step is independent and catches `BaseException` (`:1100`).
- Scenario-level `finally` blocks restore at these points:
  - `:456`: API token;
  - `:559`: Authentik groups and unblock;
  - `:683`, `:685`: token and MCP rules;
  - `:721`: MCP rules;
  - `:830`: CIMD row;
  - `:1008`: `claude mcp remove`.
- Gaps: finding 7.

Verified after my run:

- settings back (`MCP_REQUIRED_GROUPS=''`);
- 0 CIMD rows;
- 0 live `membership-e2e` tokens;
- no application created during the run remains;
- no `inventree-dev` Claude entry;
- no lease and no block.

**Secrets.**

- Token values are printed only as lengths (`access_token=<30 chars>`) and `sha256:` prefixes.
- Helper failure paths print bounded excerpts: `oauth_smoke.py:377` prints `landed[:120]` on an SSO failure. That could include an Authentik callback code on a failure path, which is pre-existing.
- Shell outputs carrying keys are captured, not echoed.

**Coverage against the ten scenarios.** All ten are implemented.

- 1: the core `/.well-known/` redirects SKIP on 1.5.7, automatically, with the proxy rewrites asserted instead.
- 4: skipped in my run (`--skip-outage`). It passed in the e2e report's full run.
- 8: uses a simulated CIMD row, as the spec allows.
- 10: stops at "Needs authentication" and prints the manual steps, as the spec allows.

**WARN classification.**

- **The decision.** Classifying core's SSO-callback request lines as WARN is right for this suite:
  - the lines come from InvenTree core (django-structlog `request_started`/`finished` at INFO, gunicorn's default access log), not the plugin;
  - the Authentik code is single use, short-lived and bound to InvenTree's confidential client secret, so it cannot be redeemed from a log;
  - the classifier (`_core_request_log_callback`, `:865-872`) is narrow: a secret is WARN only if every line holding it is a core callback request line, and any other leak is FAIL.
- **Production mitigation** (OPERATIONS §7):
  - `INVENTREE_LOG_LEVEL=WARNING`;
  - a gunicorn access-log format with `%(U)s` (path without query) via `GUNICORN_CMD_ARGS` (command-line settings override `gunicorn.conf.py`);
  - stripping `code`/`state` from both the request URI and the `Location` response header in the proxy log (now in `dev/Caddyfile`, commit `3a86fc9`; the proxy check passes);
  - the upstream fix is a query-less access-log format in core's `gunicorn.conf.py` and django-structlog's request path.
- **Caveat.** At INFO, django-structlog logs `request_started` before the code is redeemed, so for under a second the code is live in the log. That is still useless without the client secret.

### 7. CI (`.github/workflows/ci.yaml`, unchanged in this range)

- `npm test` and `npm run check:bundles` exist (`frontend/package.json` scripts, `frontend/scripts/check-bundles.mjs`).
- The `[test]` extra exists (`pyproject.toml:30-31`, `coverage`).
- The ruff pin `0.16.0` matches `.pre-commit-config.yaml` `rev: v0.16.0`.
- The wheel check asserts the template and `static/Groups.js`.
- All four style commands pass with ruff 0.16.0 locally:
  - "All checks passed!";
  - "53 files already formatted";
  - "All checks passed!";
  - "All checks passed!" for `dev/*.py dev/authentik/*.py`. The earlier `UP035` is fixed.
- Nothing here would make GitHub red. Findings 3 and 4 are gaps, not failures.

### 8. Upstream patches

- `git apply --check` on InvenTree master `9b8fdeb06`, each patch on its own: `OK` for 0001, 0002, 0003 and 0004.
- `git format-patch master..upstream-patches` (worktree, merge base `9b8fdeb06`) re-exported into the scratchpad. All four bodies are identical to `docs/upstream/*.patch` (compared without the `From <sha>` line and signature).
- Patch 4:
  - `api_version.py` goes 556 → 557 with a v557 text entry (PR `XXXXX` placeholder).
  - README §4 matches: three callers named, the importer and metadata relaxation described, the v557 bump and the renumber instruction, and the four tests listed with the names that are in the patch.

### 9. Whole branch

`git log --oneline c1dcdfa..HEAD` gives 26 commits:

```
3a86fc9 WP5: strip authorization codes from the proxy's Location log, apply the hardening doc deltas
9db8d6a WP5: end-to-end runner, operations guide, restructured README
e79d088 docs: token rate limit setting in the overview, MCP Access gating wording
7c1e52d WP5 (backend): hardening
a12c4db WP5 (frontend, dev): hardening from the WP4 review
3f33d59 docs: WP3 fixes and WP4 review
adf3bb2 dev: sort imports in sso_smoke.py
128aa4f WP4: commit the shared frontend code and fix the dev lint error
b7c814f WP3 review fixes and WP4 backend wiring
42e4524 ci: whole-package tests with the OIDC backend, frontend job, pinned ruff, bundles in the wheel
bd04e4c docs: WP3 review
af49ee3 WP3: MCP permission set per group
c9e623c WP2: apply review fixes
e7dad0e docs: prepared upstream InvenTree patches
6d6be74 docs: WP2 review and decisions recorded in the overview
7081bad WP2: membership re-check against the identity provider
c2f9305 docs: WP4 bundles are build output, not committed
fd2bff3 WP4 (frontend): build pipeline, role grid, MCP access and connections pages
7e4cca2 packaging: ship the OAuth templates in built packages
94bbd50 docs: WP1 verification of the review fixes
465f691 WP1: apply review fixes
cf67f05 WP-dev: apply review fixes
6375ace WP1: discovery and OAuth façade
1bf973b WP-dev: docker development and test environment
abbead2 docs: microplans for WP2 to WP5
099d07b docs: implementation microplans for the OAuth façade and MCP permission set
```

- `git ls-files | grep -E 'static/|node_modules|\.env$|bootstrap.json|egg-info'` gives only `dev/inventree.env`. That file holds disposable dev values (`admin`/`admin`, DB `inventree`), and its header says so; no real secret.
- A grep for long literal secrets in tracked files found none.
- `frontend/src/lib` is tracked (11 files).
- Root-owned files are all untracked and ignored: `inventree_mcp.egg-info/PKG-INFO` and the collected `dev/data/static/plugins/inventree-mcp/*`.
- `git status --short` is empty. The docs agent's Caddyfile change was committed in `3a86fc9` during the review.

## Test and e2e results (quoted)

`dev/test.sh inventree_mcp` (dev stack, InvenTree 1.5.7):

```
Ran 343 tests in 335.287s
OK (skipped=3)
EXIT=0
```

`python3 dev/e2e/run.py --skip-outage` (started 07:01:33 UTC; PASS lines condensed to counts per scenario, every other line verbatim):

```
scenario 1: 18 PASS
SKIP 1 core /.well-known/<name> redirects  (InvenTree 1.5.7 has no plugin /.well-known/ support (inventree/InvenTree#12698, newer than 1.5.x): /.well-known/oauth-authorization-server -> 302 /accounts/login/; the proxy rewrites above stand in)
scenario 2: 7 PASS, scenario 3: 7 PASS
SKIP scenario 4 outage  (--skip-outage)
scenario 5: 12 PASS, scenario 6: 7 PASS, scenario 7: 4 PASS, scenario 8: 5 PASS
PASS 9 inventree-server+inventree-worker: none of the run's tokens and codes (...)  (36 checked)
WARN 9 inventree-server+inventree-worker: 9 Authentik authorization code(s) in InvenTree core's request/access log lines of the SSO callback. [...]
PASS 9 the plugin did log during the run (the grep is not vacuous)  (46 lines)
PASS 9 proxy: none of the run's tokens and codes (...)  (36 checked)
PASS 9 proxy: the Authentik callback lines carry no code (Caddy's log filter strips code and state)  (9 callback lines, 0 with a code)
PASS 10 claude mcp list: the dev server needs authentication  (inventree-dev: http://localhost:8080/plugin/inventree-mcp/mcp/ (HTTP) - ! Needs authentication)

== summary
PASS scenario 1 discovery
PASS scenario 2 member
PASS scenario 3 removed
SKIP scenario 4 outage
PASS scenario 5 non-members
PASS scenario 6 narrowing
PASS scenario 7 reduction
PASS scenario 8 clients
PASS scenario 9 logs
PASS scenario 10 claude-code
WARN 9 inventree-server+inventree-worker: 9 Authentik code(s) in core's request/access log
9 passed, 0 failed, 1 skipped, 1 warning(s)
exit=0
```

Every restore step printed `[restore] …` with no `FAIL restore` line.

Frontend: see §4. Ruff: see §7. Patches: see §8.

## Residual risks for production

- **InvenTree versions.**
  - The MCP endpoint and OAuth need InvenTree ≥ 1.5.0 with django-oauth-toolkit ≥ 3.4 (feature-detected).
  - The GUI pages and navigation need plugin routes (core PR #12900, not in any release yet, so 1.6.0 or the `latest` image) and *Enable interface integration*.
  - The "MCP Roles" group panel additionally needs upstream patch 3.
  - On 1.5.x, administrators manage MCP roles through `api/rulesets/` only, and new groups start with no MCP access.
- **Core bugs found.** All are worked around or documented, none fixed upstream yet:
  - the 1.5.x first-SSO-login 500 (`setup_user_email` assertion; PR #12842);
  - `map_scope` requiring every role's scope (patch 4; plugin workaround `_any_role_alternatives`);
  - the raw `TokenMatchesOASRequirements()` in `check_oauth2_authentication` and the broken `super().has_permission` fallback (`oauth2_bridge.py` docstring);
  - `account_login` dropping `next` (patch 2);
  - `OAUTH2_PROVIDER` not configurable (patch 1);
  - core request logging of the SSO callback query string (below).
- **Service accounts.** With `MEMBERSHIP_ENFORCED` on, API-token-only accounts that never sign in through SSO are suspended on `/mcp/` once a re-check records `no_token` and the last login is older than `RELOGIN_INTERVAL_HOURS`. Turn enforcement off on password-only instances, or sign the account in through SSO once (OPERATIONS §6).
- **1.5.x proxy rewrites.** Without core plugin `/.well-known/` support (#12698), the RFC 8414/9728 path-insertion URLs must be rewritten by the reverse proxy. Clients such as Claude Code fail discovery without them.
- **Instance-wide lifetimes.** While `OAUTH_ENABLED` is on, the plugin's DOT values (access 1 h, refresh 30 d, code 10 min, rotation with reuse protection, 120 s grace) apply to every OAuth2 application on the instance, including core's `/o/`.
- **Core request logging.** gunicorn's default access log and django-structlog at INFO record the SSO callback's `code` and `state`. Set `INVENTREE_LOG_LEVEL=WARNING`, use a query-less gunicorn access-log format, and filter `code`/`state` from the proxy's request URI and `Location` header.
- **Lease semantics.** See findings 1 and 2: a refresh concurrent with an in-flight re-check is not held to the grace and re-login windows, and an over-long re-check can lose its lease.
- **Rate limits without Redis.** Without Redis, the rate limits and the provider back-off are per process. Run with `INVENTREE_CACHE_ENABLED`.
- **Shared egress addresses.** claude.ai's code exchanges and registrations reach the server from Anthropic's egress addresses. Many users connecting at once share one counter (60 code exchanges and 30 registrations per minute by default).

## Recommended next steps

1. Fix finding 1 (refresh during a lease uses the windows) and finding 2 (re-assert the lease before the redeem). Add the two unit tests. Record the lease decision in `00-overview.md` (finding 10).
2. In CI: add `include-hidden-files: true` to the bundle upload. Lint `dev/e2e/*.py`. Optionally run the integration tests against the built wheel.
3. In `dev/up.sh`: republish the bundles on a running stack after a rebuild. In `ui_smoke.py`: the cleanup and skip edge cases (findings 5, 6).
4. Run the full `dev/e2e/run.py` (with the outage scenario) on a fresh `dev/down.sh -v && dev/up.sh` as the WP5 acceptance requires. Then schedule the 1.6 browser e2e described in the frontend report (connections-page revoke followed by `invalid_grant`, and the group panel with patch 3).
5. Before proposing patch 4 upstream: run core's `invoke dev.test` for `InvenTree`, `importer` and `users` and the schema export diff in a master environment, and fill in the PR number.
6. For production go-live, work through OPERATIONS §1, including the logging mitigations (§7) and the proxy `X-Forwarded-For` replacement (§3).
