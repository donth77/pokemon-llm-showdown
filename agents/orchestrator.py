"""
Battle orchestrator — runs one (or many) gen8randombattle matches.

It runs two LLM-powered players, reporting results to the overlay.
"""

import asyncio
from dataclasses import dataclass
import os
import time
import traceback
import re
import random
from pathlib import Path
import json
import aiohttp
from poke_env.player import Player
from poke_env.ps_client import AccountConfiguration, ServerConfiguration

from llm_player import LLMPlayer
from max_damage_player import MaxDamagePlayer
from smart_player import SmartPlayer

SHOWDOWN_HOST = os.getenv("SHOWDOWN_HOST", "showdown")
SHOWDOWN_PORT = int(os.getenv("SHOWDOWN_PORT", "8000"))
OVERLAY_HOST = os.getenv("OVERLAY_HOST", "overlay")
OVERLAY_PORT = int(os.getenv("OVERLAY_PORT", "8080"))
DELAY_BETWEEN_MATCHES = float(os.getenv("DELAY_BETWEEN_MATCHES") or "15")
# MATCH_COUNT: N>0 = exactly N matches then exit; N<=0 = run until the process stops.
# Note: int(os.getenv("MATCH_COUNT") or "1") breaks MATCH_COUNT=0 (0 is falsy).
_mc_raw = os.getenv("MATCH_COUNT")
if _mc_raw is None or not str(_mc_raw).strip():
    MATCH_COUNT = 1
else:
    _mc_stripped = str(_mc_raw).strip().strip('"').strip("'")
    MATCH_COUNT = int(_mc_stripped)


def _required_env(name: str) -> str:
    val = os.getenv(name)
    if val is None or not str(val).strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return str(val).strip()


@dataclass(frozen=True)
class PersonaDefinition:
    slug: str
    name: str
    abbreviation: str
    description: str
    prompt_body: str
    # Resolved URL for overlay trainer art (path or absolute http(s) URL).
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
    """Parse optional YAML-like front matter and markdown body."""
    text = markdown_text.strip()
    if not text:
        raise ValueError(f"Persona file is empty: {source_path}")

    if not text.startswith("---\n"):
        return {}, text

    closing_idx = text.find("\n---\n", 4)
    if closing_idx == -1:
        raise ValueError(
            f"Persona front matter is missing closing '---' marker: {source_path}"
        )

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


def _available_persona_slugs() -> list[str]:
    if not PERSONAS_DIR.exists():
        return []
    return sorted(path.stem for path in PERSONAS_DIR.glob("*.md"))


def _safe_trainer_filename(raw: str) -> str | None:
    """Plain filename only (no path segments), safe for /static/trainers/{name}."""
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
        raise ValueError(
            f"Invalid persona slug '{persona_slug}'. Use only lowercase letters, "
            "numbers, '-' or '_'."
        )

    persona_path = PERSONAS_DIR / f"{slug}.md"
    if not persona_path.exists():
        available = ", ".join(_available_persona_slugs()) or "(none found)"
        raise ValueError(
            f"Persona '{slug}' not found at {persona_path}. "
            f"Available personas: {available}"
        )

    text = persona_path.read_text(encoding="utf-8")
    metadata, prompt_body = _parse_persona_markdown(text, source_path=persona_path)
    display_name = metadata.get("name", slug.capitalize())
    abbr = (metadata.get("abbreviation") or "").strip() or display_name
    description = metadata.get("description", "")
    sprite_url = _resolve_persona_sprite_url(slug, metadata)
    return PersonaDefinition(
        slug=slug,
        name=display_name,
        abbreviation=abbr,
        description=description,
        prompt_body=prompt_body,
        sprite_url=sprite_url,
    )


def build_system_prompt(prompt_body: str, *, player_name: str, opponent_name: str) -> str:
    try:
        persona_prompt = prompt_body.format(
            player_name=player_name, opponent_name=opponent_name
        )
    except KeyError as e:
        raise ValueError(f"Unknown template variable in persona prompt: {e}") from e
    return f"{persona_prompt.strip()}\n\n{ACTION_FORMAT_INSTRUCTIONS}"


# Per-player provider/model overrides (required).
# Persona defaults can be overridden via PLAYER1_PERSONA/PLAYER2_PERSONA.
#
# Example: DeepSeek-Aggro vs Claude-Stall
#   PLAYER1_PROVIDER=deepseek
#   PLAYER1_MODEL=deepseek-chat
#   PLAYER2_PROVIDER=anthropic
#   PLAYER2_MODEL=claude-3-5-sonnet-latest
#   PLAYER1_PERSONA=aggro
#   PLAYER2_PERSONA=stall
PLAYER1_PROVIDER = _required_env("PLAYER1_PROVIDER").lower()
PLAYER1_MODEL = _required_env("PLAYER1_MODEL")
PLAYER2_PROVIDER = _required_env("PLAYER2_PROVIDER").lower()
PLAYER2_MODEL = _required_env("PLAYER2_MODEL")
PLAYER1_PERSONA = (os.getenv("PLAYER1_PERSONA") or "aggro").strip()
PLAYER2_PERSONA = (os.getenv("PLAYER2_PERSONA") or "stall").strip()

# Validate provider values early so a misconfig fails fast.
_ALLOWED_PROVIDERS = {"anthropic", "deepseek", "openrouter"}
if PLAYER1_PROVIDER not in _ALLOWED_PROVIDERS:
    raise ValueError(f"PLAYER1_PROVIDER must be one of {sorted(_ALLOWED_PROVIDERS)}")
if PLAYER2_PROVIDER and PLAYER2_PROVIDER not in _ALLOWED_PROVIDERS:
    raise ValueError(f"PLAYER2_PROVIDER must be one of {sorted(_ALLOWED_PROVIDERS)}")

def _short_model_name(provider: str, model_id: str) -> str:
    """Derive a short display name from provider + model for Showdown usernames."""
    if provider == "deepseek":
        return "DeepSeek"
    if provider == "anthropic":
        return "Claude"
    if provider == "openrouter":
        # "meta-llama/llama-3.1-8b-instruct" -> "llama-3.1-8b-instruct"
        raw = model_id.split("/")[-1] if "/" in model_id else model_id
        return raw or "OpenRouter"
    return provider.capitalize()


# Showdown enforces a max username length of 18 characters.
_MAX_USERNAME_LEN = 18


def _make_player_name(provider: str, model_id: str, persona_name: str, include_provider: bool = False) -> str:
    """Build a Showdown-safe player name like 'gemini-2.0-Aggro'."""
    base = _short_model_name(provider, model_id)
    # Preserve persona display casing from front matter; remove spaces for usernames.
    normalized_persona = re.sub(r"\s+", "", (persona_name or "").strip()) or "Persona"
    
    if include_provider:
         full = f"{base}-{normalized_persona}"
    else:
        full = f"{normalized_persona}"

    if len(full) > _MAX_USERNAME_LEN:
        return full[:_MAX_USERNAME_LEN]
    return full


BATTLE_FORMAT_POOL = os.getenv(
    "BATTLE_FORMAT_POOL", "gen8randombattle,gen9randombattle"
)
REPLAY_DIR = Path(os.getenv("REPLAY_DIR", "/replays"))
LOG_DIR = Path(os.getenv("LOG_DIR", "/logs"))
LOG_RAW_BATTLE = (os.getenv("LOG_RAW_BATTLE") or "1").lower() not in (
    "0",
    "false",
    "no",
)
STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
CURRENT_BATTLE_FILE = STATE_DIR / "current_battle.json"
THOUGHTS_FILE = STATE_DIR / "thoughts.json"


def make_server_config() -> ServerConfiguration:
    return ServerConfiguration(
        f"ws://{SHOWDOWN_HOST}:{SHOWDOWN_PORT}/showdown/websocket",
        f"https://{SHOWDOWN_HOST}/",  # unused — no auth
    )


def _format_pool() -> list[str]:
    items = [x.strip() for x in BATTLE_FORMAT_POOL.split(",") if x.strip()]
    normalized: list[str] = []
    for item in items:
        # common typo guard
        normalized.append(item.replace("randombatttle", "randombattle"))
    return normalized or ["gen8randombattle"]


async def _disconnect_players(*players: Player | None) -> None:
    """
    Close Showdown WebSockets so the next match can log in with the same names.

    Without this, a second player using the same username can hit |nametaken|
    or leave the server thinking the user is still connected, so challenges stall.
    """
    for p in players:
        if p is None:
            continue
        name = getattr(p, "username", "?")
        try:
            await p.ps_client.stop_listening()
            print(f"[cleanup] Disconnected {name}", flush=True)
        except Exception as e:
            print(f"[cleanup] Disconnect {name}: {e}", flush=True)


async def wait_for_showdown() -> None:
    """Block until the Showdown websocket is accepting connections."""
    url = f"http://{SHOWDOWN_HOST}:{SHOWDOWN_PORT}/"
    print(f"Waiting for Showdown at {url} ...", flush=True)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        print("Showdown is up!", flush=True)
                        return
            except Exception:
                pass
            await asyncio.sleep(2)


async def report_result(
    winner: str,
    loser: str,
    *,
    battle_format: str = "",
    duration: float = 0,
) -> None:
    """POST a match result to the overlay service."""
    url = f"http://{OVERLAY_HOST}:{OVERLAY_PORT}/result"
    payload = {
        "winner": winner,
        "loser": loser,
        "timestamp": time.time(),
        "battle_format": battle_format,
        "duration": round(duration, 1),
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    print(f"Reported result: {winner} beat {loser}", flush=True)
                else:
                    print(f"Overlay returned {resp.status}", flush=True)
    except Exception as e:
        print(f"Failed to report result: {e}", flush=True)


def save_local_replay(player: Player, battle_tag: str) -> Path:
    """Export replay HTML to the mounted replay directory."""
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    safe_tag = battle_tag.lstrip(">").replace("/", "_")
    output_path = REPLAY_DIR / f"{safe_tag}.html"
    player.save_replay(battle_tag, output_path)
    print(f"Saved local replay: {output_path}", flush=True)
    return output_path


def _extract_replay_log_lines(replay_path: Path) -> list[str]:
    """
    Extract canonical protocol stream from replay HTML as a list of lines.

    Looks for: <script type="text/plain" class="battle-log-data"> ... </script>
    """
    try:
        html = replay_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    pattern = (
        r"<script[^>]*class=[\"']battle-log-data[\"'][^>]*>"
        r"(.*?)"
        r"</script>"
    )
    match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return []

    raw_text = match.group(1).strip()
    lines = [line for line in raw_text.splitlines() if line.strip()]
    return lines


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
) -> None:
    """Write current live battle state for overlay/broadcast page."""
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
    CURRENT_BATTLE_FILE.write_text(json.dumps(payload))


def clear_thoughts_state(*, battle_tag: str | None = None) -> None:
    """Reset per-player thought feed between battles."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "battle_tag": battle_tag,
        "updated_at": time.time(),
        "players": {},
    }
    THOUGHTS_FILE.write_text(json.dumps(payload))


async def _post_thoughts_clear() -> None:
    url = f"http://{OVERLAY_HOST}:{OVERLAY_PORT}/thoughts/clear"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, timeout=aiohttp.ClientTimeout(total=3)
            ):
                pass
    except Exception:
        pass


def _extract_raw_log_lines(battle) -> list[str]:
    """
    Best-effort extraction of raw log/protocol lines from a poke-env battle object.
    poke-env versions expose different attributes; we try a few common ones.
    """
    candidates = [
        "battle_log",
        "log",
        "_battle_log",
        "_log",
        "messages",
        "_messages",
    ]
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
    # Fallback: poke-env doesn't always expose raw protocol logs on the battle object.
    # The replay HTML contains the canonical `battle-log-data` stream, so use it
    # if extracted logs are empty.
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


async def main() -> None:
    await wait_for_showdown()

    server_config = make_server_config()
    battle_formats = _format_pool()
    player1_persona = load_persona(PLAYER1_PERSONA)
    player2_persona = load_persona(PLAYER2_PERSONA)

    # Names are tied to persona; derived from provider/model for readability.
    player1_name = _make_player_name(PLAYER1_PROVIDER, PLAYER1_MODEL, player1_persona.name)
    player2_name = _make_player_name(PLAYER2_PROVIDER, PLAYER2_MODEL, player2_persona.name)
    win_totals: dict[str, int] = {player1_name: 0, player2_name: 0}

    match_num = 0
    while True:
        match_num += 1
        if MATCH_COUNT > 0 and match_num > MATCH_COUNT:
            break
        print(f"\n{'=' * 50}", flush=True)
        print(f"Starting match #{match_num}", flush=True)
        print(f"{'=' * 50}", flush=True)
        battle_format = random.choice(battle_formats)
        print(f"Selected battle format: {battle_format}", flush=True)

        agent1: Player | None = None
        agent2: Player | None = None
        try:
            agent1 = LLMPlayer(
                account_configuration=AccountConfiguration(player1_name, None),
                server_configuration=server_config,
                battle_format=battle_format,
                max_concurrent_battles=1,
                save_replays=True,
                provider=PLAYER1_PROVIDER,
                model_id=PLAYER1_MODEL,
                opponent_name=player2_persona.abbreviation,
                opponent_account_name=player2_name,
                turn_delay_seconds=float(os.getenv("TURN_DELAY_SECONDS") or "0"),
                battle_side="p1",
                system_prompt=build_system_prompt(
                    player1_persona.prompt_body,
                    player_name=player1_name,
                    opponent_name=player2_persona.abbreviation,
                ),
            )

            agent2 = LLMPlayer(
                account_configuration=AccountConfiguration(player2_name, None),
                server_configuration=server_config,
                battle_format=battle_format,
                max_concurrent_battles=1,
                save_replays=True,
                provider=PLAYER2_PROVIDER,
                model_id=PLAYER2_MODEL,
                opponent_name=player1_persona.abbreviation,
                opponent_account_name=player1_name,
                turn_delay_seconds=float(os.getenv("TURN_DELAY_SECONDS") or "0"),
                battle_side="p2",
                system_prompt=build_system_prompt(
                    player2_persona.prompt_body,
                    player_name=player2_name,
                    opponent_name=player1_persona.abbreviation,
                ),
            )

            write_current_battle_state(
                status="starting",
                battle_format=battle_format,
                player1_name=player1_name,
                player2_name=player2_name,
                player1_model_id=PLAYER1_MODEL,
                player2_model_id=PLAYER2_MODEL,
                player1_persona=player1_persona,
                player2_persona=player2_persona,
            )
            clear_thoughts_state()
            await _post_thoughts_clear()
            match_start = time.time()
            known_tags = set(agent1.battles.keys())
            battle_task = asyncio.create_task(
                agent1.battle_against(agent2, n_battles=1)
            )
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
                        player1_model_id=PLAYER1_MODEL,
                        player2_model_id=PLAYER2_MODEL,
                        player1_persona=player1_persona,
                        player2_persona=player2_persona,
                    )
                    print(f"Live battle detected: {live_battle_tag}", flush=True)
                await asyncio.sleep(0.3)

            await battle_task

            last_battle = list(agent1.battles.values())[-1]
            if last_battle.won:
                winner, loser = player1_name, player2_name
            elif last_battle.lost:
                winner, loser = player2_name, player1_name
            else:
                winner, loser = "Draw", "Draw"

            if winner in win_totals:
                win_totals[winner] += 1

            print(f"Match #{match_num} result: {winner} wins!", flush=True)
            print(
                f"Running totals — {player1_name}: {win_totals[player1_name]}W | "
                f"{player2_name}: {win_totals[player2_name]}W",
                flush=True,
            )

            match_duration = time.time() - match_start
            await report_result(
                winner,
                loser,
                battle_format=battle_format,
                duration=match_duration,
            )
            safe_tag = last_battle.battle_tag.lstrip(">").replace("/", "_")
            replay_path = save_local_replay(agent1, last_battle.battle_tag)
            save_raw_battle_log(
                last_battle,
                safe_tag=safe_tag,
                winner=winner,
                loser=loser,
                battle_format=battle_format,
                provider=f"{PLAYER1_PROVIDER}+{PLAYER2_PROVIDER}",
                model_id=f"p1={PLAYER1_MODEL},p2={PLAYER2_MODEL}",
                replay_path=replay_path,
            )
            write_current_battle_state(
                status="idle",
                battle_format=battle_format,
                player1_name=player1_name,
                player2_name=player2_name,
                player1_model_id=PLAYER1_MODEL,
                player2_model_id=PLAYER2_MODEL,
                player1_persona=player1_persona,
                player2_persona=player2_persona,
            )
            clear_thoughts_state()
            await _post_thoughts_clear()

        except Exception as e:
            write_current_battle_state(
                status="error",
                battle_format=battle_format,
                player1_name=player1_name,
                player2_name=player2_name,
                player1_model_id=PLAYER1_MODEL,
                player2_model_id=PLAYER2_MODEL,
                player1_persona=player1_persona,
                player2_persona=player2_persona,
            )
            clear_thoughts_state()
            await _post_thoughts_clear()
            print(f"Error in match #{match_num}: {e}", flush=True)
            traceback.print_exc()
        finally:
            await _disconnect_players(agent1, agent2)

        if MATCH_COUNT <= 0 or match_num < MATCH_COUNT:
            print(f"Next match in {DELAY_BETWEEN_MATCHES}s ...", flush=True)
            await asyncio.sleep(DELAY_BETWEEN_MATCHES)

    print("Match run complete. Exiting agents container.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
