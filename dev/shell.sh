#!/usr/bin/env bash
# Django shell in the InvenTree server container.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
dc exec inventree-server python src/backend/InvenTree/manage.py shell "$@"
