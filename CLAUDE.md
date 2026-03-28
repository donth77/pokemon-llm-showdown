# CLAUDE.md

## Project Overview

Two LLM-powered agents battle on a local Pokémon Showdown server. The **manager** (web UI + API) and **scripts** queue matchups and tournaments so you can **compare models, providers, and personas** via results, stats, and replays. **Streaming is optional:** core Compose is `showdown` + `web` + `agents`; the **`stream`** service adds headless Xvfb + Chromium + FFmpeg → Twitch RTMP (no OBS required for that path). You can also drive the same broadcast URLs from **OBS** (see README).

## Architecture

Four Docker services on a shared bridge network (`battle-net`):

| Service | Dir | Port | Role |
|---------|-----|------|------|
| `showdown` | `showdown/` | 8000 | Local Pokémon Showdown battle server (Node 20, no auth) |
| `web` | `web/` | 8080 | FastAPI: scoreboard, `/manager` + tournament API, broadcast, victory splash, thoughts WebSocket, replay index |
| `agents` | `agents/` | — | LLM battle agents (Python, poke-env); queue worker polls `/api/manager` for matches |
| `stream` | `stream/` | 9222 | Xvfb + Chromium + FFmpeg → Twitch RTMP capture pipeline |

**Data flow:** Agents ↔ Showdown (WebSocket game protocol) · Agents → Web (`/thought`, match completion via `/api/manager/matches/{id}/complete`, plus `current_battle.json` / `thoughts.json` on `state-data`) · Stream → Web + Showdown (HTTP, for browser capture only)

**Shared Docker volumes:**
- `web-data` → `/data` (legacy flat `results.json` if used; scoreboard primarily reads SQLite on `manager-data`)
- `manager-data` → `/manager-data` (SQLite for tournaments/matches)
- `replay-data` → `/replays` (HTML replay exports)
- `log-data` → `/logs` (raw JSON battle logs)
- `state-data` → `/state` (current_battle.json, thoughts.json — written by agents, read by web)

**Match queue (design):** The queue is **not** a separate service — it is **`matches` rows** in SQLite (`status`, `queued_at`). Dequeue = transactional **UPDATE** `queued` → `running` in `pop_next_queued_match()`. Completed matches remain in the same table (`completed` / `error` / `cancelled`) for results and stats. Workers pull jobs via **`GET /api/manager/queue/next`** (HTTP polling in `agents/queue_worker.py`), not via Redis or an internal task queue.

## Tech Stack

- **Agents:** Python 3.11, poke-env, asyncio, **aiohttp**, Anthropic SDK, OpenAI-compatible SDK (DeepSeek / OpenRouter), optional Pokédex data layer (`agents/pokedex.py`)
- **Web:** Python 3.11, **FastAPI**, **Uvicorn**, **Starlette**, **Jinja2**, **aiosqlite** (async SQLite). App code under `web/`; tournament/match persistence and API in `web/manager/` (`db.py`, `routes.py`, `tournament_logic.py`, `env_registry.py`, `env_host_file.py` for `/manager/config`)
- **Showdown:** Node 20, upstream [pokemon-showdown](https://github.com/smogon/pokemon-showdown) repo
- **Stream:** Python 3.11, Playwright (Chromium), Xvfb, FFmpeg, PulseAudio
- **Infra:** Docker Compose v2, bridge network, named volumes; `web` mounts `./agents/personas` at `PERSONAS_DIR` (default `/personas`) for manager persona metadata and editing. Optional: `./.env` → `/app/host.env` with `MANAGER_HOST_ENV_FILE=/app/host.env` so `/manager/config` can edit the project env file (restart stack to apply to all services)

## Quick Commands

```bash
# Full stack
docker compose up -d --build

# Without Twitch streaming
docker compose up -d --build showdown web agents

# Logs
docker compose logs -f agents
docker compose logs -f stream

# Health check
bash scripts/healthcheck.sh

# Restart core (no stream)
bash scripts/restart_stack.sh

# Restart everything including stream
bash scripts/restart_stack.sh --stream

# Manager CLI (web must be reachable — WEB_URL / OVERLAY_URL for non-default host)
bash scripts/create_match.sh --help
bash scripts/create_tournament.sh --help

# Stack shutdown (optional --volumes; optional delay — see script)
bash scripts/stack_down.sh

# Stop
docker compose down
```

**Scripts:** See `scripts/` — `healthcheck`, `restart_stack`, `stack_down`, `create_match`, `create_tournament`, `set_twitch_title`. The README has a summary table with descriptions and env vars.

## Key Endpoints (web service, port 8080)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/manager` | GET | Manager dashboard |
| `/manager/tournaments`, `/manager/tournaments/new`, `/manager/tournaments/{tid}` | GET | Tournament list, create form, detail |
| `/manager/matches/new` | GET | Queue one-off match / series |
| `/manager/series/{sid}` | GET | Series + games |
| `/manager/results`, `/manager/results/stats` | GET | Completed matches, aggregate stats |
| `/manager/personas` | GET | Persona markdown + trainer uploads |
| `/manager/config` | GET | Documented env vars (`env_registry.py`); values from mounted host `.env` + web process env |
| `/manager/config/update` | POST | Form: update one registered key in host `.env` (`key`, `value`) |
| `/api/manager/config` | GET | Providers, formats, personas for UI |
| `/api/manager/tournaments` | GET, POST | List / create tournaments |
| `/api/manager/tournaments/{tid}` | GET | Tournament payload |
| `/api/manager/tournaments/{tid}/cancel` | POST | Cancel |
| `/api/manager/series` | POST | Create series |
| `/api/manager/series/{sid}` | GET | Series payload |
| `/api/manager/matches` | GET, POST | List / create match |
| `/api/manager/matches/{mid}` | GET | One match |
| `/api/manager/matches/{mid}/start` | POST | Mark running (optional) |
| `/api/manager/matches/{mid}/complete` | POST | Worker: record result |
| `/api/manager/matches/{mid}/error` | POST | Worker: fail + bracket side-effects |
| `/api/manager/queue/next` | GET | Worker: dequeue next match (404 if none) |
| `/api/manager/queue/depth` | GET | Queued match count |
| `/api/manager/queue/running` | GET | Running match JSON or `null` |
| `/api/manager/results` | GET | Completed matches |
| `/api/manager/stats` | GET | Analytics aggregates |
| `/scoreboard` | GET | Win/loss records, player info, recent matches (JSON) |
| `/result` | POST | Legacy: append match to DB (optional; worker uses manager API) |
| `/broadcast` | GET | Full broadcast scene: battle iframe + scoreboard `/overlay` route + thoughts |
| `/broadcast/battle_frame` | GET | Showdown iframe + battle sync + callouts (OBS layering) |
| `/broadcast/top_bar` | GET | Transparent title + format bar (OBS layering) |
| `/thoughts_overlay` | GET | Transparent LLM thoughts panels (OBS layering) |
| `/overlay` | GET | Transparent scoreboard page for compositing (URL path name; not the service name) |
| `/victory` | GET | Animated post-match winner splash |
| `/replays` | GET | Replay + log index page |
| `/current_battle` | GET | Live battle metadata (JSON) |
| `/thoughts` | GET | Current LLM reasoning per player (JSON) |
| `/thought` | POST | Submit a player thought (called by agents) |
| `/thoughts/clear` | POST | Clear thought history |
| `/thoughts/ws` | WebSocket | Real-time thought stream |
| `/health` | GET | Health check |

## Code Conventions

- **Python style:** Type hints throughout (`str | None`, dataclasses). No automated linter config committed, but `.ruff_cache/` is gitignored (ruff is used informally).
- **One folder per service:** Each has its own `Dockerfile` and `requirements.txt`. No monorepo package manager.
- **Async I/O:** aiohttp for non-blocking HTTP in agents; FastAPI async routes + WebSocket fanout in web.
- **SQLite (web):** Use **`async with _db() as db:`** in `web/manager/db.py`. `_db()` is an `@asynccontextmanager` around **`async with aiosqlite.connect(...)`**. Do **not** `await aiosqlite.connect()` and then `async with` the same `Connection`: aiosqlite starts a worker thread on connect/`__aenter__`, and doing both triggers `RuntimeError: threads can only be started once`. See `tournament_logic.py` for `async with db._db()`.
- **LLM output format:** Structured JSON with `action_type`, `index`, `reasoning`, optional `callout` — defined in `ACTION_FORMAT_INSTRUCTIONS` in `match_runner.py` (appended to persona prompts in `build_system_prompt`).
- **Pokédex data layer:** `agents/pokedex.py` provides lookup functions for moves, species, abilities, items, and type matchups. Move/species/type data comes from poke-env `GenData`; item/ability/move text descriptions are extracted from Showdown's upstream repo at build time by `agents/scripts/extract_showdown_data.py` into `/app/data/*.json`.
- **Inter-service state:** JSON files on shared Docker volumes plus SQLite on `manager-data` are the integration contract between agents and web.
- **Scripts:** Bash with `set -euo pipefail`.
- **No test suite:** No `tests/` directory, no CI/CD workflows. Health verification is via `scripts/healthcheck.sh`.

## Personas

Persona files live in `agents/personas/*.md`. Each has YAML front matter (`name`, `abbreviation`, `description`) and a free-form prompt body. The slug is the filename without `.md`.

Built-in personas: `aggro` (Damage Dan — hyper-offense) and `stall` (Stall Stella — defensive).

Match participants are configured via the **manager** (`/manager` or API), not env-only.

## Tournaments (brackets)

Logic lives in `web/manager/tournament_logic.py` (`generate_bracket`, `on_match_completed`, `on_match_failed`).

| Type | Behavior |
| --- | --- |
| Round robin | All pairs get a series (`best_of` per tournament); completion when all series resolved or cancelled. |
| Single elimination | Winners bracket only; completing the last winners series completes the tournament. |
| Double elimination | Winners + losers + **grand finals**. Completed **winners** series: advance winner in WB; drop loser into LB (pairing rules for small brackets + fallbacks). **Winners finals:** WB champion → grand finals **player 1**; WB finals loser into last LB feeder; LB advances like a secondary bracket; last LB winner → grand finals **player 2**. **Grand finals** completion → tournament `completed`. `_queue_series_matches` skips if the series already has queued/running games. |

**Gaps / product notes:** No **bracket-reset** grand finals (second set if LB winner wins). WB→LB mapping for **very large** fields is heuristic; 4- and 8-player flows are the most intentional. Tournament UI: `tournament_detail.html` shows winners, losers, and grand finals for double elim.

## Environment Variables

All config is via environment variables. Copy `.env.example` to `.env` and edit. Key groups:

- **API keys:** `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`
- **OpenRouter tuning (agents):** `OPENROUTER_STRUCTURED_OUTPUTS`, `OPENROUTER_EXTRA_BODY_JSON` (forwarded in `docker-compose.yml`)
- **Battle pacing:** `TURN_DELAY_SECONDS`, `DELAY_BETWEEN_MATCHES`, `QUEUE_POLL_INTERVAL`, `LLM_MAX_OUTPUT_TOKENS`, `LLM_TURN_TIMEOUT`
- **Pokédex:** `POKEDEX_TOOL_ENABLED` (Anthropic tool calling), `POKEDEX_AUTO_ENRICH` (context injection for all providers), `POKEDEX_MAX_LOOKUPS`
- **Storage:** `REPLAY_DIR`, `LOG_DIR`, `LOG_RAW_BATTLE`, `STATE_DIR` (in-container paths)
- **Stream:** `TWITCH_STREAM_KEY`, `STREAM_VIEW_URL`, `STREAM_AUDIO_SOURCE`
- **Network:** `SHOWDOWN_HOST`, `SHOWDOWN_PORT`, `WEB_HOST`, `WEB_PORT` (deprecated aliases: `OVERLAY_HOST`, `OVERLAY_PORT` still read by agents/stream for migration)
- **Twitch API (optional):** `TWITCH_CLIENT_ID`, `TWITCH_OAUTH_TOKEN`, `TWITCH_BROADCASTER_ID`, `TWITCH_AUTO_SET_TITLE`
- **Manager Config page:** `MANAGER_HOST_ENV_FILE` (in-container path to mounted host `.env`; `docker-compose.yml` sets `/app/host.env`). Only keys listed in `web/manager/env_registry.py` are editable in `/manager/config`.

See `.env.example` for the full documented list with defaults.

## Pokédex Tools

Optional feature gated by env vars (default off). Two independent modes:

- **Tool calling** (`POKEDEX_TOOL_ENABLED=1`): Anthropic models get five `pokedex_lookup_*` tools alongside `submit_action`. The `_anthropic_completion` method loops up to `POKEDEX_MAX_LOOKUPS` times, executing lookups and appending tool results, then forces `submit_action`. DeepSeek/OpenRouter are unaffected (they don't use Anthropic-style tool calling).
- **Auto-enrich** (`POKEDEX_AUTO_ENRICH=1`): A `=== POKEDEX NOTES ===` block is appended to the battle state text in `choose_move` for ALL providers. Adds ~100-200 tokens per turn with ability/item/move descriptions.

Data layer: `agents/pokedex.py` — lookup functions return formatted strings. `GenData` (poke-env) provides move stats, species data, type chart. Extracted JSON in `/app/data/` provides text descriptions for items, abilities, and moves (built at Docker image time by `agents/scripts/extract_showdown_data.py`).

## Gotchas

- **Gen 1 (and similar) battle formats:** poke-env’s `Move` helpers (e.g. `.heal`) assume movedex fractions exist; Gen 1 moves like **Recover** can have `null` entries and crash inside poke-env. `agents/llm_player.py` `_move_summary` uses **`_safe_move_attr`** so optional move metadata is skipped instead of aborting the turn.
- **`/manager/config`:** Unauthenticated like the rest of `/manager`. When the host `.env` is mounted writable, anyone who can reach the web port can change API keys and stream settings. Restrict network access. Saving only updates the file; restart or recreate containers (`docker compose up -d`, `scripts/restart_stack.sh`) so `agents` and `stream` see new values.
- **Bind-mount `./.env`:** Create the host file before the first `docker compose up` (`cp .env.example .env`). If `.env` is missing, Docker can create a **directory** named `.env`, which breaks Compose env substitution and the Config page mount.
- **Queue worker:** The agents container runs `queue_worker.py` by default. Match count and battle format come from the manager API / SQLite queue, not from env vars.
- **Compose env passthrough:** Variables must appear under a service's `environment:` block (or `env_file:`) to reach a container — a key-only line in `.env` is not enough. `docker-compose.yml` forwards LLM timeouts, OpenRouter tuning knobs, Pokédex flags, etc., for the agents service; add more there if you introduce new agent-side env vars.
- **Volume rename:** `overlay-data` was renamed to `web-data` for `/data`. Existing deployments keep old volume names until recreated; copy data or reattach the old volume name in `docker-compose.yml` if needed.
- **Manager DB / `queue/next` errors:** Startup runs `init_db()` then **`_migrate_sqlite_columns()`** (`web/manager/db.py`) to add any missing columns on existing `manager.db` files (e.g. `queued_at`) plus queue indexes. If you still see SQLite errors, check `docker compose logs web`, then delete `manager.db` (and `-wal`/`-shm` if present) on the **`manager-data`** volume and restart **`web`**, or recreate that volume / use **`docker compose down -v`** / **`scripts/stack_down.sh -v`** for a full wipe.
- **aiosqlite `threads can only be started once`:** Indicates the forbidden double-enter pattern above. Rebuild `web` from current sources (`docker compose up -d --build web`) so `db._db()` is used everywhere.
- **Clearing persisted data:** Replays, logs, and live JSON state use **`replay-data`**, **`log-data`**, **`state-data`** (and legacy **`web-data`** for flat `results.json`). Manager tournaments/matches use **`manager-data`**. Dropping **all** named volumes: **`docker compose down -v`** or **`bash scripts/stack_down.sh -v`**. Manager-only reset: remove `manager.db` (and `-wal`/`-shm` if present) under **`/manager-data`** in the **`web`** container, then restart **`web`**.
- **Failed tournament matches:** A match reported to `/api/manager/matches/{id}/error` cancels its parent **series** (and any still-queued games in that series). **Round-robin** tournaments may then mark **completed** once every series is finished or cancelled. **Single/double elimination** may still need a manual **tournament cancel** in the UI if the bracket depended on that series; the error JSON may include a `recovery_hint`.
- **Legacy `POST /result`:** Prefer sending `winner_side` or `player1_name` / `player2_name` plus provider/model/persona fields so `/stats` stays accurate (queue worker uses the manager complete endpoint instead).
- `HIDE_BATTLE_UI` is set in `docker-compose.yml` (default `1`) but not always in `.env.example`. Override it in `.env` to toggle.
- Persona prompt templates use Python `str.format()` with two variables: `{player_name}` and `{opponent_name}`. Other placeholders will raise `KeyError`.
- Showdown is cloned and built inside its Docker image from the upstream smogon/pokemon-showdown repo. Config overrides are in `showdown/config/config.js`.
- The stream container needs `shm_size: 2gb` for Chromium.
- Storage paths like `/replays`, `/logs`, `/state`, `/data`, `/manager-data` are in-container paths backed by Docker named volumes, not host mounts.
- Pokédex text data (`/app/data/*.json`) is extracted at Docker build time from GitHub. If the build runs without network access, the files will be empty and lookups will return "not found" — the agent still functions (falls back gracefully).
- `POKEDEX_TOOL_ENABLED` only affects Anthropic; DeepSeek/OpenRouter ignore it. `POKEDEX_AUTO_ENRICH` affects all providers.
