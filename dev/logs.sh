#!/usr/bin/env bash
# Logs: dev/logs.sh [service ...]          follow (no argument = all services)
#       dev/logs.sh --dump [service ...]   print and return (pipe it into grep), without the service prefix
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
[[ -n "$(dc ps -aq)" ]] || die "the dev stack has no containers, run dev/up.sh first"
if [[ "${1:-}" == "--dump" ]]; then
    shift
    dc logs --no-color --no-log-prefix "$@"
else
    dc logs -f --tail=100 "$@"
fi
