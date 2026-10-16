#!/usr/bin/env bash
# Bring up the beta stack on this machine (idempotent): image build, database, InvenTree server + worker, Caddy on
# port 8999, running off the restored instance copy (data/, pgdata/); configuration on every run. See README.md.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
HERE="$(pwd)"
REPO_DIR="$(cd ../.. && pwd)"
export COMPOSE_PROJECT_NAME=inventree-mcp-beta

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "==> $*"; }
dc() { docker compose -f "$HERE/compose.yaml" "$@"; }

# --- 1. secrets ---------------------------------------------------------------------------------------------------
if [[ ! -f .env ]]; then
    log "writing .env (generated secrets; edit the Authentik values later and re-run)"
    rnd() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
    (
        umask 077
        sed -e "s/^PG_PASS=.*/PG_PASS=$(rnd 24)/" -e "s/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=$(rnd 16)/" .env.example > .env
    )
fi
set -a; source .env; set +a
[[ -n "${PUBLIC_HOST:-}" ]] || die "PUBLIC_HOST is empty in .env"

# --- 2. config.yaml: the restored file, with the SSO block set or removed per .env ------------------------------------
mkdir -p data
python3 - <<'PY'
import os, pathlib, yaml
issuer, cid, secret = (os.environ.get(k, "").strip() for k in ("AUTHENTIK_ISSUER", "AUTHENTIK_CLIENT_ID", "AUTHENTIK_CLIENT_SECRET"))
out = pathlib.Path("data/config.yaml")
old = out.read_text() if out.exists() else ""
cfg = yaml.safe_load(old) or {}
if issuer and cid and secret:
    # InvenTree 1.6 installs the openid_connect provider app itself; listing it here duplicates the app label
    cfg.pop("social_backends", None)
    cfg["social_providers"] = {"openid_connect": {
        "SCOPE": ["openid", "profile", "email", "offline_access"],
        "APPS": [{"provider_id": os.environ.get("AUTHENTIK_PROVIDER_ID", "authentik") or "authentik", "name": "Authentik", "client_id": cid, "secret": secret,
                  "settings": {"server_url": issuer.rstrip("/")}}]}}
    sso = "with SSO (Authentik from .env)"
else:
    cfg.pop("social_providers", None)
    sso = "SSO as in the restored instance (no AUTHENTIK_* in .env)"
new = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
if new != old:
    out.write_text(new)
out.chmod(0o600)
pathlib.Path("data/.config-changed").write_text("1" if new != old else "0")
print("config.yaml:", sso)
PY
config_changed="$(cat data/.config-changed)"; rm -f data/.config-changed
[[ -f data/plugins.txt ]] || : > data/plugins.txt  # the MCP plugin is baked into the image; other plugins stay listed

# --- 3. image, database, cache -------------------------------------------------------------------------------------
log "building the image (base image + plugin wheel)"
dc build --quiet inventree-server
# The Postgres data directory must belong to the image's postgres user (uid 999); tools that hand the checkout back
# to the host user (the dev stack's scripts prune deploy/, other tools may not) would otherwise break every connection.
if [[ -d pgdata ]] && docker run --rm -v "$HERE/pgdata:/d" alpine sh -c 'find /d ! -user 999 -print -quit | grep -q .'; then
    log "repairing the ownership of pgdata/ (postgres uid 999)"
    docker run --rm -v "$HERE/pgdata:/d" alpine sh -c 'find /d ! -user 999 -exec chown 999:999 {} +'
    [[ -n "$(dc ps -q --status running db 2>/dev/null || true)" ]] && dc restart db
fi
log "starting database and cache"
dc up -d --wait --wait-timeout 300 db cache
dc exec -T db psql -U "${DB_USER:-inventree}" -v ON_ERROR_STOP=1 -Atc \
    "ALTER ROLE \"${DB_USER:-inventree}\" WITH LOGIN PASSWORD '${PG_PASS}'" > /dev/null

# --- 4. migrations and static files, then the server, worker and proxy --------------------------------------------
server_was_running="$(dc ps -q --status running inventree-server 2>/dev/null || true)"
if [[ -z "$server_was_running" ]]; then
    log "invoke update (migrations, static files; a few minutes on the first run)"
    dc run --rm --no-deps inventree-server invoke update --skip-backup --no-frontend > data/update.log 2>&1 \
        || { tail -30 data/update.log; die "invoke update failed (full output in data/update.log)"; }
fi
log "starting InvenTree server, worker and proxy"
dc up -d --wait --wait-timeout 900 inventree-server inventree-worker proxy

# --- 5. demo data (once) -----------------------------------------------------------------------------------------
if [[ "${DEMO_DATA:-0}" == 1 && ! -f data/.demo-loaded ]]; then
    DEMO_REPO="$(cd "$REPO_DIR/.." && pwd)/demo-dataset"
    [[ -f "$DEMO_REPO/inventree_data.json" ]] || die "demo dataset not found at $DEMO_REPO"
    log "loading the demo dataset ($(git -C "$DEMO_REPO" rev-parse --short HEAD))"
    cp "$DEMO_REPO/inventree_data.json" data/demo-data.json
    dc exec -T inventree-server invoke import-records -c -f /home/inventree/data/demo-data.json > data/import.log 2>&1 \
        || { tail -30 data/import.log; die "demo data import failed (full output in data/import.log)"; }
    dc exec -T inventree-server bash -c 'mkdir -p /home/inventree/data/media' 
    docker cp "$DEMO_REPO/media/." "$(dc ps -q inventree-server):/home/inventree/data/media/"
    touch data/.demo-loaded
fi

# --- 6. configuration (every run) --------------------------------------------------------------------------------
sso_configured=0; [[ -n "${AUTHENTIK_ISSUER:-}" && -n "${AUTHENTIK_CLIENT_ID:-}" && -n "${AUTHENTIK_CLIENT_SECRET:-}" ]] && sso_configured=1
configure() {
    dc exec -T -e "SSO_GROUP_MAP=${SSO_GROUP_MAP:-}" -e "SSO_CONFIGURED=$sso_configured" -e "MCP_GROUPS=${MCP_GROUPS:-ItAdmins}" -e "MCP_WRITE=${MCP_WRITE:-1}" inventree-server \
        python src/backend/InvenTree/manage.py shell < configure_inventree.py 2>&1 | tee data/configure.log \
        | grep -v "^SETTINGS_CHANGED" || true
}
log "configuring InvenTree and the plugin"
configure
if grep -q '^configure_inventree: NEEDS_MIGRATE' data/configure.log; then
    # the plugin's app was switched on just now: apply its migrations, restart (gunicorn preloads the app), configure again
    log "applying the plugin's migrations"
    dc exec -T inventree-server invoke migrate > data/migrate-plugin.log 2>&1 \
        || { tail -20 data/migrate-plugin.log; die "plugin migrations failed (full output in data/migrate-plugin.log)"; }
    dc restart inventree-server inventree-worker
    dc up -d --wait --wait-timeout 900 inventree-server inventree-worker proxy
    configure
fi
grep -q '^configure_inventree: done' data/configure.log || die "configure_inventree.py failed (see output above)"

need_restart=0
grep -q '^SETTINGS_CHANGED' data/configure.log && need_restart=1
[[ -n "$server_was_running" && "$config_changed" == 1 ]] && need_restart=1
if [[ "$need_restart" == 1 ]]; then
    log "restarting InvenTree (settings or config.yaml changed; gunicorn preloads them)"
    dc restart inventree-server inventree-worker
    dc up -d --wait --wait-timeout 900 inventree-server inventree-worker proxy
fi

cat <<MSG

Beta stack is up.
  Web:        https://${PUBLIC_HOST}/   (local: http://127.0.0.1:8999/, send Host: ${PUBLIC_HOST})
  MCP URL:    https://${PUBLIC_HOST}/plugin/inventree-mcp/mcp/
  Users:      the restored instance's accounts (passwords untouched); MCP roles on: ${MCP_GROUPS:-ItAdmins}
  SSO:        $( [[ -n "${AUTHENTIK_CLIENT_ID:-}" ]] && echo "Authentik ${AUTHENTIK_ISSUER}" || echo "not configured (AUTHENTIK_* in .env, then re-run up.sh)" )
  Logs:       docker compose -p inventree-mcp-beta logs -f inventree-server inventree-worker proxy
MSG
