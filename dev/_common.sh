# Sourced by the dev scripts. Not executable on its own.
DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$DEV_DIR")"
export COMPOSE_PROJECT_NAME=inventree-mcp-dev

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "==> $*"; }

command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "docker compose v2 is not available"

# compose refuses to run when required secrets are missing, even for `down`/`exec`; dummies keep those commands
# usable when dev/.env does not exist (up.sh generates the real one before it starts anything).
dc() {
    if [[ -f "$DEV_DIR/.env" ]]; then
        docker compose -f "$DEV_DIR/compose.yaml" --project-directory "$DEV_DIR" "$@"
    else
        PG_PASS=unset AUTHENTIK_SECRET_KEY=unset docker compose -f "$DEV_DIR/compose.yaml" \
            --project-directory "$DEV_DIR" "$@"
    fi
}

# exec in the server container; allocate a TTY only when stdin is one
dcx() {
    local flags=(-T)
    [[ -t 0 && -t 1 ]] && flags=()
    dc exec "${flags[@]}" "$@"
}
