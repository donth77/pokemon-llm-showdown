"""
SQLite database layer for the tournament manager.

Provides schema creation, async CRUD helpers, and query utilities backed by
aiosqlite.  The DB file lives on the manager-data volume so it persists across
container restarts.
"""

from __future__ import annotations

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
    bracket         TEXT    CHECK (bracket IN ('winners', 'losers', 'grand_finals', NULL)),
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
    await _migrate_tournament_columns()


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
    """Mutate tournament payload: entry persona_display_slug + series player*_persona_display."""
    entries = t.get("entries") or []
    if not entries:
        for s in t.get("series") or []:
            s["player1_persona_display"] = s.get("player1_persona")
            s["player2_persona_display"] = s.get("player2_persona")
        return
    emap = entry_rows_to_display_slug_map(entries)
    for e in entries:
        e["persona_display_slug"] = emap[int(e["id"])]
    for s in t.get("series") or []:
        p1e, p2e = s.get("player1_entry_id"), s.get("player2_entry_id")
        s["player1_persona_display"] = (
            emap.get(int(p1e), s.get("player1_persona")) if p1e is not None else s.get("player1_persona")
        )
        s["player2_persona_display"] = (
            emap.get(int(p2e), s.get("player2_persona")) if p2e is not None else s.get("player2_persona")
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
            await db.execute(
                """INSERT INTO tournament_entries
                   (tournament_id, provider, model, persona_slug, seed, display_name)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (tid, e["provider"], e["model"], e["persona_slug"], e.get("seed", i + 1), e.get("display_name", "")),
            )
        await db.commit()
    return await get_tournament(tid)  # type: ignore[arg-type]


async def get_tournament(tournament_id: int) -> dict | None:
    async with _db() as db:
        cur = await db.execute("SELECT * FROM tournaments WHERE id = ?", (tournament_id,))
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


def _winner_entry_from_series(series_completed: list[dict], ttype: str) -> tuple[int | None, bool]:
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


async def list_tournaments(*, status: str | None = None) -> list[dict]:
    async with _db() as db:
        if status:
            cur = await db.execute(
                "SELECT * FROM tournaments WHERE status = ? ORDER BY created_at DESC",
                (status,),
            )
        else:
            cur = await db.execute("SELECT * FROM tournaments ORDER BY created_at DESC")
        tournaments = [dict(r) for r in await cur.fetchall()]
        await _attach_tournament_winner_fields(db, tournaments)
        return tournaments


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
                tournament_id, best_of, battle_format, round_number,
                match_position, bracket,
                player1_provider, player1_model, player1_persona, player1_entry_id,
                player2_provider, player2_model, player2_persona, player2_entry_id,
                now, now,
            ),
        )
        sid = cur.lastrowid

        if auto_queue and all([player1_provider, player2_provider]):
            for g in range(1, best_of + 1):
                await db.execute(
                    """INSERT INTO matches
                       (series_id, tournament_id, game_number, status, battle_format,
                        player1_provider, player1_model, player1_persona,
                        player2_provider, player2_model, player2_persona,
                        queued_at)
                       VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        sid, tournament_id, g, battle_format,
                        player1_provider, player1_model, player1_persona,
                        player2_provider, player2_model, player2_persona,
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


async def list_series_results(*, limit: int = 100) -> list[dict]:
    """Completed or cancelled series for the manager results page (newest first)."""
    cap = max(1, min(int(limit), 500))
    async with _db() as db:
        cur = await db.execute(
            """SELECT s.*, t.name AS tournament_name
               FROM series s
               LEFT JOIN tournaments t ON s.tournament_id = t.id
               WHERE s.status IN ('completed', 'cancelled')
               ORDER BY s.updated_at DESC
               LIMIT ?""",
            (cap,),
        )
        return [dict(r) for r in await cur.fetchall()]


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


async def complete_series(series_id: int, winner_side: str, winner_entry_id: int | None = None) -> None:
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
) -> dict:
    now = _now()
    async with _db() as db:
        cur = await db.execute(
            """INSERT INTO matches
               (series_id, tournament_id, game_number, status, battle_format,
                player1_provider, player1_model, player1_persona,
                player2_provider, player2_model, player2_persona,
                queued_at)
               VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                series_id, tournament_id, game_number, battle_format,
                player1_provider, player1_model, player1_persona,
                player2_provider, player2_model, player2_persona,
                now,
            ),
        )
        mid = cur.lastrowid
        await db.commit()
    return await get_match(mid)  # type: ignore[return-value]


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


async def pop_next_queued_match() -> dict | None:
    """Atomically fetch and mark the next queued match as running."""
    async with _db() as db:
        cur = await db.execute(
            """SELECT id FROM matches
               WHERE status = 'queued'
               ORDER BY queued_at ASC
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
            (winner, loser, winner_side, duration, replay_file, log_file, battle_tag, _now(), match_id),
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
        cur = await db.execute(
            """SELECT id FROM matches WHERE series_id = ? AND status = 'completed'
               ORDER BY completed_at DESC, id DESC LIMIT 1""",
            (sid,),
        )
        row = await cur.fetchone()
        series_clinched = row is not None and int(row[0]) == int(mid)
    out["victory_series_clinched"] = series_clinched

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
                if m.get("series_bracket") == "grand_finals":
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
                      m.series_id, m.tournament_id, m.game_number,
                      t.name AS tournament_name,
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
               ORDER BY m.completed_at DESC LIMIT ?""",
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


async def list_queued_matches(*, limit: int = 50) -> list[dict]:
    """Matches waiting for the worker, oldest first (next to run first)."""
    cap = max(1, min(int(limit), 200))
    async with _db() as db:
        cur = await db.execute(
            """SELECT m.*, t.name AS tournament_name,
                      t.type AS tournament_type,
                      s.bracket AS series_bracket,
                      s.round_number AS series_round_number,
                      s.match_position AS series_match_position,
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
               ORDER BY m.queued_at ASC LIMIT ?""",
            (cap,),
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
                ELSE 3
              END,
              IFNULL(s.round_number, 0),
              IFNULL(s.match_position, 0),
              s.id
            LIMIT ?
            """,
            (cap,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_stats() -> dict:
    """Aggregate stats for the analytics dashboard."""
    async with _db() as db:
        # Win rates by model (provider/model combo)
        model_cur = await db.execute(
            """SELECT
                 player1_provider || '/' || player1_model as model,
                 SUM(CASE WHEN winner_side = 'p1' THEN 1 ELSE 0 END) as wins,
                 SUM(CASE WHEN winner_side = 'p2' THEN 1 ELSE 0 END) as losses,
                 COUNT(*) as total
               FROM matches WHERE status = 'completed'
               GROUP BY model
               UNION ALL
               SELECT
                 player2_provider || '/' || player2_model as model,
                 SUM(CASE WHEN winner_side = 'p2' THEN 1 ELSE 0 END) as wins,
                 SUM(CASE WHEN winner_side = 'p1' THEN 1 ELSE 0 END) as losses,
                 COUNT(*) as total
               FROM matches WHERE status = 'completed'
               GROUP BY model"""
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
            """SELECT
                 player1_provider || '/' || player1_model as model1,
                 player2_provider || '/' || player2_model as model2,
                 SUM(CASE WHEN winner_side = 'p1' THEN 1 ELSE 0 END) as model1_wins,
                 SUM(CASE WHEN winner_side = 'p2' THEN 1 ELSE 0 END) as model2_wins,
                 COUNT(*) as total
               FROM matches WHERE status = 'completed'
               GROUP BY model1, model2
               ORDER BY total DESC"""
        )
        head_to_head = [dict(r) for r in await h2h_cur.fetchall()]

        h2h_persona_cur = await db.execute(
            """SELECT
                 player1_persona as persona1,
                 player2_persona as persona2,
                 SUM(CASE WHEN winner_side = 'p1' THEN 1 ELSE 0 END) as persona1_wins,
                 SUM(CASE WHEN winner_side = 'p2' THEN 1 ELSE 0 END) as persona2_wins,
                 COUNT(*) as total
               FROM matches WHERE status = 'completed'
               GROUP BY player1_persona, player2_persona
               ORDER BY total DESC"""
        )
        head_to_head_personas = [dict(r) for r in await h2h_persona_cur.fetchall()]

        h2h_mp_cur = await db.execute(
            """SELECT
                 player1_provider || '/' || player1_model as model1,
                 player1_persona as persona1,
                 player2_provider || '/' || player2_model as model2,
                 player2_persona as persona2,
                 SUM(CASE WHEN winner_side = 'p1' THEN 1 ELSE 0 END) as model1_wins,
                 SUM(CASE WHEN winner_side = 'p2' THEN 1 ELSE 0 END) as model2_wins,
                 COUNT(*) as total
               FROM matches WHERE status = 'completed'
               GROUP BY
                 player1_provider, player1_model, player1_persona,
                 player2_provider, player2_model, player2_persona
               ORDER BY total DESC"""
        )
        head_to_head_model_persona = [dict(r) for r in await h2h_mp_cur.fetchall()]

        # Win rates by persona
        persona_cur = await db.execute(
            """SELECT
                 player1_persona as persona,
                 SUM(CASE WHEN winner_side = 'p1' THEN 1 ELSE 0 END) as wins,
                 COUNT(*) as total
               FROM matches WHERE status = 'completed'
               GROUP BY persona
               UNION ALL
               SELECT
                 player2_persona as persona,
                 SUM(CASE WHEN winner_side = 'p2' THEN 1 ELSE 0 END) as wins,
                 COUNT(*) as total
               FROM matches WHERE status = 'completed'
               GROUP BY persona"""
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
