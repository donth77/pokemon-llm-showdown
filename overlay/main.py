"""
Overlay service — tracks match results and serves a transparent scoreboard overlay.
"""

import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Pokemon Battle Overlay")
templates = Jinja2Templates(directory="templates")

DATA_FILE = Path("/data/results.json")
REPLAY_DIR = Path("/replays")
LOG_DIR = Path("/logs")
STATE_FILE = Path("/state/current_battle.json")
THOUGHTS_FILE = Path("/state/thoughts.json")
STREAM_TITLE = os.getenv("STREAM_TITLE", "Testing Pokemon Showdown battles with LLMs")
HIDE_BATTLE_UI = os.getenv("HIDE_BATTLE_UI", "1").strip() in ("1", "true", "yes")

app.mount(
    "/replays/files",
    StaticFiles(directory=str(REPLAY_DIR), html=False),
    name="replay-files",
)
app.mount(
    "/logs/files", StaticFiles(directory=str(LOG_DIR), html=False), name="log-files"
)


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


@app.get("/scoreboard", response_class=JSONResponse)
async def get_scoreboard():
    data = _load_data()
    state = _load_current_battle_state()
    total_matches = len(data["matches"])
    p1_name = state.get("player1_name") or "Player 1"
    p2_name = state.get("player2_name") or "Player 2"
    p1_model = state.get("player1_model_id") or ""
    p2_model = state.get("player2_model_id") or ""
    return {
        "total_matches": total_matches,
        "wins": data["wins"],
        "player1_name": p1_name,
        "player2_name": p2_name,
        "player1_model_id": p1_model,
        "player2_model_id": p2_model,
        "last_match": data["matches"][-1] if data["matches"] else None,
    }


@app.post("/result", response_class=JSONResponse)
async def post_result(request: Request):
    body = await request.json()
    winner = body.get("winner", "Unknown")
    loser = body.get("loser", "Unknown")
    timestamp = body.get("timestamp", 0)

    data = _load_data()
    data["matches"].append(
        {
            "winner": winner,
            "loser": loser,
            "timestamp": timestamp,
        }
    )
    data["wins"][winner] = data["wins"].get(winner, 0) + 1
    data["wins"].setdefault(loser, 0)
    _save_data(data)

    return {"status": "ok", "total_matches": len(data["matches"]), "wins": data["wins"]}


@app.get("/overlay", response_class=HTMLResponse)
async def get_overlay(request: Request):
    data = _load_data()
    wins = data["wins"]
    state = _load_current_battle_state()
    names = list(wins.keys())
    player1_name = state.get("player1_name") or (
        names[0] if len(names) >= 1 else "Player 1"
    )
    player2_name = state.get("player2_name") or (
        names[1] if len(names) >= 2 else "Player 2"
    )
    player1_model_id = state.get("player1_model_id") or ""
    player2_model_id = state.get("player2_model_id") or ""
    return templates.TemplateResponse(
        request=request,
        name="overlay.html",
        context={
            "request": request,
            "wins": wins,
            "player1_name": player1_name,
            "player2_name": player2_name,
            "player1_model_id": player1_model_id,
            "player2_model_id": player2_model_id,
            "player1_wins": wins.get(player1_name, 0),
            "player2_wins": wins.get(player2_name, 0),
            "total_matches": len(data["matches"]),
            "last_match": data["matches"][-1] if data["matches"] else None,
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
