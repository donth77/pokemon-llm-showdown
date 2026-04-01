#!/usr/bin/env bash
#
# Create a match or best-of-N series via the manager API.
#
# Usage:
#   ./scripts/create_match.sh \
#     --p1-provider anthropic --p1-model claude-sonnet-4-20250514 --p1-persona aggro \
#     --p2-provider openrouter --p2-model meta-llama/llama-3.1-70b-instruct --p2-persona stall \
#     --format gen9randombattle
#
#   # Best-of-5 series:
#   ./scripts/create_match.sh \
#     --p1-provider anthropic --p1-model claude-sonnet-4-20250514 --p1-persona aggro \
#     --p2-provider deepseek --p2-model deepseek-chat --p2-persona stall \
#     --format gen9randombattle --best-of 5
#
#   # Multiple individual matches:
#   ./scripts/create_match.sh \
#     --p1-provider anthropic --p1-model claude-sonnet-4-20250514 --p1-persona aggro \
#     --p2-provider deepseek --p2-model deepseek-chat --p2-persona stall \
#     --format gen9randombattle --count 10

set -euo pipefail

WEB_URL="${WEB_URL:-${OVERLAY_URL:-http://localhost:8080}}"

P1_PROVIDER=""
P1_MODEL=""
P1_PERSONA="aggro"
P2_PROVIDER=""
P2_MODEL=""
P2_PERSONA="stall"
FORMAT="gen9randombattle"
BEST_OF=0
COUNT=1
P1_TEAM_ID=""
P2_TEAM_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help)
      cat <<'EOF'
Create a match or best-of-N series via POST /api/manager/matches.

Required: --p1-provider, --p1-model, --p2-provider, --p2-model
Optional: --p1-persona, --p2-persona, --format, --best-of N, --count N, --url BASE,
  --p1-team-id ID, --p2-team-id ID (required for custom-team formats; disallowed for *randombattle)

WEB_URL or OVERLAY_URL env (default http://localhost:8080).

Examples:
  scripts/create_match.sh --p1-provider anthropic --p1-model claude-sonnet-4-20250514 \\
    --p1-persona aggro --p2-provider deepseek --p2-model deepseek-chat --p2-persona stall \\
    --format gen9randombattle --best-of 5
EOF
      exit 0
      ;;
    --p1-provider) P1_PROVIDER="$2"; shift 2 ;;
    --p1-model)    P1_MODEL="$2"; shift 2 ;;
    --p1-persona)  P1_PERSONA="$2"; shift 2 ;;
    --p2-provider) P2_PROVIDER="$2"; shift 2 ;;
    --p2-model)    P2_MODEL="$2"; shift 2 ;;
    --p2-persona)  P2_PERSONA="$2"; shift 2 ;;
    --format)      FORMAT="$2"; shift 2 ;;
    --best-of)     BEST_OF="$2"; shift 2 ;;
    --count)       COUNT="$2"; shift 2 ;;
    --url)         WEB_URL="$2"; shift 2 ;;
    --p1-team-id)  P1_TEAM_ID="$2"; shift 2 ;;
    --p2-team-id)  P2_TEAM_ID="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$P1_PROVIDER" || -z "$P1_MODEL" || -z "$P2_PROVIDER" || -z "$P2_MODEL" ]]; then
  echo "Error: --p1-provider, --p1-model, --p2-provider, and --p2-model are required." >&2
  exit 1
fi

fmt_lc=$(printf '%s' "$FORMAT" | tr '[:upper:]' '[:lower:]')
if [[ "$fmt_lc" != *randombattle ]]; then
  if [[ -z "${P1_TEAM_ID:-}" || -z "${P2_TEAM_ID:-}" ]]; then
    echo "Error: custom-team formats require --p1-team-id and --p2-team-id (manager team preset ids)." >&2
    exit 1
  fi
fi

PAYLOAD=$(FORMAT="$FORMAT" P1_PROVIDER="$P1_PROVIDER" P1_MODEL="$P1_MODEL" P1_PERSONA="$P1_PERSONA" \
  P2_PROVIDER="$P2_PROVIDER" P2_MODEL="$P2_MODEL" P2_PERSONA="$P2_PERSONA" \
  BEST_OF="$BEST_OF" COUNT="$COUNT" P1_TEAM_ID="$P1_TEAM_ID" P2_TEAM_ID="$P2_TEAM_ID" python3 - <<'PY'
import json
import os

def iopt(k):
    v = (os.environ.get(k) or "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None

body = {
    "battle_format": os.environ["FORMAT"],
    "player1_provider": os.environ["P1_PROVIDER"],
    "player1_model": os.environ["P1_MODEL"],
    "player1_persona": os.environ["P1_PERSONA"],
    "player2_provider": os.environ["P2_PROVIDER"],
    "player2_model": os.environ["P2_MODEL"],
    "player2_persona": os.environ["P2_PERSONA"],
    "best_of": int(os.environ.get("BEST_OF") or 0),
    "count": int(os.environ.get("COUNT") or 1),
}
t1 = iopt("P1_TEAM_ID")
t2 = iopt("P2_TEAM_ID")
if t1 is not None:
    body["player1_team_id"] = t1
if t2 is not None:
    body["player2_team_id"] = t2
print(json.dumps(body))
PY
)

echo "Creating match at $WEB_URL/api/manager/matches ..."
echo "$PAYLOAD" | python3 -m json.tool 2>/dev/null || echo "$PAYLOAD"
echo ""

RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" \
  "$WEB_URL/api/manager/matches")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [[ "$HTTP_CODE" -ge 200 && "$HTTP_CODE" -lt 300 ]]; then
  echo "Success!"
  echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"
else
  echo "Error (HTTP $HTTP_CODE):"
  echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"
  exit 1
fi
