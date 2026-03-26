#!/usr/bin/env bash
#
# Quick health check for all services.
# Run from the host or any container on the shared network.
#

set -euo pipefail

SHOWDOWN_HOST="${SHOWDOWN_HOST:-localhost}"
SHOWDOWN_PORT="${SHOWDOWN_PORT:-8000}"
OVERLAY_HOST="${OVERLAY_HOST:-localhost}"
OVERLAY_PORT="${OVERLAY_PORT:-8080}"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

check() {
    local name="$1" url="$2"
    if curl -sf --max-time 5 "$url" > /dev/null 2>&1; then
        echo -e "${GREEN}[OK]${NC}   $name ($url)"
    else
        echo -e "${RED}[FAIL]${NC} $name ($url)"
    fi
}

echo "=== Service Health Check ==="
echo ""
check "Showdown"  "http://${SHOWDOWN_HOST}:${SHOWDOWN_PORT}/"
check "Overlay"   "http://${OVERLAY_HOST}:${OVERLAY_PORT}/health"
check "Scoreboard" "http://${OVERLAY_HOST}:${OVERLAY_PORT}/scoreboard"
echo ""
echo "Done."
