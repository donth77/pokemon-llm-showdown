"""
Tournament bracket generation and progression logic.

Supports round-robin, single elimination, and double elimination formats.
All functions operate through the db module — they read entries/series/matches
and create new series or update statuses as results come in.

Double elimination: winners feed winners + losers bracket; losers bracket feeds
grand finals (slot 2) alongside the winners-bracket champion (slot 1). WB→LB
routing uses pairing rules for common sizes (4/8/16) with fallbacks; very large
brackets may need further tuning.
"""

from __future__ import annotations

import math
from itertools import combinations

from . import db


async def generate_bracket(tournament: dict) -> None:
    """
    Generate all initial series for a tournament based on its type.
    Called once after tournament creation.  Sets tournament to 'in_progress'.
    """
    t_type = tournament["type"]
    if t_type == "round_robin":
        await _generate_round_robin(tournament)
    elif t_type == "single_elimination":
        await _generate_single_elimination(tournament)
    elif t_type == "double_elimination":
        await _generate_double_elimination(tournament)
    else:
        raise ValueError(f"Unknown tournament type: {t_type}")

    await db.update_tournament_status(tournament["id"], "in_progress")


async def on_match_failed(match: dict | None) -> None:
    """
    Called when a queued match ends in error (worker reported /matches/{id}/error).

    Cancels the parent series so the bracket does not wait forever on a dead game.
    Round-robin tournaments treat cancelled series like finished slots and may mark
    the tournament complete. Elimination brackets may still need a manual
    tournament cancel if the bracket relied on this series.
    """
    if not match:
        return
    series_id = match.get("series_id")
    if not series_id:
        return
    await db.abandon_series_after_failed_match(series_id)
    tournament_id = match.get("tournament_id")
    if not tournament_id:
        return
    t = await db.get_tournament(tournament_id)
    if not t:
        return
    if t["type"] == "round_robin":
        await _check_round_robin_complete(t)


async def on_match_completed(match: dict) -> None:
    """
    Called when a match finishes.  Updates series win counts and triggers
    bracket advancement when a series is decided.
    """
    series_id = match.get("series_id")
    if not series_id:
        return

    winner_side = match["winner_side"]
    if not winner_side:
        return

    series = await db.update_series_wins(series_id, winner_side)
    if not series:
        return

    wins_needed = math.ceil(series["best_of"] / 2)
    p1w = series["player1_wins"]
    p2w = series["player2_wins"]

    if p1w >= wins_needed or p2w >= wins_needed:
        decided_side = "p1" if p1w >= wins_needed else "p2"
        winner_entry = (
            series["player1_entry_id"] if decided_side == "p1"
            else series["player2_entry_id"]
        )
        await db.complete_series(series_id, decided_side, winner_entry)

        tournament_id = series.get("tournament_id")
        if tournament_id:
            # Must reload: ``series`` is stale (no winner_side / winner_entry_id)
            # until complete_series runs; advancement needs those for slotting.
            completed_row = await db.get_series(series_id)
            if completed_row:
                await _advance_bracket(tournament_id, completed_row)


# ---------------------------------------------------------------------------
# Round-Robin
# ---------------------------------------------------------------------------

async def _generate_round_robin(tournament: dict) -> None:
    tid = tournament["id"]
    entries = tournament["entries"]
    fmt = tournament["battle_format"]
    best_of = tournament["best_of"]

    for pos, (a, b) in enumerate(combinations(entries, 2), start=1):
        await db.create_series(
            tournament_id=tid,
            best_of=best_of,
            battle_format=fmt,
            round_number=1,
            match_position=pos,
            player1_provider=a["provider"],
            player1_model=a["model"],
            player1_persona=a["persona_slug"],
            player1_entry_id=a["id"],
            player2_provider=b["provider"],
            player2_model=b["model"],
            player2_persona=b["persona_slug"],
            player2_entry_id=b["id"],
        )


# ---------------------------------------------------------------------------
# Single Elimination
# ---------------------------------------------------------------------------

def _next_power_of_two(n: int) -> int:
    return 1 << (n - 1).bit_length()


async def _generate_single_elimination(tournament: dict) -> None:
    tid = tournament["id"]
    entries = sorted(tournament["entries"], key=lambda e: e["seed"])
    fmt = tournament["battle_format"]
    best_of = tournament["best_of"]
    n = len(entries)

    bracket_size = _next_power_of_two(n)
    num_byes = bracket_size - n
    total_rounds = int(math.log2(bracket_size))

    # Seed into bracket slots (standard 1v16, 8v9, etc. seeding)
    slots: list[dict | None] = [None] * bracket_size
    seed_order = _standard_seed_order(bracket_size)
    for i, slot_idx in enumerate(seed_order):
        if i < n:
            slots[slot_idx] = entries[i]

    # Generate first round
    pos = 0
    for i in range(0, bracket_size, 2):
        pos += 1
        a = slots[i]
        b = slots[i + 1]

        if a and b:
            await db.create_series(
                tournament_id=tid,
                best_of=best_of,
                battle_format=fmt,
                round_number=1,
                match_position=pos,
                bracket="winners",
                player1_provider=a["provider"],
                player1_model=a["model"],
                player1_persona=a["persona_slug"],
                player1_entry_id=a["id"],
                player2_provider=b["provider"],
                player2_model=b["model"],
                player2_persona=b["persona_slug"],
                player2_entry_id=b["id"],
            )
        elif a or b:
            # Bye — create already-completed series for the present player
            present = a or b
            side = "p1" if a else "p2"
            s = await db.create_series(
                tournament_id=tid,
                best_of=1,
                battle_format=fmt,
                round_number=1,
                match_position=pos,
                bracket="winners",
                player1_provider=present["provider"] if a else None,
                player1_model=present["model"] if a else None,
                player1_persona=present["persona_slug"] if a else None,
                player1_entry_id=present["id"] if a else None,
                player2_provider=present["provider"] if b else None,
                player2_model=present["model"] if b else None,
                player2_persona=present["persona_slug"] if b else None,
                player2_entry_id=present["id"] if b else None,
                auto_queue=False,
            )
            await db.complete_series(s["id"], side, present["id"])

    # Create placeholder series for later rounds
    for rnd in range(2, total_rounds + 1):
        matches_in_round = bracket_size // (2 ** rnd)
        for p in range(1, matches_in_round + 1):
            await db.create_series(
                tournament_id=tid,
                best_of=best_of,
                battle_format=fmt,
                round_number=rnd,
                match_position=p,
                bracket="winners",
                auto_queue=False,
            )

    # Process byes — advance winners immediately
    t = await db.get_tournament(tid)
    if t:
        for s in t["series"]:
            if s["status"] == "completed" and s["round_number"] == 1:
                await _advance_single_elim(tid, s)


def _standard_seed_order(size: int) -> list[int]:
    """Return slot indices for standard tournament seeding."""
    if size == 1:
        return [0]
    half = _standard_seed_order(size // 2)
    return [2 * x for x in half] + [size - 1 - 2 * x for x in half]


def _entry_for_id(tournament: dict, entry_id: int | None) -> dict | None:
    if not entry_id:
        return None
    for e in tournament["entries"]:
        if e["id"] == entry_id:
            return e
    return None


def _loser_entry_from_series(tournament: dict, series: dict) -> dict | None:
    ws = series.get("winner_side")
    if ws == "p1":
        eid = series.get("player2_entry_id")
    elif ws == "p2":
        eid = series.get("player1_entry_id")
    else:
        return None
    return _entry_for_id(tournament, eid)


def _find_series(
    tournament: dict, *, bracket: str, round_number: int, match_position: int
) -> dict | None:
    for s in tournament["series"]:
        if (
            s.get("bracket") == bracket
            and s.get("round_number") == round_number
            and s.get("match_position") == match_position
        ):
            return s
    return None


def _find_first_open_in_losers_round(tournament: dict, round_number: int) -> dict | None:
    """First pending/in_progress losers series in this round missing a player."""
    for s in tournament["series"]:
        if s.get("bracket") != "losers" or s.get("round_number") != round_number:
            continue
        if s.get("status") not in ("pending", "in_progress"):
            continue
        if not s.get("player1_provider") or not s.get("player2_provider"):
            return s
    return None


async def _queue_series_matches(tournament_id: int, series_id: int) -> None:
    refreshed = await db.get_series(series_id)
    if not refreshed or not refreshed.get("player1_provider") or not refreshed.get(
        "player2_provider"
    ):
        return
    existing = refreshed.get("matches") or []
    if any(m.get("status") in ("queued", "running") for m in existing):
        return
    async with db._db() as conn:
        now = db._now()
        for g in range(1, refreshed["best_of"] + 1):
            await conn.execute(
                """INSERT INTO matches
                   (series_id, tournament_id, game_number, status, battle_format,
                    player1_provider, player1_model, player1_persona,
                    player2_provider, player2_model, player2_persona,
                    queued_at)
                   VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    series_id,
                    tournament_id,
                    g,
                    refreshed["battle_format"],
                    refreshed["player1_provider"],
                    refreshed["player1_model"],
                    refreshed["player1_persona"],
                    refreshed["player2_provider"],
                    refreshed["player2_model"],
                    refreshed["player2_persona"],
                    now,
                ),
            )
        await conn.commit()


async def _slot_entry_into_series_side(
    tournament_id: int, series_id: int, entry: dict, prefer_player1: bool | None = None
) -> None:
    """Fill first empty player slot (or prefer_player1 side if still empty)."""
    s = await db.get_series(series_id)
    if not s or not entry:
        return
    p1_empty = not s.get("player1_provider")
    p2_empty = not s.get("player2_provider")
    if not p1_empty and not p2_empty:
        return
    if prefer_player1 is True and p1_empty:
        side = "player1"
    elif prefer_player1 is False and p2_empty:
        side = "player2"
    elif p1_empty:
        side = "player1"
    else:
        side = "player2"
    updates = {
        f"{side}_provider": entry["provider"],
        f"{side}_model": entry["model"],
        f"{side}_persona": entry["persona_slug"],
        f"{side}_entry_id": entry["id"],
    }
    async with db._db() as conn:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [db._now(), series_id]
        await conn.execute(
            f"UPDATE series SET {set_clause}, updated_at = ? WHERE id = ?",
            params,
        )
        await conn.commit()
    await _queue_series_matches(tournament_id, series_id)


async def _slot_winner_into_next_winners_round(
    tournament_id: int, completed_series: dict, tournament: dict
) -> bool:
    """
    Move WB winner into the next winners series. Returns False if this was the
    winners-bracket final (no next winners slot).
    """
    rnd = completed_series["round_number"]
    pos = completed_series["match_position"]
    winner_entry_id = completed_series["winner_entry_id"]

    next_round = rnd + 1
    next_pos = (pos + 1) // 2
    is_p1 = pos % 2 == 1

    target = _find_series(
        tournament, bracket="winners", round_number=next_round, match_position=next_pos
    )

    if not target:
        return False

    entry = _entry_for_id(tournament, winner_entry_id)
    if not entry:
        return True

    side_prefix = "player1" if is_p1 else "player2"
    updates = {
        f"{side_prefix}_provider": entry["provider"],
        f"{side_prefix}_model": entry["model"],
        f"{side_prefix}_persona": entry["persona_slug"],
        f"{side_prefix}_entry_id": entry["id"],
    }

    async with db._db() as conn:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [db._now(), target["id"]]
        await conn.execute(
            f"UPDATE series SET {set_clause}, updated_at = ? WHERE id = ?",
            params,
        )
        await conn.commit()

    await _queue_series_matches(tournament_id, target["id"])
    return True


async def _advance_single_elim(tournament_id: int, completed_series: dict) -> None:
    """Move the winner of a single-elimination series to the next round."""
    t = await db.get_tournament(tournament_id)
    if not t:
        return

    slotted = await _slot_winner_into_next_winners_round(
        tournament_id, completed_series, t
    )
    if not slotted:
        await db.update_tournament_status(tournament_id, "completed")


def _de_wb_loser_lb_cell(
    bracket_size: int, wb_round: int, wb_pos: int, wb_rounds: int
) -> tuple[int, int] | None:
    """
    Losers bracket (round, position) for a loser exiting winners, except the
    WB finals loser (handled separately). None means caller should use fallback.
    """
    if wb_round >= wb_rounds:
        return None
    if wb_round == 1:
        return (1, (wb_pos + 1) // 2)
    if bracket_size == 8 and wb_round == 2:
        # One LB slot absorbs both WB semi losers (see generated LB count for R3).
        return (3, 1)
    return (min(2 * wb_round - 1, 2 * (wb_rounds - 1)), (wb_pos + 1) // 2)


async def _de_drop_wb_loser(
    tournament_id: int, t: dict, completed_series: dict, bracket_size: int, wb_rounds: int
) -> None:
    if completed_series.get("bracket") != "winners":
        return
    wb_r = completed_series["round_number"] or 0
    wb_p = completed_series["match_position"] or 0
    if wb_r == wb_rounds:
        return

    loser = _loser_entry_from_series(t, completed_series)
    if not loser:
        return

    cell = _de_wb_loser_lb_cell(bracket_size, wb_r, wb_p, wb_rounds)
    target = None
    if cell:
        lr, lp = cell
        target = _find_series(t, bracket="losers", round_number=lr, match_position=lp)
    if not target:
        t2 = await db.get_tournament(tournament_id)
        if not t2:
            return
        lr_guess = 2 * wb_r - 1 if wb_r > 1 else 1
        target = _find_first_open_in_losers_round(t2, lr_guess)
        if not target:
            for s in t2["series"]:
                if (
                    s.get("bracket") == "losers"
                    and s.get("status") in ("pending", "in_progress")
                    and (not s.get("player1_provider") or not s.get("player2_provider"))
                ):
                    target = s
                    break
        t = t2

    if not target:
        return

    await _slot_entry_into_series_side(tournament_id, target["id"], loser)


async def _de_wb_finals_to_grand_finals(
    tournament_id: int, t: dict, completed_series: dict, bracket_size: int, wb_rounds: int
) -> None:
    """WB finals: winners-bracket champion to grand finals p1; loser toward last LB or GF (2p)."""
    winner_entry = _entry_for_id(t, completed_series.get("winner_entry_id"))
    loser = _loser_entry_from_series(t, completed_series)
    lb_rounds = 2 * (wb_rounds - 1)

    t_cur = await db.get_tournament(tournament_id) or t
    gf = _find_series(t_cur, bracket="grand_finals", round_number=1, match_position=1)

    if gf and winner_entry:
        await _slot_entry_into_series_side(
            tournament_id, gf["id"], winner_entry, prefer_player1=True
        )

    t_cur = await db.get_tournament(tournament_id) or t
    gf = _find_series(t_cur, bracket="grand_finals", round_number=1, match_position=1)

    if lb_rounds <= 0:
        if gf and loser:
            await _slot_entry_into_series_side(
                tournament_id, gf["id"], loser, prefer_player1=False
            )
        if gf:
            await _queue_series_matches(tournament_id, gf["id"])
        return

    if loser:
        dest_r = lb_rounds if bracket_size <= 4 else max(1, lb_rounds - 1)
        target = _find_series(
            t_cur, bracket="losers", round_number=dest_r, match_position=1
        )
        if not target:
            tf = await db.get_tournament(tournament_id)
            if tf:
                target = _find_first_open_in_losers_round(tf, dest_r)
        if target:
            await _slot_entry_into_series_side(
                tournament_id, target["id"], loser, prefer_player1=False
            )

    t_cur = await db.get_tournament(tournament_id) or t
    gf = _find_series(t_cur, bracket="grand_finals", round_number=1, match_position=1)
    if gf:
        await _queue_series_matches(tournament_id, gf["id"])


async def _advance_de_winners(tournament_id: int, completed_series: dict) -> None:
    t = await db.get_tournament(tournament_id)
    if not t:
        return

    n = len(t["entries"])
    bracket_size = _next_power_of_two(n)
    wb_rounds = int(math.log2(bracket_size)) if bracket_size >= 2 else 0

    slotted = await _slot_winner_into_next_winners_round(
        tournament_id, completed_series, t
    )
    t = await db.get_tournament(tournament_id) or t
    await _de_drop_wb_loser(tournament_id, t, completed_series, bracket_size, wb_rounds)

    if not slotted:
        t = await db.get_tournament(tournament_id) or t
        await _de_wb_finals_to_grand_finals(
            tournament_id, t, completed_series, bracket_size, wb_rounds
        )

    t = await db.get_tournament(tournament_id) or t
    gf = _find_series(t, bracket="grand_finals", round_number=1, match_position=1)
    if gf:
        await _queue_series_matches(tournament_id, gf["id"])


async def _advance_de_losers_bracket(tournament_id: int, completed_series: dict) -> None:
    rnd = completed_series["round_number"]
    pos = completed_series["match_position"]
    winner_entry_id = completed_series["winner_entry_id"]

    next_round = rnd + 1
    next_pos = (pos + 1) // 2

    t = await db.get_tournament(tournament_id)
    if not t:
        return

    target = _find_series(
        t, bracket="losers", round_number=next_round, match_position=next_pos
    )
    entry = _entry_for_id(t, winner_entry_id)
    if not entry:
        return

    if target:
        prefer_p1 = pos % 2 == 1
        await _slot_entry_into_series_side(
            tournament_id, target["id"], entry, prefer_player1=prefer_p1
        )
        return

    gf = _find_series(t, bracket="grand_finals", round_number=1, match_position=1)
    if gf:
        await _slot_entry_into_series_side(
            tournament_id, gf["id"], entry, prefer_player1=False
        )
        await _queue_series_matches(tournament_id, gf["id"])


# ---------------------------------------------------------------------------
# Double Elimination
# ---------------------------------------------------------------------------

async def _generate_double_elimination(tournament: dict) -> None:
    """
    Generate the winners bracket first round and placeholder series for all
    subsequent rounds (winners bracket, losers bracket, grand finals).
    """
    tid = tournament["id"]
    entries = sorted(tournament["entries"], key=lambda e: e["seed"])
    fmt = tournament["battle_format"]
    best_of = tournament["best_of"]
    n = len(entries)

    bracket_size = _next_power_of_two(n)
    wb_rounds = int(math.log2(bracket_size))

    # --- Winners bracket first round (same as single elim) ---
    slots: list[dict | None] = [None] * bracket_size
    seed_order = _standard_seed_order(bracket_size)
    for i, slot_idx in enumerate(seed_order):
        if i < n:
            slots[slot_idx] = entries[i]

    pos = 0
    for i in range(0, bracket_size, 2):
        pos += 1
        a = slots[i]
        b = slots[i + 1]
        if a and b:
            await db.create_series(
                tournament_id=tid, best_of=best_of, battle_format=fmt,
                round_number=1, match_position=pos, bracket="winners",
                player1_provider=a["provider"], player1_model=a["model"],
                player1_persona=a["persona_slug"], player1_entry_id=a["id"],
                player2_provider=b["provider"], player2_model=b["model"],
                player2_persona=b["persona_slug"], player2_entry_id=b["id"],
            )
        elif a or b:
            present = a or b
            side = "p1" if a else "p2"
            s = await db.create_series(
                tournament_id=tid, best_of=1, battle_format=fmt,
                round_number=1, match_position=pos, bracket="winners",
                player1_provider=present["provider"] if a else None,
                player1_model=present["model"] if a else None,
                player1_persona=present["persona_slug"] if a else None,
                player1_entry_id=present["id"] if a else None,
                player2_provider=present["provider"] if b else None,
                player2_model=present["model"] if b else None,
                player2_persona=present["persona_slug"] if b else None,
                player2_entry_id=present["id"] if b else None,
                auto_queue=False,
            )
            await db.complete_series(s["id"], side, present["id"])

    # Winners bracket remaining rounds (placeholders)
    for rnd in range(2, wb_rounds + 1):
        matches_in_round = bracket_size // (2 ** rnd)
        for p in range(1, matches_in_round + 1):
            await db.create_series(
                tournament_id=tid, best_of=best_of, battle_format=fmt,
                round_number=rnd, match_position=p, bracket="winners",
                auto_queue=False,
            )

    # Losers bracket rounds: 2 * (wb_rounds - 1) rounds
    lb_rounds = 2 * (wb_rounds - 1)
    for rnd in range(1, lb_rounds + 1):
        # Losers bracket shrinks every other round
        if rnd == 1:
            matches_in_round = bracket_size // 4
        elif rnd % 2 == 0:
            matches_in_round = _lb_matches_in_round(bracket_size, rnd)
        else:
            matches_in_round = _lb_matches_in_round(bracket_size, rnd)
        matches_in_round = max(1, matches_in_round)
        for p in range(1, matches_in_round + 1):
            await db.create_series(
                tournament_id=tid, best_of=best_of, battle_format=fmt,
                round_number=rnd, match_position=p, bracket="losers",
                auto_queue=False,
            )

    # Grand finals (placeholder)
    await db.create_series(
        tournament_id=tid, best_of=best_of, battle_format=fmt,
        round_number=1, match_position=1, bracket="grand_finals",
        auto_queue=False,
    )

    # Process byes
    t = await db.get_tournament(tid)
    if t:
        for s in t["series"]:
            if s["status"] == "completed" and s["round_number"] == 1 and s["bracket"] == "winners":
                await _advance_double_elim(tid, s)


def _lb_matches_in_round(bracket_size: int, lb_round: int) -> int:
    """Calculate number of matches in a losers bracket round."""
    wb_first_round = bracket_size // 2
    # Every two LB rounds halves the field
    return max(1, wb_first_round // (2 ** ((lb_round + 1) // 2)))


async def _advance_double_elim(tournament_id: int, completed_series: dict) -> None:
    """Advance DE: WB (slot + loser drop + finals feed), LB chain, GF completes event."""
    bracket = completed_series.get("bracket")

    if bracket == "winners":
        await _advance_de_winners(tournament_id, completed_series)
    elif bracket == "losers":
        await _advance_de_losers_bracket(tournament_id, completed_series)
    elif bracket == "grand_finals":
        await db.update_tournament_status(tournament_id, "completed")


# ---------------------------------------------------------------------------
# Shared advancement dispatcher
# ---------------------------------------------------------------------------

async def _advance_bracket(tournament_id: int, completed_series: dict) -> None:
    t = await db.get_tournament(tournament_id)
    if not t:
        return

    t_type = t["type"]
    if t_type == "round_robin":
        await _check_round_robin_complete(t)
    elif t_type == "single_elimination":
        await _advance_single_elim(tournament_id, completed_series)
    elif t_type == "double_elimination":
        await _advance_double_elim(tournament_id, completed_series)


async def _check_round_robin_complete(tournament: dict) -> None:
    all_done = all(s["status"] in ("completed", "cancelled") for s in tournament["series"])
    if all_done and tournament["series"]:
        await db.update_tournament_status(tournament["id"], "completed")
