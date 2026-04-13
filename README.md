# Gotta Prompt 'Em All

_Technical repo name: `pokemon-llm-showdown`._

Run LLM-vs-LLM Pokemon Showdown battles locally, queue matches and tournaments from a web manager, and compare results across providers, models, personas, and formats. Streaming is optional: the core stack is `showdown`, `web`, and `agents`; add `stream` if you want headless Twitch output.

![Screenshot - Broadcast](https://i.ibb.co/b5Ycp6jy/Screenshot-2026-04-01-at-1-35-24-PM.png)

Manager
![Screenshot - Manager](https://i.ibb.co/Kc9GYTNM/Screenshot-2026-04-13-at-1-43-03-AM.png)

Human vs AI
![Screenshot - Human vs AI](https://i.ibb.co/pvDtfs6t/Screenshot-2026-04-13-at-1-37-19-AM.png)

## What You Get

- A local Pokemon Showdown server
- A web manager for queueing matches and tournaments
- LLM battle agents with selectable provider, model, and persona per side
- **Human vs AI** — play against an LLM persona through a custom battle control page at `/battle/{match_id}` (AI opponent portrait, real-time reasoning and callouts, styled move/switch buttons, Showdown battle view embedded)
- Replays, logs, results, and aggregate stats
- **Optional bring-your-own teams** for formats that are not random battles: build in local Showdown’s Teambuilder, then save presets under **Manager → Team presets** (details in `docs/technical-reference.md`)
- Optional broadcast overlays and Twitch streaming

## Prerequisites

- Docker and Docker Compose v2+
- At least one API key: `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, or `OPENROUTER_API_KEY`
- About 4 GB RAM and 10 GB disk for images
- A Twitch stream key only if you want to use the `stream` service

## Quick Start

```bash
git clone https://github.com/donth77/pokemon-llm-showdown.git
cd pokemon-llm-showdown

cp .env.example .env
# Edit .env and add at least one provider API key

# Start the core stack
docker compose up -d --build showdown web agents

# Or start everything, including Twitch streaming
# docker compose up -d --build

# Verify the stack
docker compose ps
bash scripts/healthcheck.sh
```

## First Run

1. Open `http://localhost:8080/manager`
2. Queue a match or tournament
3. Watch the live broadcast at `http://localhost:8080/broadcast`
4. Browse completed results and replays in the Manager UI

Matchups are configured through the Manager UI or the CLI scripts. The `agents` service just needs valid API keys for whichever providers your queued matches use.

## Common URLs

| URL | Purpose |
| --- | --- |
| `http://localhost:8080/manager` | Queue matches and tournaments |
| `http://localhost:8080/broadcast` | Full broadcast scene |
| `http://localhost:8080/battle/{match_id}` | Human vs AI battle control page (shown as a link in the manager after queueing a human match) |
| `http://localhost:8080/replays` | Replay and log browser |
| `http://localhost:8080/scoreboard` | Current scoreboard JSON |
| `http://localhost:8000` | Local Pokemon Showdown server |

## Common Commands

```bash
# Watch agent logs
docker compose logs -f agents

# Restart the core services
bash scripts/restart_stack.sh

# Stop everything
bash scripts/stack_down.sh

# Queue work from the CLI
bash scripts/create_match.sh --help
bash scripts/create_tournament.sh --help
```

## Streaming

You have two options:

- Use the built-in `stream` service for headless Twitch output
- Use OBS and point Browser Sources at the local web pages such as `/broadcast`, `/overlay`, and `/victory`

For OBS layering details and the full broadcast route reference, see `docs/technical-reference.md`.

## Personas and Assets

Personas live in `agents/personas/` as Markdown files with YAML front matter. Trainer sprites and portraits live under `assets/static/`. See `assets/README.md` for asset layout details.

## More Documentation

- `AGENTS.md`: contributor and coding-agent orientation—stack layout, conventions, key endpoints, and operational gotchas (source of truth). `CLAUDE.md` is a short pointer at repo root for tools that only read that filename
- `docs/technical-reference.md`: human-readable deep dive—architecture, APIs, operations, streaming, env summary, troubleshooting
- `assets/README.md`: trainer sprites and persona portrait asset layout
- `.env.example`: the full environment variable template
