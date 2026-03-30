"""
Tournament bracket generation and progression logic.

Supports round-robin, single elimination, and double elimination formats.
All functions operate through the db module — they read entries/series/matches
and create new series or update statuses as results come in.

Double elimination: winners feed winners + losers bracket; losers bracket feeds
grand finals (slot 2) alongside the winners-bracket champion (slot 1). Power-of-two
winners brackets align WB round W drop-ins with LB round W (and LB1→LB2 indexing);
compact layouts keep a heuristic path for non-standard shapes.
"""

from __future__ import annotations

import math
from itertools import combinations

from . import db


def _round_robin_champion_entry_id(series: list[dict]) -> int | None:
    """
    Return the entry_id of the outright round-robin champion (most series wins).
    None when no completed series exist or there is a tie for first.
    """
    from collections import Counter

    wins: Counter[int] = Counter()
    for s in series:
        if s.get("status") != "completed":
            continue
        we = s.get("winner_entry_id")
        if we is not None:
            wins[int(we)] += 1
    if not wins:
        return None
    top_count = max(wins.values())
    leaders = [eid for eid, cnt in wins.items() if cnt == top_count]
    return leaders[0] if len(leaders) == 1 else None


def annotate_series_tournament_champion_winner_side(tournament: dict) -> None:
    """
    Set each series row's _champ_ws to 'p1' or 'p2' only when that side won the
    tournament-deciding series (grand finals in double elim; winners final in
    single elim; series won by the outright champion in round robin). Other
    bracket wins get no _champ_ws so the UI can show a round win without
    gold/trophy champion styling.
    """
    series = tournament.get("series") or []
    t_type = tournament.get("type") or ""
    max_wb = 0
    for s in series:
        if s.get("bracket") == "winners" and s.get("round_number") is not None:
            try:
                max_wb = max(max_wb, int(s["round_number"]))
            except (TypeError, ValueError):
                pass

    rr_champ_eid: int | None = None
    if t_type == "round_robin" and tournament.get("status") == "completed":
        rr_champ_eid = _round_robin_champion_entry_id(series)

    for s in series:
        s.pop("_champ_ws", None)
        if s.get("status") != "completed":
            continue
        ws = s.get("winner_side")
        if ws not in ("p1", "p2"):
            continue
        champ = False
        if t_type == "round_robin":
            if rr_champ_eid is not None:
                winner_eid = s.get("winner_entry_id")
                champ = winner_eid is not None and int(winner_eid) == rr_champ_eid
        elif t_type == "double_elimination" and s.get("bracket") == "grand_finals":
            champ = True
        elif t_type == "single_elimination":
            br = s.get("bracket")
            if br in (None, "winners"):
                rn = s.get("round_number")
                try:
                    rn_int = int(rn) if rn is not None else None
                except (TypeError, ValueError):
                    rn_int = None
                if rn_int is not None and rn_int == max_wb and max_wb > 0:
                    champ = True
        if champ:
            s["_champ_ws"] = ws


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
    the tournament complete.  Elimination tournaments are cancelled outright because
    a missing series result makes the bracket uncompletable.
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
    elif t["type"] in ("single_elimination", "double_elimination"):
        if t["status"] in ("pending", "in_progress"):
            await db.cancel_tournament(tournament_id)


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


def _is_power_of_two(n: int) -> bool:
    return n >= 1 and (n & (n - 1)) == 0


def _elimination_uses_power_of_two_winners(tournament: dict) -> bool:
    """
    True → padded winners bracket (next 2^k). Used for single elimination and for
    double elimination's winners bracket. False → compact winners rounds.
    Column ``single_elim_bracket`` is stored for both elimination types.
    """
    n = len(tournament.get("entries") or [])
    s = (tournament.get("single_elim_bracket") or "").strip().lower()
    if s == "power_of_two":
        return True
    if s == "compact":
        return _is_power_of_two(n)
    # Legacy rows (NULL): same as compact
    return _is_power_of_two(n)


def _single_elim_round_stats(n: int) -> list[tuple[int, int]]:
    """(matches_played, bye_survivors) per round until one champion."""
    stats: list[tuple[int, int]] = []
    alive = n
    while alive > 1:
        m = alive // 2
        b = alive % 2
        stats.append((m, b))
        alive = m + b
    return stats


def _compact_single_elim_destinations(
    n: int,
) -> dict[tuple[int, int], tuple[int, int, str]]:
    """
    Map (round, match_position) -> (next_round, next_position, player1|player2)
    for compact single elimination (no power-of-2 padding).

    When a round has an odd number of survivors, the leftover entry (``rest``)
    stays in ``alive`` without a dest assignment.  It carries forward until a
    later iteration's chunk naturally absorbs it, giving it the correct final
    destination.  This avoids intermediate dead-writes that would be overwritten.
    """
    stats = _single_elim_round_stats(n)
    dest: dict[tuple[int, int], tuple[int, int, str]] = {}
    alive: list[tuple[int, int]] = [
        (1, p) for p in range(1, stats[0][0] + stats[0][1] + 1)
    ]
    for idx in range(1, len(stats)):
        m, _b = stats[idx]
        r_dest = idx + 1
        chunk = alive[: 2 * m]
        rest = alive[2 * m :]
        for i, src in enumerate(chunk):
            mp = i // 2 + 1
            side = "player1" if i % 2 == 0 else "player2"
            dest[src] = (r_dest, mp, side)
        alive = [(r_dest, i) for i in range(1, m + 1)] + list(rest)
    return dest


async def _generate_single_elimination_power_of_two(tournament: dict) -> None:
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


async def _create_compact_winners_bracket(tournament: dict) -> None:
    """
    Winners bracket only: no padding to 2^k (e.g. 6 players -> 3 round-1 games).
    Byes only when n is odd (best seeds). Does not run post-creation advancement.

    Match order is reversed so that the top seed pair occupies the highest
    match_position.  The compact routing in ``_compact_single_elim_destinations``
    gives the highest position the structural bye (skip a round) when the
    survivor count is odd, so this ensures the top seeds get that advantage.
    """
    tid = tournament["id"]
    entries = sorted(tournament["entries"], key=lambda e: e["seed"])
    fmt = tournament["battle_format"]
    best_of = tournament["best_of"]
    n = len(entries)
    stats = _single_elim_round_stats(n)
    m0, b0 = stats[0]

    bye_entries = entries[:b0]
    play_entries = entries[b0:]
    pos = 0

    for i in range(m0):
        pos += 1
        ri = m0 - 1 - i
        a = play_entries[ri]
        b = play_entries[2 * m0 - 1 - ri]
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

    for e in bye_entries:
        pos += 1
        s = await db.create_series(
            tournament_id=tid,
            best_of=1,
            battle_format=fmt,
            round_number=1,
            match_position=pos,
            bracket="winners",
            player1_provider=e["provider"],
            player1_model=e["model"],
            player1_persona=e["persona_slug"],
            player1_entry_id=e["id"],
            player2_provider=None,
            player2_model=None,
            player2_persona=None,
            player2_entry_id=None,
            auto_queue=False,
        )
        await db.complete_series(s["id"], "p1", e["id"])

    num_rounds = len(stats)
    for rnd in range(2, num_rounds + 1):
        m_count = stats[rnd - 1][0]
        for p in range(1, m_count + 1):
            await db.create_series(
                tournament_id=tid,
                best_of=best_of,
                battle_format=fmt,
                round_number=rnd,
                match_position=p,
                bracket="winners",
                auto_queue=False,
            )


async def _generate_single_elimination_compact(tournament: dict) -> None:
    tid = tournament["id"]
    await _create_compact_winners_bracket(tournament)
    t = await db.get_tournament(tid)
    if t:
        for s in t["series"]:
            if (
                s["status"] == "completed"
                and s["round_number"] == 1
                and s.get("bracket") == "winners"
            ):
                await _advance_single_elim(tid, s)


async def _generate_single_elimination(tournament: dict) -> None:
    if _elimination_uses_power_of_two_winners(tournament):
        await _generate_single_elimination_power_of_two(tournament)
    else:
        await _generate_single_elimination_compact(tournament)


def _standard_seed_order(size: int) -> list[int]:
    """Return slot indices for standard tournament seeding (fold method).

    ``seed_order[i]`` is the bracket slot where seed ``i + 1`` is placed.
    Produces the standard matchups 1 v *size*, 2 v *size − 1*, … with seeds 1
    and 2 on opposite sides.  Byes (empty slots when *n < size*) naturally land
    on the lowest seeds, giving top seeds the bye advantage.
    """
    if size == 1:
        return [0]
    slots = [1]
    while len(slots) < size:
        current_size = len(slots) * 2
        expanded = []
        for s in slots:
            expanded.append(s)
            expanded.append(current_size + 1 - s)
        slots = expanded
    seed_order = [0] * size
    for pos, seed in enumerate(slots):
        seed_order[seed - 1] = pos
    return seed_order


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


def _count_pending_lb_feeders(tournament: dict, series: dict) -> int:
    """
    Count feeder series for a losers-bracket series that are not yet resolved.
    A feeder in a terminal state (completed/cancelled) has either already slotted
    its contribution or will never do so — either way, nothing more to wait for.

    Feeder topology (power-of-two DE):
      LB R1 pos P:        WB R1 losers from WB R1 pos 2P-1 and 2P.
      LB even R pos P:    LB R(R-1) winner at pos P  +  WB R(R/2+1) loser at pos P.
      LB odd R>1 pos P:   LB R(R-1) winners at pos 2P-1 and 2P (merge).
    """
    rnd = series["round_number"]
    pos = series["match_position"]
    feeders: list[dict | None] = []

    if rnd == 1:
        for wp in (2 * pos - 1, 2 * pos):
            feeders.append(
                _find_series(tournament, bracket="winners", round_number=1, match_position=wp)
            )
    elif rnd % 2 == 0:
        feeders.append(
            _find_series(tournament, bracket="losers", round_number=rnd - 1, match_position=pos)
        )
        wb_r = rnd // 2 + 1
        feeders.append(
            _find_series(tournament, bracket="winners", round_number=wb_r, match_position=pos)
        )
    else:
        for fp in (2 * pos - 1, 2 * pos):
            feeders.append(
                _find_series(tournament, bracket="losers", round_number=rnd - 1, match_position=fp)
            )

    return sum(
        1
        for f in feeders
        if f is not None and f.get("status") not in ("completed", "cancelled")
    )


async def _resolve_de_byes(tournament_id: int) -> None:
    """
    Iteratively cancel/auto-advance LB series that can never receive 2 entrants
    (cascading from WB byes in power-of-two brackets).

    - 0 entrants possible → cancel the series.
    - 1 entrant present, no more possible → auto-complete as a bye and advance.
    - Repeats until a full pass produces no changes (fixpoint).
    """
    for _ in range(50):
        t = await db.get_tournament(tournament_id)
        if not t:
            return
        resolved_any = False
        for s in t["series"]:
            if s.get("bracket") != "losers" or s.get("status") != "pending":
                continue
            p1_set = bool(s.get("player1_provider"))
            p2_set = bool(s.get("player2_provider"))
            if p1_set and p2_set:
                continue
            have = int(p1_set) + int(p2_set)
            pending = _count_pending_lb_feeders(t, s)
            if have + pending >= 2:
                continue

            if have == 0 and pending == 0:
                async with db._db() as conn:
                    await conn.execute(
                        "UPDATE series SET status = 'cancelled', updated_at = ? "
                        "WHERE id = ? AND status = 'pending'",
                        (db._now(), s["id"]),
                    )
                    await conn.commit()
                resolved_any = True
            elif have == 1 and pending == 0:
                side = "p1" if p1_set else "p2"
                entry_id = s.get(f"player{side[1]}_entry_id")
                await db.complete_series(s["id"], side, entry_id)
                completed = await db.get_series(s["id"])
                if completed:
                    await _advance_de_losers_bracket(tournament_id, completed)
                resolved_any = True
        if not resolved_any:
            break


async def _queue_series_matches(tournament_id: int, series_id: int) -> None:
    refreshed = await db.get_series(series_id)
    if not refreshed or not refreshed.get("player1_provider") or not refreshed.get(
        "player2_provider"
    ):
        return
    if refreshed.get("status") in ("completed", "cancelled"):
        return
    existing = refreshed.get("matches") or []
    if existing:
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
        print(
            f"[tournament] WARNING: series {series_id} already has both players; "
            f"cannot slot entry {entry.get('id')} ({entry.get('persona_slug')})"
        )
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


async def _slot_compact_single_elim_winner(
    tournament_id: int,
    completed_series: dict,
    tournament: dict,
    dest_slot: tuple[int, int, str],
) -> bool:
    """
    Advance winner using compact (non power-of-two) routing.
    Returns False when dest is the last series (caller should complete tournament).
    """
    nr, np, side_prefix = dest_slot
    if side_prefix not in ("player1", "player2"):
        side_prefix = "player1"
    target = _find_series(
        tournament, bracket="winners", round_number=nr, match_position=np
    )
    if not target:
        return False

    winner_entry_id = completed_series.get("winner_entry_id")
    entry = _entry_for_id(tournament, winner_entry_id)
    if not entry:
        return True

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

    n = len(t.get("entries") or [])
    if _elimination_uses_power_of_two_winners(t):
        slotted = await _slot_winner_into_next_winners_round(
            tournament_id, completed_series, t
        )
    else:
        dest = _compact_single_elim_destinations(n)
        key = (
            completed_series["round_number"],
            completed_series["match_position"],
        )
        if key not in dest:
            slotted = False
        else:
            slotted = await _slot_compact_single_elim_winner(
                tournament_id, completed_series, t, dest[key]
            )
    if not slotted:
        await db.update_tournament_status(tournament_id, "completed")


def _de_wb_loser_lb_cell(
    bracket_size: int,
    wb_round: int,
    wb_pos: int,
    wb_rounds: int,
) -> tuple[int, int] | None:
    """
    Losers bracket (round, position) for a loser exiting winners, except the
    WB finals loser (handled separately). None means caller should use fallback.

    LB alternates internal-merge and drop-in rounds:
      LB R1          = WB R1 losers play each other
      LB R(2*(W-1))  = drop-in round for WB round W losers  (W >= 2)
    WB R1 pairs adjacent matches: ``(1, (wb_pos + 1) // 2)``.
    Later WB rounds map 1:1 by position into even LB rounds.
    """
    if wb_round >= wb_rounds:
        return None
    if wb_round == 1:
        return (1, (wb_pos + 1) // 2)
    return (2 * (wb_round - 1), wb_pos)


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
    if not cell:
        print(f"[tournament] WARNING: no LB cell for WB R{wb_r} pos {wb_p} loser")
        return
    lr, lp = cell
    target = _find_series(t, bracket="losers", round_number=lr, match_position=lp)
    if not target:
        # Stale tournament snapshot — re-read once.
        t2 = await db.get_tournament(tournament_id)
        if t2:
            target = _find_series(t2, bracket="losers", round_number=lr, match_position=lp)
    if not target:
        print(f"[tournament] WARNING: LB series R{lr} pos {lp} not found for WB loser")
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
        # WB finalist always drops into the *last* losers round (faces LB penultimate winner).
        dest_r = lb_rounds
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

    # DE always uses a power-of-two winners bracket (LB is always padded).
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
    # LB rounds alternate: odd = internal/drop-in prep (1:1 advance),
    # even = merge (pairwise halve).  Odd-round winners keep their position
    # so they land in the correct drop-in slot opposite a WB loser.
    if rnd % 2 == 1:
        next_pos = pos
        prefer_p1 = True
    else:
        next_pos = (pos + 1) // 2
        prefer_p1 = pos % 2 == 1

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

async def _add_double_elim_losers_and_grand_finals(
    tournament: dict, *, bracket_size: int, wb_rounds: int
) -> None:
    """LB placeholders + grand finals (shared by power-of-2 and compact WB)."""
    tid = tournament["id"]
    fmt = tournament["battle_format"]
    best_of = tournament["best_of"]

    lb_rounds = 2 * (wb_rounds - 1)
    for rnd in range(1, lb_rounds + 1):
        if rnd == 1:
            matches_in_round = bracket_size // 4
        elif rnd % 2 == 0:
            matches_in_round = _lb_matches_in_round(bracket_size, rnd)
        else:
            matches_in_round = _lb_matches_in_round(bracket_size, rnd)
        matches_in_round = max(1, matches_in_round)
        for p in range(1, matches_in_round + 1):
            await db.create_series(
                tournament_id=tid,
                best_of=best_of,
                battle_format=fmt,
                round_number=rnd,
                match_position=p,
                bracket="losers",
                auto_queue=False,
            )

    await db.create_series(
        tournament_id=tid,
        best_of=best_of,
        battle_format=fmt,
        round_number=1,
        match_position=1,
        bracket="grand_finals",
        auto_queue=False,
    )


async def _generate_double_elimination_power_of_two(tournament: dict) -> None:
    """
    Double elim with padded winners bracket (next 2^k). Losers sizing uses the
    same ``bracket_size`` as the classic tree.
    """
    tid = tournament["id"]
    entries = sorted(tournament["entries"], key=lambda e: e["seed"])
    fmt = tournament["battle_format"]
    best_of = tournament["best_of"]
    n = len(entries)

    bracket_size = _next_power_of_two(n)
    wb_rounds = int(math.log2(bracket_size))

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

    for rnd in range(2, wb_rounds + 1):
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

    await _add_double_elim_losers_and_grand_finals(
        tournament, bracket_size=bracket_size, wb_rounds=wb_rounds
    )

    t = await db.get_tournament(tid)
    if t:
        for s in t["series"]:
            if (
                s["status"] == "completed"
                and s["round_number"] == 1
                and s["bracket"] == "winners"
            ):
                await _advance_double_elim(tid, s)

    await _resolve_de_byes(tid)


async def _generate_double_elimination_compact(tournament: dict) -> None:
    """
    Double elim with a compact winners bracket; losers + GF sized from
    ``next_power_of_two(n)`` so WB depth matches the padded case (same ``wb_rounds``).
    """
    tid = tournament["id"]
    n = len(tournament["entries"])
    bracket_size = _next_power_of_two(n)
    wb_rounds = int(math.log2(bracket_size))

    await _create_compact_winners_bracket(tournament)
    await _add_double_elim_losers_and_grand_finals(
        tournament, bracket_size=bracket_size, wb_rounds=wb_rounds
    )

    t = await db.get_tournament(tid)
    if t:
        for s in t["series"]:
            if (
                s["status"] == "completed"
                and s["round_number"] == 1
                and s["bracket"] == "winners"
            ):
                await _advance_double_elim(tid, s)


async def _generate_double_elimination(tournament: dict) -> None:
    # Always use the power-of-two winners bracket for DE: the losers bracket
    # is always sized from next_power_of_two(n), so the WB must match for
    # round/position routing to be consistent.
    await _generate_double_elimination_power_of_two(tournament)


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
        return

    await _resolve_de_byes(tournament_id)


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
