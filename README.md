# AI Pokemon Showdown Livestream

Automated 24/7 Twitch livestream where two AI agents battle each other on Pokemon Showdown. Runs fully headless — no OBS, no GUI.

## Architecture

```
docker-compose.yml
├── showdown/    — Pokemon Showdown server (local, no auth)
├── agents/      — Two Python AI agents (poke-env)
├── stream/      — Xvfb + Chromium + FFmpeg → Twitch RTMP
├── overlay/     — FastAPI scoreboard & overlay server
└── scripts/     — Health checks & utilities
```

**Agents:**

- **MaxDamage** — always picks the highest base power move
- **SmartBot** — picks moves factoring type effectiveness + STAB

## Prerequisites

- Docker & Docker Compose v2+
- A Twitch account with a stream key ([get it here](https://dashboard.twitch.tv/settings/stream))
- ~4 GB RAM, ~10 GB disk for images

## Quick Start

```bash
# 1. Clone the repo
git clone <your-repo-url> && cd pokemon-llm-showdown

# 2. Set your Twitch stream key
cp .env.example .env
# Edit .env and paste your TWITCH_STREAM_KEY

# 3. Launch everything
docker compose up -d --build

# 4. Check service health
docker compose ps
bash scripts/healthcheck.sh
```

## Services

| Service    | Port | Description                              |
| ---------- | ---- | ---------------------------------------- |
| `showdown` | 8000 | Pokemon Showdown battle server           |
| `overlay`  | 8080 | FastAPI overlay (scoreboard, match data) |
| `agents`   | —    | AI battle agents (no exposed port)       |
| `stream`   | —    | Xvfb + Chromium + FFmpeg to Twitch       |

## Useful Commands

```bash
# View agent logs (battle output)
docker compose logs -f agents

# View stream logs (FFmpeg output)
docker compose logs -f stream

# Check scoreboard
curl http://localhost:8080/scoreboard

# View overlay in browser
open http://localhost:8080/overlay

# Browse saved replay history
open http://localhost:8080/replays

# Restart a single service
docker compose restart agents

# Manually start a fresh battle run (respects MATCH_COUNT)
bash scripts/start_battle.sh

# Manually start and reset replay/log/results history first
bash scripts/start_battle.sh --reset

# Set Twitch stream title/category via API (requires OAuth env vars)
bash scripts/set_twitch_title.sh

# Stop everything
docker compose down
```

### Set Twitch Dashboard Title

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
bash scripts/set_twitch_title.sh "Testing Pokemon Showdown battles with LLMs"
```

When `stream` starts via Docker, it also attempts to set Twitch title/category automatically if these env vars are present (`TWITCH_CLIENT_ID`, `TWITCH_OAUTH_TOKEN`, `TWITCH_BROADCASTER_ID`). Disable with `TWITCH_AUTO_SET_TITLE=0`.

## Overlay Endpoints

| Endpoint      | Method | Description                                |
| ------------- | ------ | ------------------------------------------ |
| `/scoreboard` | GET    | Win/loss records as JSON                   |
| `/result`     | POST   | Submit match result (used by orchestrator) |
| `/overlay`    | GET    | Transparent HTML overlay for compositing   |
| `/replays`    | GET    | Replay history page (clickable HTML files) |
| `/health`     | GET    | Health check                               |

## Configuration

Key environment variables (see `.env.example`):

| Variable              | Required | Default                                      | Description                                       |
| --------------------- | -------- | -------------------------------------------- | ------------------------------------------------- |
| `TWITCH_STREAM_KEY`   | Yes      | —                                            | Your Twitch stream key                            |
| `STREAM_TITLE`        | No       | `Testing Pokemon Showdown battles with LLMs` | Title text rendered on broadcast scene            |
| `STREAM_AUDIO_SOURCE` | No       | `pulse`                                      | Audio source mode: `browser`, `music`, or `pulse` |
| `BATTLE_MUSIC_INPUT`  | No       | empty                                        | Looping FFmpeg music input (file path or URL)     |
| `SHOWDOWN_HOST`       | No       | `showdown`                                   | Showdown server host                              |
| `SHOWDOWN_PORT`       | No       | `8000`                                       | Showdown server port                              |
| `OVERLAY_HOST`        | No       | `overlay`                                    | Overlay service host                              |
| `OVERLAY_PORT`        | No       | `8080`                                       | Overlay service port                              |
| `REPLAY_DIR`          | No       | `/replays`                                   | Local replay export path                          |

## Development

To test without streaming to Twitch, comment out the `stream` service in `docker-compose.yml` and run the other three:

```bash
docker compose up -d showdown overlay agents
```

Watch the battles via logs:

```bash
docker compose logs -f agents
```

## Future Additions

- LLM-powered commentary
- TTS narration
- More sophisticated AI agents
- Chat integration
- Tournament brackets
