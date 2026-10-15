# Implementation overview: OAuth-connected InvenTree MCP with a separate MCP permission set

Master plan: `/home/alex.lucaci/Projects/inventree/PLAN-inventree-mcp-oauth.md` (read it first). This file fixes the module boundaries, names and interfaces that every work package (WP) must respect, so that WPs can be implemented and reviewed independently.

## Repositories and reference material

| Path | What |
|---|---|
| `/home/alex.lucaci/Projects/inventree/inventree-mcp` | this plugin (upstream clone, branch work happens here) |
| `/home/alex.lucaci/Projects/inventree/InvenTree` | InvenTree core source, master (1.6.0 dev). Stable images are 1.5.x; both pin django-oauth-toolkit (DOT) 3.4.x |
| `/home/alex.lucaci/Projects/inventree/vendor/django-oauth-toolkit-3.4.0` and `-3.4.1` | DOT source as installed in InvenTree 1.5.x / master |
| `/home/alex.lucaci/Projects/inventree/vendor/django-allauth-65.19.6` | allauth source (SSO login, `SocialToken`) |
| `/home/alex.lucaci/Projects/inventree/KINA/docs/MCP-AUTH-GUIDE.md` | the behaviour we are reproducing (authoritative for policy and timings) |
| `/home/alex.lucaci/Projects/inventree/KINA/scripts/e2e/authentik` | reusable Authentik bootstrap and flow-driving code |
| `/home/alex.lucaci/Projects/inventree/demo-dataset` | demo data for the dev instance |

## Hard rules for every WP

1. **Reuse, do not duplicate.** OAuth state lives in DOT models (`Application`, `Grant`, `AccessToken`, `RefreshToken`); PKCE, code issuance, token rotation and client authentication are DOT/oauthlib. Group sync is core's `InvenTree.sso.ensure_sso_groups`. Permission enforcement is core's permission classes. If you find yourself re-implementing one of these, stop and subclass or call instead. Reviewers reject duplicated logic, including copy-pasted DOT view bodies longer than a few lines.
2. **Tool code never touches the ORM** (existing rule in `AGENTS.md`). Auth code may use DOT, allauth and plugin models.
3. **Never log or return tokens, keys, e-mail addresses or names of refused users.** Log user id / provider subject, issuer, reason.
4. **Every façade view is `auth_exempt`** (`InvenTree.permissions.auth_exempt`) because `AuthRequiredMiddleware` 401s anything under `/plugin/` otherwise, and **`csrf_exempt`** where a machine client posts (register, token, revoke). The authorize view checks the session itself.
5. **Settings are plugin settings** (`SettingsMixin`), read through `inventree_mcp/settings.py:get_plugin_setting(key, default)` which must coerce to `type(default)` and fail safe to `default`.
6. **Tests run inside InvenTree** via `dev/test.sh` (see WP-dev-environment). `ruff check --preview inventree_mcp` and `ruff format --preview inventree_mcp` must be clean. Existing `inventree_mcp/test_mcp.py` must keep passing.
7. **Feature detection, not version checks**: the OAuth façade requires DOT ≥ 3.4 (`oauth2_provider.views.DynamicClientRegistrationView`, `oauth2_provider.cimd`). If absent, `core.py` logs a warning and does not register the façade URLs; the rest of the plugin keeps working.
8. One commit per WP (plus fix-up commits after review), message `WPn: <summary>`, body lists the microplan file. Do not push.

## Package layout (target state after WP4)

```
inventree_mcp/
  core.py                 plugin class: mixins, SETTINGS, setup_urls, well-known, scheduled tasks, UI features
  settings.py             get_plugin_setting(key, default)   (generalised in WP1)
  mcp_transport.py        existing MCPView; WP1 adds the WWW-Authenticate challenge; WP3 adds scope narrowing for non-OAuth credentials
  models.py               plugin models (WP2: MCPUserState, WP3: MCPRuleSet) - must live at package root (Django app = inventree_mcp)
  migrations/             Django migrations for models.py
  templates/inventree_mcp/  base.html, consent.html, login_bridge.html, error.html   (WP1)
  static/inventree_mcp/     built JS for UI panels/pages (WP4)
  auth/
    __init__.py
    urls.py               urlpatterns for discovery + oauth façade + api (WP1; api added WP3)
    discovery.py          PRM + AS metadata views, issuer()/resource() helpers, challenge_header()   (WP1)
    settings_inject.py    DOT settings overrides: apply(), ensure_applied()                            (WP1)
    cors.py               TrustedOriginCORSMixin, trusted_origins()                                    (WP1)
    clients.py            MCPRegistrationView (DCR wrapper), rate limiter, is_trusted_client(), is_loopback_redirect()  (WP1)
    authorize.py          MCPAuthorizationView                                                         (WP1)
    token.py              MCPTokenView, MCPRevokeView                                                  (WP1)
    login_bridge.py       MCPLoginBridgeView, sso_provider_id()                                        (WP1)
    policy.py             mcp_scopes_available(), effective_scopes(user), may_connect(user), narrow()  (WP1 stub, WP3 full)
    membership.py         recheck(), refresh_grant_allowed(), token_grant_allowed(), block/unblock     (WP1 stub, WP2 full)
    tasks.py              scheduled: cleanup_stale_clients (WP1), membership_sweep (WP2)
    api.py                DRF endpoints: rulesets, effective, connections                             (WP3)
    signals.py            login stamps (WP2), ruleset-change revocation (WP3)
  tests/
    __init__.py
    test_auth_discovery.py test_auth_clients.py test_auth_authorize.py test_auth_token.py test_auth_flow.py (WP1)
    test_membership.py (WP2)  test_policy.py test_api.py (WP3)
  test_mcp.py             existing suite, unchanged except where explicitly required
dev/                      docker-based dev and test environment (WP-dev-environment)
docs/plans/               these microplans
```

## URL names (namespace `plugin:inventree-mcp:`)

| Name | Path under `/plugin/inventree-mcp/` | View | WP |
|---|---|---|---|
| `mcp-endpoint` | `mcp/` | `MCPView` | existing |
| `server-card` | `server-card/` | existing | existing |
| `oauth-protected-resource` | `.well-known/oauth-protected-resource` | `ProtectedResourceMetadataView` | 1 |
| `oauth-authorization-server` | `.well-known/oauth-authorization-server` | `AuthorizationServerMetadataView` | 1 |
| `openid-configuration` | `.well-known/openid-configuration` | same view (alias) | 1 |
| `oauth-register` | `oauth/register/` | `MCPRegistrationView` | 1 |
| `oauth-authorize` | `oauth/authorize/` | `MCPAuthorizationView` | 1 |
| `oauth-token` | `oauth/token/` | `MCPTokenView` | 1 |
| `oauth-revoke` | `oauth/revoke/` | `MCPRevokeView` | 1 |
| `oauth-login` | `oauth/login/` | `MCPLoginBridgeView` | 1 |
| `api-ruleset-list` / `api-ruleset-detail` | `api/rulesets/`, `api/rulesets/<int:pk>/` | WP3 | 3 |
| `api-effective` | `api/effective/<int:user_pk>/` | WP3 | 3 |
| `api-connection-list` / `api-connection-detail` | `api/connections/`, `api/connections/<int:pk>/` | WP3 | 3 |
| `api-status` | `api/status/` | WP3 | 3 |

Issuer: `discovery.issuer(request)` = absolute origin + `/plugin/inventree-mcp` (no trailing slash). Resource: `discovery.resource(request)` = absolute URL of `mcp-endpoint` (keeps its trailing slash). Both honour `X-Forwarded-*` through Django's `build_absolute_uri` (InvenTree sets `USE_X_FORWARDED_*` from config).

Well-known advertisement (`WellKnownMixin.get_well_known_urls`): `mcp-server-card` (existing), `oauth-protected-resource`, `oauth-authorization-server` → core answers `/.well-known/<name>` and `/.well-known/<name>/<anything>` with a 302 to the plugin view.

## Plugin settings (all `SettingsMixin`; WP that introduces them in brackets)

| Key | Type / default | Meaning |
|---|---|---|
| `REQUIRE_AUTH`, `MCP_READ_ONLY`, `MCP_LOG_TOOL_CALLS` | existing | unchanged |
| `OAUTH_ENABLED` | bool / False | serve discovery + façade; when off the 401 challenge carries no `resource_metadata` and façade URLs answer 404 [1] |
| `TRUSTED_CLIENT_HOSTS` | str / `claude.ai,claude.com,.anthropic.com` | CIMD host allowlist, Django `ALLOWED_HOSTS` syntax, comma separated [1] |
| `TRUSTED_CLIENT_ORIGINS` | str / `https://claude.ai,https://claude.com` | CORS origins for façade endpoints and `Application.allowed_origins` of registered clients [1] |
| `AUTO_APPROVE_TRUSTED_CLIENTS` | bool / True | skip consent for trusted CIMD clients with non-loopback redirect URIs [1] |
| `ACCESS_TOKEN_LIFETIME_SECONDS` | int / 3600 | injected into DOT (instance-wide) [1] |
| `REFRESH_TOKEN_LIFETIME_DAYS` | int / 30 | injected into DOT (instance-wide) [1] |
| `AUTH_CODE_LIFETIME_SECONDS` | int / 600 | injected into DOT [1] |
| `ALLOW_LOCALHOST_LOOPBACK` | bool / True | injected into DOT [1] |
| `REGISTER_RATE_LIMIT_PER_MINUTE` | int / 30 | DCR rate limit per client IP, 0 disables [1] |
| `TOKEN_RATE_LIMIT_PER_MINUTE` | int / 60 | authorization-code exchanges per minute and client IP at the token endpoint, 0 disables; refresh grants unlimited [5] |
| `UNUSED_CLIENT_RETENTION_DAYS` | int / 90 | daily cleanup of DCR clients with no live token [1] |
| `OAUTH_FORCE_SSO` | bool / True | login bridge sends the browser straight to the SSO provider [1] |
| `SSO_PROVIDER_ID` | str / `` | allauth provider id used by the bridge and the re-check; empty = the single configured `openid_connect` app [1] |
| `MEMBERSHIP_RECHECK_INTERVAL_MINUTES` | int / 60 | [2] |
| `MEMBERSHIP_GRACE_HOURS` | int / 4 | [2] |
| `RELOGIN_INTERVAL_HOURS` | int / 24 | degraded mode without upstream refresh token [2] |
| `MEMBERSHIP_ENFORCED` | bool / True | master switch for re-checks and blocking [2] |
| `MCP_REQUIRED_GROUPS` | str / `` | InvenTree group names, comma separated; when set a user must be in at least one to use MCP (`policy.in_required_groups`) [2] |
| `MCP_SUPERUSER_FULL_ACCESS` | bool / False | superusers bypass the MCP ruleset [3] |
| `MCP_ALLOW_BASIC_AUTH` | bool / False | Basic auth on the MCP endpoint [3] |

## Interfaces between WPs (signatures are binding)

`auth/policy.py` (WP1 ships the stub bodies marked *stub*, WP3 replaces the bodies, not the signatures):

```python
def mcp_scopes_available() -> list[str]
    # "g:read" + every "r:view:<role>"; plus "r:add/change/delete:<role>" when MCP_READ_ONLY is off. Order stable.
def effective_scopes(user) -> set[str]
    # stub: set(mcp_scopes_available()) if user.is_authenticated and user.is_active else set()
def may_connect(user) -> tuple[bool, str]
    # (True, "") or (False, reason) with reason in {"anonymous", "inactive", "blocked", "no_mcp_permission"}
    # stub: checks anonymous/inactive, then membership.is_blocked(user) -> "blocked"
def narrow(requested, user) -> list[str]
    # requested: iterable of scope strings or None/empty -> effective_scopes(user) in available order; else requested ∩ effective, in requested order
```

`auth/membership.py` (WP1 ships stubs, WP2 the real thing):

```python
class Outcome(str, Enum): MEMBER, NOT_MEMBER, NO_MCP_PERMISSION, UNREACHABLE, NO_UPSTREAM_TOKEN, NOT_DUE, DISABLED
    # NO_MCP_PERMISSION (added after the WP2 review): identity confirmed but the user's groups grant no MCP access.
    # Effect: revoke the user's MCP tokens, keep the upstream token, stamp membership_checked_at, no block;
    # refresh_grant_allowed maps it to (False, "invalid_grant"). Only identity-level refusals block:
    # inactive user, not in MCP_REQUIRED_GROUPS (revoked_reason "not_in_required_groups"), upstream invalid_grant.
def is_blocked(user) -> bool                      # stub: False
def token_grant_allowed(user) -> tuple[bool, str] # for authorization_code grants; stub: (not blocked, "")
def refresh_grant_allowed(user) -> tuple[bool, str]
    # runs recheck() when due; returns (False, "invalid_grant") on refusal; stub: (True, "")
def recheck(user, *, force: bool = False) -> Outcome   # stub: Outcome.DISABLED
def block_user(user, reason: str) -> None         # sets access_revoked_at, revokes all MCP tokens; stub: raises NotImplementedError
def unblock_user(user) -> None
def revoke_all_mcp_tokens(user) -> int            # revokes DOT access+refresh tokens of apps registered through the façade (DCR/CIMD); WP1 real
```

`auth/clients.py` helpers used by authorize/token (WP1):

```python
def is_trusted_client(application) -> bool        # CIMD application whose client_id host matches TRUSTED_CLIENT_HOSTS
def is_loopback_redirect(uri: str) -> bool        # http scheme and host in {localhost, 127.0.0.1, ::1}
def mcp_applications() -> QuerySet                # Application rows created through the façade (registration_source in {DCR, CIMD})
```

`auth/discovery.py` (WP1):

```python
def issuer(request) -> str
def resource(request) -> str
def challenge_header(request, *, invalid_token: bool) -> str   # value for WWW-Authenticate
```

`auth/settings_inject.py` (WP1): `apply() -> dict` (values injected), `ensure_applied() -> None` (cheap; re-applies when plugin settings changed). Keys injected: `CIMD_ENABLED`, `CIMD_REGISTRATION_PERMISSION_CLASSES`, `CIMD_ALLOWED_HOSTS`, `ALLOW_LOCALHOST_LOOPBACK`, `ACCESS_TOKEN_EXPIRE_SECONDS`, `REFRESH_TOKEN_EXPIRE_SECONDS`, `AUTHORIZATION_CODE_EXPIRE_SECONDS`, `REFRESH_TOKEN_REUSE_PROTECTION=True`, `REFRESH_TOKEN_GRACE_PERIOD_SECONDS=120`. **Never** `DCR_ENABLED` (the façade does its own registration) and never `OIDC_ISS_ENDPOINT`.

Models (binding field names, WP2/WP3):

```python
class MCPUserState(models.Model):          # WP2
    user = OneToOneField(User, related_name="mcp_state", on_delete=CASCADE)
    membership_checked_at = DateTimeField(null=True)
    access_revoked_at = DateTimeField(null=True)
    revoked_reason = CharField(max_length=64, blank=True)
    last_recheck_error = CharField(max_length=255, blank=True)
    last_login_at = DateTimeField(null=True)
class MCPRuleSet(models.Model):            # WP3, mirrors users.RuleSet
    group = ForeignKey(Group, related_name="mcp_rule_sets", on_delete=CASCADE)
    name = CharField(max_length=50, choices=users.ruleset.RULESET_CHOICES)
    can_view = can_add = can_change = can_delete = BooleanField(default=False)
    unique_together = (group, name)
```

## Decisions recorded after reviews

- **Blocks are honoured while `MEMBERSHIP_ENFORCED` is off** (KINA DESIGN §7.1): "off" means no re-checks and no new blocks; `is_blocked` keeps returning True for a blocked user until an SSO login that passes the identity checks, or `unblock_user`.
- **Long-lived credentials (API token, Basic) on the MCP endpoint follow the same windows as refresh grants**: denied with 401 when the last re-check found the provider unreachable and `membership_checked_at` is older than the grace period, or when there is no upstream token and the last SSO login is older than the re-login interval. One set of predicates in `membership.py`, used by both `refresh_grant_allowed` and the transport. The interactive `/api/` is never affected.
- **An SSO login lifts a block only when the identity checks pass** (`is_active`, `in_required_groups`); it always stamps `membership_checked_at` and `last_login_at`.
- **`effective_scopes(user)` is empty when the user is not in `MCP_REQUIRED_GROUPS`**, so authorize, refresh narrowing and the transport agree without separate checks.
- **The MCP endpoint calls `policy.may_connect(user)` for every credential type**: `blocked` → 401 with the challenge, `inactive`/`no_mcp_permission` → 403 JSON-RPC "No MCP permission".
- **Never query through the plugin's reverse relations from core models** (`User.objects.filter(mcp_state__…)`, `Group.objects.filter(mcp_rule_sets__…)`) — InvenTree registers plugin apps after core relations are built and the query raises `FieldError` in running processes while the test runner hides it. Use `MCPUserState.objects.filter(user=…)`, `MCPRuleSet.objects.filter(group__in=…)` or `Exists`.

## Definition of done (every WP)

- Microplan acceptance criteria met and demonstrated in the dev environment (`dev/up.sh` running, commands or scripted flow in the WP report).
- Unit tests added under `inventree_mcp/tests/`, passing via `dev/test.sh`; `test_mcp.py` still passes.
- `ruff check --preview` and `ruff format --preview --check` clean.
- `README.md` / `AGENTS.md` updated for new modules and settings.
- Opus review passed: matches the microplan, no duplication, interfaces above untouched, next WP not blocked.
