# Development and test environment

One command brings up a disposable InvenTree (stable image, this checkout installed as an editable plugin, demo data,
SSO against a disposable Authentik); one command runs the plugin tests inside it. Everything is Docker; nothing is
installed on the host and nothing is published on ports 5432, 80 or 443. Compose project name: `inventree-mcp-dev`.

## Day to day

```bash
dev/up.sh                 # bring everything up (idempotent; first run pulls ~2 GB and takes ~5 minutes)
dev/test.sh               # the whole plugin suite (label inventree_mcp)
dev/test.sh inventree_mcp.test_mcp                     # upstream suite only
dev/test.sh inventree_mcp.test_mcp.MCPTransportTest -v 2   # extra manage.py test args are passed through
dev/shell.sh              # Django shell in the server container (dev/shell.sh -c "print(1)" works too)
dev/manage.sh <args>      # manage.py in the server container, files it creates are chowned back to you
dev/manage.sh makemigrations inventree_mcp             # generates inventree_mcp/migrations/ owned by your user
dev/logs.sh [service]     # follow logs (inventree-server, inventree-worker, proxy, authentik-server, ...)
dev/logs.sh --dump [service] | grep ...                # print and return, for grepping
dev/compose.sh <args>     # docker compose against the stack, from any directory (dev/compose.sh stop authentik-server)
python3 dev/authentik/bootstrap.py set-groups e2e-member inventree-users   # change a test user's Authentik groups
python3 dev/authentik/bootstrap.py                     # full bootstrap again: restores the intended memberships
python3 dev/authentik/sso_smoke.py                     # scripted SSO round trip for the four test users
dev/down.sh               # stop (keeps volumes and data)
dev/down.sh -v            # stop and delete volumes and dev/data/ (fresh start next time)
```

Plugin code is bind-mounted and installed with `pip install -e`: Python edits show up after
`dev/compose.sh restart inventree-server inventree-worker` (`dev/up.sh` also works). A restart re-runs the entrypoint
(`pip install` over the network plus `invoke update`), so it takes about a minute.
`dev/test.sh` always runs the current files, no restart needed.

## Frontend (plugin pages)

The pages ("MCP Access" for staff, "MCP" for everybody) are React bundles built from `frontend/` into
`inventree_mcp/static/` (git-ignored build output; never edit it by hand).

```bash
dev/up.sh                              # builds them first when Groups.js is missing or older than frontend/src
(cd frontend && npm ci && npm run build)   # rebuild by hand (also: npm run lint, npm test, npm run check:bundles)
dev/compose.sh restart inventree-server inventree-worker
```

InvenTree copies the plugin's static tree to `dev/data/static/plugins/inventree-mcp/` (Caddy serves it as
`/static/plugins/inventree-mcp/`) when the plugin registry loads, so a restart of the server container is the step that
refreshes the files after a rebuild (a registry reload, e.g. a `dev/test.sh` run that reloads plugins, also copies
them). Check with `curl -I http://localhost:8080/static/plugins/inventree-mcp/Groups.js`; hard-reload the browser
afterwards (the plain `Groups.js` names are cached, core prefers the hashed copies when it builds the URLs).
A build removes the old hashed files (`--emptyOutDir`) but the copy in `dev/data/static/` keeps them, which is harmless.

Plugin pages (routes) need an InvenTree newer than 1.5.7 (core PR #12900, in `inventree/inventree:latest`). On the
default 1.5.7 stack the plugin therefore offers no pages and no navigation entries (the "MCP Roles" group panel is
ready for the core change that requests group panels). To try the pages set `INVENTREE_TAG=latest` and
`DEMO_DATASET_REF=WORKTREE` in `dev/.env`, then `dev/down.sh -v && dev/up.sh`.

Headless-browser check of both pages (`dev/ui_smoke.py`, screenshots in `docs/plans/screenshots/`); the host python is
usually externally managed, so use a virtualenv:

```bash
python3 -m venv /tmp/pw && /tmp/pw/bin/pip install playwright && /tmp/pw/bin/playwright install chromium
/tmp/pw/bin/python dev/ui_smoke.py [--base-url http://localhost:8080]
```

Hot reload with InvenTree's `plugin_dev` block (`plugin_dev: {slug: inventree-mcp, host: http://localhost:5174}` in
`config.yaml`, debug mode on) was not attempted: the frontend has no `npm run dev` script and the stable stack has no
plugin routes to load into.

## URLs and accounts

| What | URL | Login |
|---|---|---|
| InvenTree (use this one) | http://localhost:8080 | `admin` / `admin` |
| InvenTree, gunicorn direct | http://127.0.0.1:8000 | same |
| Authentik admin | http://authentik.localtest.me:19000 | `akadmin` / `AUTHENTIK_BOOTSTRAP_PASSWORD` from `dev/.env` |
| Mailpit (outgoing mail) | http://127.0.0.1:8025 | |

SSO test users (Authentik password `dev-password-123`; InvenTree group sync maps `inventree-users` to `Users` and
`inventree-mcp` to `MCP Users`):

| User | Authentik groups | Result |
|---|---|---|
| `e2e-member` | inventree-users, inventree-mcp | logs in, InvenTree groups `Users` + `MCP Users` |
| `e2e-interactive` | inventree-users | logs in, group `Users` |
| `e2e-guest` | inventree-guests | logs in, no InvenTree group |
| `e2e-nonmember` | none | refused by Authentik itself |

Connect Claude Code (plain HTTP on localhost is accepted):

```bash
claude mcp add --transport http inventree-dev http://localhost:8080/plugin/inventree-mcp/mcp/
```

API token for curl: `curl -s -u admin:admin http://localhost:8080/api/user/token/` (a GET in InvenTree 1.5).

## Layout

| File | Purpose |
|---|---|
| `compose.sh` | the single `docker compose` entry point (used by all scripts, callable from Python) |
| `manage.sh` | `manage.py` in the server container, then chowns generated files back to the host user |
| `compose.yaml` | all services (db, cache, mailpit, inventree-server/worker, proxy, authentik-db/server/worker) |
| `inventree.env` | InvenTree environment (committed, nothing secret) |
| `.env`, `.env.example` | Authentik secrets and port; `.env` is generated by `up.sh`, git-ignored |
| `entrypoint.sh` | wrapper around the image's `init.sh` (see below) |
| `config/config.yaml.tmpl` | rendered to `data/config.yaml` with the Authentik client from the bootstrap |
| `config/plugins.txt` | copied to `data/plugins.txt` (`-e` install of the plugin) |
| `Caddyfile` | proxy: static and media, everything else to gunicorn |
| `authentik/bootstrap.py` | get-or-create groups, users, scope mapping, provider, application, bindings; subcommands `set-groups`, `groups`, helpers `api_from_env`, `set_user_groups` |
| `authentik/sso_smoke.py` | scripted SSO login round trip; importable: `sso_login(username)`, `browse(..., stop=...)`, `Browser` |
| `configure_inventree.py` | global settings, groups `Users` and `MCP Users` (MCP roles: view on every role; admin is a member) (run by `up.sh`) |
| `oauth_smoke.py`, `membership_e2e.py`, `permissions_e2e.py` | live checks: OAuth façade, membership re-checks, MCP permission set |
| `ui_smoke.py` | headless-browser check of the plugin pages (see Frontend) |
| `data/` | InvenTree data dir (config, static, media, secret key), git-ignored |

Why an entrypoint wrapper: the image's `init.sh` only prepares directories and `config.yaml`; it never runs
`invoke update`, so `plugins.txt` would not be installed. `entrypoint.sh` installs the plugin editable plus
`django_slowtests==1.1.1` (needed by `manage.py test`) in both server and worker containers on every start, runs
`invoke update` on the server, then execs the original `init.sh`. It also patches one line of InvenTree 1.5.7
(see Known issues).

## Authentik reachability

The host browser and the InvenTree container must reach Authentik under the same URL, because the OIDC issuer is
derived from it: `http://authentik.localtest.me:19000`. On the host, `*.localtest.me` is public DNS for 127.0.0.1 and
port 19000 is published on `127.0.0.1` only (not on the LAN). Inside the compose network the `authentik-server`
service has the network alias `authentik.localtest.me` and listens on 19000 (`AUTHENTIK_LISTEN__HTTP`), so the same URL
works in the containers without `extra_hosts`. If your resolver does not resolve `localtest.me` (offline, DNS
rebinding protection), `up.sh` detects that and prints the fix: add `127.0.0.1 authentik.localtest.me` to
`/etc/hosts`. `up.sh` also checks that the InvenTree container can fetch the discovery document and warns if not.
If `authentik-server` never turns healthy after this layout change, see the FALLBACK comment in `compose.yaml`.

## Files owned by root

The containers run as root on the bind-mounted checkout. `PYTHONDONTWRITEBYTECODE=1` (in `inventree.env`) stops
`__pycache__` directories from appearing; the entrypoint, `dev/manage.sh` and `dev/test.sh` chown what pip, Django
and the test run leave behind (egg-info, `inventree_mcp/migrations/`) back to your uid/gid. Generate migrations with
`dev/manage.sh makemigrations inventree_mcp`, not with a bare `docker compose exec`.

## Troubleshooting

- `dev/.env is missing but the Authentik volume exists`: run `dev/down.sh -v`, then `dev/up.sh`.
- Demo data: the demo-dataset repo's master is exported from InvenTree 1.6 dev and does not load into 1.5.x, so
  `up.sh` loads the last 1.5-compatible export (`git show c400583^:inventree_data.json` from `../demo-dataset`).
  `DEMO_DATASET_REF=<ref>` selects another, `DEMO_DATASET_REF=WORKTREE` the checked-out file (with
  `INVENTREE_TAG=latest` in `dev/.env`). The demo import runs only once (marker `data/.demo-loaded`).
- Other InvenTree version: put `INVENTREE_TAG=stable` (or `latest`, or an exact tag) in `dev/.env`, then
  `dev/down.sh -v && dev/up.sh`.
- Intermittent 403/404 on `/plugin/...` after changing plugin-related global settings: restart the server
  (`up.sh` does this by itself when `configure_inventree.py` changed a setting).
- First start is slow: migrations plus `invoke update`; the healthcheck allows up to ~12 minutes.
- Logs of a failed demo import: `dev/data/import.log`.
- The proxy log strips the OAuth `code` and `state` query parameters; `inventree-server`/`inventree-worker` logs and
  `dev/logs.sh --dump` are what to grep for leaked secrets.

## Known issues

- InvenTree 1.5.x (checked on 1.5.7): the first SSO login of a new user ends in a 500 (`AssertionError` in allauth's
  `setup_user_email`) because `CustomSocialAccountAdapter.save_user` marks the SocialLogin instead of the User. The
  user, groups and `SocialToken` are saved, but the session login does not complete. Master fixed it in InvenTree
  PR #12842 ("alternative approach to fix SSO path"); no released tag contains the fix yet, so production installs on
  1.5.x show this 500 (the plugin's login bridge and `user_logged_in` handling meet it on every user's first login).
  `entrypoint.sh` patches that line in the container at start so the dev stack works. It is guarded (exact-line
  match, idempotent), logs when it is not needed, and is switched off with `DEV_PATCH_SSO_SIGNUP=0` in
  `inventree.env`; recreate the containers afterwards (`dev/compose.sh up -d --force-recreate inventree-server
  inventree-worker`) and run `sso_smoke.py` with a user that has never logged in to see the production behaviour.
- `INVENTREE_DEBUG=True` also opens CORS to every origin; acceptable for a disposable dev instance.
- Test users share the fixed password `dev-password-123` (defined once, in `authentik/bootstrap.py`); Authentik is
  published on `127.0.0.1` only, so they are not reachable from the LAN.
