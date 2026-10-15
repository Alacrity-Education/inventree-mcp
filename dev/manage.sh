#!/usr/bin/env bash
# manage.py in the InvenTree server container: dev/manage.sh makemigrations inventree_mcp
# The container runs as root, so afterwards everything it created in the plugin checkout (migrations, egg-info)
# is chowned back to the host user, and the exit status of manage.py is preserved.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
require_running
rc=0
dcx inventree-server python src/backend/InvenTree/manage.py "$@" || rc=$?
fix_ownership
exit "$rc"
