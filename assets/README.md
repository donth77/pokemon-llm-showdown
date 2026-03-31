# Shared assets

Static files used by the **web** service (manager, overlays, victory splash, etc.) and—where needed—the **Showdown** client (trainer sprites in-battle).

## Persona image requirements

Each persona (Markdown slug in `agents/personas/{slug}.md`) can use three on-disk images. Use the **same `{slug}`** in filenames, e.g. `aggro.png`.

### Trainer sprite (`static/trainers/`)

| | |
| --- | --- |
| **Purpose** | In-battle trainer graphic on the local Showdown client and anywhere the UI references `/static/trainers/...`. |
| **Target size** | About **80×80** px (what Showdown expects for custom trainers). |
| **Formats** | **PNG**, **GIF**, or **WebP** only. |
| **Upload limit** | **~2.5 MB** per file (Manager upload path). |
| **Art direction** | **Transparent background** so the sprite sits cleanly on the battle UI. **Face left** so the character reads correctly in the default trainer frame (matches Manager copy). |
| **Default file** | If you do not set a custom name in persona front matter, the app looks for `{slug}.png`. |

### Tall portrait (`static/portraits/`)

| | |
| --- | --- |
| **Purpose** | Bust / vertical art for the manager, broadcast scenes, matchup and victory splashes, and other “card” layouts. |
| **Target size** | **512×640** px (4∶5 aspect). Other sizes work but may scale or crop oddly. |
| **Formats** | **PNG**, **GIF**, or **WebP** only. |
| **Upload limit** | **~5 MB** per file (Manager upload path). |
| **Art direction** | **Transparent background recommended** so overlays and varied backgrounds look correct. |

### Square headshot (`static/portraits/square/`)

| | |
| --- | --- |
| **Purpose** | Compact avatar (the manager tournament UI clips this to a **circle** in places). |
| **Target size** | **512×512** px. |
| **Formats** | **PNG**, **GIF**, or **WebP** only. |
| **Upload limit** | **~5 MB** per file (Manager upload path). |
| **Art direction** | **Transparent background recommended**; frame the face for a small round crop. |

### Required combinations

- **Trainer sprite:** optional filename in YAML; if unset, expect `{slug}.png` on disk (you can still use `.gif` / `.webp` if you set `sprite` / upload accordingly so the resolved path matches).
- **Both portraits are required** before creating or saving a persona in the Manager: one file under `static/portraits/` **and** one under `static/portraits/square/` for the same slug.

You can add files under `assets/static/...` on disk or use **Manager → Personas** uploads (writes to the mounted `PORTRAITS_DIR` / `TRAINERS_DIR` in Docker).

## Directory layout

| Path | Role |
|------|------|
| `static/trainers/` | Trainer sprites (~80×80). Served as `/static/trainers/` on web and `/trainers/` on Showdown (see custom `index.html`). |
| `static/portraits/` | Tall portraits — target **512×640**. URL: `/static/portraits/{slug}.{ext}`. |
| `static/portraits/square/` | Square headshots — target **512×512**. URL: `/static/portraits/square/{slug}.{ext}`. |

Repo paths are bind-mounted into containers; URLs stay stable (`/static/trainers/...`, `/static/portraits/...`).

Showdown-specific overrides (e.g. custom battle UI) stay under `showdown/static/` (not here).
