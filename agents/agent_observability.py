"""Structured per-turn agent events (JSONL) for parse failures and LLM errors."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from log_print import log_print

_lock = threading.Lock()
_STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
_AGENT_EVENTS_PATH = Path(
    os.getenv("AGENT_EVENTS_FILE", str(_STATE_DIR / "agent_events.jsonl"))
)
try:
    _MAX_LINES = max(50, int(os.getenv("AGENT_EVENTS_MAX_LINES") or "2000"))
except ValueError:
    _MAX_LINES = 2000


def append_agent_event(record: dict[str, Any]) -> None:
    """Append one JSON object per line; trim the file when it grows past ``AGENT_EVENTS_MAX_LINES``."""
    rec = dict(record)
    rec.setdefault("ts", time.time())
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with _lock:
        try:
            _AGENT_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_AGENT_EVENTS_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            log_print(f"[obs] agent_events append failed: {e}", flush=True)
            return
        _trim_jsonl_if_needed()


def _trim_jsonl_if_needed() -> None:
    try:
        raw = _AGENT_EVENTS_PATH.read_text(encoding="utf-8")
    except OSError:
        return
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if len(lines) <= _MAX_LINES:
        return
    keep_n = max(_MAX_LINES // 2, 1)
    tail = lines[-keep_n:]
    try:
        _AGENT_EVENTS_PATH.write_text("\n".join(tail) + "\n", encoding="utf-8")
    except OSError:
        pass
