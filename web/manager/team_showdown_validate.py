"""Run Pokémon Showdown's validate-team CLI against pasted team export text."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from . import battle_format_rules


class TeamValidationConfigError(OSError):
    """Showdown install path is missing or unusable."""


def validate_team_showdown_sync(battle_format: str, showdown_text: str) -> list[str]:
    """Return a list of error lines from Showdown; empty if the team is legal."""
    fmt = (battle_format or "").strip()
    if not fmt:
        return ["Battle format is required."]
    if not (showdown_text or "").strip():
        return ["Showdown team export is required."]
    if battle_format_rules.uses_server_assigned_teams(fmt):
        return []

    if len(fmt) > 64:
        return ["Battle format id is too long."]

    home = Path(os.environ.get("SHOWDOWN_HOME", "/opt/pokemon-showdown")).resolve()
    cli = home / "pokemon-showdown"
    if not cli.is_file():
        raise TeamValidationConfigError(
            f"Team validation is not available (missing Showdown CLI at {cli}). "
            "Rebuild the web image or set SHOWDOWN_HOME."
        )

    try:
        proc = subprocess.run(
            ["node", str(cli), "validate-team", fmt],
            input=(showdown_text or "").encode("utf-8"),
            cwd=str(home),
            capture_output=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TeamValidationConfigError(
            "Node.js was not found; cannot run Showdown team validation."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Showdown team validation timed out") from exc
    if proc.returncode == 0:
        return []
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    combined = stderr or stdout
    lines = [ln.rstrip() for ln in combined.splitlines() if ln.strip()]
    if not lines and combined:
        lines = [combined]
    if not lines:
        lines = [f"Validation failed (exit code {proc.returncode})."]
    return lines


async def validate_team_showdown(battle_format: str, showdown_text: str) -> list[str]:
    return await asyncio.to_thread(
        validate_team_showdown_sync, battle_format, showdown_text
    )
