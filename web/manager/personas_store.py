"""
Read/write persona markdown files, trainer sprites, and optional portrait images for the manager UI.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

PERSONAS_DIR = Path(os.getenv("PERSONAS_DIR", "/personas"))
TRAINERS_DIR = Path(os.getenv("TRAINERS_DIR", "/app/static/trainers"))
PORTRAITS_DIR = Path(os.getenv("PORTRAITS_DIR", "/app/static/portraits"))
PORTRAIT_IMAGE_EXT = (".png", ".gif", ".webp")
PORTRAIT_MAX_BYTES = 5_000_000

SLUG_RE = re.compile(r"^[a-z0-9_-]+$")

FM_KEY_ORDER = ("name", "abbreviation", "description", "sprite", "sprite_url")


def validate_slug(raw: str) -> str:
    slug = (raw or "").strip().lower()
    if not slug or not SLUG_RE.fullmatch(slug):
        raise ValueError(
            "Slug must be non-empty and use only lowercase letters, digits, '-' or '_'."
        )
    return slug


def safe_trainer_filename(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s or "/" in s or "\\" in s or s.startswith("."):
        return None
    base = Path(s).name
    if base != s:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", base):
        return None
    return base


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.strip().startswith("---\n"):
        return {}, text
    closing = text.find("\n---\n", 4)
    if closing == -1:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[4:closing].splitlines():
        stripped = line.strip()
        if ":" in stripped:
            k, v = stripped.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    body = text[closing + 5 :]
    return meta, body


def resolve_sprite_url(slug: str, meta: dict[str, str]) -> str:
    raw_url = (meta.get("sprite_url") or "").strip()
    if raw_url:
        lower = raw_url.lower()
        if lower.startswith("http://") or lower.startswith("https://"):
            return raw_url
        if raw_url.startswith("/"):
            return raw_url
    sprite_key = (meta.get("sprite") or "").strip()
    safe = safe_trainer_filename(sprite_key)
    if safe:
        return f"/static/trainers/{safe}"
    return f"/static/trainers/{slug}.png"


def local_trainer_file_for_persona(slug: str, meta: dict[str, str]) -> Path | None:
    """
    Resolved on-disk sprite under TRAINERS_DIR, or None if the persona uses a remote
    or non-/static/trainers/ URL (nothing to delete locally).
    """
    raw_url = (meta.get("sprite_url") or "").strip()
    if raw_url:
        lower = raw_url.lower()
        if lower.startswith("http://") or lower.startswith("https://"):
            return None
        if raw_url.startswith("/static/trainers/"):
            tail = raw_url.removeprefix("/static/trainers/").lstrip("/").split("/")[0]
            safe = safe_trainer_filename(tail)
            return _safe_trainer_path(safe) if safe else None
        if raw_url.startswith("/"):
            return None
    sprite_key = (meta.get("sprite") or "").strip()
    safe = safe_trainer_filename(sprite_key)
    if safe:
        return _safe_trainer_path(safe)
    return _safe_trainer_path(safe_trainer_filename(f"{slug}.png"))


def _safe_trainer_path(safe_name: str | None) -> Path | None:
    if not safe_name:
        return None
    safe = safe_trainer_filename(safe_name)
    if not safe:
        return None
    try:
        base = TRAINERS_DIR.resolve()
        candidate = (TRAINERS_DIR / safe).resolve()
        if not candidate.is_relative_to(base):
            return None
    except (OSError, ValueError):
        return None
    return TRAINERS_DIR / safe


def read_persona(slug: str) -> dict:
    slug = validate_slug(slug)
    path = PERSONAS_DIR / f"{slug}.md"
    if not path.is_file():
        raise FileNotFoundError(slug)
    text = path.read_text(encoding="utf-8")
    meta, body = parse_front_matter(text)
    return {
        "slug": slug,
        "meta": meta,
        "body": body,
        "sprite_url": resolve_sprite_url(slug, meta),
        "portrait_tall_url": resolve_portrait_url(slug, square=False),
        "portrait_square_url": resolve_portrait_url(slug, square=True),
    }


def list_personas() -> list[dict]:
    if not PERSONAS_DIR.exists():
        return []
    out: list[dict] = []
    for p in sorted(PERSONAS_DIR.glob("*.md")):
        slug = p.stem
        try:
            data = read_persona(slug)
        except (FileNotFoundError, ValueError, OSError):
            continue
        desc = (data["meta"].get("description") or "").strip()
        if len(desc) > 180:
            desc = desc[:177] + "..."
        out.append(
            {
                "slug": data["slug"],
                "name": data["meta"].get("name") or slug.replace("_", " ").title(),
                "description": desc,
                "sprite_url": data["sprite_url"],
            }
        )
    return out


def write_persona(slug: str, meta: dict[str, str], body: str) -> None:
    slug = validate_slug(slug)
    PERSONAS_DIR.mkdir(parents=True, exist_ok=True)
    path = PERSONAS_DIR / f"{slug}.md"
    lines = ["---"]
    for key in FM_KEY_ORDER:
        val = (meta.get(key) or "").strip()
        if val:
            lines.append(f"{key}: {val}")
    for key, val in sorted(meta.items()):
        lk = key.lower().strip()
        if lk in FM_KEY_ORDER or not val:
            continue
        lines.append(f"{lk}: {val.strip()}")
    lines.append("---")
    lines.append("")
    body = body if body.endswith("\n") else (body + "\n")
    content = "\n".join(lines) + body
    path.write_text(content, encoding="utf-8")


def create_persona(slug: str, meta: dict[str, str] | None = None, body: str | None = None) -> None:
    slug = validate_slug(slug)
    path = PERSONAS_DIR / f"{slug}.md"
    if path.exists():
        raise FileExistsError(slug)
    meta = meta or {
        "name": slug.replace("_", " ").title(),
        "abbreviation": slug[:3].upper(),
        "description": "",
    }
    default_body = (
        f"You are a Pokémon battle AI named {{player_name}}.\n"
        f"Your opponent is {{opponent_name}}.\n\n"
        f"(Edit this prompt body — use only {{player_name}} and {{opponent_name}}.)\n"
    )
    write_persona(slug, meta, body if body is not None else default_body)


def delete_persona(
    slug: str,
    *,
    delete_trainer_sprite: bool = False,
    delete_portrait_tall: bool = False,
    delete_portrait_square: bool = False,
) -> None:
    slug = validate_slug(slug)
    path = PERSONAS_DIR / f"{slug}.md"
    trainer_path: Path | None = None
    if delete_trainer_sprite and path.is_file():
        meta, _ = parse_front_matter(path.read_text(encoding="utf-8"))
        trainer_path = local_trainer_file_for_persona(slug, meta)
    if path.is_file():
        path.unlink()
    if delete_trainer_sprite and trainer_path is not None and trainer_path.is_file():
        try:
            trainer_path.unlink()
        except OSError:
            pass
    for flag, is_sq in (
        (delete_portrait_tall, False),
        (delete_portrait_square, True),
    ):
        if not flag:
            continue
        pp = find_portrait_file(slug, square=is_sq)
        if pp is not None and pp.is_file():
            try:
                pp.unlink()
            except OSError:
                pass


def list_trainer_filenames() -> list[str]:
    if not TRAINERS_DIR.is_dir():
        return []
    names = []
    for p in TRAINERS_DIR.iterdir():
        if p.is_file() and p.suffix.lower() in (".png", ".gif", ".webp"):
            names.append(p.name)
    return sorted(names)


def _portrait_subdir(square: bool) -> Path:
    return PORTRAITS_DIR / "square" if square else PORTRAITS_DIR


def _strip_other_portrait_exts(dest_dir: Path, slug: str, keep_ext: str) -> None:
    for ext in PORTRAIT_IMAGE_EXT:
        if ext == keep_ext:
            continue
        p = dest_dir / f"{slug}{ext}"
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass


def save_portrait_upload(
    slug: str, filename: str, data: bytes, *, square: bool, max_bytes: int = PORTRAIT_MAX_BYTES
) -> None:
    """Write portrait to ``{slug}.{ext}`` under tall or ``square/``; replaces other ext for same slug."""
    slug = validate_slug(slug)
    if len(data) > max_bytes:
        raise ValueError("Portrait too large (max ~5 MB).")
    safe_name = safe_trainer_filename(filename)
    if not safe_name:
        raise ValueError("Invalid portrait filename.")
    ext = Path(safe_name).suffix.lower()
    if ext not in PORTRAIT_IMAGE_EXT:
        raise ValueError("Only .png, .gif or .webp portraits are allowed.")
    dest_dir = _portrait_subdir(square)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{slug}{ext}"
    try:
        base = dest_dir.resolve()
        resolved = dest.resolve()
        if not resolved.is_relative_to(base):
            raise ValueError("Invalid portrait path")
    except (OSError, ValueError) as e:
        raise ValueError("Invalid portrait path") from e
    _strip_other_portrait_exts(dest_dir, slug, ext)
    dest.write_bytes(data)


def find_portrait_file(slug: str, *, square: bool) -> Path | None:
    slug = validate_slug(slug)
    dest_dir = _portrait_subdir(square)
    if not dest_dir.is_dir():
        return None
    for ext in PORTRAIT_IMAGE_EXT:
        p = dest_dir / f"{slug}{ext}"
        if p.is_file():
            return p
    return None


def resolve_portrait_url(slug: str, *, square: bool) -> str | None:
    p = find_portrait_file(slug, square=square)
    if p is None:
        return None
    if square:
        return f"/static/portraits/square/{p.name}"
    return f"/static/portraits/{p.name}"


def require_both_portraits(slug: str) -> None:
    """Every persona must have tall and square portrait files on disk (PNG / GIF / WebP)."""
    slug = validate_slug(slug)
    if find_portrait_file(slug, square=False) is None:
        raise ValueError(
            "Tall portrait is required. Upload a PNG, GIF, or WebP, or place "
            f"`assets/static/portraits/{slug}.(png|gif|webp)` before saving."
        )
    if find_portrait_file(slug, square=True) is None:
        raise ValueError(
            "Square headshot is required. Upload a PNG, GIF, or WebP, or place "
            f"`assets/static/portraits/square/{slug}.(png|gif|webp)` before saving."
        )


def delete_all_portraits_for_slug(slug: str) -> None:
    """Remove tall and square portrait files for ``slug`` (e.g. failed create rollback)."""
    slug = validate_slug(slug)
    for sq in (False, True):
        p = find_portrait_file(slug, square=sq)
        if p is not None and p.is_file():
            try:
                p.unlink()
            except OSError:
                pass


def save_trainer_upload(filename: str, data: bytes, max_bytes: int = 2_500_000) -> str:
    if len(data) > max_bytes:
        raise ValueError("Image too large (max ~2.5 MB).")
    safe = safe_trainer_filename(filename)
    if not safe:
        raise ValueError("Invalid filename — use letters, numbers, . _ - only.")
    ext = Path(safe).suffix.lower()
    if ext not in (".png", ".gif", ".webp"):
        raise ValueError("Only .png, .gif or .webp trainer images are allowed.")
    TRAINERS_DIR.mkdir(parents=True, exist_ok=True)
    dest = TRAINERS_DIR / safe
    dest.write_bytes(data)
    return safe
