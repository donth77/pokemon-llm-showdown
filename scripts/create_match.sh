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

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help)
      cat <<'EOF'
Create a match or best-of-N series via POST /api/manager/matches.

Required: --p1-provider, --p1-model, --p2-provider, --p2-model
Optional: --p1-persona, --p2-persona, --format, --best-of N, --count N, --url BASE

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
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$P1_PROVIDER" || -z "$P1_MODEL" || -z "$P2_PROVIDER" || -z "$P2_MODEL" ]]; then
  echo "Error: --p1-provider, --p1-model, --p2-provider, and --p2-model are required." >&2
  exit 1
fi

PAYLOAD=$(cat <<EOF
{
  "battle_format": "$FORMAT",
  "player1_provider": "$P1_PROVIDER",
  "player1_model": "$P1_MODEL",
  "player1_persona": "$P1_PERSONA",
  "player2_provider": "$P2_PROVIDER",
  "player2_model": "$P2_MODEL",
  "player2_persona": "$P2_PERSONA",
  "best_of": $BEST_OF,
  "count": $COUNT
}
EOF
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
