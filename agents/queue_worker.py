"""
Queue worker — polls the manager API for pending matches and executes them.

Polls the manager API for queued matches (default agents container entrypoint).
entry point.  All match configuration comes from the manager API.
"""

import asyncio
import json
import os
from pathlib import Path

import aiohttp

from match_runner import (
    load_persona,
    run_single_match,
    wait_for_showdown,
    write_current_battle_state,
    write_json_atomic,
)

WEB_HOST = os.getenv("WEB_HOST") or os.getenv("OVERLAY_HOST", "web")
WEB_PORT = int(os.getenv("WEB_PORT") or os.getenv("OVERLAY_PORT", "8080"))
QUEUE_POLL_INTERVAL = float(os.getenv("QUEUE_POLL_INTERVAL") or "5")
DELAY_BETWEEN_MATCHES = float(os.getenv("DELAY_BETWEEN_MATCHES") or "15")
TOURNAMENT_INTRO_SECONDS = float(os.getenv("TOURNAMENT_INTRO_SECONDS") or "0")
TOURNAMENT_INTRO_DELAY_SECONDS = float(
    os.getenv("TOURNAMENT_INTRO_DELAY_SECONDS") or "0"
)

API_BASE = f"http://{WEB_HOST}:{WEB_PORT}/api/manager"
STATE_DIR = Path(os.getenv("STATE_DIR", "/state"))
CURRENT_BATTLE_FILE = STATE_DIR / "current_battle.json"


def _optional_showdown_account(match: dict, key: str) -> str | None:
    v = match.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


async def _fetch_series_snapshot(
    session: aiohttp.ClientSession, series_id: int
) -> dict | None:
    try:
        async with session.get(
            f"{API_BASE}/series/{series_id}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            s = await resp.json()
            return {
                "series_id": s["id"],
                "best_of": s["best_of"],
                "player1_wins": s["player1_wins"],
                "player2_wins": s["player2_wins"],
            }
    except Exception as e:
        print(f"[queue] Could not load series #{series_id}: {e}", flush=True)
        return None


def _merge_series_snapshot_into_current_battle(snapshot: dict) -> None:
    try:
        if not CURRENT_BATTLE_FILE.is_file():
            return
        raw = CURRENT_BATTLE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return
        data["series_id"] = snapshot["series_id"]
        data["series_best_of"] = snapshot["best_of"]
        data["series_player1_wins"] = snapshot["player1_wins"]
        data["series_player2_wins"] = snapshot["player2_wins"]
        write_json_atomic(CURRENT_BATTLE_FILE, data)
    except Exception as e:
        print(f"[queue] Could not merge series into current_battle: {e}", flush=True)


def _tourney_context_from_match(match: dict) -> dict | None:
    """Fields for current_battle.json / formatTournamentMatchContextLine (flat keys)."""
    if not match.get("tournament_id"):
        return None
    out: dict = {"tournament_id": int(match["tournament_id"])}
    for key in (
        "tournament_name",
        "tournament_type",
        "series_bracket",
        "series_round_number",
        "series_match_position",
        "tournament_max_winners_round",
        "game_number",
    ):
        val = match.get(key)
        if val is not None:
            out[key] = val
    return out


async def _tournament_has_completed_match(
    session: aiohttp.ClientSession, tournament_id: int
) -> bool:
    try:
        async with session.get(
            f"{API_BASE}/matches",
            params={
                "status": "completed",
                "tournament_id": str(tournament_id),
                "limit": "1",
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return True
            rows = await resp.json()
            return isinstance(rows, list) and len(rows) > 0
    except Exception:
        return True


async def _fetch_tournament_json(
    session: aiohttp.ClientSession, tournament_id: int
) -> dict | None:
    try:
        async with session.get(
            f"{API_BASE}/tournaments/{tournament_id}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            body = await resp.json()
            return body if isinstance(body, dict) else None
    except Exception:
        return None


async def _fetch_next_match(session: aiohttp.ClientSession) -> dict | None:
    """Get the next queued match from the manager API."""
    try:
        async with session.get(
            f"{API_BASE}/queue/next",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 404:
                return None
            if resp.status == 200:
                return await resp.json()
            body = (await resp.text()).strip()
            if body:
                print(
                    f"[queue] Unexpected status {resp.status} from queue/next — body: {body[:800]}",
                    flush=True,
                )
            else:
                print(f"[queue] Unexpected status {resp.status} from queue/next", flush=True)
            return None
    except Exception as e:
        print(f"[queue] Error fetching next match: {e}", flush=True)
        return None


async def _report_complete(
    session: aiohttp.ClientSession,
    match_id: int,
    payload: dict,
) -> None:
    """Report match completion to the manager API; refresh series counts in current_battle."""
    try:
        async with session.post(
            f"{API_BASE}/matches/{match_id}/complete",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                print(f"[queue] Reported match #{match_id} complete", flush=True)
                try:
                    body = await resp.json()
                    snap = body.get("series_snapshot") if isinstance(body, dict) else None
                    if isinstance(snap, dict):
                        _merge_series_snapshot_into_current_battle(snap)
                except Exception:
                    pass
            else:
                text = await resp.text()
                print(f"[queue] Failed to report #{match_id}: {resp.status} {text}", flush=True)
    except Exception as e:
        print(f"[queue] Error reporting match #{match_id}: {e}", flush=True)


async def _report_error(
    session: aiohttp.ClientSession,
    match_id: int,
    error: str,
) -> None:
    try:
        async with session.post(
            f"{API_BASE}/matches/{match_id}/error",
            json={"error": error},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                print(f"[queue] Reported match #{match_id} error", flush=True)
    except Exception as e:
        print(f"[queue] Error reporting error for #{match_id}: {e}", flush=True)


async def main() -> None:
    print("=" * 50, flush=True)
    print("Queue Worker starting", flush=True)
    print(f"Web / manager API: {API_BASE}", flush=True)
    print(f"Poll interval: {QUEUE_POLL_INTERVAL}s", flush=True)
    print("=" * 50, flush=True)

    await wait_for_showdown()

    async with aiohttp.ClientSession() as session:
        # Wait for the web service to be ready
        while True:
            try:
                async with session.get(
                    f"http://{WEB_HOST}:{WEB_PORT}/health",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        print("[queue] Web service is up!", flush=True)
                        break
            except Exception:
                pass
            print("[queue] Waiting for web service...", flush=True)
            await asyncio.sleep(2)

        while True:
            match = await _fetch_next_match(session)

            if not match:
                await asyncio.sleep(QUEUE_POLL_INTERVAL)
                continue

            match_id = match["id"]
            print(f"\n{'=' * 50}", flush=True)
            print(f"[queue] Running match #{match_id}", flush=True)
            print(
                f"  {match['player1_provider']}/{match['player1_model']} ({match['player1_persona']})"
                f" vs "
                f"{match['player2_provider']}/{match['player2_model']} ({match['player2_persona']})",
                flush=True,
            )
            print(f"  Format: {match['battle_format']}", flush=True)
            print(f"{'=' * 50}", flush=True)

            series_snapshot = None
            sid = match.get("series_id")
            if sid:
                series_snapshot = await _fetch_series_snapshot(session, int(sid))

            tourney_ctx = _tourney_context_from_match(match)
            tid = match.get("tournament_id")
            if (
                tid is not None
                and TOURNAMENT_INTRO_SECONDS > 0
                and not await _tournament_has_completed_match(session, int(tid))
            ):
                tj = await _fetch_tournament_json(session, int(tid))
                if tj:
                    try:
                        p1 = load_persona(match["player1_persona"])
                        p2 = load_persona(match["player2_persona"])
                    except Exception as e:
                        print(
                            f"[queue] Tournament intro skipped (persona load): {e}",
                            flush=True,
                        )
                        tj = None
                if tj:
                    roster = [
                        {
                            "persona_slug": str(e.get("persona_slug") or ""),
                            "seed": int(e.get("seed") or 0),
                        }
                        for e in (tj.get("entries") or [])
                    ]
                    t_bo: int | None = None
                    raw_bo = tj.get("best_of")
                    if raw_bo is not None:
                        try:
                            t_bo = int(raw_bo)
                        except (TypeError, ValueError):
                            t_bo = None
                    write_current_battle_state(
                        status="tournament_intro",
                        battle_tag=None,
                        battle_format=tj.get("battle_format") or match["battle_format"],
                        player1_name=p1.name,
                        player2_name=p2.name,
                        player1_model_id=match["player1_model"],
                        player2_model_id=match["player2_model"],
                        player1_persona=p1,
                        player2_persona=p2,
                        series_snapshot=series_snapshot,
                        tourney_context=tourney_ctx,
                        manager_match_id=int(match_id),
                        tournament_intro_roster=roster,
                        tournament_best_of=t_bo,
                    )
                    print(
                        f"[queue] Tournament intro hold {TOURNAMENT_INTRO_SECONDS}s "
                        f"(tournament #{tid})",
                        flush=True,
                    )
                    await asyncio.sleep(TOURNAMENT_INTRO_SECONDS)
                    if TOURNAMENT_INTRO_DELAY_SECONDS > 0:
                        write_current_battle_state(
                            status="intro_gap",
                            battle_tag=None,
                            battle_format=tj.get("battle_format")
                            or match["battle_format"],
                            player1_name=p1.name,
                            player2_name=p2.name,
                            player1_model_id=match["player1_model"],
                            player2_model_id=match["player2_model"],
                            player1_persona=p1,
                            player2_persona=p2,
                            series_snapshot=series_snapshot,
                            tourney_context=tourney_ctx,
                            manager_match_id=int(match_id),
                            tournament_best_of=t_bo,
                        )
                        print(
                            f"[queue] Tournament intro delay "
                            f"{TOURNAMENT_INTRO_DELAY_SECONDS}s (tournament #{tid})",
                            flush=True,
                        )
                        await asyncio.sleep(TOURNAMENT_INTRO_DELAY_SECONDS)

            result = await run_single_match(
                battle_format=match["battle_format"],
                player1_provider=match["player1_provider"],
                player1_model=match["player1_model"],
                player1_persona_slug=match["player1_persona"],
                player2_provider=match["player2_provider"],
                player2_model=match["player2_model"],
                player2_persona_slug=match["player2_persona"],
                player1_account_name=_optional_showdown_account(match, "player1_showdown_account"),
                player2_account_name=_optional_showdown_account(match, "player2_showdown_account"),
                series_snapshot=series_snapshot,
                tourney_context=tourney_ctx,
                manager_match_id=int(match_id),
            )

            if result.error:
                await _report_error(session, match_id, result.error)
            else:
                await _report_complete(
                    session,
                    match_id,
                    {
                        "winner": result.winner,
                        "loser": result.loser,
                        "winner_side": result.winner_side,
                        "duration": result.duration,
                        "replay_file": result.replay_file,
                        "log_file": result.log_file,
                        "battle_tag": result.battle_tag,
                    },
                )
                print(f"[queue] Match #{match_id}: {result.winner} wins!", flush=True)

            print(f"[queue] Next poll in {DELAY_BETWEEN_MATCHES}s ...", flush=True)
            await asyncio.sleep(DELAY_BETWEEN_MATCHES)


if __name__ == "__main__":
    asyncio.run(main())
