# WP-dev report: docker-based dev and test environment

Status: all six acceptance items pass. `dev/.ready` exists. The stack is currently up (`dev/up.sh` was re-run last).
Clean-state `dev/up.sh` (volumes, data/, .env and .bootstrap.json removed first) took 6 min 8 s with images already pulled.

## What was built (all under `dev/`)

`compose.yaml` (project `inventree-mcp-dev`), `inventree.env`, `.env.example` (+ generated git-ignored `.env`), `config/config.yaml.tmpl`, `config/plugins.txt`, `Caddyfile`, `entrypoint.sh`, `_common.sh` (shared helper sourced by the scripts), `authentik/bootstrap.py`, `authentik/sso_smoke.py`, `configure_inventree.py`, `up.sh`, `down.sh [-v]`, `test.sh`, `shell.sh`, `logs.sh`, `README.md`. `.gitignore` got `dev/data/`, `dev/.env`, `dev/.ready`, `dev/authentik/.bootstrap.json`. Nothing under `inventree_mcp/` or `README.md` was touched. (`dev/oauth_smoke.py` belongs to the other agent.) Nothing was added to git.

Published ports: 8080 (proxy, loopback), 8000 (gunicorn, loopback), 8025 (Mailpit, loopback), 19000 (Authentik, all interfaces). Nothing on 5432/80/443.

## Decisions where the spec left a choice

- **Entrypoint wrapper, not init.sh alone.** The 1.5.7 image's `init.sh` only creates directories and config.yaml and never runs `invoke update`, so `plugins.txt` would not be installed. `dev/entrypoint.sh` runs `./init.sh true`, installs `-e` plugin + `django_slowtests==1.1.1` (in server and worker, every start, since a recreated container loses site-packages), runs `invoke update --skip-backup --no-frontend` on the server only (`DEV_RUN_UPDATE=1`), then `exec ./init.sh "$@"`. `config/plugins.txt` is still copied to `data/plugins.txt`, so `invoke update` also installs from it.
- **Demo import task name:** `invoke import-records -c -f <file>` (top-level task, not under `dev`; confirmed with `invoke --list`). Run before `configure_inventree.py` (the import with `-c` wipes settings/groups), and `admin/admin` is re-asserted by the configure script.
- **Demo dataset version:** demo-dataset master is exported from InvenTree 1.6 dev and fails to load into 1.5.7 (`NotesImage has no field named 'note'`). `up.sh` loads `git show c400583^:inventree_data.json` (api 521, last export before the notes migration) from the demo-dataset repo (read-only git command), override with `DEMO_DATASET_REF` (`WORKTREE` = checked-out file, for `INVENTREE_TAG=latest`). Media comes from the working tree (superset).
- **authentik.localtest.me:** resolves on this host (`getent hosts` gives `::1` and 127.0.0.1), so no /etc/hosts edit was needed. `up.sh` checks that it resolves to a loopback address; otherwise it prints the `/etc/hosts` instruction at start and end but continues (bootstrap uses 127.0.0.1). Containers resolve it via `extra_hosts: host-gateway`.
- **`social_backends` added to config.yaml.tmpl.** The spec's template only had `social_providers`; without `social_backends: [allauth.socialaccount.providers.openid_connect]` InvenTree never installs the provider app and the config endpoint lists no providers. `server_url` has no trailing slash (allauth appends `/.well-known/...`; a trailing slash gave `//.well-known` and a 500).
- **Proxy healthcheck sends `Host: localhost`:** InvenTree derives ALLOWED_HOSTS from SITE_URL, so a request with Host `127.0.0.1:9090` gets 400 DisallowedHost.
- **Restart after settings change:** gunicorn workers started before `ENABLE_PLUGINS_URL` was switched on serve no plugin URLs (intermittent 403 "CSRF cookie not set" on `/plugin/...`, about 1 request in 4). `configure_inventree.py` prints `SETTINGS_CHANGED ...` and `up.sh` restarts server and worker when it did; on re-runs nothing changes, so no restart. Likewise a changed `config.yaml` restarts a running stack.
- **Extra settings:** `SIGNUP_GROUP=''` in `configure_inventree.py`, because the demo data names a default sign-up group (`all access`) that would put `e2e-guest` into a group.
- **Client secret stability:** `bootstrap.py` reuses the secret from `.bootstrap.json` on re-runs.
- **Token endpoint:** `GET /api/user/token/` with basic auth in 1.5.7 (POST returns 405), not POST as in the spec.
- **InvenTree 1.5.7 SSO bug, patched in the dev container only:** `CustomSocialAccountAdapter.save_user` sets `_is_registering` on the SocialLogin instead of the User, so the first SSO login of every new user returned 500 (allauth `setup_user_email` assertion; account, groups and token were already saved). `entrypoint.sh` rewrites that one line at start (guarded `grep`/`sed`, no-op on fixed versions; master removed the marker). Without this, acceptance 5 cannot pass on 1.5.7. This is a core bug worth knowing about for WP2 (first login of an SSO user).

## Acceptance

1. **Clean `up.sh`.** `dev/down.sh -v; rm -f dev/.env dev/authentik/.bootstrap.json; dev/up.sh` finished with `exit 0`, steps logged: `generating dev/.env`, `bootstrapping Authentik`, `starting InvenTree server, worker and proxy`, `loading demo data (dataset ref: c400583^)`, `configuring InvenTree`, `settings changed, restarting InvenTree ...`; no manual step, `real 6:08`. Re-running `dev/up.sh` on a running stack is idempotent (no demo reload, no restart, about 1.5 minutes).
2. `curl -s http://localhost:8080/api/` -> `{"server": "InvenTree", ..., "version": "1.5.7", ..., "apiVersion": 530, "worker_running": true, ...}`. `curl -s -u admin:admin 'http://localhost:8080/api/part/?limit=1'` -> `count 438`, first part `1551ABK`.
3. `curl -s -u admin:admin 'http://localhost:8080/api/plugins/?active=true'` -> `[('inventree-mcp', True)]`. `curl -s -w '[HTTP %{http_code}]' http://localhost:8080/plugin/inventree-mcp/mcp/` -> `{"jsonrpc": "2.0", "error": {"code": -32000, "message": "Authentication required. Provide a valid Token, Bearer, or Basic credential."}, "id": null} [HTTP 401]`. With `TOKEN=$(curl -s -u admin:admin http://localhost:8080/api/user/token/ | jq -r .token)`: 8 consecutive `tools/list` calls with `Authorization: Token $TOKEN` -> `200 200 200 200 200 200 200 200`, body lists 56 tools (`describe_filters`, `make_web_link`, `list_attachments`, ...).
4. `dev/test.sh inventree_mcp.test_mcp` -> `Found 132 test(s). Ran 132 tests in 15.685s  OK`, exit 0 (also on the first environment, before any other agent changes landed: same 132 tests, OK). A non-existent label makes `test.sh` exit 1.
5. `curl -s http://localhost:8080/api/auth/v1/config` -> `socialaccount.providers[0].id == "authentik"` (`openid_configuration_url: http://authentik.localtest.me:19000/application/o/inventree/.well-known/openid-configuration`). `python3 dev/authentik/sso_smoke.py` (exit 0):
   ```
   PASS e2e-member: round trip ends back on InvenTree  (page 200 http://localhost:8080/web/logged-in)
   PASS e2e-member: GET /api/user/me/ with the session cookie  (200 username=e2e-member)
   PASS e2e-member: InvenTree groups == ['MCP Users', 'Users']
   PASS e2e-member: refresh token stored in socialaccount_socialtoken
   PASS e2e-interactive: InvenTree groups == ['Users']
   PASS e2e-guest: GET /api/user/me/ with the session cookie  (200 username=e2e-guest)
   PASS e2e-guest: InvenTree groups == []  ([])
   PASS e2e-nonmember: refused by Authentik  (authentik-denied 200)
   PASS e2e-nonmember: no InvenTree user created  (None)
   ```
   The script follows the allauth headless flow (`POST /api/auth/v1/auth/provider/redirect`, CSRF cookie) into Authentik, drives identification + password via the flow executor API, follows back to InvenTree and calls `/api/user/me/`; group and `token_secret` are read through `manage.py shell`.
6. `dev/down.sh` -> exit 0, `docker ps -a | grep -c inventree-mcp-dev` = 0, volumes `pgdata` and `authentik-db` and `data/` kept (stopped only). `dev/down.sh -v` -> exit 0, afterwards 0 containers, `docker volume ls | grep -c inventree-mcp-dev` = 0, `dev/data/` gone (removed through a container because the files are root-owned), `.bootstrap.json` removed; `dev/.env` is kept deliberately (the next `up.sh` reuses it). Stack was then brought up again.

## Known limitations

- Needs network on first start (image pulls, `pip install` of the plugin dependencies and `django_slowtests` on every container creation).
- The 1.5.7 SSO patch is applied by `sed` at container start (dev only). The demo dataset ref `c400583^` is hard-coded (overridable).
- Python edits to the plugin need a server restart (editable install, gunicorn does not reload); tests do not.
- `dev/data/` is root-owned content; remove it via `dev/down.sh -v`, not `rm`.
- `INVENTREE_DEBUG=True` opens CORS to all origins (dev only). Mailpit receives mail but no scripted check reads it.
- The worker container has no healthcheck (upstream one needs 180 s start period); it only has to be started.
- Authentik is published on all interfaces (required for host-gateway), so other hosts on the LAN can reach the dev Authentik; its secrets are random per `.env`, test user password is fixed.

## Day-to-day commands

```bash
dev/up.sh                          # start / refresh everything (idempotent)
dev/test.sh                        # all plugin tests (label inventree_mcp)
dev/test.sh inventree_mcp.test_mcp # upstream suite
dev/test.sh inventree_mcp.tests.test_auth_discovery -v 2   # one module, extra args pass through
dev/shell.sh                       # Django shell
dev/logs.sh inventree-server       # logs
python3 dev/authentik/sso_smoke.py # SSO round trip
docker compose -f dev/compose.yaml restart inventree-server inventree-worker   # pick up plugin code changes
dev/down.sh                        # stop
dev/down.sh -v                     # stop and wipe
claude mcp add --transport http inventree-dev http://localhost:8080/plugin/inventree-mcp/mcp/
```
Accounts: InvenTree `admin/admin`; Authentik `akadmin` + password in `dev/.env`; SSO users `e2e-member`, `e2e-interactive`, `e2e-guest`, `e2e-nonmember` with `dev-password-123`.

## Review fixes applied

All 12 findings and the Duplication section of `WP-dev-review.md` were applied. Nothing was restarted or recreated, `up.sh`/`down.sh` were not run, `dev/oauth_smoke.py` was not touched, nothing was staged or committed. "Verified" below was done against the running stack without touching containers; "PENDING" must be verified by the orchestrator after the next recreate or `dev/up.sh`.

1. **SSO driver reusable** (`dev/authentik/sso_smoke.py`): `browse(..., stop=callable)` returns `("stopped", url, status, "")` when the next redirect target matches (also after an Authentik flow); new `sso_login(username, password=DEV_PASSWORD, browser=None) -> Browser` (start_login + browse + checks, raises `SsoRefused`); `scenario()` now calls `sso_login`. Verified: `python3 dev/authentik/sso_smoke.py` 12/12 PASS, exit 0 (the "InvenTree redirects into Authentik" check now lives inside `sso_login`). `dev/oauth_smoke.py` should switch to `Browser`, `browse(stop=...)`, `sso_login` in the WP1 review round (put `dev/authentik` on `sys.path`).
2. **Group helper** (`dev/authentik/bootstrap.py`): `load_env`, `api_from_env`, `user_pk`, `group_pk`, `user_groups`, `set_user_groups`; subcommands `groups USER`, `set-groups USER [GROUP...]`, `dev-password`, `public-url`; plain run keeps doing the full bootstrap; docstring and README say a re-run restores memberships. Verified: `set-groups e2e-member inventree-users inventree-mcp` (the value it already had) printed `e2e-member: inventree-mcp, inventree-users`; `groups e2e-nonmember` empty; `py_compile` ok. The full bootstrap was not re-run.
3. **Root-owned files**: `inventree.env` gets `PYTHONDONTWRITEBYTECODE=1` (PENDING: takes effect when the containers are recreated); new `dev/manage.sh` (runs manage.py in the server, then `fix_ownership`, preserves the exit status); `fix_ownership` in `dev/_common.sh` (chowns everything under the plugin checkout not owned by the host uid, excluding `.git`); also called at the end of `dev/test.sh` and, via `--reference` to the checkout owner, in `dev/entrypoint.sh` after `pip install -e` (entrypoint part PENDING). README section "Files owned by root". Verified: `dev/manage.sh check` -> "System check identified no issues", rc 0; the root-owned egg-info, `__pycache__` dirs and files were chowned back now (`find . -user root` outside `dev/data` was empty; `inventree_mcp.egg-info/PKG-INFO` was root-owned again a few seconds later because the other agent's test run or pip rewrote it, run `bash -c 'source dev/_common.sh; fix_ownership'` once the tests finish).
4. **down.sh -v order** (`dev/down.sh`): `dc down -v --remove-orphans` first, then the wipe in a one-off container and `rmdir data`, then `dc down --remove-orphans` to drop the re-created network. PENDING (would stop the stack; `bash -n` only).
5. **Authentik not on the LAN** (`dev/compose.yaml`, `dev/up.sh`): `authentik-server` gets `AUTHENTIK_LISTEN__HTTP: 0.0.0.0:19000`, network alias `authentik.localtest.me`, port `127.0.0.1:19000:19000`; `extra_hosts` removed from `x-inventree`; the Authentik environment moved to a top-level `x-authentik-env` anchor shared by server and worker; `AUTHENTIK_PORT` dropped (the port is fixed at 19000, also from `.env.example` and the generated `.env`). `up.sh`: host resolution check now uses `getent ahosts` and requires an IPv4 127.0.0.1 answer, and after the InvenTree restart step it warns if the InvenTree container cannot fetch the OIDC discovery document. A FALLBACK comment in `compose.yaml` explains the healthcheck override (probe `127.0.0.1:19000/-/health/live/`) if `ak healthcheck` ignores the listen setting, and how to revert. Verified: `dev/compose.sh config` renders (listen var, alias, single published port 19000). PENDING: the container recreate, healthcheck behaviour, host and in-container name resolution, and the SSO smoke afterwards.
6. **SSO patch gate** (`dev/entrypoint.sh`, `dev/inventree.env`, `dev/README.md`): `DEV_PATCH_SSO_SIGNUP` (default 1) gates the patch; it logs "patch not needed" when the line is absent and "left unpatched" when disabled (with a hint to recreate if an earlier start already patched it). README Known issues cite InvenTree PR #12842 and the production behaviour. PENDING (entrypoint runs at container start). Not done, outside my scope: the WP5 addition (run `sso_smoke.py` once with the patch off for a fresh user, document the production 500 in the plugin README troubleshooting).
7. **Restart once** (`dev/up.sh`): `restart_inventree()` and `need_restart`, set by a changed `config.yaml` (only if the server was already running) or by `SETTINGS_CHANGED`, executed once after `configure_inventree.py`. PENDING (`bash -n` only).
8. **Round-trip check** (`dev/authentik/sso_smoke.py`): `sso_login` requires status 200 and path `/web/logged-in`. Verified by the smoke run.
9. **Logs** (`dev/logs.sh`, `dev/Caddyfile`, README): `dev/logs.sh --dump [service]` (no follow, no prefix, no colour). The proxy access log filter deletes the `code` and `state` query parameters. Verified: `dev/logs.sh --dump proxy | tail` returns; `caddy validate` on the new Caddyfile in a throwaway container says "Valid configuration". PENDING: the filter on the live proxy (needs a proxy restart); README states that the secrets grep covers inventree-server/worker.
10. **test.sh flag as label** (`dev/test.sh`): label only if the first argument does not start with `-`. Verified: `dev/test.sh -v 2` ran `manage.py test inventree_mcp --keepdb -v 2` (see the incident note below).
11. **umask** (`dev/up.sh`): `.env` is written in a `( umask 077; ... )` subshell. PENDING (only runs when `.env` is missing; `bash -n`).
12. **Markers and numbers**: both sizes now say ~2 GB; the `dev/.ready` entry removed from `.gitignore` and the empty `dev/.ready` file deleted.

**Duplication**: `dev/compose.sh` is the single compose invocation (dummy secrets only when `.env` is missing); `dc` in `_common.sh` calls it; `sso_smoke.py` no longer invokes compose and uses `dev/shell.sh -c` instead. `shell.sh` now uses `dcx` (verified: `dev/shell.sh -c "print('hello', 1+1)"` prints `hello 2` without a TTY). `require_running` in `_common.sh` is used by `test.sh`, `shell.sh`, `manage.sh`; `logs.sh` has its own "no containers" message. The dev password, public URL and the user table live only in `bootstrap.py` (`sso_smoke` imports `DEV_PASSWORD`, `USERS`, `public_url`; `up.sh` reads them through `bootstrap.py dev-password|public-url`); `SSO_GROUP_MAP` is now a literal in `configure_inventree.py`, which `sso_smoke.expected_groups()` reads with `ast` (verified: `[['MCP Users','Users'],['Users'],[],[]]`). The restart block is deduplicated (finding 7). The KINA copies stay as accepted.

**Incident, please note**: while checking finding 10 I started `dev/test.sh -v 2` and killed only the client; its `manage.py test inventree_mcp --keepdb -v 2` kept running in the container for about 12 s, overlapping the other agent's run on the same `--keepdb` test database. I killed my process (pid 1669) and left the other one (pid 1237) running. If the other agent saw odd failures around 05:34 local time (02:34 UTC), rerun them.

Verification still needed from the orchestrator after the next recreate: `dev/up.sh` (or `dev/compose.sh up -d --force-recreate`), `authentik-server` healthy, `getent ahosts authentik.localtest.me` in the InvenTree container shows the compose network address, `python3 dev/authentik/sso_smoke.py`, `ss -ltn` shows 19000 on 127.0.0.1 only, no `__pycache__` appears as root, `DEV_PATCH_SSO_SIGNUP=0` run (WP5), proxy log has no `code=`.
