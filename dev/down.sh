#!/usr/bin/env bash
# Stop the dev stack. With -v also delete the volumes and dev/data/ (a fresh start; dev/.env is kept).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$DEV_DIR"

case "${1:-}" in
    "") dc down --remove-orphans ;;
    -v)
        # Stop everything first so that nothing re-creates files while data/ is wiped.
        dc down -v --remove-orphans
        # data/ is written as root by the containers; remove its content from inside a one-off container
        if [[ -d data ]]; then
            dc run --rm --no-deps --user root --entrypoint sh -v "$DEV_DIR/data:/wipe" \
                inventree-server -c 'find /wipe -mindepth 1 -delete'
            rmdir data
            # the one-off run re-created the network; remove it again
            dc down --remove-orphans
        fi
        rm -f authentik/.bootstrap.json
        ;;
    *) die "usage: down.sh [-v]" ;;
esac
