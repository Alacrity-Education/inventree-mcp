# Sourced by the dev scripts. Not executable on its own.
DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$DEV_DIR")"
export COMPOSE_PROJECT_NAME=inventree-mcp-dev
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"
PLUGIN_DIR_IN_CONTAINER=/home/inventree/plugins/inventree-mcp

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "==> $*"; }

command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "docker compose v2 is not available"

# docker compose against the dev stack (dev/compose.sh holds the actual invocation)
dc() { "$DEV_DIR/compose.sh" "$@"; }

# exec in a container; allocate a TTY only when stdin and stdout are terminals
dcx() {
    local flags=(-T)
    [[ -t 0 && -t 1 ]] && flags=()
    dc exec "${flags[@]}" "$@"
}

# fail with a clear message when the server container is not running
require_running() {
    [[ -n "$(dc ps -q --status running inventree-server)" ]] || die "inventree-server is not running, run dev/up.sh first"
}

# The containers run as root and write into the bind-mounted checkout (egg-info, migrations, bytecode). Hand
# everything under the plugin checkout that is not owned by the host user back to the host uid/gid.
fix_ownership() {
    # deploy/ holds deployment data (a Postgres data directory, a data volume) owned by their containers' users
    dc exec -T -u root inventree-server find "$PLUGIN_DIR_IN_CONTAINER" -xdev \
        \( -name .git -o -path "$PLUGIN_DIR_IN_CONTAINER/deploy" \) -prune -o \
        \( ! -uid "$HOST_UID" -exec chown -h "$HOST_UID:$HOST_GID" {} + \) || true
}
