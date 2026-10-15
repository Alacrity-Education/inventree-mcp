#!/usr/bin/env bash
# Stop the beta stack. `down.sh -v` also deletes the database volume and data/ (everything).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
export COMPOSE_PROJECT_NAME=inventree-mcp-beta
if [[ "${1:-}" == "-v" ]]; then
    docker compose -f compose.yaml down -v --remove-orphans
    docker run --rm -v "$PWD/data:/d" alpine sh -c 'rm -rf /d/* /d/.[!.]*' 2>/dev/null || true
    echo "removed containers, volumes and data/"
else
    docker compose -f compose.yaml down --remove-orphans
fi
