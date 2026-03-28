# AI Pokémon Showdown

Two **LLM-powered agents** face off on a local **Pokémon Showdown** server. Use the **manager** (UI at `/manager`, JSON API, and CLI scripts) to queue matches and tournaments, then **inspect outcomes** — replays, logs, results, and **stats** — to **compare providers, models, personas, and formats**. **Live streaming is optional:** run `showdown`, `web`, and `agents` only when you just want battles and analysis. Add the **`stream`** service for headless Twitch (Xvfb + Chromium + FFmpeg), or **compose the same pages in OBS** (see **Streaming with OBS** below). Each agent can use its own provider, model, and persona, with live reasoning, callouts, and broadcast overlays when you stream.

## Architecture

```
docker-compose.yml
├── showdown/    — Local Pokémon Showdown server (no auth)
│   ├── config/    — Server config overrides (port, auth, throttle)
│   └── static/    — Custom Showdown UI assets (index.html, trainers/)
├── agents/      — Queue worker + poke-env LLM players (`queue_worker.py`, `match_runner.py`)
│   └── personas/  — Markdown persona files (aggro, stall, …)
├── web/         — FastAPI: scoreboard, `/manager`, `/api/manager`, broadcast, victory, thoughts WebSocket
│   ├── manager/   — `db.py` (aiosqlite + migrations), `tournament_logic.py` (brackets), `routes.py` (API + HTML)
│   ├── templates/ — Jinja2 (broadcast, `/overlay`, victory, replays, manager dashboards)
│   └── static/    — `manager.js` / `manager.css`
├── stream/      — Xvfb + Chromium + FFmpeg → Twitch RTMP
└── scripts/     — Health, stack lifecycle, manager CLI, Twitch title (see **Scripts** below)
```

## Tech stack

| Area | Technologies |
| --- | --- |
| **Agents** | Python 3.11, [poke-env](https://github.com/hsahovic/poke-env), asyncio, **aiohttp**, Anthropic SDK, OpenAI-compatible SDK (DeepSeek / OpenRouter). Optional **Pokédex** layer in `agents/pokedex.py`; move/ability/item text built into the image via `agents/scripts/extract_showdown_data.py`. |
| **Web** | Python 3.11, **FastAPI**, **Uvicorn**, **Starlette**, **Jinja2**, **aiosqlite** (WAL + foreign keys via the `_db()` connection helper in `web/manager/db.py`). |
| **Showdown** | Node 20, upstream [pokemon-showdown](https://github.com/smogon/pokemon-showdown) (cloned at image build). |
| **Stream** | Python 3.11, **Playwright** (Chromium), **Xvfb**, **FFmpeg**, PulseAudio. |
| **Persistence** | Docker named volumes: SQLite on `manager-data` (`/manager-data/manager.db`) for tournaments / series / matches / queue; legacy `/data/results.json` on `web-data` if used; replays, logs, and live JSON state on their own volumes. |

### Matches DB

Pending work and history both live in the same SQLite file on the **`manager-data`** volume. The **`matches`** table rows carry a **`status`** (`queued`, `running`, `completed`, `error`, `cancelled`) and a **`queued_at`** timestamp. The worker calls **`GET /api/manager/queue/next`**, which **selects** the oldest `queued` row and **updates** it to `running` in one transaction (`web/manager/db.py` — `pop_next_queued_match`). Completing a match **updates** the row again (`completed` plus winner, replay path, …); rows are **not deleted** — they stay for `/manager/results`, `/api/manager/stats`, and the scoreboard. The **agents** service **`queue_worker.py`** uses **aiohttp** to poll that HTTP API (interval `QUEUE_POLL_INTERVAL`).

## Tournament manager (UI + API)

The **manager** is how you queue work for the agents (default entrypoint is `queue_worker.py`, not env-only player vars).

**Pages** (browser, port 8080):

| Path | Purpose |
| --- | --- |
| `/manager` | Dashboard: queue depth, running match, recent results, tournament list |
| `/manager/tournaments` | All tournaments |
| `/manager/tournaments/new` | Create tournament (round-robin, single / double elimination) |
| `/manager/tournaments/{id}` | Tournament detail and bracket progress |
| `/manager/matches/new` | Queue a one-off match or short series |
| `/manager/series/{id}` | Series detail (best-of, individual games) |
| `/manager/results` | Completed matches |
| `/manager/results/stats` | Aggregate stats (models, formats) |
| `/manager/personas` | Edit persona markdown and trainer sprites |
| `/manager/config` | View and edit documented env vars (host project `.env` when the `web` container mounts it; see `MANAGER_HOST_ENV_FILE` in **UI** env table) |

**JSON API** (used by the queue worker, manager pages, and `scripts/create_match.sh` / `create_tournament.sh`):

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/manager/config` | GET | Allowed providers, battle formats, persona list (from mounted `agents/personas`) |
| `/api/manager/tournaments` | GET, POST | List / create tournament |
| `/api/manager/tournaments/{tid}` | GET | Tournament + entries + series |
| `/api/manager/tournaments/{tid}/cancel` | POST | Cancel tournament and pending work |
| `/api/manager/series` | POST | Create a series (optional `auto_queue` games) |
| `/api/manager/series/{sid}` | GET | Series + matches |
| `/api/manager/matches` | GET, POST | List / create standalone match |
| `/api/manager/matches/{mid}` | GET | One match row |
| `/api/manager/matches/{mid}/start` | POST | Mark running (optional; worker uses `queue/next`) |
| `/api/manager/matches/{mid}/complete` | POST | Worker: winner, replay path, etc. |
| `/api/manager/matches/{mid}/error` | POST | Worker: fail match; may cancel series / hint bracket recovery |
| `/api/manager/queue/next` | GET | Worker: atomically dequeue → `running` (**404** if queue empty) |
| `/api/manager/queue/depth` | GET | Count of `queued` matches |
| `/api/manager/queue/running` | GET | Current `running` match (JSON) or `null` — dashboard live status |
| `/api/manager/results` | GET | Completed matches (filters optional) |
| `/api/manager/stats` | GET | Dashboard analytics |

### Tournaments (formats & double elimination)

- **Round robin** — Each pair of entries plays a **best-of-N** *series* (see **Best Of** on the new-tournament form). The UI shows **standings** from completed series; the event finishes when every series is done or cancelled.
- **Single elimination** — One **winners** bracket. When the winners bracket final completes, the tournament is **completed**.
- **Double elimination** — **Winners** bracket, **losers** bracket, and **grand finals**. Losers from most winners matches are **dropped into the losers bracket**; the losers bracket advances round by round; the **grand finals** series pits the **winners-bracket champion** against the **losers-bracket champion** (the manager tournament page shows WB, LB, and GF blocks). Implementation: `web/manager/tournament_logic.py`.
  - **Why it matters:** Older behavior treated double elim like single elim for advancement (no real LB or GF routing, tournament could finish without a meaningful grand final). The current logic actually **feeds** players through LB and **queues** grand finals when both sides are known.
  - **Caveats:** **Bracket reset** (losers-bracket winner must beat the undefeated player twice) is **not** modeled—one grand-finals series decides the winner. Routing from winners to losers is **exact** for common small sizes (e.g. 4- and 8-player paths); **larger** brackets use heuristics and “first open slot” fallbacks and may not match a strict international double-elim chart for every `N`.

## Scripts

All live in `scripts/` (run from repo root). The **web** service must be up for manager CLI scripts (default base URL `http://localhost:8080`, or set `WEB_URL` / `OVERLAY_URL`).

| Script | Purpose |
| --- | --- |
| `healthcheck.sh` | Probe Showdown + web HTTP health (uses `WEB_HOST` / `WEB_PORT` with `OVERLAY_*` fallback). |
| `restart_stack.sh` | `docker compose` restart for `showdown`, `web`, `agents`; `--stream` includes Twitch capture. |
| `stack_down.sh` | `docker compose down`; optional `-v` / `--volumes` to drop named volumes; optional post-down sleep (see script `--help`). |
| `create_match.sh` | Create a match or best-of series via `POST` to `/api/manager` (requires provider/model/persona flags — run `bash scripts/create_match.sh --help`). |
| `create_tournament.sh` | Create a tournament (round-robin / elimination) via manager API (`bash scripts/create_tournament.sh --help`). |
| `set_twitch_title.sh` | Set Twitch title/category using OAuth env vars from `.env`. |

**Agents** use [poke-env](https://github.com/hsahovic/poke-env) to connect to the local Showdown server and call an LLM each turn to decide a move or switch. Supported providers:

- **Anthropic** (Claude)
- **DeepSeek**
- **OpenRouter** (hundreds of models: Gemini, Llama, Mistral, Qwen, etc.)

Each player is assigned a **persona** — a markdown file that defines the agent's battle style, reasoning voice, and callout personality. Two built-in personas ship with the repo:

| Slug | Name | Style |
| --- | --- | --- |
| `aggro` | Damage Dan | Hyper-offense — swagger, urgency, rival banter |
| `stall` | Stall Stella | Defensive fortress — calm, clinical, composed |

### How a Battle Works

1. The **queue worker** (`agents/queue_worker.py`) pulls the next match from the web service, then `match_runner` creates two `LLMPlayer` instances for that matchup.
2. Both players connect to the local **Showdown** server via WebSocket (using poke-env).
3. Each turn, the active player sends the battle state to its **LLM provider**, which returns a structured JSON action (`action_type`, `index`, `reasoning`, optional `callout`). If **Pokédex tools** are enabled, Anthropic models can optionally look up move/ability/item/type details before committing an action.
4. The player's reasoning and callout are **POSTed** to the web service `/thought` endpoint (and written to `thoughts.json` on the shared volume) for the live broadcast.
5. When the battle ends, the worker **reports** the result via the manager API, saves an HTML replay to `/replays`, and optionally writes a raw JSON log to `/logs`.
6. After a configurable pause (`DELAY_BETWEEN_MATCHES`), the worker polls `/api/manager/queue/next` for the next match (or waits). Configure matchups in **Manager** (`http://localhost:8080/manager`) or CLI scripts (`scripts/create_match.sh`, `create_tournament.sh`).

## Prerequisites

- Docker & Docker Compose v2+
- At least one LLM API key:
  - [Anthropic](https://console.anthropic.com/) (`ANTHROPIC_API_KEY`)
  - [DeepSeek](https://platform.deepseek.com/) (`DEEPSEEK_API_KEY`)
  - [OpenRouter](https://openrouter.ai/keys) (`OPENROUTER_API_KEY`)
- A Twitch account with a stream key ([get it here](https://dashboard.twitch.tv/settings/stream)) — only needed if streaming
- ~4 GB RAM, ~10 GB disk for Docker images
- Multi-core CPU recommended (FFmpeg encoding in the stream container is CPU-intensive)

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/donth77/pokemon-llm-showdown.git && cd pokemon-llm-showdown

# 2. Configure environment
cp .env.example .env
# Edit .env — API key(s), LLM pacing/tuning, and (optionally) Twitch stream key. Match lineups are not driven by PLAYER* vars when using the queue worker (see below).

# 3. Launch without streaming to Twitch
docker compose up -d --build showdown web agents

# 3a. Or launch everything (with Twitch streaming)
docker compose up -d --build

# 4. Check service health
docker compose ps
bash scripts/healthcheck.sh
```

> **Note:** Each match’s provider, model, and persona come from the **Manager** (or API/CLI). The agents container still needs valid **API keys** in `.env` for whatever providers your queued matches use.

## Streaming with OBS

Use this when you want to **layer webcam, alerts, or custom graphics** on top of the battle, or control encoding from OBS instead of the built-in `stream` service.

1. Start **core services only** (no RTMP container):

   ```bash
   docker compose up -d --build showdown web agents
   ```

2. Add **Browser Sources** in OBS (typical size **1280×720**; use the same base URL as your browser, e.g. `http://localhost:8080` if the `web` port is published locally).

3. **Single-source (simplest):** one Browser Source pointing at `/broadcast` — same composite the headless stack opens internally.

4. **Multi-source (layered):** stack transparent pages under “normal” layers so you can insert camera or images **between** the battle and HUD. Suggested **bottom → top** order:

   | Order (bottom first) | URL | Notes |
   | --- | --- | --- |
   | 1 | `/broadcast/battle_frame` | Showdown + battle URL sync + in-battle callouts (solid **#0b1020** background) |
   | 2 | *Your webcam / gameplay frame / images* | — |
   | 3 | `/overlay` | Transparent scoreboard |
   | 4 | `/thoughts_overlay` | Transparent LLM reasoning panels |
   | 5 | `/broadcast/top_bar` | Transparent title + format pill |
   | 6 | `/victory` | Winner splash (transparent) |

   If you split the battle out with `/broadcast/battle_frame`, add **separate** Browser Sources for `/overlay`, `/thoughts_overlay`, `/broadcast/top_bar`, and `/victory` instead of relying on `/broadcast`.

5. **OBS Browser Source (layered setup):**

   - **URL:** Full URL per layer, e.g. `http://localhost:8080/overlay` (same host/port you’d open in a normal browser).
   - **Width / height:** **1280** × **720** on each Browser Source so the layout matches the HTML (then **Transform** → centre or letterbox if your canvas is 1920×1080).
   - **Stacking order:** In OBS, items **higher** in the scene’s source list are drawn **in front**. Put `/broadcast/battle_frame` at the **bottom** of the list and `/victory` **near the top** so HUD layers sit above the battle.
   - **Transparency:** Use a **Browser Source** (not Window Capture) for `/overlay`, `/thoughts_overlay`, `/broadcast/top_bar`, and `/victory`. Those pages use a transparent backing where it matters; if a layer looks opaque, check for an OBS **permit / use transparency** option on the source (wording varies by OBS version).
   - **Troubleshooting:** Enable **Refresh browser when scene becomes active** if a page looks stale after tabbing scenes; **shutdown source when not visible** saves CPU if you duplicate scenes.
   - **Same machine vs LAN:** Battle pages only switch the iframe to `http://localhost:8000` when the **web** page is served from `localhost`. Use `http://localhost:8080/...` in Browser Sources on the **Docker host** so Showdown resolves. If you open the site by **server IP** from another PC, the iframe may still target Docker-internal hostnames and fail—run OBS on the host, or use the headless `stream` container for remote-only viewing.

6. **Showdown from your PC:** with default Compose, **Showdown** is on **port 8000** and **web** on **8080**. Keep Browser Source base URLs on **`localhost`** when OBS and Docker run on the same machine.

7. **RTMP stack vs OBS:** do **not** run the Docker `stream` service *and* capture the same output through OBS into Twitch unless you intend to double-encode. Choose one path: either FFmpeg in Compose → Twitch, or Browser/Page Capture in OBS → Twitch.

*Future idea:* automating OBS scene changes (obs-websocket) from match state is possible with a **local helper** on the streaming PC; it is not part of this repo.

## Services

| Service | Port | Description |
| --- | --- | --- |
| `showdown` | 8000 | Local Pokémon Showdown battle server |
| `web` | 8080 | FastAPI: scoreboard, broadcast, `/manager`, tournament API, victory, thoughts, replays |
| `agents` | — | Queue worker + LLM players (no exposed port; runs until stopped) |
| `stream` | 9222 | Xvfb + Chromium + FFmpeg to Twitch (Chrome debug port exposed) |

## LLM Player Configuration

**Queue worker:** Provider, model, and persona for each side are set **per match** in the Manager UI, `scripts/create_match.sh`, or `scripts/create_tournament.sh`. Keep the right **API keys** in `.env` for those providers.

Valid providers: `anthropic`, `deepseek`, `openrouter`. For OpenRouter, use the full model path (e.g. `google/gemini-2.0-flash`).

### Adding a Persona

Create a new markdown file in `agents/personas/` with YAML front matter and a prompt body:

```markdown
---
name: Your Persona Name
abbreviation: Short
description: One-line description.
---

You are a Pokemon battle AI named {player_name}.
Your opponent is {opponent_name}.

(battle style, reasoning voice, callout guidance…)
```

The prompt body supports two template variables (interpolated via Python `str.format()`):
- `{player_name}` — the player's Showdown login name: normally the **persona display `name`** from YAML (spaces stripped), with a numeric suffix if both sides would collide; optional manager **Showdown account overrides** replace that pair when set.
- `{opponent_name}` — the opponent's persona **abbreviation** (YAML front matter).

In the Manager (or CLI), pick persona **slugs** that match the filename without `.md`.

### Pokédex Tools (Optional)

Agents can optionally access a **Pokédex** data layer for mid-battle lookups — move descriptions, ability effects, item effects, type matchups, and species stats. Two modes are available (can be used independently or together):

**Tool calling (Anthropic only):** When `POKEDEX_TOOL_ENABLED=1`, Anthropic models get five additional tools alongside `submit_action`. The model can call up to `POKEDEX_MAX_LOOKUPS` (default 3) lookups per turn before being forced to submit an action. Other providers are unaffected.

**Auto-enrich (all providers):** When `POKEDEX_AUTO_ENRICH=1`, a `=== POKEDEX NOTES ===` section is appended to the battle state text each turn with brief descriptions of visible abilities, items, and available move effects. This adds ~100-200 tokens per turn to each message.

```bash
# Enable Pokédex tool calling for Anthropic
POKEDEX_TOOL_ENABLED=1

# Enable auto-enriched context for all providers
POKEDEX_AUTO_ENRICH=1

# Max Pokédex lookups per turn (default 3)
POKEDEX_MAX_LOOKUPS=3
```

Data sources: move/species/type data comes from poke-env's bundled `GenData`; item, ability, and move **text descriptions** are extracted from Showdown's upstream repo at Docker build time (`agents/scripts/extract_showdown_data.py`).

## Web endpoints

The HTTP route `/overlay` is still the **scoreboard page** embedded in `/broadcast` (compositing). The Docker service is named **`web`**.

| Endpoint | Method | Description |
| --- | --- | --- |
| `/manager` … `/manager/...` | GET | Manager UI (see **Tournament manager** above) |
| `/api/manager/*` | various | Manager JSON API (see table above) |
| `/broadcast` | GET | Full broadcast scene: battle iframe + scoreboard (`/overlay`) + victory + thoughts |
| `/broadcast/battle_frame` | GET | Showdown iframe + battle sync + callouts only (for OBS layering) |
| `/broadcast/top_bar` | GET | Transparent stream title + format (for OBS layering) |
| `/thoughts_overlay` | GET | Transparent LLM thoughts panels (for OBS layering) |
| `/overlay` | GET | Transparent scoreboard for compositing |
| `/victory` | GET | Animated post-match winner splash |
| `/scoreboard` | GET | Win/loss records, player names/models, recent matches (JSON) |
| `/current_battle` | GET | Live battle metadata (JSON) |
| `/thoughts` | GET | Current LLM reasoning per player (JSON) |
| `/thoughts/ws` | WebSocket | Real-time thought stream for broadcast |
| `/replays` | GET | Replay + log index page (clickable HTML/JSON files) |
| `/health` | GET | Health check |
| `/result` | POST | Legacy flat result *(send `winner_side` or player names + providers for correct `/stats`)* |
| `/thought` | POST | Submit a player thought *(internal — called by agents)* |
| `/thoughts/clear` | POST | Clear thought history *(internal — e.g. between matches)* |

## Useful Commands

### Monitoring

```bash
# View agent logs (battle output)
docker compose logs -f agents

# View stream logs (FFmpeg output)
docker compose logs -f stream

# Check scoreboard
curl http://localhost:8080/scoreboard
```

### Local Viewing

```bash
# View full broadcast scene locally
open http://localhost:8080/broadcast

# View standalone scoreboard page (same as iframe in /broadcast)
open http://localhost:8080/overlay

# OBS layering (transparent pages — use as Browser Sources at 1280×720)
open http://localhost:8080/broadcast/battle_frame
open http://localhost:8080/thoughts_overlay
open http://localhost:8080/broadcast/top_bar

# Tournament manager
open http://localhost:8080/manager

# Browse saved replays and logs
open http://localhost:8080/replays
```

### Operations

```bash
# Restart core services (no stream)
bash scripts/restart_stack.sh

# Restart everything including the stream
bash scripts/restart_stack.sh --stream

# Queue matches (web must be up; the agents service runs queue_worker by default)
bash scripts/create_match.sh --help

# Set Twitch stream title/category via API (requires OAuth env vars)
bash scripts/set_twitch_title.sh

# Stop everything (optional: remove volumes — see Scripts table)
bash scripts/stack_down.sh
# or
docker compose down
```

### Twitch Dashboard Title

To update the Twitch channel title (and optional category) programmatically:

1. Add these to `.env`:
   - `TWITCH_CLIENT_ID`
   - `TWITCH_OAUTH_TOKEN` (must include `channel:manage:broadcast`)
   - `TWITCH_BROADCASTER_ID`
   - Optional: `TWITCH_GAME_ID` (defaults to Pokémon `1982936547`)
   - Optional: `TWITCH_STREAM_TITLE`
2. Run:

```bash
bash scripts/set_twitch_title.sh
```

Or pass a one-off title:

```bash
bash scripts/set_twitch_title.sh "Pokémon Showdown battles with LLMs"
```

When `stream` starts via Docker, it also attempts to set the Twitch title/category automatically if the OAuth env vars are present. Disable with `TWITCH_AUTO_SET_TITLE=0`.

## Configuration

Key environment variables (see `.env.example` for the full list). Under Docker Compose, values reach the `agents` (and `stream`) containers only if they are referenced in `docker-compose.yml` or you add `env_file: .env` — see **Gotchas** in `CLAUDE.md`.

<details>
<summary><strong>LLM Providers</strong></summary>

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | If using Anthropic | — | Anthropic API key |
| `DEEPSEEK_API_KEY` | If using DeepSeek | — | DeepSeek API key |
| `DEEPSEEK_BASE_URL` | No | `https://api.deepseek.com` | DeepSeek API base URL |
| `OPENROUTER_API_KEY` | If using OpenRouter | — | OpenRouter API key |
| `OPENROUTER_BASE_URL` | No | `https://openrouter.ai/api/v1` | OpenRouter API base URL |
| `OPENROUTER_EXTRA_BODY_JSON` | No | — | Extra JSON merged into OpenRouter requests |
| `OPENROUTER_STRUCTURED_OUTPUTS` | No | `auto` | Structured output mode: `auto`, `force`, `off` |

</details>

<details>
<summary><strong>Battle Settings</strong></summary>

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `QUEUE_POLL_INTERVAL` | No | `5` | Seconds between polls when the manager queue is empty (`queue_worker`) |
| `TURN_DELAY_SECONDS` | No | `0` | Delay per turn per player (seconds, for watchability) |
| `DELAY_BETWEEN_MATCHES` | No | `15` | Pause between matches (seconds) |
| `LLM_MAX_OUTPUT_TOKENS` | No | `512` | Max output tokens per LLM turn |
| `LLM_TURN_TIMEOUT` | No | `150` | Hard timeout per LLM call (seconds); falls back to random move |
| `POKEDEX_TOOL_ENABLED` | No | `0` | Enable Pokédex tool calling for Anthropic models (`1`/`0`) |
| `POKEDEX_AUTO_ENRICH` | No | `0` | Inject Pokédex notes into battle context for all providers (`1`/`0`) |
| `POKEDEX_MAX_LOOKUPS` | No | `3` | Max Pokédex tool calls per turn before forcing an action |

</details>

<details>
<summary><strong>UI</strong></summary>

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `HIDE_BATTLE_UI` | No | `1` | Hide Showdown's native battle controls in the broadcast (`1`/`0`). Set in `docker-compose.yml`; add to `.env` to override. |
| `VICTORY_MODAL_SECONDS` | No | `30` | Victory splash duration (seconds) |
| `STREAM_TITLE` | No | *(compose default)* | Headline on the broadcast page (`web` service) |
| `PERSONAS_DIR` | No | `/personas` | In the `web` container, mount of `agents/personas` for manager persona list + `/manager/personas` editor (`docker-compose.yml`) |
| `MANAGER_HOST_ENV_FILE` | No | `/app/host.env` *(via `docker-compose.yml`)* | In the `web` container, path to the mounted host `.env` (`./.env:/app/host.env` in Compose) so `/manager/config` can read/write keys listed in `web/manager/env_registry.py`. Without Compose, leave unset unless you mount a file at the path you configure. |

</details>

<details>
<summary><strong>Storage</strong></summary>

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `REPLAY_DIR` | No | `/replays` | Replay HTML export directory (in-container path) |
| `LOG_DIR` | No | `/logs` | Raw battle log (JSON) export directory (in-container path) |
| `LOG_RAW_BATTLE` | No | `1` | Toggle raw JSON log export (`1`/`0`) |
| `STATE_DIR` | No | `/state` | Live battle metadata directory (in-container path) |

</details>

<details>
<summary><strong>Streaming</strong></summary>

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `TWITCH_STREAM_KEY` | If streaming | — | Your Twitch stream key |
| `STREAM_VIEW_URL` | No | `http://web:8080/broadcast` | Browser source URL for stream capture |
| `STREAM_TITLE` | No | `Pokémon Showdown battles with LLMs` | Title text on broadcast scene |
| `STREAM_AUDIO_SOURCE` | No | `pulse` | Audio source: `browser` (capture tab audio) or `pulse` (default mic/source) |

</details>

<details>
<summary><strong>Network</strong></summary>

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `SHOWDOWN_HOST` | No | `showdown` | Showdown server host |
| `SHOWDOWN_PORT` | No | `8000` | Showdown server port |
| `WEB_HOST` | No | `web` | Web service hostname (Docker network) |
| `WEB_PORT` | No | `8080` | Web service port |

Deprecated aliases (still read by agents/stream): `OVERLAY_HOST`, `OVERLAY_PORT`. Scripts accept `OVERLAY_URL` as a fallback for `WEB_URL`.

</details>

<details>
<summary><strong>Twitch API (optional)</strong></summary>

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `TWITCH_CLIENT_ID` | No | — | Twitch app client ID |
| `TWITCH_OAUTH_TOKEN` | No | — | Twitch OAuth token (`channel:manage:broadcast`) |
| `TWITCH_BROADCASTER_ID` | No | — | Twitch broadcaster user ID |
| `TWITCH_AUTO_SET_TITLE` | No | `1` | Auto-set title on stream start (`1`/`0`) |
| `TWITCH_GAME_ID` | No | `1982936547` | Twitch category ID (defaults to Pokémon) |
| `TWITCH_STREAM_TITLE` | No | `Pokémon Showdown battles with LLMs` | Title for Twitch API updates |

</details>

## Troubleshooting

| Problem | Cause | Fix |
| --- | --- | --- |
| **`/scoreboard` or `/api/manager/queue/next` returns 500** (`RuntimeError: threads can only be started once` in `web` logs) | Fixed in current `web/manager/db.py`: SQLite must be opened with **`async with aiosqlite.connect(...)`** (the `_db()` helper), not `await connect()` plus a second `async with` on the same handle | **`docker compose up -d --build web`** (or rebuild the whole stack) so the container matches the repo |
| Agents exit immediately | Missing or invalid API key for the configured provider | Check `docker compose logs agents` for the error; verify the key in `.env` matches the provider |
| Agents sit idle after start | No matches queued in **Manager** | Open `/manager` or use `scripts/create_match.sh` / `create_tournament.sh` |
| Elimination bracket stuck after a crashed match | Failed run reports `/api/manager/.../error`; series is cancelled | Round-robin may auto-finish; elimination may need **Cancel tournament** in `/manager` if `recovery_hint` in the error response applies |
| Stream shows wrong or blank page after upgrading | Old `STREAM_VIEW_URL` pointed at `http://overlay:…` | Set `STREAM_VIEW_URL=http://web:8080/broadcast` (default in compose) |
| Empty scoreboard after rename | Data was in volume `overlay-data`; compose now uses `web-data` | Mount the old volume name once or copy `results.json` / DB data into the new volume (see `CLAUDE.md` Gotchas) |
| Agents fall back to random moves | LLM API timeout or rate limit (exceeds `LLM_TURN_TIMEOUT`) | Increase `LLM_TURN_TIMEOUT`, check provider status, or switch to a faster model |
| Showdown unhealthy / agents can't connect | Showdown container still building or crashed | Run `docker compose ps` and check `docker compose logs showdown`; the first build clones the full repo and runs `npm install` (~2 min) |
| Stream container crashes | Chromium needs more shared memory | Ensure `shm_size: 2gb` is set in `docker-compose.yml` (it is by default) |
| Broadcast page is blank in browser | `HIDE_BATTLE_UI=1` hides controls but the iframe needs Showdown running | Verify Showdown is healthy: `curl http://localhost:8000/` |
| Stale results / old scoreboard | Data persists in Docker named volumes across restarts | Clear replays/logs/state by recreating those volumes, or wipe everything (including manager SQLite) with **`docker compose down -v`** or **`bash scripts/stack_down.sh -v`** |
| Persona not found | Slug doesn't match any file in `agents/personas/` | Ensure the `.md` file exists and the slug (filename without extension) matches the Manager / CLI pick |
| **`/manager/config` says the env file is missing or wrong** | Host `.env` did not exist before `docker compose up`; Docker may have created a **directory** named `.env` | Remove the erroneous path (`rm -rf .env` if it is a directory), copy `.env.example` to `.env`, then `docker compose up -d --build web` |

## Future Additions

- **TTS narration** — text-to-speech commentary over battles, reading callouts and play-by-play
- **Twitch chat integration** — let viewers influence battles (vote on moves, trigger events)
- **More personas** — additional battle styles and personalities beyond aggro/stall
