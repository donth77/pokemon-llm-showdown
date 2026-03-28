"""
Overlay service — tracks match results and serves a transparent scoreboard overlay.
"""

import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Pokémon Battle Overlay")
templates = Jinja2Templates(directory="templates")

DATA_FILE = Path("/data/results.json")
REPLAY_DIR = Path("/replays")
LOG_DIR = Path("/logs")
STATE_FILE = Path("/state/current_battle.json")
THOUGHTS_FILE = Path("/state/thoughts.json")
STREAM_TITLE = os.getenv("STREAM_TITLE", "Pokémon Showdown battles with LLMs")
HIDE_BATTLE_UI = os.getenv("HIDE_BATTLE_UI", "1").strip() in ("1", "true", "yes")


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        v = int(raw, 10)
        return v if v > 0 else default
    except ValueError:
        return default


# How long the post-battle victory splash stays fully visible (seconds).
VICTORY_MODAL_MS = _positive_int_env("VICTORY_MODAL_SECONDS", 30) * 1000

MAX_THOUGHTS_PER_PLAYER = 80
_thought_store: dict[str, list[dict]] = {}
_ws_clients: set[WebSocket] = set()


async def _broadcast(message: dict) -> None:
    if not _ws_clients:
        return
    data = json.dumps(message)
    dead: set[WebSocket] = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)

RECENT_MATCHES_COUNT = 10

app.mount(
    "/replays/files",
    StaticFiles(directory=str(REPLAY_DIR), html=False),
    name="replay-files",
)
app.mount(
    "/logs/files", StaticFiles(directory=str(LOG_DIR), html=False), name="log-files"
)
app.mount("/static", StaticFiles(directory="static", html=False), name="static")


def _load_data() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {"matches": [], "wins": {}}


def _save_data(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=2))


def _load_current_battle_state() -> dict:
    """
    Load the current live battle metadata written by `agents/orchestrator.py`.
    Used to render stable player matchup names on the overlay.
    """
    if not STATE_FILE.exists():
        return {}
    try:
        state = json.loads(STATE_FILE.read_text())
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {}


def _display_model_id(model_id: str | None) -> str:
    """UI-only label: strip OpenRouter routing prefix; state files keep full ids."""
    if model_id is None:
        return ""
    s = str(model_id).strip()
    prefix = "openrouter/"
    if s.lower().startswith(prefix):
        rest = s[len(prefix) :].lstrip()
        return rest if rest else s
    return s


@app.get("/scoreboard", response_class=JSONResponse)
async def get_scoreboard():
    data = _load_data()
    state = _load_current_battle_state()
    total_matches = len(data["matches"])
    p1_name = state.get("player1_name") or "Player 1"
    p2_name = state.get("player2_name") or "Player 2"
    p1_model = _display_model_id(state.get("player1_model_id") or "")
    p2_model = _display_model_id(state.get("player2_model_id") or "")
    p1_slug = state.get("player1_persona_slug") or ""
    p2_slug = state.get("player2_persona_slug") or ""
    p1_sprite = state.get("player1_sprite_url") or ""
    p2_sprite = state.get("player2_sprite_url") or ""
    recent_matches = list(reversed(data["matches"]))[:RECENT_MATCHES_COUNT]
    return {
        "total_matches": total_matches,
        "wins": data["wins"],
        "player1_name": p1_name,
        "player2_name": p2_name,
        "player1_model_id": p1_model,
        "player2_model_id": p2_model,
        "player1_persona_slug": p1_slug,
        "player2_persona_slug": p2_slug,
        "player1_sprite_url": p1_sprite,
        "player2_sprite_url": p2_sprite,
        "last_match": data["matches"][-1] if data["matches"] else None,
        "recent_matches": recent_matches,
    }


@app.post("/result", response_class=JSONResponse)
async def post_result(request: Request):
    body = await request.json()
    winner = body.get("winner", "Unknown")
    loser = body.get("loser", "Unknown")
    timestamp = body.get("timestamp", 0)

    data = _load_data()
    battle_format = body.get("battle_format", "")
    duration = body.get("duration", 0)

    data["matches"].append(
        {
            "winner": winner,
            "loser": loser,
            "timestamp": timestamp,
            "battle_format": battle_format,
            "duration": duration,
        }
    )
    data["wins"][winner] = data["wins"].get(winner, 0) + 1
    data["wins"].setdefault(loser, 0)
    _save_data(data)

    return {"status": "ok", "total_matches": len(data["matches"]), "wins": data["wins"]}


@app.get("/overlay", response_class=HTMLResponse)
async def get_overlay(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="overlay.html",
        context={"request": request},
    )


@app.get("/victory", response_class=HTMLResponse)
async def get_victory_splash(request: Request):
    """Full-frame transparent overlay: animated winner announcement after each battle."""
    return templates.TemplateResponse(
        request=request,
        name="victory.html",
        context={
            "request": request,
            "victory_modal_ms": VICTORY_MODAL_MS,
        },
    )


@app.get("/replays", response_class=HTMLResponse)
async def get_replays(request: Request):
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    replay_files = sorted(
        REPLAY_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_files = {p.stem for p in LOG_DIR.glob("*.json")}
    return templates.TemplateResponse(
        request=request,
        name="replays.html",
        context={
            "request": request,
            "replay_files": [p.name for p in replay_files],
            "log_files": log_files,
        },
    )


@app.get("/broadcast", response_class=HTMLResponse)
async def get_broadcast(request: Request):
    showdown_base = "http://showdown:8000/"
    showdown_local = "http://localhost:8000/"
    if HIDE_BATTLE_UI:
        showdown_base += "?hide_battle_ui=1"
        showdown_local += "?hide_battle_ui=1"
    return templates.TemplateResponse(
        request=request,
        name="broadcast.html",
        context={
            "request": request,
            "showdown_internal_url": showdown_base,
            "showdown_local_url": showdown_local,
            "stream_title": STREAM_TITLE,
            "hide_battle_ui": HIDE_BATTLE_UI,
        },
    )


@app.get("/current_battle", response_class=JSONResponse)
async def get_current_battle():
    if not STATE_FILE.exists():
        return {"status": "idle", "battle_tag": None}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"status": "error", "battle_tag": None}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/thoughts", response_class=JSONResponse)
async def get_thoughts():
    if not THOUGHTS_FILE.exists():
        return {"battle_tag": None, "updated_at": 0, "players": {}}
    try:
        payload = json.loads(THOUGHTS_FILE.read_text())
        if not isinstance(payload, dict):
            raise ValueError("Invalid thoughts payload")
        return {
            "battle_tag": payload.get("battle_tag"),
            "updated_at": payload.get("updated_at", 0),
            "players": payload.get("players", {}),
        }
    except Exception:
        return {"battle_tag": None, "updated_at": 0, "players": {}}


@app.post("/thought", response_class=JSONResponse)
async def post_thought(request: Request):
    body = await request.json()
    player = str(body.get("player", "")).strip()
    bs = str(body.get("battle_side", "")).strip().lower()
    battle_side = bs if bs in ("p1", "p2") else ""
    thought = {
        "timestamp": body.get("timestamp", time.time()),
        "turn": body.get("turn"),
        "action": str(body.get("action", "")),
        "reasoning": str(body.get("reasoning", "")),
        "callout": str(body.get("callout", "")),
        "battle_side": battle_side,
    }
    if player:
        items = _thought_store.setdefault(player, [])
        items.append(thought)
        if len(items) > MAX_THOUGHTS_PER_PLAYER:
            _thought_store[player] = items[-MAX_THOUGHTS_PER_PLAYER:]
    await _broadcast({"type": "thought", "player": player, **thought})
    return {"status": "ok"}


@app.post("/thoughts/clear", response_class=JSONResponse)
async def clear_thoughts_endpoint():
    _thought_store.clear()
    await _broadcast({"type": "clear"})
    return {"status": "ok"}


@app.websocket("/thoughts/ws")
async def thoughts_ws(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        await ws.send_text(
            json.dumps({"type": "history", "players": _thought_store})
        )
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _ws_clients.discard(ws)
