# Technical Reference

_Repo name: `pokemon-llm-showdown`_

This document keeps the in-depth technical and operational reference for humans working on or operating the project. If you just want to get the stack running, start with the top-level `README.md`.

## Architecture

```
docker-compose.yml
├── assets/      — Shared static art: `static/trainers/`, `static/portraits/` (mounted into `web` and Showdown; see `assets/README.md`)
├── showdown/    — Local Pokémon Showdown server (no auth)
│   ├── config/    — Server config overrides (port, auth, throttle)
│   └── static/    — Custom Showdown UI (`index.html` override only; trainer art lives under `assets/static/trainers/`)
├── agents/      — Queue worker + poke-env LLM players (`queue_worker.py`, `match_runner.py`)
│   └── personas/  — Markdown persona definitions (`*.md`); optional runtime memory under `STATE_DIR/personas/{slug}/` when `ENABLE_MEMORY=true`
├── web/         — FastAPI: scoreboard, `/manager`, `/api/manager`, broadcast, unified splashes (`/victory` / `/splash`), thoughts (`GET /thoughts` + `/thoughts/ws`); SSE in `scoreboard_stream.py` (`/scoreboard/stream`) and `manager_stream.py` (`/api/manager/stream`)
│   ├── manager/   — `db.py` (aiosqlite + migrations), `tournament_logic.py` (brackets), `tournament_definition.py` (plaintext definitions), `routes.py` (API + HTML)
│   ├── templates/ — Jinja2 (`broadcast.html`, `splash.html`, `overlay.html`, partials, manager dashboards, replays)
│   └── static/    — App JS/CSS/vendor (image-baked; not for mountable art — see `assets/`)
├── stream/      — Xvfb + Chromium + FFmpeg → Twitch RTMP
└── scripts/     — Health, stack lifecycle, manager CLI
```

### Static files: `assets/` vs `web/static`

| Location | Served as | Role |
| --- | --- | --- |
| **`assets/static/`** (repo root) | Bind-mounted paths such as `/static/trainers/` and `/static/portraits/` on **web** (and trainers on **Showdown** where the client needs them) | **Content / skins:** PNG/WebP art you edit on disk. Change files without rebuilding the **web** image. See **`assets/README.md`**. |
| **`web/static/`** | `/static/...` from inside the **web** container | **App bundle:** `manager.js`, `manager.css`, vendor JS, optional default SFX (e.g. `audio/`). Shipped with the **web** Docker image; versioned and released with application code. |

Use **`assets/`** for operator- or artist-owned media shared across services; use **`web/static/`** for static behavior of the FastAPI app itself.

**Persona portraits:** both a tall (512x640) and a square (512x512) file are required per persona. Put them under `assets/static/portraits/` and `assets/static/portraits/square/`, or upload them from the Manager UI.

## Tech Stack

| Area | Technologies |
| --- | --- |
| **Agents** | Python 3.11, [poke-env](https://github.com/hsahovic/poke-env), asyncio, `aiohttp`, Anthropic SDK, OpenAI-compatible SDK (DeepSeek / OpenRouter), optional Pokédex layer, optional persona adaptive memory |
| **Web** | Python 3.11, FastAPI, Uvicorn, Starlette, Jinja2, `aiosqlite` |
| **Showdown** | Node 20, upstream [pokemon-showdown](https://github.com/smogon/pokemon-showdown) |
| **Stream** | Python 3.11, Playwright (Chromium), Xvfb, FFmpeg, PulseAudio |
| **Persistence** | Docker named volumes for SQLite, replays, logs, and shared runtime state |

### Matches DB

Pending work and history both live in the SQLite database on the `manager-data` volume. The `matches` table uses statuses such as `queued`, `running`, `completed`, `error`, and `cancelled`. The worker polls `GET /api/manager/queue/next`, which atomically promotes the oldest queued match to `running`, then later updates that same row with final results.

## Tournament Manager

The Manager UI is the primary way to queue work for the agents. The dashboard, tournament detail, and series detail pages subscribe to `GET /api/manager/stream` via SSE and refresh queue depth, running match, and bracket sections when the backend signals changes.

### Pages

| Path | Purpose |
| --- | --- |
| `/manager` | Dashboard: queue depth, running match, upcoming queue, recent results, recent tournaments |
| `/manager/tournaments` | Tournament list |
| `/manager/tournaments/new` | Create tournament, including plaintext import and presets |
| `/manager/tournament-presets` | List, create, and edit saved plaintext tournament definitions |
| `/manager/tournaments/{id}` | Tournament detail and bracket progress |
| `/manager/matches/new` | Queue a one-off match or short series |
| `/manager/series/{id}` | Series detail |
| `/manager/results` | Completed matches and series |
| `/manager/results/stats` | Aggregate stats |
| `/manager/personas` | Edit persona markdown and upload trainer sprites / portraits |
| `/manager/personas/{slug}/memory` | View adaptive memory files when enabled |
| `/manager/config` | View and edit documented env vars from the host `.env` |

### JSON API

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/manager/config` | GET | Allowed providers, battle formats, persona list |
| `/api/manager/tournaments` | GET, POST | List / create tournament |
| `/api/manager/tournaments/parse-definition` | POST | Parse plaintext tournament definition |
| `/api/manager/tournaments/{tid}` | GET | Tournament + entries + series |
| `/api/manager/tournaments/{tid}/cancel` | POST | Cancel tournament and pending work |
| `/api/manager/tournament-presets` | GET, POST | List presets / create |
| `/api/manager/tournament-presets/{id}` | GET, PATCH, DELETE | Get, update, delete preset |
| `/api/manager/series` | POST | Create a series |
| `/api/manager/series/{sid}` | GET | Series + matches |
| `/api/manager/matches` | GET, POST | List / create standalone match |
| `/api/manager/matches/{mid}` | GET | One match row |
| `/api/manager/matches/{mid}/start` | POST | Mark running |
| `/api/manager/matches/{mid}/complete` | POST | Record completion |
| `/api/manager/matches/{mid}/error` | POST | Record failure |
| `/api/manager/queue/next` | GET | Worker dequeue endpoint |
| `/api/manager/queue/depth` | GET | Count of queued matches |
| `/api/manager/queue/upcoming` | GET | Next queued matches for UI |
| `/api/manager/queue/running` | GET | Current running match |
| `/api/manager/stream` | GET | Manager SSE refresh hints |
| `/api/manager/results` | GET | Completed matches |
| `/api/manager/stats` | GET | Aggregate analytics |

## Tournaments

- **Round robin**: every pair of entries plays a best-of-N series, and the event finishes when every series is complete or cancelled.
- **Single elimination**: one winners bracket; when the final completes, the tournament is done.
- **Double elimination**: winners bracket, losers bracket, then **grand finals** (winners-bracket champion is player 1, losers-bracket champion is player 2). If **player 1 wins** the first grand-finals series, the tournament completes immediately. If **player 2 wins** that set, a **`grand_finals_reset`** series is queued with the same pairing (still WB rep as player 1); whoever wins the reset completes the tournament. Implementation: `tournament_logic.py` (`_create_grand_finals_reset_series`, `bracket == "grand_finals_reset"`).

### `single_elim_bracket`: compact vs power_of_two

Tournament definitions and the manager form expose **Single Elim Bracket** / `single_elim_bracket` with two values: **`compact`** (default; plaintext aliases include `dense`) and **`power_of_two`** (aliases include `classic`, `pow2`, `padded`; see `_BRACKET_ALIASES` in `tournament_definition.py`).

| Mode | Winners bracket behavior |
| --- | --- |
| **`power_of_two`** | Always pad the field to the **next power of two** (`2^k`). Entrants are placed with **standard tournament seeding** (e.g. 1 vs bottom seed on opposite halves). **Empty slots** in round 1 are **byes** that auto-advance; top seeds get the bye advantage. |
| **`compact`** | If the entrant count **is** a power of two, generation matches the **same classic tree** as `power_of_two` (see `_elimination_uses_power_of_two_winners` in `tournament_logic.py`). If the count **is not** a power of two, round 1 schedules **only real pairings**—no phantom “empty” slots—using **compact routing** (odd survivor counts carry a **rest** forward until paired; byes when **N** is odd favor the **best seeds**). Example: **6** players → **3** round-1 series instead of padding to **8**. |

**Single elimination** uses the above directly: `power_of_two` → `_generate_single_elimination_power_of_two`, `compact` (and legacy null) → compact path when `N` is not a power of two.

**Double elimination** stores the same column, but **`generate_bracket` always calls `_generate_double_elimination_power_of_two`** so the winners bracket stays aligned with the losers bracket (sized from `next_power_of_two(N)`). A **`_generate_double_elimination_compact`** helper exists in the same module but is **not** used by the dispatcher today—picking compact vs power_of_two for **double** elim is persisted on the tournament row but **does not change** generated series in the current code path.

### Double-elimination caveats

- WB→LB mapping for **very large** fields is heuristic; 4- and 8-player flows are the most intentional.

## Plaintext Tournament Definitions and Presets

A tournament definition is a single plain-text file or paste buffer that mirrors the new-tournament form. You can paste it into `/manager/tournaments/new`, upload a `.txt`, or save it as a preset in `/manager/tournament-presets`.

### File structure

1. Header block with `Key: value` lines. Keys are case-insensitive and normalize spaces / hyphens.
2. A line that is exactly `Participants:` or `Participant:` starts the roster section.
3. Participant lines continue until EOF.

### Header keys

| Key | Required | Meaning |
| --- | --- | --- |
| `Name` | Yes | Tournament display name |
| `Type` | Yes | `round robin`, `single elimination`, or `double elimination` |
| `Battle Format` | Yes | Showdown format id such as `gen9randombattle` |
| `Best Of` | Yes | Odd positive series length such as `1`, `3`, `Bo3`, `bo5` |
| `Single Elim Bracket` | No | `compact` or `power_of_two` for elimination brackets |

### Participant lines

Each participant line is either comma-separated or pipe-separated:

- 3 fields: `provider`, `model`, `persona_slug`
- 4 fields: the same plus integer `seed`

Providers are `anthropic`, `deepseek`, or `openrouter`. Persona values must match a file in `agents/personas/`. If any row includes a seed, all rows must include one.

### Example

```text
# Weekend mix - double elim, Gen 9
Name: OpenRouter showcase
Type: Double Elimination
Battle Format: gen9randombattle
Best Of: Bo3
Single Elim Bracket: compact

Participants:
anthropic, claude-sonnet-4-20250514, aggro
deepseek, deepseek-chat, stall
openrouter, google/gemini-2.5-flash-lite, neutral
```

## Scripts

All scripts live in `scripts/` and are run from the repo root. The `web` service must be up for manager CLI scripts.

| Script | Purpose |
| --- | --- |
| `healthcheck.sh` | Probe Showdown, web health, and scoreboard SSE |
| `complete_queued_matches.sh` | Mark queued matches completed for testing |
| `restart_stack.sh` | Restart core services; `--stream` includes Twitch capture |
| `stack_down.sh` | Stop the stack; optional volume removal |
| `stack_down_after_tournament.sh` | Wait for tournament completion, then stop the stack |
| `create_match.sh` | Create a match or best-of series through the manager API |
| `create_tournament.sh` | Create a tournament through the manager API |

## Personas

Each player is assigned a persona, defined as a Markdown file in `agents/personas/`. The repo ships examples including `aggro`, `stall`, `nerd`, `neutral`, `gambler`, `zoomer`, `villain`, and `racer`.

### Adding a persona

Create a Markdown file in `agents/personas/` with YAML front matter and a prompt body:

```markdown
---
name: Your Persona Name
abbreviation: Short
description: One-line description.
---

You are a Pokemon battle AI named {player_name}.
Your opponent is {opponent_name}.

(battle style, reasoning voice, callout guidance...)
```

The prompt body supports `{player_name}` and `{opponent_name}` via Python `str.format()`. Portraits and trainer assets should use the same slug as the persona filename.

### Persona adaptive memory

When `ENABLE_MEMORY=true`, each persona slug can accumulate:

| File | Role |
| --- | --- |
| `memory.md` | Rolling battle diary of recent matches |
| `learnings.md` | Curated tactical learnings |
| `_memory_state.json` | Internal match counters for reflection timing |

At match start, the system injects these files into the prompt before action instructions. After qualifying matches, the same provider/model may generate a reflection that appends memory and periodically refreshes learnings.

## Pokédex Tools

Two optional modes are available:

- **Tool calling**: when `POKEDEX_TOOL_ENABLED=true`, Anthropic models can perform lookups before submitting an action.
- **Auto-enrich**: when `POKEDEX_AUTO_ENRICH=true`, all providers receive a compact `POKEDEX NOTES` block in the battle context.

## How a Battle Works

1. `agents/queue_worker.py` pulls the next match from the manager API.
2. `match_runner.py` creates two `LLMPlayer` instances.
3. Each player connects to the local Showdown server over WebSocket.
4. Each turn, the provider receives battle state and returns a structured JSON action.
5. Reasoning and callouts are posted to `/thought`.
6. On completion, the worker reports results, saves the replay, and optionally saves raw battle logs.
7. The worker waits for the configured delay, then polls the queue again.

## Streaming with OBS

Use OBS when you want custom layout control or to handle encoding yourself instead of running the `stream` service.

### Recommended sources

| Order | URL | Notes |
| --- | --- | --- |
| 1 | `/broadcast/battle_frame` | Showdown battle frame |
| 2 | your own webcam / graphics | Optional |
| 3 | `/overlay` | Transparent scoreboard |
| 4 | `/thoughts_overlay` | Transparent reasoning panels |
| 5 | `/broadcast/top_bar` | Transparent title and format pill |
| 6 | `/victory` or `/splash` | Match intros, victory, bracket/upcoming splashes |

Use `http://localhost:8080/...` when OBS is running on the same machine as Docker. Do not run OBS Twitch output and the Docker `stream` service to the same channel unless you intentionally want double encoding.

## Services

| Service | Port | Description |
| --- | --- | --- |
| `showdown` | 8000 | Local Pokémon Showdown battle server |
| `web` | 8080 | FastAPI: scoreboard, broadcast, manager UI/API, thoughts, replays |
| `agents` | - | Queue worker + LLM players |
| `stream` | 9222 | Headless browser capture and FFmpeg Twitch output |

## Useful Commands

### Monitoring

```bash
docker compose logs -f agents
docker compose logs -f stream
curl http://localhost:8080/scoreboard
curl -sS -N --max-time 3 http://localhost:8080/scoreboard/stream
curl -sS -N --max-time 16 http://localhost:8080/api/manager/stream
```

### Local viewing

```bash
open http://localhost:8080/broadcast
open http://localhost:8080/overlay
open http://localhost:8080/broadcast/battle_frame
open http://localhost:8080/thoughts_overlay
open http://localhost:8080/broadcast/top_bar
open http://localhost:8080/manager
open http://localhost:8080/replays
```

### Tests

```bash
cd web
pip install -r requirements-dev.txt
pytest manager/verify_brackets_test.py -v
```

## Endpoints

| Endpoint | Method | Description |
| --- | --- | --- |
| `/broadcast` | GET | Full broadcast scene |
| `/broadcast/battle_frame` | GET | Battle-only frame for OBS layering |
| `/overlay` | GET | Transparent scoreboard |
| `/victory` and `/splash` | GET | Unified splash page |
| `/scoreboard` | GET | Current scoreboard JSON |
| `/scoreboard/stream` | GET | Scoreboard SSE updates |
| `/thoughts` | GET | Current LLM reasoning |
| `/thoughts/ws` | WebSocket | Real-time thought stream |
| `/replays` | GET | Replay and log index |
| `/health` | GET | Health check |

## Configuration Summary

See `.env.example` for the full set of variables. The most important groups are:

- LLM provider keys: `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`
- Battle pacing and queue: `QUEUE_POLL_INTERVAL`, `TURN_DELAY_SECONDS`, `DELAY_BETWEEN_MATCHES`, `LLM_TURN_TIMEOUT`
- Broadcast timing: `MATCH_INTRO_SECONDS`, `VICTORY_MODAL_SECONDS`, `TOURNAMENT_VICTORY_MODAL_SECONDS`, `BRACKET_INTERSTITIAL_SECONDS`
- Optional features: `POKEDEX_TOOL_ENABLED`, `POKEDEX_AUTO_ENRICH`, `ENABLE_MEMORY`
- Stream settings: `TWITCH_STREAM_KEY`, `STREAM_VIEW_URL`, `STREAM_AUDIO_SOURCE`
- Storage paths: `REPLAY_DIR`, `LOG_DIR`, `STATE_DIR`

## Troubleshooting

| Problem | Cause | Fix |
| --- | --- | --- |
| `/scoreboard` or `/api/manager/queue/next` returns 500 with `threads can only be started once` | Invalid `aiosqlite` usage in an out-of-date web container | Rebuild the `web` image from current sources |
| Agents exit immediately | Missing or invalid API key | Check `docker compose logs agents` and verify `.env` |
| Agents sit idle | No matches queued | Queue work in `/manager` or with the CLI scripts |
| Stream shows wrong or blank page | Old `STREAM_VIEW_URL` | Use `http://web:8080/broadcast` |
| Showdown unhealthy | Initial build still running or container crashed | Check `docker compose ps` and `docker compose logs showdown` |
| Persona memory never updates | `ENABLE_MEMORY` disabled or not forwarded into `agents` | Set the env var and restart `agents` |
| `/manager/config` cannot edit the env file | Host `.env` missing or was mounted incorrectly | Recreate a real file from `.env.example` and restart `web` |

