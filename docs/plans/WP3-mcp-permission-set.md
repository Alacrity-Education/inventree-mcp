# WP3: the second permission set (MCP roles per group)

Read first: `docs/plans/00-overview.md`, `docs/plans/WP1-report.md`, `docs/plans/WP2-report.md`, master plan section "WP3", and:
- InvenTree core: `users/models.py` (`RuleSet`, how rows are created for a new group: look for the `post_save` handler on `Group` and `RuleSet.check_table_permission`/`update_fields`), `users/ruleset.py` (`RULESET_CHOICES`, `RULESET_NAMES`, `RULESET_PERMISSIONS`), `users/oauth2_scopes.py` (`get_granular_scope`, `_roles`, `DEFAULT_READ`), `users/serializers.py` (`RuleSetSerializer` fields), `users/api.py` (`RuleSetList`, `RuleSetDetail` and their permission classes), `InvenTree/permissions.py` (`RolePermission`, `InvenTreeTokenMatchesOASRequirements`, `map_scope`), `settings.py` `REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"]`.
- DOT: `models.py` `AbstractAccessToken.is_valid`, `allow_scopes`, `is_expired` (what a token object must provide for the scope check).
- plugin: `auth/policy.py`, `oauth2_bridge.py` (`authentication_classes_for`, `scoped_view_class`), `context.py`, `mcp_transport.py`, `tool_visibility.py`, `proxy.py`.

Decided: MCP permissions are a parallel `MCPRuleSet` table per group with the same shape as core `RuleSet`. They become OAuth2 scopes on MCP tokens. Core enforces interactive roles ∩ token scopes on every proxied call; the plugin never re-implements permission checks. API tokens and Basic auth on the MCP endpoint get the same narrowing through a synthetic scoped token.

## Scope

### Model and data

`MCPRuleSet` as in the overview (`group`, `name` with `RULESET_CHOICES`, four flags, unique per group and name), `label` property like core's. `check_user_permission`: staff. Migration `0002_mcpruleset` plus a `RunPython` step creating a row per existing group and `RULESET_NAMES` entry (all flags false). `auth/signals.py`: `post_save` on `Group` creates the rows for new groups (`ensure_rulesets(group)` helper, also called lazily by the API list view so a missing role added by a core upgrade appears).

### Policy (`auth/policy.py`, replace the stub bodies, keep signatures)

- `effective_scopes(user)`: empty for anonymous/inactive. If `user.is_superuser` and `MCP_SUPERUSER_FULL_ACCESS` → `set(mcp_scopes_available())`. Otherwise union over `MCPRuleSet.objects.filter(group__in=user.groups.all())` of `get_granular_scope(perm, name)` for each true flag (`view/add/change/delete`), intersected with `mcp_scopes_available()` (so `MCP_READ_ONLY` strips write scopes), plus `DEFAULT_READ` (`g:read`) whenever the result is non-empty. One query, cached per request where there is a request (use `functools.lru_cache` keyed on user pk only if you also invalidate on ruleset save; simpler: no cache, it is one query).
- `may_connect(user)`: existing checks (anonymous, inactive, blocked, required groups from WP2) then `no_mcp_permission` when `effective_scopes(user)` is empty.
- `narrow(requested, user)`: unchanged semantics, now meaningful.

### Enforcement for non-OAuth credentials (`mcp_transport.py`)

After authentication, when `successful_authenticator` is not OAuth2 (API token or Basic): build an unsaved `oauth2_provider.models.AccessToken(user=user, scope=" ".join(policy.effective_scopes(user)), expires=<now + 1 day>)` and bind it as the identity's `oauth2_token` so `oauth2_bridge.authentication_classes_for` presents it to proxied views and `InvenTreeTokenMatchesOASRequirements` enforces the scopes (verify that DOT's `TokenMatchesOASRequirements` only calls `is_valid`/`allow_scopes`/`is_expired` on it; add a test). Empty effective scopes → 403 JSON-RPC error "No MCP permission" (not 401: the credential is valid). `BasicAuthentication` is only in the authenticator list when `MCP_ALLOW_BASIC_AUTH` is on (override `get_authenticators`). `MCP_READ_ONLY` stays as is in `proxy.call_view`.

Confirm `tool_visibility.visible_tool_names` now reflects the narrowed set for API-token callers without changes (it goes through the same context); add a test.

### Revocation on reduction (`auth/signals.py`)

`pre_save` on `MCPRuleSet` snapshots the old flags; `post_save` compares: any flag true → false, or `post_delete`, → `membership.revoke_all_mcp_tokens(user)` for every member of the group (offload with `offload_task` when there are more than 20 members; synchronous otherwise). Additions do nothing (next refresh or reconnect picks them up). Log counts only.

### API (`auth/api.py`, DRF, under `api/` in `auth/urls.py`, names from the overview)

Authentication: InvenTree's defaults (session for the web UI, API token). Permissions: rulesets and effective-of-others require staff with the `admin` role change permission (mirror the permission classes core uses on `RuleSetList`/`RuleSetDetail`); connections and status are for the requesting user, staff may pass `?user=<pk>`.

- `GET api/rulesets/?group=<pk>` → list in core's `RuleSetSerializer` shape (`pk`, `name`, `label`, `group`, `can_view`, `can_add`, `can_change`, `can_delete`), sorted by label, rows ensured. `PATCH api/rulesets/<pk>/` → flags only (name/group read-only). `POST api/rulesets/copy/` with `{"group": <pk>}` copies the group's interactive `RuleSet` flags into its `MCPRuleSet` rows (the "Copy from roles" button); `POST api/rulesets/clear/` with `{"group": <pk>}` clears them.
- `GET api/effective/<user_pk>/` → `{"user": pk, "username", "superuser", "roles": {name: {view,add,change,delete}} (interactive, from users.permissions / RuleSet of the user's groups), "mcp_roles": {...} (union of MCPRuleSet), "effective": {...} (interactive AND mcp, what MCP can actually do), "scopes": [...], "may_connect": bool, "reason": str}`.
- `GET api/connections/` → the caller's unrevoked façade refresh tokens: `{pk, client_name, client_host, client_trusted, created, expires (refresh), access_expires, scopes, last_used}` (DOT has no last-used; omit if unavailable and say so in the report). `DELETE api/connections/<pk>/` revokes that refresh token and its access token (DOT's `RefreshToken.revoke()`).
- `GET api/status/` → `{"oauth_enabled", "mcp_url", "membership": {"checked_at", "blocked", "reason"}, "scopes", "may_connect", "reason"}`.
- Register the serializers so InvenTree's schema generation does not choke (`extend_schema` where needed, or exclude the plugin views from the schema if core's spectacular settings require it; check how other plugin API views do it).

### Settings and docs

`MCP_SUPERUSER_FULL_ACCESS` (bool, False), `MCP_ALLOW_BASIC_AUTH` (bool, False). `README.md`: "Two permission sets" section with the rule "MCP access = interactive roles ∩ MCP roles", the API, and the scope mapping table. `AGENTS.md` file map.

## Tests (`tests/test_policy.py`, `tests/test_api.py`, additions to `test_auth_token.py` and `test_auth_flow.py`)

1. `effective_scopes`: no rulesets → empty; view on `part` → `{"g:read", "r:view:part"}`; add/change/delete present only when `MCP_READ_ONLY` is off; union across groups; superuser with and without `MCP_SUPERUSER_FULL_ACCESS`; inactive user → empty.
2. `may_connect` → `no_mcp_permission` when empty; authorize renders the 403 page; token refresh for a user whose group lost all MCP flags → `invalid_grant` and the refresh token revoked.
3. Flow: user with interactive `part` view+change and MCP `part` view only → code exchange grants `g:read r:view:part`; `tools/list` shows `list_parts` and not stock tools; a tool call that needs `r:view:stock` fails with the proxy's permission error; interactive API with the same user's API token still allows `PATCH /api/part/<pk>/` (interactive roles untouched).
4. API-token call on `/mcp`: same narrowing; `tools/list` identical to the OAuth case; empty effective → 403; Basic auth rejected unless `MCP_ALLOW_BASIC_AUTH`.
5. Reduction revokes: clearing `can_view` on a group revokes members' façade tokens, not other users', not API tokens; adding a flag revokes nothing.
6. API: permissions (non-staff 403 on rulesets, self-only on connections), PATCH flags, copy/clear, effective document, connections list and revoke, status; migration creates rows for existing groups; new group gets rows.

## Acceptance

All tests green via `dev/test.sh inventree_mcp`; in the dev environment, give `MCP Users` MCP view on parts only, connect through the façade (reuse `dev/oauth_smoke.py` with `e2e-member`), and show `tools/list` containing part tools only while `e2e-member`'s interactive session can still see stock; report `docs/plans/WP3-report.md`; one commit `WP3: MCP permission set`.
