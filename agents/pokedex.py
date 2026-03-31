"""
Pokédex data layer for LLM battle agents.

Provides lookup functions backed by poke-env's GenData (moves, species,
type chart, learnsets) and bundled Showdown text descriptions (items,
abilities, move descriptions).

All public functions return formatted strings suitable for tool responses
or context injection. IDs use poke-env's to_id_str convention (lowercase,
no spaces/punctuation).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from poke_env.data import GenData, to_id_str

_DATA_DIR = Path(os.getenv("POKEDEX_DATA_DIR", "/app/data"))


@lru_cache(maxsize=4)
def _load_json(filename: str) -> dict[str, Any]:
    path = _DATA_DIR / filename
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _items_text() -> dict[str, Any]:
    return _load_json("items.json")


def _abilities_text() -> dict[str, Any]:
    return _load_json("abilities.json")


def _moves_text() -> dict[str, Any]:
    return _load_json("moves.json")


def _gen_data(gen: int) -> GenData:
    gen = max(1, min(gen, 9))
    return GenData.from_gen(gen)


def gen_from_format(battle_format: str) -> int:
    """Extract generation number from a format string like 'gen8randombattle'."""
    for i, ch in enumerate(battle_format):
        if ch.isdigit():
            return int(battle_format[i])
    return 8


# ---------------------------------------------------------------------------
# Public lookup functions
# ---------------------------------------------------------------------------


def lookup_move(move_id: str, gen: int = 8) -> str:
    """Full details for a move: type, power, accuracy, PP, priority, effects, description."""
    mid = to_id_str(move_id)
    gd = _gen_data(gen)
    entry = gd.moves.get(mid)
    if not entry:
        return f"Move '{move_id}' not found in gen {gen} data."

    name = entry.get("name", mid)
    mtype = entry.get("type", "???")
    power = entry.get("basePower", 0)
    acc = entry.get("accuracy", True)
    acc_str = "always hits" if acc is True else f"{acc}%"
    pp = entry.get("pp", "?")
    cat = entry.get("category", "?")
    pri = entry.get("priority", 0)

    lines = [f"{name} ({mtype}, {cat})"]
    lines.append(f"  Power: {power} | Accuracy: {acc_str} | PP: {pp}")
    if pri != 0:
        lines.append(f"  Priority: {pri:+d}")

    target = entry.get("target", "")
    if target:
        lines.append(f"  Target: {target}")

    secondary = entry.get("secondary")
    if isinstance(secondary, dict):
        chance = secondary.get("chance", "")
        status = secondary.get("status", "")
        boosts = secondary.get("boosts")
        parts = []
        if status:
            parts.append(f"{chance}% {status}" if chance else status)
        if isinstance(boosts, dict):
            bs = " ".join(f"{k}:{v:+d}" for k, v in boosts.items())
            parts.append(f"{chance}% {bs}" if chance else bs)
        if parts:
            lines.append(f"  Secondary: {'; '.join(parts)}")

    secondaries = entry.get("secondaries")
    if isinstance(secondaries, list):
        for sec in secondaries:
            chance = sec.get("chance", "")
            status = sec.get("status", "")
            boosts = sec.get("boosts")
            parts = []
            if status:
                parts.append(f"{chance}% {status}" if chance else status)
            if isinstance(boosts, dict):
                bs = " ".join(f"{k}:{v:+d}" for k, v in boosts.items())
                parts.append(f"{chance}% {bs}" if chance else bs)
            if parts:
                lines.append(f"  Secondary: {'; '.join(parts)}")

    flags = entry.get("flags", {})
    notable_flags = []
    if flags.get("contact"):
        notable_flags.append("contact")
    if flags.get("sound"):
        notable_flags.append("sound")
    if flags.get("punch"):
        notable_flags.append("punch")
    if flags.get("bite"):
        notable_flags.append("bite")
    if flags.get("bullet"):
        notable_flags.append("bullet")
    if notable_flags:
        lines.append(f"  Flags: {', '.join(notable_flags)}")

    heal = entry.get("heal")
    if heal:
        pct = int(heal[0] / heal[1] * 100) if isinstance(heal, list) else "?"
        lines.append(f"  Heals: {pct}% of user's max HP")

    recoil = entry.get("recoil")
    if recoil and isinstance(recoil, list):
        pct = int(recoil[0] / recoil[1] * 100)
        lines.append(f"  Recoil: {pct}% of damage dealt")

    drain = entry.get("drain")
    if drain and isinstance(drain, list):
        pct = int(drain[0] / drain[1] * 100)
        lines.append(f"  Drain: recovers {pct}% of damage dealt")

    text = _moves_text().get(mid, {})
    desc = text.get("desc") or text.get("shortDesc")
    if desc:
        lines.append(f"  Description: {desc}")

    return "\n".join(lines)


def lookup_pokemon(species: str, gen: int = 8) -> str:
    """Base stats, typing, abilities, and learnset highlights for a species."""
    sid = to_id_str(species)
    gd = _gen_data(gen)
    entry = gd.pokedex.get(sid)
    if not entry:
        return f"Pokemon '{species}' not found in gen {gen} data."

    name = entry.get("name", sid)
    types = entry.get("types", [])
    bs = entry.get("baseStats", {})

    lines = [f"{name} ({'/'.join(types)})"]

    stat_line = " | ".join(
        f"{k.upper()}: {v}"
        for k, v in [
            ("hp", bs.get("hp", "?")),
            ("atk", bs.get("atk", "?")),
            ("def", bs.get("def", "?")),
            ("spa", bs.get("spa", "?")),
            ("spd", bs.get("spd", "?")),
            ("spe", bs.get("spe", "?")),
        ]
    )
    lines.append(f"  Base stats: {stat_line}")

    bst = sum(v for v in bs.values() if isinstance(v, (int, float)))
    lines.append(f"  BST: {bst}")

    abilities = entry.get("abilities", {})
    ab_parts = []
    for slot, ab_name in sorted(abilities.items()):
        label = "Hidden" if slot == "H" else f"Slot {slot}"
        ab_parts.append(f"{ab_name} ({label})")
    if ab_parts:
        lines.append(f"  Abilities: {', '.join(ab_parts)}")

    weight = entry.get("weightkg")
    if weight:
        lines.append(f"  Weight: {weight} kg")

    other_formes = entry.get("otherFormes")
    if other_formes:
        lines.append(f"  Other formes: {', '.join(other_formes)}")

    return "\n".join(lines)


def lookup_type_matchup(
    attacking_type: str, defending_types: list[str], gen: int = 8
) -> str:
    """Type effectiveness multiplier for an attacking type vs one or two defending types."""
    gd = _gen_data(gen)
    chart = gd.type_chart

    atk = attacking_type.upper()
    if atk not in chart.get(list(chart.keys())[0] if chart else "", {}):
        known = ", ".join(sorted(next(iter(chart.values()), {}).keys()))
        return f"Unknown attacking type '{attacking_type}'. Valid types: {known}"

    multiplier = 1.0
    for dt in defending_types:
        dt_upper = dt.upper()
        if dt_upper not in chart:
            return f"Unknown defending type '{dt}'. Check spelling."
        mult = chart[dt_upper].get(atk, 1.0)
        multiplier *= mult

    def_str = "/".join(defending_types)
    if multiplier == 0:
        eff = "immune (0x)"
    elif multiplier < 1:
        eff = f"not very effective ({multiplier}x)"
    elif multiplier > 1:
        eff = f"super effective ({multiplier}x)"
    else:
        eff = f"neutral ({multiplier}x)"

    return f"{attacking_type} vs {def_str}: {eff}"


def lookup_ability(ability_id: str) -> str:
    """Name and description of an ability."""
    aid = to_id_str(ability_id)
    entry = _abilities_text().get(aid, {})
    if not entry:
        return f"Ability '{ability_id}' not found."
    name = entry.get("name", aid)
    desc = entry.get("desc") or entry.get("shortDesc", "No description available.")
    return f"{name}: {desc}"


def lookup_item(item_id: str) -> str:
    """Name and description of a held item."""
    iid = to_id_str(item_id)
    entry = _items_text().get(iid, {})
    if not entry:
        return f"Item '{item_id}' not found."
    name = entry.get("name", iid)
    desc = entry.get("desc") or entry.get("shortDesc", "No description available.")
    return f"{name}: {desc}"


# ---------------------------------------------------------------------------
# Auto-enrich: inject Pokédex notes into battle state text
# ---------------------------------------------------------------------------


def auto_enrich_battle_context(battle: Any, gen: int = 8) -> str:
    """
    Build a POKEDEX NOTES section for entities visible in the current battle.

    Focuses on info NOT already present in format_battle_state output:
    ability descriptions, item descriptions, and move effect descriptions.
    """
    notes: list[str] = []

    if battle.active_pokemon and battle.active_pokemon.ability:
        ab = battle.active_pokemon.ability
        desc = lookup_ability(ab)
        if "not found" not in desc:
            notes.append(f"Your ability — {desc}")

    if battle.opponent_active_pokemon:
        opp = battle.opponent_active_pokemon
        if opp.ability:
            desc = lookup_ability(opp.ability)
            if "not found" not in desc:
                notes.append(f"Opponent's ability — {desc}")

    if battle.active_pokemon and battle.active_pokemon.item:
        desc = lookup_item(battle.active_pokemon.item)
        if "not found" not in desc:
            notes.append(f"Your item — {desc}")

    if (
        battle.opponent_active_pokemon
        and battle.opponent_active_pokemon.item
        and battle.opponent_active_pokemon.item != GenData.UNKNOWN_ITEM
    ):
        desc = lookup_item(battle.opponent_active_pokemon.item)
        if "not found" not in desc:
            notes.append(f"Opponent's item — {desc}")

    seen_moves: set[str] = set()
    for move in getattr(battle, "available_moves", []):
        mid = to_id_str(move.id)
        if mid in seen_moves:
            continue
        seen_moves.add(mid)
        text = _moves_text().get(mid, {})
        desc = text.get("desc") or text.get("shortDesc")
        if desc:
            notes.append(f"Move {move.id} — {desc}")

    if not notes:
        return ""
    return "=== POKEDEX NOTES ===\n" + "\n".join(notes)
