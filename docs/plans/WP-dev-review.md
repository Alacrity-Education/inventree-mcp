# WP-dev review: docker-based development and test environment

Reviewed: commit `1bf973b` on `feature/oauth-facade` (`dev/`, `.gitignore`), against `docs/plans/00-overview.md`, `docs/plans/WP-dev-environment.md` and `docs/plans/WP-dev-report.md`. References checked: InvenTree `contrib/container/{Caddyfile,init.sh}` and the image's `init.sh`, KINA `scripts/e2e/authentik/{compose.yaml,bootstrap.py,kina_authentik_e2e.py}`, InvenTree git history for the patched line. The stack was left running and nothing was modified apart from this file.

## Verdict

**Accept with fixes.** All six acceptance items are met, and the two I re-ran pass. Every deviation from the spec is declared and justified, nothing secret is committed, and no port is published on 5432, 80 or 443. There are no blockers. Fix three major items before the stages that depend on them:
- finding 1, before WP1's `oauth_smoke.py` is finalised;
- findings 2 and 3, before WP2 generates its migration and writes its e2e script.

## Findings

1. **major: `dev/authentik/sso_smoke.py:305-323, 366-394`. The SSO driver cannot be reused for an OAuth flow.**
   - KINA's `browse` (`kina_authentik_e2e.py:131-135`) stopped at the MCP client's redirect URI and returned `("client-callback", url, ...)`. The adaptation dropped that stop. A façade authorize flow that ends in a redirect to a loopback `redirect_uri` now makes `browse` GET the client callback, which raises an uncaught `URLError` instead of returning the code.
   - The login sequence (`start_login` + `browse` + checks) only exists inside `scenario()`. That function mixes assertions with control flow and keeps a module-global `FAILED`, so another script has no `login(username) -> Browser` to import.
   - The consequence is already visible: WP1's `dev/oauth_smoke.py:36-66` re-implements `NoRedirect` and a cookie `Session`, and it uses admin password login (its docstring, lines 10-12) instead of SSO.
   - Fix:
     - Add `stop: Callable[[str], bool] | None = None` to `browse`, returning `("stopped", url, status, "")` when it matches.
     - Add `sso_login(username, password=PASSWORD, browser=None) -> Browser`, which raises on refusal or a non-200 final page.
     - Make `scenario()` call `sso_login`.
     - Have `oauth_smoke.py` import `Browser`, `browse` and `sso_login` instead of its own copies.

2. **major: `dev/authentik/bootstrap.py:106-173, 176-190`. There is no helper to change one user's Authentik groups.**
   - WP2's e2e step and WP5 scenario 3 must remove `e2e-member` from `inventree-mcp` and add it back. `bootstrap()` is monolithic: a caller would have to read the token from `dev/.env` itself, build an `Api` and look up user and group pks by hand.
   - Fix: factor out these helpers:
     - `api_from_env()`, which loads `AUTHENTIK_BOOTSTRAP_TOKEN` from `dev/.env` when it is not in the environment;
     - `user_pk(api, username)` and `group_pk(api, name)`;
     - `set_user_groups(api, username, group_names)`.
   - Expose `set_user_groups` as a subcommand (`python3 dev/authentik/bootstrap.py set-groups e2e-member inventree-users`). Plain `bootstrap.py` keeps doing the full bootstrap.
   - Document that re-running the bootstrap restores the intended memberships (lines 141-143), which makes it the reset step for the e2e scenarios.

3. **major: `dev/entrypoint.sh:23`, `dev/test.sh:13`, `dev/shell.sh:5`. Root-owned files appear in the bind-mounted checkout.**
   - `pip install -e` and the test and shell commands all run as root in the container and write into the repo. These paths are currently root-owned in the working tree:
     - `inventree_mcp.egg-info/`
     - `inventree_mcp/__pycache__/`
     - `inventree_mcp/tools/__pycache__/`
     - `inventree_mcp/migrations/__pycache__/`
   - WP2 generates `0001_initial.py` with `makemigrations` inside the container. That file will be root-owned, so the host user cannot edit it or run `ruff format` on it without sudo.
   - Fix:
     - Set `PYTHONDONTWRITEBYTECODE=1` in `inventree.env`.
     - Add `dev/manage.sh` (`dcx inventree-server python src/backend/InvenTree/manage.py "$@"`) that ends with `chown -R "$(id -u):$(id -g)"` on `/home/inventree/plugins/inventree-mcp/inventree_mcp/migrations` and the egg-info, with the host uid/gid passed in from `_common.sh`.
     - Document `dev/manage.sh makemigrations inventree_mcp` in the README.

4. **minor: `dev/down.sh:9-18`. `-v` wipes `data/` while the containers are still running.**
   - It wipes first and runs `dc down -v` afterwards. If a running container re-creates a file in between, `rmdir data` fails under `set -e`. The script then exits before `dc down -v`, which leaves the stack running on an emptied data directory.
   - Fix:
     - Run `dc down -v --remove-orphans` first.
     - Then run the `dc run --rm --no-deps ... find /wipe -mindepth 1 -delete` wipe and `rmdir data`.
     - Finish with `dc down --remove-orphans`, which removes the network the one-off run re-created.

5. **minor: `dev/compose.yaml:14-15, 126`. Authentik is published on all interfaces.**
   - Risk: any host on the LAN reaches the Authentik UI and API on port 19000. The four test users have a fixed password that is published in the repo (`bootstrap.py:37`, `README.md:34`), so a LAN host can sign in to Authentik as them. `akadmin` and the bootstrap token are random per `.env`, and InvenTree listens on loopback only, so a LAN host cannot complete an InvenTree login. This is acceptable on a trusted network and not on shared or public Wi-Fi.
   - Side issue: inside the container, `getent hosts authentik.localtest.me` prints `::1`, the public AAAA record and the container's own loopback. `getent ahosts` and Python give `172.17.0.1` from `extra_hosts`, so the stack works, but the verification command the spec prescribes gives a misleading answer.
   - Fix that removes the exposure and the host-gateway dependency together:
     - Set `AUTHENTIK_LISTEN__HTTP: 0.0.0.0:19000` on `authentik-server`.
     - Give the service the network alias `authentik.localtest.me` on the default network.
     - Publish `127.0.0.1:19000:19000` and drop `extra_hosts` from `x-inventree`.
     - The host and the containers then both use `http://authentik.localtest.me:19000`.
     - Check that the image's healthcheck follows the listen setting.
   - If the current choice is kept, `up.sh` should print a one-line LAN-exposure warning in its summary.

6. **minor: `dev/entrypoint.sh:25-34`. The in-container patch of InvenTree core.**
   - The patch is guarded (exact-line `grep`), idempotent, limited to one line, and documented in the entrypoint, the README (Known issues) and the report. It is semantically correct: `save_user` receives the `SocialLogin`, and `getattr(user, "user", user)` marks the `User`.
   - Upstream:
     - InvenTree 1.5.7 has the buggy line (`git show 1.5.7:.../auth_overrides.py:152`). The marker came from #12839 (`6b1a3b899` on master).
     - Master fixed it in #12842 (`63a434c65`, "alternative approach to fix SSO path").
     - `git tag --contains` lists no tag for either commit, so every released 1.5.x image with the marker has the bug.
   - What it masks: in production on 1.5.7, the first SSO login of every new user ends in a 500 after the user, groups and `SocialToken` are saved, and the session login does not complete. The plugin will meet this in production:
     - WP1's login bridge sends first-time users into that 500;
     - WP2's `user_logged_in` stamp does not run on that first attempt.

     The dev environment can never show either.
   - The patch is also silent when it does not apply.
   - Fix:
     - Gate the patch with `DEV_PATCH_SSO_SIGNUP` (default `1` in `inventree.env`).
     - Log "not needed" when the grep misses.
     - Cite #12842 in the README.
     - Add to WP5: run `sso_smoke.py` once with the patch off for a fresh user, and document the production behaviour in the plugin README troubleshooting.

7. **minor: `dev/up.sh:77-81, 111-115`. Restart logic.**
   - The two blocks are copies of each other.
   - When both `config.yaml` and the settings change in one run, the stack restarts twice.
   - `dc restart` re-runs the whole entrypoint each time (`pip install` over the network plus `invoke update`).
   - The diagnosis in `configure_inventree.py:74-75` is plausible: the image's `gunicorn.conf.py` has `preload_app = True`, so URL patterns are built before `ENABLE_PLUGINS_URL` is switched on. The restart is a valid workaround.
   - Fix: a `restart_inventree()` function. Record `need_restart=1` from either condition and restart once after `configure_inventree.py`.

8. **minor: `dev/authentik/sso_smoke.py:380`. The round-trip check ignores the status.**
   - "round trip ends back on InvenTree" passes for any status, as long as the final URL is on InvenTree. A 500 on `/accounts/authentik/login/callback/`, which is exactly the bug in finding 6, passes this line.
   - Fix: require `status == 200` and `urlparse(url).path == "/web/logged-in"`.

9. **minor: `dev/logs.sh:5`, `dev/Caddyfile:31-33`. Logs are hard to grep.**
   - `logs.sh` always follows (`-f`), so `dev/logs.sh inventree-server | grep ...`, which WP5 scenario 9 needs, never returns.
   - The proxy access log records full request URIs, including upstream authorization codes. Observed: `"uri":"/accounts/authentik/login/callback/?code=5a76699d13…"`.
   - Fix:
     - Add a non-follow mode (`dev/logs.sh --dump [service]` → `dc logs --no-log-prefix "$@"`).
     - Either strip the code in Caddy (`log { format filter { request>uri query { delete code } } }`) or have the README and WP5 state that the secrets grep covers `inventree-server`/`inventree-worker` and that the proxy log carries upstream codes by design.

10. **minor: `dev/test.sh:7-8`. A flag is taken as the label.** `dev/test.sh -v 2` runs label `-v`. Fix: `if [[ $# -gt 0 && $1 != -* ]]; then LABEL=$1; shift; else LABEL=inventree_mcp; fi`.

11. **minor: `dev/up.sh:15-23`. The umask is not restored.** `umask 022` overwrites the caller's umask. Fix: `( umask 077; { ...; } > .env )`.

12. **minor: undocumented markers and wrong numbers.**
    - The README (`dev/README.md:10`) says the first run pulls about 2 GB; `up.sh:51` says about 1 GB.
    - `dev/.ready` is ignored (`.gitignore:167`) and mentioned in the report, but no script creates or reads it.
    - Fix: correct one of the two sizes. Either delete the `.ready` ignore entry or document who writes the marker.

### Spec deviations

| # | Deviation | Declared in report | Justified |
|---|---|---|---|
| 1 | `social_backends` added to `config.yaml.tmpl`; `server_url` without trailing slash | yes | yes (provider not installed otherwise; `//.well-known` 500) |
| 2 | API token via `GET /api/user/token/`, not POST | yes | yes (1.5.7 returns 405 on POST) |
| 3 | Demo data from `git show c400583^:inventree_data.json`, `import-records -c` | yes | yes (master export fails on 1.5.7) |
| 4 | Entrypoint wrapper; `invoke update` on every server start | yes (the spec allowed this path) | yes; cost noted in finding 7 |
| 5 | One-line in-container patch of core `auth_overrides.py` | yes | yes for the acceptance tests; see finding 6 |
| 6 | Extra settings: `SIGNUP_GROUP=''`; admin password re-asserted | yes | yes (demo data's sign-up group; demo import replaces admin) |
| 7 | Server/worker restart after settings or config change | yes | yes; see finding 7 |
| 8 | Proxy healthcheck sends `Host: localhost`; worker has no healthcheck | yes | yes |
| 9 | `down.sh -v` keeps `dev/.env` | yes | yes (`up.sh:10-12` guards the opposite mismatch) |
| 10 | `dev/_common.sh` and `dev/.ready` added; `.gitignore` also lists `dev/.ready` | listed, purpose of `.ready` not explained | `_common.sh` yes; `.ready` see finding 12 |
| 11 | `test.sh` uses `bash -c` rather than `bash -lc` | no | harmless |
| 12 | `wait_ready` runs inside `bootstrap.py`, not as a separate `up.sh` step 2 | no | equivalent |

All spec deliverables are present. The services, images, published ports, `inventree.env` values, Authentik data (groups, users, scope mapping, provider settings, application, `any` bindings) and the `configure_inventree.py` settings and groups match the spec.

## Duplication

- **Compose invocation:** the `docker compose -f ... --project-directory ...` call with dummy secrets lives in `_common.sh:14-21` (`dc`) and again in `sso_smoke.py:357-361`, which copies `PG_PASS=unset AUTHENTIK_SECRET_KEY=unset`. Fix: add a `dev/compose.sh` (`dc "$@"`). Python scripts call it, and it also gives WP5 `dev/compose.sh stop|start authentik-server`.
- **Django shell runner:** `manage.py shell` inside the server is spelled out three times: `up.sh:108`, `shell.sh:5` and `sso_smoke.py:358-359`. `sso_smoke.inventree_state` should call `dev/shell.sh -c` (verified to work without a TTY), or the `dev/manage.sh` from finding 3.
- **Constants:**
  - The dev password appears in `bootstrap.py:37` (`DEV_PASSWORD`), `sso_smoke.py:221` (`PASSWORD`), `up.sh:127` and `README.md:34`.
  - The Authentik public URL default appears in `bootstrap.py:178`, `sso_smoke.py:220` and `up.sh:27`. `sso_smoke` ignores `AUTHENTIK_PORT`.
  - Fix: `sso_smoke` imports `DEV_PASSWORD`, `USERS` and the URL default from `bootstrap`. Its expected InvenTree groups (`sso_smoke.py:389`) can be derived from `bootstrap.USERS` and the `SSO_GROUP_MAP` in `configure_inventree.py:26`, instead of being hand-written a third time.
- **Restart block:** copied in `up.sh:79-80` and `113-114` (finding 7).
- **`test.sh:10` running-check:** this belongs in `_common.sh` as `require_running`. `shell.sh` and `logs.sh` then fail with the same clear message instead of a raw compose error.
- **KINA helpers:** `bootstrap.py`'s `Api`, `wait_ready`, `wait_api` and `get_or_create` are byte-identical to KINA's. `sso_smoke.py`'s `NoRedirect`, `Browser`, `absolute` and `run_flow` are copies too. The spec asked for exactly this ("keep its helpers, change only the data"), and a cross-repo import would couple the plugin to KINA's path, so this is accepted.
- **New copies:** `oauth_smoke.py` (WP1, not this WP) has already added a third `NoRedirect`/cookie-session implementation. Finding 1 removes the reason for it.
- **Between `bootstrap.py` and `sso_smoke.py`:** no shared logic is duplicated. The two HTTP clients serve different purposes (bearer API vs cookie browser).

## Next-stage readiness

- **WP1**
  - `dev/test.sh` works. The report shows 132/132 for `test_mcp`, and a bad label exits 1.
  - `sso_smoke.py` is not importable as a session-capable driver (finding 1). Until it is, WP1's acceptance 2 uses password login instead of the SSO round trip the plan asks for.
- **WP2**
  - Aging `membership_checked_at`: works today through `dev/shell.sh -c "..."` (verified non-interactive), or `dc exec db psql -U inventree`. The model does not exist yet; WP2 should add a small `age_membership(username, hours)` helper next to the e2e script, mirroring KINA's `kina_authentik_e2e.py:191-193`.
  - Removing a user from an Authentik group: missing (finding 2).
  - Root-owned migration files: finding 3.
  - The first-SSO-login 500 that the dev patch hides: finding 6.
- **WP5**
  - Authentik stop/start: works with `docker compose -f dev/compose.yaml stop|start authentik-server`. The plan's bare `docker compose stop authentik-server` only works from `dev/`. A `dev/compose.sh` wrapper (Duplication section) gives Python scripts one entry point.
  - Log grep: `logs.sh` cannot be used for it (follows forever), and the proxy log carries upstream codes (finding 9).
  - The `client-callback` stop in `browse` is needed by every façade scenario (finding 1).
- **Restart cost:** every `dc restart` re-runs `pip install` and `invoke update`, so picking up plugin code changes takes about a minute and needs network. It is acceptable, but WP agents should know it.

## Spot-check results

Both checks ran against the running stack, with no restart or modification. The containers had been recreated about 4 minutes earlier, and all services were healthy.

1. `curl -s http://localhost:8080/api/` → HTTP 200: `{"server": "InvenTree", ..., "version": "1.5.7", "apiVersion": 530, "worker_running": true, "plugins_enabled": true, "email_configured": true, "debug_mode": true, ..., "active_plugins": [..., {"name": "InvenTreeMCP", "slug": "inventree-mcp", "version": "0.3.0"}], ...}`. Pass.
2. `python3 dev/authentik/sso_smoke.py` → exit 0 in 12.7 s, first attempt, no retry needed. 16/16 PASS:
   ```
   PASS e2e-member: InvenTree redirects into Authentik  (http://authentik.localtest.me:19000/application/o/authorize/?client_id=inventree)
   PASS e2e-member: round trip ends back on InvenTree  (page 200 http://localhost:8080/web/logged-in)
   PASS e2e-member: GET /api/user/me/ with the session cookie  (200 username=e2e-member)
   PASS e2e-member: InvenTree groups == ['MCP Users', 'Users']  (['MCP Users', 'Users'])
   PASS e2e-member: refresh token stored in socialaccount_socialtoken
   PASS e2e-interactive: ... groups == ['Users']
   PASS e2e-guest: GET /api/user/me/ with the session cookie  (200 username=e2e-guest)
   PASS e2e-guest: InvenTree groups == []  ([])
   PASS e2e-nonmember: refused by Authentik  (authentik-denied 200)
   PASS e2e-nonmember: no InvenTree user created  (None)
   ```
   The users already existed in InvenTree, so this run did not exercise the patched first-login path (finding 6).

Other read-only checks:
- `ss -ltn`: ports 8080, 8000 and 8025 are bound to 127.0.0.1 and 19000 to 0.0.0.0. Port 5432 belongs to the unrelated `alacrity-pg`; the dev `db` has no published port (`compose ps`: `5432/tcp`).
- Generated secrets are git-ignored (`git check-ignore`) and mode 0600: `dev/.env`, `dev/authentik/.bootstrap.json`, `dev/data/config.yaml`.
- `git ls-files dev` contains no secret file.
- In the container, `getent ahosts authentik.localtest.me` gives 172.17.0.1, and the OIDC discovery URL answers 200 from inside the container.
