# InvenTreeMCP

[![PyPI](https://img.shields.io/pypi/v/inventree-mcp)](https://pypi.org/project/inventree-mcp/)
[![CI](https://github.com/inventree/inventree-mcp/actions/workflows/ci.yaml/badge.svg)](https://github.com/inventree/inventree-mcp/actions/workflows/ci.yaml)
[![codecov](https://codecov.io/gh/inventree/inventree-mcp/graph/badge.svg)](https://codecov.io/gh/inventree/inventree-mcp)

An MCP (Model Context Protocol) server for InvenTree, exposed as an InvenTree plugin. It lets MCP
clients (Claude, other MCP-aware agents) query InvenTree inventory data over a Streamable HTTP
endpoint.

## What it is

Every tool is a thin wrapper around InvenTree's own REST API view classes (see
[`inventree_mcp/proxy.py`](inventree_mcp/proxy.py)), dispatched as the authenticated caller - MCP
requests go through exactly the same permission checks, filtering, and serialization as the regular
REST API. Tool code never queries the Django ORM directly.

Currently read-only, covering parts, stock items/locations, part categories, purchase/sales/return/
build orders (with line items and allocations), companies, contacts, addresses, manufacturer/
supplier parts, BOM items, attachments, parameters, stock tracking history, test results, and
project codes. Once write tools land, the `MCP_READ_ONLY` setting (see Configuration) will block
them by default regardless of the calling user's permissions.

Each tool's `outputSchema` and filter/ordering options are derived live from InvenTree's own
serializers and views (not hand-maintained), so they can't drift as InvenTree evolves. Call
`describe_filters(resource)` to see what's available for a given resource. The set of tools an MCP
client sees is also filtered to what the calling user can actually use - though every call is still
permission-checked in full regardless of what was advertised.

Users connect Claude by signing in through the browser ([Connect with OAuth](#connect-with-oauth)), with the
instance's single sign-on and its group-based access control; scripts keep using InvenTree API tokens. What a user
may do through MCP is a second set of roles per group ([Two permission sets](#two-permission-sets)), and group
membership at the identity provider is re-checked while a connection lives
([Membership re-checks](#membership-re-checks)).

## Install

Install via the InvenTree plugin manager, or via pip:

```bash
pip install inventree-mcp
```

Then enable the plugin under **Admin > Plugins**, switch on URL, app, schedule and interface integration for plugins
(`ENABLE_PLUGINS_URL`, `ENABLE_PLUGINS_APP`, `ENABLE_PLUGINS_SCHEDULE`, `ENABLE_PLUGINS_INTERFACE`), run
`invoke migrate`, restart InvenTree, and configure the plugin's settings ([Plugin settings](#plugin-settings)).

A user reaches MCP only through the **MCP roles** of their groups, which start empty: grant them first (see
[Two permission sets](#two-permission-sets)).

## Connect

### Connect with OAuth

The plugin can act as an OAuth 2.1 authorization server for its MCP endpoint, so a user connects
Claude (claude.ai, Claude Desktop, Claude Code) by signing in to InvenTree in the browser. Tokens
are issued and renewed automatically; nothing is copied by hand.

**Connect from Claude:**

- claude.ai / Claude Desktop: *Settings > Connectors > Add custom connector*, URL
  `https://<your-inventree-server>/plugin/inventree-mcp/mcp/`.
- Claude Code: `claude mcp add --transport http inventree https://<your-inventree-server>/plugin/inventree-mcp/mcp/`,
  then run `/mcp` inside Claude Code and choose *Authenticate*.

**What the user sees:** the browser opens InvenTree. Without a session it goes straight to the
single sign-on provider (e.g. Authentik) and comes back. Claude's web clients from trusted hosts are
approved without a consent page; other clients, and every client that receives the result on a
local port (Claude Code), show a consent page with the client, the signed-in user, the permissions
and the token lifetimes. Access is the user's InvenTree permissions, limited to the OAuth scopes
granted (read-only scopes while **Read Only** is on).

The OAuth endpoints need `OAUTH_ENABLED` on and the prerequisites under [Administration](#administration); on
InvenTree 1.5.x also the [reverse-proxy rewrites](#inventree-15x-reverse-proxy-rewrites-for-discovery).

**How it works:** a `401` from the MCP endpoint carries `WWW-Authenticate: Bearer
resource_metadata="…"`. The protected-resource metadata names the issuer
`https://<server>/plugin/inventree-mcp`, whose metadata lists the authorize, token, registration and
revocation endpoints under `/plugin/inventree-mcp/oauth/`. These are thin subclasses of
django-oauth-toolkit's own views; clients register dynamically (RFC 7591) or identify with a client ID
metadata document URL from a trusted host. Tokens are ordinary InvenTree OAuth2 tokens (visible in
the admin under *Django OAuth Toolkit*). InvenTree's own `/o/` endpoints are not changed.

#### InvenTree 1.5.x: reverse-proxy rewrites for discovery

MCP clients look up the authorization-server metadata at the RFC 8414 location
`/.well-known/oauth-authorization-server/plugin/inventree-mcp`. InvenTree 1.5.x does not let plugins
serve anything under `/.well-known/` (it answers with a redirect to the login page, and Claude Code
gives up on the HTML it gets). Newer InvenTree versions (with plugin `/.well-known/` support,
inventree/InvenTree#12698) redirect these paths to the plugin by themselves; on 1.5.x add the
rewrites below to the reverse proxy. While they are missing, the plugin logs a warning
("cannot serve /.well-known/oauth-authorization-server/...") at the first OAuth request.

Caddy (inside the site block, before `reverse_proxy`):

```
rewrite /.well-known/oauth-authorization-server/plugin/inventree-mcp /plugin/inventree-mcp/.well-known/oauth-authorization-server
rewrite /.well-known/openid-configuration/plugin/inventree-mcp /plugin/inventree-mcp/.well-known/openid-configuration
rewrite /.well-known/oauth-protected-resource/plugin/inventree-mcp/mcp* /plugin/inventree-mcp/.well-known/oauth-protected-resource
```

nginx (inside the `server` block that proxies to InvenTree):

```
location = /.well-known/oauth-authorization-server/plugin/inventree-mcp {
    rewrite ^ /plugin/inventree-mcp/.well-known/oauth-authorization-server last;
}
location = /.well-known/openid-configuration/plugin/inventree-mcp {
    rewrite ^ /plugin/inventree-mcp/.well-known/openid-configuration last;
}
location ~ ^/\.well-known/oauth-protected-resource/plugin/inventree-mcp/mcp/?$ {
    rewrite ^ /plugin/inventree-mcp/.well-known/oauth-protected-resource last;
}
```

The first line is the one Claude Code needs. The `openid-configuration` line serves clients that
probe the OpenID form, and the protected-resource line clients that do not follow the
`resource_metadata` URL of the `401` challenge (Claude does); both are harmless otherwise.

The `openid-configuration` document is an alias of the RFC 8414 authorization-server metadata.
It is not a full OpenID Connect discovery document (no `jwks_uri`, `subject_types_supported` or
`id_token_signing_alg_values_supported`; the plugin issues no ID tokens), so a strict OpenID
Connect client rejects it. MCP clients only read the OAuth fields.

### Scripts and other clients: API tokens

Create an InvenTree API token for the script or MCP client and send it with every request.

The endpoint is `<your-inventree-server>/plugin/inventree-mcp/mcp/`, using Streamable HTTP
transport with an `Authorization: Token <token>` header.

For a client that supports remote Streamable HTTP servers directly, add:

```json
{
  "mcpServers": {
    "inventree": {
      "url": "https://<your-inventree-server>/plugin/inventree-mcp/mcp/",
      "headers": {
        "Authorization": "Token <your-api-token>"
      }
    }
  }
}
```

For a client that only supports local (stdio) servers, bridge it with
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote)

*Note: You will need to have node available on your system path*

```json
{
  "mcpServers": {
    "inventree": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "https://<your-inventree-server>/plugin/inventree-mcp/mcp/",
        "--header",
        "Authorization: Token <your-api-token>"
      ]
    }
  }
}
```

*Note: On Windows, you may need to substitute `npx` with `C:\\PROGRA~1\\nodejs\\npx.cmd` in the command field.*

In a setup where the server is running a self-signed certificate, you may need to use the following `env` arguments to disable certificate verification:

```json
"args": [
  ...
],
"env": {
  "NODE_TLS_REJECT_UNAUTHORIZED": "0"
}
```

### Authentication

Access follows the calling user's normal InvenTree role assignments, intersected with the MCP roles
of their groups ([Two permission sets](#two-permission-sets)). Supported auth methods:

- An InvenTree API token: `Authorization: Token <token>`.
- Basic auth (username/password), only with `MCP_ALLOW_BASIC_AUTH` on.
- An OAuth2 bearer token: `Authorization: Bearer <token>`, from the OAuth sign-in above or any other
  InvenTree OAuth2 application (always cut to the user's MCP roles). A scoped token (e.g.
  `r:view:part`) narrows access *below* the underlying user's roles - useful for issuing an agent a tightly-scoped token without creating a
  separate low-privilege user.

Session/cookie auth is not supported (not meaningful for a machine client).

## Administration

[`docs/OPERATIONS.md`](docs/OPERATIONS.md) is the operator's guide: the go-live checklist (each item with the
end-to-end scenario that proves it), the Authentik and InvenTree prerequisites in detail, recommended production
values for every setting, the membership outcomes with the log lines and reason codes operators see, failure modes
and what to do, logging and privacy, and the list of things that went wrong once.

### Prerequisites

InvenTree:

- InvenTree >= 1.5 (django-oauth-toolkit >= 3.4); older versions keep working without the OAuth
  endpoints.
- Single sign-on configured in InvenTree (an `openid_connect` social provider, *Enable SSO* on), or
  users sign in to InvenTree first and then reconnect.
- `SITE_URL` set and the reverse proxy forwarding `X-Forwarded-Proto` / `X-Forwarded-Host`, so the
  advertised URLs use the public origin.
- InvenTree's CORS whitelist (`cors.whitelist`) containing `https://claude.ai` and
  `https://claude.com` (browser preflights to `/plugin/` are answered by InvenTree's CORS middleware).
- Plugin settings: **Enable OAuth Sign-in** (`OAUTH_ENABLED`) on; URL integration, schedule
  integration and app integration (`ENABLE_PLUGINS_APP`, needed for the plugin's `MCPUserState`
  and `MCPRuleSet` models; run `invoke migrate` after installing or upgrading: migrations `0001` to `0003` exist,
  `0003` adds `MCPUserState.recheck_lease_until`) enabled for plugins. With
  `MEMBERSHIP_ENFORCED` on and app integration off, the plugin logs a warning at start-up: re-checks
  and blocks are then disabled. While the migration is missing, reads of the MCP user state fail
  safe (logged once) instead of failing requests.
- A reverse proxy that **replaces** `X-Forwarded-For` with the client address instead of appending
  to it, and gunicorn not reachable directly. InvenTree takes the client IP from the *first*
  `X-Forwarded-For` entry, which a client can set itself when the proxy appends; the registration and token
  rate limits (`REGISTER_RATE_LIMIT_PER_MINUTE`, `TOKEN_RATE_LIMIT_PER_MINUTE`) are per client IP. Caddy replaces the header by
  default (as long as `trusted_proxies` is not configured); with nginx use
  `proxy_set_header X-Forwarded-For $remote_addr;`, not `$proxy_add_x_forwarded_for`.
- On InvenTree 1.5.x, the [reverse-proxy rewrites for discovery](#inventree-15x-reverse-proxy-rewrites-for-discovery).
- On InvenTree 1.5.x (checked on 1.5.7), the first SSO login of every new user ends in a `500` (a core bug fixed by
  InvenTree PR #12842, in no released 1.5.x tag yet); see `docs/OPERATIONS.md` §3.
- A running background worker (the hourly membership sweep, background re-checks, revocations of large groups) and
  interface integration (`ENABLE_PLUGINS_INTERFACE`) plus an InvenTree with plugin pages for the [GUI](#gui).
  Restart InvenTree after switching plugin integration settings.
- In production, InvenTree's default log level (`WARNING`) and an access log without query strings
  ([Logs and privacy](#logs-and-privacy)).

Authentik (or any OIDC provider):

- `offline_access` selected on the provider and requested by InvenTree
  (`social_providers.openid_connect.SCOPE: [openid, profile, email, offline_access]` in
  `config.yaml`), so the login stores a refresh token. Check *Social application tokens*: the
  token secret must be set after a login.
- *Refresh token validity* at least `REFRESH_TOKEN_LIFETIME_DAYS` (30 days). The shorter of the two
  is how long an idle user stays connected; when Authentik's expires first, the user is blocked until
  the next login.
- The `groups` claim in the ID token (*Include claims in token*; Authentik's `profile` mapping
  carries `groups`), and InvenTree's SSO group sync on (`LOGIN_ENABLE_SSO_GROUP_SYNC`, `SSO_GROUP_MAP`
  mapping the provider groups to InvenTree groups, `SSO_REMOVE_GROUPS` on). Without group sync a
  re-check cannot notice a removed group, only a revoked or expired grant.
- The allowed group bound to the application in Authentik. That is the first gate (Authentik refuses
  the login); Authentik does not re-evaluate it on refresh grants, which is why the plugin checks the
  claim.
- `MCP_REQUIRED_GROUPS` set to the InvenTree group(s) the MCP group maps to (e.g. `MCP Users`), so
  losing that group cuts MCP access.

Instances where MCP users sign in with local passwords only should turn `MEMBERSHIP_ENFORCED` off,
otherwise their connections end `RELOGIN_INTERVAL_HOURS` after each login.

### Plugin settings

Under **Settings > Plugin Settings**:

- **Require Authentication** (`REQUIRE_AUTH`, default `True`): reject unauthenticated requests.
  Only disable for local testing.
- **Read Only** (`MCP_READ_ONLY`, default `True`): block all write actions via MCP, regardless of
  the calling user's permissions. A plugin-wide kill switch, independent of per-user roles.
- **Log Tool Calls** (`MCP_LOG_TOOL_CALLS`, default `False`): log every tool call for debugging.
- **Superusers Bypass MCP Roles** (`MCP_SUPERUSER_FULL_ACCESS`, default `False`): superusers get every
  MCP scope regardless of their groups' MCP roles ([Two permission sets](#two-permission-sets)).
- **Allow Basic Authentication** (`MCP_ALLOW_BASIC_AUTH`, default `False`): accept username and
  password on the MCP endpoint. Off: OAuth and InvenTree API tokens only.

OAuth sign-in (see [Connect with OAuth](#connect-with-oauth)):

| Setting | Default | Meaning |
|---|---|---|
| `OAUTH_ENABLED` | `False` | Serve OAuth discovery and the OAuth endpoints. Off: the endpoints answer 404 and the 401 challenge carries no `resource_metadata`. InvenTree 1.5.x also needs the [proxy rewrites](#inventree-15x-reverse-proxy-rewrites-for-discovery). |
| `TRUSTED_CLIENT_HOSTS` | `claude.ai,claude.com,.anthropic.com` | Hosts whose client ID metadata documents (`https` client IDs) are accepted, Django `ALLOWED_HOSTS` syntax. |
| `TRUSTED_CLIENT_ORIGINS` | `https://claude.ai,https://claude.com` | Browser origins allowed to call register / token / revoke (CORS). |
| `AUTO_APPROVE_TRUSTED_CLIENTS` | `True` | Skip consent for trusted-host clients, never for loopback redirect URIs. |
| `ACCESS_TOKEN_LIFETIME_SECONDS` | `3600` | Access token lifetime. |
| `REFRESH_TOKEN_LIFETIME_DAYS` | `30` | Refresh token idle lifetime (rotated on every use). |
| `AUTH_CODE_LIFETIME_SECONDS` | `600` | Authorization code lifetime. |
| `ALLOW_LOCALHOST_LOOPBACK` | `True` | Accept `http://localhost` redirect URIs on any port, like `127.0.0.1`. |
| `REGISTER_RATE_LIMIT_PER_MINUTE` | `30` | Client registrations per minute and IP (`0` disables); `429` with `Retry-After`. |
| `TOKEN_RATE_LIMIT_PER_MINUTE` | `60` | Authorization code exchanges per minute and client IP at the OAuth token endpoint (`0` disables); refresh grants are not limited; `429` with `Retry-After`. |
| `UNUSED_CLIENT_RETENTION_DAYS` | `90` | Daily cleanup of registered clients without a live token. |
| `OAUTH_FORCE_SSO` | `True` | Send the browser straight to the SSO provider when it has no InvenTree session. |
| `SSO_PROVIDER_ID` | empty | allauth provider id to sign in with and to re-check membership at; empty selects the single configured `openid_connect` provider. |
| `MEMBERSHIP_ENFORCED` | `True` | Re-check group membership at the SSO provider and block users who lost it ([Membership re-checks](#membership-re-checks)). Off: no re-checks and no new blocks; existing blocks still apply until an SSO login that passes the identity checks or `unblock_user`. |
| `MEMBERSHIP_RECHECK_INTERVAL_MINUTES` | `60` | How old the last check may be before a refresh re-checks. |
| `MEMBERSHIP_GRACE_HOURS` | `4` | How long refreshes keep working while the provider is unreachable. |
| `RELOGIN_INTERVAL_HOURS` | `24` | Without an upstream refresh token, how long after the last login refreshes keep working. |
| `MCP_REQUIRED_GROUPS` | empty | Comma separated InvenTree group names (case-insensitive); when set, a user must be in one of them to connect and to keep the connection, with any credential (OAuth, API token, Basic). |

The lifetime settings are injected into django-oauth-toolkit at runtime and therefore apply to
**every** OAuth2 application on the instance while `OAUTH_ENABLED` is on (InvenTree has no
configuration hook for them). Every InvenTree process (web workers, background workers) applies them
at start-up and picks up changes within a few seconds of its next request or task. Turning
`OAUTH_ENABLED` off, or deactivating the plugin, restores InvenTree's defaults.

## Two permission sets

Every InvenTree group has two sets of roles with the same shape (one row per role: *view*, *add*,
*change*, *delete*):

- its **interactive roles** (core's group roles, used by the web UI and `/api/`), unchanged;
- its **MCP roles** (the plugin's `MCPRuleSet`), what members may do *through MCP*.

**MCP access = interactive roles ∩ MCP roles, per table.** The MCP roles of a user's groups (their
union) become OAuth2 scopes on the user's MCP tokens; InvenTree core then checks the user's
interactive roles *and* the token's scopes on every proxied call, as for any OAuth2 request. The
plugin re-implements no permission check, and the MCP roles never grant more than the interactive
roles. For a table shared by several roles, any interactive role and any MCP role that cover the
table suffice (see the last bullet).

- New groups get MCP roles with every flag off (so do existing groups when the plugin's migration
  runs): **after installing or upgrading, nobody can use MCP until an administrator grants MCP
  roles** ("Copy from roles" copies a group's interactive roles).
- A user whose groups grant no MCP role gets `403` "No MCP permission" on the endpoint and the *No
  MCP access* page at sign-in. With `MCP_REQUIRED_GROUPS` set, groups outside it count for nothing.
- Every credential on the MCP endpoint is cut to the user's *current* MCP scopes on every request.
  An OAuth token runs under its own scopes ∩ those scopes, whichever application issued it (the
  façade, or an application registered in InvenTree's admin and served by core's `/o/`); an
  InvenTree API token (and Basic, see `MCP_ALLOW_BASIC_AUTH`) runs under an unsaved token carrying
  them. Nothing is saved. The same API token on `/api/` keeps the user's full interactive roles.
- **Read Only** (`MCP_READ_ONLY`) strips the add / change / delete scopes; MCP write roles take
  effect only once it is off.
- Superusers pass every interactive role check; their MCP access is still their groups' MCP roles,
  unless `MCP_SUPERUSER_FULL_ACCESS` is on.
- A **reduction** (a flag cleared, a ruleset or group deleted) revokes the MCP tokens of every
  member of the group at once (more than 20 members: in a background task); clients reconnect and
  get the narrower scopes. API tokens are never revoked. An **addition** revokes nothing: it applies
  at the next sign-in, and for API tokens at the next request. Removing a user from a group applies
  at their next MCP request (nothing is revoked; the request is cut to the remaining MCP scopes, or
  refused with `403` when none are left).
- Without app integration (`ENABLE_PLUGINS_APP`) there are no MCP roles, so every MCP request is
  refused (logged once).
- **Tables shared by several roles** (parts are in both *Parts* and *Build Orders*, stock items in
  *Stock Items* and *Build Orders*, companies in the three order roles): any interactive role and
  any MCP role that cover the table suffice, even when they are different roles, exactly as the
  interactive role check accepts any one of the roles. Example: interactive *Parts: view* only and
  MCP *Build Orders: view* only lets MCP list parts. The effective document's `effective` map is per
  role and does not show such a combination; its `note` says so. (InvenTree itself would require all
  the roles' scopes for an OAuth token; the plugin's proxy relaxes that to match the role check, see
  `docs/upstream/` patch 4.)

Scope mapping (core's granular scopes, `users/oauth2_scopes.py`):

| MCP role flag | Scope | Notes |
|---|---|---|
| any flag of any role | `g:read` | added whenever the user has at least one MCP scope (metadata, `OPTIONS`, role-less tables) |
| *view* on `<role>` | `r:view:<role>` | |
| *add* on `<role>` | `r:add:<role>` | only while **Read Only** is off; implies *change* and *view* (as core's roles) |
| *change* on `<role>` | `r:change:<role>` | only while **Read Only** is off; implies *view* |
| *delete* on `<role>` | `r:delete:<role>` | only while **Read Only** is off; implies *change* and *view* |

Roles: `admin`, `part_category`, `part`, `bom`, `stock_location`, `stock`, `build`,
`purchase_order`, `sales_order`, `return_order`, `transfer_order` (core's `RULESET_CHOICES`; a role
added by a core upgrade appears with every flag off).

**API** (under `/plugin/inventree-mcp/`, InvenTree's session or API-token authentication; used by
the plugin's admin pages):

| Method and path | Who | What |
|---|---|---|
| `GET api/rulesets/?group=<pk>` | staff with the *admin* role's change permission | the group's MCP roles in core's `RuleSetSerializer` shape (`pk`, `name`, `label`, `group`, `can_view`, `can_add`, `can_change`, `can_delete`), sorted by label |
| `PATCH api/rulesets/<pk>/` | same | the four flags (`name`, `group` read-only) |
| `POST api/rulesets/copy/` `{"group": pk}` | same | MCP roles := the group's interactive roles |
| `POST api/rulesets/clear/` `{"group": pk}` | same | every MCP flag off (revokes the members) |
| `POST api/rulesets/revoke/` `{"group": pk}` | same | revoke the members' MCP tokens: `{"users": n, "tokens": m, "queued": bool}` |
| `GET api/effective/<user_pk>/` | the user; staff admins for anybody | `roles` (interactive), `mcp_roles`, `effective` (both; what MCP can do), each `{role: {label, view, add, change, delete}}`, plus `scopes` (empty whenever `may_connect` is false, e.g. for a blocked user), `may_connect`, `reason`, `note` (how to read `effective` for tables shared by several roles) |
| `GET api/connections/[?user=<pk>]` | the user; staff admins with `?user=` | live MCP connections (façade refresh tokens): `pk`, `client_name`, `client_host`, `client_trusted`, `created`, `expires`, `access_expires`, `scopes` |
| `DELETE api/connections/<pk>/` | the owner; staff admins | revoke the connection |
| `GET api/status/[?user=<pk>]` | the user; staff admins with `?user=` | `oauth_enabled`, `mcp_url`, `membership` (`checked_at`, `blocked`, `reason`), `scopes` (empty whenever `may_connect` is false, e.g. for a blocked user), `may_connect`, `reason` |

OAuth tokens are refused on the plugin API by InvenTree's middleware (`401`; it accepts sessions
and API tokens only); the views additionally require `a:staff` for the staff endpoints, which MCP
tokens never carry.

## Membership re-checks

Signing in proves the user's group membership at the SSO provider once. Because Claude keeps a
connection alive with refresh tokens for weeks, the plugin keeps proving it (`MEMBERSHIP_ENFORCED`,
on by default):

| Check point | When | What happens |
|---|---|---|
| SSO login | every login through the MCP sign-in provider | InvenTree's group sync runs as usual; the plugin records the check and the login. It lifts a block only when the user is active and in `MCP_REQUIRED_GROUPS`; otherwise the block stays. |
| Token refresh | when the last check is older than `MEMBERSHIP_RECHECK_INTERVAL_MINUTES` (60) | Synchronous re-check before the token is rotated. A refusal is `invalid_grant`, which makes Claude ask the user to sign in again. |
| MCP request | every request, any credential | Blocked users get `401` with the OAuth challenge. Users who are inactive, outside `MCP_REQUIRED_GROUPS` or without any MCP role get `403` "No MCP permission". OAuth access tokens need nothing more (they live an hour and are re-checked at refresh). Requests with an InvenTree API token or Basic auth queue a background re-check when the last one is stale (the request never waits), and follow the refresh grant's windows: they get `401` (challenge `Bearer realm="InvenTree"` only) when the last re-check found the provider unreachable and the last successful check is older than `MEMBERSHIP_GRACE_HOURS`, or when there is no upstream refresh token and the last login is older than `RELOGIN_INTERVAL_HOURS`. InvenTree's own `/api/` is never affected. |
| Hourly sweep | users holding a live MCP refresh token or API token | Background re-check of everyone who is due, see below for its time budget. |

The re-check redeems the user's upstream refresh token, which allauth stored at the login
(`SocialToken`, *Social application tokens* in the admin), at the provider's token endpoint, stores
the rotated token on the same row, verifies the new ID token against the provider's keys, and feeds
the `groups` claim (`SSO_GROUP_KEY`) to InvenTree's own SSO group sync (`SSO_GROUP_MAP`,
`SSO_REMOVE_GROUPS`), exactly as a login does. The synced user is then judged by the plugin's
policy. Identity refusals block the user: inactive, not in one of the `MCP_REQUIRED_GROUPS`, or the
provider refused the grant. A user whose identity is confirmed but whose groups grant no MCP access
(for example, no MCP ruleset) is not blocked: only their MCP tokens are revoked.

| Outcome | Effect |
|---|---|
| Still a member | Check recorded; the refresh proceeds. |
| Not in `MCP_REQUIRED_GROUPS` (`revoked_reason` `not_in_required_groups`), the provider answers `invalid_grant` (user removed, disabled, grant revoked or expired), or the InvenTree user is inactive | User blocked: every MCP token revoked, the upstream token deleted, `401` on MCP, the authorize page says *Access revoked*. The next SSO login lifts the block once the user is active and back in a required group; an admin can lift it with `unblock_user`. |
| Member without MCP permission | Check recorded, the user's MCP tokens revoked, the refresh refused with `invalid_grant`. No block, the upstream token is kept; the next authorize works as soon as an MCP permission is granted. |
| Provider unreachable (timeout, connection error, 5xx, malformed answer, unverifiable ID token) | Refreshes keep working while the last successful check is younger than `MEMBERSHIP_GRACE_HOURS` (4); after that `invalid_grant`, but nothing is revoked or rotated, and the same refresh token works again once the provider is back. After a timeout, connection error or 5xx the plugin does not call that provider again for 60 seconds; re-checks in that window count as unreachable at once. |
| No upstream refresh token (password login, or the provider issued none because `offline_access` was not granted) | Refreshes keep working while the last login to InvenTree is younger than `RELOGIN_INTERVAL_HOURS` (24), then `invalid_grant`. A warning per user and hour names the cause. The user is not queued for another background re-check within the re-check interval. This is the degraded mode, not the design. |

API tokens and Basic credentials follow the same two windows on the MCP endpoint, judged by the last
recorded re-check (see the check-point table). Service accounts that use an API token with
`MEMBERSHIP_ENFORCED` on therefore need an upstream refresh token, or an interactive login within
`RELOGIN_INTERVAL_HOURS`.

Every call to the provider has a 5 second timeout, re-checks of one user are serialised, and the logs
carry only user ids, the provider host and reason codes (never tokens, claims or addresses). The
state is kept per user in the plugin's `MCPUserState` model (`membership_checked_at`, `access_revoked_at`,
`revoked_reason`, `last_recheck_error`); staff admins read it through `GET api/status/?user=<pk>` (see the API table
under [Two permission sets](#two-permission-sets)). The model is not registered in the Django admin.

The hourly sweep runs as one background task. django-q kills a task after InvenTree's
`background.timeout` (`INVENTREE_BACKGROUND_TIMEOUT`, 90 seconds by default), and one re-check can
take up to about 20 seconds when the provider is slow. The sweep therefore starts no new re-check
after 60 seconds and queues the remaining users as a continuation task. Raising
`background.timeout` gives it more room; lowering it below about 85 seconds risks a sweep being killed
in the middle of a re-check. A re-check holds no database lock while it talks to the provider. It takes a
120-second lease on the user's `MCPUserState` row (`recheck_lease_until`), checks right before redeeming the upstream
refresh token that the lease is still its own (otherwise it stops with `lease_lost` and redeems nothing), stores the
rotated upstream refresh token the moment it arrives, and applies the outcome in a second short transaction. Every
outbound call ends after 15 seconds at most, body included. A second re-check of the same user while the lease is
held returns at once without calling the provider. A refresh grant of an overdue user arriving while another
re-check holds the lease does not wait for it either: it proceeds only within the windows of the last recorded
outcome (`MEMBERSHIP_GRACE_HOURS` since the last successful check, or `RELOGIN_INTERVAL_HOURS` since the last
login when there is no upstream token), otherwise it gets `invalid_grant`. A lease left by a killed worker expires
after 120 seconds. Logins never wait for a re-check.

The plugin keeps short-lived markers in Django's cache: the provider back-off, the "re-check queued"
marker of each user, the "no upstream token" warning marker and the provider's discovery document.
With InvenTree's global cache (Redis) they are shared by all processes. Without it every web and
background worker process has its own, so the back-off and the markers apply per process and the
hourly warning can appear once per process.

## Logs and privacy

The plugin logs to InvenTree's `inventree` logger with the prefix `inventree-mcp:`: user ids, the provider host,
application ids and reason codes, never tokens, codes, keys or e-mail addresses (checked by e2e scenario 9). Three
things outside the plugin need the operator: InvenTree's gunicorn access log and, at log level `INFO`, its request log
record the SSO callback's query string (the identity provider's one-time authorization code); a reverse proxy that
logs response headers records the plugin's own codes in the `Location` header of the authorize redirect; core's SSO
group sync and `MCP_LOG_TOOL_CALLS` log usernames. [`docs/OPERATIONS.md`](docs/OPERATIONS.md) (§5, §7) lists the log
lines and the mitigations.

## GUI

The plugin adds two pages to the InvenTree web interface. They need InvenTree 1.6.0 or later (plugin routes, core PR
#12900; not yet released, the `latest` image) and the global setting *Enable interface integration*
(`ENABLE_PLUGINS_INTERFACE`); on 1.5.7 the plugin offers no pages and no navigation entries, everything else works.

- **MCP Access** (`/web/plugin/inventree-mcp/groups`, staff with the *admin* role's change permission, the condition of the plugin's
  staff API; staff without it see neither the page, its navigation entry nor the *MCP Roles* group panel): pick a group on the left. Accordions show the
  group's interactive roles (the same grid as the core group drawer, saved to core), its **MCP Roles** (what MCP
  clients may do for its members; saved through the plugin API, "Copy from roles" and "Clear" buttons, a warning mark
  on an MCP flag whose group grants no interactive access for it: the flag only takes effect for members who hold
  that role through another group, or for tables shared with another role, because a member's MCP access is their
  interactive roles across all groups intersected with the MCP roles of their groups, per table), and its members
  with their effective MCP access and connection count. "Revoke MCP tokens for this group" signs the members' MCP
  clients out; above 20 members it reports "Revocation queued for N members" (the work runs in the background).
- **MCP** (`/web/plugin/inventree-mcp/connections`, every user): status (OAuth enabled, whether you may connect and
  why not, membership check), your effective MCP permissions, your connected clients with a revoke button, and the
  connection instructions: the MCP URL with the OAuth command `claude mcp add --transport http inventree <url>` and
  the claude.ai steps when OAuth is enabled, otherwise the API-token variant
  `claude mcp add --transport http inventree <url> --header "Authorization: Token <your-api-token>"` with a note that
  OAuth sign-in is off.

A group panel "MCP Roles" (`Panel.js:renderMCPRolesPanel`) is registered for `target_model == "group"` as the entry
point for a future core group drawer; core's group drawer requests plugin panels only with `docs/upstream/` patch 3.

The pages are React bundles built from `frontend/` into `inventree_mcp/static/` (generated and git-ignored, never
edited by hand; built by `dev/up.sh`, CI and before `python -m build`): `cd frontend && npm ci && npm run build`. A source checkout installed with pip needs that build first; a missing
build is logged as a warning and the pages are left out. See `dev/README.md` for the dev stack and `dev/ui_smoke.py`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Claude Code: `JSON Parse error: Unrecognized token '<'` | InvenTree 1.5.x without the [reverse-proxy rewrites](#inventree-15x-reverse-proxy-rewrites-for-discovery) |
| The client gets a `401` without `resource_metadata` | `OAUTH_ENABLED` is off |
| *Unknown client* page | untrusted `https` client ID (`TRUSTED_CLIENT_HOSTS`) or a redirect URI the client did not register |
| *No MCP access* page, `403` "No MCP permission" | none of the user's groups grants an MCP role, or the user is outside `MCP_REQUIRED_GROUPS` |
| *Access revoked* page, `401` "Access revoked, sign in to InvenTree again." | the user is blocked; the next SSO login lifts it once they are back in a required group |
| Users signed out every day | no upstream refresh token: `offline_access` missing at the provider or in `SCOPE` |
| API token gets `401` "MCP access suspended …" | provider unreachable past `MEMBERSHIP_GRACE_HOURS`, or no upstream token and no login within `RELOGIN_INTERVAL_HOURS` (service accounts: see OPERATIONS §6) |
| `500` at the first SSO login on InvenTree 1.5.x | core bug fixed by InvenTree PR #12842, see OPERATIONS §3 |
| Intermittent `403`/`404` on `/plugin/...` | restart InvenTree after switching plugin integration settings |
| `429` on registration or the code exchange | `REGISTER_RATE_LIMIT_PER_MINUTE` / `TOKEN_RATE_LIMIT_PER_MINUTE` per client IP; check that the proxy replaces `X-Forwarded-For` |
| No *MCP* / *MCP Access* pages | InvenTree before 1.6.0 has no plugin pages, *Enable interface integration* is off, the frontend bundles were not built or (*MCP Access* only) the user lacks the admin role's change permission |

More in [`docs/OPERATIONS.md`](docs/OPERATIONS.md) §6 (failure modes) and §8 (things that went wrong once).

## Development

- [`AGENTS.md`](AGENTS.md): code layout and the rules for changes (tool code never touches the ORM, OAuth state stays
  in django-oauth-toolkit, lint commands).
- [`dev/README.md`](dev/README.md): a disposable InvenTree with this checkout installed, demo data and SSO against a
  disposable Authentik, all in Docker: `dev/up.sh`, then `dev/test.sh` for the plugin's test suite inside InvenTree.
- End-to-end suite against that stack: `python3 dev/e2e/run.py` (`--list`, `--only N`, `--skip-outage`). It runs the
  go-live checklist of [`docs/OPERATIONS.md`](docs/OPERATIONS.md) as ten scenarios, prints PASS/FAIL lines, exits
  non-zero on a failure and restores everything it changes.
- Frontend: `frontend/` (React), built into `inventree_mcp/static/` with `npm ci && npm run build`.
