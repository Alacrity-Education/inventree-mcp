# WP3 report: the second permission set (MCP roles per group)

Microplan: `docs/plans/WP3-mcp-permission-set.md`. Branch `feature/oauth-facade`, on top of WP1, WP2 and their review fixes. Nothing is staged or committed; the orchestrator commits.

## What was built

| File | Content |
|---|---|
| `inventree_mcp/models.py` | `MCPRuleSet`: `group` (FK `auth.Group`, `related_name="mcp_rule_sets"`, CASCADE), `name` (`max_length=50`, `choices=users.ruleset.RULESET_CHOICES`), `can_view` / `can_add` / `can_change` / `can_delete` (default False), `unique_together = (("group", "name"),)`, `RULE_OPTIONS`, `label` property (as core), `save()` implies view for any write flag and change for add/delete (exactly core's `RuleSet.save()`, without the `group.save()`), `check_user_permission`: active staff. The docstring warns against reverse lookups. |
| `inventree_mcp/migrations/0002_mcpruleset.py` | Generated in the container (`dev/manage.sh makemigrations inventree_mcp`), then edited: the role choices are plain strings instead of `users.ruleset.RuleSetEnum['…']` (so a renamed core enum member cannot break the historical migration; `makemigrations --check` still reports "No changes detected"), plus `RunPython(create_rows_for_existing_groups, noop)`: one all-false row per existing group and current `RULESET_NAMES`, `bulk_create(ignore_conflicts=True)`. Depends on `auth.0012` and `inventree_mcp.0001_initial`. Applied on the live instance at restart (`[X] 0002_mcpruleset`); every existing dev group got 11 rows. |
| `inventree_mcp/auth/policy.py` | Bodies replaced, signatures kept. `effective_scopes(user)`: empty for anonymous / inactive / outside `MCP_REQUIRED_GROUPS` (WP2 behaviour kept); all advertised scopes for a superuser with `MCP_SUPERUSER_FULL_ACCESS`; otherwise `ruleset_scopes(user) ∩ mcp_scopes_available()` (so `MCP_READ_ONLY` strips add/change/delete) plus `g:read` when non-empty. One SELECT: `MCPRuleSet.objects.filter(group__in=user.groups.all()).values_list(...)` (a subquery, no reverse relation), wrapped in `membership._read` (savepoint, fail safe). New helpers `mcp_roles(user)` (union of the flags per role) and `ruleset_scopes(user)` (core's `get_granular_scope`). `may_connect`: anonymous, inactive, blocked, not in required groups, then `no_mcp_permission` when `effective_scopes` is empty; reason set unchanged. `narrow` unchanged. Without app integration there are no rulesets: fail closed, one WARNING per process. |
| `inventree_mcp/auth/signals.py` | `ensure_rulesets(group)`; Group `post_save` (created only, savepoint, `DatabaseError` logged, skipped without the app); Group `pre_delete` revokes the members when the group's rows granted anything (members are still known then) and marks the group so the cascade's row `post_delete` does not revoke again; MCPRuleSet `pre_save` snapshots the stored flags, `post_save` revokes the group's members when any flag went true → false; `post_delete` of a granting row revokes. Revocation is `membership.revoke_all_mcp_tokens(user)` only, never `block_user`. More than `REVOKE_SYNC_LIMIT = 20` members: `offload_task("inventree_mcp.auth.tasks.revoke_mcp_tokens", user_ids)`. Logs counts and the group id only. Ruleset receivers use the lazy sender `"inventree_mcp.MCPRuleSet"` (connected once the app is installed). All with `dispatch_uid`, from `connect_signals()`. |
| `inventree_mcp/auth/tasks.py` | `revoke_mcp_tokens(user_ids)`. |
| `inventree_mcp/mcp_transport.py` | `scoped_token_for(user)`: an **unsaved** `oauth2_provider.models.AccessToken(user=…, scope=" ".join(policy.narrow(None, user)), expires=now + 1 day)`, bound as the identity's `oauth2_token` for every authenticated non-OAuth request (API token, Basic), so `proxy.call_view()` / `proxy.user_has_access()` take their OAuth branch (`oauth2_bridge.authentication_classes_for`, `scoped_view_class`) and core's scope check enforces it. Empty scope → 403 "No MCP permission" (normally already refused by `may_connect`). `get_authenticators()` drops `BasicAuthentication` unless `MCP_ALLOW_BASIC_AUTH`. The WP2 `may_connect` 401/403 handling is unchanged. |
| `inventree_mcp/oauth2_bridge.py` | "Bug 3" workaround (see deviation 1): `_any_role_alternatives()` splits a multi-role scope alternative (`[["r:view:part", "r:view:build"]]`) into one alternative per role scope. |
| `inventree_mcp/context.py` | Comment only: `oauth2_token` is also the transport's scoped token now. |
| `inventree_mcp/auth/api.py` (new) | The DRF API, see "API" below. |
| `inventree_mcp/auth/urls.py` | `api-*` URL patterns (plain DRF views, not `auth_exempt`). |
| `inventree_mcp/core.py` | Settings `MCP_SUPERUSER_FULL_ACCESS` (bool, False) and `MCP_ALLOW_BASIC_AUTH` (bool, False). |
| `inventree_mcp/tests/test_policy.py`, `test_api.py` (new); additions to `test_auth_token.py`, `test_auth_flow.py`, `test_membership.py`, `oauth_helpers.py`, `test_mcp.py` | See "Tests". |
| `dev/permissions_e2e.py` (new) | The acceptance run (below). Imports `connect`, `create_api_token`, `delete_api_tokens`, `state`, `USER` from `membership_e2e.py` and `JSON`, `expect`, `mcp_tools_list`, `password_login`, `refresh`, `step` from `oauth_smoke.py`; nothing copied. |
| `dev/configure_inventree.py`, `dev/README.md` | Dev defaults for WP3 (deviation 10). |
| `README.md`, `AGENTS.md` | README: new section "Two permission sets" (the rule *MCP access = interactive roles ∩ MCP roles*, behaviour, the scope mapping table, the API table), the two settings, Authentication (Basic needs the setting), setup step 2 (grant MCP roles first), prerequisites (`MCPRuleSet` needs app integration), the MCP-request row of the check-point table. AGENTS.md: file map (models, policy, signals, api, urls, tasks, transport, bridge, tests) and the e2e command. |

### API (`/plugin/inventree-mcp/api/…`, names under `plugin:inventree-mcp:`)

| Name | Path | Methods | Who |
|---|---|---|---|
| `api-ruleset-list` | `api/rulesets/[?group=<pk>]` | GET: rows ensured (all groups without `?group`), core's `RuleSetSerializer` shape (`MCPRuleSetSerializer` subclasses it with `model = MCPRuleSet`), sorted by label, plain list | staff admin |
| `api-ruleset-detail` | `api/rulesets/<pk>/` | GET, PATCH/PUT (flags only; `pk`, `name`, `label`, `group` read-only from core's Meta) | staff admin |
| `api-ruleset-copy` | `api/rulesets/copy/` | POST `{"group": pk}`: flags := the group's core `RuleSet` flags; returns the rows | staff admin |
| `api-ruleset-clear` | `api/rulesets/clear/` | POST `{"group": pk}`: all flags false; returns the rows | staff admin |
| `api-ruleset-revoke` | `api/rulesets/revoke/` | POST `{"group": pk}` → `{"users": n, "tokens": m, "queued": bool}` | staff admin |
| `api-effective` | `api/effective/<user_pk>/` | GET → `{user, username, superuser, roles, mcp_roles, effective, scopes, may_connect, reason}`; each role map is `{role: {label, view, add, change, delete}}` for every core role | the user; staff admin for others |
| `api-connection-list` | `api/connections/[?user=<pk>]` | GET → `[{pk, client_name, client_host, client_trusted, created, expires, access_expires, scopes}]` | the user; `?user=` staff admin |
| `api-connection-detail` | `api/connections/<pk>/` | GET; DELETE → 204, DOT's `RefreshToken.revoke()` (removes the access token too) | owner; staff admin |
| `api-status` | `api/status/[?user=<pk>]` | GET → `{oauth_enabled, mcp_url, membership: {checked_at, blocked, reason}, scopes, may_connect, reason}` | the user; `?user=` staff admin |

"Staff admin" = `IsStaffAdmin`: active staff and core's `users.permissions.check_user_role(user, "admin", "change")` (superusers pass, as in core). Permission classes are `[IsAuthenticated, oauth2_bridge._FixedTokenMatchesOASRequirements]` (+ `IsStaffAdmin`), with a static `required_alternate_scopes`: `a:staff` for every method of the staff endpoints, `g:read` for the self-service ones; so an OAuth token needs `a:staff` to change MCP permissions, which no MCP token can carry. Authentication is InvenTree's default list. Copy/clear use `bulk_update` (no per-row signals) and revoke the members once when something shrank; PATCH goes through `MCPRuleSet.save()` and the signals.

## Verification of the WP2 hand-over (built on, not re-done)

- `membership._classify()` classifies identity vs. authorisation refusals; with real rulesets a member whose groups grant nothing now gets `Outcome.NO_MCP_PERMISSION` (revoked, not blocked). New unpatched test `NoMcpPermissionTest.test_real_ruleset_without_flags_is_an_authorisation_refusal`.
- `effective_scopes` is empty outside `MCP_REQUIRED_GROUPS` (kept, tested).
- The login receiver's identity rule is untouched.
- The transport's `policy.may_connect` 401/403 is unchanged; WP3 only adds the narrowing after it.

## Deviations, with reasons

1. **Multi-role tables: any one role's scope suffices on the MCP path (`oauth2_bridge._any_role_alternatives`).** Core's `map_scope(roles=[...])` requires *all* scopes of the roles that share a table (`part_part` is in *part* and *build*; `stock_stockitem` in *stock* and *build*; the company tables in *part* and the order roles), while core's role check for the same request (`check_user_permission`) accepts *any* of the roles. Without the split, "MCP view on parts only" (the spec's test 3 and acceptance) grants no part tool at all, and every MCP role grid would need *build* ticked to be useful. The bridge already pre-computes `required_alternate_scopes` from core's resolver (bugs 1 and 2); it now also splits multi-role alternatives. This affects only views proxied by the plugin (real OAuth tokens and the scoped tokens on `/mcp`), never InvenTree's own `/api/`. Visible effect with MCP view on parts only: 16 tools, including manufacturer / supplier part tools (those tables belong to the *part* role). Please confirm or reject; reverting is one line in `scoped_view_class`, and the acceptance then needs *part* and *build*.
2. **Reads of the MCP rulesets need staff + admin.change too.** Core's `StaffRolePermissionOrReadOnly` lets any authenticated user read `users.RuleSet`; the spec asks for 403 for non-staff on the rulesets. The role check itself is core's `check_user_role`. `?user=` on connections and status, effective-of-others and deleting someone else's connection use the same `IsStaffAdmin` (the spec says "staff"; the stricter check matches the staff-only GUI pages).
3. **Revoke endpoint: `queued` added.** Above 20 members the revocation is offloaded; `tokens` is then the number of live façade tokens at queueing time.
4. **Connections:** no `last_used` (DOT records none; the frontend treats it as optional). `expires` is `created + REFRESH_TOKEN_EXPIRE_SECONDS`, the rule DOT's cleanup applies (a rotated token's `created` is its rotation time). `GET api/connections/<pk>/` is served as well.
5. **The API is served whether or not `OAUTH_ENABLED` is on** (MCP roles also narrow API tokens), but, being in `auth/urls.py` as the overview places it, only when `facade_supported()` (DOT ≥ 3.4, true on every supported InvenTree). The views are not `auth_exempt`: InvenTree's middleware applies (session or API token pass; Basic/Bearer to `/plugin/` get its 401 before DRF).
6. **Row creation on Group `post_save` only for `created=True`** (core runs its RuleSet sync on every save); the API's lazy `ensure_rulesets()` covers roles added by a core upgrade.
7. **Group deletion is handled in `pre_delete` of the Group** (the members are gone by the time the rows' `post_delete` runs in the cascade); a process-local marker stops the rows' `post_delete` from revoking again.
8. **Fail closed without app integration.** `effective_scopes` is empty when the plugin's app is not installed (no `MCPRuleSet`), so every MCP request gets 403; one WARNING per process. README says so.
9. **`MCPRuleSet.save()` mirrors core's flag implication** (write implies view, add/delete imply change), so the grid behaves like core's; copy/clear set the flags directly (core rows are already consistent).
10. **Dev defaults (`dev/configure_inventree.py`):** "MCP Users" gets MCP view on every role, and `admin` is put into "MCP Users". Without that, `oauth_smoke.py` (both modes) and `membership_e2e.py` fail under WP3 because nobody has MCP roles. The block is skipped (with a message) while the plugin's table does not exist yet. The file's existing single-quote style was left as it was (it was never ruff-formatted; `ruff check --isolated` passes).
11. **Test fixtures:** `OAuthFacadeTestMixin` grants the test group every MCP role by default (`mcp_roles = ("all",)`, override per class), so the WP1/WP2 façade tests keep their meaning; `MembershipTestCase` grants "MCP Users" (core's group sync removes the test group); `test_mcp.py`'s `MCPTransportTest` and `ToolLoggingTest` grant all MCP roles in `setUpTestData` (the only `test_mcp.py` change). Two WP2 tests were adapted to WP3 semantics: `DueAndEnforcementTest.test_required_groups` (a user with no group now gets `no_mcp_permission`, then passes once a group grants an MCP role) and `TransportTest.test_unreachable_past_grace_denies_long_lived_credentials` (Basic needs `MCP_ALLOW_BASIC_AUTH`).

## Tests

New and changed, mapped to the spec:

1. `test_policy.EffectiveScopesTest`: new group gets a row per role; no rulesets → empty and `no_mcp_permission`; part view → `{"g:read", "r:view:part"}`; add/change/delete only with `MCP_READ_ONLY` off (implications included); union across groups, other groups ignored; superuser with and without `MCP_SUPERUSER_FULL_ACCESS`; inactive and outside the required groups → empty; one SELECT on `inventree_mcp_mcpruleset`.
2. `test_policy.NoMcpPermissionTest`: authorize renders the 403 page (`reason == "no_mcp_permission"`, "No MCP access"); refresh after the flags were cleared without signals → `invalid_grant` and the refresh token revoked by the narrowing. `test_auth_token`: `test_refresh_after_the_group_lost_every_mcp_flag` (through the signals: revoked at once, refresh `invalid_grant`, access token 401, no live token left) and `test_code_exchange_grants_the_mcp_roles_only`.
3. `test_auth_flow.PermissionSetFlowTest`: interactive part view+change and stock view, MCP part view → the code exchange grants `g:read r:view:part`; `tools/list` has `list_parts` / `get_part`, no stock tool; `list_stock_items` fails with the proxy's permission error, `list_parts` succeeds; the same user's API token still `PATCH`es `/api/part/<pk>/` and reads `/api/stock/`.
4. `test_policy.TransportNarrowingTest`: API token tools/list identical to an OAuth token's, stock tool call refused, `/api/stock/` 200; empty effective → 403 "No MCP permission" without challenge; Basic → 401 unless `MCP_ALLOW_BASIC_AUTH`, then narrowed; the scoped token is unsaved and carries the effective scopes; DOT's `TokenMatchesOASRequirements` touches nothing but `is_valid()` (a probe token raises on any other attribute); PartList accepts either `r:view:part` or `r:view:build`; `tool_visibility.visible_tool_names` and `proxy.user_has_access` reflect the scoped token with no change to `tool_visibility.py`.
5. `test_policy.ReductionRevokesTest`: adding a flag or saving unchanged revokes nothing; clearing view, reducing change, deleting a granting row, deleting the group revoke the members' façade tokens, never the other group's member, never API tokens; deleting a group without MCP access revokes nothing; more than 20 members → exactly one `offload_task` with all ids, and the task revokes; the MCP endpoint refuses the member's access token at once; the log lines carry no username.
6. `test_api`: non-staff 403 on every ruleset endpoint, staff without admin.change 403, anonymous refused; list sorted by label with core's field set, missing row re-created, bad `group` 400; PATCH changes flags only; copy equals the core flags and revokes nothing on additions, clear clears and revokes; revoke returns `{"users": 1, "tokens": 1, "queued": false}` and spares other groups; an OAuth token without `a:staff` cannot PATCH; new group rows; the migration's `create_rows_for_existing_groups` creates all-false rows and keeps existing ones; effective self/others/404, the document's labels, roles, mcp_roles, effective and scopes; connections own list, `?user=` 403 / staff 200, revoke own 204, someone else's 404 (staff admin 204); status fields and `?user=`.

Plus `test_membership.NoMcpPermissionTest.test_real_ruleset_without_flags_is_an_authorisation_refusal`.

Command output (dev stack, InvenTree 1.5.7, DOT 3.4.0):

```
$ dev/test.sh inventree_mcp
Ran 316 tests in 305.208s
OK (skipped=3)            (the three WellKnownRedirectTest skips, as in WP1/WP2)

$ dev/test.sh inventree_mcp.tests.test_policy      Ran 25 tests in 29.386s   OK
$ dev/test.sh inventree_mcp.tests.test_api         Ran 14 tests in 28.328s   OK
$ dev/test.sh inventree_mcp.tests.test_auth_flow   Ran 2 tests in 13.975s    OK
$ dev/test.sh inventree_mcp.tests.test_auth_token  Ran 23 tests in 20.333s   OK
$ dev/test.sh inventree_mcp.tests.test_membership  Ran 42 tests in 92.348s   OK
$ dev/test.sh inventree_mcp.test_mcp               Ran 132 tests in 33.390s  OK
```

Lint:

```
$ uvx ruff@0.16.0 check --preview inventree_mcp                              -> All checks passed!
$ uvx ruff@0.16.0 format --preview --check inventree_mcp                     -> 52 files already formatted
$ uvx ruff@0.16.0 check --preview --isolated inventree_mcp/tests             -> All checks passed!
$ uvx ruff@0.16.0 format --preview --isolated --check inventree_mcp/tests    -> 11 files already formatted
$ uvx ruff@0.16.0 check --isolated dev/permissions_e2e.py dev/membership_e2e.py dev/oauth_smoke.py dev/configure_inventree.py -> All checks passed!
$ uvx ruff@0.16.0 format --isolated --check dev/permissions_e2e.py           -> already formatted
$ dev/manage.sh makemigrations inventree_mcp --check --dry-run              -> No changes detected in app 'inventree_mcp'
```

Schema: InvenTree's `invoke dev.schema` runs with `INVENTREE_PLUGINS_ENABLED=False`, so plugin views never reach it. With plugins enabled, `manage.py schema --validate --fail-on-warn` (`INVENTREE_SITE_URL=http://localhost:8000 INVENTREE_ALLOWED_HOSTS='*'`; without the hosts override core's own `StockList` fails on `testserver`, unrelated) exits 0 and includes the nine `/plugin/inventree-mcp/api/…` paths with explicit components (`MCPRuleSet`, `GroupAction`, `RevokeResult`, `Effective`, `RoleFlags`, `Connection`, `Status`, `MembershipState`), unpaginated lists, and `oauth2: [a:staff]` security on the staff endpoints.

## End to end in the dev environment

`dev/compose.sh restart inventree-server inventree-worker`, then `python3 dev/permissions_e2e.py`. The admin works through the plugin API with a session (allauth headless login, CSRF), as the web UI does; MCP Users' rows are snapshotted and restored at the end:

```
[6] allauth headless login as 'admin' -> 200
[0] 'MCP Users' MCP roles before: ['admin', 'bom', 'build', 'part', 'part_category', 'purchase_order', 'return_order', 'sales_order', 'stock', 'stock_location', 'transfer_order']
[0] 'MCP Users' MCP roles now: [('part', ['view'])]
[0] effective document of e2e-member: interactive view on 11 roles, effective ['part'], scopes=['g:read', 'r:view:part'], may_connect=True
[6] bridge form: POST /api/auth/v1/auth/provider/redirect provider=authentik process=login callback_url=http://localhost:8080/plugin/inventree-mcp/oauth/authorize/
[6] provider redirect -> 302 to http://authentik.localtest.me:19000/application/o/authorize/
[6] Authentik as 'e2e-member' -> back on /plugin/inventree-mcp/oauth/authorize/ (page, 200)
[A] connected through the façade: expires_in=3600 scopes=2
[A] token scope: 'g:read r:view:part'
[A] tools/list -> 200; 16 tools; part tools ['get_manufacturer_part', 'get_part', 'get_supplier_part', 'list_manufacturer_parts', 'list_parts', 'list_supplier_parts']; stock tools []
[A] tools/call list_stock_items -> isError=True 'Error executing tool list_stock_items: You do not have permission to perform this action.'
[A] tools/call list_parts -> isError=True (no permission error)
[B] e2e-member's API token on /api/stock/ -> 200 (count=1278)
[B] e2e-member's API token on /mcp tools/list -> 200; 16 tools; identical to the OAuth list: True
[C] plugin API in the server process: rulesets/ 200, rulesets/ 200, effective/9/ 200, connections/ 200, status/ 200; live connections of e2e-member: 1
[D] cleared MCP view on parts: live MCP refresh tokens of e2e-member 1 -> 0
[D] refresh with the live refresh token -> 400 invalid_grant
[D] tools/list with the access token -> 401
[D] API token on /mcp -> 403 'No MCP permission'
[D] API token on /api/stock/ -> 200
[D] effective document: may_connect=False reason='no_mcp_permission'
[-] restored the MCP roles of 'MCP Users' and deleted the API token
OK: MCP permission set end to end
```

`list_parts` passes the permission check but then fails on the proxy's `testserver` host (known limitation 1, pre-existing; the same failure happens without WP3's scoped token).

`python3 dev/oauth_smoke.py --sso e2e-member` with "MCP Users" set to MCP view on parts only (through `dev/shell.sh`, restored afterwards):

```
[8] approved, code exchanged; token_type=Bearer expires_in=3600 access_token=<30 chars> refresh_token=<30 chars> scopes=2
[9] tools/list -> 200; 16 tools, e.g. ['describe_filters', 'make_web_link', 'list_attachments', 'get_attachment']
[10] refresh -> 200; expires_in=3600 refresh token rotated: True
[11] tools/list with refreshed token -> 200; 16 tools
[14] refresh with the just-revoked refresh token -> 400 invalid_grant
OK: OAuth façade flow completed (SSO as e2e-member)
```

With the dev defaults (MCP view on every role): `python3 dev/oauth_smoke.py` → `OK: OAuth façade flow completed (password login)`, `--sso e2e-member` → 56 tools, OK; `python3 dev/membership_e2e.py --skip-outage` → `OK: membership re-check end to end` (A, B, D, E, F, G as in the WP2 report; E's "after an admin unblock … 403 'No MCP permission'" unchanged).

**Reverse-relation check on the dev stack.** No plugin module filters a core model through `mcp_rule_sets__` / `mcp_state__` (grep; the only hit is a comment in `tasks.py`). Every WP3 query goes from the plugin model (`MCPRuleSet.objects.filter(group__in=user.groups.all())`, `filter(group=…)`, `group.user_set`, `Exists(MCPRuleSet…)`). In the running server process, every path that runs these queries answered 200 in scenario C: rulesets for all groups (`ensure_rulesets` per group), effective, connections, status, plus clear/PATCH and the revocations in 0 and D, and the `/mcp` narrowing in A/B/D. Note: in `dev/shell.sh` the reverse lookups `Group.objects.filter(mcp_rule_sets__can_view=True)` and `User.objects.filter(mcp_state__isnull=False)` *work* (the shell builds the relation tree after the plugin app is registered). So a shell check cannot show the hazard; the HTTP check against the server process is the meaningful one. The shell did confirm rows for all six dev groups (11 each), rows for a newly created group (11), and `MCPRuleSet.objects.filter(group__in=…)` / `Exists` queries.

Plugin log lines written during the runs (ids and counts only):

```
inventree-mcp: MCP permissions of a group reduced (group id 6); revoked 1 MCP tokens of 2 users
inventree-mcp: MCP permissions of a group replaced (group id 6); revoked 1 MCP tokens of 2 users
Revoked 1 MCP tokens for user id 9
```

## Known limitations

1. **Pre-existing, not WP3: tool calls whose serializer builds absolute URLs fail on instances with a strict `ALLOWED_HOSTS`.** `proxy.call_view()` builds its request with DRF's `APIRequestFactory`, whose host is `testserver`. InvenTree derives `ALLOWED_HOSTS` from `SITE_URL`, so for example `list_parts` raises `DisallowedHost` ("Invalid HTTP_HOST header: 'testserver'") on the dev stack, for API tokens and OAuth tokens alike, with or without WP3 (reproduced in `dev/shell.sh` with the pre-WP3 `force_authenticate` path). The test runner allows `testserver`, so the suites never saw it. Likely also in production. Suggested fix (WP5, or before rollout): build the proxied request with the incoming request's host (for example by carrying `request.get_host()` in the bound identity). Not changed here: it is outside WP3 and touches the proxy's core path.
2. Removing a user from a group (or an SSO group sync that does so) revokes nothing. OAuth access tokens keep their scopes until the next refresh (at most `ACCESS_TOKEN_LIFETIME_SECONDS`), where `narrow()` applies; API tokens follow at once. The spec only asks for ruleset reductions and group deletion.
3. Revocation of up to 20 members runs inside the admin's request; above that, inside a django-q task (run inline when no worker is running). An offloaded revocation is not rolled back if the surrounding transaction is.
4. The `pre_save` snapshot is not locked: two concurrent PATCHes of the same row could both miss a reduction made between them. The next refresh's narrowing still applies.
5. The group-deletion marker is per process; a group deleted on one worker while another worker deletes one of its rows could revoke twice (harmless).
6. A core upgrade that adds a role adds no row until the next group creation or the next API list call (`ensure_rulesets`); until then that role grants nothing through MCP, which is the safe default.
7. Without app integration, MCP is closed for everybody (deviation 8).
8. Core's `delete_owner` crashes when a group created in a management shell is deleted (core skips the Owner in management commands). Seen during the shell check, unrelated to the plugin.

## What WP4's backend wiring must know

- **URL names** (namespace `plugin:inventree-mcp:`): `api-ruleset-list`, `api-ruleset-detail`, `api-ruleset-copy`, `api-ruleset-clear`, `api-ruleset-revoke`, `api-effective`, `api-connection-list`, `api-connection-detail`, `api-status`. Paths are exactly what `frontend/src/lib/api.ts` calls; no frontend change is needed.
- **Shapes vs. the built frontend:** lists are plain arrays (the frontend's `asList` handles both). The effective document's role maps now carry `label` for every role (the frontend currently derives labels with `humanize()` and can switch to `label`). Connections omit `last_used` (optional in `Connection`). `POST api/rulesets/revoke/` with `{"group": pk}` returns `{"users", "tokens", "queued"}`; the extra key is ignored by `revokeGroupTokens`. `GET api/connections/?user=<pk>` works for staff admins (403 for others, which the members table shows as an error rather than "n/a"). Errors are DRF's `{"detail": "…"}`, which `errorMessage()` reads.
- **Permissions the pages rely on:** the groups page and the panel need staff + admin.change (the same condition as editing core's roles); the connections page works for any authenticated user. Session auth with CSRF works (`context.api` sends the token).
- **Static files:** InvenTree copies the plugin's whole `inventree_mcp/static/` tree, including `assets/` and `.vite/`, to `STATIC_ROOT/plugins/inventree-mcp/` at plugin load (`plugin.staticfiles.copy_plugin_static_files`). Verified on the dev stack: `/static/plugins/inventree-mcp/Groups.js` → 200, and `assets/McpRolesEditor-*.js` / `assets/useAsync-*.js` are present. `MANIFEST.in` already has `recursive-include inventree_mcp/static *`. Still missing in `core.py`: `UserInterfaceMixin` with the routes and panel from the WP4 frontend report (`plugin_static_file("Groups.js:renderGroupsPage")`, `"Connections.js:renderConnectionsPage"`, `"Panel.js:renderMCPRolesPanel"` for `target_model == "group"`, staff only).
- **Dev defaults:** "MCP Users" has MCP view on every role and `admin` is a member (`dev/configure_inventree.py`), so the pages have data and the smoke scripts pass. `dev/permissions_e2e.py` restores whatever it finds.
