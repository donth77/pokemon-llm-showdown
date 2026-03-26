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
        _deepseek_client = OpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    return _deepseek_client


DEFAULT_PROVIDER: Provider = (os.getenv("LLM_PROVIDER") or "anthropic").lower()  # type: ignore[assignment]
DEFAULT_MODEL_ID = (
    os.getenv("LLM_MODEL")
    or os.getenv("ANTHROPIC_MODEL")
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
    parts = [f"{pokemon.species} ({types}) HP: {hp} Status: {status}"]

    if not is_opponent:
        boosts = {k: v for k, v in pokemon.boosts.items() if v != 0}
        if boosts:
            boost_str = " ".join(f"{k}:{v:+d}" for k, v in boosts.items())
            parts.append(f"  Boosts: {boost_str}")

    return "\n".join(parts)


def _move_summary(move: Move) -> str:
    accuracy = f"{move.accuracy}%" if move.accuracy is not True else "always hits"
    parts = [
        f"{move.id}: {move.type} | power {move.base_power} | {accuracy}",
        f"  PP: {move.current_pp}/{move.max_pp} | category: {move.category}",
    ]
    if move.priority != 0:
        parts[0] += f" | priority {move.priority}"
    return "\n".join(parts)


def format_battle_state(battle: Battle) -> str:
    """Build a text description of the current battle state for the LLM."""
    lines: list[str] = []

    lines.append("=== YOUR ACTIVE POKEMON ===")
    if battle.active_pokemon:
        lines.append(_pokemon_summary(battle.active_pokemon))

    lines.append("\n=== OPPONENT'S ACTIVE POKEMON ===")
    if battle.opponent_active_pokemon:
        lines.append(_pokemon_summary(battle.opponent_active_pokemon, is_opponent=True))

    lines.append("\n=== YOUR AVAILABLE MOVES ===")
    if battle.available_moves:
        for i, move in enumerate(battle.available_moves, 1):
            lines.append(f"  move {i}: {_move_summary(move)}")
    else:
        lines.append("  (no moves available — you must switch)")

    lines.append("\n=== YOUR BENCH POKEMON (available switches) ===")
    if battle.available_switches:
        for i, pkmn in enumerate(battle.available_switches, 1):
            lines.append(f"  switch {i}: {_pokemon_summary(pkmn)}")
    else:
        lines.append("  (no switches available)")

    if battle.weather:
        weather = list(battle.weather.keys())
        lines.append(f"\nWeather: {weather[0] if weather else 'none'}")
    if battle.fields:
        fields = list(battle.fields.keys())
        lines.append(f"Terrain/Fields: {', '.join(str(f) for f in fields)}")

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
        self._turn_delay_seconds = DEFAULT_TURN_DELAY_SECONDS if turn_delay_seconds is None else turn_delay_seconds
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
                                "action_type": {"type": "string", "enum": ["move", "switch"]},
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
                if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "submit_action":
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
            print(f"  [{self.username}] {self._provider} action payload: {reply[:120]}...", flush=True)

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
                elif action_type == "switch" and 0 <= index < len(battle.available_switches):
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

            print(f"  [{self.username}] Could not parse action, falling back to random", flush=True)

        except Exception as e:
            print(f"  [{self.username}] {self._provider} API error: {e}, falling back to random", flush=True)

        return self.choose_random_move(battle)

    def _battle_finished_callback(self, battle: Battle) -> None:
        self._turn_history.pop(battle.battle_tag, None)
