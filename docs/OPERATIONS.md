# Operating the InvenTree MCP plugin with OAuth sign-in

This is the long form of the README's administration part: what has to be in place before users connect Claude
through OAuth, which values to use, what you see in the logs, and what to do when something goes wrong. It assumes an
OpenID Connect provider that InvenTree already signs in through. Authentik is named throughout because that is what
the plugin was built and tested against (`dev/`); any OIDC provider that issues refresh tokens and a groups claim
works the same way.

The behaviour reproduces `KINA/docs/MCP-AUTH-GUIDE.md` (policy and timings) on top of InvenTree's own OAuth2 provider
(django-oauth-toolkit, "DOT"), allauth (SSO login) and InvenTree's SSO group sync.

## 1. Go-live checklist

Each item names the scenario of the end-to-end suite (`python3 dev/e2e/run.py`, see `dev/README.md`) that proves it on
the dev stack. Run the suite against the dev stack after every upgrade; repeat the items marked *manual* by hand on
the production instance before opening it to users.

| # | Check | How it shows | e2e |
|---|---|---|---|
| 1 | Authentik's discovery document lists `offline_access`, InvenTree requests it, and a login stores an upstream refresh token | `<issuer>/.well-known/openid-configuration` has `offline_access` in `scopes_supported`; after a login, *Social application tokens* in the Django admin shows a token secret for the user | 2 (the upstream token exists and rotates at a due re-check); `dev/authentik/sso_smoke.py` ("refresh token stored") |
| 2 | A member connects Claude, uses tools, and is not asked to sign in again when the access token is renewed | refresh with a fresh check makes no upstream call; refresh with a due check redeems the upstream token and succeeds | 2 |
| 3 | Real tool calls work behind the reverse proxy, with OAuth tokens and with API tokens (paginated list plus a detail call) | `list_parts {"limit": 1}` returns a row and a `next` link on the public host; `get_part` returns it | 2 |
| 4 | Removed from the MCP group in Authentik: the next due refresh fails with `invalid_grant`, the user is blocked, MCP requests get `401`, the authorize page says *Access revoked*; back in the group, the next SSO login lifts the block | `revoked_reason` `not_in_required_groups` | 3 |
| 5 | Authentik down: refreshes keep working for `MEMBERSHIP_GRACE_HOURS`, then `invalid_grant` with nothing revoked; the same refresh token works once Authentik is back | `last_recheck_error` `connection_error` | 4 |
| 6 | Users without MCP access are refused: interactive-only users and users without any InvenTree group see *No MCP access*, users outside the Authentik application binding are refused by Authentik | | 5 |
| 7 | MCP access is the intersection of interactive roles and MCP roles; an API token on `/mcp/` is narrowed the same way, while `/api/` keeps the full interactive roles | | 6 |
| 8 | Reducing MCP roles revokes the members' MCP tokens at once | | 7 |
| 9 | `401` from `/mcp/` carries `WWW-Authenticate` with `resource_metadata`; protected-resource and authorization-server metadata are served (on 1.5.x through the proxy rewrites); CORS answers for `https://claude.ai` on the metadata and machine endpoints | | 1 |
| 10 | An untrusted `https` client ID and an unregistered redirect URI produce an error page, never a redirect; a trusted client keeps the consent page for a loopback redirect URI and skips it for its `https` redirect URI | | 8 |
| 11 | No test user's e-mail address, no token and no code in the InvenTree server and worker logs or the proxy log | see [§7](#7-logging-and-privacy) for the one core caveat (upstream codes in core's request/access log) | 9 |
| 12 | Claude Code finds the server and asks for authentication | `claude mcp list` shows `! Needs authentication` | 10 (finishing the sign-in is manual: the runner prints the steps) |
| 13 | Remove `offline_access` from the Authentik provider: new logins store no upstream token, the plugin logs the "no upstream refresh token" warning, connections end `RELOGIN_INTERVAL_HOURS` after the last login; put it back: the next login stores a token again, without a restart | *manual*; unit tests `NoUpstreamTokenTest` | none |
| 14 | Claude (claude.ai or Claude Desktop) connects as a custom connector from the public URL and runs a tool | *manual*: claude.ai cannot reach a dev stack on localhost | none |
| 15 | The background worker runs the plugin's schedules | *Admin > Background tasks*: `inventree_mcp.auth.tasks.membership_sweep` (hourly) and `cleanup_stale_clients` (daily) | none |

## 2. Authentik prerequisites

On the OAuth2/OpenID provider InvenTree signs in with:

- Client type confidential, grant types `authorization_code` and `refresh_token`, InvenTree's callback
  (`https://<inventree>/accounts/<provider_id>/login/callback/`) as a strict redirect URI, a signing key (RS256, so
  the ID token can be verified against the JWKS).
- Scopes `openid`, `email`, `profile`, `offline_access`. Authentik's default `profile` mapping carries the `groups`
  claim; if you use a dedicated `groups` scope mapping instead, select that scope too and add `groups` to InvenTree's
  `SCOPE`. **`offline_access` is what makes Authentik issue the refresh token** the plugin
  re-checks membership with; Authentik lists it in `scopes_supported` only once it is selected on the provider.
- *Include claims in ID token* on, so `groups` arrives in the ID token (userinfo is only the fallback).
- *Refresh token validity* at least `REFRESH_TOKEN_LIFETIME_DAYS` (30 days by default). The shorter of the two is how
  long an idle user stays connected; when Authentik's runs out first, the next re-check gets `invalid_grant` and the
  user is blocked until the next login.
- Authorization flow with implicit consent: the plugin shows its own consent page where one is needed.
- The allowed groups bound to the application (policy engine mode *any*). This is the first gate: Authentik refuses
  the login of everybody else (e2e scenario 5, `e2e-nonmember`). Authentik does **not** re-evaluate the binding on a
  refresh grant, it returns tokens whose `groups` claim lacks the group; the plugin's claim check through InvenTree's
  group sync is what cuts a removed member off (scenario 3).
- `email_verified`: Authentik's built-in `email` mapping sends `email_verified: false`. Neither the plugin nor the
  login needs a verified address: allauth stores the address as unverified and the SSO login proceeds (that is how
  the dev stack runs). Add a custom `email` scope mapping that returns `true` only if you want the addresses marked
  verified in InvenTree; decide before the first users sign in.

## 3. InvenTree prerequisites

| What | Value | Why |
|---|---|---|
| InvenTree version | ≥ 1.5.0 for the MCP endpoint and OAuth (django-oauth-toolkit ≥ 3.4, feature-detected); a release with plugin routes (core PR #12900, after 1.5.7: InvenTree ≥ 1.6) for the GUI pages; a release with plugin `/.well-known/` support (inventree/InvenTree#12698) to drop the proxy rewrites | On older DOT the plugin logs a warning and serves no OAuth endpoints; on 1.5.7 there are no plugin pages or navigation entries |
| SSO | `social_backends: [allauth.socialaccount.providers.openid_connect]` and a `social_providers.openid_connect` app in `config.yaml`, with `SCOPE: [openid, profile, email, offline_access]` (plus `groups` if you use a dedicated scope); global settings *Enable SSO* (`LOGIN_ENABLE_SSO`) and *Enable SSO registration* (`LOGIN_ENABLE_SSO_REG`) | the login bridge sends MCP sign-ins straight to the provider; the re-check needs the stored upstream token. `dev/config/config.yaml.tmpl` is a working example |
| SSO group sync | `LOGIN_ENABLE_SSO_GROUP_SYNC` on, `SSO_GROUP_KEY` = `groups`, `SSO_GROUP_MAP` mapping the provider's groups to InvenTree groups (e.g. `{"inventree-users": "Users", "inventree-mcp": "MCP Users"}`), `SSO_REMOVE_GROUPS` on; `SIGNUP_GROUP` empty unless every SSO user should get a default group | without it a re-check cannot notice a removed group, only a revoked or expired grant |
| Plugin integration | `ENABLE_PLUGINS_URL` (endpoints), `ENABLE_PLUGINS_APP` (the `MCPUserState` and `MCPRuleSet` models; run `invoke migrate` after installing or upgrading), `ENABLE_PLUGINS_SCHEDULE` (sweep and cleanup), `ENABLE_PLUGINS_INTERFACE` (GUI pages) | restart the web and worker processes after switching these: gunicorn preloads the app, and workers started before `ENABLE_PLUGINS_URL` was on keep answering `/plugin/...` with 403/404 |
| Background worker | django-q running; `background.timeout` (`INVENTREE_BACKGROUND_TIMEOUT`, 90 s) at least about 85 s | hourly sweep, background re-checks of API-token users, revocation of large groups |
| Global cache | Redis (`INVENTREE_CACHE_*`) recommended | the provider back-off, the "re-check queued" markers and the cached discovery document are shared by all processes; without it each process keeps its own |
| CORS | `cors.whitelist` / `INVENTREE_CORS_ORIGIN_WHITELIST` containing `https://claude.ai,https://claude.com` | browser preflights to `/plugin/` are answered by InvenTree's CORS middleware before any plugin view runs |
| Public origin | `SITE_URL` (`INVENTREE_SITE_URL`) set to the public URL; `INVENTREE_USE_X_FORWARDED_HOST`/`_PROTO` (and `_PORT` if needed) on; the proxy sends `X-Forwarded-Proto` and `X-Forwarded-Host` | every advertised URL (issuer, endpoints, `resource_metadata`, pagination links of tool results) uses the public origin; InvenTree derives `ALLOWED_HOSTS` from `SITE_URL` |
| Client IP | the proxy **replaces** `X-Forwarded-For` (Caddy does by default; nginx: `proxy_set_header X-Forwarded-For $remote_addr;`), gunicorn not reachable directly | the registration and token rate limits are per client IP, and InvenTree takes the first `X-Forwarded-For` entry |
| 1.5.x discovery | the proxy rewrites of the README ("InvenTree 1.5.x: reverse-proxy rewrites for discovery") | 1.5.x cannot let plugins serve `/.well-known/`; without the rewrite Claude Code gets InvenTree's HTML login page and stops with a JSON parse error |
| 1.5.x first SSO login | InvenTree 1.5.x (checked on 1.5.7): the first SSO login of a new user ends in a 500 (`AssertionError` in allauth's `setup_user_email`, `CustomSocialAccountAdapter.save_user` marks the SocialLogin instead of the User). User, groups and token are saved, the session is not. Fixed on master by InvenTree PR #12842, in no released 1.5.x tag | every user's first MCP sign-in on 1.5.x ends on this 500; starting the connection again signs in normally, because the account exists by then (expected from the cause, not verified on a production instance). The dev stack patches the line (`dev/entrypoint.sh`, `DEV_PATCH_SSO_SIGNUP`) |
| DOT lifetimes | none to set; know that they are instance-wide | while `OAUTH_ENABLED` is on the plugin injects its lifetimes (access 1 h, refresh 30 d, code 10 min), refresh token rotation with reuse protection and a 120 s grace period into DOT. That applies to **every** OAuth2 application on the instance, including ones registered in InvenTree's admin and served by core's `/o/`. Turning `OAUTH_ENABLED` off, or deactivating the plugin, restores InvenTree's values |
| Logging | `INVENTREE_LOG_LEVEL` `WARNING` (the default) in production, and an access-log format without query strings | see [§7](#7-logging-and-privacy) |

## 4. Plugin settings

*Settings > Plugin Settings > InvenTree MCP*. Changes apply within a few seconds in every process (web and background
workers), no restart needed.

| Setting | Default | Production | Notes |
|---|---|---|---|
| `REQUIRE_AUTH` | `True` | `True` | off only for local testing |
| `MCP_READ_ONLY` | `True` | `True` | strips the add / change / delete scopes; MCP write roles take effect only when off |
| `MCP_LOG_TOOL_CALLS` | `False` | `False` | logs tool name, arguments and the calling **username**; for debugging only (see §7) |
| `MCP_SUPERUSER_FULL_ACCESS` | `False` | `False` | superusers are limited by their groups' MCP roles like everybody else |
| `MCP_ALLOW_BASIC_AUTH` | `False` | `False` | username and password on `/mcp/`; prefer API tokens for scripts |
| `OAUTH_ENABLED` | `False` | `True` | serves discovery and `/plugin/inventree-mcp/oauth/*`; injects the DOT values (instance-wide) |
| `TRUSTED_CLIENT_HOSTS` | `claude.ai,claude.com,.anthropic.com` | default | hosts whose client ID metadata documents are accepted (Django `ALLOWED_HOSTS` syntax) |
| `TRUSTED_CLIENT_ORIGINS` | `https://claude.ai,https://claude.com` | default | CORS origins of register / token / revoke, stored as `allowed_origins` of registered clients |
| `AUTO_APPROVE_TRUSTED_CLIENTS` | `True` | `True` | skips consent for trusted-host clients, never for loopback redirect URIs |
| `ACCESS_TOKEN_LIFETIME_SECONDS` | `3600` | `3600` | how long a removed member keeps an already issued access token |
| `REFRESH_TOKEN_LIFETIME_DAYS` | `30` | `30`, ≤ Authentik's refresh token validity | idle limit of a connection (rotated on every use) |
| `AUTH_CODE_LIFETIME_SECONDS` | `600` | `600` | |
| `ALLOW_LOCALHOST_LOOPBACK` | `True` | `True` | Claude Code redirects to `http://localhost:<port>` |
| `REGISTER_RATE_LIMIT_PER_MINUTE` | `30` | `30` | per client IP; `429` with `Retry-After` |
| `TOKEN_RATE_LIMIT_PER_MINUTE` | `60` | `60` | authorization code exchanges at the token endpoint per client IP (`0` disables); refresh grants are not limited; `429` with `Retry-After` |
| `UNUSED_CLIENT_RETENTION_DAYS` | `90` | `90` | Claude registers a new client per connection; unused ones are deleted daily |
| `OAUTH_FORCE_SSO` | `True` | `True` | the login bridge goes straight to the provider |
| `SSO_PROVIDER_ID` | empty | empty, or the provider id when InvenTree has several OIDC apps | with several apps and no id, the bridge falls back to manual mode and re-checks find no provider |
| `MEMBERSHIP_ENFORCED` | `True` | `True` for SSO users; see [§6](#6-failure-modes-and-what-to-do) for service accounts and password-only instances | off: no re-checks and no new blocks; existing blocks still apply |
| `MEMBERSHIP_RECHECK_INTERVAL_MINUTES` | `60` | `60` | |
| `MEMBERSHIP_GRACE_HOURS` | `4` | `4` | raise temporarily for planned provider maintenance |
| `RELOGIN_INTERVAL_HOURS` | `24` | `24` | degraded mode only (no upstream token); raising it does not fix daily logouts |
| `MCP_REQUIRED_GROUPS` | empty | the InvenTree group(s) the MCP group of the provider maps to, e.g. `MCP Users` | losing it is an identity refusal (block); empty means the MCP roles alone decide |

After installing or upgrading, every group's MCP roles are off: nobody can use MCP until an administrator grants MCP
roles (*MCP Access* page, or `PATCH /plugin/inventree-mcp/api/rulesets/<pk>/`).

## 5. Membership outcomes and what operators see

The plugin logs to the `inventree` logger, prefixed `inventree-mcp:`, with user ids, the provider host and reason
codes only. Its INFO lines appear only at `INVENTREE_LOG_LEVEL=INFO`; WARNING lines always.

| Outcome | Trigger | Effect | State (`MCPUserState`) | Log line (level) | The client sees |
|---|---|---|---|---|---|
| Member | due re-check, provider confirms the groups | check recorded, refresh proceeds, the upstream token rotated in place | `membership_checked_at` now, `last_recheck_error` empty | `membership re-check for user id N: member (issuer HOST)` (INFO) | nothing |
| Not in `MCP_REQUIRED_GROUPS` | the synced groups lack every required group | **block**: every MCP token revoked, upstream token deleted | `access_revoked_at` set, `revoked_reason` `not_in_required_groups` | `MCP access blocked for user id N (not_in_required_groups), K tokens revoked` (WARNING) | refresh `invalid_grant`; `/mcp/` `401` "Access revoked, sign in to InvenTree again." with the challenge; authorize page *Access revoked* |
| Upstream grant refused | provider answers `invalid_grant` (user removed, disabled, grant revoked or expired) | block | `revoked_reason` `invalid_grant` | `identity provider HOST revoked the upstream grant of user id N` (INFO), then the block line (WARNING) | as above |
| Inactive | the InvenTree user is inactive | block | `revoked_reason` `inactive` | block line | as above |
| Member without MCP permission | identity confirmed, but the user's groups grant no MCP role | MCP tokens revoked, **no block**, upstream token kept | check recorded | `membership re-check for user id N: member without MCP permission, K tokens revoked (issuer HOST)` (INFO) | refresh `invalid_grant`; `/mcp/` `403` "No MCP permission"; authorize page *No MCP access* |
| Provider unreachable | timeout, connection error, 5xx, malformed answer, unverifiable ID token, subject mismatch | nothing revoked or rotated; refreshes allowed while the last successful check is younger than `MEMBERSHIP_GRACE_HOURS`, then `invalid_grant`; after a timeout, connection error or 5xx the provider is left alone for 60 s | `last_recheck_error` one of `timeout`, `connection_error`, `http_4xx`, `http_5xx`, `bad_response`, `bad_userinfo`, `discovery_http_4xx`/`_5xx`, `bad_discovery`, `bad_id_token`, `subject_mismatch`, `lease_lost` (the re-check lost its lease to another one before redeeming; nothing redeemed), `error` | `membership re-check for user id N inconclusive (CODE), issuer HOST` (WARNING); past grace `refresh refused for user id N: identity provider unreachable past the grace period` (INFO) | within grace nothing; past grace `invalid_grant` (Claude asks to sign in again; the login fails while the provider is down); API tokens `401` "MCP access suspended: group membership could not be confirmed …" |
| No upstream token | password login, `offline_access` not granted, no or ambiguous SSO provider | refreshes allowed for `RELOGIN_INTERVAL_HOURS` after the last login, then `invalid_grant` | `last_recheck_error` `no_token`, `no_provider` or `unsupported_provider` | `no upstream refresh token for user id N (CODE); MCP refresh grants work until … hours after the last login …` (WARNING, once per user and hour) | past the window `invalid_grant`; API tokens `401` "MCP access suspended: sign in to InvenTree again to keep using this credential." |

Other lines worth knowing:

| Line | Meaning |
|---|---|
| `SSO login lifted the MCP block of user id N` | an SSO login with the identity checks passing (active, in a required group) |
| `SSO login of user id N kept the MCP block …` | an SSO login by a blocked user who still fails the identity checks |
| `MCP block lifted for user id N` | `unblock_user()` was called |
| `authorization denied (REASON) for user id N` | authorize refused: `blocked`, `inactive`, `no_mcp_permission` (403 error page) |
| `authorization refused (ERROR) for user id N` | unknown client or unregistered redirect URI (error page, never a redirect) |
| `consent skipped for trusted client APP_PK, user id N` | trusted CIMD client with an `https` redirect URI |
| `refresh refused (REASON) for user id N`, `code exchange refused (REASON) for user id N` | token endpoint refusals before DOT runs |
| `revoked token reused, token family of user id N revoked` | a revoked refresh token was replayed inside the grace window |
| `long-lived credential refused (REASON) for user id N` | API token or Basic past the grace or re-login window |
| `MCP permissions of a group reduced (group id G); revoked K MCP tokens of U users` | an MCP flag cleared, a ruleset or group deleted |
| `membership sweep: member=…, no_upstream_token=…, not_due=…, deferred=…` | the hourly sweep's counts (INFO) |
| `registered MCP client APP_PK (public)`, `deleted N stale DCR clients` | registration, daily cleanup |
| `DOT settings injected: …` | per process, at start and after a settings change (INFO) |
| `OAuth sign-in is on, but this InvenTree version cannot serve /.well-known/oauth-authorization-server/…` | 1.5.x without the proxy rewrites (WARNING, once per process) |
| `MEMBERSHIP_ENFORCED is on, but app integration …`, `cannot read MCP user state (run invoke migrate)`, `app integration (ENABLE_PLUGINS_APP) is off …` | missing app integration or migration: re-checks disabled / MCP closed (fail closed) |
| `inventree-mcp: this InvenTree has no plugin routes, no navigation items` (INFO), the missing-frontend-bundle warning (WARNING) | once per process, not per page load |

Where to look: the plugin API `GET /plugin/inventree-mcp/api/status/?user=<pk>` (staff admins: `membership.checked_at`,
`blocked`, `reason`, `may_connect`), `GET api/effective/<pk>/` and `GET api/connections/?user=<pk>`; the *MCP Access*
page (members' effective access and connection counts) on InvenTree versions with plugin pages; the Django admin for
DOT's tokens (*Django OAuth Toolkit*) and the upstream tokens (*Social application tokens*). `MCPUserState` and
`MCPRuleSet` are not registered in the Django admin.

## 6. Failure modes and what to do

**Identity provider down.** Nothing to do for up to `MEMBERSHIP_GRACE_HOURS` (4 h after each user's last successful
check): refreshes keep working. After that refreshes get `invalid_grant` and Claude asks the user to sign in, which
fails while the provider is down. Nothing is revoked or rotated: when the provider is back, the same refresh tokens
work again (e2e scenario 4). API tokens on `/mcp/` are refused with `401` past the grace window and recover through the
background re-check they queue; `/api/` is never affected. For planned maintenance raise `MEMBERSHIP_GRACE_HOURS`
beforehand and lower it afterwards.

**A user must lose access now.** Removing the user from the group at the provider takes effect at the next due
re-check: the next refresh (access tokens live `ACCESS_TOKEN_LIFETIME_SECONDS`, the re-check runs when the last check
is older than `MEMBERSHIP_RECHECK_INTERVAL_MINUTES`), the next background re-check of an API-token user, or the hourly
sweep. To cut sooner, do one of these in InvenTree:

- remove the user from the InvenTree group that carries the MCP roles (or `MCP_REQUIRED_GROUPS`): every MCP request
  is cut to the remaining MCP scopes at once, `403` when none are left. With SSO group sync, also remove them at the
  provider, or the next login or re-check adds the group back;
- revoke the user's connections (*MCP* page as the user, `DELETE api/connections/<pk>/`, or the group's "Revoke MCP
  tokens" / `POST api/rulesets/revoke/`);
- deactivate the InvenTree user.

**Unblocking.** A blocked user is unblocked by their next SSO login once they are active and back in a required group
(scenario 3), or by hand: `python manage.py shell -c "from django.contrib.auth import get_user_model; from
inventree_mcp.auth.membership import unblock_user; unblock_user(get_user_model().objects.get(pk=<id>))"`. A block
outlives `MEMBERSHIP_ENFORCED` being turned off.

**Users are logged out every day.** The upstream token is missing: check that `offline_access` is selected on the
provider and in `SCOPE`, and that *Social application tokens* holds a token secret after a login. The log names the
cause (`no upstream refresh token for user id N (no_token)`). Raising `RELOGIN_INTERVAL_HOURS` is not the fix.

**Users are logged out earlier than 30 days, or blocked after a quiet period.** Authentik's refresh token validity is
shorter than `REFRESH_TOKEN_LIFETIME_DAYS`; when it expires the re-check gets `invalid_grant` and blocks the user
(`revoked_reason` `invalid_grant`) until the next login. Make Authentik's at least as long.

**Lifetimes.** The plugin's lifetimes apply to every OAuth2 application of the instance while `OAUTH_ENABLED` is on
(refresh tokens of other applications now expire after 30 days and rotate). If another integration depends on DOT's
previous values, it must cope with rotation, or the façade cannot be enabled on that instance.

**Service accounts and password-only instances.** API-token-only accounts that never sign in through SSO have no
upstream token. With `MEMBERSHIP_ENFORCED` on they are suspended on `/mcp/` (`401` "MCP access suspended: sign in to
InvenTree again …") once a background re-check has recorded `no_token` and the last login is older than
`RELOGIN_INTERVAL_HOURS`, although the token row is valid for longer. Either turn `MEMBERSHIP_ENFORCED` off (on
instances where MCP users sign in with local passwords only, this is the right setting), or give the account an
upstream token by signing it in through SSO once with a provider that issues refresh tokens. `/api/` is unaffected.

**Claude Code: "JSON Parse error: Unrecognized token '<'".** InvenTree 1.5.x without the discovery rewrites: Claude
Code fetched `/.well-known/oauth-authorization-server/plugin/inventree-mcp` and got InvenTree's HTML login page.

**"Unknown client" page.** The client ID is an `https` URL on a host outside `TRUSTED_CLIENT_HOSTS`, or the redirect URI
is not registered for the client. The flow never redirects in that case. A client registered on another instance or
deleted by the cleanup has to reconnect.

**`403` "No MCP permission".** The user is active and not blocked but none of their groups grants an MCP role (or they
are outside `MCP_REQUIRED_GROUPS`); grant MCP roles to one of their groups. Check with `GET api/effective/<pk>/`.

**`429` on registration or the code exchange.** More than `REGISTER_RATE_LIMIT_PER_MINUTE` registrations or `TOKEN_RATE_LIMIT_PER_MINUTE` authorization code exchanges from one IP in a minute; behind a
proxy that appends to `X-Forwarded-For` every client shares the proxy's address. Fix the proxy (see §3).

**Intermittent 403/404 on `/plugin/...` after changing plugin integration settings.** Restart the web and worker
processes.

## 7. Logging and privacy

What the plugin guarantees (checked by e2e scenario 9 on every run):

- No access token, refresh token, authorization code, client secret, upstream token or API token in its log lines or
  responses (lengths and counts only).
- Refused users are logged by user id, issuer host and reason code, never by e-mail address or name.
- The upstream refresh token is stored where allauth stores it (`SocialToken.token_secret`, in clear, as allauth
  does); it is never returned by any endpoint.

What is outside the plugin and needs the operator:

- **Core request logging records query strings.** InvenTree's gunicorn access log (on by default in the container
  image, independent of the log level) and, at `INVENTREE_LOG_LEVEL=INFO`, django-structlog's `request_started` /
  `request_finished` lines record the full path of the SSO callback, including the provider's authorization `code`
  and `state`. The code is single use, short-lived and cannot be redeemed without the OIDC client secret, but it is a
  secret in a log. In production keep `INVENTREE_LOG_LEVEL` at `WARNING` and log paths without query strings, for
  example `GUNICORN_CMD_ARGS="--access-logformat '%(h)s %(t)s \"%(m)s %(U)s %(H)s\" %(s)s %(b)s'"` (`%(U)s` is the
  path without the query; checked with `gunicorn --print-config`, not on a running production server), and strip
  `code` and `state` in the reverse proxy's access log (below). The dev stack runs at `INFO` and with
  gunicorn's default access log, so e2e scenario 9 reports these lines as `WARN`, not `FAIL`.
- **Usernames.** Core's SSO group sync logs "Adding group … to user …" / "Removing group … from …" with the username,
  at every login and re-check (INFO). `MCP_LOG_TOOL_CALLS` logs the calling username and the tool arguments. If SSO
  usernames are e-mail addresses, keep the log level at `WARNING` and tool-call logging off.
- **Reverse proxy.** Strip `code` and `state` from logged request query strings **and from the logged `Location`
  response header**: the authorize endpoint answers the consent (and a trusted client's skipped consent) with a `302`
  to `redirect_uri?code=…&state=…`, so a proxy that logs response headers (Caddy's JSON access log does) writes the
  plugin's own authorization codes. Do not log `Authorization` or cookies (Caddy redacts them by default). Caddy:

  ```
  log {
      format filter {
          request>uri query {
              delete code
              delete state
          }
          resp_headers>Location query {
              delete code
              delete state
          }
      }
  }
  ```

  (the `resp_headers>Location` part was checked in a throwaway Caddy container: the logged `Location` keeps only the
  path). E2e scenario 9 fails on the dev stack until `dev/Caddyfile` gets that block; the request part is there
  already.

## 8. Things that went wrong once

From the KINA guide (§11), adapted:

- **Discovery cached forever.** The scope was added at the provider; the app kept requesting the old scopes until a
  restart. Here InvenTree requests the scopes of `config.yaml` on every login, and the provider grants
  `offline_access` once it is selected, with no restart. The plugin's own copy of the provider's discovery document is
  cached for 10 minutes only.
- **`email_verified: false` from Authentik's default mapping.** Harmless here (see §2), but decide before the first
  login whether you want verified addresses.
- **The application binding is not re-evaluated on refresh grants.** Without group sync and `MCP_REQUIRED_GROUPS` (or
  MCP roles on the mapped group) a removed member keeps refreshing until the refresh token expires.
- **A consent skip for trusted clients must not apply to loopback redirect URIs.** It does not (scenario 8).
- **Static tokens die with the re-login rule** when there is no upstream token, although the token row says it is
  valid; the script sees a `401`. The response message and the log line name the reason (`relogin_required`).

Found in this project:

- **Proxied requests built with host `testserver`.** Tool calls are dispatched to InvenTree's API views through an
  internal request; it used DRF's test host, which a real `ALLOWED_HOSTS` (derived from `SITE_URL`) refuses, so every
  paginated tool failed on real instances while the test suite passed. The internal request now carries the MCP
  request's host, scheme and client address; e2e scenarios 2 and 6 call paginated tools through the proxy.
- **`approval_prompt=auto`.** DOT skips the consent page for any client the user already holds a live token for
  (from the query or `REQUEST_APPROVAL_PROMPT`), which would have skipped consent for loopback and self-registered
  clients. The plugin forces `approval_prompt=force`; only its own trusted-client rule skips consent.
- **Revoked refresh tokens inside the grace period.** DOT's `REFRESH_TOKEN_GRACE_PERIOD_SECONDS` (120 s) also applied
  to tokens revoked through `/oauth/revoke/` or by a block, so a revoked token could mint a new pair for two minutes
  and skip the membership gate. The plugin re-serves only rotated tokens inside the window (a successor exists) and
  refuses a revoked one at once.
- **DOT settings injection is per process.** Lifetimes and CIMD settings live in DOT's in-memory settings of each
  web and background worker process. The plugin applies them at start-up in every process and re-syncs on requests,
  before every django-q task and after a settings save; a process that never did either would have kept InvenTree's
  values.
- **Plugin pages need a newer InvenTree.** Plugin routes arrived after 1.5.7 (core PR #12900); on 1.5.7 the GUI pages
  and navigation entries are left out and everything else works.
- **Claude Code posts to `/mcp` without the trailing slash.** Before the plugin added the slash-less route, that
  request missed the view and got InvenTree's bare `401` without the challenge.
- **1.5.x `/.well-known/`.** InvenTree answered the RFC 8414 path-insertion URL with its HTML login page (status 200);
  Claude Code gave up parsing it. The proxy rewrites fix it until a release with plugin `/.well-known/` support.
- **DCR registration tokens dated year 9999.** DOT's default expiry for the registration access token overflowed in a
  PostgreSQL session east of UTC (`timestamp too large`) and broke the stale-client cleanup; the plugin injects a one
  day expiry.
- **First SSO login 500 on InvenTree 1.5.x** (core PR #12842, see §3).
- **Gunicorn workers and plugin settings.** Workers started before `ENABLE_PLUGINS_URL` was on served no plugin URLs
  (about one request in four failed); restart after switching plugin integration settings.
- **Reverse relations from core models.** InvenTree registers plugin apps after core's relation tree is built, so
  `User.objects.filter(mcp_state__…)` raises `FieldError` in running processes while tests and `manage.py shell`
  hide it. Only relevant if you write code against the plugin's models.
- **Codes in access logs.** Found by e2e scenario 9: InvenTree's gunicorn access log and its INFO request log hold
  the provider's codes of the SSO callback, and Caddy's access log held the plugin's own codes in the logged
  `Location` response header although the request query was filtered. See §7.
