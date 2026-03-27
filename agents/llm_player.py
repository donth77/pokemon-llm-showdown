"""
LLM-powered Pokemon battle player (Anthropic / DeepSeek).

Sends the full battle state to Claude each turn and parses back
a move or switch decision.
"""

import os
import re
import asyncio
import json
import time
import threading
from typing import Literal

from anthropic import Anthropic
from openai import OpenAI
from poke_env.player import Player
from poke_env.battle.move import Move
from poke_env.battle.pokemon import Pokemon
from poke_env.battle.abstract_battle import AbstractBattle as Battle
from poke_env.battle.pokemon_type import PokemonType
from poke_env.battle.side_condition import SideCondition

Provider = Literal["anthropic", "deepseek"]

_anthropic_client: Anthropic | None = None
_deepseek_client: OpenAI | None = None


def _get_anthropic_client() -> Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _anthropic_client


def _get_deepseek_client() -> OpenAI:
    global _deepseek_client
    if _deepseek_client is None:
        api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
        _deepseek_client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    return _deepseek_client


DEFAULT_PROVIDER: Provider = "anthropic"
DEFAULT_MODEL_ID = (
    os.getenv("ANTHROPIC_MODEL")
    or os.getenv("DEEPSEEK_MODEL")
    or "claude-sonnet-4-20250514"
)
DEFAULT_TURN_DELAY_SECONDS = float(os.getenv("TURN_DELAY_SECONDS") or "0")
THOUGHTS_FILE = os.getenv("THOUGHTS_FILE", "/state/thoughts.json")
MAX_THOUGHTS_PER_PLAYER = int(os.getenv("MAX_THOUGHTS_PER_PLAYER", "80"))
_thoughts_lock = threading.Lock()


def _normalize_battle_tag(tag: str | None) -> str:
    if not tag:
        return ""
    return str(tag).lstrip(">").strip()


def _read_thoughts_payload() -> dict:
    try:
        with open(THOUGHTS_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
            if isinstance(payload, dict):
                return payload
    except Exception:
        pass
    return {"battle_tag": "", "updated_at": 0, "players": {}}


def _append_thought(
    *,
    battle_tag: str,
    player: str,
    action: str,
    reasoning: str,
    turn: int | None,
) -> None:
    if not reasoning:
        return

    clean_tag = _normalize_battle_tag(battle_tag)
    thought = {
        "timestamp": time.time(),
        "turn": turn,
        "action": action,
        "reasoning": reasoning.strip(),
    }

    with _thoughts_lock:
        payload = _read_thoughts_payload()
        if payload.get("battle_tag") != clean_tag:
            payload = {"battle_tag": clean_tag, "updated_at": 0, "players": {}}

        players = payload.setdefault("players", {})
        player_items = players.setdefault(player, [])
        player_items.append(thought)
        if len(player_items) > MAX_THOUGHTS_PER_PLAYER:
            players[player] = player_items[-MAX_THOUGHTS_PER_PLAYER:]

        payload["updated_at"] = time.time()

        os.makedirs(os.path.dirname(THOUGHTS_FILE), exist_ok=True)
        with open(THOUGHTS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)


def _pokemon_summary(pokemon: Pokemon, is_opponent: bool = False) -> str:
    types = "/".join(str(t) for t in pokemon.types if t is not None)
    hp = f"{pokemon.current_hp_fraction * 100:.0f}%"
    status = str(pokemon.status) if pokemon.status else "healthy"
    status_extra = ""
    if pokemon.status and pokemon.status_counter > 0:
        status_extra = f" (turn {pokemon.status_counter})"
    parts = [
        f"{pokemon.species} Lv{pokemon.level} ({types}) HP: {hp} Status: {status}{status_extra}"
    ]

    if pokemon.ability:
        parts.append(f"  Ability: {pokemon.ability}")
    if pokemon.item:
        parts.append(f"  Item: {pokemon.item}")

    boosts = {k: v for k, v in pokemon.boosts.items() if v != 0}
    if boosts:
        boost_str = " ".join(f"{k}:{v:+d}" for k, v in boosts.items())
        parts.append(f"  Boosts: {boost_str}")

    effects = pokemon.effects
    if effects:
        effect_names = [str(e).split(".")[-1].lower() for e in effects]
        parts.append(f"  Volatiles: {', '.join(effect_names)}")

    if pokemon.protect_counter > 0:
        parts.append(f"  Protect streak: {pokemon.protect_counter}")

    if pokemon.must_recharge:
        parts.append("  (must recharge next turn)")

    if pokemon.moves:
        known = [str(m) for m in pokemon.moves.values()]
        label = "Known moves" if is_opponent else "Moves"
        parts.append(f"  {label}: {', '.join(known)}")

    return "\n".join(parts)


def _move_summary(move: Move) -> str:
    accuracy = f"{move.accuracy}%" if move.accuracy is not True else "always hits"
    parts = [
        f"{move.id}: {move.type} | power {move.base_power} | {accuracy}",
        f"  PP: {move.current_pp}/{move.max_pp} | category: {move.category}",
    ]
    if move.priority != 0:
        parts[0] += f" | priority {move.priority}"

    extras = []
    if move.boosts:
        boost_parts = [f"{k}:{v:+d}" for k, v in move.boosts.items() if v != 0]
        if boost_parts:
            extras.append(f"stat changes: {' '.join(boost_parts)}")
    if move.heal:
        extras.append(f"heals {move.heal * 100:.0f}%")
    if move.recoil:
        extras.append(f"recoil {abs(move.recoil) * 100:.0f}%")
    if move.drain:
        extras.append(f"drains {move.drain * 100:.0f}%")
    if move.status:
        extras.append(f"inflicts {move.status}")
    secondary = getattr(move, "secondary", None)
    if secondary and isinstance(secondary, list):
        for sec in secondary:
            chance = sec.get("chance", 0)
            if sec.get("status"):
                extras.append(f"{chance}% {sec['status']}")
            if sec.get("boosts"):
                b = " ".join(f"{k}:{v:+d}" for k, v in sec["boosts"].items())
                extras.append(f"{chance}% {b}")
    if move.self_boost:
        sb_parts = [f"{k}:{v:+d}" for k, v in move.self_boost.items() if v != 0]
        if sb_parts:
            extras.append(f"self: {' '.join(sb_parts)}")
    if extras:
        parts.append(f"  Effects: {'; '.join(extras)}")

    return "\n".join(parts)


def _side_conditions_text(conditions: dict) -> str:
    if not conditions:
        return "none"
    parts = []
    for cond, val in conditions.items():
        name = str(cond).split(".")[-1].lower()
        if val > 1:
            parts.append(f"{name} x{val}")
        else:
            parts.append(name)
    return ", ".join(parts)


def _estimate_hazard_damage(pokemon: Pokemon, side_conditions: dict) -> float:
    """Rough % HP lost when switching in, based on your own side conditions."""
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

    if SideCondition.TOXIC_SPIKES in side_conditions and is_grounded:
        if PokemonType.POISON not in (pokemon.types or []):
            dmg += 0  # no direct damage, but will poison on entry
        # Poison types absorb toxic spikes — no damage either way.

    if SideCondition.STICKY_WEB in side_conditions and is_grounded:
        dmg += 0  # speed drop, no HP loss — but worth noting elsewhere

    return dmg


def format_battle_state(battle: Battle) -> str:
    """Build a text description of the current battle state for the LLM."""
    lines: list[str] = []

    lines.append(f"Turn {battle.turn}")

    lines.append("\n=== YOUR ACTIVE POKEMON ===")
    if battle.active_pokemon:
        lines.append(_pokemon_summary(battle.active_pokemon))

    lines.append("\n=== OPPONENT'S ACTIVE POKEMON ===")
    if battle.opponent_active_pokemon:
        lines.append(_pokemon_summary(battle.opponent_active_pokemon, is_opponent=True))

    lines.append("\n=== FIELD CONDITIONS ===")
    weather_list = list(battle.weather.keys()) if battle.weather else []
    lines.append(f"  Weather: {weather_list[0] if weather_list else 'none'}")
    field_list = list(battle.fields.keys()) if battle.fields else []
    lines.append(
        f"  Terrain: {', '.join(str(f) for f in field_list) if field_list else 'none'}"
    )
    lines.append(f"  Your side: {_side_conditions_text(battle.side_conditions)}")
    lines.append(
        f"  Opponent side: {_side_conditions_text(battle.opponent_side_conditions)}"
    )

    if getattr(battle, "trapped", False):
        lines.append("\n** You are TRAPPED and cannot switch! **")

    if battle.active_pokemon and battle.opponent_active_pokemon:
        own_spe = battle.active_pokemon.base_stats.get("spe", "?")
        opp_spe = battle.opponent_active_pokemon.base_stats.get("spe", "?")
        lines.append(f"\nBase Speed: yours={own_spe}  opponent={opp_spe}")

    choice_locked = (
        battle.active_pokemon
        and battle.active_pokemon.item
        and battle.active_pokemon.item.startswith("choice")
        and len(battle.available_moves) == 1
    )
    if choice_locked:
        lines.append(
            f"\n** Choice-locked into {battle.available_moves[0].id} "
            f"({battle.active_pokemon.item}) **"
        )

    lines.append("\n=== YOUR AVAILABLE MOVES ===")
    if battle.available_moves:
        for i, move in enumerate(battle.available_moves, 1):
            eff = ""
            if battle.opponent_active_pokemon and move.base_power > 0:
                try:
                    mult = battle.opponent_active_pokemon.damage_multiplier(move)
                    eff = f" | {mult}x vs {battle.opponent_active_pokemon.species}"
                except Exception:
                    pass
            lines.append(f"  move {i}: {_move_summary(move)}{eff}")
    else:
        lines.append("  (no moves available — you must switch)")

    lines.append("\n=== YOUR BENCH POKEMON (available switches) ===")
    if battle.available_switches:
        for i, pkmn in enumerate(battle.available_switches, 1):
            hazard_dmg = _estimate_hazard_damage(pkmn, battle.side_conditions)
            hazard_note = (
                f" [~{hazard_dmg:.0f}% hazard damage on switch-in]"
                if hazard_dmg > 0
                else ""
            )
            lines.append(f"  switch {i}: {_pokemon_summary(pkmn)}{hazard_note}")
    else:
        lines.append("  (no switches available)")

    opp_bench = [
        p for p in battle.opponent_team.values() if not p.fainted and not p.active
    ]
    if opp_bench:
        lines.append("\n=== OPPONENT'S REVEALED BENCH ===")
        for pkmn in opp_bench:
            lines.append(f"  {_pokemon_summary(pkmn, is_opponent=True)}")

    your_remaining = sum(1 for p in battle.team.values() if not p.fainted)
    opp_remaining = sum(1 for p in battle.opponent_team.values() if not p.fainted)
    lines.append(f"\nPokemon remaining: you={your_remaining}  opponent={opp_remaining}")

    lines.append("\n=== VALID ACTIONS ===")
    actions = []
    for i, move in enumerate(battle.available_moves, 1):
        actions.append(f"move {i} ({move.id})")
    for i, pkmn in enumerate(battle.available_switches, 1):
        actions.append(f"switch {i} ({pkmn.species})")
    lines.append(", ".join(actions) if actions else "(none)")

    return "\n".join(lines)


def parse_llm_action(response_text: str, battle: Battle) -> str | None:
    """
    Extract 'move N' or 'switch N' from the LLM response.
    Returns the raw action string or None if unparseable.
    """
    text = response_text.strip().lower()

    action_match = re.search(r"\baction:\s*(move|switch)\s+(\d+)", text)
    if not action_match:
        action_match = re.search(r"\b(move|switch)\s+(\d+)", text)

    if action_match:
        action_type = action_match.group(1)
        index = int(action_match.group(2)) - 1

        if action_type == "move" and 0 <= index < len(battle.available_moves):
            return f"move:{index}"
        if action_type == "switch" and 0 <= index < len(battle.available_switches):
            return f"switch:{index}"

    for i, move in enumerate(battle.available_moves):
        if move.id.lower() in text:
            return f"move:{i}"

    for i, pkmn in enumerate(battle.available_switches):
        if pkmn.species.lower() in text:
            return f"switch:{i}"

    return None


class LLMPlayer(Player):
    """
    A Pokemon battle agent powered by Claude.

    Each instance can have its own system prompt (personality) and model.
    Falls back to a random valid move if Claude's response can't be parsed.
    """

    def __init__(
        self,
        provider: Provider = DEFAULT_PROVIDER,
        system_prompt: str | None = None,
        model_id: str = DEFAULT_MODEL_ID,
        turn_delay_seconds: float | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._provider = provider
        self._model_id = model_id
        self._turn_delay_seconds = (
            DEFAULT_TURN_DELAY_SECONDS
            if turn_delay_seconds is None
            else turn_delay_seconds
        )
        self._system_prompt = system_prompt or (
            "You are a competitive Pokemon battle AI. Analyze the battle state "
            "and choose the best action. You must respond with exactly one action "
            "in the format: ACTION: move N  or  ACTION: switch N  (where N is the "
            "number from the list). Before the action, briefly explain your reasoning "
            "in 1-2 sentences."
        )
        self._turn_history: dict[str, list[dict]] = {}

    def _get_history(self, battle_tag: str) -> list[dict]:
        if battle_tag not in self._turn_history:
            self._turn_history[battle_tag] = []
        return self._turn_history[battle_tag]

    async def _anthropic_completion(self, messages: list[dict]) -> str:
        client = _get_anthropic_client()

        def _request() -> str:
            response = client.messages.create(
                model=self._model_id,
                max_tokens=150,
                system=self._system_prompt,
                messages=messages,
                tools=[
                    {
                        "name": "submit_action",
                        "description": "Submit the selected Pokemon battle action.",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "action_type": {
                                    "type": "string",
                                    "enum": ["move", "switch"],
                                },
                                "index": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": "1-based index from the valid actions list.",
                                },
                                "reasoning": {
                                    "type": "string",
                                    "description": "Short explanation for the chosen action.",
                                },
                            },
                            "required": ["action_type", "index", "reasoning"],
                            "additionalProperties": False,
                        },
                    }
                ],
                tool_choice={"type": "tool", "name": "submit_action"},
            )
            for block in response.content:
                if (
                    getattr(block, "type", None) == "tool_use"
                    and getattr(block, "name", None) == "submit_action"
                ):
                    payload = getattr(block, "input", {})
                    return json.dumps(payload)
            return ""

        return await asyncio.to_thread(_request)

    async def _deepseek_completion(self, messages: list[dict]) -> str:
        client = _get_deepseek_client()

        system = (
            self._system_prompt
            + "\n\nRespond ONLY with strict JSON: "
            + '{"action_type":"move|switch","index":<1-based integer>,"reasoning":"short explanation"}'
        )

        def _request() -> str:
            response = client.chat.completions.create(
                model=self._model_id or "deepseek-chat",
                messages=[
                    {"role": "system", "content": system},
                    *messages,
                ],
                temperature=0.2,
                max_tokens=220,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or ""
            return content.strip()

        return await asyncio.to_thread(_request)

    async def _completion(self, messages: list[dict]) -> str:
        if self._provider == "anthropic":
            return await self._anthropic_completion(messages)
        if self._provider == "deepseek":
            return await self._deepseek_completion(messages)
        raise ValueError(f"Unsupported provider: {self._provider}")

    async def choose_move(self, battle: Battle) -> str:
        state_text = format_battle_state(battle)
        history = self._get_history(battle.battle_tag)

        history.append({"role": "user", "content": state_text})

        # Keep conversation to last 10 turns to control token usage
        trimmed = history[-20:]

        try:
            reply = await self._completion(trimmed)
            print(
                f"  [{self.username}] {self._provider} action payload: {reply[:120]}...",
                flush=True,
            )

            history.append({"role": "assistant", "content": reply})

            action = None
            reasoning = ""
            try:
                payload = json.loads(reply)
                action_type = str(payload.get("action_type", "")).lower()
                index = int(payload.get("index", 0)) - 1
                reasoning = str(payload.get("reasoning", "")).strip()
                if action_type == "move" and 0 <= index < len(battle.available_moves):
                    action = f"move:{index}"
                elif action_type == "switch" and 0 <= index < len(
                    battle.available_switches
                ):
                    action = f"switch:{index}"
            except Exception:
                # Keep a resilient fallback in case provider behavior changes.
                action = parse_llm_action(reply, battle)
                if "ACTION:" in reply:
                    reasoning = reply.split("ACTION:", 1)[0].strip()

            if action:
                _append_thought(
                    battle_tag=battle.battle_tag,
                    player=self.username,
                    action=action.replace(":", " "),
                    reasoning=reasoning,
                    turn=getattr(battle, "turn", None),
                )
                if self._turn_delay_seconds > 0:
                    await asyncio.sleep(self._turn_delay_seconds)
                kind, idx = action.split(":")
                idx = int(idx)
                if kind == "move":
                    return self.create_order(battle.available_moves[idx])
                else:
                    return self.create_order(battle.available_switches[idx])

            print(
                f"  [{self.username}] Could not parse action, falling back to random",
                flush=True,
            )

        except Exception as e:
            print(
                f"  [{self.username}] {self._provider} API error: {e}, falling back to random",
                flush=True,
            )

        return self.choose_random_move(battle)

    def _battle_finished_callback(self, battle: Battle) -> None:
        self._turn_history.pop(battle.battle_tag, None)
