#!/usr/bin/env bash
#
# Poll the manager until a tournament is finished (completed or cancelled), then
# run scripts/stack_down.sh (docker compose down for the whole project, including
# stream when it was part of the stack).
#
# Usage:
#   bash scripts/stack_down_after_tournament.sh
#   bash scripts/stack_down_after_tournament.sh --tournament-id 7
#   TOURNAMENT_ID=7 bash scripts/stack_down_after_tournament.sh
#   bash scripts/stack_down_after_tournament.sh --post-delay 0   # shut down immediately
#   bash scripts/stack_down_after_tournament.sh -v
#
# Env:
#   WEB_URL          Manager / web base URL (default http://localhost:8080)
#   OVERLAY_URL      Fallback if WEB_URL unset (same default as other scripts)
#   TOURNAMENT_ID    Same as --tournament-id when set and flag omitted
#

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SCRIPT_TAG="[$(basename "$0" .sh)]"
WEB_URL="${WEB_URL:-${OVERLAY_URL:-http://localhost:8080}}"
WEB_URL="${WEB_URL%/}"

TID="${TOURNAMENT_ID:-}"
POLL_INTERVAL=15
POST_DELAY=120
STACK_DOWN_EXTRA=()

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

  Waits until tournament status is completed or cancelled, then runs
  scripts/stack_down.sh (full docker compose down).

Options:
  -t, --tournament-id ID   Tournament id (from /manager/tournaments/{id}).
                            If omitted, resolves automatically (see below).
  --poll-interval SEC        Seconds between status checks (default: 15).
  --post-delay SEC           Sleep this many seconds after terminal status before
                            stack down (default: 120). Use 0 to skip. Useful for victory splash / stream.
  -v, --volumes              Pass through to stack_down.sh (docker compose down --volumes).
  -h, --help                 Show this help.

Auto-resolution when --tournament-id is omitted:
  - If one or more tournaments are in_progress, uses the most recently updated.
    (If more than one, prints which id was chosen.)
  - Else if exactly one tournament is pending, uses that id.
  - Else exits with an error (pass -t explicitly).

Env: WEB_URL, OVERLAY_URL, TOURNAMENT_ID
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h | --help)
            usage
            exit 0
            ;;
        -t | --tournament-id)
            if [[ $# -lt 2 ]]; then
                echo "${SCRIPT_TAG} --tournament-id requires a value." >&2
                exit 1
            fi
            TID="$2"
            shift 2
            ;;
        --poll-interval)
            if [[ $# -lt 2 ]]; then
                echo "${SCRIPT_TAG} --poll-interval requires a value." >&2
                exit 1
            fi
            POLL_INTERVAL="$2"
            shift 2
            ;;
        --post-delay)
            if [[ $# -lt 2 ]]; then
                echo "${SCRIPT_TAG} --post-delay requires a value." >&2
                exit 1
            fi
            POST_DELAY="$2"
            shift 2
            ;;
        -v | --volumes)
            STACK_DOWN_EXTRA+=("-v")
            shift
            ;;
        -*)
            echo "${SCRIPT_TAG} Unknown option: $1 (try --help)" >&2
            exit 1
            ;;
        *)
            echo "${SCRIPT_TAG} Unexpected argument: $1 (try --help)" >&2
            exit 1
            ;;
    esac
done

if ! [[ "$POLL_INTERVAL" =~ ^[0-9]+$ ]] || [[ "$POLL_INTERVAL" -lt 1 ]]; then
    echo "${SCRIPT_TAG} --poll-interval must be a positive integer." >&2
    exit 1
fi
if ! [[ "$POST_DELAY" =~ ^[0-9]+$ ]]; then
    echo "${SCRIPT_TAG} --post-delay must be a non-negative integer." >&2
    exit 1
fi

auto_resolve_tid() {
    python3 - "$WEB_URL" <<'PY'
import json
import sys
import urllib.error
import urllib.request

def get(base: str, path: str):
    url = f"{base}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

base = sys.argv[1].rstrip("/")
try:
    prog = get(base, "/api/manager/tournaments?status=in_progress")
except urllib.error.URLError as e:
    print(f"Failed to reach manager at {base}: {e}", file=sys.stderr)
    sys.exit(1)
except json.JSONDecodeError as e:
    print(f"Invalid JSON from manager: {e}", file=sys.stderr)
    sys.exit(1)

if prog:
    best = max(
        prog,
        key=lambda t: (float(t.get("updated_at") or 0), int(t.get("id") or 0)),
    )
    if len(prog) > 1:
        print(
            f"Multiple in_progress tournaments ({len(prog)}); picking id={best['id']} (latest updated_at).",
            file=sys.stderr,
        )
    print(best["id"])
    sys.exit(0)

try:
    pend = get(base, "/api/manager/tournaments?status=pending")
except (urllib.error.URLError, json.JSONDecodeError) as e:
    print(f"Could not list pending tournaments: {e}", file=sys.stderr)
    sys.exit(1)

if len(pend) == 1:
    print(pend[0]["id"])
    sys.exit(0)

sys.stderr.write(
    "Could not resolve tournament: need at least one in_progress, or exactly one pending. "
    f"Got in_progress={len(prog)} pending={len(pend)}. Use --tournament-id.\n"
)
sys.exit(1)
PY
}

if [[ -z "$TID" ]]; then
    TID="$(auto_resolve_tid)"
fi

if ! [[ "$TID" =~ ^[0-9]+$ ]]; then
    echo "${SCRIPT_TAG} Invalid tournament id: ${TID:-<empty>}" >&2
    exit 1
fi

echo "${SCRIPT_TAG} Watching tournament id=$TID ($WEB_URL) poll=${POLL_INTERVAL}s ..."

while true; do
    st=""
    st="$(
        curl -fsS "$WEB_URL/api/manager/tournaments/$TID" \
            | python3 -c "import sys, json; print(json.load(sys.stdin).get('status', ''))"
    )"
    case "$st" in
        completed | cancelled)
            echo "${SCRIPT_TAG} Tournament $TID is $st."
            break
            ;;
        pending | in_progress)
            echo "${SCRIPT_TAG} Tournament $TID status=$st — waiting ..."
            ;;
        "")
            echo "${SCRIPT_TAG} Tournament $TID: missing status in response — waiting ..." >&2
            ;;
        *)
            echo "${SCRIPT_TAG} Tournament $TID status=$st — waiting ..." >&2
            ;;
    esac
    sleep "$POLL_INTERVAL"
done

if [[ "$POST_DELAY" -gt 0 ]]; then
    echo "${SCRIPT_TAG} Post-delay ${POST_DELAY}s ..."
    sleep "$POST_DELAY"
fi

echo "${SCRIPT_TAG} Running stack_down.sh ..."
bash "$ROOT/scripts/stack_down.sh" "${STACK_DOWN_EXTRA[@]}"
