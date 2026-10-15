# WP5: hardening, end-to-end tests, CI, docs, upstream patches

Read first: `docs/plans/00-overview.md`, all WP reports, `KINA/docs/MCP-AUTH-GUIDE.md` §10 and §11, master plan "WP5" and "WP6", `KINA/scripts/e2e/authentik/kina_authentik_e2e.py` (scenario structure to mirror).

## Deliverables

### End-to-end suite against the dev environment (`dev/e2e/`)

One runner `dev/e2e/run.py` (standard library plus `requests` if available in the host python; otherwise stdlib only), scenarios as functions, each printing `PASS`/`FAIL` lines and the runner exiting non-zero on any failure. It drives the real stack brought up by `dev/up.sh`: InvenTree at `http://localhost:8080`, Authentik at `http://authentik.localtest.me:19000`. Reuse `dev/authentik/bootstrap.py` helpers for group changes and the SSO flow driver from `dev/authentik/sso_smoke.py`.

Scenarios (the go-live checklist of the guide, adapted):
1. Discovery: 401 with `WWW-Authenticate` carrying `resource_metadata`; PRM; AS metadata; well-known redirects; metadata CORS headers.
2. Member connects (DCR client, PKCE, SSO login through the bridge, consent, code, token, `tools/list`, one tool call), then refresh with a fresh check (no upstream call: assert `token_secret` unchanged) and refresh with an aged check (age `membership_checked_at` through `dev/shell.sh`; assert `token_secret` rotated).
3. Removed from `inventree-mcp` in Authentik: aged refresh → `invalid_grant`; MCP bearer → 401; authorize → blocked page; add back; new SSO login lifts the block.
4. Authentik stopped (`docker compose stop authentik-server`): aged refresh within grace succeeds, nothing revoked; past grace (age further) → `invalid_grant`; start Authentik; the same refresh token works.
5. `e2e-interactive` (interactive group only): authorize → `no_mcp_permission` page; `e2e-guest`: logs in, no roles, refused; `e2e-nonmember`: refused by Authentik.
6. MCP ruleset narrowing: give `MCP Users` view on parts only; `tools/list` shows part tools only; a stock tool call is denied; interactive API with an API token of the same user can still read stock.
7. Reduction: clear the flag through `api/rulesets/`; the live refresh token is revoked.
8. Untrusted CIMD `client_id` and unregistered redirect URI → error page, never a redirect; loopback redirect keeps consent; trusted non-loopback skips it (simulate by creating a CIMD application row through `dev/shell.sh`).
9. Logs: grep the server log for the test users' e-mail address, tokens and codes; must not appear.
10. Claude Code against the dev instance: `claude mcp add --transport http inventree-dev http://localhost:8080/plugin/inventree-mcp/mcp/` then `claude mcp list` → needs authentication; if a browser session can be driven (playwright), complete the sign-in and run a tool; otherwise document the manual steps for the user to perform.

### Hardening pass (Opus review items, fix in code)

- Rate limiting also on the token endpoint for `authorization_code` guesses? Decide and document (DOT codes are single-use and 10 minutes; a per-IP cap of 60/min is cheap: add it with the same limiter as registration).
- `ensure_applied()` cost on every request (must be a dict comparison, no DB hit when the settings cache is warm; measure).
- Multi-worker correctness: the DOT settings injection runs in gunicorn workers and the django-q worker; confirm the scheduled sweep and the background re-check see `CIMD_ENABLED` etc. (they only need DOT models, but `revoke` paths must not import views).
- `select_for_update` lock duration during the upstream call: cap by the 5 s timeouts; document.
- Error pages leak nothing (no stack traces, no client metadata beyond name/host).
- `clear_expired()` interplay: DOT's cleanup deletes expired refresh tokens; our connections page must not 500 on tokens with a deleted access token.

### CI (`.github/workflows/ci.yaml`)

Run `inventree_mcp` (whole package) instead of `inventree_mcp.test_mcp`; add a frontend job (`npm ci && npm run lint && npm run build`, and fail if the build output differs from the committed `inventree_mcp/static/`); keep the stable/latest matrix.

### Documentation

`README.md` final structure: what it is, install, connect (claude.ai, Claude Desktop, Claude Code, scripts with API tokens), administration (Authentik prerequisites from the guide §2 adapted to InvenTree, InvenTree settings, plugin settings table, two permission sets, membership re-checks and the outcome table, logs and privacy), troubleshooting (the "things that went wrong once" list adapted), development (`dev/`). `docs/OPERATIONS.md`: go-live checklist with the e2e scenario numbers next to each item.

### Upstream patches (prepared, not submitted; `docs/upstream/`)

Patch files against the InvenTree core master clone at `/home/alex.lucaci/Projects/inventree/InvenTree` (do not commit there; produce `git format-patch` output into this repo):
1. `0001-oauth2-provider-config-override.patch`: `oauth2_provider_settings` config block / `INVENTREE_OAUTH2_PROVIDER` env merged into `OAUTH2_PROVIDER`.
2. `0002-preserve-next-through-account-login.patch`: `RedirectView(query_string=True)` on `account_login` and `next` handling in the React login and logged-in pages.
3. `0003-plugin-panels-in-group-and-user-drawers.patch`: `usePluginPanels` in `GroupDrawer`/`UserDrawer` rendering extra accordion items; export `RoleTable` with an `endpoint` prop from `@inventreedb/ui`.
Each with a short cover letter in `docs/upstream/README.md` explaining the motivation and the plugin code it would retire. Also `0004-inventree-mcp-challenge-and-prm.patch` is not needed because this repo is the plugin.

## Acceptance

`dev/e2e/run.py` all PASS on a fresh `dev/up.sh`; unit suite green; CI file updated; docs complete; patches apply cleanly (`git apply --check`) on the core master clone; report `docs/plans/WP5-report.md`; commit `WP5: hardening, e2e suite, docs, upstream patches`.
