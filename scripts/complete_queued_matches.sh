#!/usr/bin/env bash
#
# Mark queued manager matches completed via POST /api/manager/matches/{id}/complete
# (for testing scoreboard / overlay without running real battles).
#
# Usage:
#   WEB_URL=http://localhost:8080 ./scripts/complete_queued_matches.sh
#
# Common failures:
#   - "No queued matches": agents dequeued everything → `docker compose stop agents`
#     then create matches, run this script, then `docker compose start agents`.
#   - Wrong URL: set WEB_URL to where the web container listens (see docker-compose port).
#
set -euo pipefail

WEB_URL="${WEB_URL:-${OVERLAY_URL:-http://localhost:8080}}"
WEB_URL="${WEB_URL%/}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,30p' "$0"
  exit 0
fi

# GET /api/manager/matches returns a JSON *array* of rows (not { "matches": [...] })
JSON="$(curl -sS -w "\n%{http_code}" "${WEB_URL}/api/manager/matches?status=queued&limit=200")" || {
  echo "curl failed (is WEB_URL=${WEB_URL} reachable?)" >&2
  exit 1
}

HTTP="${JSON##*$'\n'}"
BODY="${JSON%$'\n'*}"
if [[ "$HTTP" != "200" ]]; then
  echo "GET /api/manager/matches?status=queued failed HTTP ${HTTP}" >&2
  echo "$BODY" >&2
  exit 1
fi

export WEB_URL
export MATCH_LIST_JSON="$BODY"
export COMPLETE_BODY_JSON="${COMPLETE_BODY_JSON:-{\"winner\":\"P1\",\"loser\":\"P2\",\"winner_side\":\"p1\",\"duration\":1.0}}"

python3 <<'PY'
import json, os, sys, urllib.error, urllib.request

base = os.environ["WEB_URL"].rstrip("/")
raw = os.environ["MATCH_LIST_JSON"]
try:
    rows = json.loads(raw)
except json.JSONDecodeError:
    print("Not JSON from list endpoint (first 400 chars):", raw[:400], file=sys.stderr)
    sys.exit(1)

if not isinstance(rows, list):
    print(
        "Expected a JSON array from GET /api/manager/matches; got",
        type(rows).__name__,
        file=sys.stderr,
    )
    sys.exit(1)

ids = [int(r["id"]) for r in rows if r.get("status") == "queued"]
if not ids:
    print("No queued matches found.", file=sys.stderr)
    print("", file=sys.stderr)
    print("Typical cause: the agents worker already took them (status is running/completed).", file=sys.stderr)
    print("Fix: docker compose stop agents", file=sys.stderr)
    print("     scripts/create_match.sh ... --count 10", file=sys.stderr)
    print("     ./scripts/complete_queued_matches.sh", file=sys.stderr)
    print("     docker compose start agents", file=sys.stderr)
    sys.exit(1)

payload = os.environ.get("COMPLETE_BODY_JSON", "").encode()
if not payload:
    payload = b'{"winner":"P1","loser":"P2","winner_side":"p1","duration":1.0}'

n_ok = 0
for mid in ids:
    url = f"{base}/api/manager/matches/{mid}/complete"
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
            print(f"OK match {mid} HTTP {resp.status}")
            n_ok += 1
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"FAIL match {mid} HTTP {e.code}: {detail[:500]}", file=sys.stderr)
        sys.exit(1)

print(f"Completed {n_ok} match(es).")
PY
