#!/usr/bin/env bash
# Run the plugin tests inside the InvenTree server container: dev/test.sh [label] [extra manage.py test args]
# Default label: inventree_mcp (the whole package). Exits non-zero when tests fail.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LABEL="${1:-inventree_mcp}"
[[ $# -gt 0 ]] && shift

[[ -n "$(dc ps -q --status running inventree-server)" ]] || die "inventree-server is not running, run dev/up.sh first"

# Mirrors the env upstream CI sets; --keepdb keeps the test database between runs.
dcx -u root inventree-server bash -c \
    'cd /home/inventree/src/backend && INVENTREE_PLUGIN_TESTING=True INVENTREE_PLUGIN_TESTING_SETUP=True python InvenTree/manage.py test "$0" --keepdb "$@"' \
    "$LABEL" "$@"
