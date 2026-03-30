# AI Pokémon Showdown

Two **LLM-powered agents** face off on a local **Pokémon Showdown** server. Use the **manager** (UI at `/manager`, JSON API, and CLI scripts) to queue matches and tournaments, then **inspect outcomes** — replays, logs, results, and **stats** — to **compare providers, models, personas, and formats**. **Live streaming is optional:** run `showdown`, `web`, and `agents` only when you just want battles and analysis. Add the **`stream`** service for headless Twitch (Xvfb + Chromium + FFmpeg), or **compose the same pages in OBS** (see **Streaming with OBS** below). Each agent can use its own provider, model, and persona, with live reasoning, callouts, and broadcast overlays when you stream.

## Architecture

```
docker-compose.yml
├── assets/      — Shared static art: `static/trainers/`, `static/portraits/` (mounted into `web` and Showdown; see `assets/README.md`)
├── showdown/    — Local Pokémon Showdown server (no auth)
│   ├── config/    — Server config overrides (port, auth, throttle)
│   └── static/    — Custom Showdown UI (`index.html` override only; trainer art lives under `assets/static/trainers/`)
├── agents/      — Queue worker + poke-env LLM players (`queue_worker.py`, `match_runner.py`)
│   └── personas/  — Markdown persona definitions (`*.md`); optional runtime memory under `STATE_DIR/personas/{slug}/` when `ENABLE_MEMORY=true`
├── web/         — FastAPI: scoreboard, `/manager`, `/api/manager`, broadcast, victory, thoughts (`GET /thoughts` + `/thoughts/ws`); SSE in `scoreboard_stream.py` (`/scoreboard/stream`) and `manager_stream.py` (`/api/manager/stream`)
│   ├── manager/   — `db.py` (aiosqlite + migrations), `tournament_logic.py` (brackets), `tournament_definition.py` (plaintext definitions), `routes.py` (API + HTML)
│   ├── templates/ — Jinja2 (broadcast, `/overlay`, victory, replays, manager dashboards)
│   └── static/    — App JS/CSS/vendor (image-baked; not for mountable art — see `assets/`)
├── stream/      — Xvfb + Chromium + FFmpeg → Twitch RTMP
└── scripts/     — Health, stack lifecycle, manager CLI, Twitch title (see **Scripts** below)
```

### Static files: `assets/` vs `web/static`

| Location | Served as | Role |
| --- | --- | --- |
| **`assets/static/`** (repo root) | Bind-mounted paths such as `/static/trainers/` and `/static/portraits/` on **web** (and trainers on **Showdown** where the client needs them) | **Content / skins:** PNG/WebP art you edit on disk. Change files without rebuilding the **web** image. See **`assets/README.md`**. |
| **`web/static/`** | `/static/…` from inside the **web** container | **App bundle:** `manager.js`, `manager.css`, vendor JS, optional default SFX (e.g. `audio/`). Shipped with the **web** Docker image; versioned and released with application code. |

Use **`assets/`** for operator- or artist-owned media shared across services; use **`web/static/`** for static behavior of the FastAPI app itself.

**Persona portraits:** **Both** a tall (512×640) and a square (512×512) file are **required** per persona (same idea as trainer sprites on disk). Put them under **`assets/static/portraits/`** and **`assets/static/portraits/square/`**, or upload from **Manager** (PNG / GIF / WebP — see **`assets/README.md`**); saves are rejected if either is missing.

## Tech stack

| Area | Technologies |
| --- | --- |
| **Agents** | Python 3.11, [poke-env](https://github.com/hsahovic/poke-env), asyncio, **aiohttp**, Anthropic SDK, OpenAI-compatible SDK (DeepSeek / OpenRouter). Optional **Pokédex** layer in `agents/pokedex.py`; optional **persona adaptive memory** (Markdown under `STATE_DIR/personas/`, default off). Move/ability/item text built into the image via `agents/scripts/extract_showdown_data.py`. |
| **Web** | Python 3.11, **FastAPI**, **Uvicorn**, **Starlette**, **Jinja2**, **aiosqlite** (WAL + foreign keys via the `_db()` connection helper in `web/manager/db.py`). |
| **Showdown** | Node 20, upstream [pokemon-showdown](https://github.com/smogon/pokemon-showdown) (cloned at image build). |
| **Stream** | Python 3.11, **Playwright** (Chromium), **Xvfb**, **FFmpeg**, PulseAudio. |
| **Persistence** | Docker named volumes: SQLite on `manager-data` (`/manager-data/manager.db`) for tournaments / series / matches / queue and **tournament definition presets** (`tournament_presets` table); legacy `/data/results.json` on `web-data` if used; replays and raw battle logs on `replay-data` / `log-data`; **`state-data`** holds `current_battle.json`, `thoughts.json`, and (when enabled) **`/state/personas/{slug}/memory.md`**, **`learnings.md`**, and per-persona **`_memory_state.json`**. |

### Matches DB

Pending work and history both live in the same SQLite file on the **`manager-data`** volume. The **`matches`** table rows carry a **`status`** (`queued`, `running`, `completed`, `error`, `cancelled`) and a **`queued_at`** timestamp. The worker calls **`GET /api/manager/queue/next`**, which **selects** the oldest `queued` row and **updates** it to `running` in one transaction (`web/manager/db.py` — `pop_next_queued_match`). Completing a match **updates** the row again (`completed` plus winner, replay path, …); rows are **not deleted** — they stay for `/manager/results`, `/api/manager/stats`, and the scoreboard. The **agents** service **`queue_worker.py`** uses **aiohttp** to poll that HTTP API (interval `QUEUE_POLL_INTERVAL`).

## Tournament manager (UI + API)

The **manager** is how you queue work for the agents (default entrypoint is `queue_worker.py`, not env-only player vars). The **dashboard**, **tournament detail**, and **series detail** pages open **`GET /api/manager/stream`** (SSE) and use **`web/static/manager-stream-client.js`** to refresh queue depth, running match, and bracket sections when the server signals changes (debounced `{ "seq", "queue", "tournament_ids", "series_ids" }` events).

**Pages** (browser, port 8080):

| Path | Purpose |
| --- | --- |
| `/manager` | Dashboard: queue depth, running match, **Upcoming** queue (offset pagination via `upcoming_offset` / `upcoming_limit`), recent results, recent tournaments |
| `/manager/tournaments` | All tournaments (`offset` / `limit` pagination) |
| `/manager/tournaments/new` | Create tournament (round-robin, single / double elimination); optional **plaintext import** (paste or `.txt`) and **saved presets** |
| `/manager/tournament-presets` | List, create, and edit **saved** plaintext tournament definitions (stored in SQLite on `manager-data`) |
| `/manager/tournaments/{id}` | Tournament detail and bracket progress |
| `/manager/matches/new` | Queue a one-off match or short series |
| `/manager/series/{id}` | Series detail (best-of, individual games) |
| `/manager/results` | Completed matches and series (offset pagination: `matches_offset` / `matches_limit`, `series_offset` / `series_limit`) |
| `/manager/results/stats` | Aggregate stats (models, formats) |
| `/manager/personas` | Edit persona markdown; upload **trainer sprites** and **portraits** (see **`assets/README.md`**) |
| `/manager/config` | View and edit documented env vars (host project `.env` when the `web` container mounts it; see `MANAGER_HOST_ENV_FILE` in **UI** env table) |
| `/manager/config/update` | POST form target for `/manager/config`: writes one registered key into the host `.env` |

**JSON API** (used by the queue worker, manager pages, and `scripts/create_match.sh` / `create_tournament.sh`):

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/manager/config` | GET | Allowed providers, battle formats, persona list (from mounted `agents/personas`) |
| `/api/manager/tournaments` | GET, POST | List / create tournament |
| `/api/manager/tournaments/parse-definition` | POST | Parse plaintext tournament definition (`{ "text": "..." }`) → `{ ok, data, errors, warnings }` for the create-tournament payload |
| `/api/manager/tournaments/{tid}` | GET | Tournament + entries + series |
| `/api/manager/tournaments/{tid}/cancel` | POST | Cancel tournament and pending work |
| `/api/manager/tournament-presets` | GET, POST | List presets / create (name + body; body must parse like import) |
| `/api/manager/tournament-presets/{id}` | GET, PATCH, DELETE | Get preset (incl. definition text), update, delete |
| `/api/manager/series` | POST | Create a series (optional `auto_queue` games) |
| `/api/manager/series/{sid}` | GET | Series + matches |
| `/api/manager/matches` | GET, POST | List / create standalone match |
| `/api/manager/matches/{mid}` | GET | One match row |
| `/api/manager/matches/{mid}/start` | POST | Mark running (optional; worker uses `queue/next`) |
| `/api/manager/matches/{mid}/complete` | POST | Worker: winner, replay path, etc. |
| `/api/manager/matches/{mid}/error` | POST | Worker: fail match; may cancel series / hint bracket recovery |
| `/api/manager/queue/next` | GET | Worker: atomically dequeue → `running` (**404** if queue empty) |
| `/api/manager/queue/depth` | GET | Count of `queued` matches |
| `/api/manager/queue/upcoming` | GET | Next queued matches (oldest first) for UI tickers / dashboard; query `limit`, `offset` (defaults **10** / **0**; max **200**). When `offset=0`, may append tournament “pending opponent” placeholder rows after the slice. |
| `/api/manager/queue/running` | GET | Current `running` match (JSON) or `null` — dashboard live status |
| `/api/manager/stream` | GET | **SSE** (`text/event-stream`): debounced refresh hints `{ "seq", "queue", "tournament_ids", "series_ids" }` for manager UI; keepalive comments between events. Use the same **no proxy buffering** rules as `/scoreboard/stream` if you terminate TLS in front of `web`. |
| `/api/manager/results` | GET | Completed matches (filters optional) |
| `/api/manager/stats` | GET | Dashboard analytics |

### Tournaments (formats & double elimination)

- **Round robin** — Each pair of entries plays a **best-of-N** *series* (see **Best Of** on the new-tournament form). The UI shows **standings** from completed series; the event finishes when every series is done or cancelled.
- **Single elimination** — One **winners** bracket. When the winners bracket final completes, the tournament is **completed**.
- **Double elimination** — **Winners** bracket, **losers** bracket, and **grand finals**. Losers from most winners matches are **dropped into the losers bracket**; the losers bracket advances round by round; the **grand finals** series pits the **winners-bracket champion** against the **losers-bracket champion** (the manager tournament page shows WB, LB, and GF blocks). Implementation: `web/manager/tournament_logic.py`.
  - **Why it matters:** Older behavior treated double elim like single elim for advancement (no real LB or GF routing, tournament could finish without a meaningful grand final). The current logic actually **feeds** players through LB and **queues** grand finals when both sides are known.
  - **Caveats:** **Bracket reset** (losers-bracket winner must beat the undefeated player twice) is **not** modeled—one grand-finals series decides the winner. Routing from winners to losers is **exact** for common small sizes (e.g. 4- and 8-player paths); **larger** brackets use heuristics and “first open slot” fallbacks and may not match a strict international double-elim chart for every `N`.

### Plaintext definitions & presets

A **tournament definition** is a single plain-text file (or paste buffer): **one tournament only**. It mirrors the manual “New tournament” form. Use it from **`/manager/tournaments/new`** (import block: paste, `.txt` upload, **Parse & fill form**) or save/load it as a **preset** under **`/manager/tournament-presets`**.

#### File structure

1. **Header block** — Any number of `Key: value` lines (order does not matter). Keys are **case-insensitive**; spaces and hyphens in the key are treated like underscores (e.g. `Battle Format` and `battle_format` match). Lines whose trimmed text starts with `#` are comments; blank lines are ignored.
2. **`Participants:`** — A line that is **only** `Participants:` or `Participant:` (case-insensitive), with **nothing after the colon**, ends the header block. If you put other text on the same line, it will not start the roster section.
3. **Participant lines** — Every following non-empty, non-`#` line is one roster entry until end of file.

#### Header keys

| Key (examples) | Required | Meaning |
| --- | --- | --- |
| `Name` | Yes | Tournament display name. |
| `Type` | Yes | `round robin`, `single elimination`, `double elimination`, or snake_case (`round_robin`, …). |
| `Battle Format` | Yes | Showdown format string (e.g. `gen9randombattle`). Aliases: `Format`, `BattleFormat`. |
| `Best Of` | Yes | Odd positive series length: `1`, `3`, `Bo3`, `bo5`, etc. Aliases: `BestOf`, `Bo`. |
| `Single Elim Bracket` | No | For single/double elimination only: `compact` (default) or `power_of_two`. Aliases: `Bracket`, `Winners Bracket`. Ignored for round robin. |

#### Participant lines

Each line is **either** comma-separated **or** pipe-separated (if the line contains `|`, splits are on `|`; otherwise on commas). Fields are trimmed.

| Fields | Meaning |
| --- | --- |
| 3 | `provider`, `model`, `persona_slug` |
| 4 | Same + `seed` (integer ≥ 1) |

- **Providers:** `anthropic`, `deepseek`, `openrouter` (same as the manager UI).
- **Persona:** filename slug from `agents/personas/*.md` (e.g. `aggro`, `stall`).
- **Seeds:** Omit on **all** lines → seeds become `1, 2, 3, …` in file order. If **any** line has a seed, **every** line must have one.
- **OpenRouter:** Usually `openrouter` plus a model id containing `/`, e.g. `openrouter, anthropic/claude-3.5-sonnet, aggro`. Avoid commas inside the model id unless you use `|` as the separator.

#### Example

```text
# Weekend mix — double elim, Gen 9
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

**Validation** matches the form: provider/model pairs (`claude…` for Anthropic, `deepseek…` for DeepSeek, `/` or `openrouter` prefix for OpenRouter), and persona slugs must exist on the personas mount. Unknown formats may still parse with a warning.

**Presets** store the same text under a name in SQLite (`manager.db` on **`manager-data`**). **`docker compose down -v`** wipes them with other manager data unless you back up the DB. Implementation details and API: **CLAUDE.md** → *Tournament definitions (plaintext import) & presets*.

## Scripts

All live in `scripts/` (run from repo root). The **web** service must be up for manager CLI scripts (default base URL `http://localhost:8080`, or set `WEB_URL` / `OVERLAY_URL`).

| Script | Purpose |
| --- | --- |
| `healthcheck.sh` | Probe Showdown + web HTTP health and **scoreboard SSE** (`/scoreboard/stream` must emit a `data:` line); uses `WEB_HOST` / `WEB_PORT` with `OVERLAY_*` fallback. |
| `refresh_stream_browser.sh` | Reload the **stream** container’s Chromium view (`full` page or Showdown iframe only); requires `stream` running — run `bash scripts/refresh_stream_browser.sh --help` for env vars. |
| `restart_stack.sh` | `docker compose` restart for `showdown`, `web`, `agents`; `--stream` includes Twitch capture. |
| `stack_down.sh` | `docker compose down`; optional `-v` / `--volumes` to drop named volumes; optional post-down sleep (see script `--help`). |
| `stack_down_after_tournament.sh` | Poll the manager until a tournament is **completed** or **cancelled**, optional post-finish delay, then run `stack_down.sh` (useful after a stream/event — run `bash scripts/stack_down_after_tournament.sh --help`). |
| `create_match.sh` | Create a match or best-of series via the manager API (requires provider/model/persona flags — run `bash scripts/create_match.sh --help`). |
| `create_tournament.sh` | Create a tournament (round-robin / elimination) via manager API (`bash scripts/create_tournament.sh --help`). |
| `set_twitch_title.sh` | Set Twitch title/category using OAuth env vars from `.env`. |

**Agents** use [poke-env](https://github.com/hsahovic/poke-env) to connect to the local Showdown server and call an LLM each turn to decide a move or switch. Supported providers:

- **Anthropic** (Claude)
- **DeepSeek**
- **OpenRouter** (hundreds of models: Gemini, Llama, Mistral, Qwen, etc.)

Each player is assigned a **persona** — a markdown file that defines the agent's battle style, reasoning voice, and callout personality. The repo ships several example personas in `agents/personas/`:

| Slug | Name | Style |
| --- | --- | --- |
| `aggro` | Damage Dan | Hyper-offense — swagger, urgency, rival banter |
| `stall` | Stall Stella | Defensive — long-term control, pivots, chip damage |
| `nerd` | Intellect Imani | Poké-nerd — ties choices to concrete game knowledge |
| `neutral` | Neutral Nori | Flexible — no fixed playstyle |
| `gambler` | River Rick | High-variance reads, doubles, swinging for the fence |
| `zoomer` | Lowkey Luca | Internet-native voice; spicy lines when they still feel real |
| `villain` | Vex Vera | Evil-team swagger — disruptive tempo, predatory pressure, dramatic banter |
| `racer` | Speedy Sisu | Speed-first — tempo, first strike, closing fast |

### How a Battle Works

1. The **queue worker** (`agents/queue_worker.py`) pulls the next match from the web service, then `match_runner` creates two `LLMPlayer` instances for that matchup.
2. Both players connect to the local **Showdown** server via WebSocket (using poke-env).
3. Each turn, the active player sends the battle state to its **LLM provider**, which returns a structured JSON action (`action_type`, `index`, `reasoning`, optional `callout`). If **Pokédex tools** are enabled, Anthropic models can optionally look up move/ability/item/type details before committing an action.
4. The player's reasoning and callout are **POSTed** to the web service `/thought` endpoint (and written to `thoughts.json` on the shared volume) for the live broadcast.
5. When the battle ends, the worker **reports** the result via the manager API, saves an HTML replay to `/replays`, and optionally writes a raw JSON log to `/logs`.
6. If **`ENABLE_MEMORY=true`**, the agents service may run a **post-match reflection** LLM call per persona (see **Persona adaptive memory** below), append a diary entry to `memory.md`, and periodically refresh `learnings.md`.
7. After a configurable pause (`DELAY_BETWEEN_MATCHES`), the worker polls `/api/manager/queue/next` for the next match (or waits). Configure matchups in **Manager** (`http://localhost:8080/manager`) or CLI scripts (`scripts/create_match.sh`, `create_tournament.sh`).

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

   If you split the battle out with `/broadcast/battle_frame`, add **separate** Browser Sources for `/overlay`, `/thoughts_overlay`, `/broadcast/top_bar`, and `/victory` instead of relying on `/broadcast`. The same layered stack does **not** include `/match_intro` or `/tournament_intro` unless you add them (the all-in-one **`/broadcast`** page embeds those as iframes).

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

Create a new markdown file in `agents/personas/` with YAML front matter and a prompt body. **Sprites and portraits** (same `{slug}` as the filename) live under **`assets/static/`** — tall + square portraits are **required** before the manager will save a persona; see the **Static files: `assets/` vs `web/static`** section above.

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

### Persona adaptive memory (optional)

When **`ENABLE_MEMORY=true`** (default is **`false`** — off), each persona **slug** can accumulate **episodic** and **tactical** text that is injected into that player’s system prompt on the **next** match:

| File (under `STATE_DIR/personas/{slug}/`, typically `/state/...` in Docker) | Role |
| --- | --- |
| `memory.md` | Rolling battle diary: `## Match ...` sections appended after eligible matches; capped by **`MAX_MEMORY_ENTRIES`**. |
| `learnings.md` | Curated markdown (bullets / sections); rewritten only on turns where **`LEARNINGS_UPDATE_INTERVAL`** applies; trimmed to **`MAX_LEARNINGS_BULLETS`** bullet lines. |
| `_memory_state.json` | Internal counter (`matches_completed`) for reflection intervals. |

**Behavior:** At match start, `match_runner.build_system_prompt()` loads these files (if they exist) and adds `== YOUR BATTLE MEMORY (recent matches) ==` and `== YOUR TACTICAL LEARNINGS ==` **before** the action-format instructions. After a successful battle, each distinct persona in the match increments its counter; when **`MEMORY_REFLECTION_INTERVAL`** is satisfied, the **same provider and model** as that side runs one **JSON** reflection (battle log + current files → `memory_entry` and optional `learnings_update`). If both sides use the **same persona slug**, only one reflection runs for that slug (p1’s perspective).

**Cost / ops:** Extra **input tokens** every turn while files are non-empty, plus **one reflection API call** per persona per qualifying match (log size dominates reflection input). Disable with `ENABLE_MEMORY=false`. Clearing memory: delete `state-data` / remove `personas/` under the state volume, or only the files for one slug.

Env vars (see **Configuration** → Battle Settings, `.env.example`, and `/manager/config` registry): `ENABLE_MEMORY`, `MEMORY_REFLECTION_INTERVAL`, `LEARNINGS_UPDATE_INTERVAL`, `MAX_MEMORY_ENTRIES`, `MAX_LEARNINGS_BULLETS`, `LLM_MEMORY_REFLECTION_MAX_TOKENS`. They are **already forwarded** in `docker-compose.yml` for the `agents` service; restart **`agents`** after changing them.

### Pokédex Tools (Optional)

Agents can optionally access a **Pokédex** data layer for mid-battle lookups — move descriptions, ability effects, item effects, type matchups, and species stats. Two modes are available (can be used independently or together):

**Tool calling (Anthropic only):** When `POKEDEX_TOOL_ENABLED=true`, Anthropic models get five additional tools alongside `submit_action`. The model can call up to `POKEDEX_MAX_LOOKUPS` (default 3) lookups per turn before being forced to submit an action. Other providers are unaffected.

**Auto-enrich (all providers):** When `POKEDEX_AUTO_ENRICH=true`, a `=== POKEDEX NOTES ===` section is appended to the battle state text each turn with brief descriptions of visible abilities, items, and available move effects. This adds ~100-200 tokens per turn to each message.

```bash
# Enable Pokédex tool calling for Anthropic
POKEDEX_TOOL_ENABLED=true

# Enable auto-enriched context for all providers
POKEDEX_AUTO_ENRICH=true

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
| `/broadcast` | GET | Full broadcast scene: battle iframe + scoreboard (`/overlay`) + match/tournament intro iframes + victory + thoughts. Hub uses **SSE** (`/scoreboard/stream`) + `postMessage` to iframes; **`GET /scoreboard`** only as fallback if the stream stays closed. |
| `/broadcast/battle_frame` | GET | Showdown iframe + battle sync + callouts only (for OBS layering) |
| `/match_intro` | GET | Matchup card when a battle is starting (duration `MATCH_INTRO_SECONDS`; embedded in `/broadcast` iframe) |
| `/tournament_intro` | GET | Tournament roster opener before the first match only (driven by `TOURNAMENT_INTRO_SECONDS` on **agents**; iframe child of `/broadcast`) |
| `/broadcast/top_bar` | GET | Transparent stream title + format (for OBS layering) |
| `/thoughts_overlay` | GET | Transparent LLM thoughts panels (for OBS layering) |
| `/overlay` | GET | Transparent scoreboard for compositing |
| `/victory` | GET | Animated post-match winner splash (duration from `VICTORY_MODAL_SECONDS` or `TOURNAMENT_VICTORY_MODAL_SECONDS` when the win clinches the tournament) |
| `/scoreboard` | GET | Win/loss records, player names/models, recent matches (JSON) |
| `/scoreboard/stream` | GET | **Server-Sent Events:** JSON lines `data: {"seq":…,"payload":…}` when the scoreboard changes; same `payload` shape as `/scoreboard`. If you terminate TLS or proxy in front of `web`, turn **off buffering** for this path (e.g. nginx `proxy_buffering off` / `X-Accel-Buffering: no`) or clients may not see live updates. |
| `/current_battle` | GET | Live battle metadata (JSON) |
| `/thoughts` | GET | Current LLM reasoning per player (JSON) |
| `/thoughts/ws` | WebSocket | Real-time thought stream for broadcast |
| `/replays` | GET | Replay + log index page (clickable HTML/JSON files); optional `offset` / `limit` (default page size **10**, max **200** per page) |
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

# Smoke SSE (should print at least one `data:` line, then Ctrl+C)
curl -sS -N --max-time 3 http://localhost:8080/scoreboard/stream | head -n 5

# Manager UI SSE (no initial snapshot; first output is often `: keepalive` after ~15s, or `data:` when the queue/brackets change)
curl -sS -N --max-time 16 http://localhost:8080/api/manager/stream | head -n 5
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

### Automated tests (optional)

Bracket and round-robin helper logic is covered by **`pytest`** in **`web/manager/verify_brackets_test.py`** (no Docker required). From the **`web/`** directory:

```bash
pip install -r requirements-dev.txt
pytest manager/verify_brackets_test.py -v
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
   - Optional: `TWITCH_GAME_ID` (defaults to Pokémon Showdown [`850490686`](https://www.twitch.tv/directory/category/pokemon-showdown))
   - Optional: `TWITCH_STREAM_TITLE`
2. Run:

```bash
bash scripts/set_twitch_title.sh
```

Or pass a one-off title:

```bash
bash scripts/set_twitch_title.sh "Pokémon Showdown battles with LLMs"
```

When `stream` starts via Docker, it also attempts to set the Twitch title/category automatically if the OAuth env vars are present. Disable with `TWITCH_AUTO_SET_TITLE=false`.

## Configuration

Key environment variables (see `.env.example` for the full list). **Boolean flags** are written as **`true`** / **`false`** in examples and Compose defaults; the code also accepts **`1`** / **`0`**, **`yes`** / **`no`**, and **`on`** / **`off`** (see `parse_env_bool` in `agents/env_bool.py`, `web/env_bool.py`, and `stream/env_bool.py`). Under Docker Compose, values reach a container only if they appear in that service’s `environment:` block (with `${VAR:-default}` substitution from the host `.env`), or you add `env_file: .env` — see **Gotchas** in `CLAUDE.md`. The **`web`** service does **not** automatically inherit every key from the mounted host file as process env; broadcast timing keys (`MATCH_INTRO_SECONDS`, `VICTORY_*`, `TOURNAMENT_VICTORY_*`, …) must be **listed under `web` in `docker-compose.yml`**. **Agents-only** timing (`TOURNAMENT_INTRO_SECONDS`, `TOURNAMENT_INTRO_DELAY_SECONDS`, `MATCH_INTRO_STARTING_HOLD_SECONDS`, plus queue/LLM/Pokédex/memory keys, …) must appear under **`agents`**. `/manager/config` edits the **host** `.env` file; restart affected containers after changes.

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
| `TOURNAMENT_INTRO_SECONDS` | No | `0` | **Agents:** Hold the tournament opener (`/tournament_intro`) before the first match of a tournament; `0` disables |
| `TOURNAMENT_INTRO_DELAY_SECONDS` | No | `0` | **Agents:** Extra pause after that hold (fade / margin before match intro) |
| `MATCH_INTRO_STARTING_HOLD_SECONDS` | No | `0.45` | **Agents:** Keep battle status in `starting` briefly so `/broadcast` can show the match intro before Showdown connects; `0` skips |
| `TURN_DELAY_SECONDS` | No | `0` | Delay per turn per player (seconds, for watchability) |
| `DELAY_BETWEEN_MATCHES` | No | `15` | Pause between matches (seconds) |
| `LLM_MAX_OUTPUT_TOKENS` | No | `512` | Max output tokens per LLM turn |
| `LLM_TURN_TIMEOUT` | No | `150` | Hard timeout per LLM call (seconds); falls back to random move |
| `POKEDEX_TOOL_ENABLED` | No | `false` | Enable Pokédex tool calling for Anthropic models (`true`/`false`; `1`/`0` also accepted) |
| `POKEDEX_AUTO_ENRICH` | No | `false` | Inject Pokédex notes into battle context for all providers (`true`/`false`; `1`/`0` also accepted) |
| `POKEDEX_MAX_LOOKUPS` | No | `3` | Max Pokédex tool calls per turn before forcing an action |
| `ENABLE_MEMORY` | No | `false` | Persona adaptive memory: load/inject `memory.md` + `learnings.md`; post-match reflection (`true`/`false`; `1`/`0` also accepted) |
| `MEMORY_REFLECTION_INTERVAL` | No | `1` | Run reflection every **N** completed matches **per persona** (`0` = never reflect) |
| `LEARNINGS_UPDATE_INTERVAL` | No | `3` | When reflection runs, allow **full** `learnings.md` update every **N** matches (`0` = never update learnings) |
| `MAX_MEMORY_ENTRIES` | No | `10` | Max `## Match ...` blocks kept in `memory.md` |
| `MAX_LEARNINGS_BULLETS` | No | `30` | Max markdown bullet lines kept when trimming `learnings.md` |
| `LLM_MEMORY_REFLECTION_MAX_TOKENS` | No | `2048` | Max output tokens for the reflection JSON completion |

</details>

<details>
<summary><strong>UI</strong></summary>

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `HIDE_BATTLE_UI` | No | `true` | Hide Showdown's native battle controls in the broadcast (`true`/`false`; `1`/`0` also accepted). Forwarded from `.env` in `docker-compose.yml` for **`web`** and **`stream`**; also documented in `.env.example`. |
| `VICTORY_MODAL_SECONDS` | No | `30` | Victory splash visible duration for a normal match win (seconds; fade-out adds ~0.5s). Passed to **`web`** via Compose. |
| `TOURNAMENT_VICTORY_MODAL_SECONDS` | No | `60` | Same splash when a match **clinches the tournament** (e.g. grand-finals decider); defaults longer than `VICTORY_MODAL_SECONDS`. Passed to **`web`** via Compose. |
| `VICTORY_SHOW_DELAY_SECONDS` | No | `1` | Delay after a win is recorded before the victory layer appears (seconds; `0` = immediate). Passed to **`web`** via Compose. |
| `BATTLE_IFRAME_OUTRO_SECONDS` | No | `5` | After a battle ends, keep the Showdown iframe on the room this many seconds before resetting to the lobby (`0` = immediate). Passed to **`web`** via Compose. |
| `MATCH_INTRO_SECONDS` | No | `5` | Matchup card on **`/broadcast`** when a battle is starting; `0` disables. Passed to **`web`** via Compose. |
| `WEB_DEBUG` | No | `false` | When `true`, **`web`** emits extra **INFO** diagnostics (e.g. each scoreboard SSE publish with `seq` and subscriber count). Other modules can use **`web_debug.web_debug_enabled()`**. Forwarded in `docker-compose.yml` for **`web`**. |
| `SHOWDOWN_VIEW_BASE` | No | `http://localhost:8000` | Base URL the **manager** uses for Showdown client links (what **browsers** should open — e.g. LAN hostname or HTTPS reverse proxy). Not the Docker service name `showdown`. Passed to **`web`** via Compose. |
| `STREAM_TITLE` | No | *(compose default)* | Headline on the broadcast page (`web` service) |
| `PERSONAS_DIR` | No | `/personas` | In the `web` container, mount of `agents/personas` for manager persona list + `/manager/personas` editor (`docker-compose.yml`) |
| `TRAINERS_DIR` | No | `/app/static/trainers` | In **`web`**, on-disk trainer sprites (Compose mounts `./assets/static/trainers` here). Override only for non-Compose layouts. |
| `PORTRAITS_DIR` | No | `/app/static/portraits` | In **`web`**, tall + square persona portraits (`portraits/square/`). Compose mounts `./assets/static/portraits`. |
| `MANAGER_HOST_ENV_FILE` | No | `/app/host.env` *(via `docker-compose.yml`)* | In the `web` container, path to the mounted host `.env` (`./.env:/app/host.env` in Compose) so `/manager/config` can read/write keys listed in `web/manager/env_registry.py`. Without Compose, leave unset unless you mount a file at the path you configure. |

</details>

<details>
<summary><strong>Storage</strong></summary>

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `REPLAY_DIR` | No | `/replays` | Replay HTML export directory (in-container path) |
| `LOG_DIR` | No | `/logs` | Raw battle log (JSON) export directory (in-container path) |
| `LOG_RAW_BATTLE` | No | `true` | Toggle raw JSON log export (`true`/`false`; `1`/`0` also accepted) |
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
| `TWITCH_AUTO_SET_TITLE` | No | `true` | Auto-set title on stream start (`true`/`false`; `1`/`0` also accepted) |
| `TWITCH_GAME_ID` | No | `850490686` | Twitch category ID ([Pokémon Showdown](https://www.twitch.tv/directory/category/pokemon-showdown)) |
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
| Broadcast page is blank in browser | `HIDE_BATTLE_UI=true` hides controls but the iframe needs Showdown running | Verify Showdown is healthy: `curl http://localhost:8000/` |
| Stale results / old scoreboard | Data persists in Docker named volumes across restarts | Clear replays/logs/state by recreating those volumes, or wipe everything (including manager SQLite) with **`docker compose down -v`** or **`bash scripts/stack_down.sh -v`** |
| Persona not found | Slug doesn't match any file in `agents/personas/` | Ensure the `.md` file exists and the slug (filename without extension) matches the Manager / CLI pick |
| Persona memory never updates | `ENABLE_MEMORY` is off or not passed into **`agents`** | Set `ENABLE_MEMORY=true` in `.env`, confirm `docker-compose.yml` lists it under `agents.environment`, then `docker compose up -d agents` (rebuild not required for env-only changes). Check `docker compose logs agents` for `[memory]` lines or reflection errors |
| **`/manager/config` says the env file is missing or wrong** | Host `.env` did not exist before `docker compose up`; Docker may have created a **directory** named `.env` | Remove the erroneous path (`rm -rf .env` if it is a directory), copy `.env.example` to `.env`, then `docker compose up -d --build web` |

## Future Additions

- **TTS narration** — text-to-speech commentary over battles, reading callouts and play-by-play
- **Twitch chat integration** — let viewers influence battles (vote on moves, trigger events)
