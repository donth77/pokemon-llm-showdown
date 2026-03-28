"""
Manager routes — API + HTML pages for tournament / series / match management.

Mounted on the main FastAPI app as an APIRouter.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import (
    db,
    env_host_file as envfile,
    personas_store as pstore,
    showdown_accounts,
    tournament_logic,
)
from .env_registry import ENV_REGISTRY, REGISTRY_BY_KEY, categories_in_order
from .provider_model_validate import validate_provider_model

router = APIRouter()
templates = Jinja2Templates(directory="templates")
_log = logging.getLogger("uvicorn.error")


def _timestamp_fmt(epoch: float | int | None) -> str:
    """Jinja2 filter: epoch seconds → same style as overlay (UTC, en-GB-like + ' UTC')."""
    import datetime

    if epoch is None:
        return "—"
    try:
        ts = float(epoch)
    except (TypeError, ValueError):
        return "—"
    if ts <= 0:
        return "—"
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    mon = dt.strftime("%b")
    return f"{dt.day} {mon} {dt.year}, {dt.strftime('%H:%M')} UTC"


def _timestamp_fmt_slash(epoch: float | int | None) -> str:
    """Jinja2 filter: epoch → 'M/D/YYYY, h:MM AM/PM UTC' (Results page fallback; no TZ conversion)."""
    import datetime

    if epoch is None:
        return "—"
    try:
        ts = float(epoch)
    except (TypeError, ValueError):
        return "—"
    if ts <= 0:
        return "—"
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    h12 = dt.hour % 12
    if h12 == 0:
        h12 = 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{dt.month}/{dt.day}/{dt.year}, {h12}:{dt.minute:02d} {ampm} UTC"


templates.env.filters["timestamp_fmt"] = _timestamp_fmt
templates.env.filters["timestamp_fmt_slash"] = _timestamp_fmt_slash

PERSONAS_DIR = Path(os.getenv("PERSONAS_DIR", "/personas"))
STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
CURRENT_BATTLE_FILE = STATE_DIR / "current_battle.json"
# Browser URL for raw Showdown client (host/port or e.g. https://localhost.psim.us). Default: mapped showdown port.
SHOWDOWN_VIEW_BASE = os.getenv("SHOWDOWN_VIEW_BASE", "http://localhost:8000").rstrip("/")
_SHOWDOWN_USERNAME_MAX = 18

ALLOWED_PROVIDERS = ["anthropic", "deepseek", "openrouter"]


def _require_provider_model_pair(label: str, provider: str, model: str) -> None:
    try:
        validate_provider_model(provider, model, field_label=label)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None

BATTLE_FORMATS = [
    "gen9randombattle",
    "gen8randombattle",
    "gen7randombattle",
    "gen6randombattle",
    "gen5randombattle",
    "gen4randombattle",
    "gen3randombattle",
    "gen2randombattle",
    "gen1randombattle",
]


def _scan_personas() -> list[dict]:
    """Read persona markdown files and return slug + metadata for each."""
    if not PERSONAS_DIR.exists():
        return []
    personas = []
    for p in sorted(PERSONAS_DIR.glob("*.md")):
        slug = p.stem
        name = slug.capitalize()
        description = ""
        try:
            text = p.read_text(encoding="utf-8")
            if text.strip().startswith("---\n"):
                closing = text.find("\n---\n", 4)
                if closing != -1:
                    for line in text[4:closing].splitlines():
                        stripped = line.strip()
                        if ":" in stripped:
                            k, v = stripped.split(":", 1)
                            k = k.strip().lower()
                            if k == "name":
                                name = v.strip()
                            elif k == "description":
                                description = v.strip()
        except Exception:
            pass
        personas.append({"slug": slug, "name": name, "description": description})
    return personas


def _normalize_showdown_battle_path(battle_tag: str | None) -> str | None:
    if not battle_tag:
        return None
    t = str(battle_tag).strip().lstrip(">").lstrip("/")
    if not t:
        return None
    if not t.startswith("battle-"):
        t = f"battle-{t}"
    return f"/{t}"


def _read_live_showdown_battle_url() -> str | None:
    if not CURRENT_BATTLE_FILE.exists():
        return None
    try:
        data = json.loads(CURRENT_BATTLE_FILE.read_text(encoding="utf-8"))
        if data.get("status") != "live":
            return None
        path = _normalize_showdown_battle_path(data.get("battle_tag"))
        return f"{SHOWDOWN_VIEW_BASE}{path}" if path else None
    except Exception:
        return None


def _persona_battle_name(yaml_name: str) -> str:
    """Strip spaces from YAML display name — matches battle usernames (e.g. Damage Dan → DamageDan)."""
    return re.sub(r"\s+", "", (yaml_name or "").strip()) or "Persona"


def persona_slug_label(slug: str | None) -> str:
    """e.g. aggro → 'aggro (DamageDan)' from persona front matter; unknown slug returns the slug."""
    if not slug:
        return ""
    for p in _scan_personas():
        if p["slug"] == slug:
            battle = _persona_battle_name(p["name"])
            return f"{slug} ({battle})"
    return slug


def persona_dashboard_label(slug: str | None) -> str:
    """Stream-style label: 'DamageDan (aggro)'; unknown slug returns the slug."""
    if not slug:
        return ""
    for p in _scan_personas():
        if p["slug"] == slug:
            battle = _persona_battle_name(p["name"])
            return f"{battle} ({slug})"
    return slug


def persona_slug_only(battle_or_slug: str | None) -> str:
    """Resolve stored winner/loser (battle name or slug) to display slug; Draw/empty unknown unchanged."""
    if not battle_or_slug:
        return "—"
    if battle_or_slug == "Draw":
        return "Draw"
    s = str(battle_or_slug)
    for p in _scan_personas():
        slug = p["slug"]
        battle = _persona_battle_name(p["name"])
        if battle == s or slug == s:
            return slug
        if battle and s.startswith(battle) and len(s) > len(battle):
            rest = s[len(battle) :]
            if rest.isdigit():
                return f"{slug}{rest}"
    return battle_or_slug


def persona_battle_label(battle_or_slug: str | None) -> str:
    """Map stored winner/loser string to 'aggro (DamageDan)'; supports DamageDan2 → aggro2 (DamageDan2)."""
    if not battle_or_slug:
        return "—"
    if battle_or_slug == "Draw":
        return "Draw"
    s = str(battle_or_slug)
    for p in _scan_personas():
        slug = p["slug"]
        battle = _persona_battle_name(p["name"])
        if battle == s or slug == s:
            return f"{slug} ({battle})"
        if battle and s.startswith(battle) and len(s) > len(battle):
            rest = s[len(battle) :]
            if rest.isdigit():
                return f"{slug}{rest} ({s})"
    return battle_or_slug


def _battle_name_for_persona_slug(slug: str | None) -> str:
    if not slug:
        return ""
    for p in _scan_personas():
        if p["slug"] == slug:
            return _persona_battle_name(p["name"])
    return str(slug)


def _persona_yaml_stream_name(persona_slug: str | None) -> str:
    """Human-facing name from persona front matter (stream overlay; no slug, no compact battle id)."""
    if not persona_slug:
        return ""
    for p in _scan_personas():
        if p["slug"] == persona_slug:
            n = (p.get("name") or "").strip()
            return n or str(persona_slug).replace("_", " ").title()
    return str(persona_slug).replace("_", " ").title()


def _stream_label_from_persona_and_display(persona_slug: str, display_slug: str) -> str:
    """YAML name plus optional tournament index (e.g. damage dan roster slot 4 → 'Damage Dan 4')."""
    yaml_name = _persona_yaml_stream_name(persona_slug)
    if not display_slug or display_slug == persona_slug:
        return yaml_name
    if display_slug.startswith(persona_slug) and len(display_slug) > len(persona_slug):
        rest = display_slug[len(persona_slug) :]
        if rest.isdigit():
            return f"{yaml_name} {rest}".strip()
    return yaml_name


def _battle_mirror_suffix(base: str, n: int) -> str:
    """Match agents Showdown naming when the same persona faces itself (DamageDan1 vs DamageDan2)."""
    suffix = str(n)
    room = _SHOWDOWN_USERNAME_MAX - len(suffix)
    if room < 1:
        return suffix[-_SHOWDOWN_USERNAME_MAX:]
    return (base or "")[:room] + suffix


def _numbered_battle_name_for_tournament_slot(
    base_battle: str, persona_slug: str, display_slug: str
) -> str:
    """
    Align manager labels with Showdown usernames for tournament rosters:
    display slug aggro4 → DamageDan4; lone aggro in the bracket stays DamageDan.
    """
    if not persona_slug or not display_slug:
        return base_battle
    if display_slug == persona_slug:
        return base_battle
    if display_slug.startswith(persona_slug):
        suf = display_slug[len(persona_slug) :]
        if suf.isdigit():
            return _battle_mirror_suffix(base_battle, int(suf))
    return base_battle


async def _resolve_persona_pair_labels(
    *,
    p1: str | None,
    p2: str | None,
    series_id: int | None,
) -> dict[str, str]:
    """Dashboard labels; tournament dupes use aggro1…n + DamageDan1…n; standalone mirror uses aggro1/2 + suffixed battle names."""
    p1s = (p1 or "").strip() or None
    p2s = (p2 or "").strip() or None
    b1 = _battle_name_for_persona_slug(p1s)
    b2 = _battle_name_for_persona_slug(p2s)
    ds1 = p1s or ""
    ds2 = p2s or ""

    e1: int | None = None
    e2: int | None = None
    tid: int | None = None
    if series_id is not None:
        sm = await db.get_series_bracket_meta(series_id)
        if sm:
            e1, e2, tid = sm[0], sm[1], sm[2]

    emap: dict[int, str] | None = None
    if tid is not None and (e1 is not None or e2 is not None):
        emap = await db.tournament_entry_display_slug_map(tid)

    used_map = False
    if emap is not None and p1s and p2s and e1 is not None and e2 is not None:
        ds1 = emap.get(int(e1), p1s)
        ds2 = emap.get(int(e2), p2s)
        used_map = True
        b1 = _numbered_battle_name_for_tournament_slot(b1, p1s, ds1)
        b2 = _numbered_battle_name_for_tournament_slot(b2, p2s, ds2)
    elif emap is not None:
        if p1s and e1 is not None:
            ds1 = emap.get(int(e1), p1s)
            b1 = _numbered_battle_name_for_tournament_slot(b1, p1s, ds1)
        if p2s and e2 is not None:
            ds2 = emap.get(int(e2), p2s)
            b2 = _numbered_battle_name_for_tournament_slot(b2, p2s, ds2)

    if p1s and p2s:
        if not used_map and p1s == p2s:
            ds1 = f"{p1s}1"
            ds2 = f"{p2s}2"
            b1 = _battle_mirror_suffix(b1, 1)
            b2 = _battle_mirror_suffix(b2, 2)

    dash1 = "TBD" if not p1s else f"{b1} ({ds1})"
    dash2 = "TBD" if not p2s else f"{b2} ({ds2})"
    slug1 = "TBD" if not p1s else f"{ds1} ({b1})"
    slug2 = "TBD" if not p2s else f"{ds2} ({b2})"
    stream1 = "TBD" if not p1s else _stream_label_from_persona_and_display(p1s, ds1)
    stream2 = "TBD" if not p2s else _stream_label_from_persona_and_display(p2s, ds2)
    return {
        "dash1": dash1,
        "dash2": dash2,
        "slug1": slug1,
        "slug2": slug2,
        "ds1": ds1,
        "ds2": ds2,
        "stream1": stream1,
        "stream2": stream2,
    }


def _queue_player_label_after_persona(
    persona_label: str, provider: str | None, model: str | None
) -> str:
    """If no persona slug, fall back to provider/model so queue UI still names the side."""
    if persona_label != "TBD":
        return persona_label
    p, m = (provider or "").strip(), (model or "").strip()
    if m and p:
        return f"{p}/{m}"
    if m:
        return m
    if p:
        return p
    return "TBD"


def _queue_stream_or_fallback(
    stream: str, dash: str, provider: str | None, model: str | None
) -> str:
    """Stream overlay label: YAML name when known; else same model/provider fallback as dash labels."""
    if stream != "TBD":
        return stream
    return _queue_player_label_after_persona(dash, provider, model)


def _apply_tournament_opponent_tbd_labels(d: dict) -> None:
    tid = d.get("tournament_id")
    if tid is None:
        return
    l1, l2 = d["player1_persona_label"], d["player2_persona_label"]
    if l1 != "TBD" and l2 == "TBD":
        d["player2_persona_label"] = "Opponent TBD"
        d["player2_stream_label"] = "Opponent TBD"
    elif l2 != "TBD" and l1 == "TBD":
        d["player1_persona_label"] = "Opponent TBD"
        d["player1_stream_label"] = "Opponent TBD"


async def _enrich_series_api_payload(s: dict) -> dict:
    """Add persona display labels for manager UI + polling clients."""
    out = dict(s)
    sid = out.get("id")
    labs = await _resolve_persona_pair_labels(
        p1=out.get("player1_persona"),
        p2=out.get("player2_persona"),
        series_id=int(sid) if sid is not None else None,
    )
    out["player1_persona_label"] = labs["dash1"]
    out["player2_persona_label"] = labs["dash2"]
    out["player1_persona_slug_label"] = labs["slug1"]
    out["player2_persona_slug_label"] = labs["slug2"]
    out["player1_persona_display_slug"] = labs["ds1"] if out.get("player1_persona") else ""
    out["player2_persona_display_slug"] = labs["ds2"] if out.get("player2_persona") else ""
    matches_out = []
    for m in out.get("matches") or []:
        md = dict(m)
        md["winner_label"] = persona_battle_label(md.get("winner"))
        matches_out.append(md)
    out["matches"] = matches_out
    return out


templates.env.filters["persona_slug_label"] = persona_slug_label
templates.env.filters["persona_dashboard_label"] = persona_dashboard_label
templates.env.filters["persona_slug_only"] = persona_slug_only
templates.env.filters["persona_battle_label"] = persona_battle_label


# ===================================================================
# API routes — /api/manager/...
# ===================================================================

@router.get("/api/manager/config", response_class=JSONResponse)
async def api_config():
    return {
        "providers": ALLOWED_PROVIDERS,
        "battle_formats": BATTLE_FORMATS,
        "personas": _scan_personas(),
    }


# --- Tournaments ---

@router.post("/api/manager/tournaments", response_class=JSONResponse)
async def api_create_tournament(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    t_type = body.get("type", "")
    fmt = body.get("battle_format", "")
    best_of = int(body.get("best_of", 1))
    entries = body.get("entries", [])

    if not name:
        raise HTTPException(400, "Tournament name is required")
    if t_type not in ("round_robin", "single_elimination", "double_elimination"):
        raise HTTPException(400, f"Invalid tournament type: {t_type}")
    single_elim_bracket: str | None = None
    if t_type in ("single_elimination", "double_elimination"):
        seb = (body.get("single_elim_bracket") or "compact").strip().lower()
        if seb not in ("compact", "power_of_two"):
            raise HTTPException(
                400,
                "single_elim_bracket must be 'compact' or 'power_of_two'",
            )
        single_elim_bracket = seb
    if not fmt:
        raise HTTPException(400, "Battle format is required")
    if len(entries) < 2:
        raise HTTPException(400, "At least 2 participants are required")
    if best_of < 1 or best_of % 2 == 0:
        raise HTTPException(400, "best_of must be a positive odd number")

    for i, e in enumerate(entries):
        if not e.get("provider") or not e.get("model") or not e.get("persona_slug"):
            raise HTTPException(400, f"Entry {i+1} missing provider, model, or persona_slug")
        _require_provider_model_pair(f"Entry {i + 1}", e["provider"], e["model"])

    tournament = await db.create_tournament(
        name=name,
        type=t_type,
        battle_format=fmt,
        best_of=best_of,
        entries=entries,
        single_elim_bracket=single_elim_bracket,
    )
    await tournament_logic.generate_bracket(tournament)
    return await db.get_tournament(tournament["id"])


@router.get("/api/manager/tournaments", response_class=JSONResponse)
async def api_list_tournaments(status: str | None = None):
    return await db.list_tournaments(status=status)


@router.get("/api/manager/tournaments/{tid}", response_class=JSONResponse)
async def api_get_tournament(tid: int):
    t = await db.get_tournament(tid)
    if not t:
        raise HTTPException(404, "Tournament not found")
    return t


@router.post("/api/manager/tournaments/{tid}/cancel", response_class=JSONResponse)
async def api_cancel_tournament(tid: int):
    t = await db.get_tournament(tid)
    if not t:
        raise HTTPException(404, "Tournament not found")
    await db.cancel_tournament(tid)
    return {"status": "cancelled"}


# --- Series ---

@router.post("/api/manager/series", response_class=JSONResponse)
async def api_create_series(request: Request):
    body = await request.json()
    best_of = int(body.get("best_of", 1))
    fmt = body.get("battle_format", "")
    if not fmt:
        raise HTTPException(400, "Battle format is required")
    if best_of < 1 or best_of % 2 == 0:
        raise HTTPException(400, "best_of must be a positive odd number")

    for side in ("player1", "player2"):
        if not body.get(f"{side}_provider") or not body.get(f"{side}_model") or not body.get(f"{side}_persona"):
            raise HTTPException(400, f"{side} provider, model, and persona are required")

    _require_provider_model_pair("Player 1", body["player1_provider"], body["player1_model"])
    _require_provider_model_pair("Player 2", body["player2_provider"], body["player2_model"])

    series = await db.create_series(
        best_of=best_of,
        battle_format=fmt,
        player1_provider=body["player1_provider"],
        player1_model=body["player1_model"],
        player1_persona=body["player1_persona"],
        player2_provider=body["player2_provider"],
        player2_model=body["player2_model"],
        player2_persona=body["player2_persona"],
    )
    return series


@router.get("/api/manager/series/{sid}", response_class=JSONResponse)
async def api_get_series(sid: int):
    s = await db.get_series(sid)
    if not s:
        raise HTTPException(404, "Series not found")
    return await _enrich_series_api_payload(s)


# --- Matches ---

@router.post("/api/manager/matches", response_class=JSONResponse)
async def api_create_matches(request: Request):
    """Create standalone match(es) or a best-of-N series."""
    body = await request.json()
    fmt = body.get("battle_format", "")
    count = int(body.get("count", 1))
    best_of = int(body.get("best_of", 0))

    if not fmt:
        raise HTTPException(400, "Battle format is required")
    for side in ("player1", "player2"):
        if not body.get(f"{side}_provider") or not body.get(f"{side}_model") or not body.get(f"{side}_persona"):
            raise HTTPException(400, f"{side} provider, model, and persona are required")

    _require_provider_model_pair("Player 1", body["player1_provider"], body["player1_model"])
    _require_provider_model_pair("Player 2", body["player2_provider"], body["player2_model"])

    # best_of > 0 means create a series; count is ignored
    if best_of > 0:
        if best_of % 2 == 0:
            raise HTTPException(400, "best_of must be odd")
        series = await db.create_series(
            best_of=best_of,
            battle_format=fmt,
            player1_provider=body["player1_provider"],
            player1_model=body["player1_model"],
            player1_persona=body["player1_persona"],
            player2_provider=body["player2_provider"],
            player2_model=body["player2_model"],
            player2_persona=body["player2_persona"],
        )
        return {"type": "series", "series": series}

    # Otherwise create individual matches
    created = []
    for i in range(max(1, count)):
        m = await db.create_match(
            battle_format=fmt,
            game_number=i + 1,
            player1_provider=body["player1_provider"],
            player1_model=body["player1_model"],
            player1_persona=body["player1_persona"],
            player2_provider=body["player2_provider"],
            player2_model=body["player2_model"],
            player2_persona=body["player2_persona"],
        )
        created.append(m)
    return {"type": "matches", "matches": created}


@router.get("/api/manager/matches", response_class=JSONResponse)
async def api_list_matches(
    status: str | None = None,
    series_id: int | None = None,
    tournament_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
):
    return await db.list_matches(
        status=status, series_id=series_id, tournament_id=tournament_id,
        limit=limit, offset=offset,
    )


# --- Queue (registered before /matches/{mid} for predictable routing) ---

@router.get("/api/manager/queue/next", response_class=JSONResponse)
async def api_queue_next():
    try:
        m = await db.pop_next_queued_match()
    except Exception:
        _log.exception("GET /api/manager/queue/next failed (SQLite schema or DB error?)")
        raise HTTPException(
            500,
            detail="queue_next_failed — check web logs; if the DB predates migrations, delete manager.db (+ wal/shm) on manager-data and restart web",
        ) from None
    if not m:
        raise HTTPException(404, "No queued matches")
    m = await db.enrich_match_row_with_series_tournament(m)
    a1, a2 = await showdown_accounts.showdown_accounts_for_match(m)
    if a1 and a2:
        m["player1_showdown_account"] = a1
        m["player2_showdown_account"] = a2
    return m


@router.get("/api/manager/queue/depth", response_class=JSONResponse)
async def api_queue_depth():
    try:
        depth = await db.get_queue_depth()
    except Exception:
        _log.exception("GET /api/manager/queue/depth failed")
        raise HTTPException(500, detail="queue_depth_failed") from None
    return {"depth": depth}


async def _enrich_queued_match_for_ui(m: dict) -> dict:
    d = dict(m)
    sid = d.get("series_id")
    labs = await _resolve_persona_pair_labels(
        p1=d.get("player1_persona"),
        p2=d.get("player2_persona"),
        series_id=int(sid) if sid is not None else None,
    )
    d["player1_persona_label"] = _queue_player_label_after_persona(
        labs["dash1"], d.get("player1_provider"), d.get("player1_model")
    )
    d["player2_persona_label"] = _queue_player_label_after_persona(
        labs["dash2"], d.get("player2_provider"), d.get("player2_model")
    )
    d["player1_stream_label"] = _queue_stream_or_fallback(
        labs["stream1"], labs["dash1"], d.get("player1_provider"), d.get("player1_model")
    )
    d["player2_stream_label"] = _queue_stream_or_fallback(
        labs["stream2"], labs["dash2"], d.get("player2_provider"), d.get("player2_model")
    )
    _apply_tournament_opponent_tbd_labels(d)
    return d


async def _enrich_pending_series_row_for_ui(s: dict) -> dict:
    """Shape a partial tournament series like a queue row for ticker / dashboard."""
    sid = s.get("id")
    labs = await _resolve_persona_pair_labels(
        p1=s.get("player1_persona"),
        p2=s.get("player2_persona"),
        series_id=int(sid) if sid is not None else None,
    )
    d: dict = {
        "pending_slot": True,
        "series_id": sid,
        "tournament_id": s.get("tournament_id"),
        "tournament_name": s.get("tournament_name"),
        "series_bracket": s.get("bracket"),
        "series_round_number": s.get("round_number"),
        "series_match_position": s.get("match_position"),
        "tournament_max_winners_round": s.get("tournament_max_winners_round"),
        "battle_format": s.get("battle_format"),
        "game_number": None,
        "status": "pending_opponent",
        "player1_persona_label": _queue_player_label_after_persona(
            labs["dash1"], s.get("player1_provider"), s.get("player1_model")
        ),
        "player2_persona_label": _queue_player_label_after_persona(
            labs["dash2"], s.get("player2_provider"), s.get("player2_model")
        ),
    }
    d["player1_stream_label"] = _queue_stream_or_fallback(
        labs["stream1"], labs["dash1"], s.get("player1_provider"), s.get("player1_model")
    )
    d["player2_stream_label"] = _queue_stream_or_fallback(
        labs["stream2"], labs["dash2"], s.get("player2_provider"), s.get("player2_model")
    )
    _apply_tournament_opponent_tbd_labels(d)
    return d


@router.get("/api/manager/queue/upcoming", response_class=JSONResponse)
async def api_queue_upcoming(limit: int = 50):
    cap = max(1, min(int(limit), 200))
    slot_cap = max(1, min(24, cap))
    try:
        raw = await db.list_queued_matches(limit=cap)
        pending_rows = await db.list_tournament_series_pending_opponent(limit=slot_cap)
    except Exception:
        _log.exception("GET /api/manager/queue/upcoming failed")
        raise HTTPException(500, detail="queue_upcoming_failed") from None
    out: list[dict] = [await _enrich_queued_match_for_ui(m) for m in raw]
    for row in pending_rows:
        out.append(await _enrich_pending_series_row_for_ui(row))
    return out


@router.get("/api/manager/queue/running", response_class=JSONResponse)
async def api_queue_running():
    try:
        m = await db.get_running_match()
    except Exception:
        _log.exception("GET /api/manager/queue/running failed")
        raise HTTPException(500, detail="queue_running_failed") from None
    if not m:
        return JSONResponse(content=None)
    return JSONResponse(content=await _enrich_queued_match_for_ui(m))


@router.get("/api/manager/matches/{mid}", response_class=JSONResponse)
async def api_get_match(mid: int):
    m = await db.get_match(mid)
    if not m:
        raise HTTPException(404, "Match not found")
    return m


@router.post("/api/manager/matches/{mid}/start", response_class=JSONResponse)
async def api_match_start(mid: int):
    m = await db.start_match(mid)
    if not m:
        raise HTTPException(404, "Match not found")
    return m


@router.post("/api/manager/matches/{mid}/complete", response_class=JSONResponse)
async def api_match_complete(mid: int, request: Request):
    body = await request.json()
    winner = body.get("winner", "")
    loser = body.get("loser", "")
    winner_side = body.get("winner_side", "")
    if not winner or not winner_side:
        raise HTTPException(400, "winner and winner_side are required")

    m = await db.complete_match(
        mid,
        winner=winner,
        loser=loser,
        winner_side=winner_side,
        duration=body.get("duration"),
        replay_file=body.get("replay_file"),
        log_file=body.get("log_file"),
        battle_tag=body.get("battle_tag"),
    )
    if not m:
        raise HTTPException(404, "Match not found")

    await tournament_logic.on_match_completed(m)
    payload = dict(m)
    if m.get("series_id"):
        s = await db.get_series(m["series_id"])
        if s:
            payload["series_snapshot"] = {
                "series_id": s["id"],
                "best_of": s["best_of"],
                "player1_wins": s["player1_wins"],
                "player2_wins": s["player2_wins"],
            }
    return payload


@router.post("/api/manager/matches/{mid}/error", response_class=JSONResponse)
async def api_match_error(mid: int, request: Request):
    body = await request.json()
    m = await db.fail_match(mid, body.get("error", "Unknown error"))
    if not m:
        raise HTTPException(404, "Match not found")
    await tournament_logic.on_match_failed(m)
    hint = None
    if m.get("tournament_id") and m.get("series_id"):
        t = await db.get_tournament(m["tournament_id"])
        if t and t["type"] != "round_robin":
            hint = (
                "Series cancelled after error. If the elimination bracket looks stuck, "
                "cancel the tournament from the manager UI."
            )
    out = dict(m)
    if hint:
        out["recovery_hint"] = hint
    return out


# --- Results & Stats ---

@router.get("/api/manager/results", response_class=JSONResponse)
async def api_results(
    tournament_id: int | None = None,
    series_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
):
    return await db.list_matches(
        status="completed",
        tournament_id=tournament_id,
        series_id=series_id,
        limit=limit,
        offset=offset,
    )


@router.get("/api/manager/stats", response_class=JSONResponse)
async def api_stats():
    return await db.get_stats()


# ===================================================================
# HTML page routes — /manager/...
# ===================================================================

@router.get("/manager", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    queue_depth = await db.get_queue_depth()
    running = await db.get_running_match()
    if running:
        running = await _enrich_queued_match_for_ui(running)
    raw_q = await db.list_queued_matches(limit=50)
    queued = [await _enrich_queued_match_for_ui(m) for m in raw_q]
    recent = await db.list_matches(status="completed", limit=10)
    tournaments = await db.list_tournaments()
    return templates.TemplateResponse(
        request=request,
        name="manager/dashboard.html",
        context={
            "request": request,
            "queue_depth": queue_depth,
            "running_match": running,
            "queued_matches": queued,
            "recent_matches": recent,
            "tournaments": tournaments[:5],
            "showdown_view_base": SHOWDOWN_VIEW_BASE,
            "live_showdown_battle_url": _read_live_showdown_battle_url(),
        },
    )


@router.get("/manager/tournaments", response_class=HTMLResponse)
async def page_tournament_list(request: Request):
    tournaments = await db.list_tournaments()
    return templates.TemplateResponse(
        request=request,
        name="manager/tournament_list.html",
        context={"request": request, "tournaments": tournaments},
    )


@router.get("/manager/tournaments/new", response_class=HTMLResponse)
async def page_tournament_new(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="manager/tournament_new.html",
        context={
            "request": request,
            "providers": ALLOWED_PROVIDERS,
            "battle_formats": BATTLE_FORMATS,
            "personas": _scan_personas(),
        },
    )


@router.get("/manager/tournaments/{tid}", response_class=HTMLResponse)
async def page_tournament_detail(request: Request, tid: int):
    t = await db.get_tournament(tid)
    if not t:
        raise HTTPException(404, "Tournament not found")
    tournament_logic.annotate_series_tournament_champion_winner_side(t)
    return templates.TemplateResponse(
        request=request,
        name="manager/tournament_detail.html",
        context={"request": request, "tournament": t},
    )


@router.get("/manager/matches/new", response_class=HTMLResponse)
async def page_match_new(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="manager/match_new.html",
        context={
            "request": request,
            "providers": ALLOWED_PROVIDERS,
            "battle_formats": BATTLE_FORMATS,
            "personas": _scan_personas(),
        },
    )


@router.get("/manager/series/{sid}", response_class=HTMLResponse)
async def page_series_detail(request: Request, sid: int):
    s = await db.get_series(sid)
    if not s:
        raise HTTPException(404, "Series not found")
    s = await _enrich_series_api_payload(s)
    return templates.TemplateResponse(
        request=request,
        name="manager/series_detail.html",
        context={"request": request, "series": s},
    )


@router.get("/manager/results", response_class=HTMLResponse)
async def page_results(request: Request):
    matches = await db.list_matches(status="completed", limit=200)
    raw_series = await db.list_series_results(limit=100)
    series_results = [await _enrich_series_api_payload(s) for s in raw_series]
    return templates.TemplateResponse(
        request=request,
        name="manager/results.html",
        context={
            "request": request,
            "matches": matches,
            "series_results": series_results,
        },
    )


@router.get("/manager/results/stats", response_class=HTMLResponse)
async def page_stats(request: Request):
    stats = await db.get_stats()
    return templates.TemplateResponse(
        request=request,
        name="manager/stats.html",
        context={"request": request, "stats": stats},
    )


def _config_rows() -> tuple[dict[str, list], dict]:
    status = envfile.host_env_status()
    path = envfile.configured_host_env_path()
    file_map: dict[str, str] = {}
    if path and path.is_file():
        file_map = envfile.load_host_env_map(path)
    rows_by_cat: dict[str, list] = {c: [] for c in categories_in_order()}
    for entry in ENV_REGISTRY:
        if entry.key in file_map:
            raw = file_map[entry.key]
            source = "host .env"
        elif entry.key in os.environ:
            raw = os.environ[entry.key]
            source = "container"
        else:
            raw = ""
            source = "unset"
        if entry.sensitive and raw:
            masked_display = "••••••••"
        elif entry.sensitive:
            masked_display = ""
        else:
            masked_display = raw
        rows_by_cat[entry.category].append(
            {
                "entry": entry,
                "raw": raw,
                "masked_display": masked_display,
                "source": source,
            }
        )
    return rows_by_cat, status


@router.get("/manager/config", response_class=HTMLResponse)
async def page_config(request: Request):
    rows_by_cat, status = _config_rows()
    saved = request.query_params.get("saved") == "1"
    return templates.TemplateResponse(
        request=request,
        name="manager/config.html",
        context={
            "request": request,
            "active_page": "config",
            "rows_by_cat": rows_by_cat,
            "categories": categories_in_order(),
            "env_status": status,
            "saved": saved,
        },
    )


@router.post("/manager/config/update")
async def page_config_update(key: str = Form(...), value: str = Form("")):
    key = key.strip()
    if key not in REGISTRY_BY_KEY:
        raise HTTPException(400, detail="Unknown configuration key")
    defn = REGISTRY_BY_KEY[key]
    st = envfile.host_env_status()
    path = envfile.configured_host_env_path()
    if not st.get("configured") or not st.get("exists") or not st.get("writable") or path is None:
        raise HTTPException(
            400,
            detail="Host environment file is not mounted or not writable (check docker-compose and MANAGER_HOST_ENV_FILE)",
        )
    if defn.sensitive and value.strip() == "":
        return RedirectResponse(url="/manager/config", status_code=303)
    envfile.update_env_keys(path, {key: value})
    return RedirectResponse(url="/manager/config?saved=1", status_code=303)


# --- Personas (markdown + trainer sprites) ---


@router.get("/manager/personas", response_class=HTMLResponse)
async def page_personas_list(request: Request):
    try:
        items = pstore.list_personas()
    except OSError as e:
        _log.exception("list personas")
        raise HTTPException(500, detail=f"Cannot read personas: {e}") from e
    return templates.TemplateResponse(
        request=request,
        name="manager/personas_list.html",
        context={
            "request": request,
            "personas": items,
            "trainers_dir": str(pstore.TRAINERS_DIR),
            "personas_dir": str(pstore.PERSONAS_DIR),
        },
    )


@router.get("/manager/personas/new", response_class=HTMLResponse)
async def page_persona_new(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="manager/persona_new.html",
        context={
            "request": request,
            "trainer_files": pstore.list_trainer_filenames(),
        },
    )


@router.post("/manager/personas/new")
async def action_persona_create(
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    abbreviation: str = Form(""),
    description: str = Form(""),
    sprite: str = Form(""),
    sprite_url: str = Form(""),
    body: str = Form(...),
    sprite_file: UploadFile | None = File(None),
):
    try:
        s = pstore.validate_slug(slug)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    meta = {
        "name": name.strip(),
        "abbreviation": abbreviation.strip(),
        "description": description.strip(),
        "sprite": sprite.strip(),
        "sprite_url": sprite_url.strip(),
    }
    meta = {k: v for k, v in meta.items() if v}
    try:
        pstore.create_persona(s, meta, body)
    except FileExistsError:
        raise HTTPException(409, f"Persona '{s}' already exists") from None
    except OSError as e:
        _log.exception("create persona")
        raise HTTPException(500, detail=str(e)) from e
    await _maybe_save_persona_sprite_upload(s, sprite_file)
    return RedirectResponse(url="/manager/personas", status_code=303)


async def _maybe_save_persona_sprite_upload(slug: str, sprite_file: UploadFile | None) -> None:
    if sprite_file is None or not sprite_file.filename:
        return
    data = await sprite_file.read()
    if not data:
        return
    try:
        fn = pstore.save_trainer_upload(sprite_file.filename, data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    try:
        data_r = pstore.read_persona(slug)
        meta = data_r["meta"]
        meta["sprite"] = fn
        pstore.write_persona(slug, meta, data_r["body"])
    except Exception:
        _log.exception("attach sprite to persona after upload")


@router.get("/manager/personas/{slug}/edit", response_class=HTMLResponse)
async def page_persona_edit(request: Request, slug: str):
    try:
        data = pstore.read_persona(slug)
    except FileNotFoundError:
        raise HTTPException(404, "Persona not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return templates.TemplateResponse(
        request=request,
        name="manager/persona_edit.html",
        context={
            "request": request,
            "p": data,
            "trainer_files": pstore.list_trainer_filenames(),
        },
    )


@router.post("/manager/personas/{slug}/edit")
async def action_persona_save(
    request: Request,
    slug: str,
    name: str = Form(...),
    abbreviation: str = Form(""),
    description: str = Form(""),
    sprite: str = Form(""),
    sprite_url: str = Form(""),
    body: str = Form(...),
    sprite_file: UploadFile | None = File(None),
):
    try:
        s = pstore.validate_slug(slug)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not (pstore.PERSONAS_DIR / f"{s}.md").is_file():
        raise HTTPException(404, "Persona not found")
    meta = {
        "name": name.strip(),
        "abbreviation": abbreviation.strip(),
        "description": description.strip(),
        "sprite": sprite.strip(),
        "sprite_url": sprite_url.strip(),
    }
    meta = {k: v for k, v in meta.items() if v}
    try:
        pstore.write_persona(s, meta, body)
    except OSError as e:
        _log.exception("save persona")
        raise HTTPException(500, detail=str(e)) from e
    await _maybe_save_persona_sprite_upload(s, sprite_file)
    return RedirectResponse(url=f"/manager/personas/{s}/edit", status_code=303)


@router.post("/manager/personas/{slug}/delete")
async def action_persona_delete(
    request: Request,
    slug: str,
    delete_trainer_sprite: str | None = Form(None),
):
    try:
        s = pstore.validate_slug(slug)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    also_sprite = delete_trainer_sprite in ("1", "on", "true", "yes")
    try:
        pstore.delete_persona(s, delete_trainer_sprite=also_sprite)
    except OSError as e:
        _log.exception("delete persona")
        raise HTTPException(500, detail=str(e)) from e
    return RedirectResponse(url="/manager/personas", status_code=303)
