#!/usr/bin/env bash
#
# Start a fresh agents run manually (respects MATCH_COUNT from .env).
# Optional:
#   --reset   Clear replays/logs/results/state first.
#

set -euo pipefail

RESET_FIRST=0
if [[ "${1:-}" == "--reset" ]]; then
    RESET_FIRST=1
fi

if [[ "$RESET_FIRST" -eq 1 ]]; then
    echo "[start_battle] Clearing replay/log/results/state..."
    docker compose run --rm agents sh -lc 'rm -f /replays/*.html /logs/*.json /state/current_battle.json /state/thoughts.json'
    docker compose exec overlay sh -lc 'rm -f /data/results.json'
    docker compose restart overlay > /dev/null
fi

echo "[start_battle] Starting manual agents run..."
docker compose run --rm agents
