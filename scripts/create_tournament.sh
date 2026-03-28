#!/usr/bin/env bash
#
# Create a tournament via the manager API.
#
# Usage:
#   ./scripts/create_tournament.sh \
#     --name "LLM Battle Royale" \
#     --type round_robin \
#     --format gen9randombattle \
#     --best-of 3 \
#     --player anthropic/claude-sonnet-4-20250514/aggro \
#     --player openrouter/meta-llama/llama-3.1-70b-instruct/stall \
#     --player deepseek/deepseek-chat/aggro
#
# Player format: provider/model/persona
# For models with slashes (e.g. openrouter): provider/org/model-name/persona
#   The script splits on the first and last slash to extract provider and persona.

set -euo pipefail

WEB_URL="${WEB_URL:-${OVERLAY_URL:-http://localhost:8080}}"

NAME=""
TYPE="round_robin"
FORMAT="gen9randombattle"
BEST_OF=3
PLAYERS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help)
      cat <<'EOF'
Create a tournament via POST /api/manager/tournaments.

Required: --name, at least two --player entries
Optional: --type round_robin|single_elimination|double_elimination, --format, --best-of, --url

Each --player: provider/model/persona (for openrouter models with slashes, see script header).

WEB_URL or OVERLAY_URL env (default http://localhost:8080).
EOF
      exit 0
      ;;
    --name)     NAME="$2"; shift 2 ;;
    --type)     TYPE="$2"; shift 2 ;;
    --format)   FORMAT="$2"; shift 2 ;;
    --best-of)  BEST_OF="$2"; shift 2 ;;
    --player)   PLAYERS+=("$2"); shift 2 ;;
    --url)      WEB_URL="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$NAME" ]]; then
  echo "Error: --name is required." >&2
  exit 1
fi

if [[ ${#PLAYERS[@]} -lt 2 ]]; then
  echo "Error: At least 2 --player entries are required." >&2
  exit 1
fi

# Build entries JSON array
ENTRIES="["
SEED=1
for p in "${PLAYERS[@]}"; do
  # Split: first segment = provider, last segment = persona, middle = model
  PROVIDER="${p%%/*}"
  REST="${p#*/}"
  PERSONA="${REST##*/}"
  MODEL="${REST%/*}"

  if [[ "$SEED" -gt 1 ]]; then
    ENTRIES+=","
  fi
  ENTRIES+=$(cat <<EOF
  {
    "provider": "$PROVIDER",
    "model": "$MODEL",
    "persona_slug": "$PERSONA",
    "seed": $SEED
  }
EOF
)
  SEED=$((SEED + 1))
done
ENTRIES+="]"

PAYLOAD=$(cat <<EOF
{
  "name": "$NAME",
  "type": "$TYPE",
  "battle_format": "$FORMAT",
  "best_of": $BEST_OF,
  "entries": $ENTRIES
}
EOF
)

echo "Creating tournament at $WEB_URL/api/manager/tournaments ..."
echo "$PAYLOAD" | python3 -m json.tool 2>/dev/null || echo "$PAYLOAD"
echo ""

RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" \
  "$WEB_URL/api/manager/tournaments")

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
