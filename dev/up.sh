#!/usr/bin/env bash
# Bring up the whole dev stack (InvenTree + plugin, Authentik, Mailpit, proxy). Idempotent: safe to re-run.
set -euo pipefail
# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$DEV_DIR"

# --- 1. secrets, data directory, plugins.txt -------------------------------------------------------------------
if [[ ! -f .env ]]; then
    if docker volume ls -q | grep -q '^inventree-mcp-dev_authentik-db$'; then
        die "dev/.env is missing but the Authentik volume exists, so new secrets would not match it. Run dev/down.sh -v first."
    fi
    log "generating dev/.env"
    rnd() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
    (
        umask 077
        {
            echo "PG_PASS=$(rnd 24)"
            echo "AUTHENTIK_SECRET_KEY=$(rnd 48)"
            echo "AUTHENTIK_BOOTSTRAP_PASSWORD=$(rnd 18)"
            echo "AUTHENTIK_BOOTSTRAP_TOKEN=$(rnd 32)"
        } > .env
    )
fi
set -a; source .env; set +a
# one source for the shared constants: authentik/bootstrap.py (Authentik is published on 127.0.0.1:19000 only)
PUBLIC_URL="$(python3 authentik/bootstrap.py public-url)"
DEV_PASSWORD="$(python3 authentik/bootstrap.py dev-password)"

mkdir -p data
cp config/plugins.txt data/plugins.txt

HOSTS_OK=1
# the published port is bound on 127.0.0.1 (IPv4), so an IPv4 loopback answer is required (getent ahosts lists them all)
if ! getent ahosts authentik.localtest.me | grep -qE '^127\.0\.0\.1[[:space:]]'; then
    HOSTS_OK=0
fi
hosts_warning() {
    if [[ "$HOSTS_OK" == 0 ]]; then
        cat >&2 <<MSG

!!! authentik.localtest.me does not resolve to 127.0.0.1 on this host (no public DNS?).
!!! Add this line to /etc/hosts (needs root, up.sh cannot do it for you), then re-run dev/up.sh:
!!!     127.0.0.1 authentik.localtest.me
!!! Until then SSO login in a browser and dev/authentik/sso_smoke.py cannot work.

MSG
    fi
}
hosts_warning

# --- 1b. frontend bundles (build output, git-ignored: inventree_mcp/static/) ---------------------------------------
# Built on the host (node + npm) when Groups.js is missing or older than any frontend source or build config (src,
# package.json, package-lock.json, tsconfig*.json, biome.json, vite.config.ts); the server container collects
# inventree_mcp/static/ into /static/plugins/inventree-mcp/ when it starts. A missing npm or a failed build only warns:
# the stack still comes up, without the plugin pages (which need InvenTree 1.6+ anyway).
FRONTEND_DIR="$REPO_DIR/frontend"
BUNDLE="$REPO_DIR/inventree_mcp/static/Groups.js"
FRONTEND_TEST="inventree_mcp.tests.test_ui.UiFeatureMethodsTest.test_bundles_are_built"
frontend_warning() {
    echo "WARNING: $1; the plugin pages will not be available, and $FRONTEND_TEST will fail (dev/test.sh)" >&2
}
# A rebuild on a running stack needs a restart: the server collects the bundles only when it starts (the manifest
# would otherwise name hashed files that are not collected yet).
server_was_running="$(dc ps -q --status running inventree-server)"
bundles_built=0
if [[ -d "$FRONTEND_DIR" ]]; then
    stale=0
    if [[ ! -f "$BUNDLE" ]]; then
        stale=1
    elif [[ -n "$(find "$FRONTEND_DIR/src" "$FRONTEND_DIR/package.json" "$FRONTEND_DIR/package-lock.json" \
            "$FRONTEND_DIR/vite.config.ts" "$FRONTEND_DIR/biome.json" "$FRONTEND_DIR"/tsconfig*.json \
            -type f -newer "$BUNDLE" -print -quit)" ]]; then
        stale=1
    fi
    if [[ "$stale" == 1 ]]; then
        if ! command -v npm >/dev/null; then
            frontend_warning "npm is not installed, so the plugin frontend (frontend/) was not built"
        else
            log "building the plugin frontend"
            build_ok=1
            # npm ci only when the installed tree is missing or older than the lock file (a full network reinstall);
            # npm rewrites node_modules/.package-lock.json on every install, the directory mtime misses in-place updates
            if [[ ! -f "$FRONTEND_DIR/node_modules/.package-lock.json" \
                    || "$FRONTEND_DIR/package-lock.json" -nt "$FRONTEND_DIR/node_modules/.package-lock.json" ]]; then
                (cd "$FRONTEND_DIR" && npm ci) || build_ok=0
            fi
            if [[ "$build_ok" == 1 ]]; then
                (cd "$FRONTEND_DIR" && npm run build) || build_ok=0
            fi
            if [[ "$build_ok" == 0 ]]; then
                frontend_warning "the frontend build failed (output above)"
            else
                bundles_built=1
            fi
        fi
    fi
else
    echo "WARNING: frontend/ is missing, the plugin pages will not be available" >&2
fi

# --- 2. database, cache, mail and Authentik ------------------------------------------------------------------------
log "starting db, cache, mailpit and Authentik (first run pulls ~2 GB of images)"
dc up -d --wait --wait-timeout 900 db cache mailpit authentik-db authentik-server authentik-worker

# --- 3. bootstrap Authentik, render config.yaml -----------------------------------------------------------------------
log "bootstrapping Authentik"
AUTHENTIK_URL="http://127.0.0.1:19000" AUTHENTIK_PUBLIC_URL="$PUBLIC_URL" \
    python3 authentik/bootstrap.py > /dev/null
[[ -s authentik/.bootstrap.json ]] || die "authentik/.bootstrap.json was not written"

need_restart=0
python3 - <<'PY'
import json, pathlib
b = json.loads(pathlib.Path("authentik/.bootstrap.json").read_text())
t = pathlib.Path("config/config.yaml.tmpl").read_text()
t = t.replace("@CLIENT_ID@", b["client_id"]).replace("@CLIENT_SECRET@", b["client_secret"]).replace("@ISSUER@", b["issuer"].rstrip("/"))
out = pathlib.Path("data/config.yaml")
old = out.read_text() if out.exists() else None
out.write_text(t)
out.chmod(0o600)
pathlib.Path("data/.config-changed").write_text("1" if old != t else "0")
PY
config_changed="$(cat data/.config-changed)"; rm -f data/.config-changed

# gunicorn preloads the app, so config.yaml and the global settings that switch plugin URLs on are only picked up by a
# restart; that re-runs the entrypoint (pip install, invoke update), so it is done at most once per up.sh run.
restart_inventree() {
    log "restarting InvenTree ($1)"
    dc restart inventree-server inventree-worker
    dc up -d --wait --wait-timeout 600 inventree-server inventree-worker proxy
}

# --- 4. InvenTree -----------------------------------------------------------------------------------------------------
log "starting InvenTree server, worker and proxy (first run runs migrations, takes a few minutes)"
dc up -d --wait --wait-timeout 1200 inventree-server inventree-worker proxy
[[ -n "$server_was_running" && "$config_changed" == 1 ]] && need_restart=1
[[ -n "$server_was_running" && "$bundles_built" == 1 ]] && need_restart=1

# --- 5. demo data (once), configuration (every time) -----------------------------------------------------
if [[ ! -f data/.demo-loaded ]]; then
    DEMO_REPO=../../demo-dataset
    [[ -f $DEMO_REPO/inventree_data.json ]] || die "demo dataset not found at $(cd "$DEMO_REPO" 2>/dev/null && pwd || echo "$DEMO_REPO")"
    # The demo dataset's master is exported from InvenTree master (1.6 dev) and does not load into the stable 1.5.x
    # image ("NotesImage has no field named 'note'"). Default: the last export before the notes migration (api 521).
    # DEMO_DATASET_REF=<git ref> picks another one, DEMO_DATASET_REF=WORKTREE uses the checked-out file (use with
    # INVENTREE_TAG=latest).
    ref="${DEMO_DATASET_REF:-c400583^}"
    if [[ "$ref" == WORKTREE ]]; then
        cp "$DEMO_REPO/inventree_data.json" data/demo-data.json
    else
        git -C "$DEMO_REPO" show "$ref:inventree_data.json" > data/demo-data.json \
            || die "could not read inventree_data.json at $ref in $DEMO_REPO"
    fi
    log "loading demo data (dataset ref: $ref)"
    if ! dcx inventree-server invoke import-records -c -f /home/inventree/data/demo-data.json > data/import.log 2>&1; then
        tail -40 data/import.log >&2
        die "demo data import failed (full output in dev/data/import.log)"
    fi
    dcx inventree-server bash -c 'mkdir -p /home/inventree/data/media && cp -a /home/inventree/demo/media/. /home/inventree/data/media/'
    touch data/.demo-loaded
fi

log "configuring InvenTree (settings, groups)"
dcx inventree-server python src/backend/InvenTree/manage.py shell < configure_inventree.py 2>&1 | tee data/configure.log \
    | grep -E '^(setting|admin|group|configure|SETTINGS)' || true
grep -q '^configure_inventree: done' data/configure.log || die "configure_inventree.py failed (see output above)"
grep -q '^SETTINGS_CHANGED' data/configure.log && need_restart=1
if [[ "$need_restart" == 1 ]]; then
    restart_inventree "config.yaml, settings or the frontend bundles changed, so every gunicorn worker serves the plugin URLs, the SSO config and the collected bundles"
fi

# the OIDC issuer must resolve inside the container too (network alias on authentik-server)
if ! dcx inventree-server python -c "import urllib.request as u; u.urlopen('${PUBLIC_URL}/application/o/inventree/.well-known/openid-configuration', timeout=10)" >/dev/null 2>&1; then
    echo "WARNING: the InvenTree container cannot reach ${PUBLIC_URL}; SSO login will fail (see dev/README.md, Authentik reachability)" >&2
fi

# --- 6. summary -----------------------------------------------------------------------------------------------------
cat <<MSG

InvenTree MCP dev stack is up.

  InvenTree (use this URL)  http://localhost:8080        admin / admin
  InvenTree direct          http://127.0.0.1:8000
  Authentik admin           ${PUBLIC_URL}   akadmin / ${AUTHENTIK_BOOTSTRAP_PASSWORD}
  Mailpit                   http://127.0.0.1:8025

  SSO test accounts (password: ${DEV_PASSWORD})
    e2e-member       groups inventree-users + inventree-mcp  -> InvenTree groups Users + MCP Users
    e2e-interactive  groups inventree-users                  -> Users
    e2e-guest        groups inventree-guests                 -> logs in, no InvenTree group
    e2e-nonmember    no group                                -> refused by Authentik

  Connect Claude Code:
    claude mcp add --transport http inventree-dev http://localhost:8080/plugin/inventree-mcp/mcp/

  Run the plugin tests:  dev/test.sh
MSG
hosts_warning
