# Shared assets

Static files used by the **web** service (manager, overlays, victory splash, etc.) and—where needed—the **Showdown** client (trainer sprites in-battle).

## Layout

| Path | Role |
|------|------|
| `static/trainers/` | Trainer sprites (~80×80), PNG/GIF/WebP. Served as `/static/trainers/` on web and `/trainers/` on Showdown (see custom `index.html`). |
| `static/portraits/` | **Taller / bust** portraits — target **512×640** (4∶5). **PNG, GIF, or WebP.** Web URL: `/static/portraits/{slug}.{ext}`. |
| `static/portraits/square/` | **Square headshots** — target **512×512**. **PNG, GIF, or WebP.** Web URL: `/static/portraits/square/{slug}.{ext}`. |

Use the **same `{slug}`** as the persona markdown file (e.g. `aggro.png`, `villain.webp`). **Both** tall and square files are **required** for each persona (enforced when creating or saving in the manager). Uploads from **Manager → Personas** save here (`PORTRAITS_DIR`, default `/app/static/portraits`). Application code picks which variant to show in overlays or victory UI.

Repo paths are bind-mounted into containers; URLs stay stable (`/static/trainers/...`, `/static/portraits/...`).

Showdown-specific overrides (e.g. custom battle UI) stay under `showdown/static/` (not here).
