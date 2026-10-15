#!/usr/bin/env bash
# The single entry point for `docker compose` against the dev stack: dev/compose.sh <compose args>
# (e.g. dev/compose.sh stop authentik-server). Used by every dev shell script (through dc in _common.sh) and
# callable from Python scripts and from anywhere, regardless of the current directory.
set -euo pipefail
DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export COMPOSE_PROJECT_NAME=inventree-mcp-dev

command -v docker >/dev/null || { echo "ERROR: docker is not installed" >&2; exit 1; }

# compose refuses to run when required secrets are missing, even for `down`/`exec`; dummies keep those commands
# usable when dev/.env does not exist (up.sh generates the real one before it starts anything).
if [[ ! -f "$DEV_DIR/.env" ]]; then
    export PG_PASS=unset AUTHENTIK_SECRET_KEY=unset
fi
exec docker compose -f "$DEV_DIR/compose.yaml" --project-directory "$DEV_DIR" "$@"
