"""
Read/update the host project `.env` when mounted into the web container.

MANAGER_HOST_ENV_FILE must point at a regular file (e.g. /app/host.env).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


def configured_host_env_path() -> Path | None:
    raw = (os.getenv("MANAGER_HOST_ENV_FILE") or "").strip()
    if not raw:
        return None
    return Path(raw)


def host_env_status() -> dict:
    path = configured_host_env_path()
    if path is None:
        return {"configured": False, "exists": False, "writable": False, "path": ""}
    exists = path.is_file()
    writable = exists and os.access(path, os.W_OK)
    return {"configured": True, "exists": exists, "writable": writable, "path": str(path)}


def parse_env_file(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].strip()
        if "=" not in s:
            continue
        k, _, v = s.partition("=")
        key = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == '"' and v.endswith('"'):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                v = v[1:-1]
        elif len(v) >= 2 and v[0] == "'" and v.endswith("'"):
            v = v[1:-1].replace("\\'", "'")
        out[key] = v
    return out


def load_host_env_map(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return parse_env_file(path.read_text(encoding="utf-8"))


def _format_env_value(val: str) -> str:
    if val == "":
        return ""
    if re.fullmatch(r"[A-Za-z0-9_.,:@%+/-]+", val):
        return val
    return json.dumps(val)


def update_env_keys(path: Path, updates: dict[str, str]) -> None:
    if not path.is_file():
        path.write_text("", encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    replaced: set[str] = set()
    out: list[str] = []
    for line in lines:
        raw = line
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            out.append(raw)
            continue
        if s.startswith("export "):
            s = s[7:].strip()
        k, _, _ = s.partition("=")
        key = k.strip()
        if key in updates:
            formatted = _format_env_value(updates[key])
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}{key}={formatted}")
            replaced.add(key)
        else:
            out.append(raw)

    for key, val in updates.items():
        if key not in replaced:
            formatted = _format_env_value(val)
            out.append(f"{key}={formatted}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
