"""
Launch Chromium in kiosk mode pointed at the web service broadcast scene.

The `/broadcast` page embeds the Showdown battle view and renders
stream UI (title, scoreboard, thoughts).  Chromium is launched directly
(not via Playwright) so that --kiosk reliably hides all browser chrome
from the Xvfb display that FFmpeg captures.
"""

import os
import shutil
import subprocess
import sys
import time

import requests

from env_bool import parse_env_bool
from log_print import log_print

SHOWDOWN_HOST = os.getenv("SHOWDOWN_HOST", "showdown")
SHOWDOWN_PORT = int(os.getenv("SHOWDOWN_PORT", "8000"))
HIDE_BATTLE_UI = parse_env_bool("HIDE_BATTLE_UI", default=True)
_SHOWDOWN_BASE = f"http://{SHOWDOWN_HOST}:{SHOWDOWN_PORT}/"
SHOWDOWN_URL = (
    f"{_SHOWDOWN_BASE}?hide_battle_ui=1" if HIDE_BATTLE_UI else _SHOWDOWN_BASE
)
WEB_HOST = os.getenv("WEB_HOST") or os.getenv("OVERLAY_HOST", "web")
WEB_PORT = int(os.getenv("WEB_PORT") or os.getenv("OVERLAY_PORT", "8080"))
WEB_BASE = f"http://{WEB_HOST}:{WEB_PORT}"
STREAM_VIEW_URL = os.getenv("STREAM_VIEW_URL", f"{WEB_BASE}/broadcast")


def wait_for_http(url: str, name: str) -> None:
    """Block until an HTTP endpoint is responding."""
    log_print(f"Waiting for {name} at {url} ...", flush=True)
    while True:
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                log_print(f"{name} is up!", flush=True)
                return
        except Exception:
            pass
        time.sleep(2)


def find_chromium() -> str:
    """Locate the Chromium binary installed by Playwright or system packages."""
    for candidate in [
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
    ]:
        if candidate:
            return candidate

    # Playwright installs Chromium under ~/.cache/ms-playwright/
    pw_root = os.path.expanduser("~/.cache/ms-playwright")
    if os.path.isdir(pw_root):
        for entry in sorted(os.listdir(pw_root), reverse=True):
            candidate = os.path.join(pw_root, entry, "chrome-linux", "chrome")
            if os.path.isfile(candidate):
                return candidate

    sys.exit("Could not find a Chromium binary")


def main() -> None:
    wait_for_http(SHOWDOWN_URL, "Showdown")
    wait_for_http(f"{WEB_BASE}/health", "Web")
    wait_for_http(STREAM_VIEW_URL, "Broadcast view")

    chromium = find_chromium()
    log_print(f"Using Chromium: {chromium}", flush=True)

    cmd = [
        chromium,
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--autoplay-policy=no-user-gesture-required",
        "--window-size=1280,720",
        "--window-position=0,0",
        "--kiosk",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
        "--disable-session-crashed-bubble",
        "--disable-translate",
        "--disable-features=TranslateUI,IsolateOrigins,site-per-process",
        "--user-data-dir=/tmp/chromium-stream",
        "--disable-web-security",
        "--disable-site-isolation-trials",
        "--allow-running-insecure-content",
        "--cursor=none",
        "--remote-debugging-port=9222",
        "--remote-debugging-address=0.0.0.0",
        STREAM_VIEW_URL,
    ]

    log_print(f"Launching: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)
    proc.wait()
    log_print(f"Chromium exited with code {proc.returncode}", flush=True)


if __name__ == "__main__":
    main()
