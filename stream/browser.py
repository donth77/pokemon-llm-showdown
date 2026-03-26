"""
Launch headless Chromium pointed directly at Pokemon Showdown.

Opens Showdown without an iframe (avoiding Showdown's iframe detection),
injects a stream overlay (title + scoreboard) on top, and auto-navigates
to live battles by polling the overlay service.
"""

import asyncio
import json
import os
import time

import requests
from playwright.async_api import async_playwright, Page

SHOWDOWN_HOST = os.getenv("SHOWDOWN_HOST", "showdown")
SHOWDOWN_PORT = int(os.getenv("SHOWDOWN_PORT", "8000"))
SHOWDOWN_URL = f"http://{SHOWDOWN_HOST}:{SHOWDOWN_PORT}/"
OVERLAY_HOST = os.getenv("OVERLAY_HOST", "overlay")
OVERLAY_PORT = int(os.getenv("OVERLAY_PORT", "8080"))
OVERLAY_BASE = f"http://{OVERLAY_HOST}:{OVERLAY_PORT}"
STREAM_TITLE = os.getenv(
    "STREAM_TITLE",
    os.getenv("TWITCH_STREAM_TITLE", "Testing Pokemon Showdown battles with LLMs"),
)

OVERLAY_INIT_JS = """
(function() {
    function inject() {
        if (document.getElementById('stream-overlay')) return;
        if (!document.body) { requestAnimationFrame(inject); return; }

        var overlay = document.createElement('div');
        overlay.id = 'stream-overlay';
        overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:99999;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;';

        var title = document.createElement('div');
        title.id = 'stream-title';
        title.style.cssText = 'position:absolute;left:16px;top:16px;padding:8px 12px;border-radius:8px;background:rgba(0,0,0,0.55);color:#fff;font-size:18px;font-weight:700;letter-spacing:0.2px;text-shadow:0 1px 2px rgba(0,0,0,0.8);';
        title.textContent = window.__STREAM_TITLE__ || '';
        overlay.appendChild(title);

        var sb = document.createElement('div');
        sb.id = 'stream-scoreboard';
        sb.style.cssText = 'position:absolute;top:16px;right:16px;background:rgba(0,0,0,0.75);border:2px solid rgba(255,255,255,0.15);border-radius:12px;padding:16px 24px;min-width:200px;backdrop-filter:blur(8px);color:#fff;';
        sb.innerHTML = '<div style="font-size:14px;text-transform:uppercase;letter-spacing:2px;color:rgba(255,255,255,0.6);margin-bottom:12px;text-align:center;">Scoreboard</div><div id="sb-content"></div>';
        overlay.appendChild(sb);

        var lw = document.createElement('div');
        lw.id = 'stream-last-winner';
        lw.style.cssText = 'position:absolute;bottom:16px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,0.7);border-radius:8px;padding:8px 20px;font-size:14px;color:rgba(255,255,255,0.8);backdrop-filter:blur(8px);display:none;';
        overlay.appendChild(lw);

        document.body.appendChild(overlay);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', inject);
    } else {
        inject();
    }
})();
"""


def wait_for_http(url: str, name: str) -> None:
    """Block until an HTTP endpoint is responding."""
    print(f"Waiting for {name} at {url} ...", flush=True)
    while True:
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                print(f"{name} is up!", flush=True)
                return
        except Exception:
            pass
        time.sleep(2)


def fetch_json(url: str) -> dict:
    try:
        r = requests.get(url, timeout=3)
        return r.json()
    except Exception:
        return {}


def battle_url_from_tag(tag: str | None) -> str | None:
    if not tag:
        return None
    normalized = tag.strip().lstrip(">").lstrip("/")
    if not normalized:
        return None
    if not normalized.startswith("battle-"):
        normalized = f"battle-{normalized}"
    base = SHOWDOWN_URL if SHOWDOWN_URL.endswith("/") else f"{SHOWDOWN_URL}/"
    return f"{base}{normalized}"


async def inject_overlay(page: Page) -> None:
    """Ensure the overlay DOM exists on the current page."""
    try:
        await page.evaluate(
            "(title) => { window.__STREAM_TITLE__ = title; }",
            STREAM_TITLE,
        )
        await page.evaluate(OVERLAY_INIT_JS)
    except Exception:
        pass


async def update_scoreboard(page: Page, scoreboard: dict) -> None:
    """Push scoreboard data into the injected overlay."""
    try:
        await page.evaluate(
            """(data) => {
                var el = document.getElementById('sb-content');
                if (!el) return;
                var rows = '';
                rows += '<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.1);">';
                rows += '<span style="font-size:18px;font-weight:600;color:#ff6b6b;">ClaudeAggro</span>';
                rows += '<span style="font-size:22px;font-weight:700;">' + data.aggro + '</span></div>';
                rows += '<div style="display:flex;justify-content:space-between;padding:8px 0;">';
                rows += '<span style="font-size:18px;font-weight:600;color:#74b9ff;">ClaudeStall</span>';
                rows += '<span style="font-size:22px;font-weight:700;">' + data.stall + '</span></div>';
                rows += '<div style="text-align:center;font-size:12px;color:rgba(255,255,255,0.5);margin-top:10px;">';
                rows += data.total + ' match' + (data.total !== 1 ? 'es' : '') + ' played</div>';
                el.innerHTML = rows;

                var lw = document.getElementById('stream-last-winner');
                if (lw) {
                    if (data.lastWinner) {
                        lw.innerHTML = 'Last winner: <strong>' + data.lastWinner + '</strong>';
                        lw.style.display = '';
                    } else {
                        lw.style.display = 'none';
                    }
                }
            }""",
            {
                "aggro": scoreboard.get("wins", {}).get("ClaudeAggro", 0),
                "stall": scoreboard.get("wins", {}).get("ClaudeStall", 0),
                "total": scoreboard.get("total_matches", 0),
                "lastWinner": (scoreboard.get("last_match") or {}).get("winner", ""),
            },
        )
    except Exception:
        pass


async def main() -> None:
    wait_for_http(SHOWDOWN_URL, "Showdown")
    wait_for_http(f"{OVERLAY_BASE}/health", "Overlay")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--window-size=1280,720",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            no_viewport=False,
        )
        page = await context.new_page()

        await page.goto(SHOWDOWN_URL)
        await inject_overlay(page)
        print(f"Browser opened to {SHOWDOWN_URL}", flush=True)

        current_url = SHOWDOWN_URL
        last_battle_url: str | None = None

        while True:
            await asyncio.sleep(3)

            try:
                battle_data = await asyncio.to_thread(
                    fetch_json, f"{OVERLAY_BASE}/current_battle"
                )
                if battle_data.get("status") == "live" and battle_data.get("battle_tag"):
                    target = battle_url_from_tag(battle_data["battle_tag"])
                    if target and target != current_url:
                        print(f"Navigating to battle: {target}", flush=True)
                        await page.goto(target, wait_until="domcontentloaded")
                        current_url = target
                        last_battle_url = target
                        await inject_overlay(page)

                scoreboard = await asyncio.to_thread(
                    fetch_json, f"{OVERLAY_BASE}/scoreboard"
                )
                await update_scoreboard(page, scoreboard)

                has_overlay = await page.evaluate(
                    "() => !!document.getElementById('stream-overlay')"
                )
                if not has_overlay:
                    await inject_overlay(page)

            except Exception as e:
                print(f"Update loop error: {e}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
