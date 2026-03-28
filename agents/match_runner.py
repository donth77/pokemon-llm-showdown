"""
Core battle execution for the queue worker (single match or series game).

Provides `run_single_match()` that takes a match config dict (from the manager
API) and returns a result dict, handling player creation, battle execution,
replay/log saving, and cleanup.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import json
from poke_env.player import Player
from poke_env.ps_client import AccountConfiguration, ServerConfiguration

from llm_player import LLMPlayer


# ---------------------------------------------------------------------------
# Config from environment (infrastructure only — no match-specific config)
# ---------------------------------------------------------------------------

SHOWDOWN_HOST = os.getenv("SHOWDOWN_HOST", "showdown")
SHOWDOWN_PORT = int(os.getenv("SHOWDOWN_PORT", "8000"))
WEB_HOST = os.getenv("WEB_HOST") or os.getenv("OVERLAY_HOST", "web")
WEB_PORT = int(os.getenv("WEB_PORT") or os.getenv("OVERLAY_PORT", "8080"))
REPLAY_DIR = Path(os.getenv("REPLAY_DIR", "/replays"))
LOG_DIR = Path(os.getenv("LOG_DIR", "/logs"))
LOG_RAW_BATTLE = (os.getenv("LOG_RAW_BATTLE") or "1").lower() not in ("0", "false", "no")
STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
CURRENT_BATTLE_FILE = STATE_DIR / "current_battle.json"
THOUGHTS_FILE = STATE_DIR / "thoughts.json"
TURN_DELAY_SECONDS = float(os.getenv("TURN_DELAY_SECONDS") or "0")

ALLOWED_PROVIDERS = {"anthropic", "deepseek", "openrouter"}
_MAX_USERNAME_LEN = 18


# ---------------------------------------------------------------------------
# Persona handling
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PersonaDefinition:
    slug: str
    name: str
    abbreviation: str
    description: str
    prompt_body: str
    sprite_url: str


PERSONAS_DIR = Path(__file__).resolve().parent / "personas"

ACTION_FORMAT_INSTRUCTIONS = (
    "Respond with strict JSON containing:\n"
    '- "action_type": "move" or "switch"\n'
    '- "index": 1-based action index from the valid actions list\n'
    '- "reasoning": 1-3 sentences in YOUR persona voice (first person, same tone and '
    "vocabulary as your character above — not a dry analyst or generic bullet list)\n"
    '- "callout": optional; usually leave empty. Short in-character phrase only on standout turns.\n'
    "Callout guidance:\n"
    "- Default to no callout (empty string) on most turns — roughly half or more should have none.\n"
    "- Reserve callouts for spikes: big damage, KOs, key switches, predicts, momentum swings, or "
    "last-stand moments.\n"
    "- Keep callouts fresh and varied; avoid repeating your recent phrasing.\n"
    "- You may direct callouts at your opponent using 'you' or their name.\n"
    "- Keep callouts concise and in-character, but not a copy of canned examples.\n"
    "- When you are clearly behind or about to lose, you may use a callout that shows "
    "weariness or exasperation in YOUR persona's style (still PG and sportsmanlike).\n"
)


def _parse_persona_markdown(markdown_text: str, *, source_path: Path) -> tuple[dict[str, str], str]:
    text = markdown_text.strip()
    if not text:
        raise ValueError(f"Persona file is empty: {source_path}")
    if not text.startswith("---\n"):
        return {}, text
    closing_idx = text.find("\n---\n", 4)
    if closing_idx == -1:
        raise ValueError(f"Persona front matter is missing closing '---' marker: {source_path}")
    front_matter_raw = text[4:closing_idx]
    body = text[closing_idx + len("\n---\n") :].strip()
    if not body:
        raise ValueError(f"Persona prompt body is empty: {source_path}")
    metadata: dict[str, str] = {}
    for line in front_matter_raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise ValueError(f"Invalid front matter line '{stripped}' in {source_path}")
        key, value = stripped.split(":", 1)
        metadata[key.strip().lower()] = value.strip()
    return metadata, body


def _safe_trainer_filename(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s or "/" in s or "\\" in s or s.startswith("."):
        return None
    base = Path(s).name
    if base != s:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", base):
        return None
    return base


def _resolve_persona_sprite_url(slug: str, metadata: dict[str, str]) -> str:
    raw_url = (metadata.get("sprite_url") or "").strip()
    if raw_url:
        lower = raw_url.lower()
        if lower.startswith("http://") or lower.startswith("https://"):
            return raw_url
        if raw_url.startswith("/"):
            return raw_url
    sprite_key = (metadata.get("sprite") or "").strip()
    safe_name = _safe_trainer_filename(sprite_key)
    if safe_name:
        return f"/static/trainers/{safe_name}"
    return f"/static/trainers/{slug}.png"


def load_persona(persona_slug: str) -> PersonaDefinition:
    slug = (persona_slug or "").strip().lower()
    if not slug:
        raise ValueError("Persona slug cannot be empty.")
    if not re.fullmatch(r"[a-z0-9_-]+", slug):
        raise ValueError(f"Invalid persona slug '{persona_slug}'.")
    persona_path = PERSONAS_DIR / f"{slug}.md"
    if not persona_path.exists():
        available = sorted(p.stem for p in PERSONAS_DIR.glob("*.md")) if PERSONAS_DIR.exists() else []
        raise ValueError(f"Persona '{slug}' not found. Available: {', '.join(available) or '(none)'}")
    text = persona_path.read_text(encoding="utf-8")
    metadata, prompt_body = _parse_persona_markdown(text, source_path=persona_path)
    display_name = metadata.get("name", slug.capitalize())
    abbr = (metadata.get("abbreviation") or "").strip() or display_name
    description = metadata.get("description", "")
    sprite_url = _resolve_persona_sprite_url(slug, metadata)
    return PersonaDefinition(
        slug=slug, name=display_name, abbreviation=abbr,
        description=description, prompt_body=prompt_body, sprite_url=sprite_url,
    )


def build_system_prompt(prompt_body: str, *, player_name: str, opponent_name: str) -> str:
    try:
        persona_prompt = prompt_body.format(player_name=player_name, opponent_name=opponent_name)
    except KeyError as e:
        raise ValueError(f"Unknown template variable in persona prompt: {e}") from e
    return f"{persona_prompt.strip()}\n\n{ACTION_FORMAT_INSTRUCTIONS}"


# ---------------------------------------------------------------------------
# Showdown helpers
# ---------------------------------------------------------------------------

def make_server_config() -> ServerConfiguration:
    return ServerConfiguration(
        f"ws://{SHOWDOWN_HOST}:{SHOWDOWN_PORT}/showdown/websocket",
        f"https://{SHOWDOWN_HOST}/",
    )


async def wait_for_showdown() -> None:
    url = f"http://{SHOWDOWN_HOST}:{SHOWDOWN_PORT}/"
    print(f"Waiting for Showdown at {url} ...", flush=True)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        print("Showdown is up!", flush=True)
                        return
            except Exception:
                pass
            await asyncio.sleep(2)


async def _disconnect_players(*players: Player | None) -> None:
    for p in players:
        if p is None:
            continue
        name = getattr(p, "username", "?")
        try:
            await p.ps_client.stop_listening()
            print(f"[cleanup] Disconnected {name}", flush=True)
        except Exception as e:
            print(f"[cleanup] Disconnect {name}: {e}", flush=True)


# ---------------------------------------------------------------------------
# Name building
# ---------------------------------------------------------------------------

def _make_player_name(_provider: str, _model_id: str, persona_name: str) -> str:
    """Showdown account username from persona display name (Showdown max 18)."""
    normalized_persona = re.sub(r"\s+", "", (persona_name or "").strip()) or "Persona"
    if len(normalized_persona) > _MAX_USERNAME_LEN:
        return normalized_persona[:_MAX_USERNAME_LEN]
    return normalized_persona


def _showdown_name_with_numeric_suffix(base: str, n: int) -> str:
    """Append n to base, trimming base so the total length is <= _MAX_USERNAME_LEN."""
    suffix = str(n)
    room = _MAX_USERNAME_LEN - len(suffix)
    if room < 1:
        return suffix[-_MAX_USERNAME_LEN:]
    trimmed = base[:room]
    return f"{trimmed}{suffix}"


def _normalize_showdown_override(name: str | None) -> str | None:
    if name is None:
        return None
    s = str(name).strip()
    if not s:
        return None
    if len(s) > _MAX_USERNAME_LEN:
        s = s[:_MAX_USERNAME_LEN]
    return s


def assign_distinct_showdown_pair(
    *,
    player1_provider: str,
    player1_model: str,
    p1_persona: PersonaDefinition,
    player2_provider: str,
    player2_model: str,
    p2_persona: PersonaDefinition,
) -> tuple[str, str]:
    """
    Two poke-env players cannot share the same Showdown username (|nametaken|).

    Same persona on both sides (or any collision after normalization) yields
    ``DamageDan1`` vs ``DamageDan2`` (matches tournament-style numbering).
    """
    n1 = _make_player_name(player1_provider, player1_model, p1_persona.name)
    n2 = _make_player_name(player2_provider, player2_model, p2_persona.name)
    if n1 == n2:
        base = n1
        n1 = _showdown_name_with_numeric_suffix(base, 1)
        n2 = _showdown_name_with_numeric_suffix(base, 2)
        print(
            f"[match] Showdown names collided; using distinct logins: {n1!r} vs {n2!r}",
            flush=True,
        )
    return n1, n2


# ---------------------------------------------------------------------------
# State / thoughts helpers
# ---------------------------------------------------------------------------

def write_current_battle_state(
    *,
    status: str,
    battle_tag: str | None = None,
    battle_format: str | None = None,
    player1_name: str | None = None,
    player2_name: str | None = None,
    player1_model_id: str | None = None,
    player2_model_id: str | None = None,
    player1_persona: PersonaDefinition | None = None,
    player2_persona: PersonaDefinition | None = None,
    series_snapshot: dict | None = None,
) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "status": status,
        "battle_tag": battle_tag,
        "battle_format": battle_format,
        "player1_name": player1_name,
        "player2_name": player2_name,
        "player1_model_id": player1_model_id,
        "player2_model_id": player2_model_id,
        "updated_at": time.time(),
    }
    if player1_persona is not None:
        payload["player1_persona_slug"] = player1_persona.slug
        payload["player1_sprite_url"] = player1_persona.sprite_url
    if player2_persona is not None:
        payload["player2_persona_slug"] = player2_persona.slug
        payload["player2_sprite_url"] = player2_persona.sprite_url
    if series_snapshot:
        payload["series_id"] = series_snapshot["series_id"]
        payload["series_best_of"] = series_snapshot["best_of"]
        payload["series_player1_wins"] = series_snapshot["player1_wins"]
        payload["series_player2_wins"] = series_snapshot["player2_wins"]
    CURRENT_BATTLE_FILE.write_text(json.dumps(payload))


def clear_thoughts_state(*, battle_tag: str | None = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"battle_tag": battle_tag, "updated_at": time.time(), "players": {}}
    THOUGHTS_FILE.write_text(json.dumps(payload))


async def _post_thoughts_clear() -> None:
    url = f"http://{WEB_HOST}:{WEB_PORT}/thoughts/clear"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, timeout=aiohttp.ClientTimeout(total=3)):
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Replay / log saving
# ---------------------------------------------------------------------------

def save_local_replay(player: Player, battle_tag: str) -> Path:
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    safe_tag = battle_tag.lstrip(">").replace("/", "_")
    output_path = REPLAY_DIR / f"{safe_tag}.html"
    player.save_replay(battle_tag, output_path)
    print(f"Saved local replay: {output_path}", flush=True)
    return output_path


def _extract_replay_log_lines(replay_path: Path) -> list[str]:
    try:
        html = replay_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    pattern = r"<script[^>]*class=[\"']battle-log-data[\"'][^>]*>(.*?)</script>"
    match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    return [line for line in match.group(1).strip().splitlines() if line.strip()]


def _extract_raw_log_lines(battle) -> list[str]:
    candidates = ["battle_log", "log", "_battle_log", "_log", "messages", "_messages"]
    for attr in candidates:
        if hasattr(battle, attr):
            val = getattr(battle, attr)
            if val is None:
                continue
            if isinstance(val, str):
                return [line for line in val.splitlines() if line.strip()]
            if isinstance(val, (list, tuple)):
                return [str(x) for x in val]
    return []


def save_raw_battle_log(
    battle,
    *,
    safe_tag: str,
    winner: str,
    loser: str,
    battle_format: str,
    model_id: str,
    provider: str,
    replay_path: Path,
) -> Path | None:
    if not LOG_RAW_BATTLE:
        return None
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    output_path = LOG_DIR / f"{safe_tag}.json"
    replay_lines = _extract_replay_log_lines(replay_path)
    extracted_raw_lines = _extract_raw_log_lines(battle)
    raw_lines = extracted_raw_lines or replay_lines
    payload = {
        "battle_tag": getattr(battle, "battle_tag", safe_tag),
        "timestamp": time.time(),
        "format": getattr(battle, "format", None),
        "battle_format": str(battle_format),
        "winner": winner,
        "loser": loser,
        "llm_provider": provider,
        "llm_model": model_id,
        "replay_html": replay_path.name,
        "raw_log_lines": raw_lines,
        "replay_log_lines": replay_lines,
    }
    output_path.write_text(json.dumps(payload, indent=2))
    print(f"Saved raw log: {output_path}", flush=True)
    return output_path


# ---------------------------------------------------------------------------
# Main match execution
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    winner: str
    loser: str
    winner_side: str  # "p1" or "p2"
    duration: float
    replay_file: str | None
    log_file: str | None
    battle_tag: str | None
    error: str | None = None


async def run_single_match(
    *,
    battle_format: str,
    player1_provider: str,
    player1_model: str,
    player1_persona_slug: str,
    player2_provider: str,
    player2_model: str,
    player2_persona_slug: str,
    player1_account_name: str | None = None,
    player2_account_name: str | None = None,
    series_snapshot: dict | None = None,
) -> MatchResult:
    """
    Run a single battle and return the result.

    This is the core execution function used by the queue worker.
    """
    server_config = make_server_config()
    p1_persona = load_persona(player1_persona_slug)
    p2_persona = load_persona(player2_persona_slug)
    o1 = _normalize_showdown_override(player1_account_name)
    o2 = _normalize_showdown_override(player2_account_name)
    if o1 and o2:
        player1_name, player2_name = o1, o2
        if player1_name == player2_name:
            player2_name = _showdown_name_with_numeric_suffix(player1_name, 2)
            print(
                f"[match] Manager Showdown names collided; using distinct logins: "
                f"{player1_name!r} vs {player2_name!r}",
                flush=True,
            )
        else:
            print(
                f"[match] Using manager Showdown logins: {player1_name!r} vs {player2_name!r}",
                flush=True,
            )
    else:
        player1_name, player2_name = assign_distinct_showdown_pair(
            player1_provider=player1_provider,
            player1_model=player1_model,
            p1_persona=p1_persona,
            player2_provider=player2_provider,
            player2_model=player2_model,
            p2_persona=p2_persona,
        )

    agent1: Player | None = None
    agent2: Player | None = None

    try:
        agent1 = LLMPlayer(
            account_configuration=AccountConfiguration(player1_name, None),
            server_configuration=server_config,
            battle_format=battle_format,
            max_concurrent_battles=1,
            save_replays=True,
            provider=player1_provider,
            model_id=player1_model,
            opponent_name=p2_persona.abbreviation,
            opponent_account_name=player2_name,
            turn_delay_seconds=TURN_DELAY_SECONDS,
            battle_side="p1",
            system_prompt=build_system_prompt(
                p1_persona.prompt_body,
                player_name=player1_name,
                opponent_name=p2_persona.abbreviation,
            ),
        )

        agent2 = LLMPlayer(
            account_configuration=AccountConfiguration(player2_name, None),
            server_configuration=server_config,
            battle_format=battle_format,
            max_concurrent_battles=1,
            save_replays=True,
            provider=player2_provider,
            model_id=player2_model,
            opponent_name=p1_persona.abbreviation,
            opponent_account_name=player1_name,
            turn_delay_seconds=TURN_DELAY_SECONDS,
            battle_side="p2",
            system_prompt=build_system_prompt(
                p2_persona.prompt_body,
                player_name=player2_name,
                opponent_name=p1_persona.abbreviation,
            ),
        )

        write_current_battle_state(
            status="starting",
            battle_format=battle_format,
            player1_name=player1_name,
            player2_name=player2_name,
            player1_model_id=player1_model,
            player2_model_id=player2_model,
            player1_persona=p1_persona,
            player2_persona=p2_persona,
            series_snapshot=series_snapshot,
        )
        clear_thoughts_state()
        await _post_thoughts_clear()

        match_start = time.time()
        known_tags = set(agent1.battles.keys())
        battle_task = asyncio.create_task(agent1.battle_against(agent2, n_battles=1))
        live_battle_tag: str | None = None

        while not battle_task.done():
            current_tags = set(agent1.battles.keys())
            new_tags = list(current_tags - known_tags)
            if new_tags and not live_battle_tag:
                live_battle_tag = new_tags[0].lstrip(">")
                write_current_battle_state(
                    status="live",
                    battle_tag=live_battle_tag,
                    battle_format=battle_format,
                    player1_name=player1_name,
                    player2_name=player2_name,
                    player1_model_id=player1_model,
                    player2_model_id=player2_model,
                    player1_persona=p1_persona,
                    player2_persona=p2_persona,
                    series_snapshot=series_snapshot,
                )
                print(f"Live battle detected: {live_battle_tag}", flush=True)
            await asyncio.sleep(0.3)

        await battle_task

        last_battle = list(agent1.battles.values())[-1]
        if last_battle.won:
            winner, loser = player1_name, player2_name
            winner_side = "p1"
        elif last_battle.lost:
            winner, loser = player2_name, player1_name
            winner_side = "p2"
        else:
            winner, loser = "Draw", "Draw"
            winner_side = "p1"

        match_duration = time.time() - match_start
        safe_tag = last_battle.battle_tag.lstrip(">").replace("/", "_")
        replay_path = save_local_replay(agent1, last_battle.battle_tag)
        log_path = save_raw_battle_log(
            last_battle,
            safe_tag=safe_tag,
            winner=winner,
            loser=loser,
            battle_format=battle_format,
            provider=f"{player1_provider}+{player2_provider}",
            model_id=f"p1={player1_model},p2={player2_model}",
            replay_path=replay_path,
        )

        write_current_battle_state(
            status="idle",
            battle_format=battle_format,
            player1_name=player1_name,
            player2_name=player2_name,
            player1_model_id=player1_model,
            player2_model_id=player2_model,
            player1_persona=p1_persona,
            player2_persona=p2_persona,
            series_snapshot=series_snapshot,
        )
        clear_thoughts_state()
        await _post_thoughts_clear()

        return MatchResult(
            winner=winner,
            loser=loser,
            winner_side=winner_side,
            duration=round(match_duration, 1),
            replay_file=replay_path.name,
            log_file=log_path.name if log_path else None,
            battle_tag=safe_tag,
        )

    except Exception as e:
        write_current_battle_state(
            status="error",
            battle_format=battle_format,
            player1_name=player1_name,
            player2_name=player2_name,
            player1_model_id=player1_model,
            player2_model_id=player2_model,
            player1_persona=p1_persona,
            player2_persona=p2_persona,
            series_snapshot=series_snapshot,
        )
        clear_thoughts_state()
        await _post_thoughts_clear()
        print(f"Error in match: {e}", flush=True)
        traceback.print_exc()
        return MatchResult(
            winner="", loser="", winner_side="",
            duration=0, replay_file=None, log_file=None,
            battle_tag=None, error=str(e),
        )

    finally:
        await _disconnect_players(agent1, agent2)
