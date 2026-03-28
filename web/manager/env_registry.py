"""
Documented environment variables for the manager Config page.

Values are applied when Docker Compose starts containers from the project `.env` file.
The web service can optionally mount that file to edit it in-browser (see MANAGER_HOST_ENV_FILE).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnvVarDef:
    key: str
    category: str
    description: str
    default: str
    services: str
    sensitive: bool = False


# Single source for Config UI + API; keep in sync with .env.example when adding vars.
ENV_REGISTRY: tuple[EnvVarDef, ...] = (
    EnvVarDef(
        "ANTHROPIC_API_KEY",
        "LLM API keys",
        "Anthropic API key (agents).",
        "",
        "agents",
        sensitive=True,
    ),
    EnvVarDef(
        "DEEPSEEK_API_KEY",
        "LLM API keys",
        "DeepSeek API key (agents).",
        "",
        "agents",
        sensitive=True,
    ),
    EnvVarDef(
        "DEEPSEEK_BASE_URL",
        "LLM API keys",
        "DeepSeek API base URL.",
        "https://api.deepseek.com",
        "agents",
    ),
    EnvVarDef(
        "OPENROUTER_API_KEY",
        "LLM API keys",
        "OpenRouter API key (agents).",
        "",
        "agents",
        sensitive=True,
    ),
    EnvVarDef(
        "OPENROUTER_BASE_URL",
        "LLM API keys",
        "OpenRouter API base URL.",
        "https://openrouter.ai/api/v1",
        "agents",
    ),
    EnvVarDef(
        "OPENROUTER_STRUCTURED_OUTPUTS",
        "LLM API keys",
        "OpenRouter JSON schema mode: auto, force, or off.",
        "auto",
        "agents",
    ),
    EnvVarDef(
        "OPENROUTER_EXTRA_BODY_JSON",
        "LLM API keys",
        "Optional JSON merged into OpenRouter chat requests.",
        "",
        "agents",
    ),
    EnvVarDef(
        "TURN_DELAY_SECONDS",
        "Battle & agents",
        "Seconds to wait per turn (watchability).",
        "0",
        "agents",
    ),
    EnvVarDef(
        "DELAY_BETWEEN_MATCHES",
        "Battle & agents",
        "Pause after a match finishes before the next (seconds).",
        "15",
        "agents",
    ),
    EnvVarDef(
        "QUEUE_POLL_INTERVAL",
        "Battle & agents",
        "How often the queue worker polls when empty (seconds).",
        "5",
        "agents",
    ),
    EnvVarDef(
        "LLM_MAX_OUTPUT_TOKENS",
        "Battle & agents",
        "Max completion tokens per LLM turn.",
        "512",
        "agents",
    ),
    EnvVarDef(
        "LLM_TURN_TIMEOUT",
        "Battle & agents",
        "Hard timeout per LLM call; fallback to random move (seconds).",
        "150",
        "agents",
    ),
    EnvVarDef(
        "POKEDEX_TOOL_ENABLED",
        "Pokédex",
        "Enable Anthropic Pokédex tool calls (1/0).",
        "0",
        "agents",
    ),
    EnvVarDef(
        "POKEDEX_AUTO_ENRICH",
        "Pokédex",
        "Inject Pokédex notes into context for all providers (1/0).",
        "0",
        "agents",
    ),
    EnvVarDef(
        "POKEDEX_MAX_LOOKUPS",
        "Pokédex",
        "Max tool lookups per turn before forcing action.",
        "3",
        "agents",
    ),
    EnvVarDef(
        "TWITCH_STREAM_KEY",
        "Twitch",
        "Twitch RTMP stream key (stream service).",
        "",
        "stream",
        sensitive=True,
    ),
    EnvVarDef(
        "TWITCH_CLIENT_ID",
        "Twitch",
        "Twitch API application client ID (set title script / stream auto-title).",
        "",
        "stream",
        sensitive=True,
    ),
    EnvVarDef(
        "TWITCH_OAUTH_TOKEN",
        "Twitch",
        "OAuth token with channel edit scope (auto title on stream start).",
        "",
        "stream",
        sensitive=True,
    ),
    EnvVarDef(
        "TWITCH_BROADCASTER_ID",
        "Twitch",
        "Numeric broadcaster user ID for Helix API.",
        "",
        "stream",
        sensitive=False,
    ),
    EnvVarDef(
        "TWITCH_AUTO_SET_TITLE",
        "Twitch",
        "Whether stream container updates channel title on start (1/0).",
        "1",
        "stream",
    ),
    EnvVarDef(
        "TWITCH_GAME_ID",
        "Twitch",
        "Twitch category ID (default Pokémon).",
        "1982936547",
        "stream",
    ),
    EnvVarDef(
        "TWITCH_STREAM_TITLE",
        "Twitch",
        "Title sent to Twitch when auto-setting metadata.",
        "Pokémon Showdown battles with LLMs",
        "stream",
    ),
    EnvVarDef(
        "STREAM_VIEW_URL",
        "Stream",
        "Broadcast URL opened inside the stream browser (Docker: http://web:8080/broadcast).",
        "http://web:8080/broadcast",
        "stream",
    ),
    EnvVarDef(
        "STREAM_TITLE",
        "Stream",
        "Headline on the broadcast page (web).",
        "Pokémon Showdown battles with LLMs",
        "web",
    ),
    EnvVarDef(
        "STREAM_AUDIO_SOURCE",
        "Stream",
        "Audio capture mode: browser (null sink) or pulse (default mic/source).",
        "pulse",
        "stream",
    ),
    EnvVarDef(
        "HIDE_BATTLE_UI",
        "Stream",
        "Hide native Showdown battle chrome in embedded client (1/0).",
        "1",
        "web, stream",
    ),
    EnvVarDef(
        "VICTORY_MODAL_SECONDS",
        "Web",
        "Victory splash duration on /broadcast (seconds).",
        "30",
        "web",
    ),
    EnvVarDef(
        "SHOWDOWN_HOST",
        "Network",
        "Showdown hostname on Docker network.",
        "showdown",
        "agents, stream",
    ),
    EnvVarDef(
        "SHOWDOWN_PORT",
        "Network",
        "Showdown HTTP/WebSocket port.",
        "8000",
        "agents, stream",
    ),
    EnvVarDef(
        "SHOWDOWN_VIEW_BASE",
        "Web",
        "Browser URL for raw Showdown (manager “Open battle” links).",
        "http://localhost:8000",
        "web",
    ),
    EnvVarDef(
        "WEB_HOST",
        "Network",
        "Web service hostname (internal).",
        "web",
        "agents, stream",
    ),
    EnvVarDef(
        "WEB_PORT",
        "Network",
        "Web HTTP port.",
        "8080",
        "agents, stream",
    ),
    EnvVarDef(
        "OVERLAY_HOST",
        "Network",
        "Deprecated alias for WEB_HOST (agents/stream).",
        "",
        "agents, stream",
    ),
    EnvVarDef(
        "OVERLAY_PORT",
        "Network",
        "Deprecated alias for WEB_PORT.",
        "",
        "agents, stream",
    ),
    EnvVarDef(
        "PERSONAS_DIR",
        "Paths",
        "Persona markdown directory inside web/agents containers.",
        "/personas",
        "web, agents",
    ),
    EnvVarDef(
        "TRAINERS_DIR",
        "Paths",
        "Trainer sprites directory (web manager uploads / static).",
        "/app/static/trainers",
        "web",
    ),
    EnvVarDef(
        "REPLAY_DIR",
        "Paths",
        "Replay HTML output directory (container path).",
        "/replays",
        "agents",
    ),
    EnvVarDef(
        "LOG_DIR",
        "Paths",
        "Raw battle log JSON directory.",
        "/logs",
        "agents",
    ),
    EnvVarDef(
        "LOG_RAW_BATTLE",
        "Paths",
        "Write raw JSON battle logs (1/0).",
        "1",
        "agents",
    ),
    EnvVarDef(
        "STATE_DIR",
        "Paths",
        "Live state (current_battle.json, thoughts.json).",
        "/state",
        "agents",
    ),
    EnvVarDef(
        "MANAGER_HOST_ENV_FILE",
        "Web",
        "Path to mounted host `.env` inside web for Config editor (e.g. /app/host.env).",
        "",
        "web",
    ),
)

REGISTRY_BY_KEY: dict[str, EnvVarDef] = {e.key: e for e in ENV_REGISTRY}


def categories_in_order() -> tuple[str, ...]:
    seen: list[str] = []
    for e in ENV_REGISTRY:
        if e.category not in seen:
            seen.append(e.category)
    return tuple(seen)
