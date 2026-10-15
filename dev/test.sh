#!/usr/bin/env bash
# Run the plugin tests inside the InvenTree server container: dev/test.sh [label] [extra manage.py test args]
# Default label: inventree_mcp (the whole package). A first argument starting with "-" is an extra test argument,
# not a label. Exits non-zero when tests fail.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

if [[ $# -gt 0 && $1 != -* ]]; then
    LABEL=$1
    shift
else
    LABEL=inventree_mcp
fi

require_running

# Mirrors the env upstream CI sets; --keepdb keeps the test database between runs.
rc=0
dcx -u root inventree-server bash -c \
    'cd /home/inventree/src/backend && INVENTREE_PLUGIN_TESTING=True INVENTREE_PLUGIN_TESTING_SETUP=True python InvenTree/manage.py test "$0" --keepdb "$@"' \
    "$LABEL" "$@" || rc=$?
fix_ownership
exit "$rc"
