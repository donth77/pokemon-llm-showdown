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
├── agents/      — Queue worker + poke-env players (`queue_worker.py`, `match_runner.py`, `llm_player.py`, `human_player.py`)
│   └── personas/  — Markdown persona definitions (`*.md`); optional runtime memory under `STATE_DIR/personas/{slug}/` when `ENABLE_MEMORY=true`
├── web/         — FastAPI: scoreboard, `/manager`, `/api/manager`, broadcast, unified splashes (`/victory` / `/splash`), thoughts (`GET /thoughts` + `/thoughts/ws`), **human-vs-AI battle control page (`/battle/{id}`) + relay (`/api/battle/{id}/*`)**; SSE in `scoreboard_stream.py` (`/scoreboard/stream`), `manager_stream.py` (`/api/manager/stream`), and `battle_relay.py` (`/api/battle/{id}/stream`)
│   ├── manager/   — `db.py` (aiosqlite + migrations), `tournament_logic.py` (brackets), `tournament_definition.py` (plaintext definitions), `routes.py` (API + HTML), `battle_format_rules.py` (random vs BYO formats), `team_showdown_validate.py` (Showdown `validate-team` for imports)
│   ├── templates/ — Jinja2 (`broadcast.html`, `splash.html`, `overlay.html`, `battle.html`, partials, manager dashboards, replays)
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

**Team presets:** A **`teams`** table stores operator-defined squads for **bring-your-own-team** formats (anything whose Showdown id does **not** end with **`randombattle`** — see `battle_format_rules.py`). Columns: **`name`** (unique case-insensitively), **`battle_format`** (Showdown format id, required on create via the manager form), **`showdown_text`** (full paste from Teambuilder export), **`notes`**, timestamps. **`tournament_entries`** may reference **`team_id`**. When a match is queued, **`player1_team_showdown`** and **`player2_team_showdown`** on the **`matches`** row hold **snapshots** of that text (from explicit match **`player*_team_id`** and/or the entries’ **`team_id`**) so later edits to the library row do not change already-queued games. **`GET /api/manager/queue/next`** includes those snapshot fields for the agents service.

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
| `/manager/matches/new` | Queue a one-off match or short series (optional team presets per side for non-`randombattle` formats). **Per-side "Player Type"** dropdown (AI / Human) — selecting Human hides provider/model/persona and reveals a required **Display Name** input; human matches are forced to Single Match. |
| `/battle/{match_id}` | **Human vs AI battle control page** — opponent persona panel, callout bubble, reasoning feed, typed move/switch buttons, embedded Showdown battle view. Linked from the match-create success screen. |
| `/manager/teams` | Team preset library (paste Showdown export) |
| `/manager/teams/new`, `/manager/teams/{id}/edit` | Create or edit a preset |
| `/manager/series/{id}` | Series detail |
| `/manager/results` | Completed matches and series |
| `/manager/results/stats` | Aggregate stats |
| `/manager/personas` | Edit persona markdown and upload trainer sprites / portraits |
| `/manager/personas/{slug}/memory` | View adaptive memory files when enabled |
| `/manager/config` | View and edit documented env vars from the host `.env` |

### JSON API

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/manager/config` | GET | Allowed providers, battle formats, persona list; `random_team_battle_format_suffix` (`randombattle`) for UI gating |
| `/api/manager/tournaments` | GET, POST | List / create tournament |
| `/api/manager/tournaments/parse-definition` | POST | Parse plaintext tournament definition |
| `/api/manager/tournaments/{tid}` | GET | Tournament + entries + series |
| `/api/manager/tournaments/{tid}/cancel` | POST | Cancel tournament and pending work |
| `/api/manager/tournament-presets` | GET, POST | List presets / create |
| `/api/manager/tournament-presets/{id}` | GET, PATCH, DELETE | Get, update, delete preset |
| `/api/manager/series` | POST | Create a series (optional `player1_team_id` / `player2_team_id` when not using `*randombattle`) |
| `/api/manager/series/{sid}` | GET | Series + matches |
| `/api/manager/matches` | GET, POST | List / create standalone match or series (optional `player1_team_id` / `player2_team_id`) |
| `/api/manager/teams/validate-showdown` | POST | Body: `battle_format`, `showdown_text`. Runs Showdown’s **`validate-team`** in the **web** container (bundled checkout under **`SHOWDOWN_HOME`**, default `/opt/pokemon-showdown`). Response: `{ "ok", "errors": string[], "skipped"? }`. If **`TEAM_VALIDATION_DISABLED=true`**, returns **`ok: true`** with **`skipped: true`** without invoking Node. Random formats (`*randombattle`) return success without validating. |
| `/api/manager/teams` | GET, POST | List / create team preset (`name`, `battle_format`, `showdown_text`, optional `notes`). Create responses redact full text to a short preview on the wire. |
| `/api/manager/teams/{id}` | GET, PATCH, DELETE | Full row on GET. Patch/delete preset (**delete** blocked if a **queued** or **running** match references the team via **`player*_team_id`**) |
| `/api/manager/tournament-entries/{id}` | PATCH | Set `team_id` on a roster entry (`{"team_id": N}` or `null`) |
| `/api/manager/matches/{mid}` | GET | One match row |
| `/api/manager/matches/{mid}/start` | POST | Mark running |
| `/api/manager/matches/{mid}/complete` | POST | Record completion |
| `/api/manager/matches/{mid}/error` | POST | Record failure |
| `/api/manager/queue/next` | GET | Worker dequeue endpoint (match JSON may include `player1_team_showdown` / `player2_team_showdown`) |
| `/api/manager/queue/depth` | GET | Count of queued matches |
| `/api/manager/queue/upcoming` | GET | Next queued matches for UI |
| `/api/manager/queue/running` | GET | Current running match |
| `/api/manager/stream` | GET | Manager SSE refresh hints |
| `/api/manager/results` | GET | Completed matches |
| `/api/manager/stats` | GET | Aggregate analytics |
| `/api/battle/{match_id}/state` | POST, GET | `POST` (agents → web): `HumanPlayer` pushes the current structured battle state. `GET` (browser): latest state (non-SSE fallback). |
| `/api/battle/{match_id}/stream` | GET | **SSE** fan-out of battle state updates to the browser (per-subscriber `asyncio.Queue`). Ends with `event: match_end` when the match finishes. |
| `/api/battle/{match_id}/action` | POST, GET | `POST` (browser): submit human's `{action_type, index}` (1-based). `GET` (agents): `HumanPlayer` pops the pending action (404 if none). |
| `/api/battle/{match_id}/relay` | DELETE | `HumanPlayer` cleans up at match end. |

### Team presets (teambuilder workflow)

**Operator flow**

1. Open the **Showdown client** (local stack: `SHOWDOWN_VIEW_BASE`, default `http://localhost:8000`; used for “open teambuilder” links in the manager).
2. Build in **Teambuilder** and **Import/Export → Export** the team text.
3. In **`/manager/teams/new`** (or edit), choose **Battle format** from the same allowlist as matches/tournaments, paste the export, submit. The form calls **`POST /api/manager/teams/validate-showdown`** first; only if **`ok`** does it **POST** or **PATCH** `/api/manager/teams`.

**API note:** **`POST`/`PATCH` `/api/manager/teams`** persist the body **without** re-running Showdown validation. Scripts and integrations should call **`validate-showdown`** first if they need the same legality guarantees as the browser form.

Presets live in **SQLite** on **`manager-data`**, not in the browser’s Showdown `localStorage`.

**Random vs BYO**

- Formats whose id **ends with** **`randombattle`** use **server-assigned** teams. **`team_id`** / **`player*_team_id`** must **not** be sent; tournament plaintext import ignores extra team columns.
- All other listed formats are **custom-team**: both players need a preset (**`player1_team_id`** and **`player2_team_id`** on matches/series, or **`team_id`** on each tournament entry / plaintext line).

Shared rules live in **`web/manager/battle_format_rules.py`**. The manager UI loads **`web/static/battle-format-team-presets.js`**, which uses the same suffix as the server; **`GET /api/manager/config`** exposes **`random_team_battle_format_suffix`** for injection.

**Validation and image layout**

- The **`web`** Docker image **clones and builds** a copy of [pokemon-showdown](https://github.com/smogon/pokemon-showdown) and sets **`ENV SHOWDOWN_HOME=/opt/pokemon-showdown`** so **`team_showdown_validate.py`** can run  
  `node "${SHOWDOWN_HOME}/pokemon-showdown" validate-team <format>` with the paste on stdin (120s timeout).
- **`TEAM_VALIDATION_DISABLED=true`** (web service env) short-circuits validation: the validate endpoint returns success with **`skipped: true`**; use only when the bundled CLI path is broken or you intentionally skip legality checks.
- Misconfigured installs raise **503** (missing CLI / Node) or **504** (timeout).

**Matching presets to a matchup**

When a **`team_id`** is used, the row must exist (**404** otherwise). If the preset row’s **`battle_format`** is **non-empty**, it must equal the match or tournament **`battle_format`** (normalized case-insensitively). A **blank** stored format skips that check (legacy or advanced reuse); the create form always sets a format from the dropdown.

**Delete rule**

**`DELETE /api/manager/teams/{id}`** fails if any **queued** or **running** match still references that team via **`player1_team_id`** / **`player2_team_id`**.

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

Each participant line is comma-separated or pipe-separated (use `|` if the model id contains commas).

**`*randombattle` formats** (header `Battle Format` ends with `randombattle`):

- 3 fields: `provider`, `model`, `persona_slug`
- 4 fields: the same plus integer `seed` (then **every** line must include a seed)
- 5 fields: `provider`, `model`, `persona_slug`, `seed`, and an extra integer (e.g. copied from a BYO template) — the **fifth value is ignored**; team presets are not used for random battles.

`POST /api/manager/tournaments` also **ignores** `team_id` on each entry when the tournament `battle_format` is random.

**Custom-team formats** (anything that is **not** `*randombattle`): each line must end with a **team preset id** — the numeric `id` from the manager team library (`/manager/teams`, same as `GET /api/manager/teams`):

- 4 fields: `provider`, `model`, `persona_slug`, `team_id`
- 5 fields: `provider`, `model`, `persona_slug`, `seed`, `team_id` (seeds still follow the “all rows or none” rule)

Providers are `anthropic`, `deepseek`, or `openrouter`. Persona values must match a file in `agents/personas/`.

### Example (random teams)

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

### Example (BYO teams — `team_id` from team presets)

```text
Name: OU round robin
Type: Round Robin
Battle Format: gen9ou
Best Of: Bo1

Participants:
anthropic, claude-sonnet-4-20250514, aggro, 1
deepseek, deepseek-chat, stall, 2
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
| `create_match.sh` | Create a match or best-of series through the manager API (`--p1-team-id` / `--p2-team-id` **required** when `--format` is not `*randombattle`; **disallowed** for `*randombattle`) |
| `create_tournament.sh` | Create a tournament through the manager API (`--team-id` per `--player`, same order, when format is not `*randombattle`) |

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

1. `agents/queue_worker.py` pulls the next match from the manager API (including optional `player1_team_showdown` / `player2_team_showdown`, `player1_type` / `player2_type`, and `human_display_name`).
2. `match_runner.run_single_match()` dispatches:
   - **AI vs AI** (both `*_type == 'llm'`) → create two `LLMPlayer` instances, pass poke-env's **`team=`** when snapshots are present, run `agent1.battle_against(agent2)`.
   - **Human vs AI** (either side `== 'human'`) → `_run_human_vs_ai_match()` creates **one** `LLMPlayer` for the AI side and **one** `HumanPlayer` (`agents/human_player.py`) for the human side, then runs `battle_against()`. See **Human vs AI** below.
3. Each player connects to the local Showdown server over WebSocket.
4. Each turn:
   - **AI side:** `LLMPlayer.choose_move(battle)` sends state + persona prompt to the provider, parses the JSON action, posts reasoning/callout to `/thought`, returns a poke-env order.
   - **Human side:** `HumanPlayer.choose_move(battle)` POSTs structured state to the battle relay, polls `/api/battle/{id}/action` for the human's submission (up to `HUMAN_TURN_TIMEOUT`; falls back to random on timeout), returns a poke-env order.
5. On completion, the worker reports results, saves the replay, and optionally saves raw battle logs.
6. The worker waits for the configured delay, then polls the queue again.

## Human vs AI

Either side of a match can be flipped from AI to Human. Instead of two LLM agents, one poke-env `LLMPlayer` battles one `HumanPlayer` relay while the human plays through a custom web page at `/battle/{match_id}`.

### Flow

1. In **`/manager/matches/new`**, toggle one side's **Player Type** to **Human** and enter a **Display Name** (required; ≤18 chars, used as the Showdown login username and the `{opponent_name}` value in the AI's prompt). The other side locks to AI; match type is forced to **Single Match**.
2. On submit, the success message shows **"Open Battle Page →"** → `/battle/{match_id}`.
3. The agents container's queue worker picks up the match and calls `_run_human_vs_ai_match()`.
4. `HumanPlayer` (agents) connects to Showdown as the display name; `LLMPlayer` (agents) connects with its persona-derived username. `battle_against()` coordinates challenge + accept.
5. On each turn:
   - `HumanPlayer.choose_move(battle)` serializes state (`build_battle_state_json` in `agents/human_player.py`) and POSTs to **`/api/battle/{match_id}/state`**.
   - The web service fans the state out via SSE to every browser subscribed to **`/api/battle/{match_id}/stream`**.
   - The battle control page renders the state and enables move/switch buttons.
   - The human clicks Submit → browser POSTs **`/api/battle/{match_id}/action`** with `{action_type: 'move'|'switch', index: 1-based}`.
   - `HumanPlayer` polls **`GET /api/battle/{match_id}/action`** every 500 ms, receives the action, converts it to a poke-env order and returns.
   - Meanwhile the AI side plays normally; its reasoning + callouts flow to the battle control page via the existing `/thoughts/ws` websocket (filtered to the AI username).
6. On match end, `HumanPlayer` `DELETE`s the relay; the SSE stream emits `event: match_end`; the battle page shows the result and a Dashboard link.

### Prompt adaptation

`build_system_prompt()` in `agents/match_runner.py` accepts **`opponent_is_human=True`**, which injects a `_HUMAN_OPPONENT_BLOCK` telling the AI its opponent is a real person — lean into callouts, address them directly, play to win. The `{opponent_name}` template variable resolves to `human_display_name` in every persona's prompt body (e.g. aggro.md's *"Your opponent is {opponent_name}"* becomes *"Your opponent is Tom"*).

### Battle control page

`web/templates/battle.html` + `web/static/battle.{js,css}`:

- **Opponent persona panel:** large square portrait (preferred: `/static/portraits/square/{slug}.png`; falls back to tall portrait, then trainer sprite; shimmer loading state until a real image resolves), persona name, opponent's active Pokémon stats.
- **Callout bubble:** most recent AI callout, styled as a quote.
- **Reasoning feed:** last ~5 AI reasoning entries with turn labels (via `/thoughts/ws`, filtered to the AI's Showdown username).
- **Field bar:** weather, terrain, side conditions, turn counter, remaining-Pokémon totals.
- **Your active Pokémon:** species, types, HP bar (green/yellow/red), ability/item/status/boosts.
- **Action buttons:** move buttons with type badges, effectiveness badges (`0x` / `0.5x` / `1x` / `2x` / `4x`), power/accuracy/PP; switch buttons with HP % and hazard-damage estimate. Force-switch state hides moves when the active Pokémon faints.
- **Battle iframe:** embedded Showdown battle view (`SHOWDOWN_VIEW_BASE`) with the AI persona's trainer sprite injected via `postMessage({type: "llm_trainer_sprites", ...})` (same protocol as `/broadcast`). Callouts are also posted (`{type: "llm_callouts"}`) but only visible when the user is on Showdown's battle tab — the pill overlay auto-hides on the chat tab (anchor visibility check in `showdown/static/index.html`).
- **Turn timer:** 150s countdown (`HUMAN_TURN_TIMEOUT`) per turn; urgent red below 30s.
- **Responsive:** single column on narrow viewports; on ≥1100 px, controls on the left and iframe on the right (sticky).

### Relay implementation

`web/battle_relay.py`:

- In-memory `BattleRelay` per match: latest state, pending action, list of subscriber queues.
- **Per-subscriber `asyncio.Queue`** fan-out (each browser tab gets its own). `push_state` iterates all subscribers; each SSE generator pulls from its own queue.
- Stale relays (no state for 10 min) are cleaned up.
- Action validation: checks `action_type in {'move','switch'}` and that `index` is within the current state's `available_moves` / `available_switches`.

### DB columns

Added by `_migrate_human_player_columns` in `web/manager/db.py` on both `matches` and `series`:

| Column | Type | Default | Meaning |
| --- | --- | --- | --- |
| `player1_type` | TEXT | `'llm'` | `'llm'` or `'human'` |
| `player2_type` | TEXT | `'llm'` | `'llm'` or `'human'` |
| `human_display_name` | TEXT | `NULL` | Human's Showdown username + `{opponent_name}` in the AI prompt. Defaults to `'Challenger'` when empty. |
| `human_play_mode` | TEXT | `'showdown'` | Currently always written as `'control_page'` — the Showdown-client play mode is disabled (UI removed). Kept for forward-compat. |

### Out of scope today

- **Human vs Human** — form blocks both sides being human.
- **Humans in tournaments** — tournament entries can't be marked human.
- **Showdown-client play mode** — the toggle was removed; humans always play via the battle control page. The `showdown/static/index.html` auto-nick-from-URL hook remains for future use but is no longer wired into match creation.

### Env vars

| Variable | Default | Purpose |
| --- | --- | --- |
| `HUMAN_TURN_TIMEOUT` | `150` | Seconds `HumanPlayer.choose_move()` polls for the human's action before falling back to random. |
| `HUMAN_ACTION_POLL_INTERVAL` | `0.5` | Poll interval for `GET /api/battle/{id}/action`. |

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
| `/battle/{match_id}` | GET | Human vs AI battle control page |
| `/api/battle/{match_id}/state` | POST, GET | HumanPlayer push state; browser fallback read |
| `/api/battle/{match_id}/stream` | GET | SSE: battle state updates |
| `/api/battle/{match_id}/action` | POST, GET | Browser submit action; HumanPlayer pop action |
| `/api/battle/{match_id}/relay` | DELETE | HumanPlayer cleanup |
| `/replays` | GET | Replay and log index |
| `/health` | GET | Health check |

## Configuration Summary

See `.env.example` for the full set of variables. The most important groups are:

- LLM provider keys: `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`
- Battle pacing and queue: `QUEUE_POLL_INTERVAL`, `TURN_DELAY_SECONDS`, `DELAY_BETWEEN_MATCHES`, `LLM_TURN_TIMEOUT`
- Broadcast timing: `MATCH_INTRO_SECONDS`, `VICTORY_MODAL_SECONDS`, `TOURNAMENT_VICTORY_MODAL_SECONDS`, `BRACKET_INTERSTITIAL_SECONDS`
- Optional features: `POKEDEX_TOOL_ENABLED`, `POKEDEX_AUTO_ENRICH`, `ENABLE_MEMORY`
- Human vs AI: `HUMAN_TURN_TIMEOUT`, `HUMAN_ACTION_POLL_INTERVAL`
- Team import (web): `TEAM_VALIDATION_DISABLED`, `SHOWDOWN_HOME` (see `.env.example`)
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

