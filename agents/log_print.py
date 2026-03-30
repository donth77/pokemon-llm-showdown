"""Line-oriented stdout/stderr logging with local ISO-8601 timestamps."""

from __future__ import annotations

import sys
from datetime import datetime


def _ts() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def log_print(
    *args: object,
    sep: str = " ",
    end: str = "\n",
    file=None,
    flush: bool = True,
) -> None:
    """Like built-in print, but each output line is prefixed with a timestamp."""
    if file is None:
        file = sys.stdout
    text = sep.join(str(a) for a in args)
    stamp = _ts()
    lines = text.split("\n")
    n = len(lines)
    for i, line in enumerate(lines):
        line_end = end if i == n - 1 else "\n"
        print(f"{stamp} {line}", end=line_end, file=file, flush=flush and i == n - 1)
