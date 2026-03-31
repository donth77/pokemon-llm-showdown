#!/usr/bin/env bash
#
# Quick health check for all services.
# Run from the host or any container on the shared network.
#

set -euo pipefail

SHOWDOWN_HOST="${SHOWDOWN_HOST:-localhost}"
SHOWDOWN_PORT="${SHOWDOWN_PORT:-8000}"
WEB_HOST="${WEB_HOST:-${OVERLAY_HOST:-localhost}}"
WEB_PORT="${WEB_PORT:-${OVERLAY_PORT:-8080}}"

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

check_sse_scoreboard() {
    local url="http://${WEB_HOST}:${WEB_PORT}/scoreboard/stream"
    if curl -sS -N --max-time 3 "$url" 2>/dev/null | head -n 30 | grep -q '^data:'; then
        echo -e "${GREEN}[OK]${NC}   Scoreboard SSE ($url)"
    else
        echo -e "${RED}[FAIL]${NC} Scoreboard SSE ($url)"
    fi
}

echo "=== Service Health Check ==="
echo ""
check "Showdown"  "http://${SHOWDOWN_HOST}:${SHOWDOWN_PORT}/"
check "Web"   "http://${WEB_HOST}:${WEB_PORT}/health"
check "Scoreboard" "http://${WEB_HOST}:${WEB_PORT}/scoreboard"
check_sse_scoreboard
echo ""
echo "Done."
