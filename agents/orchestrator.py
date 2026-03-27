"""
Battle orchestrator — runs one (or many) gen8randombattle matches.

It runs two LLM-powered players, reporting results to the overlay.
"""

import asyncio
import os
import time
import traceback
import re
import random
from pathlib import Path
import json
from typing import Any

import aiohttp
from poke_env.ps_client import AccountConfiguration, ServerConfiguration

from llm_player import LLMPlayer

SHOWDOWN_HOST = os.getenv("SHOWDOWN_HOST", "showdown")
SHOWDOWN_PORT = int(os.getenv("SHOWDOWN_PORT", "8000"))
OVERLAY_HOST = os.getenv("OVERLAY_HOST", "overlay")
OVERLAY_PORT = int(os.getenv("OVERLAY_PORT", "8080"))
DELAY_BETWEEN_MATCHES = float(os.getenv("DELAY_BETWEEN_MATCHES") or "15")
MATCH_COUNT = int(os.getenv("MATCH_COUNT") or "1")


def _required_env(name: str) -> str:
    val = os.getenv(name)
    if val is None or not str(val).strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return str(val).strip()


# Per-player provider/model overrides (required).
# Player 1 is Aggro; Player 2 is Stall.
#
# Example: DeepSeek-Aggro vs Claude-Stall
#   PLAYER1_PROVIDER=deepseek
#   PLAYER1_MODEL=deepseek-chat
#   PLAYER2_PROVIDER=anthropic
#   PLAYER2_MODEL=claude-3-5-sonnet-latest
PLAYER1_PROVIDER = _required_env("PLAYER1_PROVIDER").lower()
PLAYER1_MODEL = _required_env("PLAYER1_MODEL")
PLAYER2_PROVIDER = _required_env("PLAYER2_PROVIDER").lower()
PLAYER2_MODEL = _required_env("PLAYER2_MODEL")

# Validate provider values early so a misconfig fails fast.
_ALLOWED_PROVIDERS = {"anthropic", "deepseek"}
if PLAYER1_PROVIDER not in _ALLOWED_PROVIDERS:
    raise ValueError(f"PLAYER1_PROVIDER must be one of {sorted(_ALLOWED_PROVIDERS)}")
if PLAYER2_PROVIDER not in _ALLOWED_PROVIDERS:
    raise ValueError(f"PLAYER2_PROVIDER must be one of {sorted(_ALLOWED_PROVIDERS)}")

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


async def report_result(winner: str, loser: str) -> None:
    """POST a match result to the overlay service."""
    url = f"http://{OVERLAY_HOST}:{OVERLAY_PORT}/result"
    payload = {"winner": winner, "loser": loser, "timestamp": time.time()}
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


def save_local_replay(player: LLMPlayer, battle_tag: str) -> Path:
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
) -> None:
    """Write current live battle state for overlay/broadcast page."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "battle_tag": battle_tag,
        "battle_format": battle_format,
        "player1_name": player1_name,
        "player2_name": player2_name,
        "player1_model_id": player1_model_id,
        "player2_model_id": player2_model_id,
        "updated_at": time.time(),
    }
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

    # Names are tied to play style; system prompts are also style-specific.
    # Provider/model is chosen per player via PLAYER*_PROVIDER/PLAYER*_MODEL.
    player1_name = (
        "DeepSeek-Aggro" if PLAYER1_PROVIDER == "deepseek" else "Claude-Aggro"
    )
    player2_name = (
        "DeepSeek-Stall" if PLAYER2_PROVIDER == "deepseek" else "Claude-Stall"
    )
    win_totals: dict[str, int] = {player1_name: 0, player2_name: 0}

    for match_num in range(1, MATCH_COUNT + 1):
        print(f"\n{'=' * 50}", flush=True)
        print(f"Starting match #{match_num}", flush=True)
        print(f"{'=' * 50}", flush=True)
        battle_format = random.choice(battle_formats)
        print(f"Selected battle format: {battle_format}", flush=True)

        try:
            agent1 = LLMPlayer(
                account_configuration=AccountConfiguration(player1_name, None),
                server_configuration=server_config,
                battle_format=battle_format,
                max_concurrent_battles=1,
                save_replays=True,
                provider=PLAYER1_PROVIDER,
                model_id=PLAYER1_MODEL,
                turn_delay_seconds=float(os.getenv("TURN_DELAY_SECONDS") or "0"),
                system_prompt=(
                    f"You are an aggressive Pokemon battle AI named {player1_name}. "
                    "You favor high-damage offensive plays — super effective hits, "
                    "STAB moves, and setup sweeps. You only switch defensively as a "
                    "last resort. Think about type matchups and pick the move that "
                    "deals the most damage this turn.\n\n"
                    "You MUST end your response with exactly one line in the format:\n"
                    "ACTION: move N\nor\nACTION: switch N\n"
                    "where N is the number from the valid actions list."
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
                turn_delay_seconds=float(os.getenv("TURN_DELAY_SECONDS") or "0"),
                system_prompt=(
                    f"You are a strategic, defensive Pokemon battle AI named {player2_name}. "
                    "You think long-term: you prioritize favorable type matchups by "
                    "switching, use status moves to cripple opponents, set up hazards, "
                    "and play for chip damage. You predict your opponent's moves and "
                    "switch to resist them. Only go offensive when you have a clear advantage.\n\n"
                    "You MUST end your response with exactly one line in the format:\n"
                    "ACTION: move N\nor\nACTION: switch N\n"
                    "where N is the number from the valid actions list."
                ),
            )

            write_current_battle_state(
                status="starting",
                battle_format=battle_format,
                player1_name=player1_name,
                player2_name=player2_name,
                player1_model_id=PLAYER1_MODEL,
                player2_model_id=PLAYER2_MODEL,
            )
            clear_thoughts_state()
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

            await report_result(winner, loser)
            safe_tag = last_battle.battle_tag.lstrip(">").replace("/", "_")
            replay_path = save_local_replay(agent1, last_battle.battle_tag)
            save_raw_battle_log(
                last_battle,
                safe_tag=safe_tag,
                winner=winner,
                loser=loser,
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
            )
            clear_thoughts_state()

        except Exception as e:
            write_current_battle_state(
                status="error",
                battle_format=battle_format,
                player1_name=player1_name,
                player2_name=player2_name,
                player1_model_id=PLAYER1_MODEL,
                player2_model_id=PLAYER2_MODEL,
            )
            clear_thoughts_state()
            print(f"Error in match #{match_num}: {e}", flush=True)
            traceback.print_exc()

        if match_num < MATCH_COUNT:
            print(f"Next match in {DELAY_BETWEEN_MATCHES}s ...", flush=True)
            await asyncio.sleep(DELAY_BETWEEN_MATCHES)

    print("Match run complete. Exiting agents container.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
