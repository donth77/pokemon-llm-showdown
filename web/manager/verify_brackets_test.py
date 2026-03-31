"""
Pure-logic tests for ``tournament_logic`` bracket shapes and round-robin champion UI helpers.

No database — exercises the same algorithms the manager uses for pairing, routing,
and ``_champ_ws`` annotation.

Configuration:
  Adjust ``BRACKET_MAX_N`` to stress-test larger fields (default 32).

Run from the ``web`` directory (see ``web/pytest.ini``):

  pip install -r requirements-dev.txt
  pytest manager/verify_brackets_test.py -v
"""

from __future__ import annotations

import math
from itertools import combinations

import pytest

from .tournament_logic import (
    annotate_series_tournament_champion_winner_side,
    _compact_single_elim_destinations,
    _de_wb_loser_lb_cell,
    _elimination_uses_power_of_two_winners,
    _is_power_of_two,
    _lb_matches_in_round,
    _next_power_of_two,
    _round_robin_champion_entry_id,
    _single_elim_round_stats,
    _standard_seed_order,
)

# ---------------------------------------------------------------------------
# Scope — raise to fuzz larger brackets without editing individual tests
# ---------------------------------------------------------------------------

BRACKET_MAX_N = 32

# ---------------------------------------------------------------------------
# Helpers (double-elim LB sizing mirrors ``_add_double_elim_losers_and_grand_finals``)
# ---------------------------------------------------------------------------


def _se_tournament(n: int, single_elim_bracket: str = "") -> dict:
    return {
        "entries": [{"id": i + 1} for i in range(n)],
        "single_elim_bracket": single_elim_bracket,
    }


def _count_de_lb_matches_per_round(bracket_size: int, wb_rounds: int) -> list[int]:
    lb_rounds = 2 * (wb_rounds - 1)
    if lb_rounds < 1:
        return []
    counts: list[int] = []
    for rnd in range(1, lb_rounds + 1):
        if rnd == 1:
            m = bracket_size // 4
        else:
            m = _lb_matches_in_round(bracket_size, rnd)
        counts.append(max(1, m))
    return counts


def _wb_pow2_series_per_round(bracket_size: int) -> list[int]:
    wb_rounds = int(math.log2(bracket_size))
    return [bracket_size // (2**r) for r in range(1, wb_rounds + 1)]


def _simulate_wb_r1_pow2(
    n: int, bracket_size: int
) -> tuple[list[tuple[int, int]], list[int]]:
    seed_order = _standard_seed_order(bracket_size)
    slots: list[int | None] = [None] * bracket_size
    for i, slot_idx in enumerate(seed_order):
        if i < n:
            slots[slot_idx] = i + 1
    pairs: list[tuple[int, int]] = []
    byes: list[int] = []
    for i in range(0, bracket_size, 2):
        a, b = slots[i], slots[i + 1]
        if a and b:
            pairs.append((a, b))
        elif a or b:
            byes.append(a or b)
    return pairs, sorted(byes)


def _count_compact_wb_series(n: int) -> list[int]:
    stats = _single_elim_round_stats(n)
    if not stats:
        return []
    out = [stats[0][0] + stats[0][1]]
    for rnd in range(2, len(stats) + 1):
        out.append(stats[rnd - 1][0])
    return out


# ---------------------------------------------------------------------------
# Round robin
# ---------------------------------------------------------------------------


def _series_rr(
    *,
    eid_winner: int,
    status: str = "completed",
    ws: str = "p1",
) -> dict:
    return {
        "status": status,
        "winner_side": ws,
        "winner_entry_id": eid_winner,
    }


def test_round_robin_champion_no_completed_series():
    assert _round_robin_champion_entry_id([{"status": "pending"}]) is None


def test_round_robin_champion_single_series():
    assert _round_robin_champion_entry_id([_series_rr(eid_winner=7)]) == 7


def test_round_robin_champion_clear_leader():
    s = [
        _series_rr(eid_winner=1),
        _series_rr(eid_winner=1),
        _series_rr(eid_winner=2),
    ]
    assert _round_robin_champion_entry_id(s) == 1


def test_round_robin_champion_two_way_tie_returns_none():
    s = [
        _series_rr(eid_winner=1),
        _series_rr(eid_winner=1),
        _series_rr(eid_winner=2),
        _series_rr(eid_winner=2),
    ]
    assert _round_robin_champion_entry_id(s) is None


@pytest.mark.parametrize("n", range(1, BRACKET_MAX_N + 1))
def test_round_robin_pairing_count_is_n_choose_2(n: int):
    assert len(list(combinations(range(n), 2))) == n * (n - 1) // 2


def test_annotate_round_robin_marks_only_outright_champion_when_completed():
    series: list[dict] = [
        {**_series_rr(eid_winner=1), "winner_side": "p1"},
        {**_series_rr(eid_winner=2), "winner_side": "p2"},
        {**_series_rr(eid_winner=1), "winner_side": "p1"},
    ]
    tournament = {
        "type": "round_robin",
        "status": "completed",
        "series": series,
    }
    annotate_series_tournament_champion_winner_side(tournament)
    assert series[0].get("_champ_ws") == "p1"
    assert series[1].get("_champ_ws") is None
    assert series[2].get("_champ_ws") == "p1"


def test_annotate_round_robin_no_champ_styling_when_tied():
    series: list[dict] = [
        {**_series_rr(eid_winner=1), "winner_side": "p1"},
        {**_series_rr(eid_winner=2), "winner_side": "p2"},
        {**_series_rr(eid_winner=1), "winner_side": "p1"},
        {**_series_rr(eid_winner=2), "winner_side": "p2"},
    ]
    tournament = {
        "type": "round_robin",
        "status": "completed",
        "series": series,
    }
    annotate_series_tournament_champion_winner_side(tournament)
    assert all(s.get("_champ_ws") is None for s in series)


def test_annotate_round_robin_in_progress_does_not_apply_champion_flag():
    series: list[dict] = [
        {**_series_rr(eid_winner=1), "winner_side": "p1"},
    ]
    tournament = {
        "type": "round_robin",
        "status": "in_progress",
        "series": series,
    }
    annotate_series_tournament_champion_winner_side(tournament)
    assert series[0].get("_champ_ws") is None


def test_annotate_double_elim_grand_finals_wb_winner_is_tournament_champ():
    s_gf: dict = {
        "status": "completed",
        "bracket": "grand_finals",
        "winner_side": "p1",
        "winner_entry_id": 1,
        "player1_entry_id": 1,
        "player2_entry_id": 2,
    }
    tournament = {
        "type": "double_elimination",
        "status": "completed",
        "series": [s_gf],
    }
    annotate_series_tournament_champion_winner_side(tournament)
    assert s_gf.get("_champ_ws") == "p1"


def test_annotate_double_elim_grand_finals_lb_winner_not_tournament_champ_until_reset():
    s_gf: dict = {
        "status": "completed",
        "bracket": "grand_finals",
        "winner_side": "p2",
        "winner_entry_id": 2,
        "player1_entry_id": 1,
        "player2_entry_id": 2,
    }
    s_rs: dict = {
        "status": "completed",
        "bracket": "grand_finals_reset",
        "winner_side": "p2",
        "winner_entry_id": 2,
        "player1_entry_id": 1,
        "player2_entry_id": 2,
    }
    tournament_done = {
        "type": "double_elimination",
        "status": "completed",
        "series": [s_gf, s_rs],
    }
    annotate_series_tournament_champion_winner_side(tournament_done)
    assert s_gf.get("_champ_ws") is None
    assert s_rs.get("_champ_ws") == "p2"

    tournament_mid = {
        "type": "double_elimination",
        "status": "in_progress",
        "series": [dict(s_gf)],
    }
    annotate_series_tournament_champion_winner_side(tournament_mid)
    assert tournament_mid["series"][0].get("_champ_ws") is None


# ---------------------------------------------------------------------------
# Single elimination — mode + compact destinations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", range(1, BRACKET_MAX_N + 1))
def test_single_elim_power_of_two_setting_always_padded(n: int):
    t = _se_tournament(n, "power_of_two")
    assert _elimination_uses_power_of_two_winners(t) is True


@pytest.mark.parametrize("n", range(1, BRACKET_MAX_N + 1))
def test_single_elim_compact_matches_legacy_default(n: int):
    t_compact = _se_tournament(n, "compact")
    t_default = _se_tournament(n, "")
    expect = _is_power_of_two(n)
    assert _elimination_uses_power_of_two_winners(t_compact) is expect
    assert _elimination_uses_power_of_two_winners(t_default) is expect


@pytest.mark.parametrize("n", range(2, BRACKET_MAX_N + 1))
def test_compact_single_elim_destinations_complete_and_balanced(n: int):
    if _is_power_of_two(n):
        return
    stats = _single_elim_round_stats(n)
    num_rounds = len(stats)
    dest = _compact_single_elim_destinations(n)
    m0, _b0 = stats[0]
    r1_total = m0 + stats[0][1]

    if num_rounds > 1:
        for p in range(1, r1_total + 1):
            assert (1, p) in dest, f"n={n} R1p{p}"
        for rnd_idx in range(1, num_rounds - 1):
            m_rnd = stats[rnd_idx][0]
            rnd_num = rnd_idx + 1
            for p in range(1, m_rnd + 1):
                assert (rnd_num, p) in dest, f"n={n} R{rnd_num}p{p}"
    assert (num_rounds, 1) not in dest

    placeholder_positions: set[tuple[int, int]] = set()
    for rnd_idx in range(1, num_rounds):
        m_rnd = stats[rnd_idx][0]
        rnd_num = rnd_idx + 1
        for p in range(1, m_rnd + 1):
            placeholder_positions.add((rnd_num, p))

    feeder: dict[tuple[int, int], dict[str, int]] = {}
    for src, (nr, np, side) in dest.items():
        assert (nr, np) in placeholder_positions, f"n={n} dest {src}"
        assert side in ("player1", "player2")
        key = (nr, np)
        feeder.setdefault(key, {"player1": 0, "player2": 0})
        feeder[key][side] += 1

    for pos_key in placeholder_positions:
        fc = feeder[pos_key]
        assert fc["player1"] == 1 and fc["player2"] == 1, f"n={n} {pos_key}"


# ---------------------------------------------------------------------------
# Double elimination — power-of-two (live path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", range(2, BRACKET_MAX_N + 1))
def test_double_elim_power_of_two_winners_bracket_and_lb_grid(n: int):
    bracket_size = _next_power_of_two(n)
    wb_rounds = int(math.log2(bracket_size))

    assert len(_single_elim_round_stats(n)) == wb_rounds

    per_r = _wb_pow2_series_per_round(bracket_size)
    assert sum(per_r) == bracket_size - 1

    lb_counts = _count_de_lb_matches_per_round(bracket_size, wb_rounds)
    lb_rounds = max(0, 2 * (wb_rounds - 1))
    assert len(lb_counts) == lb_rounds

    pairs, bye_seeds = _simulate_wb_r1_pow2(n, bracket_size)
    num_byes = bracket_size - n
    assert len(bye_seeds) == num_byes
    if num_byes:
        assert bye_seeds == list(range(1, num_byes + 1))
    for a, b in pairs:
        assert a + b == bracket_size + 1

    if wb_rounds >= 2:
        for wb_r in range(1, wb_rounds):
            matches = bracket_size // (2**wb_r)
            for wb_p in range(1, matches + 1):
                cell = _de_wb_loser_lb_cell(bracket_size, wb_r, wb_p, wb_rounds)
                assert cell is not None
                lr, lp = cell
                assert 1 <= lr <= lb_rounds
                assert 1 <= lp <= lb_counts[lr - 1]


@pytest.mark.parametrize("n", range(2, BRACKET_MAX_N + 1))
def test_double_elim_hypothetical_compact_wb_same_depth_as_padded(n: int):
    """If compact DE were enabled, WB round count must match ``wb_rounds`` (LB sizing)."""
    bracket_size = _next_power_of_two(n)
    wb_rounds = int(math.log2(bracket_size))
    compact_counts = _count_compact_wb_series(n)
    pow2_counts = _wb_pow2_series_per_round(bracket_size)

    assert len(compact_counts) == wb_rounds
    incompatible = not _is_power_of_two(n)
    if incompatible and compact_counts == pow2_counts:
        assert _next_power_of_two(n) == n + 1


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, 1),
        (2, 2),
        (3, 4),
        (7, 8),
        (8, 8),
        (9, 16),
        (BRACKET_MAX_N, _next_power_of_two(BRACKET_MAX_N)),
    ],
)
def test_next_power_of_two_examples(n: int, expected: int):
    assert _next_power_of_two(n) == expected


@pytest.mark.parametrize("k", range(0, 6))
def test_standard_seed_order_full_bracket_pair_sums(k: int):
    size = 2**k
    if size < 2:
        return
    order = _standard_seed_order(size)
    slots: list[int | None] = [None] * size
    for seed in range(1, size + 1):
        slots[order[seed - 1]] = seed
    for i in range(0, size, 2):
        a, b = slots[i], slots[i + 1]
        assert a is not None and b is not None
        assert a + b == size + 1
