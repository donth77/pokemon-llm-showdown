"""
Showdown login names aligned with manager display slugs when a persona appears more than once
in the same tournament (e.g. aggro1/aggro2 → DamageDan1/DamageDan2); unique personas stay unsuffixed.

Used when dequeuing tournament matches so agents use stable, distinct account names per entry.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from . import db
from .personas_store import PERSONAS_DIR, parse_front_matter

_MAX_USERNAME_LEN = 18


def _showdown_suffix(base: str, n: int) -> str:
    suffix = str(n)
    room = _MAX_USERNAME_LEN - len(suffix)
    if room < 1:
        return suffix[-_MAX_USERNAME_LEN:]
    return (base or "")[:room] + suffix


def battle_username_from_persona_slug(slug: str) -> str:
    """Strip spaces from YAML ``name`` (same rule as agents ``_make_player_name``)."""
    slug = (slug or "").strip()
    if not slug:
        return "Persona"
    path = PERSONAS_DIR / f"{slug}.md"
    if not path.is_file():
        s = re.sub(r"\s+", "", slug) or "Persona"
        return s[:_MAX_USERNAME_LEN]
    text = path.read_text(encoding="utf-8")
    meta, _ = parse_front_matter(text)
    raw_name = (meta.get("name") or slug).strip()
    normalized = re.sub(r"\s+", "", raw_name) or slug
    if len(normalized) > _MAX_USERNAME_LEN:
        return normalized[:_MAX_USERNAME_LEN]
    return normalized


async def entry_id_to_showdown_account_map(tournament_id: int) -> dict[int, str]:
    """
    Map tournament_entries.id → Showdown username (DamageDan / DamageDan1 / …).
    Same ordering and duplicate-slug rules as ``entry_rows_to_display_slug_map``.
    """
    rows = await db.tournament_entries_ordered(tournament_id)
    if not rows:
        return {}
    counts = Counter(str(r["persona_slug"]) for r in rows)
    next_n: dict[str, int] = defaultdict(int)
    out: dict[int, str] = {}
    ordered = sorted(rows, key=lambda r: (r.get("seed") or 0, int(r["id"])))
    for r in ordered:
        slug = str(r["persona_slug"])
        eid = int(r["id"])
        base = battle_username_from_persona_slug(slug)
        if counts[slug] <= 1:
            out[eid] = base[:_MAX_USERNAME_LEN]
        else:
            next_n[slug] += 1
            out[eid] = _showdown_suffix(base, next_n[slug])
    return out


async def showdown_accounts_for_match(m: dict) -> tuple[str | None, str | None]:
    """
    Return (player1_account, player2_account) for a running/queued match row, or (None, None)
    when not a tournament series with entry ids (workers fall back to mirror logic).
    """
    tid = m.get("tournament_id")
    sid = m.get("series_id")
    if tid is None or sid is None:
        return (None, None)
    sm = await db.get_series_bracket_meta(int(sid))
    if not sm:
        return (None, None)
    e1, e2, sm_tid = sm
    if e1 is None or e2 is None:
        return (None, None)
    if sm_tid is None or int(sm_tid) != int(tid):
        return (None, None)
    emap = await entry_id_to_showdown_account_map(int(tid))
    a1 = emap.get(int(e1))
    a2 = emap.get(int(e2))
    if not a1 or not a2:
        return (None, None)
    if a1 == a2:
        a2 = _showdown_suffix(a1, 2)
    return (a1, a2)
