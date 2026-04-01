"""
LLM-powered Pokémon battle player (Anthropic / DeepSeek / OpenRouter).

Sends the full battle state to the LLM each turn and parses back
a move or switch decision.
"""

import os
import re
import asyncio
import json
import time
import threading
from typing import Any, Literal

import httpx
import aiohttp
import requests
from anthropic import Anthropic
from openai import BadRequestError, OpenAI
from poke_env.player import Player
from poke_env.battle.move import Move, SPECIAL_MOVES
from poke_env.battle.pokemon import Pokemon
from poke_env.battle.abstract_battle import AbstractBattle as Battle
from poke_env.battle.pokemon_type import PokemonType
from poke_env.battle.side_condition import SideCondition

from pokedex import (
    lookup_move as _pdex_lookup_move,
    lookup_pokemon as _pdex_lookup_pokemon,
    lookup_type_matchup as _pdex_lookup_type_matchup,
    lookup_ability as _pdex_lookup_ability,
    lookup_item as _pdex_lookup_item,
    auto_enrich_battle_context,
    gen_from_format,
)
from provider_model_validate import validate_provider_model
from log_print import log_print
from env_bool import parse_env_bool
from agent_observability import append_agent_event


def _patch_poke_env_pseudo_move_entries() -> None:
    """Gen 1 requests use Showdown pseudo-move id fight (asleep/frozen); some poke-env
    releases omit it from Move.entry, so Move.__init__ hits max_pp → ValueError."""
    if getattr(Move, "_llm_showdown_entry_patch", False):
        return
    _orig = Move.entry.fget

    def _entry(self):  # noqa: ANN001
        if self._id in {"fight", "recharge"}:
            return {
                "pp": 1,
                "type": "normal",
                "category": "Special",
                "accuracy": 1,
                "flags": [],
            }
        return _orig(self)

    Move.entry = property(_entry)  # type: ignore[assignment]
    Move._llm_showdown_entry_patch = True  # type: ignore[attr-defined]


_patch_poke_env_pseudo_move_entries()

Provider = Literal["anthropic", "deepseek", "openrouter"]

_LLM_REQUEST_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=120.0,
    write=30.0,
    pool=30.0,
)


def _make_anthropic_client() -> Anthropic:
    return Anthropic(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        timeout=_LLM_REQUEST_TIMEOUT,
    )


def _make_deepseek_client() -> OpenAI:
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    return OpenAI(
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        timeout=_LLM_REQUEST_TIMEOUT,
    )


def _make_openrouter_client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("OPENROUTER_API_KEY"),
        base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        timeout=_LLM_REQUEST_TIMEOUT,
        default_headers={
            "HTTP-Referer": "https://github.com/donth77/pokemon-llm-showdown",
            "X-Title": "Pokemon LLM Showdown",
        },
    )


def _openrouter_api_base_for_rest() -> str:
    return os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")


# JSON ``reasoning`` field: shared by OpenRouter schema, chat templates, Anthropic tool schema.
_REASONING_JSON_VALUE_HELP = (
    "Short spectator blurb only: at most 3 sentences and about 60 words total, first person, "
    "persona voice. Do not put long analysis, damage math, type charts, or multi-paragraph "
    "simulations in this field — decide privately, then summarize the choice briefly. "
    "Plain text only (no markdown). Direct prose only — no leading labels or headings."
)


def _openrouter_structured_output_mode() -> str:
    """auto | force | off — whether to use json_schema when the model supports it."""
    return (os.getenv("OPENROUTER_STRUCTURED_OUTPUTS") or "auto").strip().lower()


# OpenRouter GET /models?supported_parameters=structured_outputs — cached for process lifetime.
_openrouter_structured_ids: set[str] | None = None
_openrouter_structured_ids_attempted: bool = False
_openrouter_structured_ids_lock = threading.Lock()
# Models that returned 400 on json_schema this run (API list can be wrong or route-specific).
_openrouter_structured_deny: set[str] = set()
_openrouter_structured_deny_lock = threading.Lock()


def _openrouter_fetch_structured_model_ids() -> set[str] | None:
    """Return model ids that advertise structured_outputs support, or None if the fetch failed."""
    api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        return None
    url = f"{_openrouter_api_base_for_rest()}/models?supported_parameters=structured_outputs"
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=25,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        log_print(
            f"  [openrouter] Structured-output model list unavailable ({e}); "
            "using json_object",
            flush=True,
        )
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return set()
    out: set[str] = set()
    for m in data:
        if isinstance(m, dict) and m.get("id"):
            out.add(str(m["id"]))
    return out


def _openrouter_get_structured_model_id_set() -> set[str] | None:
    global _openrouter_structured_ids, _openrouter_structured_ids_attempted
    with _openrouter_structured_ids_lock:
        if _openrouter_structured_ids_attempted:
            return _openrouter_structured_ids
    fetched = _openrouter_fetch_structured_model_ids()
    with _openrouter_structured_ids_lock:
        if not _openrouter_structured_ids_attempted:
            _openrouter_structured_ids = fetched
            _openrouter_structured_ids_attempted = True
    return _openrouter_structured_ids


def _openrouter_wants_json_schema(model_id: str) -> bool:
    mode = _openrouter_structured_output_mode()
    if mode in ("off", "0", "false", "no"):
        return False
    with _openrouter_structured_deny_lock:
        denied = model_id in _openrouter_structured_deny
    if denied and mode not in ("force", "on", "1", "true", "yes"):
        return False
    if mode in ("force", "on", "1", "true", "yes"):
        return True
    ids = _openrouter_get_structured_model_id_set()
    if not ids:
        return False
    return model_id in ids


def _openrouter_battle_response_format(use_json_schema: bool) -> dict:
    if not use_json_schema:
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "pokemon_battle_action",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "action_type": {
                        "type": "string",
                        "enum": ["move", "switch"],
                    },
                    "index": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "1-based index from the valid actions list for this turn."
                        ),
                    },
                    "reasoning": {
                        "type": "string",
                        "description": _REASONING_JSON_VALUE_HELP,
                    },
                    "callout": {
                        "type": "string",
                        "description": "Short in-character phrase or empty string.",
                    },
                },
                "required": [
                    "action_type",
                    "index",
                    "reasoning",
                    "callout",
                ],
                "additionalProperties": False,
            },
        },
    }


WEB_HOST = os.getenv("WEB_HOST") or os.getenv("OVERLAY_HOST", "web")
WEB_PORT = int(os.getenv("WEB_PORT") or os.getenv("OVERLAY_PORT", "8080"))

DEFAULT_PROVIDER: Provider = "anthropic"
DEFAULT_TURN_DELAY_SECONDS = float(os.getenv("TURN_DELAY_SECONDS") or "0")
# Cap on model output per turn (tool JSON, chat JSON, OpenRouter completions).
# Reasoning/thinking on some providers counts toward this budget — keep high enough to
# avoid truncated JSON (default 4096 when env unset).
_LLM_MAX_OUTPUT_TOKENS = int(
    os.getenv("LLM_MAX_OUTPUT_TOKENS")
    or os.getenv("CHAT_COMPLETION_MAX_TOKENS")
    or "4096"
)

THOUGHTS_FILE = os.getenv("THOUGHTS_FILE", "/state/thoughts.json")
MAX_THOUGHTS_PER_PLAYER = int(os.getenv("MAX_THOUGHTS_PER_PLAYER", "80"))
_thoughts_lock = threading.Lock()

# Hard ceiling: even if httpx read-timeout doesn't fire (e.g. keepalive bytes drip in),
# abort the entire _completion coroutine after this many seconds.
_LLM_TURN_TIMEOUT = float(os.getenv("LLM_TURN_TIMEOUT") or "150")
_LLM_MEMORY_REFLECTION_MAX_TOKENS = int(
    os.getenv("LLM_MEMORY_REFLECTION_MAX_TOKENS") or "2048"
)

_POKEDEX_TOOL_ENABLED = parse_env_bool("POKEDEX_TOOL_ENABLED", default=False)
_POKEDEX_AUTO_ENRICH = parse_env_bool("POKEDEX_AUTO_ENRICH", default=False)
_POKEDEX_MAX_LOOKUPS = int(os.getenv("POKEDEX_MAX_LOOKUPS") or "3")


def _normalize_battle_tag(tag: str | None) -> str:
    if not tag:
        return ""
    return str(tag).lstrip(">").strip()


def _clip_obs_text(text: str | None, max_len: int) -> str:
    if not text:
        return ""
    t = str(text).strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


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
    callout: str,
    turn: int | None,
    event_kind: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    clean_tag = _normalize_battle_tag(battle_tag)
    thought: dict[str, Any] = {
        "timestamp": time.time(),
        "turn": turn,
        "action": action,
        "reasoning": (reasoning or "").strip(),
        "callout": (callout or "").strip(),
    }
    if event_kind:
        thought["event_kind"] = event_kind
    if detail:
        thought["detail"] = detail

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


def _recent_callouts(
    *,
    battle_tag: str,
    player: str,
    limit: int = 5,
) -> list[str]:
    clean_tag = _normalize_battle_tag(battle_tag)
    if not clean_tag or not player:
        return []
    payload = _read_thoughts_payload()
    if payload.get("battle_tag") != clean_tag:
        return []
    players = payload.get("players", {})
    items = players.get(player, []) if isinstance(players, dict) else []
    callouts: list[str] = []
    for thought in reversed(items if isinstance(items, list) else []):
        callout = str((thought or {}).get("callout", "")).strip()
        if callout:
            callouts.append(callout)
        if len(callouts) >= limit:
            break
    return callouts


async def _post_thought_to_overlay(
    *,
    player: str,
    action: str,
    reasoning: str,
    callout: str,
    turn: int | None,
    battle_side: str | None = None,
    event_kind: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    url = f"http://{WEB_HOST}:{WEB_PORT}/thought"
    payload: dict[str, object] = {
        "player": player,
        "action": action,
        "reasoning": reasoning,
        "callout": callout,
        "turn": turn,
        "timestamp": time.time(),
    }
    if battle_side in ("p1", "p2"):
        payload["battle_side"] = battle_side
    if event_kind:
        payload["event_kind"] = event_kind
    if detail:
        payload["detail"] = detail
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=3)
            ):
                pass
    except Exception as e:
        log_print(f"  [web] Failed to post thought: {e}", flush=True)


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


def _safe_move_attr(move: Move, name: str) -> Any:
    """poke-env Move properties index movedex entries; missing/null fields (e.g. Gen 1) can raise."""
    try:
        return getattr(move, name)
    except Exception:
        return None


def _move_summary(move: Move) -> str:
    accuracy = f"{move.accuracy}%" if move.accuracy is not True else "always hits"
    parts = [
        f"{move.id}: {move.type} | power {move.base_power} | {accuracy}",
        f"  PP: {move.current_pp}/{move.max_pp} | category: {move.category}",
    ]
    # Pseudo-moves (e.g. gen1 "recharge") omit movedex keys like "priority".
    try:
        prio = int(move.priority)
    except Exception:
        prio = 0
    if prio != 0:
        parts[0] += f" | priority {prio}"

    extras = []
    boosts = _safe_move_attr(move, "boosts")
    if boosts:
        boost_parts = [f"{k}:{v:+d}" for k, v in boosts.items() if v != 0]
        if boost_parts:
            extras.append(f"stat changes: {' '.join(boost_parts)}")
    heal = _safe_move_attr(move, "heal")
    if heal:
        try:
            extras.append(f"heals {float(heal) * 100:.0f}%")
        except (TypeError, ValueError):
            pass
    recoil = _safe_move_attr(move, "recoil")
    if recoil:
        try:
            extras.append(f"recoil {abs(float(recoil)) * 100:.0f}%")
        except (TypeError, ValueError):
            pass
    drain = _safe_move_attr(move, "drain")
    if drain:
        try:
            extras.append(f"drains {float(drain) * 100:.0f}%")
        except (TypeError, ValueError):
            pass
    status = _safe_move_attr(move, "status")
    if status:
        extras.append(f"inflicts {status}")
    secondary = getattr(move, "secondary", None)
    if secondary and isinstance(secondary, list):
        for sec in secondary:
            chance = sec.get("chance", 0)
            if sec.get("status"):
                extras.append(f"{chance}% {sec['status']}")
            if sec.get("boosts"):
                b = " ".join(f"{k}:{v:+d}" for k, v in sec["boosts"].items())
                extras.append(f"{chance}% {b}")
    self_boost = _safe_move_attr(move, "self_boost")
    if self_boost:
        sb_parts = [f"{k}:{v:+d}" for k, v in self_boost.items() if v != 0]
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


def _opponent_unseen_bench_count(battle: Battle) -> int | None:
    """How many of the opponent's team have never been sent in (not in opponent_team).

    poke-env only registers opponent Pokémon after they appear in the protocol;
    |teamsize| gives their total roster size for this battle.
    """
    if battle.teampreview:
        return None
    opp_role = battle.opponent_role
    if not opp_role:
        return None
    sizes = getattr(battle, "_team_size", None) or {}
    total = sizes.get(opp_role)
    if total is None:
        return None
    known = len(battle.opponent_team)
    return max(0, int(total) - known)


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

    unseen_bench = _opponent_unseen_bench_count(battle)
    if unseen_bench is not None:
        lines.append(
            f"\nOpponent unseen bench (never sent in, identity unknown): {unseen_bench}"
        )

    your_remaining = sum(1 for p in battle.team.values() if not p.fainted)
    opp_remaining = sum(1 for p in battle.opponent_team.values() if not p.fainted)
    lines.append(
        "\nRemaining non-fainted Pokémon: "
        f"you={your_remaining}  opponent={opp_remaining} "
        "(opponent: revealed / entered battle only; excludes unseen bench)"
    )

    lines.append("\n=== VALID ACTIONS ===")
    actions = []
    for i, move in enumerate(battle.available_moves, 1):
        actions.append(f"move {i} ({move.id})")
    for i, pkmn in enumerate(battle.available_switches, 1):
        actions.append(f"switch {i} ({pkmn.species})")
    lines.append(", ".join(actions) if actions else "(none)")

    return "\n".join(lines)


# Protocol lines poke-env stores in `_replay_data` even when it skips them in `parse_message`
# (e.g. `-fail`, `-immune`). Surfaced to the LLM as human-readable failed-outcome hints.
_FAILED_OUTCOME_PROTOCOL_KINDS = frozenset({"-fail", "-immune", "-miss"})
_FAIL_STATUS_LABELS: dict[str, str] = {
    "brn": "already burned",
    "par": "already paralyzed",
    "slp": "already asleep",
    "frz": "already frozen",
    "psn": "already poisoned",
    "tox": "already badly poisoned",
}
_MAX_FAILED_OUTCOME_BULLETS = 3


def _protocol_battle_side(slot_ident: str) -> str | None:
    s = (slot_ident or "").strip()
    if len(s) >= 2 and s.startswith("p") and s[1] in "12":
        return s[:2]
    return None


def _pretty_protocol_subject(raw: str) -> str:
    raw = (raw or "").strip()
    if ":" in raw:
        slot, name = raw.split(":", 1)
        return f"{slot.strip()} ({name.strip()})"
    return raw


def _format_failed_outcome_line(ev: list[str], my_role: str | None) -> str | None:
    if len(ev) < 2 or ev[1] not in _FAILED_OUTCOME_PROTOCOL_KINDS:
        return None
    kind = ev[1]

    if kind == "-miss":
        if not my_role or len(ev) < 3:
            return None
        atk_side = _protocol_battle_side(ev[2])
        if atk_side != my_role:
            return None
        attacker = _pretty_protocol_subject(ev[2])
        target = _pretty_protocol_subject(ev[3]) if len(ev) > 3 else "the target"
        return f"Your Pokémon's attack missed ({attacker} → {target})."

    if kind == "-immune":
        if len(ev) < 3:
            return None
        subj = _pretty_protocol_subject(ev[2])
        extra = " ".join(x.strip() for x in ev[3:] if x.strip())
        if extra:
            return f"{subj}: immune — the attack had no effect ({extra})."
        return f"{subj}: immune — the attack had no effect."

    if kind == "-fail":
        if len(ev) < 3:
            return None
        subj = _pretty_protocol_subject(ev[2])
        if len(ev) == 3:
            return f"{subj}: a move or effect failed."
        reason = (ev[3] or "").strip()
        label = _FAIL_STATUS_LABELS.get(reason)
        if label:
            return f"{subj}: {label} (no additional effect from this attempt)."
        rest = " ".join(x.strip() for x in ev[3:] if x.strip())
        if rest.startswith("[from]"):
            return f"{subj}: failed ({rest})."
        if rest:
            return f"{subj}: failed — {rest}."
        return f"{subj}: a move or effect failed."

    return None


def _consume_new_failed_outcomes(
    battle: Battle,
    start_idx: int,
    turn_before_slice: int | None,
) -> tuple[str, int, int | None]:
    """Humanized failed-outcome lines for new protocol rows, new cursor, last |turn| in slice."""
    data = getattr(battle, "_replay_data", None)
    if not isinstance(data, list):
        return "", start_idx, turn_before_slice
    n = len(data)
    my_role = battle.player_role
    if start_idx >= n:
        return "", n, turn_before_slice

    replay_turn = turn_before_slice
    bullets: list[str] = []
    seen: set[str] = set()
    for ev in data[start_idx:n]:
        if len(bullets) >= _MAX_FAILED_OUTCOME_BULLETS:
            break
        if not isinstance(ev, list) or len(ev) < 2:
            continue
        if ev[1] == "turn" and len(ev) >= 3:
            try:
                replay_turn = int(ev[2])
            except (TypeError, ValueError):
                pass
            continue
        line = _format_failed_outcome_line(ev, my_role)
        if not line:
            continue
        if replay_turn is not None:
            display = f"[turn {replay_turn}] {line}"
        else:
            display = line
        if display not in seen:
            seen.add(display)
            bullets.append(display)

    if not bullets:
        return "", n, replay_turn
    block = (
        "=== FAILED OR NO-EFFECT OUTCOMES (since your last decision) ===\n"
        + "\n".join(f"  • {b}" for b in bullets)
    )
    return block, n, replay_turn


def _parse_json_action_payload(reply: str) -> tuple[dict | None, str]:
    """
    Many chat models return JSON wrapped in markdown fences or with leading prose.
    Prefer the last JSON object that looks like a battle action (move/switch + index)
    so chatty models can narrate first and append JSON at the end.
    """
    text = (reply or "").strip()
    if not text:
        return None, ""

    if text.startswith("```"):
        lines = text.split("\n")
        if lines:
            lines = lines[1:]
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    decoder = json.JSONDecoder()
    brace_positions = [i for i, ch in enumerate(text) if ch == "{"]

    def decode_dict_at(i: int) -> tuple[dict, int] | None:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            return None
        if isinstance(obj, dict):
            return obj, end
        return None

    for i in reversed(brace_positions):
        got = decode_dict_at(i)
        if got is None:
            continue
        obj, end = got
        if _json_payload_looks_like_battle_action(obj):
            suf = text[end:].strip()[:4000]
            return obj, suf

    for i in brace_positions:
        got = decode_dict_at(i)
        if got is None:
            continue
        obj, end = got
        suf = text[end:].strip()[:4000]
        return obj, suf

    return None, ""


def _coerce_action_index(raw: object) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def _normalize_json_action_type(raw: object) -> str:
    s = str(raw or "").strip().lower()
    if s in ("move", "switch"):
        return s
    if s in ("moves",):
        return "move"
    if s in ("switches", "swap"):
        return "switch"
    return ""


def _json_payload_looks_like_battle_action(obj: dict) -> bool:
    at = _normalize_json_action_type(obj.get("action_type") or obj.get("actionType"))
    if at not in ("move", "switch"):
        return False
    return _coerce_action_index(obj.get("index")) is not None


def _action_string_from_json_fields(
    action_type: str,
    raw_idx: int | None,
    battle: Battle,
) -> str | None:
    """Map action_type + index (1-based or 0 for first slot) to poke-env order string."""
    if not action_type or raw_idx is None:
        return None
    if raw_idx >= 1:
        index = raw_idx - 1
    elif raw_idx == 0:
        index = 0
    else:
        return None
    if action_type == "move" and 0 <= index < len(battle.available_moves):
        return f"move:{index}"
    if action_type == "switch" and 0 <= index < len(battle.available_switches):
        return f"switch:{index}"
    return None


def _regex_extract_action_fields(text: str) -> tuple[str, int] | None:
    """
    Recover action_type and index when JSON is truncated (verbose reasoning + low
    max_tokens) or otherwise invalid for json.loads/raw_decode.
    """
    if not text:
        return None
    at = re.search(
        r'["\']action_type["\']\s*:\s*["\'](move|switch)["\']',
        text,
        re.I,
    )
    if not at:
        at = re.search(
            r'["\']actionType["\']\s*:\s*["\'](move|switch)["\']',
            text,
            re.I,
        )
    idx_m = re.search(r'["\']index["\']\s*:\s*(\d+)', text)
    if not at or not idx_m:
        return None
    return (at.group(1).lower(), int(idx_m.group(1)))


def _preamble_reasoning_from_reply(reply: str, max_chars: int = 4000) -> str:
    """
    Reasoning models often write analysis before the JSON object. The JSON may still
    carry an empty "reasoning" field — use the leading prose for overlay / thoughts.json.
    """
    text = (reply or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :].strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    brace = text.find("{")
    if brace < 0:
        return text[:max_chars].strip()
    if brace == 0:
        return ""
    return text[:brace].strip()[:max_chars]


def _openrouter_message_text_chunks(msg: dict) -> list[str]:
    """Collect visible strings from an OpenAI-style chat message object."""
    out: list[str] = []
    # Reasoning models often put assistant ``content`` first (strict JSON) and chain-of-thought
    # in ``reasoning``. Merge reasoning first so ``_preamble_reasoning_from_reply`` sees prose
    # before the opening ``{``.
    r = msg.get("reasoning")
    if isinstance(r, str) and r.strip():
        out.append(r.strip())
    c = msg.get("content")
    if isinstance(c, str) and c.strip():
        out.append(c.strip())
    elif isinstance(c, list):
        for part in c:
            if not isinstance(part, dict):
                continue
            t = part.get("text")
            if t is None:
                t = part.get("content")
            if isinstance(t, str) and t.strip():
                out.append(t.strip())
    return out


def _openrouter_reasoning_detail_chunks(item: object, depth: int = 0) -> list[str]:
    """Flatten OpenRouter choice.reasoning_details entries to strings."""
    if depth > 14:
        return []
    out: list[str] = []
    if isinstance(item, str) and item.strip():
        return [item.strip()]
    if not isinstance(item, dict):
        return out
    for key in ("text", "content", "summary", "data", "encrypted_text"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
        elif isinstance(v, list):
            for sub in v:
                out.extend(_openrouter_reasoning_detail_chunks(sub, depth + 1))
        elif isinstance(v, dict):
            out.extend(_openrouter_reasoning_detail_chunks(v, depth + 1))
    return out


def _openrouter_raw_response_text(data: dict) -> tuple[str, str]:
    """
    Merge message.content, message.reasoning, and choice.reasoning_details into one
    string for JSON parsing. Reasoning models on OpenRouter often leave content empty;
    the OpenAI SDK may drop reasoning_details when building the typed response.
    """
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or json.dumps(err)
        code = err.get("code", "")
        raise RuntimeError(f"OpenRouter API error ({code}): {msg}")
    if isinstance(err, str) and err.strip():
        raise RuntimeError(f"OpenRouter API error: {err}")

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", ""

    choice = choices[0]
    if not isinstance(choice, dict):
        return "", ""

    finish = str(choice.get("finish_reason") or "")
    parts: list[str] = []
    msg = choice.get("message")
    if isinstance(msg, dict):
        parts.extend(_openrouter_message_text_chunks(msg))

    rd = choice.get("reasoning_details")
    if isinstance(rd, list):
        for item in rd:
            parts.extend(_openrouter_reasoning_detail_chunks(item))

    seen: set[str] = set()
    uniq: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return "\n".join(uniq), finish


_openrouter_legacy_extra_body_warned = False


def _openrouter_default_extra_body() -> dict | None:
    """Base ``extra_body`` for every OpenRouter request (``OPENROUTER_DEFAULT_EXTRA_BODY_JSON``)."""
    global _openrouter_legacy_extra_body_warned
    raw = os.getenv("OPENROUTER_DEFAULT_EXTRA_BODY_JSON", "").strip()
    if not raw:
        legacy = os.getenv("OPENROUTER_EXTRA_BODY_JSON", "").strip()
        if legacy:
            if not _openrouter_legacy_extra_body_warned:
                _openrouter_legacy_extra_body_warned = True
                log_print(
                    "  [openrouter] OPENROUTER_EXTRA_BODY_JSON is deprecated; "
                    "use OPENROUTER_DEFAULT_EXTRA_BODY_JSON",
                    flush=True,
                )
            raw = legacy
    if not raw:
        return None
    try:
        extra = json.loads(raw)
    except json.JSONDecodeError:
        log_print(
            "  [openrouter] Invalid OPENROUTER_DEFAULT_EXTRA_BODY_JSON, ignoring",
            flush=True,
        )
        return None
    return extra if isinstance(extra, dict) else None


def _openrouter_extra_body_by_model_map() -> dict[str, dict[str, Any]]:
    """
    Per-model ``extra_body`` fragments keyed by OpenRouter model id (case-insensitive).
    From ``OPENROUTER_EXTRA_BODY_BY_MODEL_JSON``.
    """
    raw = os.getenv("OPENROUTER_EXTRA_BODY_BY_MODEL_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log_print(
            "  [openrouter] Invalid OPENROUTER_EXTRA_BODY_BY_MODEL_JSON, ignoring",
            flush=True,
        )
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in data.items():
        if not isinstance(v, dict):
            log_print(
                f"  [openrouter] OPENROUTER_EXTRA_BODY_BY_MODEL_JSON: "
                f"skip non-object value for {k!r}",
                flush=True,
            )
            continue
        out[str(k).strip().lower()] = v
    return out


def _openrouter_resolve_extra_body(model_id: str) -> dict | None:
    """
    Merge OpenRouter ``extra_body``: ``OPENROUTER_DEFAULT_EXTRA_BODY_JSON`` then
    model-specific overrides from ``OPENROUTER_EXTRA_BODY_BY_MODEL_JSON`` (shallow merge;
    per-model keys replace default keys entirely so ``reasoning`` is not partially mixed).
    """
    default = _openrouter_default_extra_body()
    spec = _openrouter_extra_body_by_model_map().get((model_id or "").strip().lower())
    if spec is None:
        return default
    if not default:
        return dict(spec)
    out: dict[str, Any] = dict(default)
    out.update(spec)
    return out


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


_SUBMIT_ACTION_TOOL = {
    "name": "submit_action",
    "description": "Submit the selected Pokémon battle action.",
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
                "description": _REASONING_JSON_VALUE_HELP,
            },
            "callout": {
                "type": "string",
                "description": (
                    "Usually empty. Only on standout turns: short phrase — "
                    "taunt, quip, or battle cry (otherwise omit or use empty string)."
                ),
            },
        },
        "required": ["action_type", "index", "reasoning"],
        "additionalProperties": False,
    },
}

_POKEDEX_TOOLS = [
    {
        "name": "pokedex_lookup_move",
        "description": (
            "Look up full details for a move: type, power, accuracy, effects, description."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "move_id": {
                    "type": "string",
                    "description": "Move identifier (e.g. 'thunderbolt', 'stealthrock').",
                },
            },
            "required": ["move_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pokedex_lookup_pokemon",
        "description": ("Look up a Pokémon's base stats, typing, and abilities."),
        "input_schema": {
            "type": "object",
            "properties": {
                "species": {
                    "type": "string",
                    "description": "Species name (e.g. 'charizard', 'ferrothorn').",
                },
            },
            "required": ["species"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pokedex_lookup_type_matchup",
        "description": (
            "Check type effectiveness: attacking type vs one or two defending types."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attacking_type": {
                    "type": "string",
                    "description": "Attacking type (e.g. 'Fire', 'Electric').",
                },
                "defending_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One or two defending types (e.g. ['Grass', 'Steel']).",
                },
            },
            "required": ["attacking_type", "defending_types"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pokedex_lookup_ability",
        "description": "Look up what an ability does.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ability_id": {
                    "type": "string",
                    "description": "Ability identifier (e.g. 'intimidate', 'drizzle').",
                },
            },
            "required": ["ability_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pokedex_lookup_item",
        "description": "Look up what a held item does.",
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "Item identifier (e.g. 'choiceband', 'leftovers').",
                },
            },
            "required": ["item_id"],
            "additionalProperties": False,
        },
    },
]


def _dispatch_pokedex_tool(tool_name: str, tool_input: dict, gen: int) -> str:
    """Execute a Pokédex tool call and return the result string."""
    if tool_name == "pokedex_lookup_move":
        return _pdex_lookup_move(tool_input.get("move_id", ""), gen)
    if tool_name == "pokedex_lookup_pokemon":
        return _pdex_lookup_pokemon(tool_input.get("species", ""), gen)
    if tool_name == "pokedex_lookup_type_matchup":
        return _pdex_lookup_type_matchup(
            tool_input.get("attacking_type", ""),
            tool_input.get("defending_types", []),
            gen,
        )
    if tool_name == "pokedex_lookup_ability":
        return _pdex_lookup_ability(tool_input.get("ability_id", ""))
    if tool_name == "pokedex_lookup_item":
        return _pdex_lookup_item(tool_input.get("item_id", ""))
    return f"Unknown tool: {tool_name}"


async def reflection_json_completion(
    *,
    provider: Provider,
    model_id: str,
    system: str,
    user: str,
    max_tokens: int | None = None,
) -> str:
    """
    One-shot LLM call (no tools). Returns assistant text expected to be a JSON object.
    Used for post-match persona memory / learnings updates.
    """
    mt = (
        int(max_tokens) if max_tokens is not None else _LLM_MEMORY_REFLECTION_MAX_TOKENS
    )
    if provider == "anthropic":
        client = _make_anthropic_client()

        def _req() -> str:
            response = client.messages.create(
                model=model_id,
                max_tokens=mt,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            parts: list[str] = []
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    parts.append(getattr(block, "text", "") or "")
            return "".join(parts).strip()

        return await asyncio.wait_for(
            asyncio.to_thread(_req), timeout=_LLM_TURN_TIMEOUT
        )

    if provider in ("deepseek", "openrouter"):
        if provider == "deepseek":
            client = _make_deepseek_client()
        else:
            client = _make_openrouter_client()

        def _req() -> str:
            extra = (
                _openrouter_resolve_extra_body(model_id)
                if provider == "openrouter"
                else None
            )
            kwargs: dict = dict(
                model=model_id,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                max_tokens=mt,
                response_format={"type": "json_object"},
            )
            if extra:
                kwargs["extra_body"] = extra
            response = client.chat.completions.create(**kwargs)
            return (response.choices[0].message.content or "").strip()

        return await asyncio.wait_for(
            asyncio.to_thread(_req), timeout=_LLM_TURN_TIMEOUT
        )

    raise ValueError(f"Unsupported provider for reflection: {provider}")


class LLMPlayer(Player):
    """
    A Pokémon battle agent powered by Claude.

    Each instance can have its own system prompt (personality) and model.
    Falls back to a random valid move if Claude's response can't be parsed.
    """

    def __init__(
        self,
        provider: Provider = DEFAULT_PROVIDER,
        system_prompt: str | None = None,
        opponent_name: str | None = None,
        opponent_account_name: str | None = None,
        model_id: str = "",
        turn_delay_seconds: float | None = None,
        battle_side: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        # Gen 1 only: Showdown sends pseudo-move id "fight" (frozen/asleep Fight button).
        # poke-env's SPECIAL_MOVES usually omits it until upstream releases; add when needed.
        _fmt = kwargs.get("battle_format")
        if _fmt is not None and gen_from_format(str(_fmt)) == 1:
            SPECIAL_MOVES.add("fight")
        self._provider = provider
        raw_mid = (model_id or "").strip()
        if not raw_mid:
            raise ValueError(
                f"Empty model_id for LLMPlayer (provider={provider!r}, "
                f"user={getattr(self, 'username', '?')!r}). Set model in the manager."
            )
        validate_provider_model(
            provider,
            raw_mid,
            field_label=getattr(self, "username", "player"),
        )
        self._model_id = raw_mid
        self._llm_client = self._create_llm_client()
        self._turn_delay_seconds = (
            DEFAULT_TURN_DELAY_SECONDS
            if turn_delay_seconds is None
            else turn_delay_seconds
        )
        self._opponent_name = (opponent_name or "").strip()
        # Showdown username; thoughts.json keys callouts by this, not persona abbreviation.
        self._opponent_account_name = (
            opponent_account_name or opponent_name or ""
        ).strip()
        self._system_prompt = system_prompt or (
            "You are a competitive Pokémon battle AI. Analyze the battle state "
            "and choose the best action. You must respond with exactly one action "
            "in the format: ACTION: move N  or  ACTION: switch N  (where N is the "
            "number from the list). Before the action, briefly explain your reasoning "
            "in 1-2 sentences."
        )
        self._turn_history: dict[str, list[dict]] = {}
        # battle_tag -> (exclusive replay index, last |turn| N at that cursor)
        self._failed_outcome_state: dict[str, tuple[int, int | None]] = {}
        self._battle_side = battle_side if battle_side in ("p1", "p2") else None
        self._current_gen: int = 8

        pdex_flags = []
        if _POKEDEX_TOOL_ENABLED and self._provider == "anthropic":
            pdex_flags.append(f"tool_calling (max {_POKEDEX_MAX_LOOKUPS} lookups/turn)")
        if _POKEDEX_AUTO_ENRICH:
            pdex_flags.append("auto_enrich")
        if pdex_flags:
            log_print(
                f"  [{self.username}] Pokédex enabled: {', '.join(pdex_flags)}",
                flush=True,
            )

    def _create_llm_client(self) -> Anthropic | OpenAI:
        if self._provider == "anthropic":
            return _make_anthropic_client()
        if self._provider == "deepseek":
            return _make_deepseek_client()
        if self._provider == "openrouter":
            return _make_openrouter_client()
        raise ValueError(f"Unsupported provider: {self._provider}")

    def _get_history(self, battle_tag: str) -> list[dict]:
        if battle_tag not in self._turn_history:
            self._turn_history[battle_tag] = []
        return self._turn_history[battle_tag]

    def _callout_context_text(self, battle: Battle) -> str:
        battle_tag = _normalize_battle_tag(getattr(battle, "battle_tag", ""))
        if not battle_tag:
            return ""
        my_recent = _recent_callouts(
            battle_tag=battle_tag, player=self.username, limit=5
        )
        opp_recent = (
            _recent_callouts(
                battle_tag=battle_tag, player=self._opponent_account_name, limit=2
            )
            if self._opponent_account_name
            else []
        )
        if not my_recent and not opp_recent:
            return ""
        lines = ["=== CALLOUT CONTEXT ==="]
        if my_recent:
            lines.append(
                "Your recent callouts (avoid reusing these exact lines): "
                + " | ".join(my_recent)
            )
        if opp_recent:
            lines.append(
                f"{self._opponent_name}'s recent callouts (react to this rivalry when natural): "
                + " | ".join(opp_recent)
            )
        return "\n".join(lines)

    async def _anthropic_completion(self, messages: list[dict]) -> str:
        client = self._llm_client
        use_pokedex = _POKEDEX_TOOL_ENABLED and self._provider == "anthropic"
        gen = self._current_gen

        def _request() -> str:
            tools = (
                list(_POKEDEX_TOOLS) + [_SUBMIT_ACTION_TOOL]
                if use_pokedex
                else [_SUBMIT_ACTION_TOOL]
            )
            tool_choice = (
                {"type": "auto"}
                if use_pokedex
                else {"type": "tool", "name": "submit_action"}
            )
            conv = list(messages)
            lookups_done = 0

            for _ in range(_POKEDEX_MAX_LOOKUPS + 2):
                response = client.messages.create(
                    model=self._model_id,
                    max_tokens=_LLM_MAX_OUTPUT_TOKENS,
                    system=self._system_prompt,
                    messages=conv,
                    tools=tools,
                    tool_choice=tool_choice,
                )

                tool_use_blocks = [
                    b
                    for b in response.content
                    if getattr(b, "type", None) == "tool_use"
                ]

                submit_block = next(
                    (b for b in tool_use_blocks if b.name == "submit_action"),
                    None,
                )
                if submit_block:
                    return json.dumps(getattr(submit_block, "input", {}))

                pokedex_block = next(
                    (b for b in tool_use_blocks if b.name.startswith("pokedex_")),
                    None,
                )
                if not pokedex_block or not use_pokedex:
                    return ""

                result_text = _dispatch_pokedex_tool(
                    pokedex_block.name,
                    getattr(pokedex_block, "input", {}),
                    gen,
                )
                lookups_done += 1
                log_print(
                    f"  [{self.username}] Pokédex lookup #{lookups_done}: "
                    f"{pokedex_block.name}({getattr(pokedex_block, 'input', {})}) "
                    f"-> {result_text[:120]}",
                    flush=True,
                )

                conv.append({"role": "assistant", "content": response.content})
                conv.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": pokedex_block.id,
                                "content": result_text,
                            }
                        ],
                    }
                )

                if lookups_done >= _POKEDEX_MAX_LOOKUPS:
                    tool_choice = {"type": "tool", "name": "submit_action"}
                    log_print(
                        f"  [{self.username}] Pokédex lookup limit reached "
                        f"({_POKEDEX_MAX_LOOKUPS}), forcing submit_action",
                        flush=True,
                    )

            return ""

        return await asyncio.to_thread(_request)

    async def _deepseek_completion(self, messages: list[dict]) -> str:
        client = self._llm_client

        system = (
            self._system_prompt
            + "\n\nRespond ONLY with strict JSON: "
            + '{"action_type":"move|switch","index":<1-based integer>,'
            + '"reasoning":"<brief in-character summary, see rules below>",'
            + '"callout":"usually empty; short phrase only on standout moments"}\n\n'
            "The reasoning string must stay short: about 60 words max, 3 sentences max — a quick "
            "in-character summary only. Never dump calculations, type-chart essays, or full turn "
            "projections into JSON; do that mentally, then compress. No leading labels (e.g. "
            "do not start with Reasoning: or similar)."
        )

        def _request() -> str:
            response = client.chat.completions.create(
                model=self._model_id or "deepseek-chat",
                messages=[
                    {"role": "system", "content": system},
                    *messages,
                ],
                temperature=0.2,
                max_tokens=_LLM_MAX_OUTPUT_TOKENS,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or ""
            return content.strip()

        return await asyncio.to_thread(_request)

    async def _openrouter_completion(self, messages: list[dict]) -> str:
        client = self._llm_client

        system = (
            self._system_prompt
            + "\n\nRespond ONLY with strict JSON: "
            + '{"action_type":"move|switch","index":<1-based integer>,'
            + '"reasoning":"<brief in-character summary>",'
            + '"callout":"usually empty; short phrase only on standout moments"}\n\n'
            "Hard rules: Output only that JSON object (no text before { or after }). "
            "Reasoning value: plain text, no markdown — at most ~60 words / 3 sentences; "
            "no damage math or long simulations (summarize only). Direct prose only; "
            "do not prefix with Reasoning: or any other label."
        )

        def _request() -> str:
            extra = _openrouter_resolve_extra_body(self._model_id)
            raw_api = getattr(client.chat.completions, "with_raw_response", None)
            for attempt in range(2):
                use_schema = (
                    _openrouter_wants_json_schema(self._model_id) and attempt == 0
                )
                kwargs: dict = dict(
                    model=self._model_id,
                    messages=[
                        {"role": "system", "content": system},
                        *messages,
                    ],
                    temperature=0.2,
                    max_tokens=_LLM_MAX_OUTPUT_TOKENS,
                    response_format=_openrouter_battle_response_format(use_schema),
                )
                if extra:
                    kwargs["extra_body"] = extra

                try:
                    if raw_api is not None:
                        raw = raw_api.create(**kwargs)
                        try:
                            data = raw.http_response.json()
                        except Exception:
                            parsed = raw.parse()
                            return (parsed.choices[0].message.content or "").strip()
                        text, finish = _openrouter_raw_response_text(data)
                        if len(text) < 60:
                            hint = (
                                "tune OPENROUTER_DEFAULT_EXTRA_BODY_JSON or "
                                "OPENROUTER_EXTRA_BODY_BY_MODEL_JSON (reasoning.max_tokens vs effort)"
                            )
                            log_print(
                                f"  [openrouter] short merged reply (len={len(text)}, "
                                f"finish_reason={finish!r}, model={data.get('model')!r}); "
                                f"{hint}",
                                flush=True,
                            )
                        if finish == "length":
                            log_print(
                                f"  [openrouter] finish_reason=length (truncated); "
                                f"raise LLM_MAX_OUTPUT_TOKENS (max_tokens={_LLM_MAX_OUTPUT_TOKENS}) "
                                f"if JSON is incomplete (len={len(text)})",
                                flush=True,
                            )
                        if not text.strip():
                            model = data.get("model", "")
                            choice0 = (data.get("choices") or [{}])[0]
                            ckeys = (
                                list(choice0.keys())
                                if isinstance(choice0, dict)
                                else []
                            )
                            log_print(
                                f"  [openrouter] No assistant text in raw response "
                                f"(finish_reason={finish!r}, model={model!r}, choice_keys={ckeys})",
                                flush=True,
                            )
                        return text.strip()

                    response = client.chat.completions.create(**kwargs)
                    return (response.choices[0].message.content or "").strip()
                except BadRequestError as e:
                    if use_schema and attempt == 0:
                        with _openrouter_structured_deny_lock:
                            _openrouter_structured_deny.add(self._model_id)
                        log_print(
                            f"  [openrouter] json_schema rejected for "
                            f"{self._model_id!r}; falling back to json_object ({e})",
                            flush=True,
                        )
                        continue
                    raise

        return await asyncio.to_thread(_request)

    async def _completion(self, messages: list[dict]) -> str:
        return await asyncio.wait_for(
            self._provider_completion(messages), timeout=_LLM_TURN_TIMEOUT
        )

    async def _provider_completion(self, messages: list[dict]) -> str:
        if self._provider == "anthropic":
            return await self._anthropic_completion(messages)
        if self._provider == "deepseek":
            return await self._deepseek_completion(messages)
        if self._provider == "openrouter":
            return await self._openrouter_completion(messages)
        raise ValueError(f"Unsupported provider: {self._provider}")

    async def _emit_choose_move_issue(
        self,
        *,
        battle: Battle,
        kind: Literal["parse_failure", "llm_error"],
        reply: str | None,
        exc: BaseException | None = None,
    ) -> None:
        """Record parse/API failures in thoughts.json, agent_events JSONL, and the web overlay."""
        turn = getattr(battle, "turn", None)
        bt_norm = _normalize_battle_tag(battle.battle_tag)
        preview = _clip_obs_text(reply, 500)
        detail: dict[str, Any] = {
            "battle_tag": bt_norm,
            "turn": turn,
            "provider": self._provider,
            "player": self.username,
        }
        if reply is not None:
            detail["reply_len"] = len(reply)
            if preview:
                detail["reply_preview"] = preview
        if exc is not None:
            detail["error_class"] = type(exc).__name__
            detail["error_message"] = _clip_obs_text(str(exc), 800)

        if kind == "parse_failure":
            action_label = "[parse_failure]"
            reasoning = (
                f"Parse failure — could not map model output to a legal move/switch "
                f"(reply_len={len(reply) if reply is not None else 0}). "
                f"Snippet: {_clip_obs_text(preview, 220)}"
            )
        else:
            action_label = "[llm_error]"
            em = detail.get("error_message", "unknown error")
            ec = detail.get("error_class", "Error")
            reasoning = f"LLM request failed ({ec}): {_clip_obs_text(str(em), 400)}"

        append_agent_event({"scope": "choose_move", "event": kind, **detail})

        _append_thought(
            battle_tag=battle.battle_tag,
            player=self.username,
            action=action_label,
            reasoning=reasoning,
            callout="",
            turn=turn,
            event_kind=kind,
            detail=detail,
        )
        await _post_thought_to_overlay(
            player=self.username,
            action=action_label,
            reasoning=reasoning,
            callout="",
            turn=turn,
            battle_side=self._battle_side,
            event_kind=kind,
            detail=detail,
        )

    async def choose_move(self, battle: Battle) -> str:
        fmt = (
            getattr(battle, "format", None)
            or getattr(battle, "battle_format", "")
            or ""
        )
        if fmt:
            self._current_gen = gen_from_format(str(fmt))

        tag = battle.battle_tag
        prev_i, turn_ctx = self._failed_outcome_state.get(tag, (0, None))
        failed_outcomes_block, new_i, turn_ctx = _consume_new_failed_outcomes(
            battle, prev_i, turn_ctx
        )
        self._failed_outcome_state[tag] = (new_i, turn_ctx)

        state_text = format_battle_state(battle)
        if failed_outcomes_block:
            state_text = f"{failed_outcomes_block}\n\n{state_text}"
        callout_context = self._callout_context_text(battle)
        if callout_context:
            state_text = f"{state_text}\n\n{callout_context}"

        if _POKEDEX_AUTO_ENRICH:
            enrich = auto_enrich_battle_context(battle, gen=self._current_gen)
            if enrich:
                note_count = enrich.count("\n")
                log_print(
                    f"  [{self.username}] Pokédex auto-enrich: "
                    f"{note_count} notes injected",
                    flush=True,
                )
                state_text = f"{state_text}\n\n{enrich}"

        history = self._get_history(battle.battle_tag)

        history.append({"role": "user", "content": state_text})

        # Keep conversation to last 10 turns to control token usage
        trimmed = history[-20:]

        try:
            reply = await self._completion(trimmed)
        except Exception as e:
            log_print(
                f"  [{self.username}] {self._provider} API error: {e}, falling back to random",
                flush=True,
            )
            await self._emit_choose_move_issue(
                battle=battle,
                kind="llm_error",
                reply=None,
                exc=e,
            )
            return self.choose_random_move(battle)

        history.append({"role": "assistant", "content": reply})

        try:
            action = None
            reasoning = ""
            callout = ""
            payload, json_trailing = _parse_json_action_payload(reply)
            if isinstance(payload, dict):
                action_type = _normalize_json_action_type(
                    payload.get("action_type") or payload.get("actionType")
                )
                raw_idx = _coerce_action_index(payload.get("index"))
                reasoning = str(payload.get("reasoning", "")).strip()
                callout = str(payload.get("callout", "")).strip()
                action = _action_string_from_json_fields(action_type, raw_idx, battle)

            if not action:
                reg = _regex_extract_action_fields(reply)
                if reg:
                    rt, ri = reg
                    action = _action_string_from_json_fields(rt, ri, battle)

            if not action:
                action = parse_llm_action(reply, battle)
                if "ACTION:" in reply:
                    reasoning = reply.split("ACTION:", 1)[0].strip()

            if action and not (reasoning or "").strip():
                reasoning = _preamble_reasoning_from_reply(reply)
            if action and not (reasoning or "").strip() and json_trailing:
                reasoning = json_trailing

            if action:
                rp = (reasoning or "").strip()
                rp_show = (rp[:100] + "…") if len(rp) > 100 else rp
                co = (callout or "").strip()
                co_part = f" callout={co!r}" if co else ""
                log_print(
                    f"  [{self.username}] {self._provider} choice: {action} "
                    f"reasoning={rp_show!r}{co_part}",
                    flush=True,
                )
                if self._turn_delay_seconds > 0:
                    await asyncio.sleep(self._turn_delay_seconds)
                _append_thought(
                    battle_tag=battle.battle_tag,
                    player=self.username,
                    action=action.replace(":", " "),
                    reasoning=reasoning,
                    callout=callout,
                    turn=getattr(battle, "turn", None),
                )
                await _post_thought_to_overlay(
                    player=self.username,
                    action=action.replace(":", " "),
                    reasoning=reasoning,
                    callout=callout,
                    turn=getattr(battle, "turn", None),
                    battle_side=self._battle_side,
                )
                kind, idx = action.split(":")
                idx = int(idx)
                if kind == "move":
                    return self.create_order(battle.available_moves[idx])
                else:
                    return self.create_order(battle.available_switches[idx])

            preview = reply[:200] + ("…" if len(reply) > 200 else "")
            log_print(
                f"  [{self.username}] Could not parse action (reply_len={len(reply)}), "
                f"preview={preview!r}, falling back to random",
                flush=True,
            )
            await self._emit_choose_move_issue(
                battle=battle,
                kind="parse_failure",
                reply=reply,
                exc=None,
            )
        except Exception as e:
            log_print(
                f"  [{self.username}] {self._provider} error while applying action: {e}, "
                f"falling back to random",
                flush=True,
            )
            await self._emit_choose_move_issue(
                battle=battle,
                kind="parse_failure",
                reply=reply,
                exc=e,
            )

        return self.choose_random_move(battle)

    def _battle_finished_callback(self, battle: Battle) -> None:
        self._turn_history.pop(battle.battle_tag, None)
        self._failed_outcome_state.pop(battle.battle_tag, None)
