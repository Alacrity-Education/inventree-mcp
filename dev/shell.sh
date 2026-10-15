#!/usr/bin/env bash
# Django shell in the InvenTree server container: dev/shell.sh [-c "code"]  (reads stdin when it is not a terminal)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
require_running
dcx inventree-server python src/backend/InvenTree/manage.py shell "$@"
