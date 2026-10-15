#!/usr/bin/env bash
# Stop the dev stack. With -v also delete the volumes and dev/data/ (a fresh start; dev/.env is kept).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$DEV_DIR"

case "${1:-}" in
    "") dc down --remove-orphans ;;
    -v)
        # data/ is written as root by the containers; remove its content from inside a container
        if [[ -d data ]]; then
            dc run --rm --no-deps --user root --entrypoint sh -v "$DEV_DIR/data:/wipe" \
                inventree-server -c 'find /wipe -mindepth 1 -delete'
            rmdir data
        fi
        dc down -v --remove-orphans
        rm -f authentik/.bootstrap.json
        ;;
    *) die "usage: down.sh [-v]" ;;
esac
