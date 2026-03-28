#!/usr/bin/env bash
#
# FFmpeg supervisor — restarts the stream if FFmpeg exits unexpectedly.
#

set -euo pipefail

TWITCH_STREAM_KEY="${TWITCH_STREAM_KEY:?TWITCH_STREAM_KEY env var is required}"
RTMP_URL="rtmp://live.twitch.tv/app/${TWITCH_STREAM_KEY}"
DISPLAY="${DISPLAY:-:99}"
STREAM_AUDIO_SOURCE="${STREAM_AUDIO_SOURCE:-pulse}"

ffmpeg_cmd() {
    if [[ "${STREAM_AUDIO_SOURCE}" == "browser" ]]; then
        echo "[supervisor] Capturing browser audio from PulseAudio monitor: stream.monitor"
        ffmpeg \
            -f x11grab -video_size 1280x720 -framerate 30 -i "${DISPLAY}" \
            -f pulse -ac 2 -i stream.monitor \
            -c:v libx264 -preset veryfast -b:v 2500k -maxrate 2500k -bufsize 5000k \
            -g 60 -keyint_min 60 \
            -c:a aac -b:a 128k -ar 44100 \
            -f flv \
            -pix_fmt yuv420p \
            "${RTMP_URL}"
        return
    fi

    if [[ "${STREAM_AUDIO_SOURCE}" == "music" ]]; then
        echo "[supervisor] STREAM_AUDIO_SOURCE=music is no longer supported; using PulseAudio default source."
    else
        echo "[supervisor] Using PulseAudio default source."
    fi
    ffmpeg \
        -f x11grab -video_size 1280x720 -framerate 30 -i "${DISPLAY}" \
        -f pulse -ac 2 -i default \
        -c:v libx264 -preset veryfast -b:v 2500k -maxrate 2500k -bufsize 5000k \
        -g 60 -keyint_min 60 \
        -c:a aac -b:a 128k -ar 44100 \
        -f flv \
        -pix_fmt yuv420p \
        "${RTMP_URL}"
}

while true; do
    echo "[supervisor] Starting FFmpeg stream to Twitch..."
    ffmpeg_cmd || true
    echo "[supervisor] FFmpeg exited. Restarting in 5 seconds..."
    sleep 5
done
