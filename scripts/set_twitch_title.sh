#!/usr/bin/env bash
#
# Update Twitch stream title/category via Helix API.
# Usage:
#   bash scripts/set_twitch_title.sh
#   bash scripts/set_twitch_title.sh "Custom title"
#

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"

trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf "%s" "$s"
}

load_dotenv_file() {
    local file="$1"
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        [[ -z "$(trim "$line")" ]] && continue
        [[ "$(trim "$line")" == \#* ]] && continue
        [[ "$line" != *"="* ]] && continue

        local key="${line%%=*}"
        local value="${line#*=}"
        key="$(trim "$key")"
        value="$(trim "$value")"

        if [[ "${value}" == \"*\" && "${value}" == *\" ]]; then
            value="${value:1:${#value}-2}"
        elif [[ "${value}" == \'*\' && "${value}" == *\' ]]; then
            value="${value:1:${#value}-2}"
        fi

        export "${key}=${value}"
    done < "$file"
}

if [[ -f "${ENV_FILE}" ]]; then
    load_dotenv_file "${ENV_FILE}"
fi

TWITCH_CLIENT_ID="${TWITCH_CLIENT_ID:-}"
TWITCH_OAUTH_TOKEN="${TWITCH_OAUTH_TOKEN:-}"
TWITCH_BROADCASTER_ID="${TWITCH_BROADCASTER_ID:-}"
TWITCH_GAME_ID="${TWITCH_GAME_ID:-491931}"
DEFAULT_TITLE="Testing Pokemon Showdown battles with LLMs"
TITLE="${1:-${TWITCH_STREAM_TITLE:-${DEFAULT_TITLE}}}"

if [[ -z "${TWITCH_CLIENT_ID}" ]]; then
    echo "[set_twitch_title] Missing TWITCH_CLIENT_ID in .env"
    exit 1
fi
if [[ -z "${TWITCH_OAUTH_TOKEN}" ]]; then
    echo "[set_twitch_title] Missing TWITCH_OAUTH_TOKEN in .env"
    exit 1
fi
if [[ -z "${TWITCH_BROADCASTER_ID}" ]]; then
    echo "[set_twitch_title] Missing TWITCH_BROADCASTER_ID in .env"
    exit 1
fi

if [[ -n "${TWITCH_GAME_ID}" ]]; then
    PAYLOAD=$(printf '{"title":"%s","game_id":"%s"}' "${TITLE}" "${TWITCH_GAME_ID}")
else
    PAYLOAD=$(printf '{"title":"%s"}' "${TITLE}")
fi

echo "[set_twitch_title] Updating Twitch title..."
HTTP_CODE="$(
    curl -sS -o /tmp/twitch_set_title_response.json -w "%{http_code}" \
        -X PATCH "https://api.twitch.tv/helix/channels?broadcaster_id=${TWITCH_BROADCASTER_ID}" \
        -H "Authorization: Bearer ${TWITCH_OAUTH_TOKEN}" \
        -H "Client-Id: ${TWITCH_CLIENT_ID}" \
        -H "Content-Type: application/json" \
        -d "${PAYLOAD}"
)"

if [[ "${HTTP_CODE}" == "204" ]]; then
    echo "[set_twitch_title] Success. Title set to: ${TITLE}"
    if [[ -n "${TWITCH_GAME_ID}" ]]; then
        echo "[set_twitch_title] Category set to game_id: ${TWITCH_GAME_ID}"
    fi
    exit 0
fi

echo "[set_twitch_title] Twitch API returned HTTP ${HTTP_CODE}"
echo "[set_twitch_title] Response:"
python - <<'PY'
import json
from pathlib import Path
path = Path("/tmp/twitch_set_title_response.json")
if not path.exists():
    print("(no response body)")
else:
    txt = path.read_text().strip()
    if not txt:
        print("(empty response body)")
    else:
        try:
            print(json.dumps(json.loads(txt), indent=2))
        except Exception:
            print(txt)
PY
exit 1
