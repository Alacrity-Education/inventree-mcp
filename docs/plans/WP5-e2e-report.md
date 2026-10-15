# WP5 report: end-to-end suite and operations documentation

Scope of this round (per the orchestrator): `dev/e2e/run.py` (new), `docs/OPERATIONS.md` (new), `README.md`
(restructured), `dev/README.md` (e2e runner, plus the frontend deltas listed in `docs/plans/WP5-frontend-report.md`).
No Python under `inventree_mcp/` and no other dev script was changed. Nothing was staged or committed.

## Result

Against the live dev stack (InvenTree 1.5.7, DOT 3.4.0, Authentik 2026.8; `python3 dev/e2e/run.py`, 3 minutes):
**9 scenarios PASS, scenario 9 FAILs on a real finding, 1 WARN.**

- **Scenario 9 FAIL (proxy log): the plugin's own authorization codes are in Caddy's access log.** The authorize
  endpoint answers consent (and a trusted client's skipped consent) with a `302` to `redirect_uri?code=…&state=…`;
  Caddy's JSON access log records response headers, and `dev/Caddyfile` filters only `request>uri`. Six façade codes
  of the run were found in the `Location` response header. Fix (one block, for the `dev/Caddyfile` owner; checked in a
  throwaway `caddy:alpine` container, the logged `Location` keeps only the path):
  ```
  resp_headers>Location query {
      delete code
      delete state
  }
  ```
  inside the existing `format filter { … }`. With it the scenario should pass; that was not run, because the change was
  not applied (the file is not mine) and the filter was only checked in isolation. The premise "the proxy log strips codes" was only true for request URIs; `docs/OPERATIONS.md` §7 and
  `dev/README.md` now say so and give the production advice.
- **Scenario 9 WARN (server log): Authentik's authorization codes are in InvenTree core's request logging.**
  gunicorn's access log (on by default in InvenTree's container `gunicorn.conf.py`, `accesslog = '-'`, independent of
  the log level) and django-structlog's `request_started`/`request_finished` lines at `INVENTREE_LOG_LEVEL=INFO`
  record `GET /accounts/authentik/login/callback/?code=…&state=…`. That is core, not the plugin, so the runner
  classifies exactly these lines (an Authentik code that appears only in django_structlog or gunicorn access lines of
  the SSO callback) as `WARN`; any other leak, including Authentik codes elsewhere, is `FAIL`. Mitigation documented
  in `docs/OPERATIONS.md` §7: keep `INVENTREE_LOG_LEVEL=WARNING` and set
  `GUNICORN_CMD_ARGS="--access-logformat '%(h)s %(t)s \"%(m)s %(U)s %(H)s\" %(s)s %(b)s'"` (verified with
  `gunicorn --print-config` inside the server container, not on a running server). If the orchestrator prefers FAIL,
  drop the `core_request_log` classification in `scenario_logs`; an upstream fix would be a query-less access-log
  format in `contrib/container/gunicorn.conf.py` and django-structlog's request path without the query string.
- The plugin's own lines (118 in the run) contain no address, token or code; no test user address appears in any
  log.

## Full output of the final run

```
InvenTree 1.5.7 at http://localhost:8080; Authentik at http://authentik.localtest.me:19000; started 06:49:52 UTC
[6] allauth headless login as 'admin' -> 200
[setup] settings for the run: {'OAUTH_ENABLED': True, 'MEMBERSHIP_ENFORCED': True, 'MCP_REQUIRED_GROUPS': 'MCP Users'} (were {'OAUTH_ENABLED': True, 'MEMBERSHIP_ENFORCED': True, 'MCP_REQUIRED_GROUPS': ''})

== scenario 1: discovery - 401 + resource_metadata, PRM, AS metadata, well-known redirects and proxy rewrites, CORS headers.
PASS 1 POST /mcp/ without a token: 401 with resource_metadata  (Bearer realm="InvenTree",scope="g:read r:view:admin r:view:part_category r:view:part r:vie)
PASS 1 POST /mcp/ with an unknown bearer: 401 with error="invalid_token"
PASS 1 POST /mcp (no slash, as Claude Code sends it): 401 with the challenge
PASS 1 protected-resource metadata  (resource=http://localhost:8080/plugin/inventree-mcp/mcp/ issuer=http://localhost:8080/plugin/inventree-mcp scopes=12)
PASS 1 authorization-server metadata  (issuer=http://localhost:8080/plugin/inventree-mcp)
PASS 1 openid-configuration alias under the issuer
PASS 1 path-insertion URL /.well-known/oauth-authorization-server/plugin/inventree-mcp: 200 JSON
PASS 1 path-insertion URL /.well-known/openid-configuration/plugin/inventree-mcp: 200 JSON
PASS 1 path-insertion URL /.well-known/oauth-protected-resource/plugin/inventree-mcp/mcp: 200 JSON
PASS 1 path-insertion URL /.well-known/oauth-protected-resource/plugin/inventree-mcp/mcp/: 200 JSON
SKIP 1 core /.well-known/<name> redirects  (InvenTree 1.5.7 has no plugin /.well-known/ support (inventree/InvenTree#12698, newer than 1.5.x): /.well-known/oauth-authorization-server -> 302 /accounts/login/; the proxy rewrites above stand in)
PASS 1 CORS on oauth-protected-resource: Access-Control-Allow-Origin without and with Origin  (no Origin: *; Origin https://claude.ai: https://claude.ai; Cache-Control: max-age=300)
PASS 1 CORS on oauth-authorization-server: Access-Control-Allow-Origin without and with Origin  (no Origin: *; Origin https://claude.ai: https://claude.ai; Cache-Control: max-age=300)
PASS 1 CORS preflight GET /plugin/inventree-mcp/.well-known/oauth-protected-resource from https://claude.ai  (200, allow-origin=https://claude.ai)
PASS 1 CORS preflight POST /plugin/inventree-mcp/oauth/register/ from https://claude.ai  (200, allow-origin=https://claude.ai)
PASS 1 CORS preflight POST /plugin/inventree-mcp/oauth/token/ from https://claude.ai  (200, allow-origin=https://claude.ai)
PASS 1 CORS preflight POST /plugin/inventree-mcp/oauth/revoke/ from https://claude.ai  (200, allow-origin=https://claude.ai)
PASS 1 CORS preflight POST /plugin/inventree-mcp/mcp/ from https://claude.ai  (200, allow-origin=https://claude.ai)
PASS 1 the 401 of /mcp/ carries CORS headers for a trusted origin

== scenario 2: member - connect, tool calls with OAuth and API token, refresh with a fresh check, refresh with an aged check.
[6] bridge form: POST /api/auth/v1/auth/provider/redirect provider=authentik process=login callback_url=http://localhost:8080/plugin/inventree-mcp/oauth/authorize/
[6] provider redirect -> 302 to http://authentik.localtest.me:19000/application/o/authorize/
[6] Authentik as 'e2e-member' -> back on /plugin/inventree-mcp/oauth/authorize/ (page, 200)
[2] connected through the façade: expires_in=3600 scopes=12
PASS 2 e2e-member connects (DCR, PKCE S256, login bridge, Authentik, consent, code, token)  (expires_in=3600 scopes=12 access_token=<30 chars>)
PASS 2 tools/list with the access token  (56 tools)
[2] OAuth token: tools/call list_parts {'limit': 1} -> isError=False, count=438, rows=1, next='http://localhost:8080/api/part/'
[2] OAuth token: tools/call get_part {'part_id': 82} -> isError=False, pk=82
PASS 2 paginated list_parts and get_part with the OAuth token
[2] API token: tools/call list_parts {'limit': 1} -> isError=False, count=438, rows=1, next='http://localhost:8080/api/part/'
[2] API token: tools/call get_part {'part_id': 82} -> isError=False, pk=82
PASS 2 paginated list_parts and get_part with an InvenTree API token
PASS 2 refresh with a fresh check: 200, no upstream call (upstream token unchanged)  (200; last check 10 s ago; rotated: True)
PASS 2 the new access token works, the rotated one is refused
PASS 2 refresh with an aged check (2 h): 200, real re-check (upstream token rotated, check stamped)  (200; last check 2 s ago; groups=['MCP Users', 'Users'])

== scenario 3: removed - removed from inventree-mcp in Authentik, blocked; back in the group, an SSO login lifts the block.
[6] bridge form: POST /api/auth/v1/auth/provider/redirect provider=authentik process=login callback_url=http://localhost:8080/plugin/inventree-mcp/oauth/authorize/
[6] provider redirect -> 302 to http://authentik.localtest.me:19000/application/o/authorize/
[6] Authentik as 'e2e-member' -> back on /plugin/inventree-mcp/oauth/authorize/ (page, 200)
[3] connected through the façade: expires_in=3600 scopes=12
PASS 3 e2e-member removed from inventree-mcp in Authentik  (['inventree-users'])
PASS 3 aged refresh: 400 invalid_grant  (400)
PASS 3 the user is blocked, every MCP token revoked, the upstream token deleted  (reason='not_in_required_groups' groups=['Users'] live MCP refresh tokens=0)
PASS 3 MCP bearer of the blocked user: 401 with the challenge  (401)
PASS 3 authorize after a new SSO login outside the group: 403 'Access revoked' page, no redirect  (403 'Access revoked · InvenTree')
PASS 3 back in inventree-mcp: a new SSO login lifts the block  (groups=['MCP Users', 'Users'])
PASS 3 authorize shows the consent page again  (200)

== scenario 4: outage - Authentik stopped: grace, then invalid_grant, nothing revoked; back up, the same refresh token works.
[6] bridge form: POST /api/auth/v1/auth/provider/redirect provider=authentik process=login callback_url=http://localhost:8080/plugin/inventree-mcp/oauth/authorize/
[6] provider redirect -> 302 to http://authentik.localtest.me:19000/application/o/authorize/
[6] Authentik as 'e2e-member' -> back on /plugin/inventree-mcp/oauth/authorize/ (page, 200)
[4] connected through the façade: expires_in=3600 scopes=12
[C] stopping authentik-server
[C] Authentik down, last check 2 h ago (grace 4 h): refresh -> 200; last_error='connection_error'
[C] Authentik down, last check 5 h ago: refresh -> 400 invalid_grant; blocked=False live MCP refresh tokens=1
[C] tools/list with the current access token -> 200 (nothing revoked)
[C] starting authentik-server
[C] Authentik back: the same refresh token -> 200 (attempt 11); checked 2 s ago
PASS 4 grace period, invalid_grant past it, nothing revoked, recovery with the same refresh token
PASS 4 tools/list with the token issued after the recovery  (200)

== scenario 5: non-members - interactive-only and guest users are refused by the plugin, the non-member by Authentik.
PASS 5 e2e-interactive: anonymous authorize goes to the login bridge
PASS 5 e2e-interactive: the login bridge renders the SSO form
PASS 5 e2e-interactive: signs in through Authentik, back on authorize: 403 'No MCP access', no redirect  (page 403 'No MCP access · InvenTree')
PASS 5 e2e-interactive: InvenTree user with groups ['Users'] (interactive roles only / none)  (['Users'])
PASS 5 e2e-guest: anonymous authorize goes to the login bridge
PASS 5 e2e-guest: the login bridge renders the SSO form
PASS 5 e2e-guest: signs in through Authentik, back on authorize: 403 'No MCP access', no redirect  (page 403 'No MCP access · InvenTree')
PASS 5 e2e-guest: InvenTree user with groups [] (interactive roles only / none)  ([])
PASS 5 e2e-nonmember: anonymous authorize goes to the login bridge
PASS 5 e2e-nonmember: the login bridge renders the SSO form
PASS 5 e2e-nonmember: refused by Authentik  (authentik-denied 200 'Permission denied')
PASS 5 e2e-nonmember: no InvenTree user exists

== scenario 6: narrowing - MCP view on parts only: part tools only, stock refused on MCP, stock still readable on /api/.
PASS 6 'MCP Users' MCP roles set to view on parts only (plugin API)  (['part'])
[6] bridge form: POST /api/auth/v1/auth/provider/redirect provider=authentik process=login callback_url=http://localhost:8080/plugin/inventree-mcp/oauth/authorize/
[6] provider redirect -> 302 to http://authentik.localtest.me:19000/application/o/authorize/
[6] Authentik as 'e2e-member' -> back on /plugin/inventree-mcp/oauth/authorize/ (page, 200)
[6] connected through the façade: expires_in=3600 scopes=2
PASS 6 the token carries the MCP scopes only  ('g:read r:view:part')
PASS 6 tools/list: part tools, no stock tool  (16 tools)
PASS 6 tools/call list_stock_items is refused
[6] OAuth token: tools/call list_parts {'limit': 1} -> isError=False, count=438, rows=1, next='http://localhost:8080/api/part/'
[6] OAuth token: tools/call get_part {'part_id': 82} -> isError=False, pk=82
PASS 6 the same user's API token still reads /api/stock/  (count=1278)
PASS 6 the API token on /mcp/ sees exactly the OAuth tool list
PASS 6 the API token's stock tool call on /mcp/ is refused
[6] cleanup: MCP roles of 'MCP Users' restored

== scenario 7: reduction - clearing an MCP flag revokes the members' live refresh tokens at once.
[6] bridge form: POST /api/auth/v1/auth/provider/redirect provider=authentik process=login callback_url=http://localhost:8080/plugin/inventree-mcp/oauth/authorize/
[6] provider redirect -> 302 to http://authentik.localtest.me:19000/application/o/authorize/
[6] Authentik as 'e2e-member' -> back on /plugin/inventree-mcp/oauth/authorize/ (page, 200)
[7] connected through the façade: expires_in=3600 scopes=12
PASS 7 PATCH api/rulesets/<part>/ can_view=false revokes the live MCP refresh tokens at once  (2 -> 0)
PASS 7 refresh with the revoked token: invalid_grant
PASS 7 the access token is refused  (401)
PASS 7 the user is not blocked (a reduction is not an identity refusal)
[7] cleanup: MCP roles of 'MCP Users' restored

== scenario 8: clients - untrusted client / unregistered redirect -> error page; trusted CIMD: loopback consent, https skip.
PASS 8 untrusted https client_id: error page, never a redirect  (400 'Unknown client · InvenTree')
PASS 8 registered client with an unregistered redirect URI: error page, never a redirect  (400 'Unknown client · InvenTree')
PASS 8 trusted CIMD client, loopback redirect URI: the consent page is still shown  (200, redirect_uri=http://127.0.0.1:43123/callback)
PASS 8 trusted CIMD client, https redirect URI: consent skipped, code sent to the redirect URI  (302 to https://claude.ai/api/mcp/auth_callback)
PASS 8 the trusted client's code exchanges (public client, PKCE) and its token works on /mcp/  (token 200, tools/list 200)
[8] cleanup: simulated CIMD row deleted (3 rows incl. grants and tokens)

== scenario 9: logs - no test user address, token or code in the server, worker and proxy logs of this run.
[9] grepping logs since 2026-10-15T06:49:52Z for 4 addresses and 44 secrets (lengths [30, 32, 53, 128, 1794, 1820, 1832]; not printed)
PASS 9 inventree-server+inventree-worker: log of this run is not empty  (1681 lines)
PASS 9 inventree-server+inventree-worker: no test user e-mail address
[9] inventree-server+inventree-worker: sha256:9de0d6fa23 in 5 line(s):                [django_structlog.middlewares.request] ip=172.23.0.1 request='GET /accounts/authentik/login/callback/?code=<secret sha256:9de0d6fa23 len=32>&state=OZ2rWJBpJUyiHrYw' request_id=004cd21b-5374-4138-ae0a-33f9c3171f8b user_agent=Python-urllib/3.14 us
[9] inventree-server+inventree-worker: sha256:f6bd9e9533 in 5 line(s):                [django_structlog.middlewares.request] ip=172.23.0.1 request='GET /accounts/authentik/login/callback/?code=<secret sha256:f6bd9e9533 len=32>&state=THs8D6gqCO4Q7Bpd' request_id=547751c8-572f-48a7-98f9-22e69cd5d771 user_agent=Python-urllib/3.14 us
[9] inventree-server+inventree-worker: sha256:1a40e59cf4 in 5 line(s):                [django_structlog.middlewares.request] ip=172.23.0.1 request='GET /accounts/authentik/login/callback/?code=<secret sha256:1a40e59cf4 len=32>&state=LiyhXnIuaoBfetbQ' request_id=d2e0c8b9-be95-4c90-b18e-db55276fa766 user_agent=Python-urllib/3.14 us
[9] inventree-server+inventree-worker: sha256:2978a6c49d in 5 line(s):                [django_structlog.middlewares.request] ip=172.23.0.1 request='GET /accounts/authentik/login/callback/?code=<secret sha256:2978a6c49d len=32>&state=RVSGD6vxFSs6bof6' request_id=aafdf150-3fae-4ee7-be74-7dcc9ef53d17 user_agent=Python-urllib/3.14 us
[9] inventree-server+inventree-worker: sha256:ebcac124f1 in 5 line(s):                [django_structlog.middlewares.request] ip=172.23.0.1 request='GET /accounts/authentik/login/callback/?code=<secret sha256:ebcac124f1 len=32>&state=e7PftmbxRdGEKV2o' request_id=a4cbc7e2-5ba3-4f07-8c80-2cb78382737c user_agent=Python-urllib/3.14 us
[9] inventree-server+inventree-worker: sha256:a003350dbc in 5 line(s):                [django_structlog.middlewares.request] ip=172.23.0.1 request='GET /accounts/authentik/login/callback/?code=<secret sha256:a003350dbc len=32>&state=fBhEMTKDHBAC0axV' request_id=ad6c47ca-7797-4782-b102-ca9bedb03fb7 user_agent=Python-urllib/3.14 us
[9] inventree-server+inventree-worker: sha256:ebfb7380ec in 5 line(s):                [django_structlog.middlewares.request] ip=172.23.0.1 request='GET /accounts/authentik/login/callback/?code=<secret sha256:ebfb7380ec len=32>&state=z5Ad0qxk4qK4WNCE' request_id=a5c9bcbd-3582-44c2-9ec9-1c98c5931642 user_agent=Python-urllib/3.14 us
[9] inventree-server+inventree-worker: sha256:254239e921 in 5 line(s):                [django_structlog.middlewares.request] ip=172.23.0.1 request='GET /accounts/authentik/login/callback/?code=<secret sha256:254239e921 len=32>&state=zoviZouYaNTn8Nba' request_id=c22f8e8d-32e0-43e5-a87e-81f2e91e2647 user_agent=Python-urllib/3.14 us
[9] inventree-server+inventree-worker: sha256:7637c90f1f in 5 line(s):                [django_structlog.middlewares.request] ip=172.23.0.1 request='GET /accounts/authentik/login/callback/?code=<secret sha256:7637c90f1f len=32>&state=39LlK79RqAvzG1At' request_id=85047f0e-d6dd-4d54-a925-3a2dd29c5aef user_agent=Python-urllib/3.14 us
[9] inventree-server+inventree-worker: sha256:38eae7b105 in 5 line(s):                [django_structlog.middlewares.request] ip=172.23.0.1 request='GET /accounts/authentik/login/callback/?code=<secret sha256:38eae7b105 len=32>&state=MgH2IOHmfOqXJXz4' request_id=d9add6f2-fa3a-4ac9-b48e-42144368a969 user_agent=Python-urllib/3.14 us
PASS 9 inventree-server+inventree-worker: none of the run's tokens and codes (MCP access/refresh tokens, MCP authorization codes, upstream tokens, API tokens; Authentik codes outside core's request log)  (44 checked)
WARN 9 inventree-server+inventree-worker: 10 Authentik authorization code(s) in InvenTree core's request/access log lines of the SSO callback. Not the plugin: InvenTree's gunicorn access log (on by default in the container image) and, at INVENTREE_LOG_LEVEL=INFO (the dev stack), django_structlog's request lines record the SSO callback's query string. The code is single use, short-lived and useless without the OIDC client secret. Mitigation in docs/OPERATIONS.md (logging).
PASS 9 the plugin did log during the run (the grep is not vacuous)  (118 lines)
PASS 9 proxy: log of this run is not empty  (172 lines)
PASS 9 proxy: no test user e-mail address
[9] proxy: sha256:4b43212aee in 1 line(s): "],"X-Inventree-Reqid":["ac8d1da7-ec79-4eab-940e-cfb8fdbb8bd8"],"Location":["https://claude.ai/api/mcp/auth_callback?code=<secret sha256:4b43212aee len=30>&state=LRzIuLFooCE"],"X-Frame-Options":["DENY"],"Content-Language":["en"],"X-Inventree-Version":["1.5.7"]
[9] proxy: sha256:8e2329ee23 in 1 line(s): "],"X-Inventree-Version":["1.5.7"],"X-Content-Type-Options":["nosniff"],"Location":["http://127.0.0.1:43123/callback?code=<secret sha256:8e2329ee23 len=30>&state=oc14gGLIrDg"],"X-Inventree-Reqid":["84a234ce-0b37-49db-9e2b-6c1d2b513099"],"X-Inventree-Api":["530
[9] proxy: sha256:f217291771 in 1 line(s): ser_id":"","duration":0.032682149,"size":0,"status":302,"resp_headers":{"Location":["http://127.0.0.1:43123/callback?code=<secret sha256:f217291771 len=30>&state=2Wt2SVM6w3o"],"X-Inventree-Reqid":["4ba12de8-14e1-4f0b-b0b5-c804df0b8c15"],"Vary":["Authorization,
[9] proxy: sha256:c841eed81e in 1 line(s): same-origin"],"Vary":["Authorization, origin, Accept-Language, Cookie"],"Location":["http://127.0.0.1:43123/callback?code=<secret sha256:c841eed81e len=30>&state=YtGjcXvdr84"],"Via":["1.1 Caddy"],"X-Inventree-Python":["3.14.8"],"Server":["gunicorn"],"X-Inventr
[9] proxy: sha256:f1eeffb5e8 in 1 line(s): gin-Opener-Policy":["same-origin"],"X-Content-Type-Options":["nosniff"],"Location":["http://127.0.0.1:43123/callback?code=<secret sha256:f1eeffb5e8 len=30>&state=XSC6b9upH4Y"],"Content-Language":["en"],"Content-Type":["text/html; charset=utf-8"]}}
[9] proxy: sha256:ce53f9b9eb in 1 line(s): GMT"],"X-Inventree-Python":["3.14.8"],"Referrer-Policy":["same-origin"],"Location":["http://127.0.0.1:43123/callback?code=<secret sha256:ce53f9b9eb len=30>&state=HXtTOsWfmKs"],"Vary":["Authorization, origin, Accept-Language, Cookie"],"Via":["1.1 Caddy"],"X-Inv
FAIL 9 proxy: none of the run's tokens and codes (MCP access/refresh tokens, MCP authorization codes, upstream tokens, API tokens; Authentik codes outside core's request log)  (6 leaked)

== scenario 10: claude-code - claude mcp add, claude mcp list reports "Needs authentication", claude mcp remove.
PASS 10 claude mcp add --transport http inventree-dev http://localhost:8080/plugin/inventree-mcp/mcp/  (Added HTTP MCP server inventree-dev with URL: http://localhost:8080/plugin/inventree-mcp/mcp to local config)
PASS 10 claude mcp list: the dev server needs authentication  (inventree-dev: http://localhost:8080/plugin/inventree-mcp/mcp/ (HTTP) - ! Needs authentication)
[10] cleanup: claude mcp remove inventree-dev -> exit 0
manual steps to finish the connection (needs a browser and an interactive Claude Code):
  1. claude mcp add --transport http inventree-dev http://localhost:8080/plugin/inventree-mcp/mcp/
  2. start claude in this directory, run /mcp, select inventree-dev, choose Authenticate
  3. the browser opens InvenTree, goes on to Authentik: sign in as e2e-member / dev-password-123
  4. consent page (always shown: Claude Code receives the code on a loopback port): Approve
  5. /mcp lists inventree-dev as connected; ask "list three parts from InvenTree" (tool list_parts)
  6. claude mcp remove inventree-dev

== restore
[restore] authentik-server started
[restore] Authentik ready
[restore] Authentik groups of the test users
[restore] block state of e2e-member
[restore] MCP roles of 'MCP Users'
[restore] API tokens
[restore] simulated CIMD row
[restore] OAuth clients registered by the run
[restore] plugin settings OAUTH_ENABLED, MEMBERSHIP_ENFORCED, MCP_REQUIRED_GROUPS

== summary
PASS scenario 1 discovery
PASS scenario 2 member
PASS scenario 3 removed
PASS scenario 4 outage
PASS scenario 5 non-members
PASS scenario 6 narrowing
PASS scenario 7 reduction
PASS scenario 8 clients
FAIL scenario 9 logs
PASS scenario 10 claude-code
WARN 9 inventree-server+inventree-worker: 10 Authentik code(s) in core's request/access log
9 passed, 1 failed, 0 skipped, 1 warning(s)
exit=1
```

`[C]` lines come from `membership_e2e.outage()`, `[6] bridge form/provider redirect/Authentik as` lines from
`oauth_smoke.sso_login_through_bridge()` (both reused unchanged). "attempt 11" in scenario 4 is the plugin's 60 s
provider back-off after the outage; `outage()` polls 12 times at 5 s, and because the back-off starts before the
poll loop it always ends inside it, but the margin is small (see gaps).

## Runner

`dev/e2e/run.py` (standard library, host python 3.11+): `--list`, `--only N|NAME` (repeatable), `--skip-outage`,
`--base-url`. Each scenario is a function; checks print `PASS`/`FAIL` (a failed check ends that scenario), whole
scenarios can `SKIP` with a reason; a helper's `expect()`/`sys.exit` is caught as a scenario FAIL; unexpected
exceptions print a short traceback and the next scenario runs. Exit code 1 when any scenario failed. Lint:
`uvx ruff@0.16.0 check --isolated dev/e2e` and `format --isolated --check`: clean.

Set-up: makes sure e2e-member exists in InvenTree (first SSO login on a fresh stack), snapshots the plugin settings
`OAUTH_ENABLED`, `MEMBERSHIP_ENFORCED`, `MCP_REQUIRED_GROUPS`, the Authentik groups of the four test users, the MCP
rulesets of "MCP Users" and e2e-member's block state, then sets `OAUTH_ENABLED=True`, `MEMBERSHIP_ENFORCED=True`,
`MCP_REQUIRED_GROUPS="MCP Users"`. Restore (outer `finally`, each step independent): `authentik-server` started and
ready, Authentik groups, block state (unblock if the run left a block), MCP rulesets (PATCH back what differs), API
tokens named `membership-e2e`, the simulated CIMD row, every DCR client the run registered (deleted with their
grants and tokens), plugin settings. Scenarios 2, 3, 6, 7, 8 also clean up in their own `finally`. Checked after the
run: settings back (`MCP_REQUIRED_GROUPS=''`), 11 MCP view rows on "MCP Users", no CIMD row, no e2e API token, nobody
blocked, Authentik groups as bootstrapped, no `inventree-dev` entry in Claude Code.

Secrets for scenario 9: every HTTP call of the imported helpers goes through `sso_smoke.Browser.request`, so the
runner wraps that method once and harvests codes from `Location` headers and response bodies (Authentik's flow
executor returns its redirect, code included, in JSON), tokens from JSON bodies (`access_token`, `refresh_token`,
`token`), and registered client ids; API token keys and, at scenario 9, the current upstream (Authentik) access and
refresh tokens of the e2e users are added. 44 secrets in the final run, never printed (lengths and
`sha256:` prefixes; a leaked secret's log line is shown with every secret masked).

## Coverage

| # | Spec item | What the runner does | Reused |
|---|---|---|---|
| 1 | 401 + `resource_metadata`; PRM; AS metadata; well-known redirects; metadata CORS | challenge without token, with an unknown bearer (`error="invalid_token"`) and on the slash-less `/mcp`; PRM and AS metadata fields; `openid-configuration` alias; the four RFC 8414/9728 path-insertion URLs through the proxy rewrites; core `/.well-known/<name>` redirects (SKIP on 1.5.7, checked automatically where core redirects to the plugin); `Access-Control-Allow-Origin` on PRM/AS metadata with and without `Origin`, preflights of PRM, register, token, revoke and `/mcp/` from `https://claude.ai`, CORS on the `401` | `Browser`, `oauth_smoke.JSON` |
| 2 | member connects, `tools/list`, tool call; fresh refresh keeps `token_secret`; aged refresh rotates it | plus paginated `list_parts` and `get_part` with the OAuth token and with an API token; old access token refused after rotation | `membership_e2e.connect/state/age_check/create_api_token/delete_api_tokens`, `permissions_e2e.real_tool_calls`, `oauth_smoke.refresh` |
| 3 | removed in Authentik: aged refresh `invalid_grant`, bearer 401, authorize blocked page; add back, SSO login lifts | also asserts block reason, revoked tokens, deleted upstream token, consent shown again | `bootstrap.set_user_groups/user_groups`, `sso_smoke.sso_login`, `membership_e2e` |
| 4 | Authentik stopped: grace, past grace `invalid_grant`, nothing revoked, same refresh token works after start | the whole step is `membership_e2e.outage()` (stops/starts `authentik-server` in a `finally`), then a tool call with the recovered token | `membership_e2e.outage` |
| 5 | e2e-interactive → `no_mcp_permission` page; e2e-guest logs in, no roles, refused; e2e-nonmember refused by Authentik | façade authorize through the login bridge and Authentik for each user; InvenTree groups read back; no InvenTree user for the non-member | `oauth_smoke.register/authorize_url/parse_forms`, `sso_smoke.browse/inventree_state` |
| 6 | MCP view on parts only: part tools only, stock call denied, API token still reads stock | plus token scope `g:read r:view:part`, real part calls, API token's tool list equals the OAuth one and its stock call is refused | `permissions_e2e.Admin/group_pk/rows_by_name/call_tool/stock_with_api_token/mcp_with_api_token/real_tool_calls` |
| 7 | clear the flag through `api/rulesets/`: live refresh token revoked | PATCH part `can_view=false`; live tokens N → 0, refresh `invalid_grant`, access token 401, no block | same |
| 8 | untrusted CIMD `client_id` and unregistered redirect URI → error page, never a redirect; loopback keeps consent; trusted non-loopback skips it (CIMD row via `dev/shell.sh`) | signed-in browser; `https://untrusted.example/…` client → 400 *Unknown client*, no `Location`; DCR client with `https://attacker.example/callback` → same; pre-created CIMD `Application` (`https://claude.ai/oauth/inventree-mcp-e2e-client`, fresh `cimd_expires_at`, so DOT never fetches it): loopback → consent page; `https://claude.ai/api/mcp/auth_callback` → `302` with code and state, code exchange 200, token works on `/mcp/`; row deleted in `finally` | `membership_e2e.shell` |
| 9 | grep server and worker logs for the test users' addresses, tokens, codes, Authentik codes | `dev/logs.sh --dump --since <run start>` for `inventree-server inventree-worker` and, separately, `proxy`; addresses of the four users; every recorded secret; sanity checks (logs not empty, plugin lines present, proxy callback lines without `code=`) | `dev/logs.sh` |
| 10 | `claude mcp add`, `claude mcp list` needs authentication, `claude mcp remove`; SKIP with manual steps without the binary | as specified; also SKIPs (instead of overwriting) when an `inventree-dev` entry already exists; prints the manual sign-in steps either way | |

## Not automated, and why

- **Finishing the Claude Code sign-in (scenario 10).** Authentication runs inside an interactive `claude` session
  (`/mcp` → *Authenticate*) that opens a browser on a loopback callback; there is no non-interactive command for it.
  The runner prints the manual steps.
- **claude.ai / Claude Desktop connectors.** claude.ai must reach the server over the internet; the dev stack is on
  localhost. Manual item 14 in the OPERATIONS checklist.
- **Removing `offline_access` from the provider** (guide §10 item 6). Possible through the Authentik API, but it
  changes the shared provider for every other agent and needs a fresh login without a stored token; covered by unit
  tests (`NoUpstreamTokenTest`). Manual item 13.
- **A real CIMD document fetch.** A trusted host (`claude.ai`) cannot serve a test document; the row is simulated as
  the spec allows. The fetch, validation and host allowlist are DOT's (`HostAllowlistCIMDPermission` refusal is what
  the untrusted check exercises).
- **Core `/.well-known/<name>` redirects.** InvenTree 1.5.7 has none (SKIP line in scenario 1); the check runs by
  itself on an InvenTree with plugin well-known support.
- **CORS refusal of an untrusted origin.** `INVENTREE_DEBUG=True` in the dev stack opens CORS to every origin
  (django-cors-headers echoes any `Origin`), so only the positive cases are asserted.

## Gaps found in the existing helpers

- `oauth_smoke.approve_and_exchange()` keeps the authorization code to itself (returns only the token response), and
  Authentik's codes exist only inside `sso_smoke.browse()`/`run_flow()`; scenario 9 needs them. Worked around by
  wrapping `sso_smoke.Browser.request` in the runner (no copy). A `on_response` hook on `Browser`, or returning the
  code, would be cleaner.
- `oauth_smoke.sso_login_through_bridge()` exits the process unless the authorize page answers 200, so it cannot
  drive the refusal paths of scenario 5. The runner composes the same steps from `register`, `authorize_url`,
  `parse_forms` and `browse` (`bridge_attempt`, about ten lines overlap with the helper's form submission). A
  `expect_status=None` parameter on the helper would remove that.
- The helpers signal failure with `sys.exit` (`expect()`, `membership_e2e.shell()`), and print `FAILED:` lines; the
  runner catches `SystemExit`. Raising an exception would compose better.
- `membership_e2e.USER`, `connect()` and `state()` are hard-wired to e2e-member; fine for these scenarios, but other
  users need their own Django snippets.
- `membership_e2e.outage()` polls the recovery 12 × 5 s, which just covers the plugin's 60 s provider back-off
  ("attempt 11" in every run). Not flaky by construction (the back-off starts before the loop), but a longer loop
  would give margin.
- `membership_e2e.state()` (and every `MCPUserState` query from `dev/shell.sh`) fails while the checkout's models are
  newer than the database: during this round the other agent's `0003_mcpuserstate_recheck_lease` existed on disk
  before it was applied, and a full run failed scenarios 2, 3, 4, 7 with `ProgrammingError: column
  …recheck_lease_until does not exist`. The final run was taken after the migration was applied and the server
  restarted. `dev/README.md` mentions it.
- `dev/Caddyfile` filters the request query only (scenario 9 FAIL above).
- `MCPUserState` and `MCPRuleSet` are not registered in the Django admin, while README said the state was "visible in
  the admin under *MCP user states*"; README now points to `GET api/status/?user=<pk>` and says the models are not in
  the admin. There is no unblock button or endpoint: `docs/OPERATIONS.md` §6 gives the `unblock_user` shell command.

## Documentation

- `docs/OPERATIONS.md`: §1 go-live checklist (15 items, the KINA §10 items adapted, each with its e2e scenario or
  marked manual), §2 Authentik prerequisites (`offline_access`, refresh validity ≥ 30 days, group binding as first
  gate, groups in the ID token, `email_verified` caveat: SSO users are stored unverified and login is unaffected,
  checked on the dev stack), §3 InvenTree prerequisites (SSO and `SCOPE`, group sync settings, `ENABLE_PLUGINS_*`
  with restart, worker and timeout, Redis, CORS, `SITE_URL` and forwarded headers, `X-Forwarded-For`, 1.5.x rewrites,
  1.5.7 first-SSO-login bug and PR #12842, instance-wide DOT lifetimes, logging), §4 plugin settings with production
  values, §5 membership outcome table with the log lines (levels checked against the code), `MCPUserState` fields,
  reason and error codes and what the client sees, plus a table of the other log lines, §6 failure modes (provider
  down, cutting access now, unblocking, daily logouts, Authentik shorter lifetime, instance-wide lifetimes, service
  accounts, the common error pages and statuses), §7 logging and privacy (the two log findings above with
  mitigations), §8 things that went wrong once (KINA §11 adapted, plus testserver host, `approval_prompt`, revoked
  token grace, per-process settings injection, plugin routes only on 1.6+, slash-less `/mcp`, 1.5.x `/.well-known/`,
  DCR registration token year 9999, first SSO login 500, gunicorn workers and plugin settings, reverse relations,
  codes in access logs).
- `README.md`: What it is, Install, Connect (Connect with OAuth with the 1.5.x rewrites as a subsection, so the
  plugin's log hint "README (section 'Connect with OAuth')" stays true; scripts with API tokens; authentication
  methods), Administration (pointer to OPERATIONS, prerequisites, plugin settings), Two permission sets, Membership
  re-checks, Logs and privacy, GUI, Troubleshooting, Development (dev/, e2e runner, AGENTS.md, frontend). Every
  earlier statement is kept except the false "visible in the admin under MCP user states"; the GUI section has the
  deltas from the frontend report (1.6.0 wording, `ENABLE_PLUGINS_INTERFACE`, shadow-flag wording, queued revocation,
  API-token Connect card, patch 3 for the group panel, generated bundles), each checked against `frontend/src` and
  `dev/ui_smoke.py`. Internal anchors checked.
- `dev/README.md`: e2e runner in "Day to day", layout table and a new "End-to-end suite" section (scenarios, flags,
  what it changes and restores, the WARN, the migration caveat); corrected troubleshooting line about the proxy log;
  frontend deltas (1.6.0 wording, `ui_smoke.py` behaviour and `--screenshots`, `up.sh` frontend step).

## For the hardening round

The working tree of the concurrent hardening change (re-check lease, `auth/ratelimit.py`) makes two README sentences
stale once it lands: "A re-check holds a database row lock for the user while it talks to the provider; replacing
that with a short lease is planned hardening" (Membership re-checks) and the rate limit being registration-only
(`REGISTER_RATE_LIMIT_PER_MINUTE` rows, OPERATIONS §4/§6). They describe HEAD and were left as they are.
