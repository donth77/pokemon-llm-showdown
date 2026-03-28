#!/usr/bin/env python3
"""
Extract item, ability, and move text descriptions from Showdown's data/text/ TS files.

Downloads the raw TypeScript source from GitHub and parses name + desc + shortDesc
into simple JSON files consumable by the Pokedex data layer.

Usage:
    python extract_showdown_data.py [--output-dir /app/data]
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

GITHUB_RAW = "https://raw.githubusercontent.com/smogon/pokemon-showdown/master/data/text"
FILES = {
    "items": f"{GITHUB_RAW}/items.ts",
    "abilities": f"{GITHUB_RAW}/abilities.ts",
    "moves": f"{GITHUB_RAW}/moves.ts",
}

_ENTRY_RE = re.compile(
    r"""
    ^[ \t]+                     # leading whitespace
    "?([a-z0-9]+)"?             # entry key (optionally quoted)
    \s*:\s*\{                   # opening brace
    """,
    re.VERBOSE | re.MULTILINE,
)

_FIELD_RE = re.compile(
    r"""
    ^[ \t]+
    (name|desc|shortDesc)
    \s*:\s*
    "((?:[^"\\]|\\.)*)"         # quoted value (handles escaped chars)
    """,
    re.VERBOSE | re.MULTILINE,
)


def _download(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "pokemon-llm-showdown/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _parse_ts_text(source: str) -> dict[str, dict[str, str]]:
    """Parse a Showdown data/text/*.ts file into {id: {name, desc, shortDesc}}."""
    result: dict[str, dict[str, str]] = {}
    current_key: str | None = None
    current_entry: dict[str, str] = {}
    depth = 0

    for line in source.split("\n"):
        stripped = line.strip()

        if depth == 1:
            entry_m = _ENTRY_RE.match(line)
            if entry_m:
                if current_key and current_entry:
                    result[current_key] = dict(current_entry)
                current_key = entry_m.group(1)
                current_entry = {}
                depth = 2
                continue

        if depth >= 2:
            field_m = _FIELD_RE.match(line)
            if field_m:
                field_name, field_val = field_m.group(1), field_m.group(2)
                field_val = field_val.replace('\\"', '"').replace("\\\\", "\\")
                current_entry[field_name] = field_val

            depth += stripped.count("{") - stripped.count("}")
            if depth <= 1:
                if current_key and current_entry:
                    result[current_key] = dict(current_entry)
                current_key = None
                current_entry = {}
                depth = 1
            continue

        if stripped.startswith("export const") or stripped == "{":
            depth = 1

    if current_key and current_entry:
        result[current_key] = dict(current_entry)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Showdown text data to JSON")
    parser.add_argument("--output-dir", default="/app/data", help="Output directory")
    args = parser.parse_args()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    for name, url in FILES.items():
        print(f"Downloading {name} from {url} ...", flush=True)
        try:
            source = _download(url)
        except Exception as e:
            print(f"  WARN: failed to download {name}: {e}", file=sys.stderr)
            continue

        data = _parse_ts_text(source)
        dest = out / f"{name}.json"
        dest.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        print(f"  Wrote {len(data)} entries to {dest}", flush=True)


if __name__ == "__main__":
    main()
