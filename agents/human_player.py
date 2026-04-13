"""
Human-controlled Pokémon battle player (relay via web service).

Extends poke-env's Player to relay battle state to a web UI and wait for
the human's move/switch choice.  Used when ``human_play_mode`` is
``control_page`` — the human plays through ``/battle/{match_id}`` instead
of the standard Showdown client.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import aiohttp
from poke_env.player import Player
from poke_env.battle.move import Move
from poke_env.battle.pokemon import Pokemon
from poke_env.battle.abstract_battle import AbstractBattle as Battle
from poke_env.battle.pokemon_type import PokemonType
from poke_env.battle.side_condition import SideCondition

from log_print import log_print

WEB_HOST = os.getenv("WEB_HOST") or os.getenv("OVERLAY_HOST", "web")
WEB_PORT = int(os.getenv("WEB_PORT") or os.getenv("OVERLAY_PORT", "8080"))
RELAY_BASE = f"http://{WEB_HOST}:{WEB_PORT}/api/battle"

HUMAN_TURN_TIMEOUT = float(os.getenv("HUMAN_TURN_TIMEOUT") or "150")
HUMAN_ACTION_POLL_INTERVAL = float(os.getenv("HUMAN_ACTION_POLL_INTERVAL") or "0.5")


# ---------------------------------------------------------------------------
# Battle state → structured JSON
# ---------------------------------------------------------------------------


def _enum_name(x: Any) -> str | None:
    """Safely extract the lowercase name from a poke-env enum (PokemonType, Status, etc.)."""
    if x is None:
        return None
    name = getattr(x, "name", None)
    if name:
        return str(name).lower()
    # Fallback: str() returns things like "PSYCHIC (pokemon type) object"
    s = str(x)
    return s.split(" ")[0].lower() if s else None


def _pokemon_to_dict(
    pokemon: Pokemon | None, *, is_opponent: bool = False
) -> dict | None:
    if pokemon is None:
        return None
    types = [_enum_name(t) for t in pokemon.types if t is not None]
    boosts = {k: v for k, v in pokemon.boosts.items() if v != 0}
    effects = [_enum_name(e) for e in pokemon.effects] if pokemon.effects else []
    d: dict[str, Any] = {
        "species": pokemon.species,
        "level": pokemon.level,
        "types": types,
        "hp_pct": round(pokemon.current_hp_fraction * 100),
        "status": _enum_name(pokemon.status) if pokemon.status else None,
        "status_counter": pokemon.status_counter if pokemon.status else 0,
        "ability": pokemon.ability,
        "item": pokemon.item,
        "boosts": boosts,
        "effects": effects,
        "fainted": pokemon.fainted,
    }
    if not is_opponent:
        d["base_stats"] = dict(pokemon.base_stats) if pokemon.base_stats else {}
    if pokemon.moves:
        d["known_moves"] = [str(m) for m in pokemon.moves]
    return d


def _move_to_dict(move: Move, opponent_active: Pokemon | None) -> dict:
    eff: float | None = None
    if opponent_active and move.base_power > 0:
        try:
            eff = opponent_active.damage_multiplier(move)
        except Exception:
            pass
    try:
        prio = int(move.priority)
    except Exception:
        prio = 0
    # poke-env's Move.accuracy is True for always-hit moves; otherwise float 0.0-1.0.
    acc_raw = move.accuracy
    if acc_raw is True:
        acc_out = 100
        always_hits = True
    elif isinstance(acc_raw, (int, float)):
        acc_out = int(round(float(acc_raw) * 100)) if acc_raw <= 1 else int(acc_raw)
        always_hits = False
    else:
        acc_out = None
        always_hits = False
    return {
        "id": move.id,
        "name": move.id.replace("-", " ").replace("_", " ").title(),
        "type": _enum_name(move.type) or "normal",
        "power": move.base_power,
        "accuracy": acc_out,
        "always_hits": always_hits,
        "pp": move.current_pp,
        "max_pp": move.max_pp,
        "category": _enum_name(move.category) or "status",
        "priority": prio,
        "effectiveness": eff,
    }


def _estimate_hazard_damage(pokemon: Pokemon, side_conditions: dict) -> float:
    dmg = 0.0
    if SideCondition.STEALTH_ROCK in side_conditions:
        try:
            sr_mult = pokemon.damage_multiplier(PokemonType.ROCK)
            dmg += 12.5 * sr_mult
        except Exception:
            dmg += 12.5
    spikes = side_conditions.get(SideCondition.SPIKES, 0)
    is_grounded = PokemonType.FLYING not in (pokemon.types or [])
    if spikes and is_grounded:
        dmg += [0, 12.5, 16.67, 25.0][min(spikes, 3)]
    return dmg


def _side_conditions_list(conditions: dict) -> list[str]:
    out = []
    for cond, val in conditions.items():
        name = _enum_name(cond) or "?"
        if val > 1:
            out.append(f"{name} x{val}")
        else:
            out.append(name)
    return out


def build_battle_state_json(battle: Battle) -> dict:
    """Convert a poke-env Battle object to structured JSON for the battle control page."""
    opponent_active = battle.opponent_active_pokemon

    moves = []
    for i, move in enumerate(battle.available_moves):
        d = _move_to_dict(move, opponent_active)
        d["index"] = i + 1  # 1-based for display
        moves.append(d)

    switches = []
    for i, pkmn in enumerate(battle.available_switches):
        hazard = _estimate_hazard_damage(pkmn, battle.side_conditions)
        d = _pokemon_to_dict(pkmn, is_opponent=False)
        d["index"] = i + 1  # 1-based for display
        d["hazard_damage_pct"] = round(hazard, 1)
        switches.append(d)

    opp_bench = [
        _pokemon_to_dict(p, is_opponent=True)
        for p in battle.opponent_team.values()
        if not p.fainted and not p.active
    ]

    weather_list = list(battle.weather.keys()) if battle.weather else []
    field_list = list(battle.fields.keys()) if battle.fields else []

    your_remaining = sum(1 for p in battle.team.values() if not p.fainted)
    opp_remaining = sum(1 for p in battle.opponent_team.values() if not p.fainted)

    return {
        "turn": battle.turn,
        "battle_tag": (getattr(battle, "battle_tag", None) or "").lstrip(">"),
        "active_pokemon": _pokemon_to_dict(battle.active_pokemon),
        "opponent_active": _pokemon_to_dict(opponent_active, is_opponent=True),
        "available_moves": moves,
        "available_switches": switches,
        "opponent_bench": opp_bench,
        "field": {
            "weather": _enum_name(weather_list[0]) if weather_list else None,
            "terrain": [_enum_name(f) for f in field_list],
            "your_side": _side_conditions_list(battle.side_conditions),
            "opponent_side": _side_conditions_list(battle.opponent_side_conditions),
        },
        "your_remaining": your_remaining,
        "opponent_remaining": opp_remaining,
        "trapped": getattr(battle, "trapped", False),
        "force_switch": len(battle.available_moves) == 0,
        "finished": battle.finished,
    }


# ---------------------------------------------------------------------------
# HumanPlayer
# ---------------------------------------------------------------------------


class HumanPlayer(Player):
    """poke-env Player that relays decisions to a human via the web service."""

    def __init__(
        self,
        *,
        match_id: int | None = None,
        battle_side: str = "p1",
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self._match_id = match_id
        self._battle_side = battle_side

    # -- poke-env contract --------------------------------------------------

    async def choose_move(self, battle: Battle) -> str:
        state_json = build_battle_state_json(battle)
        state_json["match_id"] = self._match_id

        try:
            async with aiohttp.ClientSession() as session:
                # Push state to relay
                await self._push_state(session, state_json)

                # Poll for human's action
                action = await self._poll_action(session)

                if action:
                    kind = action.get("action_type", "")
                    raw_idx = action.get("index")
                    if raw_idx is not None:
                        idx = int(raw_idx) - 1  # UI sends 1-based, convert to 0-based
                        if kind == "move" and 0 <= idx < len(battle.available_moves):
                            order = self.create_order(battle.available_moves[idx])
                            await self._post_thought(
                                battle,
                                f"move {idx + 1} ({battle.available_moves[idx].id})",
                            )
                            return order
                        if kind == "switch" and 0 <= idx < len(
                            battle.available_switches
                        ):
                            order = self.create_order(battle.available_switches[idx])
                            await self._post_thought(
                                battle,
                                f"switch {idx + 1} ({battle.available_switches[idx].species})",
                            )
                            return order

                    log_print(
                        f"  [HumanPlayer] Invalid action: {action}, falling back to random",
                        flush=True,
                    )
        except Exception as e:
            log_print(
                f"  [HumanPlayer] Relay error: {e}, falling back to random", flush=True
            )

        return self.choose_random_move(battle)

    # -- relay helpers -------------------------------------------------------

    async def _push_state(self, session: aiohttp.ClientSession, state: dict) -> None:
        url = f"{RELAY_BASE}/{self._match_id}/state"
        try:
            async with session.post(
                url, json=state, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    log_print(
                        f"  [HumanPlayer] State push failed: {resp.status}", flush=True
                    )
        except Exception as e:
            log_print(f"  [HumanPlayer] State push error: {e}", flush=True)

    async def _poll_action(self, session: aiohttp.ClientSession) -> dict | None:
        url = f"{RELAY_BASE}/{self._match_id}/action"
        start = time.time()
        while time.time() - start < HUMAN_TURN_TIMEOUT:
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    # 404 = no action yet, keep polling
            except Exception:
                pass
            await asyncio.sleep(HUMAN_ACTION_POLL_INTERVAL)

        log_print(
            f"  [HumanPlayer] Turn timeout after {HUMAN_TURN_TIMEOUT}s", flush=True
        )
        return None

    async def _cleanup_relay(self) -> None:
        if self._match_id is None:
            return
        url = f"{RELAY_BASE}/{self._match_id}/relay"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(url, timeout=aiohttp.ClientTimeout(total=3)):
                    pass
        except Exception:
            pass

    async def _post_thought(self, battle: Battle, action_text: str) -> None:
        url = f"http://{WEB_HOST}:{WEB_PORT}/thought"
        payload = {
            "player": self.username,
            "action": action_text,
            "reasoning": "",
            "callout": "",
            "turn": getattr(battle, "turn", None),
            "timestamp": time.time(),
            "battle_side": self._battle_side,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=3)
                ):
                    pass
        except Exception:
            pass

    def _battle_finished_callback(self, battle: Battle) -> None:
        # Schedule relay cleanup (fire-and-forget)
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self._cleanup_relay())
        except Exception:
            pass
