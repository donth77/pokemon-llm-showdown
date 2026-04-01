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
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import json
from poke_env.player import Player
from poke_env.ps_client import AccountConfiguration, ServerConfiguration

from env_bool import parse_env_bool
from llm_player import LLMPlayer, reflection_json_completion
from log_print import log_print


# ---------------------------------------------------------------------------
# Config from environment (infrastructure only — no match-specific config)
# ---------------------------------------------------------------------------

SHOWDOWN_HOST = os.getenv("SHOWDOWN_HOST", "showdown")
SHOWDOWN_PORT = int(os.getenv("SHOWDOWN_PORT", "8000"))
WEB_HOST = os.getenv("WEB_HOST") or os.getenv("OVERLAY_HOST", "web")
WEB_PORT = int(os.getenv("WEB_PORT") or os.getenv("OVERLAY_PORT", "8080"))
MANAGER_API = f"http://{WEB_HOST}:{WEB_PORT}/api/manager"
REPLAY_DIR = Path(os.getenv("REPLAY_DIR", "/replays"))
LOG_DIR = Path(os.getenv("LOG_DIR", "/logs"))
LOG_RAW_BATTLE = parse_env_bool("LOG_RAW_BATTLE", default=True)
STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
CURRENT_BATTLE_FILE = STATE_DIR / "current_battle.json"
THOUGHTS_FILE = STATE_DIR / "thoughts.json"
TURN_DELAY_SECONDS = float(os.getenv("TURN_DELAY_SECONDS") or "0")
# Keep status=starting on the wire long enough for /broadcast (250ms hub) to show match intro
# before the battle loop flips to live (often immediate once battle_against starts).
_MATCH_INTRO_STARTING_HOLD_SEC = float(
    os.getenv("MATCH_INTRO_STARTING_HOLD_SECONDS") or "0.45"
)


ENABLE_MEMORY = parse_env_bool("ENABLE_MEMORY", default=False)
MEMORY_REFLECTION_INTERVAL = max(0, int(os.getenv("MEMORY_REFLECTION_INTERVAL") or "1"))
LEARNINGS_UPDATE_INTERVAL = max(0, int(os.getenv("LEARNINGS_UPDATE_INTERVAL") or "3"))
MAX_MEMORY_ENTRIES = max(1, int(os.getenv("MAX_MEMORY_ENTRIES") or "10"))
MAX_LEARNINGS_BULLETS = max(1, int(os.getenv("MAX_LEARNINGS_BULLETS") or "30"))

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
    '- "reasoning": A short spectator blurb only (~60 words max, 3 sentences max) in YOUR '
    "persona voice (first person). Not a damage calc, type-chart essay, or multi-turn writeup — "
    "keep analysis in your head, then give a tight summary. Plain text only (no markdown); "
    "direct prose, no leading labels.\n"
    '- "callout": optional; usually leave empty. Short in-character phrase only on standout turns.\n'
    "Callout guidance:\n"
    "- Default to no callout (empty string) on most turns — roughly half or more should have none.\n"
    "- Reserve callouts for spikes: big damage, KOs, key switches, predicts, momentum swings, or "
    "last-stand moments.\n"
    "- Keep callouts fresh and varied; avoid repeating your recent phrasing.\n"
    "- You may direct callouts at your opponent using 'you' or their name.\n"
    "- Keep callouts concise and in-character, but not a copy of canned examples.\n"
    "- When you are clearly behind or about to lose, you may use a callout that shows "
    "weariness or exasperation in YOUR persona's style.\n"
)


def _parse_persona_markdown(
    markdown_text: str, *, source_path: Path
) -> tuple[dict[str, str], str]:
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
        available = (
            sorted(p.stem for p in PERSONAS_DIR.glob("*.md"))
            if PERSONAS_DIR.exists()
            else []
        )
        raise ValueError(
            f"Persona '{slug}' not found. Available: {', '.join(available) or '(none)'}"
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


def build_system_prompt(
    prompt_body: str,
    *,
    player_name: str,
    opponent_name: str,
    memory_md: str = "",
    learnings_md: str = "",
) -> str:
    try:
        persona_prompt = prompt_body.format(
            player_name=player_name, opponent_name=opponent_name
        )
    except KeyError as e:
        raise ValueError(f"Unknown template variable in persona prompt: {e}") from e
    blocks: list[str] = [persona_prompt.strip()]
    mem = (memory_md or "").strip()
    if mem:
        blocks.append(f"== YOUR BATTLE MEMORY (recent matches) ==\n{mem}")
    learn = (learnings_md or "").strip()
    if learn:
        blocks.append(f"== YOUR TACTICAL LEARNINGS ==\n{learn}")
    blocks.append(ACTION_FORMAT_INSTRUCTIONS)
    return "\n\n".join(blocks)


def _load_persona_memory_texts(persona_slug: str) -> tuple[str, str]:
    if not ENABLE_MEMORY:
        return "", ""
    slug = (persona_slug or "").strip().lower()
    if not slug or not re.fullmatch(r"[a-z0-9_-]+", slug):
        return "", ""
    base = STATE_DIR / "personas" / slug
    mem_p = base / "memory.md"
    learn_p = base / "learnings.md"
    mem_ok = mem_p.is_file()
    learn_ok = learn_p.is_file()
    memory_md = mem_p.read_text(encoding="utf-8").strip() if mem_ok else ""
    learnings_md = learn_p.read_text(encoding="utf-8").strip() if learn_ok else ""
    log_print(
        f"[memory] load persona {slug}: memory.md "
        f"{'ok' if mem_ok else 'missing'} ({len(memory_md)} chars), "
        f"learnings.md {'ok' if learn_ok else 'missing'} ({len(learnings_md)} chars)",
        flush=True,
    )
    return memory_md, learnings_md


def _trim_memory_entries(md: str, max_entries: int) -> str:
    md = (md or "").strip()
    if not md or max_entries <= 0:
        return ""
    entries = [c.strip() for c in re.split(r"(?m)^(?=## Match )", md) if c.strip()]
    entries = [c for c in entries if c.startswith("## Match")]
    if len(entries) <= max_entries:
        return "\n\n".join(entries).strip()
    return "\n\n".join(entries[-max_entries:]).strip()


def _trim_learnings_bullets(md: str, max_bullets: int) -> str:
    md = (md or "").strip()
    if not md:
        return md
    if max_bullets <= 0:
        return ""
    lines = md.splitlines()
    bullet_count = sum(1 for ln in lines if re.match(r"\s*[-*]\s+\S", ln))
    if bullet_count <= max_bullets:
        return "\n".join(lines).strip()
    skip = bullet_count - max_bullets
    out_rev: list[str] = []
    skipped = 0
    for ln in reversed(lines):
        is_bullet = bool(re.match(r"\s*[-*]\s+\S", ln))
        if is_bullet and skipped < skip:
            skipped += 1
            continue
        out_rev.append(ln)
    return "\n".join(reversed(out_rev)).strip()


def _increment_persona_match_count(slug: str) -> int:
    base = STATE_DIR / "personas" / slug
    base.mkdir(parents=True, exist_ok=True)
    meta = base / "_memory_state.json"
    n = 0
    if meta.is_file():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            n = int(data.get("matches_completed", 0))
        except Exception:
            n = 0
    n += 1
    meta.write_text(
        json.dumps({"matches_completed": n}, indent=2),
        encoding="utf-8",
    )
    log_print(
        f"[memory] persona {slug}: wrote {meta.relative_to(STATE_DIR)} "
        f"matches_completed={n}",
        flush=True,
    )
    return n


def _strip_json_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


def _battle_log_text_for_reflection(
    *,
    log_file: str | None,
    replay_path: Path,
) -> str:
    lines: list[str] = []
    if log_file:
        p = LOG_DIR / log_file
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                lines = data.get("raw_log_lines") or data.get("replay_log_lines") or []
                if not isinstance(lines, list):
                    lines = []
                lines = [str(x) for x in lines]
            except Exception:
                lines = []
    if not lines and replay_path.is_file():
        lines = _extract_replay_log_lines(replay_path)
    text = "\n".join(lines) if lines else ""
    if not text:
        return "(no battle log available)"
    max_chars = 120_000
    if len(text) > max_chars:
        return text[:max_chars] + "\n... (truncated for reflection prompt)"
    return text


_REFLECTION_SYSTEM = """You help a Pokémon Showdown battle persona update their memory after a match.
Reply with a single JSON object only (no markdown code fences). Use exactly these keys:
- "memory_entry": string. Markdown body summarizing THIS match only (3–8 short lines; you may use short subheadings like "Key moments:"). Write in first person, matching the persona's voice described in the user message. Do NOT include a "## Match" title line.
- "learnings_update": string or null. If the user indicates tactical learnings must be updated, return the FULL updated learnings document (markdown with sections and bullet points). If learnings should not change this match, use null.

Keep memory_entry concise. For learnings, prefer durable tactical patterns over one-off events; cap total bullet points mentally around 30 (the system will trim excess)."""


async def _run_one_persona_reflection(
    *,
    persona_slug: str,
    provider: str,
    model_id: str,
    persona_display_name: str,
    opponent_persona_name: str,
    battle_format: str,
    outcome: str,
    battle_log_text: str,
    update_learnings: bool,
) -> None:
    if provider not in ALLOWED_PROVIDERS:
        return
    base = STATE_DIR / "personas" / persona_slug
    base.mkdir(parents=True, exist_ok=True)
    mem_path = base / "memory.md"
    learn_path = base / "learnings.md"
    current_memory = mem_path.read_text(encoding="utf-8") if mem_path.is_file() else ""
    current_learnings = (
        learn_path.read_text(encoding="utf-8") if learn_path.is_file() else ""
    )
    log_print(
        f"[memory] {persona_slug}: reflection reading "
        f"{mem_path.relative_to(STATE_DIR)} ({len(current_memory)} chars), "
        f"{learn_path.relative_to(STATE_DIR)} ({len(current_learnings)} chars); "
        f"update_learnings={update_learnings}",
        flush=True,
    )

    learnings_instruction = (
        'Set "learnings_update" to the full updated learnings markdown document '
        "if you have meaningful new tactical insights or refinements; otherwise null."
        if update_learnings
        else 'Set "learnings_update" to null (do not revise learnings this match).'
    )
    user_msg = "\n".join(
        [
            f"You are persona: {persona_display_name!r} (slug: {persona_slug}).",
            f"Opponent persona: {opponent_persona_name!r}.",
            f"Format: {battle_format}",
            f"Result for you: {outcome}",
            "",
            learnings_instruction,
            "",
            "YOUR CURRENT BATTLE MEMORY FILE (may be empty):",
            "---",
            current_memory.strip() or "(empty)",
            "---",
            "",
            "YOUR CURRENT TACTICAL LEARNINGS FILE (may be empty):",
            "---",
            current_learnings.strip() or "(empty)",
            "---",
            "",
            "BATTLE LOG:",
            battle_log_text,
        ]
    )
    reply = await reflection_json_completion(
        provider=provider,  # type: ignore[arg-type]
        model_id=model_id,
        system=_REFLECTION_SYSTEM,
        user=user_msg,
    )
    raw = _strip_json_fences(reply)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        log_print(
            f"[memory] {persona_slug}: invalid JSON from reflection: {e}", flush=True
        )
        return
    if not isinstance(payload, dict):
        return
    entry = payload.get("memory_entry")
    if not isinstance(entry, str) or not entry.strip():
        log_print(f"[memory] {persona_slug}: missing memory_entry", flush=True)
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = f"## Match {ts} -- {battle_format} vs {opponent_persona_name} ({outcome})"
    block = f"{header}\n\n{entry.strip()}\n"
    prev = mem_path.read_text(encoding="utf-8") if mem_path.is_file() else ""
    combined = f"{prev.rstrip()}\n\n{block}\n".strip() + "\n"
    trimmed_mem = _trim_memory_entries(combined, MAX_MEMORY_ENTRIES)
    mem_path.write_text(trimmed_mem, encoding="utf-8")
    log_print(
        f"[memory] {persona_slug}: wrote {mem_path.relative_to(STATE_DIR)} "
        f"({len(trimmed_mem)} chars, max_entries={MAX_MEMORY_ENTRIES})",
        flush=True,
    )

    lu = payload.get("learnings_update")
    if update_learnings and isinstance(lu, str) and lu.strip():
        trimmed = _trim_learnings_bullets(lu.strip(), MAX_LEARNINGS_BULLETS)
        learn_path.write_text(trimmed + "\n", encoding="utf-8")
        log_print(
            f"[memory] {persona_slug}: wrote {learn_path.relative_to(STATE_DIR)} "
            f"({len(trimmed)} chars, max_bullets={MAX_LEARNINGS_BULLETS})",
            flush=True,
        )
    elif update_learnings:
        log_print(
            f"[memory] {persona_slug}: learnings_update null/empty; "
            f"{learn_path.relative_to(STATE_DIR)} not modified",
            flush=True,
        )


async def _post_match_persona_memory(
    *,
    log_file: str | None,
    replay_path: Path,
    battle_format: str,
    winner_side: str,
    is_draw: bool,
    player1_provider: str,
    player1_model: str,
    p1_persona: PersonaDefinition,
    player1_name: str,
    player2_provider: str,
    player2_model: str,
    p2_persona: PersonaDefinition,
    player2_name: str,
) -> None:
    if not ENABLE_MEMORY or MEMORY_REFLECTION_INTERVAL <= 0:
        return
    battle_log_text = _battle_log_text_for_reflection(
        log_file=log_file, replay_path=replay_path
    )
    seen_slug: set[str] = set()

    async def _side(
        *,
        slug: str,
        provider: str,
        model_id: str,
        display_name: str,
        opp_name: str,
        side: str,
    ) -> None:
        if slug in seen_slug:
            return
        seen_slug.add(slug)
        matches_done = _increment_persona_match_count(slug)
        if matches_done % MEMORY_REFLECTION_INTERVAL != 0:
            return
        if is_draw:
            outcome = "DRAW"
        else:
            outcome = "WIN" if winner_side == side else "LOSS"
        update_learnings = LEARNINGS_UPDATE_INTERVAL > 0 and (
            matches_done % LEARNINGS_UPDATE_INTERVAL == 0
        )
        try:
            await _run_one_persona_reflection(
                persona_slug=slug,
                provider=provider,
                model_id=model_id,
                persona_display_name=display_name,
                opponent_persona_name=opp_name,
                battle_format=battle_format,
                outcome=outcome,
                battle_log_text=battle_log_text,
                update_learnings=update_learnings,
            )
        except Exception as e:
            log_print(f"[memory] reflection failed for {slug!r}: {e}", flush=True)
            traceback.print_exc()

    await _side(
        slug=p1_persona.slug,
        provider=player1_provider,
        model_id=player1_model,
        display_name=p1_persona.name,
        opp_name=p2_persona.name,
        side="p1",
    )
    await _side(
        slug=p2_persona.slug,
        provider=player2_provider,
        model_id=player2_model,
        display_name=p2_persona.name,
        opp_name=p1_persona.name,
        side="p2",
    )


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
    log_print(f"Waiting for Showdown at {url} ...", flush=True)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        log_print("Showdown is up!", flush=True)
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
            log_print(f"[cleanup] Disconnected {name}", flush=True)
        except Exception as e:
            log_print(f"[cleanup] Disconnect {name}: {e}", flush=True)


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
        log_print(
            f"[match] Showdown names collided; using distinct logins: {n1!r} vs {n2!r}",
            flush=True,
        )
    return n1, n2


# ---------------------------------------------------------------------------
# State / thoughts helpers
# ---------------------------------------------------------------------------


def write_json_atomic(path: Path, data: dict) -> None:
    """Replace a JSON file atomically so concurrent readers never see partial bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    tmp.write_text(serialized, encoding="utf-8")
    os.replace(tmp, path)


def _merge_series_snapshot_into_current_battle(snapshot: dict) -> None:
    try:
        if not CURRENT_BATTLE_FILE.is_file():
            return
        raw = CURRENT_BATTLE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return
        data["series_id"] = snapshot["series_id"]
        data["series_best_of"] = snapshot["best_of"]
        data["series_player1_wins"] = snapshot["player1_wins"]
        data["series_player2_wins"] = snapshot["player2_wins"]
        write_json_atomic(CURRENT_BATTLE_FILE, data)
    except Exception as e:
        log_print(
            f"[match] Could not merge series into current_battle: {e}", flush=True
        )


async def _post_manager_match_complete(
    match_id: int,
    *,
    winner: str,
    loser: str,
    winner_side: str,
    duration: float,
    replay_file: str | None,
    log_file: str | None,
    battle_tag: str | None,
) -> dict | None:
    """Notify manager (scoreboard / tournaments). Safe to call twice with artifacts on 2nd."""
    url = f"{MANAGER_API}/matches/{match_id}/complete"
    payload = {
        "winner": winner,
        "loser": loser,
        "winner_side": winner_side,
        "duration": duration,
        "replay_file": replay_file,
        "log_file": log_file,
        "battle_tag": battle_tag,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log_print(
                        f"[match] manager complete HTTP {resp.status}: {text[:500]}",
                        flush=True,
                    )
                    return None
                return await resp.json()
    except Exception as e:
        log_print(f"[match] manager complete request failed: {e}", flush=True)
        return None


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
    tourney_context: dict | None = None,
    manager_match_id: int | None = None,
    tournament_intro_roster: list[dict] | None = None,
    tournament_best_of: int | None = None,
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
    if tourney_context:
        for key in (
            "tournament_id",
            "tournament_name",
            "tournament_type",
            "series_bracket",
            "series_round_number",
            "series_match_position",
            "tournament_max_winners_round",
            "game_number",
        ):
            val = tourney_context.get(key)
            if val is not None:
                payload[key] = val
    if manager_match_id is not None:
        payload["match_id"] = manager_match_id
    if tournament_intro_roster is not None:
        payload["tournament_intro_roster"] = tournament_intro_roster
    if tournament_best_of is not None:
        try:
            payload["tournament_best_of"] = int(tournament_best_of)
        except (TypeError, ValueError):
            pass
    write_json_atomic(CURRENT_BATTLE_FILE, payload)


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
    log_print(f"Saved local replay: {output_path}", flush=True)
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
    log_print(f"Saved raw log: {output_path}", flush=True)
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
    player1_team_showdown: str | None = None,
    player2_team_showdown: str | None = None,
    series_snapshot: dict | None = None,
    tourney_context: dict | None = None,
    manager_match_id: int | None = None,
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
            log_print(
                f"[match] Manager Showdown names collided; using distinct logins: "
                f"{player1_name!r} vs {player2_name!r}",
                flush=True,
            )
        else:
            log_print(
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
        p1_mem, p1_learn = _load_persona_memory_texts(p1_persona.slug)
        p2_mem, p2_learn = _load_persona_memory_texts(p2_persona.slug)
        p1_team = (player1_team_showdown or "").strip() or None
        p2_team = (player2_team_showdown or "").strip() or None
        agent1_kw: dict = dict(
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
                memory_md=p1_mem,
                learnings_md=p1_learn,
            ),
        )
        if p1_team:
            agent1_kw["team"] = p1_team
        agent1 = LLMPlayer(**agent1_kw)

        agent2_kw: dict = dict(
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
                memory_md=p2_mem,
                learnings_md=p2_learn,
            ),
        )
        if p2_team:
            agent2_kw["team"] = p2_team
        agent2 = LLMPlayer(**agent2_kw)

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
            tourney_context=tourney_context,
            manager_match_id=manager_match_id,
        )
        if _MATCH_INTRO_STARTING_HOLD_SEC > 0:
            await asyncio.sleep(_MATCH_INTRO_STARTING_HOLD_SEC)
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
                    tourney_context=tourney_context,
                    manager_match_id=manager_match_id,
                )
                log_print(f"Live battle detected: {live_battle_tag}", flush=True)
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

        # Idle + manager complete **before** replay/memory so the broadcast can show
        # the victory splash within ~poll + network instead of after disk + reflection.
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
            tourney_context=tourney_context,
            manager_match_id=manager_match_id,
        )
        clear_thoughts_state()
        await _post_thoughts_clear()

        if manager_match_id is not None:
            body = await _post_manager_match_complete(
                int(manager_match_id),
                winner=winner,
                loser=loser,
                winner_side=winner_side,
                duration=round(match_duration, 1),
                replay_file=None,
                log_file=None,
                battle_tag=safe_tag,
            )
            if isinstance(body, dict):
                snap = body.get("series_snapshot")
                if isinstance(snap, dict):
                    _merge_series_snapshot_into_current_battle(snap)

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

        is_draw = winner == "Draw"
        try:
            await _post_match_persona_memory(
                log_file=log_path.name if log_path else None,
                replay_path=replay_path,
                battle_format=battle_format,
                winner_side=winner_side,
                is_draw=is_draw,
                player1_provider=player1_provider,
                player1_model=player1_model,
                p1_persona=p1_persona,
                player1_name=player1_name,
                player2_provider=player2_provider,
                player2_model=player2_model,
                p2_persona=p2_persona,
                player2_name=player2_name,
            )
        except Exception as e:
            log_print(f"[memory] post-match memory cycle failed: {e}", flush=True)
            traceback.print_exc()

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
            tourney_context=tourney_context,
            manager_match_id=manager_match_id,
        )
        clear_thoughts_state()
        await _post_thoughts_clear()
        log_print(f"Error in match: {e}", flush=True)
        traceback.print_exc()
        return MatchResult(
            winner="",
            loser="",
            winner_side="",
            duration=0,
            replay_file=None,
            log_file=None,
            battle_tag=None,
            error=str(e),
        )

    finally:
        await _disconnect_players(agent1, agent2)
