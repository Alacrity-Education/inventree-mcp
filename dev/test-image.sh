#!/usr/bin/env bash
# Run the plugin test suite inside any inventree/inventree image tag, the way CI's integration matrix does (root user,
# throwaway Postgres, the same INVENTREE_* environment). Independent of the dev stack (dev/up.sh), so `latest` (master)
# can be tested next to the stack's pinned release.
#   dev/test-image.sh up [TAG]              start Postgres + an app container for TAG (default: latest), prepare InvenTree
#   dev/test-image.sh test [labels...] [args] manage.py test (default label inventree_mcp, --keepdb; options passed through)
#   dev/test-image.sh shell                 bash in the app container (/home/inventree/src/backend)
#   dev/test-image.sh down                  remove the containers and the network
# The checkout is bind-mounted and installed editable: edits are picked up by the next `test`, no restart needed.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

NET=inventree-mcp-img
DB=inventree-mcp-img-db
APP=inventree-mcp-img-app
PLUGIN_MOUNT=/plugin

# Mirrors .github/workflows/ci.yaml (integration-tests job)
APP_ENV=(
    -e HOME=/root
    -e PYTHONDONTWRITEBYTECODE=1
    -e INVENTREE_DB_ENGINE=postgresql
    -e INVENTREE_DB_NAME=inventree
    -e "INVENTREE_DB_HOST=$DB"
    -e INVENTREE_DB_PORT=5432
    -e INVENTREE_DB_USER=inventree
    -e INVENTREE_DB_PASSWORD=inventree
    -e INVENTREE_PLUGINS_ENABLED=True
    -e INVENTREE_PLUGINS_MANDATORY=inventree-mcp
    -e INVENTREE_PLUGIN_TESTING=True
    -e INVENTREE_PLUGIN_TESTING_SETUP=True
    -e INVENTREE_SITE_URL=http://localhost:8000
    -e INVENTREE_SOCIAL_BACKENDS=openid_connect
)

app_running() { [[ -n "$(docker ps -q -f "name=^${APP}$")" ]]; }
require_app() { app_running || die "the test image container is not running, run dev/test-image.sh up [TAG] first"; }

# root wrote egg-info (and anything else) into the bind mount: hand it back to the host user
fix_mount_ownership() {
    docker exec "$APP" find "$PLUGIN_MOUNT" -xdev \( -name .git -o -path "$PLUGIN_MOUNT/deploy" \) -prune -o \
        \( ! -uid "$HOST_UID" -exec chown -h "$HOST_UID:$HOST_GID" {} + \) || true
}

cmd_up() {
    local tag=${1:-latest}
    app_running && die "already up (image $(docker inspect -f '{{.Config.Image}}' "$APP")); run dev/test-image.sh down first"
    docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
    docker rm -f "$DB" "$APP" >/dev/null 2>&1 || true

    log "starting Postgres ($DB)"
    docker run -d --name "$DB" --network "$NET" \
        -e POSTGRES_USER=inventree -e POSTGRES_PASSWORD=inventree -e POSTGRES_DB=inventree \
        postgres:17 >/dev/null
    log "starting inventree/inventree:$tag ($APP)"
    docker run -d --name "$APP" --network "$NET" --user root "${APP_ENV[@]}" \
        -v "$REPO_DIR:$PLUGIN_MOUNT" --entrypoint bash "inventree/inventree:$tag" -c 'sleep infinity' >/dev/null

    log "waiting for Postgres"
    local i
    for i in $(seq 1 60); do
        docker exec "$DB" pg_isready -q -U inventree && break
        sleep 1
    done
    docker exec "$DB" pg_isready -q -U inventree || die "Postgres did not become ready"

    log "preparing InvenTree (invoke update, plugin install, invoke migrate)"
    docker exec "$APP" bash -c '
        set -e
        cd /home/inventree
        invoke update --skip-backup --no-frontend
        pip3 install --quiet --disable-pip-version-check django_slowtests==1.1.1 -e "'"$PLUGIN_MOUNT"'[test]"
        invoke migrate
        python3 -c "import InvenTree.version as v" 2>/dev/null || true
        cd src/backend/InvenTree && python3 -c "from InvenTree.api_version import INVENTREE_API_VERSION as a; from InvenTree.version import inventreeVersion as v; print(f\"InvenTree {v()} (API v{a})\")"
    '
    fix_mount_ownership
    log "ready: dev/test-image.sh test [label] [args]"
}

cmd_test() {
    require_app
    # leading arguments that do not start with "-" are test labels; the rest are manage.py test options
    local labels=()
    while [[ $# -gt 0 && $1 != -* ]]; do
        labels+=("$1")
        shift
    done
    [[ ${#labels[@]} -gt 0 ]] || labels=(inventree_mcp)
    local rc=0
    docker exec "$APP" bash -c \
        'cd /home/inventree/src/backend && python3 InvenTree/manage.py test "$@"' _ "${labels[@]}" --keepdb "$@" || rc=$?
    fix_mount_ownership
    return "$rc"
}

cmd_shell() {
    require_app
    docker exec -it -w /home/inventree/src/backend "$APP" bash
}

cmd_down() {
    app_running && fix_mount_ownership
    docker rm -f "$DB" "$APP" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    log "removed"
}

case "${1:-}" in
    up) shift; cmd_up "$@" ;;
    test) shift; cmd_test "$@" ;;
    shell) cmd_shell ;;
    down) cmd_down ;;
    *) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 2 ;;
esac
