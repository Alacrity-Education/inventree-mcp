# WP3 review: MCP permission set per group

Reviewed: `git diff c9e623c..HEAD` (commit `af49ee3 WP3: MCP permission set per group`), branch `feature/oauth-facade`. Reference: InvenTree core master (1.6.0 dev) and the dev stack (InvenTree 1.5.7, DOT 3.4.0). Read-only review. The only file written is this one.

## Verdict

**Accept with fixes.**

The spec is implemented with these properties:

- The binding interfaces are untouched.
- Revocation never blocks.
- The reverse-relation rule is respected.
- The API matches the built frontend.
- The test suite, the WP3 e2e and the OAuth smoke test pass.

Two items must be fixed before WP5 starts:

- **F1 (major, WP3 scope).** OAuth tokens that InvenTree's own `/o/` endpoints issued are not narrowed on `/mcp`. Any non-façade application can bypass the MCP roles.
- **F2 (major, pre-existing, decision 2).** Every paginated list tool fails on a real instance. No e2e has ever completed a list tool call.

Everything else is minor.

## Findings

### F1 (major). OAuth tokens of non-façade applications bypass the MCP roles on `/mcp`

`inventree_mcp/mcp_transport.py:346-347` binds the presented OAuth token as is (`oauth2_token = drf_request.auth`). Only API-token and Basic requests get `scoped_token_for()` (`:348-352`).

Narrowing to the MCP roles therefore happens only where the façade issues tokens: at authorize and refresh, through `policy.narrow`.

Core serves its own DOT endpoints at `/o/`:

- `InvenTree/urls.py:140`: `flagged_path('OIDC', 'o/', …)`.
- `settings.py:1174`: the flag defaults to `True`.

`ExtendedOAuth2Authentication` (`mcp_transport.py:278`) accepts tokens of any DOT application. So a token from an admin-registered application can carry, for example, `r:view:stock` or `r:change:part`. On `/mcp` that token gets interactive roles ∩ *its own* scopes, not ∩ MCP roles. `may_connect` only requires that some MCP role exists.

Such tokens are also never revoked on a reduction. `revoke_all_mcp_tokens` only covers `mcp_applications()`.

Today the dev DB has only façade applications (38 of 38), which is why the e2e cannot show the gap. It exists on any instance that uses core's OAuth for other clients.

**Fix (one place, also closes F3).** In the transport, intersect every OAuth token with the user's current MCP scopes:

```python
if is_oauth2:
    allowed = set(policy.narrow(None, user))
    scope = " ".join(s for s in drf_request.auth.scope.split() if s in allowed)
    oauth2_token = get_access_token_model()(user=user, scope=scope, expires=drf_request.auth.expires)
```

Apply the 403 "No MCP permission" rule when `scope` is empty. Alternatively, refuse with 401 `invalid_token` any token whose `application_id` is not in `mcp_applications()`.

The intersection is preferable for two reasons:

- It also makes reductions and group-membership removals take effect at the next request, without depending on revocation.
- `effective_scopes` is already computed in `may_connect` on the same request.

Add a test with a token of a `registration_source="admin"` application that carries a stock scope while the MCP roles grant only parts.

### F2 (major, pre-existing; decision 2). Proxied requests use host `testserver`, so paginated tool calls fail on real instances

**Where.** `inventree_mcp/proxy.py:26` (`_factory = APIRequestFactory()`) and `:86`, `:88`. The factory requests carry `SERVER_NAME=testserver`.

DRF's `LimitOffsetPagination` builds `next` and `previous` with `request.build_absolute_uri()` → `get_host()` → `DisallowedHost`. InvenTree derives `ALLOWED_HOSTS` from `SITE_URL`. The test runner allows `testserver`, so the suites never see the failure. The precise trigger is pagination, not serializers in general:

- Every tool that passes `limit` fails.
- Detail tools work.

Reproduced on the dev stack. The API token is admin's, from `GET /api/user/token/`, sent to `/plugin/inventree-mcp/mcp/`:

```
tools/call list_parts {"limit":1}
{"jsonrpc":"2.0","id":1,"result":{"content":[{"text":"Error executing tool list_parts: Invalid HTTP_HOST header: 'testserver'. You may need to add 'testserver' to ALLOWED_HOSTS.","type":"text"}],"isError":true}}
tools/call list_locations / list_companies / list_build_orders {"limit":1}   -> same error
tools/call get_part {"part_id":1}   -> success (part JSON)
```

`dev/shell.sh` with a bare `APIRequestFactory` and `force_authenticate` (no WP3 code involved):

```
PartList GET /api/part/?limit=1  -> 500  (DisallowedHost logged)
PartList GET /api/part/          -> 200
```

**Side effect.** Each failure goes through InvenTree's error handler. That writes an Error Log entry and sends the `inventree.error_log` notification to admins; the shell log shows `Gathering users for notification 'inventree.error_log'`. On a production instance, every MCP list call therefore spams the admins.

My reproduction added a few such entries to the dev DB.

**Fix.** See "Decisions assessment", decision 2.

### F3 (minor, declared limitation 2). Removing a user from a group revokes nothing

OAuth access tokens keep their scopes until refresh, which can take up to `ACCESS_TOKEN_LIFETIME_SECONDS` (1 h). API tokens follow at once. The spec does not require more, and the README states it.

The F1 fix (intersecting at the transport) closes this at the next request at no extra cost. No `m2m_changed` receiver is needed.

### F4 (minor). The effective document and README describe a per-role intersection, while access is per table

With decision 1, access to a table T is granted when both of these hold:

- some role containing T is granted interactively;
- some role containing T is granted as an MCP scope.

The two roles may differ.

**Example.** Interactive *part view* only, MCP *build view* only. `list_parts` is allowed:

- the scope `r:view:build` satisfies the split alternative;
- `RolePermission` accepts the interactive *part* role.

Yet `effective_document` (`auth/api.py:409-413`) shows *part* and *build* as false.

The README's "MCP access = interactive roles ∩ MCP roles" (README "Two permission sets") is therefore not literally true for multi-role tables. The README's last bullet only covers the case where the two sets agree.

This is not a widening beyond interactive access, but the admin page under-reports.

**Fix.** Do both:

- Reword the README bullet: "for a table shared by several roles, any interactive role and any MCP role that cover the table suffice".
- Add a note in the effective document, or compute `effective` per table group.

### F5 (minor). `test_mcp_token_cannot_change_mcp_permissions` does not test what it claims

**Where.** `tests/test_api.py:181-194`.

InvenTree's `AuthRequiredMiddleware` (`InvenTree/middleware.py:187-210`) runs before `OAuth2TokenMiddleware` (`settings.py:387-389`). It recognises only sessions and API tokens, so a Bearer request to `/plugin/inventree-mcp/api/…` gets the middleware's 401 before DRF runs. The `a:staff` requirement (`auth/api.py:64`, `:262`, `:297`, `:305`) is unreachable defence in depth. The test passes on the 401.

The README sentence "An OAuth token needs the `a:staff` scope for the staff endpoints" suggests OAuth tokens can reach the API at all.

**Fix.**

- Assert `401` and say why in the test.
- Add one unit test that calls the view directly (an `APIRequestFactory` request with `authentication_classes_for(user, token)`) to cover the scope check.
- README: "OAuth tokens are refused on the plugin API by InvenTree's middleware; the views additionally require `a:staff`".

### F6 (minor). Duplicated token queries and revoke loop

- `auth/signals.py:133-146` (`live_mcp_token_count`) rebuilds the two querysets of `membership.revoke_all_mcp_tokens` (`membership.py:832-843`).
- `signals.revoke_users`'s synchronous branch (`signals.py:182-185`) is the body of `tasks.revoke_mcp_tokens` (`tasks.py:96-111`).

**Fix.**

- Expose `membership.mcp_token_querysets(user_ids)` and use it in both places.
- Let `revoke_users` call `tasks.revoke_mcp_tokens(user_ids)` inline.

`live_mcp_token_count` also counts expired access tokens as "live". This is a nit, and it disappears with the shared helper if that helper filters `expires__gt=now`.

### F7 (minor). Repeated queries on every MCP request

`mcp_transport.dispatch` calls `may_connect(user)` (`:324`), then `scoped_token_for(user)` (`:350`). Together they run:

- `in_required_groups` three times (`policy.py:183`, plus twice through `effective_scopes` at `:77`);
- the ruleset SELECT twice.

**Fix.** Compute `effective_scopes` once in the transport. For example, return the scopes from a private helper that `may_connect` also uses, or bind the scoped token first and derive the 403 from an empty scope. Keep `may_connect`'s signature.

### F8 (minor). `GET api/rulesets/` without `?group` writes, and runs two queries per group

**Where.** `auth/api.py:281-284`. `ensure_rulesets` runs for every group on a GET.

This is acceptable for the spec's lazy ensure, but it is O(groups) queries and writes on a read.

**Fix.** One `bulk_create(ignore_conflicts=True)` over the cross product of groups and the missing names. Alternatively, run the ensure only with `?group`; the frontend always passes `?group`.

### F9 (nit). Migration role choices as plain strings

`migrations/0002_mcpruleset.py:37` is sound:

- Choices do not affect the schema.
- `RuleSetEnum` is a `StringEnum`, so values compare equal.
- `dev/manage.sh makemigrations inventree_mcp --check --dry-run` reports "No changes detected in app 'inventree_mcp'".
- It avoids importing enum members into a historical migration.

The model still uses `choices=RULESET_CHOICES` (`models.py:108`). A core upgrade that adds a role will therefore make Django report a pending `AlterField` for `inventree_mcp`. That is harmless (no SQL), but noisy.

**Optional.** Drop `choices` from the field and validate in the serializer. The serializer already gets `name` as read-only.

The `RunPython` step imports the live `users.ruleset.RULESET_NAMES` rather than a frozen list. That is acceptable: `ensure_rulesets` covers later additions.

### F10 (nit). The WP3 e2e accepts a failing `list_parts`

`dev/permissions_e2e.py:232-243` asserts only that the error is not a permission error. Since F2 is fixed in the next round, change the expectation to `not isError` and assert at least one result.

### Spec compliance (question 1)

**Model.**

- `models.py:82-153`. Field names, `related_name="mcp_rule_sets"`, CASCADE, `unique_together` (`:129`), `label` (`:137-140`) and `check_user_permission` (staff, `:150-153`) all match.
- `save()` (`:142-148`) mirrors core's flag implication. This is deviation 9, declared and sensible.

**Migration.**

- `0002_mcpruleset.py`: `CreateModel` plus `RunPython` (`:9-22`, `bulk_create(ignore_conflicts=True)`) for existing groups, with dependencies `auth.0012` and `inventree_mcp.0001`.
- Tested by calling `create_rows_for_existing_groups` directly (`test_api.py:202-211`).

**Group `post_save`.**

- `signals.py:203-220`, for `created` only. This is deviation 6: declared, justified by the lazy ensure.
- It has a savepoint and a `DatabaseError` guard.

**Lazy ensure.** `api.py:281-284` (list) and `:315` (copy/clear).

**Policy (`auth/policy.py`).**

| Spec item | Where |
|---|---|
| `effective_scopes`: empty for anonymous/inactive | `:75-76` |
| `effective_scopes`: empty outside required groups | `:77-78` |
| `effective_scopes`: superuser with the setting | `:81-82` |
| `effective_scopes`: union ∩ available, so `MCP_READ_ONLY` strips writes | `:84` |
| `effective_scopes`: `g:read` when non-empty | `:85-86` |
| One SELECT through `group__in` (no reverse relation), savepoint-guarded | `:144-151` |
| `may_connect` order: anonymous, inactive, blocked, required groups, empty scopes; reason set unchanged | `:177-187` |
| `narrow` unchanged | `:190-206` |

**Non-OAuth narrowing.**

- `scoped_token_for` (`mcp_transport.py:251-262`) builds an unsaved `AccessToken(user, scope, expires)`.
- What DOT touches on it: `TokenMatchesOASRequirements.has_permission` (`contrib/rest_framework/permissions.py:146-168`) uses `bool(token)`, `hasattr(token, "scope")` and `token.is_valid(alt)`. `AbstractAccessToken.is_valid` (`models.py:547-553`) calls `is_expired()`, which reads `expires` (`:555-562`), and `allow_scopes()`, which reads `scope` (`:591-603`).
- Core's `is_oauth2ed` (`InvenTree/permissions.py:113-119`) reads only `request.user` and `request.successful_authenticator`.
- `_PreResolvedOAuth2Authentication.authenticate` returns the pair without touching the token (`oauth2_bridge.py:77-84`).
- Nothing in that path calls `save()`. `application` is `None` and `token` is empty, so the object is not a usable credential even if it leaked.
- Tested: `test_policy.py:295-302` (unsaved, no row) and `:304-328` (probe).
- `tool_visibility` follows without changes, tested at `test_policy.py:338-354`.
- Empty scope → 403 (`mcp_transport.py:351-352`). `may_connect` normally refuses first.

**Basic auth gating.** `get_authenticators` (`mcp_transport.py:282-289`), tested at `test_policy.py:282-293`. When Basic is refused it falls to the `REQUIRE_AUTH` 401.

**Revocation.**

- `pre_save` snapshot `signals.py:255-261`, `post_save` comparison `:264-271`, row `post_delete` `:274-283`.
- Group `pre_delete` `:223-246`: the members are still known; deviation 7, declared and justified.
- Only `revoke_all_mcp_tokens` is called, never `block_user`.
- Offload above `REVOKE_SYNC_LIMIT = 20` (`:39`, `:166-181`).
- Logs carry counts and group id only (asserted at `test_policy.py:395-400`).
- Group membership removal: nothing (F3).

**API.**

- Every endpoint from the overview, plus copy, clear and revoke (`auth/urls.py:68-85`).
- Shapes are as the spec says. `label` comes from core's `RuleSetSerializer` (subclassed, `api.py:123-129`) and in every role map (`:380-386`).
- `?user=` on connections and status (`:99-117`, `:460-463`, `:516-518`).
- Revoke returns `{"users","tokens","queued"}` (`:359-374`); deviation 3, declared.
- Permissions: `IsStaffAdmin` (`:71-89`) is core's write condition for `RuleSet`, applied to reads too; deviation 2, declared and consistent with the staff-only GUI.
- `last_used` is omitted; deviation 4, declared (DOT records none).

**Settings.** `core.py` adds `MCP_SUPERUSER_FULL_ACCESS` and `MCP_ALLOW_BASIC_AUTH` (bool, False).

**Docs.** README "Two permission sets", API table, scope table, settings and AGENTS.md file map are all present (see F4 and F5 for two inaccurate sentences).

**Tests 1-6.** All present, mapped in the report. Gaps: F1 (no non-façade-token test), F5.

**Deviations.** All ten listed are declared. 1-9 are justified. 10 (dev defaults: "MCP Users" gets MCP view on every role, admin added to it) is justified for the smoke scripts.

Undeclared: none found, beyond the README imprecision (F4).

### Security (question 4)

- **Non-staff and rulesets.** Non-staff users cannot read or change MCP rulesets. Every ruleset endpoint has `STAFF_PERMISSIONS`, and `test_api.py:51-76` covers non-staff, staff without `admin.change`, and anonymous.
- **Other users' documents and connections.**
  - `target_user` (`api.py:99-117`) raises 403 before any lookup for a pk other than the caller's, so pk existence never leaks to non-staff.
  - `ConnectionDetail._token` (`:473-487`) answers 404 for someone else's token.
- **Revoke endpoint.** Staff admin only. `GroupActionSerializer`'s `PrimaryKeyRelatedField` validation runs after `check_permissions`. Live check: an unknown group gives 400 "Invalid pk" only to a staff admin.
- **Sensitive data.**
  - No token values are returned.
  - `username` is returned only to the user or to a staff admin.
  - No e-mail addresses.
  - `client_name` is DCR-supplied text; React escapes it.
- **Middleware.** The API views are not `auth_exempt`. Live: anonymous `GET` on rulesets, status, connections and effective → `401 Unauthorized` from InvenTree's middleware. Session and API token pass; Bearer and Basic are refused by the middleware (F5).
- **CSRF.** Live, admin session through allauth headless:

  ```
  session POST without CSRF token: 403 {"detail":"CSRF Failed: CSRF token missing."}
  session POST with CSRF token:    400 {"group":["This field is required."]}
  ```

- **Synthetic token per request.**
  - Built inside `dispatch` for each request (`mcp_transport.py:350`) and bound through a `ContextVar` (`:354`), reset in `finally` (`:367`).
  - `_Identity` is frozen (`context.py:20-29`).
  - A new `StreamableHTTPSessionManager` is created per request (`:122`), so no task group outlives a request or inherits another request's context.
  - Not shared.
- **Open hole: F1.**

## Decisions assessment

### Decision 1: any-one-role scope rule (`oauth2_bridge._any_role_alternatives`)

**Scope of effect.**

- The function (`oauth2_bridge.py:109-127`) is called only from `scoped_view_class` (`:138-140`).
- `scoped_view_class` is used only by `proxy._call_view_sync` and `proxy._user_has_access_sync`, on the OAuth branch (the real façade token or the scoped token).
- InvenTree's own `/api/` never sees it.
- The plugin API sets static `required_alternate_scopes` (`api.py:64-65`) and is unaffected.

**No widening beyond interactive roles.**

- `_fixed_permission_classes` (`oauth2_bridge.py:97-106`) keeps every permission class of the view and swaps only `InvenTreeTokenMatchesOASRequirements`.
- `RolePermission.has_permission` (`InvenTree/permissions.py:206-243`) still runs `check_user_permission(user, model, permission)` on the interactive roles.
- The split only relaxes the scope side. Access ⊆ interactive access always holds.

**Core semantics.**

- `users/permissions.py:170-172` iterates the roles of the table and returns True on the first role the user holds (any-role).
- The all-roles requirement comes from `InvenTree/permissions.py:67-72`:

  ```python
  if roles:
      return [[get_granular_scope(action, table) for table in roles]]
  ```

  This is one alternative containing every role's scope. `get_required_alternate_scopes` (`:131-139`) feeds it `precalculated_roles[table]` (`:86-92`), which lists every role containing the table: `part_part` → part and build; `stock_stockitem` → stock and build; `company_company` → purchase/sales/return order.
- The split `[[s] for s in scopes]` reproduces the any-role rule.

**Core bug?** Yes, and worth an upstream issue.

- The OAuth path is stricter than the role path for the same request.
- A token issued with exactly the scopes a role grants cannot use that role's tables.
- The parameter is even named `table` while it holds role names.
- Proposed core fix: `return [[get_granular_scope(action, role)] for role in roles]`.

Add it to the prepared upstream patches next to bugs 1 and 2. Then the workaround can go away with them.

**Security downside.**

- A scope for any role covering a table opens that table, provided the user has some interactive role on it. So MCP *build view* exposes parts, part categories and stock items to MCP, and MCP *purchase order view* exposes companies. That is the same breadth an interactive *build* role gives.
- The cost is that the per-role mental model (and `effective_document`) under-reports (F4).
- Writes follow the same rule. With `MCP_READ_ONLY` on (the default), writes are blocked anyway.
- No downside beyond that. **Confirm**, with F4's doc fix.

### Decision 2: proxied requests built with host `testserver`

**Reproduced.** See F2.

**Is this why no e2e called a tool successfully?** Yes.

- `oauth_smoke.py` and `membership_e2e.py` only run `tools/list`. `user_has_access` runs `initial()` only, so no pagination is involved.
- `permissions_e2e.py` is the first to run `tools/call`, and it accepts the failing `list_parts` (F10).
- Detail tools (e.g. `get_part`) work, but no e2e calls one.

**Location.** `proxy.py:26` (module-level `APIRequestFactory()`), `:83-88` (`factory_method(path, data=…)`) and `:138` (`_factory.generic(method, "/")` for the visibility check, where the host does not matter today).

**Minimal fix.** Carry the originating request's resolved host and scheme in the bound identity and pass them to the factory:

1. `context._Identity` gains `request_meta: Mapping[str, str] = {}`. `set_current_user(user, oauth2_token, request_meta=None)` stays backward compatible.
2. In `mcp_transport.dispatch`, build `request_meta` before `set_current_user`:
   - `{"HTTP_HOST": request.get_host(), "wsgi.url_scheme": "https" if request.is_secure() else "http", "REMOTE_ADDR": request.META.get("REMOTE_ADDR", "127.0.0.1")}`;
   - plus `HTTP_ACCEPT_LANGUAGE` when present.

   `get_host()` has already validated the host against `ALLOWED_HOSTS` and applied `USE_X_FORWARDED_HOST`. `is_secure()` has applied `SECURE_PROXY_SSL_HEADER`. Pass the *resolved* values, not the raw `X-Forwarded-*` headers: forwarding those as well would make the inner request re-derive them, and an internal request should not depend on proxy settings twice.
3. In `proxy._call_view_sync`: `factory_method(path, data=…, **get_current_request_meta())`. `APIRequestFactory.generic` merges `**extra` into the WSGI environ, so `HTTP_HOST` and `wsgi.url_scheme` override `testserver` and http. Use the same in `_user_has_access_sync` for consistency.

Setting `SERVER_NAME` alone is not enough: `get_host()` prefers `HTTP_HOST`, which the factory sets to `testserver`.

**Other headers.**

- **`REMOTE_ADDR`: yes.**
  - DRF throttles key anonymous and some scoped throttles on it.
  - InvenTree's request logging and error log record it.
  - Today every proxied call looks like `127.0.0.1`.
- **`Accept-Language`: optional.**
  - The proxied view runs on the same thread under `thread_sensitive=True`, after the outer `LocaleMiddleware` activated the language. Translated labels are therefore already correct.
  - Forwarding the header costs nothing and keeps `request.LANGUAGE_CODE`-style reads consistent.
- **Not to propagate:**
  - `Authorization` and cookies: the bridge supplies authentication.
  - CSRF headers.

**Test.** Add a regression test with `@override_settings(ALLOWED_HOSTS=["mcp.example"])` that posts to `/mcp` with `HTTP_HOST="mcp.example"`, calls `list_parts {"limit": 1}`, and asserts no error and `next` starting with `http://mcp.example/`.

**Must WP5's e2e include a successful tool call?** Yes, and it must include both:

- a paginated list call, e.g. `list_parts {"limit":1}` with `isError` false and a result row;
- a detail call.

Do this for an OAuth token and for an API token, on the dev stack behind the Caddy proxy (`X-Forwarded-*` in play). It is the only check that exercises the real host and scheme path.

## Duplication

- **Reused from core:**
  - `RuleSetSerializer` (subclassed, `api.py:123-129`);
  - `get_user_roles` (`:392`), `check_user_role` (`:88`), `map_scope` and `DEFAULT_STAFF` (`:64-65`);
  - `RULESET_CHOICES`, `RULESET_NAMES`, `get_granular_scope`;
  - `ListAPI` and `RetrieveUpdateAPI`;
  - `offload_task`.
- **Reused from DOT:**
  - `RefreshToken.revoke()` (`api.py:502`, via `revoke_all_mcp_tokens`);
  - `get_refresh_token_model()`, `get_access_token_model()`.
- **Reused from the plugin:** the fixed scope check `_FixedTokenMatchesOASRequirements` and `clients.mcp_applications()` / `is_trusted_client()`.
- **Duplicated:**
  - `live_mcp_token_count` vs `revoke_all_mcp_tokens` querysets, and `revoke_users` vs `tasks.revoke_mcp_tokens` (F6).
  - `signals._FLAGS`, `MCPRuleSet.RULE_OPTIONS` and `policy.FLAG_PERMISSIONS` are three spellings of the same four names. Use `MCPRuleSet.RULE_OPTIONS` in `signals.py` (nit).
  - `policy.py`, `api.py` and `signals.py` share no query logic otherwise:
    - the MCP rows are read only by `policy._ruleset_rows`;
    - `api.effective_document` uses `policy.mcp_roles` and `policy.narrow`;
    - `signals.grants_anything` only reads flags.
- **Reverse-relation caveat.** `grep -rn "mcp_rule_sets\|mcp_state__\|\.groups\.filter("` over `inventree_mcp` and `dev/*.py` finds only:
  - `related_name` declarations and docstrings;
  - `policy.py:166`: `user.groups.filter(match)` with `Q(name__iexact=…)`. That is a core relation, not a plugin one.
  - `tests/test_membership.py:220`: `name=` only.
  - Plugin-side forward lookups: `MCPRuleSet.objects.filter(group__in=…)` / `group__name=`.
  - The e2e's step C confirms the endpoints answer 200 in the server process.
- **`dev/permissions_e2e.py`** imports `connect`, `create_api_token`, `delete_api_tokens`, `state` and `USER` from `membership_e2e`; `JSON`, `expect`, `mcp_tools_list`, `password_login`, `refresh` and `step` from `oauth_smoke`; and `Browser` from `sso_smoke`. Nothing is copied.

## Test and e2e results

Full suite (`dev/test.sh inventree_mcp`, dev stack InvenTree 1.5.7, DOT 3.4.0):

```
Ran 316 tests in 306.840s
OK (skipped=3)            (the three WellKnownRedirectTest skips, as in WP1/WP2)
EXIT 0
```

`python3 dev/permissions_e2e.py`:

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

`python3 dev/oauth_smoke.py --sso e2e-member` (tail):

```
[8] approved, code exchanged; token_type=Bearer expires_in=3600 access_token=<30 chars> refresh_token=<30 chars> scopes=12
[9] tools/list -> 200; 56 tools, e.g. ['describe_filters', 'make_web_link', 'list_attachments', 'get_attachment']
[10] refresh -> 200; expires_in=3600 refresh token rotated: True
[12] tools/list with the pre-refresh access token -> 401 (revoked by rotation)
[14] refresh with the just-revoked refresh token -> 400 invalid_grant
OK: OAuth façade flow completed (SSO as e2e-member)
```

Lint and migrations:

```
uvx ruff@0.16.0 check --preview inventree_mcp              -> All checks passed!
uvx ruff@0.16.0 format --preview --check inventree_mcp     -> 52 files already formatted
uvx ruff@0.16.0 check --isolated dev/permissions_e2e.py dev/configure_inventree.py -> All checks passed!
uvx ruff@0.16.0 format --isolated --check dev/permissions_e2e.py -> 1 file already formatted
dev/manage.sh makemigrations inventree_mcp --check --dry-run -> No changes detected in app 'inventree_mcp'
```

Schema, with plugins enabled, in the server container: `INVENTREE_ALLOWED_HOSTS='*' manage.py schema --validate --fail-on-warn --file /tmp/wp3-schema.yml` → `rc=0`. The schema contains the nine paths:

- `/plugin/inventree-mcp/api/connections/`
- `/plugin/inventree-mcp/api/connections/{id}/`
- `/plugin/inventree-mcp/api/effective/{user_pk}/`
- `/plugin/inventree-mcp/api/rulesets/`
- `/plugin/inventree-mcp/api/rulesets/{id}/`
- `/plugin/inventree-mcp/api/rulesets/clear/`
- `/plugin/inventree-mcp/api/rulesets/copy/`
- `/plugin/inventree-mcp/api/rulesets/revoke/`
- `/plugin/inventree-mcp/api/status/`

The report's claim is confirmed. The `ALLOWED_HOSTS` override is needed for core's own views, which is the same `testserver` effect as F2.

### Test quality (question 6)

**API token vs OAuth `tools/list`.**

- `test_policy.py:246-273` compares the two tool-name sets for equality.
- It then calls a stock tool, expecting a refusal, and `list_parts`, expecting success; that passes in tests only because the runner allows `testserver`.
- It then checks `/api/stock/` with the same API token, expecting 200.
- The e2e repeats the equality live (B: "identical to the OAuth list: True").

**Interactive API untouched.**

- `test_auth_flow.py:252-262` PATCHes `/api/part/<pk>/` and reads `/api/stock/` with the same user's API token.
- e2e B and D show `/api/stock/` 200 both before and after the MCP role is cleared.

**Revocation targets only the group's members.**

- `ReductionRevokesTest.assert_member_revoked` (`test_policy.py:376-387`) checks on every case that:
  - the other group's member keeps both tokens;
  - the API token is never revoked.
- `test_api.py:163-179` checks the same for the revoke endpoint.

**Tautology.**

- `test_dot_permission_needs_only_is_valid_on_the_token` (`test_policy.py:304-328`) exercises DOT's class against a probe. It documents the contract the spec asked for, but it tests the library, not plugin code.
- The meaningful counterpart is the end-to-end use of the real unsaved token in `test_api_token_sees_what_an_oauth_token_sees`.
- Suggested addition: assert `AccessToken.objects.filter(user=…).count()` is unchanged after a full `/mcp` tool call with an API token. That shows nothing persists through the whole proxy path.

**Mocks.**

- Only `InvenTree.tasks.offload_task` is patched (`test_policy.py:428`). The task body is then run for real (`:436-439`). This is appropriate.
- The real gap is environmental: every test runs with `testserver` allowed, which hides F2. Add the regression test from decision 2.

**Missing tests.**

- F1: a non-façade OAuth token on `/mcp`.
- F5: the `a:staff` check reached for real.

## Compatibility with WP4 (question 7)

**URL names and paths.** These match `frontend/src/lib/api.ts`:

- `api/rulesets/?group=`, `api/rulesets/<pk>/` (PATCH with the four flags);
- `api/rulesets/copy|clear|revoke/` with `{group}`;
- `api/effective/<pk>/`;
- `api/connections/[?user=]`, `api/connections/<pk>/` (DELETE);
- `api/status/`.

**Shapes.**

- `RuleSet` has `pk, group, name, label, can_*`. This is an exact match.
- `Effective` role maps carry an extra `label`. TypeScript's `RoleFlags` ignores it; the frontend can drop `humanize()` in favour of it.
- `Connection` has `client_host` and `access_expires` present and `last_used` absent. All are optional in TypeScript.
- `RevokeResult` has an extra `queued`, which is ignored.
- Lists are plain arrays, which `asList` handles.
- Errors are `{"detail": …}`, which `errorMessage` reads.
- `GET api/connections/?user=` returns 403 for non-admins, which the members table renders as an error. The page itself is staff-only, so this is not reachable in practice.

**Implication on PATCH.** `patchMcpRuleSet` sends all four flags. `MCPRuleSet.save()` applies core's implication: a write flag sets view, and add/delete set change. The returned row can therefore differ from what was sent. Core's RoleTable behaves the same, so this is no change.

**Nothing differs** from what the frontend assumes.

## Next-stage readiness

### WP4 backend wiring must

- Add `UserInterfaceMixin` in `core.py`:
  - routes `groups` (staff) → `Groups.js:renderGroupsPage`;
  - route `connections` → `Connections.js:renderConnectionsPage`;
  - the group panel (`target_model == "group"`, staff) → `Panel.js:renderMCPRolesPanel`.
- Rely on session + CSRF, which is verified live above.
- Optionally switch the frontend role labels to the API's `label`.

No API change is needed.

### Before or as part of WP5

1. Fix F1: intersect OAuth tokens with `policy.narrow(None, user)` in the transport, or refuse non-façade applications. Add the test.
2. Fix F2: propagate host, scheme and `REMOTE_ADDR` (optionally `Accept-Language`) from the MCP request into the factory requests. Add the `ALLOWED_HOSTS` regression test.
3. The e2e must complete real tool calls behind the proxy:
   - a paginated list and a detail call;
   - with an OAuth token and with an API token;
   - asserting `isError` false.

   Change `permissions_e2e.py:241-243` accordingly (F10).
4. File the upstream issue or patch for `map_scope` (decision 1) next to the prepared bug 1 and 2 patches.
5. Fix F4 and F5 (wording and tests).
6. Optional cleanups: F6, F7, F8 and F9.
7. CI: keep `INVENTREE_SOCIAL_BACKENDS=openid_connect` (WP2 review). The new suites need no extra environment.
