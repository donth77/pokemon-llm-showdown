# CLAUDE.md

## Project Overview

Two LLM-powered agents battle on a local Pokémon Showdown server. The **manager** (web UI + API) and **scripts** queue matchups and tournaments so you can **compare models, providers, and personas** via results, stats, and replays. **Streaming is optional:** core Compose is `showdown` + `web` + `agents`; the **`stream`** service adds headless Xvfb + Chromium + FFmpeg → Twitch RTMP (no OBS required for that path). You can also drive the same broadcast URLs from **OBS** (see README).

## Architecture

Four Docker services on a shared bridge network (`battle-net`), plus repo-root **`assets/static/`** (trainer sprites + persona portraits) bind-mounted into **`web`** and **`showdown`** — see **Static files (`assets/` vs `web/static`)** below and **`assets/README.md`**.

| Service | Dir | Port | Role |
|---------|-----|------|------|
| `showdown` | `showdown/` | 8000 | Local Pokémon Showdown battle server (Node 20, no auth) |
| `web` | `web/` | 8080 | FastAPI: scoreboard, `/manager` + tournament API, broadcast, victory splash, thoughts WebSocket, replay index |
| `agents` | `agents/` | — | LLM battle agents (Python, poke-env); queue worker polls `/api/manager` for matches |
| `stream` | `stream/` | 9222 | Xvfb + Chromium + FFmpeg → Twitch RTMP capture pipeline |

**Data flow:** Agents ↔ Showdown (WebSocket game protocol) · Agents → Web (`/thought`, match completion via `/api/manager/matches/{id}/complete`, plus `current_battle.json` / `thoughts.json` on `state-data`) · Optional persona memory files on `state-data` (`/state/personas/{slug}/memory.md`, `learnings.md`, `_memory_state.json` when `ENABLE_MEMORY=1`) · Stream → Web + Showdown (HTTP, for browser capture only)

**Shared Docker volumes:**
- `web-data` → `/data` (legacy flat `results.json` if used; scoreboard primarily reads SQLite on `manager-data`)
- `manager-data` → `/manager-data` (SQLite for tournaments/matches/queue and **tournament definition presets** — `manager.db`)
- `replay-data` → `/replays` (HTML replay exports)
- `log-data` → `/logs` (raw JSON battle logs)
- `state-data` → `/state` (`current_battle.json`, `thoughts.json` — written by agents, read by web; optional **`personas/{slug}/`** adaptive memory files when enabled)

**Match queue (design):** The queue is **not** a separate service — it is **`matches` rows** in SQLite (`status`, `queued_at`). Dequeue = transactional **UPDATE** `queued` → `running` in `pop_next_queued_match()`. Completed matches remain in the same table (`completed` / `error` / `cancelled`) for results and stats. Workers pull jobs via **`GET /api/manager/queue/next`** (HTTP polling in `agents/queue_worker.py`), not via Redis or an internal task queue.

## Static files (`assets/` vs `web/static`)

- **`assets/static/`** (repo root) — **Mountable content** in Compose (e.g. `./assets/static/trainers` → `/app/static/trainers` on **web** and Showdown’s `server/static/trainers`; `./assets/static/portraits` → `/app/static/portraits` on **web**). Trainer sprites; **persona portraits** (tall under `portraits/`, square under `portraits/square/` — PNG/GIF/WebP; recommended **512×640** and **512×512**); **both portraits are required** per persona (`personas_store.require_both_portraits` on manager create/save). Future optional `audio/` if you add a volume. Operators can swap files on disk without `docker compose build web`, or use **Manager → Personas** upload. **`PORTRAITS_DIR`** (default `/app/static/portraits`). See **`assets/README.md`**.
- **`web/static/`** — **Application static bundle** copied into the **web** image (`Dockerfile` + `StaticFiles` at `/static/`). JS, CSS, vendor scripts, optional default UI sounds (e.g. `victory.html` → `/static/audio/…`). Same release lifecycle as Python/templates — not the home for large persona art.

## Tech Stack

- **Agents:** Python 3.11, poke-env, asyncio, **aiohttp**, Anthropic SDK, OpenAI-compatible SDK (DeepSeek / OpenRouter), optional Pokédex data layer (`agents/pokedex.py`), optional **persona adaptive memory** (`match_runner.py` + `reflection_json_completion` in `llm_player.py`, default off via `ENABLE_MEMORY`)
- **Web:** Python 3.11, **FastAPI**, **Uvicorn**, **Starlette**, **Jinja2**, **aiosqlite** (async SQLite). App code under `web/`; tournament/match persistence and API in `web/manager/` (`db.py`, `routes.py`, `tournament_logic.py`, `tournament_definition.py` — plaintext tournament parse/validate, `env_registry.py`, `env_host_file.py`, `personas_store.py`, `showdown_accounts.py`)
- **Showdown:** Node 20, upstream [pokemon-showdown](https://github.com/smogon/pokemon-showdown) repo
- **Stream:** Python 3.11, Playwright (Chromium), Xvfb, FFmpeg, PulseAudio
- **Infra:** Docker Compose v2, bridge network, named volumes; `web` mounts `./agents/personas` at `PERSONAS_DIR` (default `/personas`), `./assets/static/trainers` → `/app/static/trainers`, `./assets/static/portraits` → `/app/static/portraits`. Optional: `./.env` → `/app/host.env` with `MANAGER_HOST_ENV_FILE=/app/host.env` so `/manager/config` can edit the project env file (restart stack to apply to all services). Broadcast timing env vars (`MATCH_INTRO_SECONDS`, `VICTORY_MODAL_SECONDS`, `TOURNAMENT_VICTORY_MODAL_SECONDS`, `VICTORY_SHOW_DELAY_SECONDS`, …) must be listed on the **`web`** service in `docker-compose.yml` to take effect in containers (not only present in the mounted host file).

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
| `/manager/tournaments`, `/manager/tournaments/new`, `/manager/tournaments/{tid}` | GET | Tournament list, create form (plaintext import + presets), detail |
| `/manager/tournament-presets`, `/manager/tournament-presets/new`, `/manager/tournament-presets/{id}/edit` | GET | Saved definition presets (list, create, edit) |
| `/manager/matches/new` | GET | Queue one-off match / series |
| `/manager/series/{sid}` | GET | Series + games |
| `/manager/results`, `/manager/results/stats` | GET | Completed matches, aggregate stats |
| `/manager/personas` | GET | Persona markdown + trainer / portrait uploads (disk: `assets/static/…`) |
| `/manager/config` | GET | Documented env vars (`env_registry.py`); values from mounted host `.env` + web process env |
| `/manager/config/update` | POST | Form: update one registered key in host `.env` (`key`, `value`) |
| `/api/manager/config` | GET | Providers, formats, personas for UI |
| `/api/manager/tournaments` | GET, POST | List / create tournaments |
| `/api/manager/tournaments/parse-definition` | POST | Parse plaintext definition → JSON body for `POST /tournaments` (`{ "text": "..." }` → `ok`, `data`, `errors`, `warnings`) |
| `/api/manager/tournaments/{tid}` | GET | Tournament payload |
| `/api/manager/tournaments/{tid}/cancel` | POST | Cancel |
| `/api/manager/tournament-presets` | GET, POST | List presets (id, name, updated_at) / create (validated definition + unique name) |
| `/api/manager/tournament-presets/{id}` | GET, PATCH, DELETE | Get full preset (incl. body), update name/body, delete |
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
| `/broadcast` | GET | Full broadcast scene: battle iframe + scoreboard `/overlay` route + thoughts. Parent polls **`/scoreboard` every 250ms** and fans out to embedded **`/overlay`**, **`/match_intro`**, **`/victory`** (`?embed=broadcast`) via **`postMessage`**; thoughts use the **`broadcast_scoreboard`** `CustomEvent`. Standalone pages keep their own intervals. |
| `/broadcast/battle_frame` | GET | Showdown iframe + battle sync + callouts (OBS layering) |
| `/match_intro` | GET | Matchup intro card (`MATCH_INTRO_SECONDS`; iframe child of `/broadcast`) |
| `/tournament_intro` | GET | Tournament opener before the first match only (`TOURNAMENT_INTRO_SECONDS` on agents; iframe child of `/broadcast`) |
| `/broadcast/top_bar` | GET | Transparent title + format bar (OBS layering) |
| `/thoughts_overlay` | GET | Transparent LLM thoughts panels (OBS layering) |
| `/overlay` | GET | Transparent scoreboard page for compositing (URL path name; not the service name) |
| `/victory` | GET | Animated post-match winner splash (`VICTORY_MODAL_SECONDS` vs `TOURNAMENT_VICTORY_MODAL_SECONDS` when the win clinches the tournament) |
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
- **SQLite (web):** In **`web/manager/db.py`**, use **`async with _db() as db:`** (the bound name `db` is the **connection**, despite the name). From other modules (`routes.py`, `tournament_logic.py`, …), **`from . import db`** then **`async with db._db() as conn:`**. `_db()` is an `@asynccontextmanager` around **`async with aiosqlite.connect(...)`** and sets WAL + `foreign_keys`. Do **not** `await aiosqlite.connect()` and then `async with` the same `Connection`: aiosqlite starts a worker thread on connect/`__aenter__`, and doing both triggers `RuntimeError: threads can only be started once`.
- **LLM output format:** Structured JSON with `action_type`, `index`, `reasoning`, optional `callout` — defined in `ACTION_FORMAT_INSTRUCTIONS` in `match_runner.py` (appended after optional memory blocks in `build_system_prompt()`). Post-match **reflection** uses `reflection_json_completion()` in `llm_player.py` (no tools; JSON object with `memory_entry` and optional `learnings_update`).
- **Pokédex data layer:** `agents/pokedex.py` provides lookup functions for moves, species, abilities, items, and type matchups. Move/species/type data comes from poke-env `GenData`; item/ability/move text descriptions are extracted from Showdown's upstream repo at build time by `agents/scripts/extract_showdown_data.py` into `/app/data/*.json`.
- **Inter-service state:** JSON files on shared Docker volumes plus SQLite on `manager-data` are the integration contract between agents and web.
- **Scripts:** Bash with `set -euo pipefail`.
- **No test suite:** No `tests/` directory, no CI/CD workflows. Health verification is via `scripts/healthcheck.sh`.

## Personas

Persona files live in `agents/personas/*.md`. Each has YAML front matter (`name`, `abbreviation`, `description`) and a free-form prompt body. The slug is the filename without `.md`. Showdown usernames default from **`name`** (see `_make_player_name` in `match_runner.py`), not from the LLM model id.

Shipped example slugs include `aggro`, `stall`, `nerd`, `neutral`, `gambler`, `zoomer`, `villain`, `racer` (see repo `agents/personas/`).

**Adaptive memory (optional, default off):** When `ENABLE_MEMORY=1`, `match_runner._load_persona_memory_texts()` reads `/state/personas/{slug}/memory.md` and `learnings.md`; `build_system_prompt()` injects them before `ACTION_FORMAT_INSTRUCTIONS`. After each successful match, `_post_match_persona_memory()` may call `_run_one_persona_reflection()` (same provider/model as that side) to append a `## Match ...` diary block and optionally rewrite learnings; intervals and caps via `MEMORY_REFLECTION_INTERVAL`, `LEARNINGS_UPDATE_INTERVAL`, `MAX_MEMORY_ENTRIES`, `MAX_LEARNINGS_BULLETS`. Same persona slug on both sides: one reflection per match for that slug (deduped `seen_slug` set). Counter persisted in `_memory_state.json`.

Match participants are configured via the **manager** (`/manager` or API), not env-only.

## Tournaments (brackets)

Logic lives in `web/manager/tournament_logic.py` (`generate_bracket`, `on_match_completed`, `on_match_failed`).

| Type | Behavior |
| --- | --- |
| Round robin | All pairs get a series (`best_of` per tournament); completion when all series resolved or cancelled. |
| Single elimination | Winners bracket only; completing the last winners series completes the tournament. |
| Double elimination | Winners + losers + **grand finals**. Completed **winners** series: advance winner in WB; drop loser into LB (pairing rules for small brackets + fallbacks). **Winners finals:** WB champion → grand finals **player 1**; WB finals loser into last LB feeder; LB advances like a secondary bracket; last LB winner → grand finals **player 2**. **Grand finals** completion → tournament `completed`. `_queue_series_matches` skips if the series already has queued/running games. |

**Gaps / product notes:** No **bracket-reset** grand finals (second set if LB winner wins). WB→LB mapping for **very large** fields is heuristic; 4- and 8-player flows are the most intentional. Tournament UI: `tournament_detail.html` shows winners, losers, and grand finals for double elim.

## Tournament definitions (plaintext import) & presets

**Import UI:** On `/manager/tournaments/new`, an optional collapsible block supports pasting a plaintext definition, uploading a `.txt` file, **Parse & fill form** (client → `POST /api/manager/tournaments/parse-definition`), and **Load saved preset** (loads `body` from `GET /api/manager/tournament-presets/{id}` into the textarea). Parsed payloads match the JSON shape of `POST /api/manager/tournaments`.

**User-facing spec** (copy/paste examples): see **README.md** → *Plaintext definitions & presets*.

### File layout

| Section | Rule |
| --- | --- |
| Header | Lines are `Key: value` (split on first `:`). Only one line may define each logical key (duplicates error). Unknown keys error. |
| Comments / blanks | Trimmed line empty, or trimmed line starts with `#` → ignored everywhere. |
| `Participants:` | Section break: the trimmed line must be exactly `Participants:` or `Participant:` (case-insensitive). Text after the colon on that line is **not** supported—the line is parsed as a normal `Key: value` header and fails as an unknown key. |
| Roster | All non-comment lines after the header until EOF; each line = one tournament entry. |

### Header keys (normalized internal names)

After normalization (`_normalize_key` in `tournament_definition.py`): spaces/hyphens → underscores; aliases below map to the canonical key.

| Canonical key | Required | Aliases (examples) | Value |
| --- | --- | --- | --- |
| `name` | Yes | `Tournament Name` | Non-empty string. |
| `type` | Yes | — | Resolved via `_TYPE_ALIASES` to `round_robin`, `single_elimination`, or `double_elimination` (friendly + snake_case + shortcuts like `rr`, `knockout` → single elim). |
| `battle_format` | Yes | `Format`, `BattleFormat` | Non-empty Showdown format id. |
| `best_of` | Yes | `BestOf`, `Bo` | Odd integer ≥ 1; `_parse_best_of` accepts `Bo3`, `3`, `bestof5`-style compact forms. |
| `single_elim_bracket` | No | `Bracket`, `Winners Bracket`, `WinnersBracket` | `compact` (default for elim types if omitted) or `power_of_two` (`_BRACKET_ALIASES`: e.g. `pow2`, `classic`). Meaningless for round robin. |

### Participant lines

- **Delimiter:** If `|` appears in the line, split on `|`; else split on `,`. Parts are stripped; empty parts are dropped (too few fields → error).
- **Fields:** 3 = `provider`, `model`, `persona_slug`. 4 = same + integer `seed` (≥ 1). More than 4 fields → error.
- **Seed rule:** Either zero or all lines carry an explicit seed; mixing errors (`line 0` message).
- **Provider:** Lowercased; must be `anthropic`, `deepseek`, or `openrouter`.
- **Persona:** Must exist in `_scan_personas()` slugs when parsing from the web app.
- **Model:** `provider_model_validate.validate_provider_model` per line.

### Parser module

Implementation: `web/manager/tournament_definition.py` (`parse_tournament_definition`). API validation helper: `_require_valid_tournament_definition_text` in `routes.py` (presets + parity with parse).

### Presets

CRUD in the manager UI (`/manager/tournament-presets`) and the preset API above. Storage: **`tournament_presets`** table in **`/manager-data/manager.db`**. **Create/update** rejects invalid definitions (same parser as parse-definition). **Name** unique case-insensitively (`COLLATE NOCASE`). Wiped with **`manager-data`** on `docker compose down -v` / `scripts/stack_down.sh -v`.

## Environment Variables

All config is via environment variables. Copy `.env.example` to `.env` and edit. Key groups:

- **API keys:** `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`
- **OpenRouter tuning (agents):** `OPENROUTER_STRUCTURED_OUTPUTS`, `OPENROUTER_EXTRA_BODY_JSON` (forwarded in `docker-compose.yml`)
- **Battle pacing:** `TURN_DELAY_SECONDS`, `DELAY_BETWEEN_MATCHES`, `QUEUE_POLL_INTERVAL`, `LLM_MAX_OUTPUT_TOKENS`, `LLM_TURN_TIMEOUT`
- **Tournament intro + match-intro sync (agents):** `TOURNAMENT_INTRO_SECONDS`, `TOURNAMENT_INTRO_DELAY_SECONDS` (queue worker holds before the first match of a tournament when the intro duration is non-zero), `MATCH_INTRO_STARTING_HOLD_SECONDS` (brief `starting` state so `/broadcast` can show `/match_intro` before battle connects) — forwarded in `docker-compose.yml` for `agents`
- **Persona memory (agents):** `ENABLE_MEMORY` (default off), `MEMORY_REFLECTION_INTERVAL`, `LEARNINGS_UPDATE_INTERVAL`, `MAX_MEMORY_ENTRIES`, `MAX_LEARNINGS_BULLETS`, `LLM_MEMORY_REFLECTION_MAX_TOKENS` — forwarded in `docker-compose.yml` for `agents`
- **Pokédex:** `POKEDEX_TOOL_ENABLED` (Anthropic tool calling), `POKEDEX_AUTO_ENRICH` (context injection for all providers), `POKEDEX_MAX_LOOKUPS`
- **Storage:** `REPLAY_DIR`, `LOG_DIR`, `LOG_RAW_BATTLE`, `STATE_DIR` (in-container paths)
- **Stream:** `TWITCH_STREAM_KEY`, `STREAM_VIEW_URL`, `STREAM_AUDIO_SOURCE`
- **Network:** `SHOWDOWN_HOST`, `SHOWDOWN_PORT`, `WEB_HOST`, `WEB_PORT` (deprecated aliases: `OVERLAY_HOST`, `OVERLAY_PORT` still read by agents/stream for migration)
- **Twitch API (optional):** `TWITCH_CLIENT_ID`, `TWITCH_OAUTH_TOKEN`, `TWITCH_BROADCASTER_ID`, `TWITCH_AUTO_SET_TITLE`
- **Broadcast / web UI timing:** `MATCH_INTRO_SECONDS`, `VICTORY_MODAL_SECONDS`, `TOURNAMENT_VICTORY_MODAL_SECONDS`, `VICTORY_SHOW_DELAY_SECONDS`, `HIDE_BATTLE_UI`, `STREAM_TITLE`, `SHOWDOWN_VIEW_BASE` — listed on the **`web`** service in `docker-compose.yml`; **`stream`** receives a smaller subset (e.g. `HIDE_BATTLE_UI`). Defaults apply if omitted from host `.env`.
- **Mount paths (web):** `TRAINERS_DIR`, `PORTRAITS_DIR` (defaults `/app/static/trainers`, `/app/static/portraits` under Compose)
- **Manager Config page:** `MANAGER_HOST_ENV_FILE` (in-container path to mounted host `.env`; `docker-compose.yml` sets `/app/host.env`). Only keys listed in `web/manager/env_registry.py` are editable in `/manager/config`.

See `.env.example` for the full documented list with defaults.

## Pokédex Tools

Optional feature gated by env vars (default off). Two independent modes:

- **Tool calling** (`POKEDEX_TOOL_ENABLED=1`): Anthropic models get five `pokedex_lookup_*` tools alongside `submit_action`. The `_anthropic_completion` method loops up to `POKEDEX_MAX_LOOKUPS` times, executing lookups and appending tool results, then forces `submit_action`. DeepSeek/OpenRouter are unaffected (they don't use Anthropic-style tool calling).
- **Auto-enrich** (`POKEDEX_AUTO_ENRICH=1`): A `=== POKEDEX NOTES ===` block is appended to the battle state text in `choose_move` for ALL providers. Adds ~100-200 tokens per turn with ability/item/move descriptions.

Data layer: `agents/pokedex.py` — lookup functions return formatted strings. `GenData` (poke-env) provides move stats, species data, type chart. Extracted JSON in `/app/data/` provides text descriptions for items, abilities, and moves (built at Docker image time by `agents/scripts/extract_showdown_data.py`).

## Gotchas

- **Gen 1 (and similar) battle formats:** poke-env’s `Move` helpers (e.g. `.heal`) assume movedex fractions exist; Gen 1 moves like **Recover** can have `null` entries and crash inside poke-env. `agents/llm_player.py` `_move_summary` uses **`_safe_move_attr`** so optional move metadata is skipped instead of aborting the turn. **Gen 1 asleep/frozen:** Showdown sends a pseudo-move id **`fight`** (the client “Fight” action, not in the movedex). Older poke-env builds raise `ValueError: Unknown move: fight` when parsing the request because `Move.__init__` reads `max_pp` → `entry`. **`_patch_poke_env_pseudo_move_entries()`** in `llm_player.py` (runs at import) wraps `Move.entry` with synthetic data for **`fight`** and **`recharge`**, matching newer upstream poke-env; `SPECIAL_MOVES.add("fight")` in `LLMPlayer` alone does not fix that parse path.
- **`/manager/config`:** Unauthenticated like the rest of `/manager`. When the host `.env` is mounted writable, anyone who can reach the web port can change API keys and stream settings. Restrict network access. Saving only updates the file; restart or recreate containers (`docker compose up -d`, `scripts/restart_stack.sh`) so `agents` and `stream` see new values.
- **Bind-mount `./.env`:** Create the host file before the first `docker compose up` (`cp .env.example .env`). If `.env` is missing, Docker can create a **directory** named `.env`, which breaks Compose env substitution and the Config page mount.
- **Queue worker:** The agents container runs `queue_worker.py` by default. Match count and battle format come from the manager API / SQLite queue, not from env vars.
- **Compose env passthrough:** Variables must appear under a service's `environment:` block (or `env_file:`) to reach a container — a key-only line in `.env` is not enough. `docker-compose.yml` forwards LLM timeouts, OpenRouter tuning knobs, Pokédex flags, persona memory flags, etc., for the agents service; add more there if you introduce new agent-side env vars.
- **Volume rename:** `overlay-data` was renamed to `web-data` for `/data`. Existing deployments keep old volume names until recreated; copy data or reattach the old volume name in `docker-compose.yml` if needed.
- **Manager DB / `queue/next` errors:** Startup runs `init_db()` then **`_migrate_sqlite_columns()`** (`web/manager/db.py`) to add any missing columns on existing `manager.db` files (e.g. `queued_at`) plus queue indexes. If you still see SQLite errors, check `docker compose logs web`, then delete `manager.db` (and `-wal`/`-shm` if present) on the **`manager-data`** volume and restart **`web`**, or recreate that volume / use **`docker compose down -v`** / **`scripts/stack_down.sh -v`** for a full wipe.
- **aiosqlite `threads can only be started once`:** Indicates the forbidden double-enter pattern above. Rebuild `web` from current sources (`docker compose up -d --build web`) so `db._db()` is used everywhere.
- **Clearing persisted data:** Replays, logs, and live JSON state use **`replay-data`**, **`log-data`**, **`state-data`** (and legacy **`web-data`** for flat `results.json`). Manager tournaments/matches use **`manager-data`**. Dropping **all** named volumes: **`docker compose down -v`** or **`bash scripts/stack_down.sh -v`**. Manager-only reset: remove `manager.db` (and `-wal`/`-shm` if present) under **`/manager-data`** in the **`web`** container, then restart **`web`**.
- **Failed tournament matches:** A match reported to `/api/manager/matches/{id}/error` cancels its parent **series** (and any still-queued games in that series). **Round-robin** tournaments may then mark **completed** once every series is finished or cancelled. **Single/double elimination** may still need a manual **tournament cancel** in the UI if the bracket depended on that series; the error JSON may include a `recovery_hint`.
- **Legacy `POST /result`:** Prefer sending `winner_side` or `player1_name` / `player2_name` plus provider/model/persona fields so `/stats` stays accurate (queue worker uses the manager complete endpoint instead).
- `HIDE_BATTLE_UI` defaults to `1` in `docker-compose.yml` for **`web`** and **`stream`**; override in `.env` (also documented in `.env.example`).
- Persona prompt templates use Python `str.format()` with two variables: `{player_name}` and `{opponent_name}`. Other placeholders will raise `KeyError`.
- Showdown is cloned and built inside its Docker image from the upstream smogon/pokemon-showdown repo. Config overrides are in `showdown/config/config.js`.
- The stream container needs `shm_size: 2gb` for Chromium.
- Storage paths like `/replays`, `/logs`, `/state`, `/data`, `/manager-data` are in-container paths backed by Docker named volumes, not host mounts.
- Pokédex text data (`/app/data/*.json`) is extracted at Docker build time from GitHub. If the build runs without network access, the files will be empty and lookups will return "not found" — the agent still functions (falls back gracefully).
- `POKEDEX_TOOL_ENABLED` only affects Anthropic; DeepSeek/OpenRouter ignore it. `POKEDEX_AUTO_ENRICH` affects all providers.
- **Persona memory:** Default `ENABLE_MEMORY=0` — no extra tokens or reflection calls until enabled. Reflection uses the battle JSON log when `LOG_RAW_BATTLE=1`, else log lines extracted from the saved replay HTML. Wiping **`state-data`** removes memory files; they are **not** stored under `agents/personas/` (source personas stay clean).
