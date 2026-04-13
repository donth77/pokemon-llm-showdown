"""
SQLite database layer for the tournament manager.

Provides schema creation, async CRUD helpers, and query utilities backed by
aiosqlite.  The DB file lives on the manager-data volume so it persists across
container restarts.
"""

from __future__ import annotations

import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

DB_PATH = Path("/manager-data/manager.db")

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tournaments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    type        TEXT    NOT NULL CHECK (type IN ('round_robin', 'single_elimination', 'double_elimination')),
    status      TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'completed', 'cancelled')),
    battle_format TEXT  NOT NULL,
    best_of     INTEGER NOT NULL DEFAULT 1,
    single_elim_bracket TEXT,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS tournament_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    provider      TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    persona_slug  TEXT    NOT NULL,
    seed          INTEGER NOT NULL DEFAULT 0,
    display_name  TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_te_tournament ON tournament_entries(tournament_id);

CREATE TABLE IF NOT EXISTS series (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id   INTEGER REFERENCES tournaments(id) ON DELETE CASCADE,
    best_of         INTEGER NOT NULL DEFAULT 1,
    status          TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'completed', 'cancelled')),
    battle_format   TEXT    NOT NULL,
    round_number    INTEGER,
    match_position  INTEGER,
    bracket         TEXT    CHECK (bracket IN ('winners', 'losers', 'grand_finals', 'grand_finals_reset', NULL)),
    player1_provider TEXT,
    player1_model    TEXT,
    player1_persona  TEXT,
    player1_entry_id INTEGER REFERENCES tournament_entries(id),
    player2_provider TEXT,
    player2_model    TEXT,
    player2_persona  TEXT,
    player2_entry_id INTEGER REFERENCES tournament_entries(id),
    player1_wins    INTEGER NOT NULL DEFAULT 0,
    player2_wins    INTEGER NOT NULL DEFAULT 0,
    winner_entry_id INTEGER REFERENCES tournament_entries(id),
    winner_side     TEXT    CHECK (winner_side IN ('p1', 'p2', NULL)),
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_series_tournament ON series(tournament_id);
CREATE INDEX IF NOT EXISTS idx_series_status ON series(status);

CREATE TABLE IF NOT EXISTS matches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id       INTEGER REFERENCES series(id) ON DELETE CASCADE,
    tournament_id   INTEGER REFERENCES tournaments(id) ON DELETE CASCADE,
    game_number     INTEGER NOT NULL DEFAULT 1,
    status          TEXT    NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'completed', 'error', 'cancelled')),
    battle_format   TEXT    NOT NULL,
    player1_provider TEXT   NOT NULL,
    player1_model    TEXT   NOT NULL,
    player1_persona  TEXT   NOT NULL,
    player2_provider TEXT   NOT NULL,
    player2_model    TEXT   NOT NULL,
    player2_persona  TEXT   NOT NULL,
    winner          TEXT,
    loser           TEXT,
    winner_side     TEXT    CHECK (winner_side IN ('p1', 'p2', NULL)),
    replay_file     TEXT,
    log_file        TEXT,
    battle_tag      TEXT,
    duration        REAL,
    error_message   TEXT,
    queued_at       REAL    NOT NULL,
    started_at      REAL,
    completed_at    REAL
);

CREATE TABLE IF NOT EXISTS tournament_presets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL COLLATE NOCASE,
    body        TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    UNIQUE(name)
);
CREATE INDEX IF NOT EXISTS idx_tpresets_name ON tournament_presets(name);
"""


async def _migrate_sqlite_columns() -> None:
    """
    Add columns/indexes missing from older manager DBs (volume survived a schema change).

    CREATE TABLE IF NOT EXISTS does not upgrade existing tables; without this,
    queue ORDER BY queued_at etc. can raise OperationalError and return HTTP 500.
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='matches'"
        )
        if not await cur.fetchone():
            await db.commit()
            return
        cur = await db.execute("PRAGMA table_info(matches)")
        have = {r[1] for r in await cur.fetchall()}
        alters: list[str] = []
        # Keep in sync with CREATE TABLE matches (...) in _SCHEMA_SQL
        needed = [
            ("game_number", "INTEGER NOT NULL DEFAULT 1"),
            ("status", "TEXT NOT NULL DEFAULT 'queued'"),
            ("battle_format", "TEXT NOT NULL DEFAULT ''"),
            ("player1_provider", "TEXT NOT NULL DEFAULT ''"),
            ("player1_model", "TEXT NOT NULL DEFAULT ''"),
            ("player1_persona", "TEXT NOT NULL DEFAULT ''"),
            ("player2_provider", "TEXT NOT NULL DEFAULT ''"),
            ("player2_model", "TEXT NOT NULL DEFAULT ''"),
            ("player2_persona", "TEXT NOT NULL DEFAULT ''"),
            ("winner", "TEXT"),
            ("loser", "TEXT"),
            ("winner_side", "TEXT"),
            ("replay_file", "TEXT"),
            ("log_file", "TEXT"),
            ("battle_tag", "TEXT"),
            ("duration", "REAL"),
            ("error_message", "TEXT"),
            ("queued_at", "REAL NOT NULL DEFAULT 0"),
            ("started_at", "REAL"),
            ("completed_at", "REAL"),
            ("series_id", "INTEGER"),
            ("tournament_id", "INTEGER"),
            ("player1_type", "TEXT NOT NULL DEFAULT 'llm'"),
            ("player2_type", "TEXT NOT NULL DEFAULT 'llm'"),
            ("human_display_name", "TEXT"),
            ("human_play_mode", "TEXT NOT NULL DEFAULT 'showdown'"),
        ]
        for col, ctype in needed:
            if col not in have:
                alters.append(f"ALTER TABLE matches ADD COLUMN {col} {ctype}")
        for stmt in alters:
            await db.execute(stmt)
        # Indexes reference match columns — run only after migrations (not in executescript),
        # otherwise an older matches table missing columns breaks init_db entirely.
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_matches_series ON matches(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_matches_tournament ON matches(tournament_id)",
            "CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status)",
            "CREATE INDEX IF NOT EXISTS idx_matches_queue ON matches(status, queued_at)",
        ):
            await db.execute(idx_sql)
        await db.commit()


async def _migrate_series_bracket_grand_finals_reset() -> None:
    """
    Allow series.bracket = 'grand_finals_reset' (SQLite CHECK cannot be altered in place).
    Rebuilds ``series`` when the live table DDL predates this value.
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cur = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='series'"
        )
        row = await cur.fetchone()
        ddl = (row[0] or "") if row else ""
        if not ddl or "grand_finals_reset" in ddl:
            return
        await db.execute("PRAGMA foreign_keys=OFF")
        await db.executescript(
            """
            CREATE TABLE series__gfreset (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id   INTEGER REFERENCES tournaments(id) ON DELETE CASCADE,
                best_of         INTEGER NOT NULL DEFAULT 1,
                status          TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'completed', 'cancelled')),
                battle_format   TEXT    NOT NULL,
                round_number    INTEGER,
                match_position  INTEGER,
                bracket         TEXT    CHECK (bracket IN ('winners', 'losers', 'grand_finals', 'grand_finals_reset', NULL)),
                player1_provider TEXT,
                player1_model    TEXT,
                player1_persona  TEXT,
                player1_entry_id INTEGER REFERENCES tournament_entries(id),
                player2_provider TEXT,
                player2_model    TEXT,
                player2_persona  TEXT,
                player2_entry_id INTEGER REFERENCES tournament_entries(id),
                player1_wins    INTEGER NOT NULL DEFAULT 0,
                player2_wins    INTEGER NOT NULL DEFAULT 0,
                winner_entry_id INTEGER REFERENCES tournament_entries(id),
                winner_side     TEXT    CHECK (winner_side IN ('p1', 'p2', NULL)),
                created_at      REAL    NOT NULL,
                updated_at      REAL    NOT NULL
            );
            INSERT INTO series__gfreset SELECT * FROM series;
            DROP TABLE series;
            ALTER TABLE series__gfreset RENAME TO series;
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_series_tournament ON series(tournament_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_series_status ON series(status)"
        )
        cur_m = await db.execute("SELECT MAX(id) FROM series")
        r = await cur_m.fetchone()
        mx = int(r[0]) if r and r[0] is not None else 0
        await db.execute("DELETE FROM sqlite_sequence WHERE name='series'")
        await db.execute(
            "INSERT INTO sqlite_sequence(name,seq) VALUES ('series', ?)", (mx,)
        )
        await db.execute("PRAGMA foreign_keys=ON")
        await db.commit()


async def _migrate_tournament_columns() -> None:
    """Add columns missing from older tournaments table."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tournaments'"
        )
        if not await cur.fetchone():
            await db.commit()
            return
        cur = await db.execute("PRAGMA table_info(tournaments)")
        have = {r[1] for r in await cur.fetchall()}
        if "single_elim_bracket" not in have:
            await db.execute(
                "ALTER TABLE tournaments ADD COLUMN single_elim_bracket TEXT"
            )
        await db.commit()


async def _migrate_human_player_columns() -> None:
    """Add player_type + human_display_name columns to matches and series."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        for table in ("matches", "series"):
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            if not await cur.fetchone():
                continue
            cur = await db.execute(f"PRAGMA table_info({table})")
            have = {r[1] for r in await cur.fetchall()}
            needed = [
                ("player1_type", "TEXT NOT NULL DEFAULT 'llm'"),
                ("player2_type", "TEXT NOT NULL DEFAULT 'llm'"),
                ("human_display_name", "TEXT"),
                ("human_play_mode", "TEXT NOT NULL DEFAULT 'showdown'"),
            ]
            for col, ctype in needed:
                if col not in have:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")
        await db.commit()


async def init_db() -> None:
    """Create tables if they don't exist and stamp the schema version."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.executescript(_SCHEMA_SQL)
        cur = await db.execute("SELECT COUNT(*) FROM schema_version")
        row = await cur.fetchone()
        if row and row[0] == 0:
            await db.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (_SCHEMA_VERSION,),
            )
        await db.commit()
    await _migrate_sqlite_columns()
    await _migrate_series_bracket_grand_finals_reset()
    await _migrate_tournament_columns()
    await _ensure_tournament_presets_table()
    await _migrate_teams_library()
    await _migrate_human_player_columns()


async def _ensure_tournament_presets_table() -> None:
    """CREATE TABLE for DBs created before tournament_presets existed."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_presets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL COLLATE NOCASE,
                body        TEXT    NOT NULL,
                created_at  REAL    NOT NULL,
                updated_at  REAL    NOT NULL,
                UNIQUE(name)
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tpresets_name ON tournament_presets(name)"
        )
        await db.commit()


async def _migrate_teams_library() -> None:
    """teams table, entry team_id, match team FK + snapshot columns."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                battle_format TEXT NOT NULL DEFAULT '',
                showdown_text TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_teams_name_nocase ON teams(name COLLATE NOCASE)"
        )
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tournament_entries'"
        )
        if await cur.fetchone():
            cur = await db.execute("PRAGMA table_info(tournament_entries)")
            te_cols = {r[1] for r in await cur.fetchall()}
            if "team_id" not in te_cols:
                await db.execute(
                    "ALTER TABLE tournament_entries ADD COLUMN team_id INTEGER"
                )
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='matches'"
        )
        if await cur.fetchone():
            cur = await db.execute("PRAGMA table_info(matches)")
            m_cols = {r[1] for r in await cur.fetchall()}
            for col, decl in (
                ("player1_team_id", "INTEGER"),
                ("player2_team_id", "INTEGER"),
                ("player1_team_showdown", "TEXT"),
                ("player2_team_showdown", "TEXT"),
            ):
                if col not in m_cols:
                    await db.execute(f"ALTER TABLE matches ADD COLUMN {col} {decl}")
        await db.commit()


def _now() -> float:
    return time.time()


@asynccontextmanager
async def _db():
    """One aiosqlite connection per block (avoids double ``__aenter__`` / thread start)."""
    async with aiosqlite.connect(str(DB_PATH)) as conn:
        conn.row_factory = aiosqlite.Row
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn


def entry_rows_to_display_slug_map(rows: list[dict[str, Any]]) -> dict[int, str]:
    """
    Stable aggro1, aggro2, … among entries sharing a persona_slug in one tournament.
    Rows must include id, persona_slug, seed (ORDER BY seed, id recommended).
    """
    if not rows:
        return {}
    counts = Counter(str(r["persona_slug"]) for r in rows)
    next_n: dict[str, int] = defaultdict(int)
    ordered = sorted(rows, key=lambda r: (r.get("seed") or 0, int(r["id"])))
    out: dict[int, str] = {}
    for r in ordered:
        slug = str(r["persona_slug"])
        eid = int(r["id"])
        if counts[slug] <= 1:
            out[eid] = slug
        else:
            next_n[slug] += 1
            out[eid] = f"{slug}{next_n[slug]}"
    return out


async def tournament_entries_ordered(tournament_id: int) -> list[dict[str, Any]]:
    """Tournament roster in bracket order (seed, id) for display + Showdown account mapping."""
    async with _db() as db:
        cur = await db.execute(
            """SELECT id, persona_slug, seed FROM tournament_entries
               WHERE tournament_id = ? ORDER BY seed, id""",
            (tournament_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def tournament_entry_display_slug_map(tournament_id: int) -> dict[int, str]:
    rows = await tournament_entries_ordered(tournament_id)
    return entry_rows_to_display_slug_map(rows)


async def get_series_bracket_meta(
    series_id: int,
) -> tuple[int | None, int | None, int | None]:
    """(player1_entry_id, player2_entry_id, tournament_id) from series row."""
    async with _db() as db:
        cur = await db.execute(
            """SELECT player1_entry_id, player2_entry_id, tournament_id
               FROM series WHERE id = ?""",
            (series_id,),
        )
        row = await cur.fetchone()
        if not row:
            return (None, None, None)
        r = dict(row)
        return (
            r.get("player1_entry_id"),
            r.get("player2_entry_id"),
            r.get("tournament_id"),
        )


def attach_tournament_persona_display_fields(t: dict[str, Any]) -> None:
    """Mutate tournament payload: slug disambiguation + Showdown-style names on entries/series."""
    entries = t.get("entries") or []
    if not entries:
        for s in t.get("series") or []:
            s["player1_persona_display"] = s.get("player1_persona")
            s["player2_persona_display"] = s.get("player2_persona")
    else:
        emap = entry_rows_to_display_slug_map(entries)
        for e in entries:
            e["persona_display_slug"] = emap[int(e["id"])]
        for s in t.get("series") or []:
            p1e, p2e = s.get("player1_entry_id"), s.get("player2_entry_id")
            s["player1_persona_display"] = (
                emap.get(int(p1e), s.get("player1_persona"))
                if p1e is not None
                else s.get("player1_persona")
            )
            s["player2_persona_display"] = (
                emap.get(int(p2e), s.get("player2_persona"))
                if p2e is not None
                else s.get("player2_persona")
            )

    from .routes import (
        _battle_name_for_persona_slug,
        _numbered_battle_name_for_tournament_slot,
    )

    series_list = t.get("series") or []
    if not entries:
        for s in series_list:
            s1 = (s.get("player1_persona") or "").strip()
            s2 = (s.get("player2_persona") or "").strip()
            s["player1_battle_display"] = (
                _battle_name_for_persona_slug(s1) or "TBD" if s1 else "TBD"
            )
            s["player2_battle_display"] = (
                _battle_name_for_persona_slug(s2) or "TBD" if s2 else "TBD"
            )
    else:
        slugs_by_eid: dict[int, str] = {}
        emap_eid: dict[int, str] = {}
        for e in entries:
            eid = int(e["id"])
            slug = (e.get("persona_slug") or "").strip()
            slugs_by_eid[eid] = slug
            ds = (e.get("persona_display_slug") or slug).strip()
            emap_eid[eid] = ds
            if slug:
                base = _battle_name_for_persona_slug(slug)
                e["battle_display_name"] = _numbered_battle_name_for_tournament_slot(
                    base, slug, ds
                )
            else:
                e["battle_display_name"] = (e.get("display_name") or "").strip() or "—"

        for s in series_list:
            for side in ("player1", "player2"):
                entry_key = f"{side}_entry_id"
                persona_key = f"{side}_persona"
                out_key = f"{side}_battle_display"
                eid_raw = s.get(entry_key)
                pslug = (s.get(persona_key) or "").strip()
                if eid_raw is not None:
                    eid_i = int(eid_raw)
                    pslug = slugs_by_eid.get(eid_i) or pslug
                    ds = emap_eid.get(eid_i, pslug)
                else:
                    ds = pslug
                if not pslug:
                    s[out_key] = "TBD"
                else:
                    base = _battle_name_for_persona_slug(pslug)
                    s[out_key] = _numbered_battle_name_for_tournament_slot(
                        base, pslug, ds
                    )


# ---------------------------------------------------------------------------
# Tournament CRUD
# ---------------------------------------------------------------------------


async def create_tournament(
    *,
    name: str,
    type: str,
    battle_format: str,
    best_of: int = 1,
    entries: list[dict[str, Any]],
    single_elim_bracket: str | None = None,
) -> dict:
    now = _now()
    # Column name legacy: also applies to double elimination (winners bracket layout).
    seb: str | None = None
    if type in ("single_elimination", "double_elimination"):
        x = (single_elim_bracket or "compact").strip().lower()
        seb = x if x in ("compact", "power_of_two") else "compact"
    async with _db() as db:
        cur = await db.execute(
            """INSERT INTO tournaments (name, type, status, battle_format, best_of,
                single_elim_bracket, created_at, updated_at)
               VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)""",
            (name, type, battle_format, best_of, seb, now, now),
        )
        tid = cur.lastrowid
        for i, e in enumerate(entries):
            raw_tid = e.get("team_id")
            entry_team_id: int | None
            if raw_tid is None or raw_tid == "":
                entry_team_id = None
            else:
                try:
                    entry_team_id = int(raw_tid)
                except (TypeError, ValueError):
                    entry_team_id = None
            await db.execute(
                """INSERT INTO tournament_entries
                   (tournament_id, provider, model, persona_slug, seed, display_name, team_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    tid,
                    e["provider"],
                    e["model"],
                    e["persona_slug"],
                    e.get("seed", i + 1),
                    e.get("display_name", ""),
                    entry_team_id,
                ),
            )
        await db.commit()
    return await get_tournament(tid)  # type: ignore[arg-type]


async def get_tournament(tournament_id: int) -> dict | None:
    async with _db() as db:
        cur = await db.execute(
            "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        t = dict(row)
        cur2 = await db.execute(
            "SELECT * FROM tournament_entries WHERE tournament_id = ? ORDER BY seed",
            (tournament_id,),
        )
        t["entries"] = [dict(r) for r in await cur2.fetchall()]
        cur3 = await db.execute(
            "SELECT * FROM series WHERE tournament_id = ? ORDER BY round_number, match_position",
            (tournament_id,),
        )
        t["series"] = [dict(r) for r in await cur3.fetchall()]
    attach_tournament_persona_display_fields(t)
    return t


# ---------------------------------------------------------------------------
# Tournament definition presets (plaintext; stored on manager-data volume)
# ---------------------------------------------------------------------------

PRESET_NAME_DUP = "duplicate_preset_name"


async def list_tournament_presets() -> list[dict[str, Any]]:
    async with _db() as db:
        cur = await db.execute(
            """SELECT id, name, created_at, updated_at FROM tournament_presets
               ORDER BY name COLLATE NOCASE"""
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_tournament_preset(preset_id: int) -> dict[str, Any] | None:
    async with _db() as db:
        cur = await db.execute(
            "SELECT * FROM tournament_presets WHERE id = ?", (preset_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def create_tournament_preset(*, name: str, body: str) -> dict[str, Any]:
    now = _now()
    nm = name.strip()
    async with _db() as db:
        try:
            cur = await db.execute(
                """INSERT INTO tournament_presets (name, body, created_at, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (nm, body, now, now),
            )
            pid = cur.lastrowid
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper():
                raise ValueError(PRESET_NAME_DUP) from exc
            raise
        await db.commit()
    out = await get_tournament_preset(int(pid))
    assert out is not None
    return out


async def update_tournament_preset(
    preset_id: int,
    *,
    name: str | None = None,
    body: str | None = None,
) -> dict[str, Any] | None:
    if name is None and body is None:
        return await get_tournament_preset(preset_id)
    now = _now()
    async with _db() as db:
        cur = await db.execute(
            "SELECT id FROM tournament_presets WHERE id = ?", (preset_id,)
        )
        if not await cur.fetchone():
            return None
        if name is not None and body is not None:
            try:
                await db.execute(
                    """UPDATE tournament_presets SET name = ?, body = ?, updated_at = ?
                       WHERE id = ?""",
                    (name.strip(), body, now, preset_id),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc).upper():
                    raise ValueError(PRESET_NAME_DUP) from exc
                raise
        elif name is not None:
            try:
                await db.execute(
                    "UPDATE tournament_presets SET name = ?, updated_at = ? WHERE id = ?",
                    (name.strip(), now, preset_id),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc).upper():
                    raise ValueError(PRESET_NAME_DUP) from exc
                raise
        else:
            await db.execute(
                "UPDATE tournament_presets SET body = ?, updated_at = ? WHERE id = ?",
                (body if body is not None else "", now, preset_id),
            )
        await db.commit()
    return await get_tournament_preset(preset_id)


async def delete_tournament_preset(preset_id: int) -> bool:
    async with _db() as db:
        cur = await db.execute(
            "DELETE FROM tournament_presets WHERE id = ?", (preset_id,)
        )
        await db.commit()
        return cur.rowcount > 0


def _winner_entry_from_series(
    series_completed: list[dict], ttype: str
) -> tuple[int | None, bool]:
    """
    Pick champion entry id for a completed tournament; return (entry_id, tie).
    tie is True for round-robin when multiple entries share the best series-wins count.
    """
    if not series_completed:
        return (None, False)
    if ttype == "round_robin":
        c = Counter()
        for s in series_completed:
            we = s.get("winner_entry_id")
            if we is not None:
                c[int(we)] += 1
        if not c:
            return (None, False)
        maxw = max(c.values())
        top = [eid for eid, cnt in c.items() if cnt == maxw]
        if len(top) == 1:
            return (top[0], False)
        return (None, True)
    if ttype == "double_elimination":
        gf_reset = [
            s for s in series_completed if s.get("bracket") == "grand_finals_reset"
        ]
        if gf_reset:
            last = max(
                gf_reset, key=lambda s: (s.get("updated_at") or 0, s.get("id") or 0)
            )
            we = last.get("winner_entry_id")
            return (int(we), False) if we is not None else (None, False)
        gfs = [s for s in series_completed if s.get("bracket") == "grand_finals"]
        if not gfs:
            return (None, False)
        last = max(gfs, key=lambda s: (s.get("updated_at") or 0, s.get("id") or 0))
        we = last.get("winner_entry_id")
        return (int(we), False) if we is not None else (None, False)
    if ttype == "single_elimination":
        wseries = [s for s in series_completed if s.get("bracket") in (None, "winners")]
        if not wseries:
            wseries = series_completed
        last = max(
            wseries,
            key=lambda s: (s.get("round_number") or 0, s.get("match_position") or 0),
        )
        we = last.get("winner_entry_id")
        return (int(we), False) if we is not None else (None, False)
    return (None, False)


async def _attach_tournament_winner_fields(
    db: aiosqlite.Connection, tournaments: list[dict]
) -> None:
    """Set winner_persona_slug and winner_is_tie on each row (list / API)."""
    completed = [t for t in tournaments if t.get("status") == "completed"]
    for t in tournaments:
        if t.get("status") != "completed":
            t["winner_persona_slug"] = None
            t["winner_is_tie"] = False

    if not completed:
        return

    ids = [t["id"] for t in completed]
    placeholders = ",".join("?" * len(ids))
    cur = await db.execute(
        f"""SELECT tournament_id, round_number, match_position, bracket, winner_entry_id, updated_at, id
            FROM series WHERE tournament_id IN ({placeholders}) AND status = 'completed'""",
        ids,
    )
    all_s = [dict(r) for r in await cur.fetchall()]
    by_tid: dict[int, list[dict]] = {}
    for s in all_s:
        by_tid.setdefault(int(s["tournament_id"]), []).append(s)

    meta: dict[int, tuple[int | None, bool]] = {}
    entry_ids: set[int] = set()
    for tc in completed:
        tid = int(tc["id"])
        slist = by_tid.get(tid, [])
        we, tie = _winner_entry_from_series(slist, tc["type"])
        meta[tid] = (we, tie)
        if we is not None:
            entry_ids.add(we)

    slug_by_eid: dict[int, str] = {}
    if entry_ids:
        e_place = ",".join("?" * len(entry_ids))
        cur2 = await db.execute(
            f"SELECT id, persona_slug FROM tournament_entries WHERE id IN ({e_place})",
            list(entry_ids),
        )
        for r in await cur2.fetchall():
            slug_by_eid[int(r["id"])] = str(r["persona_slug"])

    for t in tournaments:
        if t.get("status") != "completed":
            continue
        tid = int(t["id"])
        we, tie = meta.get(tid, (None, False))
        t["winner_is_tie"] = tie
        t["winner_persona_slug"] = None if tie or we is None else slug_by_eid.get(we)


async def list_tournaments(
    *,
    status: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    async with _db() as db:
        sql = "SELECT * FROM tournaments"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        if limit is not None:
            cap = max(1, min(int(limit), 500))
            off = max(0, int(offset))
            sql += " LIMIT ? OFFSET ?"
            params.extend([cap, off])
        cur = await db.execute(sql, params)
        tournaments = [dict(r) for r in await cur.fetchall()]
        await _attach_tournament_winner_fields(db, tournaments)
        return tournaments


async def count_tournaments(*, status: str | None = None) -> int:
    async with _db() as db:
        if status:
            cur = await db.execute(
                "SELECT COUNT(*) FROM tournaments WHERE status = ?", (status,)
            )
        else:
            cur = await db.execute("SELECT COUNT(*) FROM tournaments")
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def update_tournament_status(tournament_id: int, status: str) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE tournaments SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), tournament_id),
        )
        await db.commit()


async def cancel_tournament(tournament_id: int) -> None:
    """Cancel a tournament and all its pending/queued series and matches."""
    async with _db() as db:
        await db.execute(
            "UPDATE tournaments SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (_now(), tournament_id),
        )
        await db.execute(
            "UPDATE series SET status = 'cancelled', updated_at = ? WHERE tournament_id = ? AND status IN ('pending', 'in_progress')",
            (_now(), tournament_id),
        )
        await db.execute(
            "UPDATE matches SET status = 'cancelled' WHERE tournament_id = ? AND status IN ('queued', 'running')",
            (tournament_id,),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Series CRUD
# ---------------------------------------------------------------------------


async def _team_snapshot_from_id(
    db: aiosqlite.Connection, team_id: int | None
) -> tuple[int | None, str | None]:
    if team_id is None:
        return None, None
    cur = await db.execute(
        "SELECT id, showdown_text FROM teams WHERE id = ?", (int(team_id),)
    )
    row = await cur.fetchone()
    if not row:
        return None, None
    return int(row["id"]), str(row["showdown_text"])


async def _team_snapshot_for_series_side(
    db: aiosqlite.Connection,
    entry_id: int | None,
    explicit_team_id: int | None,
) -> tuple[int | None, str | None]:
    tid: int | None = explicit_team_id
    if entry_id is not None:
        cur = await db.execute(
            "SELECT team_id FROM tournament_entries WHERE id = ?", (int(entry_id),)
        )
        row = await cur.fetchone()
        if row and row["team_id"] is not None:
            tid = int(row["team_id"])
    return await _team_snapshot_from_id(db, tid)


async def create_series(
    *,
    tournament_id: int | None = None,
    best_of: int = 1,
    battle_format: str,
    round_number: int | None = None,
    match_position: int | None = None,
    bracket: str | None = None,
    player1_provider: str | None = None,
    player1_model: str | None = None,
    player1_persona: str | None = None,
    player1_entry_id: int | None = None,
    player2_provider: str | None = None,
    player2_model: str | None = None,
    player2_persona: str | None = None,
    player2_entry_id: int | None = None,
    player1_team_id: int | None = None,
    player2_team_id: int | None = None,
    auto_queue: bool = True,
) -> dict:
    """Create a series and optionally queue its matches."""
    now = _now()
    async with _db() as db:
        cur = await db.execute(
            """INSERT INTO series
               (tournament_id, best_of, status, battle_format, round_number,
                match_position, bracket,
                player1_provider, player1_model, player1_persona, player1_entry_id,
                player2_provider, player2_model, player2_persona, player2_entry_id,
                player1_wins, player2_wins, created_at, updated_at)
               VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)""",
            (
                tournament_id,
                best_of,
                battle_format,
                round_number,
                match_position,
                bracket,
                player1_provider,
                player1_model,
                player1_persona,
                player1_entry_id,
                player2_provider,
                player2_model,
                player2_persona,
                player2_entry_id,
                now,
                now,
            ),
        )
        sid = cur.lastrowid

        if auto_queue and all([player1_provider, player2_provider]):
            p1_tid, p1_txt = await _team_snapshot_for_series_side(
                db, player1_entry_id, player1_team_id
            )
            p2_tid, p2_txt = await _team_snapshot_for_series_side(
                db, player2_entry_id, player2_team_id
            )
            for g in range(1, best_of + 1):
                await db.execute(
                    """INSERT INTO matches
                       (series_id, tournament_id, game_number, status, battle_format,
                        player1_provider, player1_model, player1_persona,
                        player2_provider, player2_model, player2_persona,
                        player1_team_id, player2_team_id,
                        player1_team_showdown, player2_team_showdown,
                        queued_at)
                       VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        sid,
                        tournament_id,
                        g,
                        battle_format,
                        player1_provider,
                        player1_model,
                        player1_persona,
                        player2_provider,
                        player2_model,
                        player2_persona,
                        p1_tid,
                        p2_tid,
                        p1_txt,
                        p2_txt,
                        now,
                    ),
                )
        await db.commit()
    return await get_series(sid)  # type: ignore[arg-type]


async def get_series(series_id: int) -> dict | None:
    async with _db() as db:
        cur = await db.execute(
            """SELECT s.*, t.name AS tournament_name
               FROM series s
               LEFT JOIN tournaments t ON s.tournament_id = t.id
               WHERE s.id = ?""",
            (series_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        s = dict(row)
        cur2 = await db.execute(
            "SELECT * FROM matches WHERE series_id = ? ORDER BY game_number",
            (series_id,),
        )
        s["matches"] = [dict(r) for r in await cur2.fetchall()]
    return s


async def list_series(*, tournament_id: int | None = None) -> list[dict]:
    async with _db() as db:
        if tournament_id is not None:
            cur = await db.execute(
                "SELECT * FROM series WHERE tournament_id = ? ORDER BY created_at DESC",
                (tournament_id,),
            )
        else:
            cur = await db.execute("SELECT * FROM series ORDER BY created_at DESC")
        return [dict(r) for r in await cur.fetchall()]


async def list_series_results(*, limit: int = 100, offset: int = 0) -> list[dict]:
    """Completed or cancelled series for the manager results page (newest first)."""
    cap = max(1, min(int(limit), 500))
    off = max(0, int(offset))
    async with _db() as db:
        cur = await db.execute(
            """SELECT s.*, t.name AS tournament_name
               FROM series s
               LEFT JOIN tournaments t ON s.tournament_id = t.id
               WHERE s.status IN ('completed', 'cancelled')
               ORDER BY s.updated_at DESC
               LIMIT ? OFFSET ?""",
            (cap, off),
        )
        return [dict(r) for r in await cur.fetchall()]


async def count_series_results() -> int:
    async with _db() as db:
        cur = await db.execute(
            """SELECT COUNT(*) FROM series s
               WHERE s.status IN ('completed', 'cancelled')"""
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def update_series_wins(series_id: int, winner_side: str) -> dict:
    """Increment win count for a side and return updated series row."""
    col = "player1_wins" if winner_side == "p1" else "player2_wins"
    async with _db() as db:
        await db.execute(
            f"UPDATE series SET {col} = {col} + 1, updated_at = ? WHERE id = ?",
            (_now(), series_id),
        )
        await db.commit()
    return await get_series(series_id)  # type: ignore[return-value]


async def complete_series(
    series_id: int, winner_side: str, winner_entry_id: int | None = None
) -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE series SET status = 'completed', winner_side = ?, winner_entry_id = ?, updated_at = ? WHERE id = ?",
            (winner_side, winner_entry_id, _now(), series_id),
        )
        # Cancel remaining queued matches in this series
        await db.execute(
            "UPDATE matches SET status = 'cancelled' WHERE series_id = ? AND status = 'queued'",
            (series_id,),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Match CRUD
# ---------------------------------------------------------------------------


async def create_match(
    *,
    series_id: int | None = None,
    tournament_id: int | None = None,
    game_number: int = 1,
    battle_format: str,
    player1_provider: str,
    player1_model: str,
    player1_persona: str,
    player2_provider: str,
    player2_model: str,
    player2_persona: str,
    player1_team_id: int | None = None,
    player2_team_id: int | None = None,
    player1_type: str = "llm",
    player2_type: str = "llm",
    human_display_name: str | None = None,
    human_play_mode: str = "showdown",
) -> dict:
    now = _now()
    async with _db() as db:
        p1_tid, p1_txt = await _team_snapshot_from_id(db, player1_team_id)
        p2_tid, p2_txt = await _team_snapshot_from_id(db, player2_team_id)
        cur = await db.execute(
            """INSERT INTO matches
               (series_id, tournament_id, game_number, status, battle_format,
                player1_provider, player1_model, player1_persona,
                player2_provider, player2_model, player2_persona,
                player1_team_id, player2_team_id,
                player1_team_showdown, player2_team_showdown,
                player1_type, player2_type, human_display_name,
                human_play_mode,
                queued_at)
               VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                series_id,
                tournament_id,
                game_number,
                battle_format,
                player1_provider,
                player1_model,
                player1_persona,
                player2_provider,
                player2_model,
                player2_persona,
                p1_tid,
                p2_tid,
                p1_txt,
                p2_txt,
                player1_type,
                player2_type,
                human_display_name,
                human_play_mode,
                now,
            ),
        )
        mid = cur.lastrowid
        await db.commit()
    return await get_match(mid)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Team presets (Showdown paste text)
# ---------------------------------------------------------------------------


async def list_teams() -> list[dict]:
    async with _db() as db:
        cur = await db.execute(
            """SELECT id, name, battle_format, notes, created_at, updated_at
               FROM teams ORDER BY name COLLATE NOCASE"""
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_team(team_id: int) -> dict | None:
    async with _db() as db:
        cur = await db.execute("SELECT * FROM teams WHERE id = ?", (team_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def create_team(
    *,
    name: str,
    battle_format: str,
    showdown_text: str,
    notes: str = "",
) -> dict:
    now = _now()
    nm = name.strip()
    bf = (battle_format or "").strip()
    txt = showdown_text or ""
    nt = notes or ""
    async with _db() as db:
        try:
            cur = await db.execute(
                """INSERT INTO teams (name, battle_format, showdown_text, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (nm, bf, txt, nt, now, now),
            )
            tid = cur.lastrowid
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper():
                raise ValueError("duplicate_team_name") from exc
            raise
        await db.commit()
    out = await get_team(int(tid))
    assert out is not None
    return out


async def update_team(
    team_id: int,
    *,
    name: str | None = None,
    battle_format: str | None = None,
    showdown_text: str | None = None,
    notes: str | None = None,
) -> dict | None:
    cur_team = await get_team(team_id)
    if not cur_team:
        return None
    now = _now()
    nm = cur_team["name"] if name is None else name.strip()
    bf = (
        cur_team["battle_format"]
        if battle_format is None
        else (battle_format or "").strip()
    )
    txt = cur_team["showdown_text"] if showdown_text is None else (showdown_text or "")
    nt = cur_team["notes"] if notes is None else (notes or "")
    async with _db() as db:
        try:
            await db.execute(
                """UPDATE teams SET name = ?, battle_format = ?, showdown_text = ?, notes = ?, updated_at = ?
                   WHERE id = ?""",
                (nm, bf, txt, nt, now, team_id),
            )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper():
                raise ValueError("duplicate_team_name") from exc
            raise
        await db.commit()
    return await get_team(team_id)


async def count_queued_running_matches_referencing_team(team_id: int) -> int:
    async with _db() as db:
        cur = await db.execute(
            """SELECT COUNT(*) FROM matches
               WHERE status IN ('queued', 'running')
               AND (player1_team_id = ? OR player2_team_id = ?)""",
            (team_id, team_id),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def delete_team(team_id: int) -> bool:
    n = await count_queued_running_matches_referencing_team(team_id)
    if n > 0:
        raise ValueError("team_in_active_matches")
    async with _db() as db:
        cur = await db.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_tournament_entry(entry_id: int) -> dict | None:
    async with _db() as db:
        cur = await db.execute(
            "SELECT * FROM tournament_entries WHERE id = ?", (int(entry_id),)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_tournament_entry_team(entry_id: int, team_id: int | None) -> bool:
    async with _db() as db:
        cur = await db.execute(
            "UPDATE tournament_entries SET team_id = ? WHERE id = ?",
            (team_id, entry_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_match(match_id: int) -> dict | None:
    async with _db() as db:
        cur = await db.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def enrich_match_row_with_series_tournament(m: dict) -> dict:
    """Attach tournament + bracket columns (for overlays / current_battle live JSON)."""
    out = dict(m)
    mid = m.get("id")
    if mid is None:
        return out
    async with _db() as db:
        cur = await db.execute(
            """SELECT t.name AS tournament_name, t.type AS tournament_type,
                      s.bracket AS series_bracket, s.round_number AS series_round_number,
                      s.match_position AS series_match_position,
                      wbmax.max_wr AS tournament_max_winners_round
               FROM matches m2
               LEFT JOIN tournaments t ON m2.tournament_id = t.id
               LEFT JOIN series s ON m2.series_id = s.id
               LEFT JOIN (
                 SELECT tournament_id, MAX(round_number) AS max_wr
                 FROM series
                 WHERE bracket = 'winners' AND tournament_id IS NOT NULL
                 GROUP BY tournament_id
               ) wbmax ON m2.tournament_id = wbmax.tournament_id
               WHERE m2.id = ?""",
            (mid,),
        )
        row = await cur.fetchone()
        if not row:
            return out
        for key, val in dict(row).items():
            if val is not None:
                out[key] = val
    return out


async def list_matches(
    *,
    status: str | None = None,
    series_id: int | None = None,
    tournament_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if series_id is not None:
        clauses.append("series_id = ?")
        params.append(series_id)
    if tournament_id is not None:
        clauses.append("tournament_id = ?")
        params.append(tournament_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.extend([limit, offset])
    async with _db() as db:
        cur = await db.execute(
            f"SELECT * FROM matches{where} ORDER BY queued_at DESC LIMIT ? OFFSET ?",
            params,
        )
        return [dict(r) for r in await cur.fetchall()]


async def count_matches(
    *,
    status: str | None = None,
    series_id: int | None = None,
    tournament_id: int | None = None,
) -> int:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if series_id is not None:
        clauses.append("series_id = ?")
        params.append(series_id)
    if tournament_id is not None:
        clauses.append("tournament_id = ?")
        params.append(tournament_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with _db() as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM matches{where}", params)
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def pop_next_queued_match() -> dict | None:
    """Atomically fetch and mark the next queued match as running."""
    async with _db() as db:
        cur = await db.execute(
            """SELECT id FROM matches
               WHERE status = 'queued'
               ORDER BY queued_at ASC, id ASC
               LIMIT 1"""
        )
        row = await cur.fetchone()
        if not row:
            return None
        mid = row["id"]
        now = _now()
        await db.execute(
            "UPDATE matches SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
            (now, mid),
        )
        # Also mark parent series as in_progress if still pending
        await db.execute(
            """UPDATE series SET status = 'in_progress', updated_at = ?
               WHERE id = (SELECT series_id FROM matches WHERE id = ?)
               AND status = 'pending'""",
            (now, mid),
        )
        # Also mark parent tournament as in_progress if still pending
        await db.execute(
            """UPDATE tournaments SET status = 'in_progress', updated_at = ?
               WHERE id = (SELECT tournament_id FROM matches WHERE id = ?)
               AND status = 'pending'""",
            (now, mid),
        )
        await db.commit()
    return await get_match(mid)


async def start_match(match_id: int) -> dict | None:
    async with _db() as db:
        now = _now()
        await db.execute(
            "UPDATE matches SET status = 'running', started_at = ? WHERE id = ?",
            (now, match_id),
        )
        await db.commit()
    return await get_match(match_id)


async def complete_match(
    match_id: int,
    *,
    winner: str,
    loser: str,
    winner_side: str,
    duration: float | None = None,
    replay_file: str | None = None,
    log_file: str | None = None,
    battle_tag: str | None = None,
) -> dict | None:
    async with _db() as db:
        await db.execute(
            """UPDATE matches SET
                 status = 'completed', winner = ?, loser = ?, winner_side = ?,
                 duration = ?, replay_file = ?, log_file = ?, battle_tag = ?,
                 completed_at = ?
               WHERE id = ?""",
            (
                winner,
                loser,
                winner_side,
                duration,
                replay_file,
                log_file,
                battle_tag,
                _now(),
                match_id,
            ),
        )
        await db.commit()
    return await get_match(match_id)


async def update_match_replay_artifacts(
    match_id: int,
    *,
    replay_file: str | None = None,
    log_file: str | None = None,
    battle_tag: str | None = None,
) -> dict | None:
    """Fill in replay/log paths after an early complete (match already ``completed``)."""
    async with _db() as db:
        await db.execute(
            """UPDATE matches SET
                 replay_file = COALESCE(?, replay_file),
                 log_file = COALESCE(?, log_file),
                 battle_tag = COALESCE(?, battle_tag)
               WHERE id = ? AND status = 'completed'""",
            (replay_file, log_file, battle_tag, match_id),
        )
        await db.commit()
    return await get_match(match_id)


async def fail_match(match_id: int, error_message: str) -> dict | None:
    async with _db() as db:
        await db.execute(
            "UPDATE matches SET status = 'error', error_message = ?, completed_at = ? WHERE id = ?",
            (error_message, _now(), match_id),
        )
        await db.commit()
    return await get_match(match_id)


async def abandon_series_after_failed_match(series_id: int) -> None:
    """Cancel a series left by a failed match and drop any still-queued games."""
    async with _db() as db:
        now = _now()
        await db.execute(
            """UPDATE series SET status = 'cancelled', updated_at = ?
               WHERE id = ? AND status IN ('pending', 'in_progress')""",
            (now, series_id),
        )
        await db.execute(
            "UPDATE matches SET status = 'cancelled' WHERE series_id = ? AND status = 'queued'",
            (series_id,),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Scoreboard / stats queries (replace results.json)
# ---------------------------------------------------------------------------

_VICTORY_TOURNEY_TIME_EPS = 20.0


async def _enrich_victory_context(db: aiosqlite.Connection, m: dict) -> dict:
    """Add victory_tournament_clinched / victory_series_clinched for splash labels."""
    out = dict(m)
    mid = m.get("id")
    sid = m.get("series_id")
    series_clinched = False
    if mid is not None and sid is not None:
        cur = await db.execute("SELECT status FROM series WHERE id = ?", (sid,))
        srow = await cur.fetchone()
        series_status = srow[0] if srow else None
        # Only the match that *decided* the series (parent row completed) may use
        # stage labels like "R1 Winner". Intermediate BO wins leave the series
        # in_progress; the latest completed game would wrongly qualify otherwise.
        if series_status == "completed":
            cur = await db.execute(
                """SELECT id FROM matches WHERE series_id = ? AND status = 'completed'
                   ORDER BY completed_at DESC, id DESC LIMIT 1""",
                (sid,),
            )
            row = await cur.fetchone()
            series_clinched = row is not None and int(row[0]) == int(mid)
    out["victory_series_clinched"] = series_clinched

    out["victory_de_pending_grand_finals_reset"] = False
    ts = out.get("tournament_status")
    if (
        series_clinched
        and m.get("tournament_id") is not None
        and out.get("tournament_type") == "double_elimination"
        and out.get("series_bracket") == "grand_finals"
        and ts != "completed"
    ):
        out["victory_de_pending_grand_finals_reset"] = True

    tournament_clinched = False
    tid = m.get("tournament_id")
    if series_clinched and tid is not None:
        cur = await db.execute(
            "SELECT status, updated_at FROM tournaments WHERE id = ?",
            (tid,),
        )
        row = await cur.fetchone()
        if row and row[0] == "completed":
            m_done = m.get("timestamp")
            t_updated = row[1]
            if m_done is not None and t_updated is not None:
                tournament_clinched = (
                    abs(float(t_updated) - float(m_done)) <= _VICTORY_TOURNEY_TIME_EPS
                )
            if not tournament_clinched:
                sb = m.get("series_bracket")
                if row[0] == "completed" and sb in (
                    "grand_finals",
                    "grand_finals_reset",
                ):
                    tournament_clinched = True
                elif m.get("tournament_type") == "single_elimination":
                    bracket = m.get("series_bracket")
                    rn = m.get("series_round_number")
                    mx = m.get("tournament_max_winners_round")
                    if bracket == "winners" and rn is not None and mx is not None:
                        try:
                            tournament_clinched = int(rn) == int(mx)
                        except (TypeError, ValueError):
                            pass
    out["victory_tournament_clinched"] = tournament_clinched
    return out


async def get_scoreboard_data(recent_count: int = 10) -> dict:
    """Return data shaped like the old results.json for the stream scoreboard."""
    async with _db() as db:
        total_cur = await db.execute(
            "SELECT COUNT(*) FROM matches WHERE status = 'completed'"
        )
        total_row = await total_cur.fetchone()
        total_matches = total_row[0] if total_row else 0

        wins_cur = await db.execute(
            """SELECT winner, COUNT(*) as cnt FROM matches
               WHERE status = 'completed' AND winner IS NOT NULL
               GROUP BY winner"""
        )
        wins = {r["winner"]: r["cnt"] for r in await wins_cur.fetchall()}

        recent_cur = await db.execute(
            """SELECT m.id, m.winner, m.loser, m.winner_side, m.completed_at AS timestamp,
                      m.battle_format, m.duration,
                      m.player1_persona, m.player2_persona,
                      m.series_id, m.tournament_id, m.game_number,
                      t.name AS tournament_name,
                      t.status AS tournament_status,
                      t.type AS tournament_type,
                      s.bracket AS series_bracket,
                      s.round_number AS series_round_number,
                      s.match_position AS series_match_position,
                      wbmax.max_wr AS tournament_max_winners_round,
                      CASE m.winner_side
                        WHEN 'p1' THEN m.winner
                        WHEN 'p2' THEN m.loser
                        ELSE m.winner
                      END AS player1_name,
                      CASE m.winner_side
                        WHEN 'p1' THEN m.loser
                        WHEN 'p2' THEN m.winner
                        ELSE m.loser
                      END AS player2_name
               FROM matches m
               LEFT JOIN tournaments t ON m.tournament_id = t.id
               LEFT JOIN series s ON m.series_id = s.id
               LEFT JOIN (
                 SELECT tournament_id, MAX(round_number) AS max_wr
                 FROM series
                 WHERE bracket = 'winners' AND tournament_id IS NOT NULL
                 GROUP BY tournament_id
               ) wbmax ON m.tournament_id = wbmax.tournament_id
               WHERE m.status = 'completed'
               ORDER BY m.completed_at DESC, m.id DESC LIMIT ?""",
            (recent_count,),
        )
        recent_matches = [dict(r) for r in await recent_cur.fetchall()]
        if recent_matches:
            recent_matches[0] = await _enrich_victory_context(db, recent_matches[0])

    return {
        "total_matches": total_matches,
        "wins": wins,
        "recent_matches": recent_matches,
        "last_match": recent_matches[0] if recent_matches else None,
    }


async def get_queue_depth() -> int:
    async with _db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM matches WHERE status = 'queued'")
        row = await cur.fetchone()
        return row[0] if row else 0


async def get_running_match() -> dict | None:
    async with _db() as db:
        cur = await db.execute(
            "SELECT * FROM matches WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_queued_matches(*, limit: int = 50, offset: int = 0) -> list[dict]:
    """Matches waiting for the worker, oldest first (next to run first)."""
    cap = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    async with _db() as db:
        cur = await db.execute(
            """SELECT m.*, t.name AS tournament_name,
                      t.type AS tournament_type,
                      s.bracket AS series_bracket,
                      s.round_number AS series_round_number,
                      s.match_position AS series_match_position,
                      s.best_of AS series_best_of,
                      wbmax.max_wr AS tournament_max_winners_round
               FROM matches m
               LEFT JOIN tournaments t ON m.tournament_id = t.id
               LEFT JOIN series s ON m.series_id = s.id
               LEFT JOIN (
                 SELECT tournament_id, MAX(round_number) AS max_wr
                 FROM series
                 WHERE bracket = 'winners' AND tournament_id IS NOT NULL
                 GROUP BY tournament_id
               ) wbmax ON m.tournament_id = wbmax.tournament_id
               WHERE m.status = 'queued'
               ORDER BY m.queued_at ASC LIMIT ? OFFSET ?""",
            (cap, off),
        )
        return [dict(r) for r in await cur.fetchall()]


async def list_tournament_series_pending_opponent(*, limit: int = 24) -> list[dict]:
    """
    Tournament series with exactly one player slotted and no queued/running games yet.

    Matches are only inserted once both sides are known; until then the bracket still
    has a known vs TBD pairing that clients (overlay ticker) may want to show.
    """
    cap = max(1, min(int(limit), 100))
    async with _db() as db:
        cur = await db.execute(
            """
            SELECT s.*, t.name AS tournament_name,
                   t.type AS tournament_type,
                   wbmax.max_wr AS tournament_max_winners_round
            FROM series s
            LEFT JOIN tournaments t ON s.tournament_id = t.id
            LEFT JOIN (
              SELECT tournament_id, MAX(round_number) AS max_wr
              FROM series
              WHERE bracket = 'winners' AND tournament_id IS NOT NULL
              GROUP BY tournament_id
            ) wbmax ON s.tournament_id = wbmax.tournament_id
            WHERE s.tournament_id IS NOT NULL
              AND s.status IN ('pending', 'in_progress')
              AND (
                (
                  IFNULL(TRIM(s.player1_provider), '') != ''
                  AND IFNULL(TRIM(s.player2_provider), '') = ''
                )
                OR (
                  IFNULL(TRIM(s.player2_provider), '') != ''
                  AND IFNULL(TRIM(s.player1_provider), '') = ''
                )
              )
              AND NOT EXISTS (
                SELECT 1 FROM matches m
                WHERE m.series_id = s.id AND m.status IN ('queued', 'running')
              )
            ORDER BY
              s.tournament_id,
              CASE s.bracket
                WHEN 'winners' THEN 0
                WHEN 'losers' THEN 1
                WHEN 'grand_finals' THEN 2
                WHEN 'grand_finals_reset' THEN 3
                ELSE 4
              END,
              IFNULL(s.round_number, 0),
              IFNULL(s.match_position, 0),
              s.id
            LIMIT ?
            """,
            (cap,),
        )
        return [dict(r) for r in await cur.fetchall()]


def _stats_battle_format_filter(
    battle_formats: list[str] | None,
) -> tuple[str, tuple[Any, ...], tuple[Any, ...]]:
    """
    Returns (SQL fragment, params for a single SELECT, params for UNION of two SELECTs).
    When battle_formats is empty/None, no filter (all completed matches).
    """
    if not battle_formats:
        return "", (), ()
    placeholders = ",".join("?" * len(battle_formats))
    frag = f" AND battle_format IN ({placeholders})"
    t = tuple(battle_formats)
    return frag, t, t + t


async def list_completed_battle_formats() -> list[str]:
    """Distinct battle formats that appear in at least one completed match."""
    async with _db() as db:
        cur = await db.execute(
            """
            SELECT DISTINCT battle_format FROM matches
            WHERE status = 'completed' AND battle_format != ''
            ORDER BY battle_format COLLATE NOCASE
            """
        )
        return [str(r[0]) for r in await cur.fetchall() if r[0]]


async def get_stats(battle_formats: list[str] | None = None) -> dict:
    """Aggregate stats for the analytics dashboard."""
    bf_frag, bf_one, bf_two = _stats_battle_format_filter(battle_formats)
    async with _db() as db:
        # Win rates by model (provider/model combo)
        model_cur = await db.execute(
            f"""SELECT
                 player1_provider || '/' || player1_model as model,
                 SUM(CASE WHEN winner_side = 'p1' THEN 1 ELSE 0 END) as wins,
                 SUM(CASE WHEN winner_side = 'p2' THEN 1 ELSE 0 END) as losses,
                 COUNT(*) as total
               FROM matches WHERE status = 'completed'{bf_frag}
               GROUP BY model
               UNION ALL
               SELECT
                 player2_provider || '/' || player2_model as model,
                 SUM(CASE WHEN winner_side = 'p2' THEN 1 ELSE 0 END) as wins,
                 SUM(CASE WHEN winner_side = 'p1' THEN 1 ELSE 0 END) as losses,
                 COUNT(*) as total
               FROM matches WHERE status = 'completed'{bf_frag}
               GROUP BY model""",
            bf_two,
        )
        model_rows = [dict(r) for r in await model_cur.fetchall()]
        # Merge duplicate model keys
        model_stats: dict[str, dict] = {}
        for r in model_rows:
            m = r["model"]
            if m not in model_stats:
                model_stats[m] = {"wins": 0, "losses": 0, "total": 0}
            model_stats[m]["wins"] += r["wins"]
            model_stats[m]["losses"] += r["losses"]
            model_stats[m]["total"] += r["total"]
        for v in model_stats.values():
            v["win_rate"] = round(v["wins"] / v["total"] * 100, 1) if v["total"] else 0
        model_stats = dict(
            sorted(
                model_stats.items(),
                key=lambda kv: (
                    -float(kv[1].get("win_rate") or 0),
                    -int(kv[1].get("total") or 0),
                    str(kv[0]),
                ),
            )
        )

        # Head-to-head matrix
        h2h_cur = await db.execute(
            f"""SELECT
                 player1_provider || '/' || player1_model as model1,
                 player2_provider || '/' || player2_model as model2,
                 SUM(CASE WHEN winner_side = 'p1' THEN 1 ELSE 0 END) as model1_wins,
                 SUM(CASE WHEN winner_side = 'p2' THEN 1 ELSE 0 END) as model2_wins,
                 COUNT(*) as total
               FROM matches WHERE status = 'completed'{bf_frag}
               GROUP BY model1, model2
               ORDER BY total DESC""",
            bf_one,
        )
        head_to_head = [dict(r) for r in await h2h_cur.fetchall()]

        h2h_persona_cur = await db.execute(
            f"""SELECT
                 player1_persona as persona1,
                 player2_persona as persona2,
                 SUM(CASE WHEN winner_side = 'p1' THEN 1 ELSE 0 END) as persona1_wins,
                 SUM(CASE WHEN winner_side = 'p2' THEN 1 ELSE 0 END) as persona2_wins,
                 COUNT(*) as total
               FROM matches WHERE status = 'completed'{bf_frag}
               GROUP BY player1_persona, player2_persona
               ORDER BY total DESC""",
            bf_one,
        )
        head_to_head_personas = [dict(r) for r in await h2h_persona_cur.fetchall()]

        h2h_mp_cur = await db.execute(
            f"""SELECT
                 player1_provider || '/' || player1_model as model1,
                 player1_persona as persona1,
                 player2_provider || '/' || player2_model as model2,
                 player2_persona as persona2,
                 SUM(CASE WHEN winner_side = 'p1' THEN 1 ELSE 0 END) as model1_wins,
                 SUM(CASE WHEN winner_side = 'p2' THEN 1 ELSE 0 END) as model2_wins,
                 COUNT(*) as total
               FROM matches WHERE status = 'completed'{bf_frag}
               GROUP BY
                 player1_provider, player1_model, player1_persona,
                 player2_provider, player2_model, player2_persona
               ORDER BY total DESC""",
            bf_one,
        )
        head_to_head_model_persona = [dict(r) for r in await h2h_mp_cur.fetchall()]

        # Win rates by persona
        persona_cur = await db.execute(
            f"""SELECT
                 player1_persona as persona,
                 SUM(CASE WHEN winner_side = 'p1' THEN 1 ELSE 0 END) as wins,
                 COUNT(*) as total
               FROM matches WHERE status = 'completed'{bf_frag}
               GROUP BY persona
               UNION ALL
               SELECT
                 player2_persona as persona,
                 SUM(CASE WHEN winner_side = 'p2' THEN 1 ELSE 0 END) as wins,
                 COUNT(*) as total
               FROM matches WHERE status = 'completed'{bf_frag}
               GROUP BY persona""",
            bf_two,
        )
        persona_rows = [dict(r) for r in await persona_cur.fetchall()]
        persona_stats: dict[str, dict] = {}
        for r in persona_rows:
            p = r["persona"]
            if p not in persona_stats:
                persona_stats[p] = {"wins": 0, "total": 0}
            persona_stats[p]["wins"] += r["wins"]
            persona_stats[p]["total"] += r["total"]
        for v in persona_stats.values():
            v["losses"] = v["total"] - v["wins"]
            v["win_rate"] = round(v["wins"] / v["total"] * 100, 1) if v["total"] else 0
        persona_stats = dict(
            sorted(
                persona_stats.items(),
                key=lambda kv: (
                    -float(kv[1].get("win_rate") or 0),
                    -int(kv[1].get("total") or 0),
                    str(kv[0]),
                ),
            )
        )

    return {
        "model_stats": model_stats,
        "head_to_head": head_to_head,
        "head_to_head_personas": head_to_head_personas,
        "head_to_head_model_persona": head_to_head_model_persona,
        "persona_stats": persona_stats,
    }
