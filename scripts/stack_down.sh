#!/usr/bin/env bash
#
# Stop the full Docker Compose stack. Optional: remove named volumes (-v / --volumes).
# Optional duration: after compose down, the process stays up that long before
# exiting (for scheduling / coordination). Omit duration to exit right after down.
# Does not start services again; use scripts/restart_stack.sh when ready.
#
# Usage:
#   bash scripts/stack_down.sh
#   bash scripts/stack_down.sh -v
#   bash scripts/stack_down.sh --volumes
#   bash scripts/stack_down.sh 300              # down, then 300 seconds before exit
#   bash scripts/stack_down.sh 5m
#   bash scripts/stack_down.sh 1h
#   bash scripts/stack_down.sh -v 300
#   bash scripts/stack_down.sh --volumes 300
#

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SCRIPT_TAG="[$(basename "$0" .sh)]"

usage() {
    echo "Usage: $(basename "$0") [--volumes|-v] [duration]" >&2
    echo "" >&2
    echo "  Shuts down the whole stack (docker compose down)." >&2
    echo "  --volumes, -v  Also remove named volumes (docker compose down --volumes)." >&2
    echo "  duration (optional): after down, exit after this delay; plain seconds (300) or 30s, 5m, 2h" >&2
}

duration_to_seconds() {
    local raw="$1"
    if [[ "$raw" =~ ^[0-9]+$ ]]; then
        echo "$raw"
        return
    fi
    if [[ "$raw" =~ ^([0-9]+)s$ ]]; then
        echo "${BASH_REMATCH[1]}"
        return
    fi
    if [[ "$raw" =~ ^([0-9]+)m$ ]]; then
        echo $((${BASH_REMATCH[1]} * 60))
        return
    fi
    if [[ "$raw" =~ ^([0-9]+)h$ ]]; then
        echo $((${BASH_REMATCH[1]} * 3600))
        return
    fi
    echo "Invalid duration: $raw (use seconds or 30s, 5m, 1h)" >&2
    exit 1
}

REMOVE_VOLUMES=0
DURATION_ARG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h | --help)
            usage
            exit 0
            ;;
        -v | --volumes)
            REMOVE_VOLUMES=1
            shift
            ;;
        -*)
            echo "Unknown option: $1 (try --help)" >&2
            exit 1
            ;;
        *)
            if [[ -n "$DURATION_ARG" ]]; then
                echo "Unexpected extra argument: $1 (expected at most one duration)" >&2
                usage >&2
                exit 1
            fi
            DURATION_ARG="$1"
            shift
            ;;
    esac
done

DURATION_SECONDS=""
if [[ -n "$DURATION_ARG" ]]; then
    DURATION_SECONDS="$(duration_to_seconds "$DURATION_ARG")"
    if [[ "$DURATION_SECONDS" -le 0 ]]; then
        echo "Duration must be positive." >&2
        exit 1
    fi
fi

if [[ "$REMOVE_VOLUMES" -eq 1 ]]; then
    echo "${SCRIPT_TAG} docker compose down --volumes..."
    docker compose down --volumes
else
    echo "${SCRIPT_TAG} docker compose down..."
    docker compose down
fi

if [[ -n "$DURATION_SECONDS" ]]; then
    echo "${SCRIPT_TAG} ${DURATION_SECONDS}s until exit..."
    sleep "$DURATION_SECONDS"
fi

echo "${SCRIPT_TAG} Done."
