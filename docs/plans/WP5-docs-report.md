# WP5 docs report

## 1. dev/Caddyfile
Added `resp_headers>Location query { delete code; delete state }` inside `:80` > `log` > `format filter` (next to the `request>uri` filter).

- `caddy validate`: `Valid configuration`.
- `dev/compose.sh restart proxy`, 8 s wait.
- Scenario 9 reads tokens and codes minted by earlier scenarios (SECRETS), so I ran `python3 dev/e2e/run.py --skip-outage` (full run).

Scenario 9 lines:

```
[9] grepping logs since 2026-10-15T06:55:20Z for 4 addresses and 36 secrets (lengths [30, 32, 53, 128, 1794, 1820, 1832]; not printed)
PASS 9 inventree-server+inventree-worker: log of this run is not empty  (1263 lines)
PASS 9 inventree-server+inventree-worker: no test user e-mail address
[9] ... 9 x core django_structlog request lines 'GET /accounts/authentik/login/callback/?code=<secret ...>&state=...' (expected)
PASS 9 inventree-server+inventree-worker: none of the run's tokens and codes (...; Authentik codes outside core's request log)  (36 checked)
WARN 9 inventree-server+inventree-worker: 9 Authentik authorization code(s) in InvenTree core's request/access log lines of the SSO callback. Not the plugin: ...
PASS 9 the plugin did log during the run (the grep is not vacuous)  (46 lines)
PASS 9 proxy: log of this run is not empty  (148 lines)
PASS 9 proxy: no test user e-mail address
PASS 9 proxy: none of the run's tokens and codes (...)  (36 checked)
PASS 9 proxy: the Authentik callback lines carry no code (Caddy's log filter strips code and state)  (9 callback lines, 0 with a code)
```

Summary: `PASS` scenarios 1, 2, 3, 5, 6, 7, 8, 9, 10; scenario 4 SKIP (--skip-outage);
`WARN 9 ... 9 Authentik code(s) in core's request/access log`; `9 passed, 0 failed, 1 skipped, 1 warning(s)`.

## 2. Doc changes
README.md:
- Prerequisites, plugin settings bullet: migration note now says migrations `0001` to `0003` exist, `0003` adds `MCPUserState.recheck_lease_until` (backend delta 4).
- Prerequisites, proxy bullet: "registration and token rate limits (... `TOKEN_RATE_LIMIT_PER_MINUTE`) are per client IP" (delta 1).
- Plugin settings, OAuth table: new `TOKEN_RATE_LIMIT_PER_MINUTE` row (60) after the registration row; registration row now mentions 429 with Retry-After (delta 2).
- Membership re-checks: the stale "row lock ... planned hardening" sentence replaced by the 60-second lease description (delta 3).
- GUI: MCP Access is for "staff with the admin role's change permission" (delta 5).
- API table: `scopes` "(empty whenever `may_connect` is false, e.g. for a blocked user)" in `api/effective/` and `api/status/` rows (delta 6).
- Troubleshooting: "429 on registration or the code exchange" with both limits (delta 7); "No MCP / MCP Access pages" adds the missing admin change permission (delta 8).

docs/OPERATIONS.md:
- Prerequisites, Client IP row: "registration and token rate limits".
- Settings table: `TOKEN_RATE_LIMIT_PER_MINUTE` row (default 60, recommended 60) after the registration row.
- Failure modes / log table: row for the "no plugin routes" INFO and the missing-bundle WARNING, once per process (backend delta 9).
- Troubleshooting: "429 on registration or the code exchange" paragraph with both limits.

Already present, not touched (frontend deltas): shadow-flag wording, Connect card API-token variant, "Revocation queued for N members", 1.6.0 wording, patch 3, `ENABLE_PLUGINS_INTERFACE`, "generated and git-ignored" bundles in README; dev/README.md frontend wording, `ui_smoke.py` row/run line, `up.sh` text.
Not in scope: `frontend/README.md` and `docs/plans/00-overview.md` deltas (other owners).

## 3. Cross-checks
- Settings: all 19 keys of `core.py` SETTINGS are in README (plugin settings bullets and OAuth table) and in OPERATIONS' table with the code's defaults. Only `TOKEN_RATE_LIMIT_PER_MINUTE` (60) was missing in both; added. No default mismatch.
- Migration note: `inventree_mcp/migrations/` holds 0001_initial, 0002_mcpruleset, 0003_mcpuserstate_recheck_lease; README states `invoke migrate` plus the 0001 to 0003 detail now; OPERATIONS already says `invoke migrate` after installing or upgrading (unchanged).
- GUI: README says InvenTree 1.6.0+, panel needs `docs/upstream/` patch 3, MCP Access needs staff with the admin role's change permission (fixed; `_is_staff_admin` gates the nav item). OPERATIONS mentions 1.6 for pages and "staff admins" elsewhere; consistent. dev/README.md line 35 still says "MCP Access" for staff (informal, left).

No Python, frontend or test files changed; nothing staged or committed.
