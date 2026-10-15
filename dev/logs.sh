#!/usr/bin/env bash
# Follow logs: dev/logs.sh [service ...]  (no argument = all services)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
dc logs -f --tail=100 "$@"
