#!/usr/bin/env bash
#
# Restart the Docker stack. By default: showdown, web, agents (no Twitch stream).
# Pass -s / --stream to also restart and start the stream service.
#
# Usage:
#   bash scripts/restart_stack.sh
#   bash scripts/restart_stack.sh -s
#   bash scripts/restart_stack.sh --stream
#

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WITH_STREAM=0
for arg in "$@"; do
    case "$arg" in
        -s | --stream)
            WITH_STREAM=1
            ;;
        -h | --help)
            echo "Usage: $(basename "$0") [-s|--stream]"
            echo ""
            echo "  Restarts showdown, web, and agents. Stream is excluded unless -s or --stream is set."
            exit 0
            ;;
        *)
            echo "Unknown option: $arg (try --help)" >&2
            exit 1
            ;;
    esac
done

SERVICES=(showdown web agents)
if [[ "$WITH_STREAM" -eq 1 ]]; then
    SERVICES+=(stream)
else
    docker compose stop stream 2>/dev/null || true
fi

echo "[restart_stack] Stopping: ${SERVICES[*]}"
docker compose stop "${SERVICES[@]}"

echo "[restart_stack] Starting (build if needed): ${SERVICES[*]}"
docker compose up -d --build "${SERVICES[@]}"

echo "[restart_stack] Done."
docker compose ps "${SERVICES[@]}"
