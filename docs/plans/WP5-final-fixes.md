# WP5 final fixes (findings of `WP5-review.md`)

Branch `feature/oauth-facade`, on top of `0503868` (docs: final WP5 review). Nothing is staged or committed.

Findings 1 to 8 and 10 are fixed as scoped below. Finding 7 is fixed only for its second bullet (the re-block). Finding 9 was out of scope.

## Changes per finding

### 1. A refresh during another process's lease now follows the grace and re-login windows

All changes are in `inventree_mcp/auth/membership.py`.

- **New private sentinel `_LEASE_HELD`.** `_take_lease()` now returns it, instead of `Outcome.NOT_DUE`, when another process holds a live lease.
- **New `_recheck(user, *, force)`.** It holds the old body of `recheck()` and can return `_LEASE_HELD`.
  - The public `recheck(user, *, force=False)` keeps its signature and turns `_LEASE_HELD` into `Outcome.NOT_DUE`.
  - The `Outcome` enum is unchanged, and so are the sweep and the `recheck_user` task.
- **`refresh_grant_allowed()` now calls `_recheck()`.** On `_LEASE_HELD` it reads the state and decides without waiting. The lease is never touched.
  - If `last_recheck_error` is one of `NO_TOKEN_CODES`: the refresh is allowed only while `within_relogin(user, state)`.
  - Otherwise: it is allowed only while `within_grace(state)`.
  - Outside the window: `(False, "invalid_grant")`, with an INFO log line.
- **Test `LeaseTest.test_refresh_during_a_live_lease_follows_the_windows`.** Five subtests:
  - grace allowed (2 h);
  - grace refused (5 h);
  - grace refused (5 h, `timeout`);
  - re-login allowed (`no_token`, login 1 h ago);
  - re-login refused (`no_token`, login 30 h ago).

  Each subtest also asserts that `recheck()` still returns `NOT_DUE`, that no outbound call was made, that the lease value and `last_recheck_error` are unchanged, and that the user is not blocked.
- **The existing `test_live_lease_means_in_progress_without_a_call`** (2 h, within grace, so `(True, "")`) is unchanged and still passes.
- **Decision recorded.** One bullet, "The re-check lease (WP5 review findings 1, 2)", was added under "Decisions recorded after reviews" in `docs/plans/00-overview.md`.

### 2. The lease is checked again before the redeem, every outbound call has a total deadline, and the lease is 120 s

All code changes are in `inventree_mcp/auth/membership.py`.

- **`RECHECK_LEASE_SECONDS`** goes from 60 to 120.
- **New `_assert_lease(context)`.**
  - `_redeem_and_verify()` calls it right before `_redeem()`.
  - It re-reads `MCPUserState.recheck_lease_until` and compares it with the value this process set.
  - If the lease was lost, it raises `_Unreachable("lease_lost")`. Nothing is then redeemed, stored or revoked, and the run ends as `Outcome.UNREACHABLE` with `last_recheck_error = "lease_lost"`.
  - `lease_lost` does not start the provider back-off.
- **How the lease reaches `_assert_lease()`.** `_recheck_under_lease(user, lease)` puts `user_id` and `lease` into the existing `context` dict. `_check_upstream()` passes the dict on to `_redeem_and_verify(..., context)`. All of these are private functions; no binding signature changed.
- **`_http()` deadline.**
  - New `HTTP_DEADLINE_SECONDS = 15`. It is a wall-clock limit per call, body included.
  - The request now runs with `stream=True` and reads the body with `iter_content`. Once the deadline has passed, the call raises `_Unreachable("timeout")`.
  - The response is closed, and `_content` is set as requests does without streaming.
  - The `timeout=HTTP_TIMEOUT_SECONDS` (5 s per socket operation) stays.
  - `_http()` makes no retries, so 15 s per call is the "total" bound.
  - Why this is enough: the only call before the redeem is discovery (≤ 15 s), so at least about 105 s of the lease remain when the redeem starts. The redeem (≤ 15 s) and the store finish well inside the lease.
- **Tests in `inventree_mcp/tests/test_membership.py`.**
  - `LeaseTest.test_lease_lost_before_the_redeem_redeems_nothing`: the lease is taken over during discovery. Asserted: no token call, `redeemed == []`, `UNREACHABLE`, `last_recheck_error == "lease_lost"`, the new holder's lease kept, no block, the upstream token unchanged, and no back-off marker.
  - `LeaseTest.test_outbound_call_has_a_total_deadline`: with `HTTP_DEADLINE_SECONDS = -1`, the result is `UNREACHABLE` / `timeout` and no token call is made.
  - The fake IdP's `_response()` now returns an unread response whose body is in `raw` (`io.BytesIO`), as requests returns it. This makes the streamed read work for both `_http()` and jwtkit.
  - All existing `LeaseTest` cases pass with the 120 s lease.
- **Docs.**
  - `README.md` and `AGENTS.md` (membership entry): 120 s lease, 15 s deadline, and the lease check before the redeem.
  - `docs/OPERATIONS.md`: `lease_lost` added to the unreachable codes.

### 3 and 4. CI

`.github/workflows/ci.yaml`:

- `include-hidden-files: true` on the `Upload frontend bundles` step, with a comment about `.vite/manifest.json`;
- the style step now runs `ruff check --isolated dev/*.py dev/authentik/*.py dev/e2e/*.py`.

### 5. `dev/up.sh`

- `server_was_running` is now recorded before step 1b, the frontend build. Its old assignment in step 3 was removed.
- A successful build sets `bundles_built=1`.
- After step 4, `need_restart=1` is set when the server was running and the bundles were rebuilt. The restart re-collects the static files. The restart log message now also mentions the bundles.
- The `npm ci` check now compares `package-lock.json` with `node_modules/.package-lock.json`. Before, it used the `node_modules` directory mtime.
- `bash -n dev/up.sh`: OK. `up.sh` was not run.

### 6. `dev/ui_smoke.py`

- **Token revocation in `finally`.** If `token_id` is still unknown because the lookup failed, `finally` retries `find_token_id()`. If that also fails, it prints a WARNING that names the token, instead of skipping silently.
- **`assert` replaced** by an explicit `raise SystemExit("FAILED: the toggled flag was not persisted")`.
- **New `Api.interface_integration_on()`.** It reads `GET /api/settings/global/ENABLE_PLUGINS_INTERFACE/`. When the pages are not offered, the script now reports one of two cases:
  - the setting is off: "FAILED: … interface integration is off (global setting ENABLE_PLUGINS_INTERFACE) …", exit 1;
  - the setting is on: "SKIPPED … interface integration is on, but this InvenTree has no plugin routes (needs 1.6 or later …)", exit 3.
- The docstring was updated to match.
- **Formatting.** `ruff format --isolated` would reformat this file (88 columns). The lines involved were already wider than 88 before this change, and CI does not format-check `dev/`, so I left the formatting as it was.

### 7 (second bullet). `Restore._block` in `dev/e2e/run.py`

- `Restore.__init__` now also snapshots the block reason (`self.block_reason`).
- `_block()`:
  - still unblocks a user who was blocked by the run;
  - now also re-blocks a user who was blocked before the run and is unblocked afterwards. It calls `membership.block_user(user, <snapshotted reason>)` through `membership_e2e.shell`, so the reason is kept.

### 8. `frontend/src/Groups.tsx`: a queued revocation leaves the Members table pending

I chose the simpler option.

- In `GroupPane`, a queued revocation (`r.queued`) sets `revocationPending` and does not reload the Members table.
- A note is shown above the table: "Revocation pending: it runs in the background, so the connection counts below may still include revoked tokens. Reload the page later to see the result."
- A synchronous revocation reloads the table as before.
- The state resets per group, because `GroupPane` is keyed by the selected group.
- `frontend/src/pages.test.tsx`: the queued-revocation test now also expects the "Revocation pending" note.
- The frontend was rebuilt.

### 10. Docs

- **`docs/plans/00-overview.md`:**
  - `recheck_lease_until = DateTimeField(null=True, blank=True)` added to the binding `MCPUserState` listing, marked WP5, migration 0003;
  - the decision bullet from finding 1.
- **`README.md` lease paragraph.** Now says 120 s. Added sentences on the lease check before the redeem and the 15 s deadline, plus this one: a refresh grant of an overdue user that arrives during another re-check's lease does not wait; it proceeds only within the grace window (or the re-login window when there is no upstream token), otherwise `invalid_grant`.

## Verification

Ruff 0.16.0:

```
uvx ruff@0.16.0 check --preview inventree_mcp                         All checks passed!
uvx ruff@0.16.0 format --preview --check inventree_mcp                53 files already formatted
uvx ruff@0.16.0 check --preview --isolated inventree_mcp/tests        All checks passed!
uvx ruff@0.16.0 check --isolated dev/*.py dev/authentik/*.py dev/e2e/*.py   All checks passed!
uvx ruff@0.16.0 format --isolated --check dev/e2e/run.py              1 file already formatted
bash -n dev/up.sh                                                     OK
```

Frontend (`npm run lint && npm test && npm run build && npm run check:bundles`, exit 0):

```
Checked 17 files in 4ms. No fixes applied.
 Test Files  4 passed (4)
      Tests  17 passed (17)
✓ built in 914ms
ok Groups-MLFbdbCC.js / Groups.js / Connections-CaGfFcB6.js / Connections.js / Panel-DQklP5Jf.js / Panel.js
```

Unit tests:

```
dev/test.sh inventree_mcp.tests.test_membership   Ran 50 tests in 105.153s  OK
dev/test.sh inventree_mcp.tests.test_auth_token   Ran 24 tests in 21.647s   OK
dev/test.sh inventree_mcp                         Ran 346 tests in 334.680s OK (skipped=3)
```

The full suite was 343 tests before; the 3 new ones are `LeaseTest` cases.

After `dev/compose.sh restart inventree-server inventree-worker` (all three containers healthy), `python3 dev/e2e/run.py --skip-outage` gave exit 0:

```
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
```

- Every restore step printed `[restore] …`, including `[restore] block state of e2e-member`. No `FAIL restore` line appeared.
- The WARN is the known core request-log classification from the review, not a plugin leak.

Extra check, not required: `python3 dev/ui_smoke.py` against the 1.5.7 stack exited 3 with this output:

```
SKIPPED browser steps: interface integration is on, but this InvenTree has no plugin routes (needs 1.6 or later, e.g. INVENTREE_TAG=latest)
deleted scratch group pk=13
revoked the script's API token
```

## Remaining notes

- The 15 s deadline covers the body and each socket operation. It does not cover a DNS resolution stall, which requests cannot bound.
- The JWKS fetch inside jwtkit does not go through `_http()`. It runs after the rotated token is stored, so it does not affect the double-redeem risk.
- Not done, because out of scope: finding 7's other bullets (masking `state=`, the e2e-member check stamp, the `mcp_call` overlap note, leftover DCR clients), finding 9 (`Access-Control-Expose-Headers: Retry-After`), and the optional install from the wheel in CI.
