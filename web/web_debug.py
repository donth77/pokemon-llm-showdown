"""Operators: ``WEB_DEBUG=true`` enables extra operational logs from the web process."""

from __future__ import annotations

from env_bool import parse_env_bool


def web_debug_enabled() -> bool:
    return parse_env_bool("WEB_DEBUG", default=False)
