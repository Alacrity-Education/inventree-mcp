# WP1 verification: review fixes (F1 to F3, F8, F10)

Branch `feature/oauth-facade` at `465f691` (`WP1: apply review fixes`), compared against the reviewed commit `6375ace` (`git diff 6375ace -- inventree_mcp`). This was a read-only check against DOT 3.4.0 (`vendor/django-oauth-toolkit-3.4.0`) and the running dev stack on `http://localhost:8080`. Probe scripts are in the session scratchpad and are not part of the repo. The probes created a few DCR clients and tokens (`wp1-verify`) in the dev database. Nothing else was changed.

## Verdict

**The fixes are confirmed.** The blocker F1 and the majors F2 and F3 are fixed. Live probes and DOT's code paths agree. F8 and F10 are fixed too. The three requested test modules pass.

I found **no path where a refresh token revoked through `/oauth/revoke/` or `RefreshToken.revoke()` mints tokens** in sequential use. Two small gaps remain in F1, and one in F3:

- **R1 (low):** a stale rotated token replayed inside the grace window is refused, but its token family is not revoked.
- **R2 (low):** a revocation that commits between the façade's pre-check and DOT's locked save still falls through to DOT's mint path. This is a millisecond race.
- **R3 (minor):** DOT settings in a web process are only re-synced by a façade request. A process that serves only `/o/` or `/api/` keeps stale values. Deactivating the plugin never restores DOT's values in web processes.

None of these blocks WP2.

## Test runs

```
$ dev/test.sh inventree_mcp.tests.test_auth_token
Ran 18 tests in 9.691s
OK

$ dev/test.sh inventree_mcp.tests.test_auth_flow
Ran 1 test in 3.270s
OK

$ dev/test.sh inventree_mcp.tests.test_settings_inject
Ran 8 tests in 5.576s
OK
```

## 1. F1: refresh after revocation

**Code** (`inventree_mcp/auth/token.py`):

- **:129-136:** the row lookup copies DOT's `validate_refresh_token` (`oauth2_validators.py:1197-1203`): same checksum, unrevoked row preferred.
- **:137-140:** façade applications only (see F8).
- **:142-150:** `membership.refresh_grant_allowed(user)` now runs before the revoked branch, so it runs for every row: live, rotated, revoked and past-grace.
- **:152-161:** for a revoked row, a revocation inside the grace window with no successor access token (`AccessToken.objects.filter(source_refresh_token=rt).exists()` is false) gets `invalid_grant`. Two cases still go to DOT: a rotated row inside the window, and any row past the window.
- **:188-192:** `_revoked_within_grace` is the negation of DOT's test at `oauth2_validators.py:1208-1210` and uses the same setting.
- **:163-171:** unknown original scopes get `invalid_grant`.
- **:51-53:** `_scope_list` drops the `''` that `scope_to_list('')` yields.

**Why this closes the hole.** Look at what DOT does with a revoked row inside the grace window. `validate_refresh_token` accepts it (`:1208-1224`). In `_save_bearer_token`, `previous_access_token = AccessToken.objects.filter(source_refresh_token=rt).first()` (`:982`) decides the outcome:

- If it is `None`, DOT mints a fresh pair (`:997-1006`). That was F1.
- Otherwise DOT re-serves the existing pair (`:1007-1014`).

Both `RefreshToken.revoke()` (`models.py:676-699`) and `AccessToken.revoke()` (`models.py:605-610`, a delete) remove a rotated row's successor access token. So "successor exists" is exactly DOT's re-serve condition. The façade sends DOT only the rows for which DOT re-serves and does not mint.

Past the window, DOT returns `False` and applies reuse protection (`:1211-1215`), so nothing is minted there either. `get_original_scopes` (`:1178-1187`) returns `[]` when the access token is gone, and the façade now refuses that case (`:166-171`) instead of passing it on. If a client sends an explicit `scope` while the original scope is unknown, oauthlib refuses it with `invalid_scope`: the requested scopes are not within `[]`.

**(a) Live probe** (DCR client, admin session as in `dev/oauth_smoke.py`). The DB counts come from `dev/shell.sh -c`. One `AccessToken` row per client is DOT's DCR registration token (`user=None`, scope `oauth2_provider:registration`), so the baseline is 1, not 0.

```
after token exchange:   AT 2 RT live 1 RT revoked 0
revoke -> 200 | access token on MCP -> 401
after revoke:           AT 1 RT live 0 RT revoked 1
refresh with revoked RT 2.44s after revoke -> 400 {'error': 'invalid_grant', 'error_description': 'The refresh token was revoked. Sign in again.'}
after refresh attempt:  AT 1 RT live 0 RT revoked 1     <- no new AccessToken or RefreshToken row
```

The test `test_refresh_after_revoke_all_mcp_tokens_is_refused` covers the `RefreshToken.revoke()` path through `revoke_all_mcp_tokens`. That is WP2's `block_user` path. The test passes.

**(b) Rotation retry inside the grace window (live):**

```
refresh -> 200 rotated: True
after rotation: AT 2 RT live 1 RT revoked 1
retry with old RT -> 200 same pair: True
after retry:    AT 2 RT live 1 RT revoked 1 | re-served AT on MCP -> 200
```

`test_refresh_rotates_and_old_token_is_rejected` (`test_auth_token.py:98-103`) asserts the same.

**(c) Membership gate.** Every row reaches `token.py:143` before the revoked branch at `:152`, whether revoked, rotated or past the grace window. `test_membership_gate_runs_for_rotated_tokens_too` (`test_auth_token.py:205-219`) mocks the gate on a rotated row and asserts `gate.assert_called_once_with(self.user)` plus a 400. No test mocks the gate on a revoked, non-rotated row. The outcome there is `invalid_grant` either way, and the call order is clear from the code.

**(d) Unknown and empty scopes:**

- `test_unknown_original_scope_is_refused` deletes the access token, so `get_original_scopes` returns `[]`, and asserts a 400.
- `test_empty_scope_parameter_keeps_the_original_scope` posts `scope=` and asserts 200 with the original scope. Before the fix, `['']` narrowed to `[]` and the token was revoked.

**(e) Reuse protection (live).** RT1 is rotated to RT2, then to RT3, and RT1 is replayed:

```
in grace:   replay RT1 -> 400 invalid_grant | AT 2 RT live 1 RT revoked 2 | RT3's AT on MCP -> 200
>120 s:     replay RT1 -> 400 invalid_grant | AT 1 RT live 0 RT revoked 3   (family revoked by DOT)
            RT3's AT on MCP -> 401 ; refresh with RT3 -> 400 invalid_grant
```

Past the grace window the family is revoked as before. Inside the window the replay is refused, but the family survives. See R1.

**Can a revoked token still mint?** Not in sequential use, for either revocation route. The only remaining path is the concurrent race in R2.

## 2. F2: `approval_prompt=auto`

**Code:** `authorize.py:157-162` copies `request.GET` and forces `approval_prompt=force` before both the skip decision and `super().get()`. DOT reads `request.GET.get("approval_prompt", oauth2_settings.REQUEST_APPROVAL_PROMPT)` (`views/base.py:257`). The key is now always present, so neither the query parameter nor an instance-wide `REQUEST_APPROVAL_PROMPT=auto` reaches the `auto` branch (`:274-291`).

Consent can still be skipped in two ways:

- `_may_skip_consent()` (`authorize.py:190-197`): a trusted CIMD client, no loopback redirect, no `prompt`.
- DOT's `application.skip_authorization` (`views/base.py:268`), which only an admin can set. DCR clients are created with `skip_authorization=False` (`clients.py:145`).

The oauthlib request is built from `get_full_path()`, not `request.GET`, but oauthlib ignores `approval_prompt`, so that does not matter.

**Live probe:** a loopback DCR client holding a live token (MCP returned 200).

```
authorize approval_prompt=auto             -> 200 consent shown: True
authorize approval_prompt=auto&prompt=none -> 200 consent shown: True
```

`test_approval_prompt_auto_does_not_skip_consent` covers an https DCR client and a loopback DCR client. No test sets `REQUEST_APPROVAL_PROMPT=auto` instance-wide. The code makes that case safe, so it is only a coverage gap.

**Informational:** `prompt=none` renders the consent page. OIDC expects `consent_required` or `login_required` instead. This was not a WP1 finding, and MCP clients do not send it.

## 3. F3: start-up injection, restore, workers

**How `_stored_value` works** (`settings.py:24-49`, used at `:79-80`):

- `get_plugin_setting` checks `PLUGIN_SLUG in registry.plugins`, the dict of active instances.
- While the plugin is absent from that dict, it reads `PluginConfig(key=slug)`, requires `is_active()` (active or mandatory), and reads the raw `PluginSetting.value`.
- `_coerce` converts the value (`str2bool` / `int`). Any exception falls back to the caller's default (`:89-90`).

The plugin is absent from `registry.plugins` in three situations:

- During the plugin's own `__init__`. `_init_plugin` instantiates the class before `safe_reference` adds it (`plugin/registry.py:828` instantiates, `:872` registers), and `_clean_registry` empties the dict on every reload.
- In processes that have not loaded plugins.
- While the plugin is inactive.

**Is it safe without the plugin tables (migrations)?** Yes, in practice.

- `PluginAppConfig.ready()` loads plugins during `migrate` (`canAppAccessDatabase(allow_plugins=True)`). But `ready()` runs at `django.setup()`, before the migrate command opens any transaction, so queries run in autocommit.
- `_init_plugins` runs `PluginConfig.objects.all()` (`registry.py:891`) before it instantiates any plugin. If the table is missing, that query raises first, `_load_plugins` swallows the error (`:421-423`), and the plugin's `__init__` never runs.
- If a DB error ever reached `_stored_value` inside an atomic block, the `except Exception` would hide it but leave a PostgreSQL transaction aborted. I found no path that does this (see R4).

**Start-up injection (live):** both fresh shells see the façade values.

```
server shell 2592000 3600 True True
worker shell 2592000 3600 True
```

The values are `REFRESH_TOKEN_EXPIRE_SECONDS`, `ACCESS_TOKEN_EXPIRE_SECONDS`, `CIMD_ENABLED` and, in the server shell, `REFRESH_TOKEN_REUSE_PROTECTION`. In the review these read `None 36000 False`.

**Restore when off:**

- `FacadeViewMixin.dispatch` (`auth/__init__.py:80-83`) and `challenge_header` (`discovery.py:57`) call `ensure_applied()` before the enabled check.
- `_write` (`settings_inject.py:107-125`) restores `_originals`.
- `test_disabled_facade_view_restores_dot_settings` and `test_disabling_restores_what_inventree_configured` cover this.

**django-q:** `connect_signals()` (`settings_inject.py:188-212`) connects `_before_task`. django-q sends `pre_execute` in the worker process, before the payload runs (`django_q/worker.py:92`). So the daily `run_oauth_maintenance` / `clear_expired` sees the façade lifetimes. `_before_task` swallows exceptions (`:180-185`), so it cannot break a task.

**Idempotence across registry reloads:**

- InvenTree's reload runs `importlib.reload()` only on the module that defines the plugin class, `inventree_mcp.core` (`registry.py:806-815`). `inventree_mcp.auth.settings_inject` is never reloaded, so the receivers are always the live module's functions.
- `dispatch_uid` stops duplicates.
- Live check: two `InvenTreeMCP()` calls, then `importlib.reload(inventree_mcp.core)`, then a third `InvenTreeMCP()`, left 1 `pre_execute` receiver and 2 `post_save` receivers (one each for `PluginSetting` and `PluginConfig`). `settings_inject` was the same module object throughout.
- Caveat: with `dispatch_uid`, a reloaded `settings_inject` would keep the first receiver. Django does not replace a receiver on a repeated `connect`. That would only matter if the module itself were reloaded, which InvenTree does not do.

**Staleness across processes:**

- `post_save` invalidation only reaches the saving process.
- Another gunicorn worker picks up a change at its first `ensure_applied()` after its 5 s memo expires (`settings_inject.py:158-175`). That call only happens on a façade request, an MCP 401 challenge, or (in qcluster) a task.
- `plugin.get_setting()` itself is not stale. InvenTree caches settings per request (thread-local, cleared by middleware) and, when configured, in the shared Redis cache that `save()` updates.
- The bound is therefore "≤ 5 s after the next façade request, 401 or task", not "≤ 5 s". See R3.

## 4. F8 and F10

**F8:**

- `token.py:98`: the code grant is checked only if `_is_mcp_application(grant.application_id)`.
- `token.py:137-140`: the same check applies to the refresh row.
- Otherwise the hook returns `None` and DOT alone answers, after client authentication.
- `MCPRevokeView` (`token.py:203`) has no hooks.
- `test_other_applications_are_left_to_dot` uses a manual confidential application with `effective_scopes` patched to empty. It asserts DOT's 401 and that the refresh token is not revoked. Under the old code the hook revoked the token, so this test would fail there.

**F10 at the authorize GET:** `authorize.py:151-155` accepts only `{resource(request), resource(request).rstrip('/')}` (`:102-105`). Anything else gets an `invalid_target` redirect to the validated redirect URI.

**F10 at the consent POST:** `form_valid` (`authorize.py:240-241`) re-checks the posted hidden `resource` before DOT's `form_valid` stores it on the grant. It answers with a 400 error page, not a redirect. The deny branch goes to DOT, which issues no grant. If the field is dropped, the grant has no resource, which is the same as an authorize request without `resource`.

**Live probe:**

```
authorize resource=https://other.example/api/ -> 302 http://127.0.0.1:43123/callback?error=invalid_target&...
hidden resource field: http://localhost:8080/plugin/inventree-mcp/mcp/
consent POST with tampered resource -> 400 (no Location) | grants for client: 0
```

**Informational:** at the token endpoint, a code whose grant has no resource can still be exchanged with `resource=<foreign URI>`. DOT accepts any absolute URI when the grant has none (`oauth2_validators.py:864-890`). The review showed such tokens are refused by MCP and `/api/`, so the consent-POST restriction is not bypassed in effect. To make it strict, refuse a foreign `resource` in `MCPTokenView.post` as well.

## 5. Test quality

**Fail on the old behaviour.** I checked `test_revoke_endpoint_revokes` (`test_auth_token.py:170-193`) by reading it. With the old code the revoked row went straight to DOT, and DOT minted a pair inside the grace window. So `assertEqual(status, 400)` fails. The new `assertFalse(AccessToken.objects.filter(user=self.user, application__client_id=...).exists())` also checks that nothing was minted. It filters on `user`, so the registration token (`user=None`) does not interfere.

By the same reading, these also fail on the old code:

- `test_membership_gate_runs_for_rotated_tokens_too`: the gate was never called for revoked rows.
- `test_unknown_original_scope_is_refused`: DOT issued a token with an empty scope.
- `test_empty_scope_parameter_keeps_the_original_scope`: the token was revoked.
- `test_other_applications_are_left_to_dot`: the token was revoked.
- `test_approval_prompt_auto_does_not_skip_consent`: the old code answered with a 302.
- `test_auth_flow` step 10.

**Tautological tests:** none.

- `test_desired_values_are_memoised_between_saves` simulates a change from another process with `update()`, which sends no signal. It then checks both the memo and the expiry.
- `test_plugin_init_injects_while_the_registry_is_loading` removes the slug from `registry.plugins` to simulate start-up. That is faithful to `registry.py:828-872`.

**Weak spots (low):**

- `test_scheduled_task_sees_the_facade_values` calls `connect_signals()` itself, so it does not prove that the plugin connects the receiver. `core.py:151` does connect it, and the live receiver count confirms this.
- Missing tests:
  - gate invocation on a revoked, non-rotated row;
  - an instance-wide `REQUEST_APPROVAL_PROMPT=auto`;
  - the in-grace replay after a double rotation (R1).

## Residual issues

**R1 (low): in-grace replay of a stale rotated token does not revoke the family.**

- **Where:** `token.py:152-160`.
- **Why:** RT1 is rotated to RT2 and then to RT3. Replaying RT1 inside the window finds no successor, because rotating RT2 deleted AT2, so the façade answers `invalid_grant`. A legitimate client cannot hold RT1 once RT2 has been used, so this replay is a theft signal. DOT revokes the family for that signal only past the window.
- **Effect:** before the fix DOT minted here, so the fix is still strictly better.
- **Fix:** in that branch, before `_refused(...)`, revoke the family when `oauth2_settings.REFRESH_TOKEN_REUSE_PROTECTION and refresh_token.token_family`:

  ```python
  for related in get_refresh_token_model().objects.filter(token_family=refresh_token.token_family):
      related.revoke()
  ```

  This mirrors `oauth2_validators.py:1211-1214`. It is harmless for a plain revocation, because the rest of that family is already revoked. Add a test that rotates twice, replays the first token, and asserts the third refresh token is revoked.

**R2 (low): time-of-check/time-of-use race between the pre-check and DOT.**

- **Where:** `_check_refresh_grant` reads the row before `super().post()`, without a lock.
- **Why:** a revocation that commits after the check but before `validate_refresh_token` sends DOT down the mint path: inside the window, `previous_access_token` is `None` (`oauth2_validators.py:982-1006`). It needs a concurrent revocation and refresh within milliseconds.
- **Fix:** set `oauth2_validator_class` on `MCPTokenView` to a subclass of DOT's `OAuth2Validator`. Override `_save_bearer_token` to repeat the check under DOT's own lock: after the `select_for_update` of `refresh_token_instance` (`:977`), if the row is revoked and has no successor, raise `oauthlib.oauth2.rfc6749.errors.InvalidGrantError`. The pre-check can stay for the clean error text. Alternatively, report this upstream together with the grace-for-revoked behaviour (the report's WP6 item).

**R3 (minor): only façade traffic re-syncs DOT settings in web processes.**

- **Where:** `settings_inject.ensure_applied()` is called only from `FacadeViewMixin.dispatch`, `challenge_header`, the `pre_execute` hook and `cleanup_stale_clients`.
- **Effect 1:** a gunicorn worker that serves only `/o/` (DOT's own endpoints for other applications) or `/api/` keeps the previous `CIMD_ENABLED` and lifetimes until it serves a façade request. That happens after an admin changes a lifetime or switches `OAUTH_ENABLED` off in another worker.
- **Effect 2:** deactivating the plugin itself, as opposed to switching `OAUTH_ENABLED` off, never restores DOT's values in web processes. The façade routes disappear, and a registry reload does not instantiate an inactive plugin. Workers do restore at their next task, because `_stored_value` sees the inactive config.
- **Fix:** in `connect_signals()`, also connect `django.core.signals.request_started` to a receiver that calls `ensure_applied()`, with a `dispatch_uid`. The 5 s memo keeps it to one small settings read per process every 5 s. The receiver stays connected after deactivation, because the module stays imported. Otherwise, document "restart InvenTree after deactivating the plugin" in the README.

**R4 (informational): the `_stored_value` failure mode inside a transaction.**

- **Why:** if a DB error occurs inside an outer `atomic()` block, the `except Exception` in `get_plugin_setting` leaves a PostgreSQL transaction aborted. I found no path to this today (see §3).
- **Fix (optional):** wrap the two queries in `with transaction.atomic():`, which creates a savepoint so the error can be rolled back cleanly.

## What WP2 may rely on

WP2 may rely on the following:

- **Gate placement.** `membership.refresh_grant_allowed(user)` runs for every refresh of a façade application's token, before DOT and before any revoked-row handling. So a refusal there always ends in `invalid_grant`, including for tokens re-served inside the grace window. `membership.token_grant_allowed(user)` gates every code exchange.
- **Revocation is final.** After `membership.revoke_all_mcp_tokens(user)` or `/oauth/revoke/`, the revoked refresh tokens are refused immediately and mint nothing. A rotated token retried within 120 s gets the existing pair back, after the gate.
- **Narrowing on refresh.** Scopes are re-narrowed with `policy.narrow()` on every refresh. Empty narrowing revokes the token.
- **Consent.** Consent is skipped only through `_may_skip_consent()`.
- **Injected settings.** The façade's DOT settings are in effect from process start in web, shell and qcluster processes, and before every django-q task.

WP2 should not assume:

- that a web process switches DOT's settings back on its own after the plugin is deactivated (R3);
- that a replayed stale token inside the grace window revokes the family (R1).

Because the gate also runs for dead and past-grace tokens, an upstream membership re-check in `refresh_grant_allowed` should be cheap or cached. It can be triggered by replays of tokens that would be refused anyway.
