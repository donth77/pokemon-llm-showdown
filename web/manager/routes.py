"""
Manager routes — API + HTML pages for tournament / series / match management.

Mounted on the main FastAPI app as an APIRouter.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from env_bool import parse_env_bool
from manager_stream import manager_sse, notify_manager_events
from scoreboard_stream import request_scoreboard_publish

from . import (
    battle_format_rules,
    db,
    env_host_file as envfile,
    personas_store as pstore,
    showdown_accounts,
    team_showdown_validate,
    tournament_definition,
    tournament_logic,
)
from .env_registry import ENV_REGISTRY, REGISTRY_BY_KEY, categories_in_order
from .provider_model_validate import validate_provider_model

router = APIRouter()
templates = Jinja2Templates(directory="templates")
_log = logging.getLogger("uvicorn.error")

_HTML_LIST_DEFAULT = 10
_HTML_LIST_MAX = 200


def _series_id_list_from_match(m: dict | None) -> list[int]:
    if not m:
        return []
    sid = m.get("series_id")
    if sid is None:
        return []
    try:
        return [int(sid)]
    except (TypeError, ValueError):
        return []


def _clamp_page_offset(offset: int, limit: int, total: int) -> int:
    if total <= 0:
        return 0
    max_off = max(0, ((total - 1) // limit) * limit)
    return min(max(0, offset), max_off)


def _offset_pagination_bar(
    *,
    path: str,
    offset: int,
    limit: int,
    total: int,
    offset_param: str,
    limit_param: str,
    preserve: dict[str, int],
) -> dict[str, str | int | None]:
    def make_url(new_off: int) -> str:
        q = {k: str(v) for k, v in preserve.items()}
        q[offset_param] = str(max(0, new_off))
        q[limit_param] = str(limit)
        return f"{path}?{urlencode(q)}"

    prev_url = None
    if offset > 0:
        prev_url = make_url(max(0, offset - limit))
    next_url = None
    if offset + limit < total:
        next_url = make_url(offset + limit)
    end = min(offset + limit, total)
    start = offset + 1 if total else 0
    return {
        "offset": offset,
        "limit": limit,
        "total": total,
        "start": start,
        "end": end,
        "prev_url": prev_url,
        "next_url": next_url,
    }


async def _analytics_format_filter(
    query_formats: list[str],
) -> tuple[list[str] | None, list[str]]:
    """
    Map repeated ``formats`` query values to a ``get_stats`` filter.
    Empty selection or a selection that includes every known format → no filter (all).
    """
    known = await db.list_completed_battle_formats()
    if not known:
        return None, known
    known_set = set(known)
    sel = [f for f in query_formats if f in known_set]
    if not sel or set(sel) == known_set:
        return None, known
    return sel, known


def _stats_export_timestamp_stem() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _stats_to_csv(stats: dict, battle_formats: list[str] | None) -> str:
    out = StringIO()
    w = csv.writer(out)
    w.writerow(["# pokemon-llm-showdown analytics export"])
    w.writerow(["battle_formats_filter", json.dumps(battle_formats)])
    w.writerow([])

    w.writerow(["## model_stats"])
    w.writerow(["model", "wins", "losses", "total", "win_rate_pct"])
    for model, s in stats.get("model_stats", {}).items():
        w.writerow(
            [
                model,
                s.get("wins", 0),
                s.get("losses", 0),
                s.get("total", 0),
                s.get("win_rate", 0),
            ]
        )

    w.writerow([])
    w.writerow(["## persona_stats"])
    w.writerow(["persona", "wins", "losses", "total", "win_rate_pct"])
    for persona, s in stats.get("persona_stats", {}).items():
        w.writerow(
            [
                persona or "",
                s.get("wins", 0),
                s.get("losses", 0),
                s.get("total", 0),
                s.get("win_rate", 0),
            ]
        )

    w.writerow([])
    w.writerow(["## head_to_head_models"])
    w.writerow(["model_p1", "model_p2", "p1_wins", "p2_wins", "total"])
    for h in stats.get("head_to_head", []):
        w.writerow(
            [
                h.get("model1", ""),
                h.get("model2", ""),
                h.get("model1_wins", 0),
                h.get("model2_wins", 0),
                h.get("total", 0),
            ]
        )

    w.writerow([])
    w.writerow(["## head_to_head_personas"])
    w.writerow(["persona_p1", "persona_p2", "p1_wins", "p2_wins", "total"])
    for h in stats.get("head_to_head_personas", []):
        w.writerow(
            [
                h.get("persona1") or "",
                h.get("persona2") or "",
                h.get("persona1_wins", 0),
                h.get("persona2_wins", 0),
                h.get("total", 0),
            ]
        )

    w.writerow([])
    w.writerow(["## head_to_head_model_persona"])
    w.writerow(
        [
            "model_p1",
            "persona_p1",
            "model_p2",
            "persona_p2",
            "p1_wins",
            "p2_wins",
            "total",
        ]
    )
    for h in stats.get("head_to_head_model_persona", []):
        w.writerow(
            [
                h.get("model1", ""),
                h.get("persona1") or "",
                h.get("model2", ""),
                h.get("persona2") or "",
                h.get("model1_wins", 0),
                h.get("model2_wins", 0),
                h.get("total", 0),
            ]
        )

    return out.getvalue()


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


def _read_persona_adaptive_file(slug: str, relative_name: str) -> str | None:
    """Return file text if present under STATE_DIR/personas/{slug}/; None if missing or unreadable."""
    path = STATE_DIR / "personas" / slug / relative_name
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# Browser URL for raw Showdown client (host/port or e.g. https://localhost.psim.us). Default: mapped showdown port.
SHOWDOWN_VIEW_BASE = os.getenv("SHOWDOWN_VIEW_BASE", "http://localhost:8000").rstrip(
    "/"
)
_SHOWDOWN_USERNAME_MAX = 18

ALLOWED_PROVIDERS = ["anthropic", "deepseek", "openrouter"]


def _require_provider_model_pair(label: str, provider: str, model: str) -> None:
    try:
        validate_provider_model(provider, model, field_label=label)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


# Dropdown + tournament plaintext import allowlist. Random = server-built teams;
# others are custom-team singles (use paste / future team presets). Older gens:
# only OU here; UU/RU/1v1 for gen8–1 can be typed in the custom format field.
BATTLE_FORMATS = [
    # --- Random (server teams) ---
    "gen9randombattle",
    "gen8randombattle",
    "gen7randombattle",
    "gen6randombattle",
    "gen5randombattle",
    "gen4randombattle",
    "gen3randombattle",
    "gen2randombattle",
    "gen1randombattle",
    # --- Built teams: Gen 9 singles ---
    "gen9ou",
    "gen9uu",
    "gen9ru",
    "gen91v1",
    # --- Built teams: OU by generation (gen8 → gen1) ---
    "gen8ou",
    "gen7ou",
    "gen6ou",
    "gen5ou",
    "gen4ou",
    "gen3ou",
    "gen2ou",
    "gen1ou",
]

# Team preset "format hint" dropdown: BYO formats only (no server-assigned teams).
BATTLE_FORMATS_TEAM_PRESET_HINTS = [
    f for f in BATTLE_FORMATS if not battle_format_rules.uses_server_assigned_teams(f)
]


def _team_battle_format_in_preset_hints(battle_format: str) -> bool:
    b = battle_format_rules.normalize_battle_format_id(battle_format)
    if not b:
        return False
    for h in BATTLE_FORMATS_TEAM_PRESET_HINTS:
        if battle_format_rules.normalize_battle_format_id(h) == b:
            return True
    return False


def _require_valid_tournament_definition_text(text: str) -> None:
    raw = (text or "").strip()
    if not raw:
        raise HTTPException(400, "Definition body is required")
    personas = _scan_personas()
    slugs = {p["slug"] for p in personas}
    _, errors, _ = tournament_definition.parse_tournament_definition(
        raw,
        valid_battle_formats=frozenset(BATTLE_FORMATS),
        valid_persona_slugs=slugs,
    )
    if errors:
        raise HTTPException(
            400,
            {"message": "Invalid tournament definition", "errors": errors},
        )


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


def tournament_entry_persona_label(entry: dict) -> str:
    """Roster cell: 'DamageDan (aggro)' or 'DamageDan1 (aggro1)' when the slug repeats in the bracket."""
    slug = (entry.get("persona_slug") or "").strip()
    ds = (entry.get("persona_display_slug") or slug).strip()
    if not slug:
        return ds or "—"
    base = _battle_name_for_persona_slug(slug)
    battle = _numbered_battle_name_for_tournament_slot(base, slug, ds)
    return f"{battle} ({ds})"


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
    out["player1_persona_display_slug"] = (
        labs["ds1"] if out.get("player1_persona") else ""
    )
    out["player2_persona_display_slug"] = (
        labs["ds2"] if out.get("player2_persona") else ""
    )
    matches_out = []
    for m in out.get("matches") or []:
        md = dict(m)
        md["winner_label"] = persona_battle_label(md.get("winner"))
        matches_out.append(md)
    out["matches"] = matches_out
    return out


def _portrait_square_url_filter(slug: str | None) -> str:
    raw = (slug or "").strip()
    if not raw:
        return ""
    try:
        u = pstore.resolve_portrait_url(raw, square=True)
    except ValueError:
        return ""
    return u or ""


templates.env.filters["persona_slug_label"] = persona_slug_label
templates.env.filters["persona_dashboard_label"] = persona_dashboard_label
templates.env.filters["tournament_entry_persona_label"] = tournament_entry_persona_label
templates.env.filters["persona_slug_only"] = persona_slug_only
templates.env.filters["persona_battle_label"] = persona_battle_label
templates.env.filters["portrait_square_url"] = _portrait_square_url_filter


# ===================================================================
# API routes — /api/manager/...
# ===================================================================


@router.get("/api/manager/config", response_class=JSONResponse)
async def api_config():
    return {
        "providers": ALLOWED_PROVIDERS,
        "battle_formats": BATTLE_FORMATS,
        "personas": _scan_personas(),
        "random_team_battle_format_suffix": battle_format_rules.SHOWDOWN_SERVER_ASSIGNED_TEAM_SUFFIX,
    }


def _optional_team_id(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def _require_team_preset_matches_battle_format(
    label: str,
    battle_format: str,
    team_id: int | None,
) -> None:
    """When team_id is set, require row to exist; if it has a format hint, it must match."""
    if team_id is None:
        return
    row = await db.get_team(team_id)
    if not row:
        raise HTTPException(404, f"Team {team_id} not found")
    tag = battle_format_rules.normalize_battle_format_id(
        str(row.get("battle_format") or "")
    )
    want = battle_format_rules.normalize_battle_format_id(battle_format)
    if not tag:
        return
    if tag != want:
        raise HTTPException(
            400,
            f"{label}: team preset {team_id} ({row['name']!r}) is tagged for "
            f"{row['battle_format']!r}, not {battle_format!r}.",
        )


def _require_player_teams_for_custom_battle_format(
    fmt: str,
    p1t: int | None,
    p2t: int | None,
) -> None:
    """BYO formats need both snapshot teams; random* formats ignore presets."""
    if battle_format_rules.uses_server_assigned_teams(fmt):
        return
    if p1t is None or p2t is None:
        raise HTTPException(
            400,
            "player1_team_id and player2_team_id are required for custom-team battle formats "
            f"(not *{battle_format_rules.SHOWDOWN_SERVER_ASSIGNED_TEAM_SUFFIX}).",
        )


# --- Team presets ---


@router.get("/api/manager/teams", response_class=JSONResponse)
async def api_list_teams():
    return await db.list_teams()


@router.post("/api/manager/teams/validate-showdown", response_class=JSONResponse)
async def api_validate_team_showdown(request: Request):
    if parse_env_bool("TEAM_VALIDATION_DISABLED", default=False):
        return {"ok": True, "skipped": True, "errors": []}
    body = await request.json()
    battle_format = str(body.get("battle_format", "")).strip()
    showdown_text = body.get("showdown_text")
    if showdown_text is not None and not isinstance(showdown_text, str):
        raise HTTPException(400, "showdown_text must be a string")
    text = showdown_text if isinstance(showdown_text, str) else ""
    try:
        errors = await team_showdown_validate.validate_team_showdown(
            battle_format, text
        )
    except team_showdown_validate.TeamValidationConfigError as exc:
        raise HTTPException(503, str(exc)) from None
    except TimeoutError as exc:
        raise HTTPException(504, str(exc)) from None
    return {"ok": len(errors) == 0, "skipped": False, "errors": errors}


@router.post("/api/manager/teams", response_class=JSONResponse)
async def api_create_team(request: Request):
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "name is required")
    battle_format = str(body.get("battle_format", "")).strip()
    if not battle_format:
        raise HTTPException(400, "battle_format is required")
    notes = str(body.get("notes", "") or "")
    showdown_text = (body.get("showdown_text") or "").strip()
    if not showdown_text:
        raise HTTPException(400, "showdown_text is required")
    try:
        row = await db.create_team(
            name=name,
            battle_format=battle_format,
            showdown_text=showdown_text,
            notes=notes,
        )
    except ValueError as exc:
        if exc.args and exc.args[0] == "duplicate_team_name":
            raise HTTPException(409, "A team with this name already exists") from None
        raise
    await notify_manager_events()
    # Redact full text in API response for list hygiene; include id + metadata
    return {
        "id": row["id"],
        "name": row["name"],
        "battle_format": row["battle_format"],
        "notes": row["notes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "showdown_text_preview": (showdown_text[:200] + "…")
        if len(showdown_text) > 200
        else showdown_text,
    }


@router.get("/api/manager/teams/{tid}", response_class=JSONResponse)
async def api_get_team(tid: int):
    row = await db.get_team(tid)
    if not row:
        raise HTTPException(404, "Team not found")
    return dict(row)


@router.patch("/api/manager/teams/{tid}", response_class=JSONResponse)
async def api_patch_team(tid: int, request: Request):
    body = await request.json()
    name = body.get("name") if "name" in body else None
    battle_format = body.get("battle_format") if "battle_format" in body else None
    notes = body.get("notes") if "notes" in body else None
    showdown_text = body.get("showdown_text") if "showdown_text" in body else None
    if name is not None and not str(name).strip():
        raise HTTPException(400, "name cannot be empty")
    cur_team = await db.get_team(tid)
    if not cur_team:
        raise HTTPException(404, "Team not found")
    merged_bf = (
        str(battle_format).strip()
        if battle_format is not None
        else str(cur_team.get("battle_format") or "").strip()
    )
    if not merged_bf:
        raise HTTPException(400, "battle_format is required")
    try:
        row = await db.update_team(
            tid,
            name=str(name).strip() if name is not None else None,
            battle_format=str(battle_format).strip()
            if battle_format is not None
            else None,
            showdown_text=str(showdown_text).strip()
            if showdown_text is not None
            else None,
            notes=str(notes) if notes is not None else None,
        )
    except ValueError as exc:
        if exc.args and exc.args[0] == "duplicate_team_name":
            raise HTTPException(409, "A team with this name already exists") from None
        raise
    if not row:
        raise HTTPException(404, "Team not found")
    await notify_manager_events()
    return dict(row)


@router.delete("/api/manager/teams/{tid}", response_class=JSONResponse)
async def api_delete_team(tid: int):
    try:
        ok = await db.delete_team(tid)
    except ValueError as exc:
        if exc.args and exc.args[0] == "team_in_active_matches":
            raise HTTPException(
                409,
                "Cannot delete team while it is referenced by a queued or running match",
            ) from None
        raise
    if not ok:
        raise HTTPException(404, "Team not found")
    await notify_manager_events()
    return {"status": "deleted"}


@router.patch(
    "/api/manager/tournament-entries/{entry_id}",
    response_class=JSONResponse,
)
async def api_patch_tournament_entry(entry_id: int, request: Request):
    body = await request.json()
    if "team_id" not in body:
        raise HTTPException(400, "team_id is required")
    tid = _optional_team_id(body.get("team_id"))
    entry_row = await db.get_tournament_entry(entry_id)
    if not entry_row:
        raise HTTPException(404, "Tournament entry not found")
    tournament_row = await db.get_tournament(int(entry_row["tournament_id"]))
    if not tournament_row:
        raise HTTPException(404, "Tournament not found")
    t_fmt = str(tournament_row.get("battle_format") or "")
    if tid is not None:
        if battle_format_rules.uses_server_assigned_teams(t_fmt):
            raise HTTPException(
                400,
                f"team_id is not allowed for server-assigned team formats "
                f"(*{battle_format_rules.SHOWDOWN_SERVER_ASSIGNED_TEAM_SUFFIX}).",
            )
        await _require_team_preset_matches_battle_format(
            f"Tournament entry {entry_id}", t_fmt, tid
        )
    ok = await db.update_tournament_entry_team(entry_id, tid)
    if not ok:
        raise HTTPException(404, "Tournament entry not found")
    await notify_manager_events(queue=True)
    return {"status": "ok", "team_id": tid}


# --- Tournaments ---


@router.post("/api/manager/tournaments/parse-definition", response_class=JSONResponse)
async def api_parse_tournament_definition(request: Request):
    """Parse plaintext tournament definition; returns payload compatible with POST /tournaments."""
    body = await request.json()
    text = body.get("text", "")
    if not isinstance(text, str):
        raise HTTPException(400, "text must be a string")
    personas = _scan_personas()
    slugs = {p["slug"] for p in personas}
    data, errors, warnings = tournament_definition.parse_tournament_definition(
        text,
        valid_battle_formats=frozenset(BATTLE_FORMATS),
        valid_persona_slugs=slugs,
    )
    return JSONResponse(
        {"ok": not errors, "data": data, "errors": errors, "warnings": warnings}
    )


@router.get("/api/manager/tournament-presets", response_class=JSONResponse)
async def api_list_tournament_presets():
    rows = await db.list_tournament_presets()
    return [
        {"id": r["id"], "name": r["name"], "updated_at": r["updated_at"]} for r in rows
    ]


@router.post("/api/manager/tournament-presets", response_class=JSONResponse)
async def api_create_tournament_preset(request: Request):
    body = await request.json()
    name = str(body.get("name", "")).strip()
    raw_body = body.get("body", "")
    if not name:
        raise HTTPException(400, "Preset name is required")
    if not isinstance(raw_body, str):
        raise HTTPException(400, "body must be a string")
    _require_valid_tournament_definition_text(raw_body)
    try:
        row = await db.create_tournament_preset(name=name, body=raw_body.strip())
    except ValueError as exc:
        if exc.args and exc.args[0] == db.PRESET_NAME_DUP:
            raise HTTPException(409, "A preset with this name already exists") from None
        raise
    return row


@router.get("/api/manager/tournament-presets/{pid}", response_class=JSONResponse)
async def api_get_tournament_preset(pid: int):
    row = await db.get_tournament_preset(pid)
    if not row:
        raise HTTPException(404, "Preset not found")
    return row


@router.patch("/api/manager/tournament-presets/{pid}", response_class=JSONResponse)
async def api_update_tournament_preset(pid: int, request: Request):
    body = await request.json()
    name = body.get("name")
    if name is not None:
        name = str(name).strip()
        if not name:
            raise HTTPException(400, "Preset name cannot be empty")
    raw_body = body.get("body")
    if raw_body is not None and not isinstance(raw_body, str):
        raise HTTPException(400, "body must be a string")
    if name is None and raw_body is None:
        raise HTTPException(400, "Provide name and/or body to update")
    if raw_body is not None:
        _require_valid_tournament_definition_text(raw_body)
        raw_body = raw_body.strip()
    try:
        row = await db.update_tournament_preset(pid, name=name, body=raw_body)
    except ValueError as exc:
        if exc.args and exc.args[0] == db.PRESET_NAME_DUP:
            raise HTTPException(409, "A preset with this name already exists") from None
        raise
    if not row:
        raise HTTPException(404, "Preset not found")
    return row


@router.delete("/api/manager/tournament-presets/{pid}", response_class=JSONResponse)
async def api_delete_tournament_preset(pid: int):
    ok = await db.delete_tournament_preset(pid)
    if not ok:
        raise HTTPException(404, "Preset not found")
    return {"status": "deleted"}


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

    normalized_entries: list[dict] = []
    for i, e in enumerate(entries):
        if not e.get("provider") or not e.get("model") or not e.get("persona_slug"):
            raise HTTPException(
                400, f"Entry {i + 1} missing provider, model, or persona_slug"
            )
        _require_provider_model_pair(f"Entry {i + 1}", e["provider"], e["model"])
        etid = _optional_team_id(e.get("team_id"))
        if battle_format_rules.uses_server_assigned_teams(fmt):
            etid = None
        elif etid is None:
            raise HTTPException(
                400,
                f"Entry {i + 1}: team_id is required for custom-team battle formats "
                f"(not *{battle_format_rules.SHOWDOWN_SERVER_ASSIGNED_TEAM_SUFFIX}).",
            )
        else:
            await _require_team_preset_matches_battle_format(
                f"Entry {i + 1}", fmt, etid
            )
        ne = dict(e)
        ne["team_id"] = etid
        normalized_entries.append(ne)

    tournament = await db.create_tournament(
        name=name,
        type=t_type,
        battle_format=fmt,
        best_of=best_of,
        entries=normalized_entries,
        single_elim_bracket=single_elim_bracket,
    )
    await tournament_logic.generate_bracket(tournament)
    await notify_manager_events(queue=True, tournament_ids=[int(tournament["id"])])
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
    await notify_manager_events(queue=True, tournament_ids=[tid])
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
        if (
            not body.get(f"{side}_provider")
            or not body.get(f"{side}_model")
            or not body.get(f"{side}_persona")
        ):
            raise HTTPException(
                400, f"{side} provider, model, and persona are required"
            )

    _require_provider_model_pair(
        "Player 1", body["player1_provider"], body["player1_model"]
    )
    _require_provider_model_pair(
        "Player 2", body["player2_provider"], body["player2_model"]
    )

    p1t = _optional_team_id(body.get("player1_team_id"))
    p2t = _optional_team_id(body.get("player2_team_id"))
    for side, tid in (("player1", p1t), ("player2", p2t)):
        if tid is not None and battle_format_rules.uses_server_assigned_teams(fmt):
            raise HTTPException(
                400,
                f"{side}_team_id is not allowed for server-assigned team formats",
            )
    _require_player_teams_for_custom_battle_format(fmt, p1t, p2t)
    await _require_team_preset_matches_battle_format("Player 1", fmt, p1t)
    await _require_team_preset_matches_battle_format("Player 2", fmt, p2t)

    series = await db.create_series(
        best_of=best_of,
        battle_format=fmt,
        player1_provider=body["player1_provider"],
        player1_model=body["player1_model"],
        player1_persona=body["player1_persona"],
        player2_provider=body["player2_provider"],
        player2_model=body["player2_model"],
        player2_persona=body["player2_persona"],
        player1_team_id=p1t,
        player2_team_id=p2t,
    )
    t_extra = (
        [int(series["tournament_id"])]
        if series.get("tournament_id") is not None
        else []
    )
    await notify_manager_events(
        queue=True,
        series_ids=[int(series["id"])],
        tournament_ids=t_extra,
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

    # --- Human vs AI support ---
    p1_type = (body.get("player1_type") or "llm").strip().lower()
    p2_type = (body.get("player2_type") or "llm").strip().lower()
    if p1_type not in ("llm", "human") or p2_type not in ("llm", "human"):
        raise HTTPException(400, "player_type must be 'llm' or 'human'")
    if p1_type == "human" and p2_type == "human":
        raise HTTPException(400, "Both players cannot be human")
    human_display_name = (body.get("human_display_name") or "").strip() or None

    # For human sides, set sentinel provider/model/persona so downstream
    # code that expects non-empty strings keeps working.
    if p1_type == "human":
        body.setdefault("player1_provider", "human")
        body.setdefault("player1_model", "human")
        body.setdefault("player1_persona", "human")
    if p2_type == "human":
        body.setdefault("player2_provider", "human")
        body.setdefault("player2_model", "human")
        body.setdefault("player2_persona", "human")

    for side in ("player1", "player2"):
        if (
            not body.get(f"{side}_provider")
            or not body.get(f"{side}_model")
            or not body.get(f"{side}_persona")
        ):
            raise HTTPException(
                400, f"{side} provider, model, and persona are required"
            )

    # Only validate provider/model pairs for LLM sides.
    if p1_type == "llm":
        _require_provider_model_pair(
            "Player 1", body["player1_provider"], body["player1_model"]
        )
    if p2_type == "llm":
        _require_provider_model_pair(
            "Player 2", body["player2_provider"], body["player2_model"]
        )

    p1t = _optional_team_id(body.get("player1_team_id"))
    p2t = _optional_team_id(body.get("player2_team_id"))
    for side, tid in (("player1", p1t), ("player2", p2t)):
        if tid is not None and battle_format_rules.uses_server_assigned_teams(fmt):
            raise HTTPException(
                400,
                f"{side}_team_id is not allowed for server-assigned team formats "
                f"(*{battle_format_rules.SHOWDOWN_SERVER_ASSIGNED_TEAM_SUFFIX}).",
            )
    # For human vs AI in custom formats, only the AI side needs a team preset.
    # The human builds/pastes their team in the Showdown client.
    has_human = p1_type == "human" or p2_type == "human"
    if has_human:
        ai_team = p2t if p1_type == "human" else p1t
        ai_label = "Player 2" if p1_type == "human" else "Player 1"
        if not battle_format_rules.uses_server_assigned_teams(fmt) and ai_team is None:
            raise HTTPException(
                400,
                f"{ai_label} (AI) team_id is required for custom-team battle formats.",
            )
        await _require_team_preset_matches_battle_format(ai_label, fmt, ai_team)
    else:
        _require_player_teams_for_custom_battle_format(fmt, p1t, p2t)
        await _require_team_preset_matches_battle_format("Player 1", fmt, p1t)
        await _require_team_preset_matches_battle_format("Player 2", fmt, p2t)

    # Only one play mode is supported today: the custom battle control page.
    # Column stored as-is for forward compat.
    human_kw = dict(
        player1_type=p1_type,
        player2_type=p2_type,
        human_display_name=human_display_name,
        human_play_mode="control_page",
    )

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
            player1_team_id=p1t,
            player2_team_id=p2t,
        )
        await notify_manager_events(queue=True, series_ids=[int(series["id"])])
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
            player1_team_id=p1t,
            player2_team_id=p2t,
            **human_kw,
        )
        created.append(m)
    await notify_manager_events(queue=True)
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
        status=status,
        series_id=series_id,
        tournament_id=tournament_id,
        limit=limit,
        offset=offset,
    )


# --- Queue (registered before /matches/{mid} for predictable routing) ---


@router.get("/api/manager/stream")
async def api_manager_stream(request: Request):
    return await manager_sse(request)


@router.get("/api/manager/queue/next", response_class=JSONResponse)
async def api_queue_next():
    try:
        m = await db.pop_next_queued_match()
    except Exception:
        _log.exception(
            "GET /api/manager/queue/next failed (SQLite schema or DB error?)"
        )
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
    # For human matches, use the display name as the Showdown account.
    p1t = m.get("player1_type") or "llm"
    p2t = m.get("player2_type") or "llm"
    if p1t == "human":
        m["player1_showdown_account"] = m.get("human_display_name") or "Challenger"
    elif p2t == "human":
        m["player2_showdown_account"] = m.get("human_display_name") or "Challenger"
    tids = [int(m["tournament_id"])] if m.get("tournament_id") is not None else []
    await notify_manager_events(
        queue=True, tournament_ids=tids, series_ids=_series_id_list_from_match(m)
    )
    return m


@router.get("/api/manager/queue/depth", response_class=JSONResponse)
async def api_queue_depth():
    try:
        depth = await db.get_queue_depth()
    except Exception:
        _log.exception("GET /api/manager/queue/depth failed")
        raise HTTPException(500, detail="queue_depth_failed") from None
    return {"depth": depth}


def _queue_row_game_if_necessary(d: dict) -> bool:
    """True when this queued game may be cancelled if the series clinches earlier (e.g. Bo5 game 4+)."""
    gn_raw, bo_raw = d.get("game_number"), d.get("series_best_of")
    if gn_raw is None or bo_raw is None:
        return False
    try:
        gn_i = int(gn_raw)
        bo_i = int(bo_raw)
    except (TypeError, ValueError):
        return False
    if bo_i < 2:
        return False
    wins_needed = (bo_i + 1) // 2
    return gn_i > wins_needed


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
        labs["stream1"],
        labs["dash1"],
        d.get("player1_provider"),
        d.get("player1_model"),
    )
    d["player2_stream_label"] = _queue_stream_or_fallback(
        labs["stream2"],
        labs["dash2"],
        d.get("player2_provider"),
        d.get("player2_model"),
    )
    d["queue_game_if_necessary"] = _queue_row_game_if_necessary(d)
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
        labs["stream1"],
        labs["dash1"],
        s.get("player1_provider"),
        s.get("player1_model"),
    )
    d["player2_stream_label"] = _queue_stream_or_fallback(
        labs["stream2"],
        labs["dash2"],
        s.get("player2_provider"),
        s.get("player2_model"),
    )
    d["queue_game_if_necessary"] = False
    _apply_tournament_opponent_tbd_labels(d)
    return d


async def _compose_queue_upcoming_rows(*, limit: int, offset: int) -> list[dict]:
    """Queued matches for a window, plus pending opponent placeholders only on the first window."""
    cap = max(1, min(int(limit), _HTML_LIST_MAX))
    off = max(0, int(offset))
    raw = await db.list_queued_matches(limit=cap, offset=off)
    out: list[dict] = [await _enrich_queued_match_for_ui(m) for m in raw]
    if off == 0:
        slot_cap = max(1, min(24, cap))
        for row in await db.list_tournament_series_pending_opponent(limit=slot_cap):
            out.append(await _enrich_pending_series_row_for_ui(row))
    return out


@router.get("/api/manager/queue/upcoming", response_class=JSONResponse)
async def api_queue_upcoming(
    limit: int = Query(_HTML_LIST_DEFAULT, ge=1, le=_HTML_LIST_MAX),
    offset: int = Query(0, ge=0),
):
    cap = max(1, min(int(limit), _HTML_LIST_MAX))
    try:
        out = await _compose_queue_upcoming_rows(limit=cap, offset=offset)
    except Exception:
        _log.exception("GET /api/manager/queue/upcoming failed")
        raise HTTPException(500, detail="queue_upcoming_failed") from None
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
    await notify_manager_events(queue=True, series_ids=_series_id_list_from_match(m))
    return m


@router.post("/api/manager/matches/{mid}/complete", response_class=JSONResponse)
async def api_match_complete(mid: int, request: Request):
    body = await request.json()
    winner = body.get("winner", "")
    loser = body.get("loser", "")
    winner_side = body.get("winner_side", "")
    if not winner or not winner_side:
        raise HTTPException(400, "winner and winner_side are required")

    existing = await db.get_match(mid)
    if not existing:
        raise HTTPException(404, "Match not found")

    if existing.get("status") == "completed":
        m = await db.update_match_replay_artifacts(
            mid,
            replay_file=body.get("replay_file"),
            log_file=body.get("log_file"),
            battle_tag=body.get("battle_tag"),
        )
        if not m:
            raise HTTPException(404, "Match not found")
        await notify_manager_events(
            series_ids=_series_id_list_from_match(m),
        )
    else:
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
    if existing.get("status") != "completed":
        tids = [int(m["tournament_id"])] if m.get("tournament_id") is not None else []
        await notify_manager_events(
            queue=True,
            tournament_ids=tids,
            series_ids=_series_id_list_from_match(m),
        )
    await request_scoreboard_publish()
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
    tids = [int(m["tournament_id"])] if m.get("tournament_id") is not None else []
    await notify_manager_events(
        queue=True,
        tournament_ids=tids,
        series_ids=_series_id_list_from_match(m),
    )
    await request_scoreboard_publish()
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
async def api_stats(
    formats: list[str] = Query(default=[]),
    download: str | None = Query(
        None,
        description="Set to json or csv to download with Content-Disposition attachment.",
    ),
):
    battle_formats, _ = await _analytics_format_filter(formats)
    stats = await db.get_stats(battle_formats=battle_formats)
    if not download or not download.strip():
        return stats
    kind = download.strip().lower()
    stem = _stats_export_timestamp_stem()
    if kind in ("json", "1", "true"):
        payload = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "battle_formats_filter": battle_formats,
            **stats,
        }
        body = json.dumps(payload, indent=2)
        return Response(
            content=body,
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="analytics-stats-{stem}.json"'
            },
        )
    if kind == "csv":
        csv_text = _stats_to_csv(stats, battle_formats)
        return Response(
            content=csv_text.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="analytics-stats-{stem}.csv"'
            },
        )
    raise HTTPException(
        status_code=400,
        detail='Invalid download; use "json" or "csv".',
    )


# ===================================================================
# HTML page routes — /manager/...
# ===================================================================


@router.get("/manager", response_class=HTMLResponse)
async def page_dashboard(
    request: Request,
    upcoming_offset: int = Query(0, ge=0),
    upcoming_limit: int = Query(_HTML_LIST_DEFAULT, ge=1, le=_HTML_LIST_MAX),
):
    queue_depth = await db.get_queue_depth()
    upcoming_offset = _clamp_page_offset(upcoming_offset, upcoming_limit, queue_depth)
    running = await db.get_running_match()
    if running:
        running = await _enrich_queued_match_for_ui(running)
    queued = await _compose_queue_upcoming_rows(
        limit=upcoming_limit, offset=upcoming_offset
    )
    upcoming_pagination = _offset_pagination_bar(
        path=str(request.url.path),
        offset=upcoming_offset,
        limit=upcoming_limit,
        total=queue_depth,
        offset_param="upcoming_offset",
        limit_param="upcoming_limit",
        preserve={},
    )
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
            "upcoming_pagination": upcoming_pagination,
            "recent_matches": recent,
            "tournaments": tournaments[:5],
            "showdown_view_base": SHOWDOWN_VIEW_BASE,
            "live_showdown_battle_url": _read_live_showdown_battle_url(),
        },
    )


@router.get("/manager/tournaments", response_class=HTMLResponse)
async def page_tournament_list(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(_HTML_LIST_DEFAULT, ge=1, le=_HTML_LIST_MAX),
):
    path = request.url.path
    total = await db.count_tournaments()
    offset = _clamp_page_offset(offset, limit, total)
    tournaments = await db.list_tournaments(limit=limit, offset=offset)
    pagination = _offset_pagination_bar(
        path=path,
        offset=offset,
        limit=limit,
        total=total,
        offset_param="offset",
        limit_param="limit",
        preserve={},
    )
    return templates.TemplateResponse(
        request=request,
        name="manager/tournament_list.html",
        context={
            "request": request,
            "tournaments": tournaments,
            "tournament_pagination": pagination,
        },
    )


@router.get("/manager/tournament-presets", response_class=HTMLResponse)
async def page_tournament_preset_list(request: Request):
    presets = await db.list_tournament_presets()
    return templates.TemplateResponse(
        request=request,
        name="manager/tournament_preset_list.html",
        context={"request": request, "presets": presets},
    )


@router.get("/manager/tournament-presets/new", response_class=HTMLResponse)
async def page_tournament_preset_new(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="manager/tournament_preset_form.html",
        context={"request": request, "preset": None},
    )


@router.get("/manager/tournament-presets/{pid}/edit", response_class=HTMLResponse)
async def page_tournament_preset_edit(request: Request, pid: int):
    preset = await db.get_tournament_preset(pid)
    if not preset:
        raise HTTPException(404, "Preset not found")
    return templates.TemplateResponse(
        request=request,
        name="manager/tournament_preset_form.html",
        context={"request": request, "preset": preset},
    )


@router.get("/manager/tournaments/new", response_class=HTMLResponse)
async def page_tournament_new(request: Request):
    tournament_presets = await db.list_tournament_presets()
    return templates.TemplateResponse(
        request=request,
        name="manager/tournament_new.html",
        context={
            "request": request,
            "providers": ALLOWED_PROVIDERS,
            "battle_formats": BATTLE_FORMATS,
            "personas": _scan_personas(),
            "tournament_presets": tournament_presets,
            "random_team_battle_format_suffix": battle_format_rules.SHOWDOWN_SERVER_ASSIGNED_TEAM_SUFFIX,
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


@router.get("/manager/teams", response_class=HTMLResponse)
async def page_teams_list(request: Request):
    rows = await db.list_teams()
    return templates.TemplateResponse(
        request=request,
        name="manager/teams_list.html",
        context={
            "request": request,
            "teams": rows,
            "showdown_view_base": SHOWDOWN_VIEW_BASE,
        },
    )


@router.get("/manager/teams/new", response_class=HTMLResponse)
async def page_teams_new(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="manager/team_form.html",
        context={
            "request": request,
            "team": None,
            "team_format_hints": BATTLE_FORMATS_TEAM_PRESET_HINTS,
            "team_format_needs_repick": False,
            "showdown_view_base": SHOWDOWN_VIEW_BASE,
        },
    )


@router.get("/manager/teams/{team_id}/edit", response_class=HTMLResponse)
async def page_teams_edit(request: Request, team_id: int):
    team = await db.get_team(team_id)
    if not team:
        raise HTTPException(404, "Team not found")
    needs_repick = not _team_battle_format_in_preset_hints(
        str(team.get("battle_format") or "")
    )
    return templates.TemplateResponse(
        request=request,
        name="manager/team_form.html",
        context={
            "request": request,
            "team": team,
            "team_format_hints": BATTLE_FORMATS_TEAM_PRESET_HINTS,
            "team_format_needs_repick": needs_repick,
            "showdown_view_base": SHOWDOWN_VIEW_BASE,
        },
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
            "random_team_battle_format_suffix": battle_format_rules.SHOWDOWN_SERVER_ASSIGNED_TEAM_SUFFIX,
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
async def page_results(
    request: Request,
    matches_offset: int = Query(0, ge=0),
    matches_limit: int = Query(_HTML_LIST_DEFAULT, ge=1, le=_HTML_LIST_MAX),
    series_offset: int = Query(0, ge=0),
    series_limit: int = Query(_HTML_LIST_DEFAULT, ge=1, le=_HTML_LIST_MAX),
):
    path = request.url.path
    match_total = await db.count_matches(status="completed")
    matches_offset = _clamp_page_offset(matches_offset, matches_limit, match_total)
    matches = await db.list_matches(
        status="completed", limit=matches_limit, offset=matches_offset
    )
    series_total = await db.count_series_results()
    series_offset = _clamp_page_offset(series_offset, series_limit, series_total)
    raw_series = await db.list_series_results(limit=series_limit, offset=series_offset)
    series_results = [await _enrich_series_api_payload(s) for s in raw_series]
    matches_pagination = _offset_pagination_bar(
        path=path,
        offset=matches_offset,
        limit=matches_limit,
        total=match_total,
        offset_param="matches_offset",
        limit_param="matches_limit",
        preserve={"series_offset": series_offset, "series_limit": series_limit},
    )
    series_pagination = _offset_pagination_bar(
        path=path,
        offset=series_offset,
        limit=series_limit,
        total=series_total,
        offset_param="series_offset",
        limit_param="series_limit",
        preserve={"matches_offset": matches_offset, "matches_limit": matches_limit},
    )
    return templates.TemplateResponse(
        request=request,
        name="manager/results.html",
        context={
            "request": request,
            "matches": matches,
            "series_results": series_results,
            "matches_pagination": matches_pagination,
            "series_pagination": series_pagination,
        },
    )


@router.get("/manager/results/stats", response_class=HTMLResponse)
async def page_stats(request: Request, formats: list[str] = Query(default=[])):
    battle_formats, all_battle_formats = await _analytics_format_filter(formats)
    if battle_formats is None and formats:
        clean = request.url.replace(query="")
        return RedirectResponse(url=str(clean), status_code=307)
    stats = await db.get_stats(battle_formats=battle_formats)
    export_params: list[tuple[str, str]] = []
    if battle_formats is not None:
        for bf in battle_formats:
            export_params.append(("formats", bf))
    stats_export_query = urlencode(export_params)
    return templates.TemplateResponse(
        request=request,
        name="manager/stats.html",
        context={
            "request": request,
            "stats": stats,
            "all_battle_formats": all_battle_formats,
            "selected_formats": battle_formats,
            "stats_export_query": stats_export_query,
        },
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
    if (
        not st.get("configured")
        or not st.get("exists")
        or not st.get("writable")
        or path is None
    ):
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
    portrait_tall_file: UploadFile | None = File(None),
    portrait_square_file: UploadFile | None = File(None),
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
    await _maybe_save_persona_portrait_uploads(
        s, portrait_tall_file, portrait_square_file
    )
    try:
        pstore.require_both_portraits(s)
    except ValueError as e:
        md_path = pstore.PERSONAS_DIR / f"{s}.md"
        if md_path.is_file():
            try:
                md_path.unlink()
            except OSError:
                pass
        pstore.delete_all_portraits_for_slug(s)
        raise HTTPException(400, str(e)) from e
    return RedirectResponse(url="/manager/personas", status_code=303)


async def _maybe_save_persona_sprite_upload(
    slug: str, sprite_file: UploadFile | None
) -> None:
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


async def _maybe_save_persona_portrait_uploads(
    slug: str,
    portrait_tall_file: UploadFile | None,
    portrait_square_file: UploadFile | None,
) -> None:
    for uf, square in (
        (portrait_tall_file, False),
        (portrait_square_file, True),
    ):
        if uf is None or not uf.filename:
            continue
        data = await uf.read()
        if not data:
            continue
        try:
            pstore.save_portrait_upload(slug, uf.filename, data, square=square)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e


@router.get("/manager/personas/{slug}/memory", response_class=HTMLResponse)
async def page_persona_memory(request: Request, slug: str):
    try:
        data = pstore.read_persona(slug)
    except FileNotFoundError:
        raise HTTPException(404, "Persona not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    slug_ok = data["slug"]
    return templates.TemplateResponse(
        request=request,
        name="manager/persona_memory.html",
        context={
            "request": request,
            "p": data,
            "persona_memory_text": _read_persona_adaptive_file(slug_ok, "memory.md"),
            "persona_learnings_text": _read_persona_adaptive_file(
                slug_ok, "learnings.md"
            ),
        },
    )


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
    portrait_tall_file: UploadFile | None = File(None),
    portrait_square_file: UploadFile | None = File(None),
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
    await _maybe_save_persona_portrait_uploads(
        s, portrait_tall_file, portrait_square_file
    )
    try:
        pstore.require_both_portraits(s)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return RedirectResponse(url=f"/manager/personas/{s}/edit", status_code=303)


@router.post("/manager/personas/{slug}/delete")
async def action_persona_delete(
    request: Request,
    slug: str,
    delete_trainer_sprite: str | None = Form(None),
    delete_portrait_tall: str | None = Form(None),
    delete_portrait_square: str | None = Form(None),
):
    try:
        s = pstore.validate_slug(slug)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    also_sprite = delete_trainer_sprite in ("1", "on", "true", "yes")
    also_pt = delete_portrait_tall in ("1", "on", "true", "yes")
    also_ps = delete_portrait_square in ("1", "on", "true", "yes")
    try:
        pstore.delete_persona(
            s,
            delete_trainer_sprite=also_sprite,
            delete_portrait_tall=also_pt,
            delete_portrait_square=also_ps,
        )
    except OSError as e:
        _log.exception("delete persona")
        raise HTTPException(500, detail=str(e)) from e
    return RedirectResponse(url="/manager/personas", status_code=303)
