"""
Parse plaintext tournament definitions (textarea / .txt import).

Format (single tournament per file) —
  Lines are trimmed. Whole-line comments start with #.

  Header keys (Key: Value), case-insensitive keys; spaces/hyphens in keys are ignored.

    Name: …
    Type: single_elimination   # or: single elimination, round robin, …
    Battle Format: gen9randombattle
    Best Of: 3                 # or Bo3, bo5, …
    Single Elim Bracket: compact   # optional: compact | power_of_two (elim types only)

  Participants:
    provider, model_id, persona_slug
    provider | model_id | persona_slug | seed

  Each participant line is comma- OR pipe-separated.
  *randombattle formats: 3 fields, or 4 with optional seed (integer), or 5 with
  seed plus an extra integer column that is ignored (for shared templates with BYO).
  Custom-team formats: 4 fields (provider, model, persona, team_id), or 5 with seed
  before team_id. Team ids are manager team preset integers.

  Model ids must not contain the delimiter you use (prefer | if the id has commas).

Validation reuses provider_model_validate; persona slugs are checked when
valid_persona_slugs is provided.
"""

from __future__ import annotations

import re
from typing import Any

from . import battle_format_rules
from .provider_model_validate import validate_provider_model

_TYPE_ALIASES: dict[str, frozenset[str]] = {
    "round_robin": frozenset(
        {"round_robin", "roundrobin", "round robin", "round-robin", "rr"}
    ),
    "single_elimination": frozenset(
        {
            "single_elimination",
            "singleelimination",
            "single elimination",
            "single-elimination",
            "single elim",
            "knockout",
            "elimination",
        }
    ),
    "double_elimination": frozenset(
        {
            "double_elimination",
            "doubleelimination",
            "double elimination",
            "double-elimination",
            "double elim",
        }
    ),
}

_BRACKET_ALIASES: dict[str, frozenset[str]] = {
    "compact": frozenset({"compact", "dense"}),
    "power_of_two": frozenset(
        {"power_of_two", "power-of-two", "power of two", "pow2", "padded", "classic"}
    ),
}

KNOWN_KEYS_BEFORE_PARTICIPANTS = frozenset(
    {"name", "type", "battle_format", "best_of", "single_elim_bracket"}
)


def _normalize_key(raw: str) -> str:
    s = raw.strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    aliases = {
        "battleformat": "battle_format",
        "format": "battle_format",
        "tournament_name": "name",
        "tournament_type": "type",
        "bestof": "best_of",
        "bo": "best_of",
        "bracket": "single_elim_bracket",
        "single_elim_bracket": "single_elim_bracket",
        "winners_bracket": "single_elim_bracket",
        "winnersbracket": "single_elim_bracket",
    }
    return aliases.get(s, s)


def _resolve_type(value: str) -> str | None:
    v = value.strip().lower().replace("-", "_")
    v_compact = re.sub(r"[\s_]+", "_", v)
    for canonical, aliases in _TYPE_ALIASES.items():
        if v_compact == canonical or value.strip().lower() in aliases:
            return canonical
        if v_compact in {re.sub(r"[\s_]+", "_", a) for a in aliases}:
            return canonical
    for canonical, aliases in _TYPE_ALIASES.items():
        for a in aliases:
            if a.replace(" ", "_") == v_compact or a.replace("-", "_") == v_compact:
                return canonical
    return None


def _resolve_bracket(value: str) -> str | None:
    x = value.strip().lower()
    for canonical, aliases in _BRACKET_ALIASES.items():
        if x == canonical or x.replace("_", " ") in {
            a.replace("_", " ") for a in aliases
        }:
            return canonical
        if x in aliases:
            return canonical
    return None


def _parse_best_of(value: str) -> int | None:
    s = value.strip().lower().replace(" ", "")
    m = re.match(r"^(?:bo|bestof)?(\d+)$", s)
    if not m:
        m = re.match(r"^(\d+)$", s)
    if not m:
        return None
    n = int(m.group(1))
    return n if n >= 1 else None


def _split_participant_line(line: str) -> list[str]:
    if "|" in line:
        parts = [p.strip() for p in line.split("|")]
    else:
        parts = [p.strip() for p in line.split(",")]
    return [p for p in parts if p != ""]


def _strip_bom(text: str) -> str:
    if text.startswith("\ufeff"):
        return text[1:]
    return text


def parse_tournament_definition(
    text: str,
    *,
    valid_battle_formats: frozenset[str] | None = None,
    valid_persona_slugs: set[str] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    """
    Returns (payload_for_create_tournament_api | None, errors, warnings).
    errors: { "line": int, "message": str }
    """
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []

    def err(line_no: int, msg: str) -> None:
        errors.append({"line": line_no, "message": msg})

    raw_lines = _strip_bom(text).splitlines()
    lines: list[tuple[int, str]] = []
    for i, ln in enumerate(raw_lines, start=1):
        t = ln.strip()
        if not t or t.startswith("#"):
            continue
        lines.append((i, t))

    headers: dict[str, str] = {}
    participant_lines: list[tuple[int, str]] = []
    phase = "headers"

    for line_no, t in lines:
        if phase == "headers":
            low = t.lower().strip()
            if low in ("participants:", "participant:"):
                phase = "participants"
                continue
            if ":" not in t:
                err(line_no, "Expected 'Key: value' or 'Participants:' section header")
                continue
            key_raw, val_raw = t.split(":", 1)
            key = _normalize_key(key_raw)
            val = val_raw.strip()
            if key not in KNOWN_KEYS_BEFORE_PARTICIPANTS:
                err(
                    line_no,
                    f"Unknown key {key_raw.strip()!r}; use Name, Type, Battle Format, "
                    "Best Of, Single Elim Bracket, or start Participants:",
                )
                continue
            if key in headers:
                err(line_no, f"Duplicate key {key_raw.strip()!r}")
                continue
            headers[key] = val
        else:
            participant_lines.append((line_no, t))

    name = (headers.get("name") or "").strip()
    if not name:
        err(0, "Name is required")

    t_raw = headers.get("type", "").strip()
    if not t_raw:
        err(0, "Type is required")
    t_type = _resolve_type(t_raw) if t_raw else None
    if t_raw and t_type is None:
        err(
            0,
            f"Invalid Type {t_raw!r}; use round robin, single elimination, or double elimination",
        )

    fmt = (headers.get("battle_format") or "").strip()
    if not fmt:
        err(0, "Battle Format is required")

    bo_raw = headers.get("best_of", "").strip()
    if not bo_raw:
        err(0, "Best Of is required")
    best_of = _parse_best_of(bo_raw) if bo_raw else None
    if best_of is None and bo_raw:
        err(0, f"Invalid Best Of {bo_raw!r}; use Bo1, Bo3, 3, etc.")
    if best_of is not None and (best_of < 1 or best_of % 2 == 0):
        err(0, "Best Of must be a positive odd number (1, 3, 5, …)")

    single_elim_bracket: str | None = None
    seb_raw = (headers.get("single_elim_bracket") or "").strip()
    if seb_raw:
        single_elim_bracket = _resolve_bracket(seb_raw)
        if single_elim_bracket is None:
            err(
                0,
                f"Invalid Single Elim Bracket {seb_raw!r}; use compact or power_of_two",
            )
    elif t_type in ("single_elimination", "double_elimination"):
        single_elim_bracket = "compact"

    if valid_battle_formats is not None and fmt and fmt not in valid_battle_formats:
        warnings.append(
            f"Battle format {fmt!r} is not in the built-in list; it will still be used if valid on the server."
        )

    entries: list[dict[str, Any]] = []
    for line_no, t in participant_lines:
        parts = _split_participant_line(t)
        if len(parts) < 3:
            err(
                line_no,
                "Each participant needs provider, model, and persona "
                "(comma- or | -separated).",
            )
            continue

        provider, model, persona_slug = parts[0], parts[1], parts[2]
        prov_l = provider.lower().strip()
        if prov_l not in ("anthropic", "deepseek", "openrouter"):
            err(
                line_no,
                f"Unknown provider {provider!r}; use anthropic, deepseek, or openrouter",
            )
            continue

        ps = persona_slug.strip()
        if valid_persona_slugs is not None and ps not in valid_persona_slugs:
            err(
                line_no,
                f"Unknown persona slug {ps!r}; use a slug from /manager/personas.",
            )
            continue

        label = f"line {line_no} ({provider}/{model})"
        try:
            validate_provider_model(prov_l, model, field_label=label)
        except ValueError as exc:
            err(line_no, str(exc))
            continue

        seed: int | None = None
        team_id: int | None = None

        if battle_format_rules.uses_server_assigned_teams(fmt):
            if len(parts) > 5:
                err(
                    line_no,
                    "Too many fields for *randombattle formats "
                    "(3 columns, 4 with seed, or 5 with seed + ignored column).",
                )
                continue
            if len(parts) == 5:
                try:
                    seed = int(parts[3])
                except ValueError:
                    err(line_no, f"Seed must be an integer, got {parts[3]!r}")
                    continue
                if seed < 1:
                    err(line_no, "Seed must be >= 1")
                    continue
                try:
                    int(parts[4])
                except ValueError:
                    err(
                        line_no,
                        f"*randombattle: optional 5th field must be an integer "
                        f"(ignored, not stored), got {parts[4]!r}",
                    )
                    continue
                warnings.append(
                    f"Line {line_no}: fifth column ignored "
                    f"(*randombattle uses server-assigned teams)."
                )
            elif len(parts) == 4:
                try:
                    seed = int(parts[3])
                except ValueError:
                    err(line_no, f"Seed must be an integer, got {parts[3]!r}")
                    continue
                if seed < 1:
                    err(line_no, "Seed must be >= 1")
                    continue
            row = {
                "provider": prov_l,
                "model": model.strip(),
                "persona_slug": ps,
            }
            if seed is not None:
                row["seed"] = seed
            entries.append(row)
            continue

        if len(parts) == 3:
            err(
                line_no,
                "Custom-team formats require a team preset id as the last field: "
                "4 columns (provider, model, persona, team_id), "
                "or 5 with seed before team_id.",
            )
            continue
        if len(parts) == 4:
            try:
                team_id = int(parts[3])
            except ValueError:
                err(line_no, f"team_id must be an integer, got {parts[3]!r}")
                continue
            if team_id < 1:
                err(line_no, "team_id must be >= 1")
                continue
        elif len(parts) == 5:
            try:
                seed = int(parts[3])
            except ValueError:
                err(line_no, f"Seed must be an integer, got {parts[3]!r}")
                continue
            if seed < 1:
                err(line_no, "Seed must be >= 1")
                continue
            try:
                team_id = int(parts[4])
            except ValueError:
                err(line_no, f"team_id must be an integer, got {parts[4]!r}")
                continue
            if team_id < 1:
                err(line_no, "team_id must be >= 1")
                continue
        else:
            err(
                line_no,
                "Too many fields (custom formats use 4 or 5 columns).",
            )
            continue

        row = {
            "provider": prov_l,
            "model": model.strip(),
            "persona_slug": ps,
            "team_id": team_id,
        }
        if seed is not None:
            row["seed"] = seed
        entries.append(row)

    if len(entries) < 2:
        errors.append(
            {
                "line": 0,
                "message": "At least two valid participant lines are required after 'Participants:'",
            }
        )
    else:
        seeded = sum(1 for e in entries if "seed" in e)
        if 0 < seeded < len(entries):
            errors.append(
                {
                    "line": 0,
                    "message": "Use either a seed on every participant line or omit seeds on all lines",
                }
            )

    if errors:
        return None, errors, warnings

    assert t_type is not None and best_of is not None

    if not any("seed" in e for e in entries):
        for i, e in enumerate(entries):
            e["seed"] = i + 1

    out: dict[str, Any] = {
        "name": name,
        "type": t_type,
        "battle_format": fmt,
        "best_of": best_of,
        "entries": entries,
    }
    if t_type in ("single_elimination", "double_elimination") and single_elim_bracket:
        out["single_elim_bracket"] = single_elim_bracket

    return out, [], warnings
