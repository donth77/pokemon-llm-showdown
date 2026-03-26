#!/usr/bin/env bash
#
# Stream container entrypoint:
#   1. Start Xvfb virtual framebuffer
#   2. Start PulseAudio (for FFmpeg audio input)
#   3. Launch browser pointed at Showdown
#   4. Start FFmpeg supervisor
#

set -euo pipefail

export DISPLAY=:99
STREAM_AUDIO_SOURCE="${STREAM_AUDIO_SOURCE:-pulse}"
TWITCH_AUTO_SET_TITLE="${TWITCH_AUTO_SET_TITLE:-1}"

maybe_set_twitch_title() {
    if [[ "${TWITCH_AUTO_SET_TITLE}" != "1" ]]; then
        echo "[entrypoint] Skipping Twitch title update (TWITCH_AUTO_SET_TITLE=${TWITCH_AUTO_SET_TITLE})."
        return
    fi

    local client_id="${TWITCH_CLIENT_ID:-}"
    local oauth_token="${TWITCH_OAUTH_TOKEN:-}"
    local broadcaster_id="${TWITCH_BROADCASTER_ID:-}"
    local stream_title="${TWITCH_STREAM_TITLE:-Testing Pokemon Showdown battles with LLMs}"
    local game_id="${TWITCH_GAME_ID:-491931}"

    if [[ -z "${client_id}" || -z "${oauth_token}" || -z "${broadcaster_id}" ]]; then
        echo "[entrypoint] Twitch title update skipped (missing TWITCH_CLIENT_ID/TWITCH_OAUTH_TOKEN/TWITCH_BROADCASTER_ID)."
        return
    fi

    local payload
    if [[ -n "${game_id}" ]]; then
        payload=$(printf '{"title":"%s","game_id":"%s"}' "${stream_title}" "${game_id}")
    else
        payload=$(printf '{"title":"%s"}' "${stream_title}")
    fi

    echo "[entrypoint] Updating Twitch stream title via Helix API..."
    local http_code
    http_code="$(
        curl -sS -o /tmp/twitch_set_title_response.json -w "%{http_code}" \
            -X PATCH "https://api.twitch.tv/helix/channels?broadcaster_id=${broadcaster_id}" \
            -H "Authorization: Bearer ${oauth_token}" \
            -H "Client-Id: ${client_id}" \
            -H "Content-Type: application/json" \
            -d "${payload}" || true
    )"

    if [[ "${http_code}" == "204" ]]; then
        echo "[entrypoint] Twitch stream title updated."
    else
        echo "[entrypoint] Twitch title update failed (HTTP ${http_code})."
    fi
}

echo "[entrypoint] Starting Xvfb on ${DISPLAY} at 1280x720x24..."
Xvfb "${DISPLAY}" -screen 0 1280x720x24 -nolisten tcp &
XVFB_PID=$!
sleep 2

echo "[entrypoint] Starting PulseAudio..."
pulseaudio --start --exit-idle-time=-1 2>/dev/null || true

if [[ "${STREAM_AUDIO_SOURCE}" == "browser" ]]; then
    echo "[entrypoint] Configuring PulseAudio browser sink..."
    pactl load-module module-null-sink sink_name=stream sink_properties=device.description=stream >/dev/null || true
    pactl set-default-sink stream >/dev/null || true
fi

maybe_set_twitch_title

echo "[entrypoint] Launching browser..."
python -u browser.py &
BROWSER_PID=$!
sleep 5

echo "[entrypoint] Starting FFmpeg supervisor..."
exec ./supervisor.sh
