"""
Web service — HTTP API for the stack: scoreboard, broadcast, manager, replays, thoughts.

The route ``/overlay`` is still the transparent scoreboard page for stream compositing;
the Docker service is named ``web``.

OBS / multi-source layouts can use ``/thoughts_overlay``, ``/broadcast/top_bar``,
and ``/broadcast/battle_frame`` alongside ``/overlay``, ``/victory``, and ``/match_intro`` (see README).
"""

import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from manager import db
from manager.personas_store import resolve_portrait_url
from manager.routes import router as manager_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    yield


app = FastAPI(title="Pokémon LLM Showdown — Web", lifespan=lifespan)
app.include_router(manager_router)
templates = Jinja2Templates(directory="templates")
_BOOT_TS = str(int(time.time()))
templates.env.globals["cache_bust"] = _BOOT_TS

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


def _non_negative_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        v = int(raw, 10)
        return v if v >= 0 else default
    except ValueError:
        return default


VICTORY_MODAL_MS = _positive_int_env("VICTORY_MODAL_SECONDS", 30) * 1000
TOURNAMENT_VICTORY_MODAL_MS = _positive_int_env(
    "TOURNAMENT_VICTORY_MODAL_SECONDS", 60
) * 1000
VICTORY_SHOW_DELAY_MS = _non_negative_int_env("VICTORY_SHOW_DELAY_SECONDS", 1) * 1000
MATCH_INTRO_MS = _non_negative_int_env("MATCH_INTRO_SECONDS", 5) * 1000

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


def _load_current_battle_state() -> dict:
    """Load the current live battle metadata written by the queue worker."""
    if not STATE_FILE.exists():
        return {}
    try:
        state = json.loads(STATE_FILE.read_text())
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {}


# Merged into /current_battle when ``match_id`` is set (broadcast tournament pill).
_CURRENT_BATTLE_TOURNEY_KEYS = (
    "tournament_id",
    "tournament_name",
    "tournament_type",
    "tournament_best_of",
    "series_bracket",
    "series_round_number",
    "series_match_position",
    "tournament_max_winners_round",
    "game_number",
)


def _tournament_context_from_state(state: dict) -> dict:
    out: dict = {}
    for key in _CURRENT_BATTLE_TOURNEY_KEYS:
        v = state.get(key)
        if v is not None:
            out[key] = v
    return out


async def _hydrate_current_battle_tournament(state: dict) -> dict:
    """Fill tournament overlay fields from SQLite so the broadcast pill works even if
    the agents container wrote an older current_battle.json shape."""
    mid = state.get("match_id")
    if mid is None:
        return state
    try:
        mid_int = int(mid)
    except (TypeError, ValueError):
        return state
    mrow = await db.get_match(mid_int)
    if not mrow:
        return state
    enriched = await db.enrich_match_row_with_series_tournament(mrow)
    for key in _CURRENT_BATTLE_TOURNEY_KEYS:
        v = enriched.get(key)
        if v is not None:
            state[key] = v
    return state


def _tournament_intro_roster_for_api(state: dict) -> list[dict]:
    raw = state.get("tournament_intro_roster")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        slug = (item.get("persona_slug") or "").strip()
        if not slug:
            continue
        row = {
            "persona_slug": slug,
            "portrait_square_url": _safe_square_portrait_url(slug),
        }
        seed = item.get("seed")
        if seed is not None:
            row["seed"] = seed
        out.append(row)
    return out


def _safe_square_portrait_url(slug: str | None) -> str:
    raw = (slug or "").strip()
    if not raw:
        return ""
    try:
        u = resolve_portrait_url(raw, square=True)
    except ValueError:
        return ""
    return u or ""


def _enrich_match_row_portrait_urls(m: dict) -> dict:
    """Attach square portrait URLs from stored persona slugs (for last_match / recent rows)."""
    out = dict(m)
    out["player1_portrait_square_url"] = _safe_square_portrait_url(
        out.get("player1_persona")
    )
    out["player2_portrait_square_url"] = _safe_square_portrait_url(
        out.get("player2_persona")
    )
    return out


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
    """Scoreboard data sourced from SQLite + live battle state file."""
    scoreboard = await db.get_scoreboard_data(RECENT_MATCHES_COUNT)
    recent_matches = [
        _enrich_match_row_portrait_urls(dict(r))
        for r in scoreboard["recent_matches"]
    ]
    last_match_payload = recent_matches[0] if recent_matches else None
    state = _load_current_battle_state()
    if isinstance(state, dict) and state.get("match_id") is not None:
        try:
            state = await _hydrate_current_battle_tournament(dict(state))
        except Exception:
            pass

    p1_name = state.get("player1_name") or "Player 1"
    p2_name = state.get("player2_name") or "Player 2"
    p1_model = _display_model_id(state.get("player1_model_id") or "")
    p2_model = _display_model_id(state.get("player2_model_id") or "")

    wins_out = scoreboard["wins"]
    scope = "all_time"
    footnote = None
    sid = state.get("series_id")
    if sid is not None and state.get("series_best_of") is not None:
        try:
            p1w = int(state.get("series_player1_wins", 0))
            p2w = int(state.get("series_player2_wins", 0))
            bo = int(state["series_best_of"])
            wins_out = {p1_name: p1w, p2_name: p2w}
            scope = "series"
            footnote = f"Best of {bo}"
        except (TypeError, ValueError):
            pass

    return {
        "total_matches": scoreboard["total_matches"],
        "wins": wins_out,
        "scoreboard_scope": scope,
        "series_footnote": footnote,
        "player1_name": p1_name,
        "player2_name": p2_name,
        "player1_model_id": p1_model,
        "player2_model_id": p2_model,
        "player1_persona_slug": state.get("player1_persona_slug") or "",
        "player2_persona_slug": state.get("player2_persona_slug") or "",
        "player1_sprite_url": state.get("player1_sprite_url") or "",
        "player2_sprite_url": state.get("player2_sprite_url") or "",
        "player1_portrait_square_url": _safe_square_portrait_url(
            state.get("player1_persona_slug")
        ),
        "player2_portrait_square_url": _safe_square_portrait_url(
            state.get("player2_persona_slug")
        ),
        "battle_status": (state.get("status") or "idle"),
        "battle_format": state.get("battle_format") or "",
        "battle_tag": state.get("battle_tag"),
        "battle_updated_at": state.get("updated_at"),
        "match_id": state.get("match_id"),
        "tournament_context": _tournament_context_from_state(state),
        "tournament_intro_roster": _tournament_intro_roster_for_api(state),
        "last_match": last_match_payload,
        "recent_matches": recent_matches,
    }


def _legacy_result_winner_side(body: dict) -> str:
    ws = (body.get("winner_side") or "").strip().lower()
    if ws in ("p1", "p2"):
        return ws
    w = (body.get("winner") or "").strip()
    p1n = (body.get("player1_name") or "").strip()
    p2n = (body.get("player2_name") or "").strip()
    if p1n and w == p1n:
        return "p1"
    if p2n and w == p2n:
        return "p2"
    return "p1"


@app.post("/result", response_class=JSONResponse)
async def post_result(request: Request):
    """Legacy result endpoint — kept for backward compatibility.

    The queue worker reports via /api/manager/matches/{id}/complete.
    Callers should send ``winner_side`` or ``player1_name`` / ``player2_name`` for
    correct stats when ``winner`` is a Showdown display name.
    """
    body = await request.json()
    winner = body.get("winner", "Unknown")
    loser = body.get("loser", "Unknown")
    battle_format = body.get("battle_format", "")
    duration = body.get("duration", 0)
    winner_side = _legacy_result_winner_side(body)

    def _s(key: str, default: str = "unknown") -> str:
        v = body.get(key)
        if v is None or str(v).strip() == "":
            return default
        return str(v).strip()

    m = await db.create_match(
        battle_format=battle_format,
        player1_provider=_s("player1_provider"),
        player1_model=_s("player1_model"),
        player1_persona=_s("player1_persona"),
        player2_provider=_s("player2_provider"),
        player2_model=_s("player2_model"),
        player2_persona=_s("player2_persona"),
    )
    await db.complete_match(
        m["id"],
        winner=winner,
        loser=loser,
        winner_side=winner_side,
        duration=duration,
    )

    scoreboard = await db.get_scoreboard_data()
    return {"status": "ok", "total_matches": scoreboard["total_matches"], "wins": scoreboard["wins"]}


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
            "tournament_victory_modal_ms": TOURNAMENT_VICTORY_MODAL_MS,
            "victory_show_delay_ms": VICTORY_SHOW_DELAY_MS,
        },
    )


@app.get("/match_intro", response_class=HTMLResponse)
async def get_match_intro(request: Request):
    """Transparent overlay: matchup card when agents set current_battle status ``starting``."""
    return templates.TemplateResponse(
        request=request,
        name="match_intro.html",
        context={
            "request": request,
            "match_intro_ms": MATCH_INTRO_MS,
        },
    )


@app.get("/tournament_intro", response_class=HTMLResponse)
async def get_tournament_intro(request: Request):
    """Transparent overlay: opening card before the first match of a tournament."""
    return templates.TemplateResponse(
        request=request,
        name="tournament_intro.html",
        context={"request": request},
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


@app.get("/thoughts_overlay", response_class=HTMLResponse)
async def get_thoughts_overlay(request: Request):
    """Transparent 1280×720 LLM thoughts panels for OBS Browser Sources."""
    return templates.TemplateResponse(
        request=request,
        name="thoughts_overlay.html",
        context={
            "request": request,
            "stream_title": STREAM_TITLE,
            "hide_battle_ui": HIDE_BATTLE_UI,
        },
    )


@app.get("/broadcast/top_bar", response_class=HTMLResponse)
async def get_broadcast_top_bar(request: Request):
    """Transparent stream title + battle format bar (matches /broadcast top-left)."""
    return templates.TemplateResponse(
        request=request,
        name="broadcast_top_bar.html",
        context={
            "request": request,
            "stream_title": STREAM_TITLE,
        },
    )


@app.get("/broadcast/battle_frame", response_class=HTMLResponse)
async def get_broadcast_battle_frame(request: Request):
    """Showdown iframe + battle sync + in-frame callouts only (no scoreboard/thoughts UI)."""
    showdown_base = "http://showdown:8000/"
    showdown_local = "http://localhost:8000/"
    if HIDE_BATTLE_UI:
        showdown_base += "?hide_battle_ui=1"
        showdown_local += "?hide_battle_ui=1"
    return templates.TemplateResponse(
        request=request,
        name="broadcast_battle_frame.html",
        context={
            "request": request,
            "showdown_internal_url": showdown_base,
            "showdown_local_url": showdown_local,
        },
    )


@app.get("/current_battle", response_class=JSONResponse)
async def get_current_battle():
    if not STATE_FILE.exists():
        return {"status": "idle", "battle_tag": None}
    try:
        data = json.loads(STATE_FILE.read_text())
    except Exception:
        return {"status": "error", "battle_tag": None}
    if isinstance(data, dict) and data.get("match_id") is not None:
        try:
            data = await _hydrate_current_battle_tournament(data)
        except Exception:
            pass
    return data


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
