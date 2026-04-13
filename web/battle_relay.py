"""
In-memory relay between the HumanPlayer (agents container) and the battle
control page (browser).  The HumanPlayer pushes battle state here; the
browser reads state via SSE and submits actions via POST.

Mounted as an APIRouter on the main FastAPI app.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

_log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/battle")

# ---------------------------------------------------------------------------
# Relay state
# ---------------------------------------------------------------------------


@dataclass
class BattleRelay:
    match_id: int
    state: dict | None = None
    pending_action: dict | None = None
    # One queue per connected SSE subscriber (browser tab).  push_state
    # puts the new state in every queue; each SSE generator pulls from its own.
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_state_at: float | None = None
    finished: bool = False


_relays: dict[int, BattleRelay] = {}
_STALE_RELAY_SECONDS = 600  # 10 minutes

# Sentinel for "match ended" — pushed to queues when the relay is cleaned up.
_END_SENTINEL = object()


def _get_or_create_relay(match_id: int) -> BattleRelay:
    if match_id not in _relays:
        _relays[match_id] = BattleRelay(match_id=match_id)
    return _relays[match_id]


def _get_relay(match_id: int) -> BattleRelay | None:
    return _relays.get(match_id)


def _broadcast_to_subscribers(relay: BattleRelay, payload) -> None:
    """Put payload on every subscriber's queue; drop if the queue is full."""
    for q in list(relay.subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            # Subscriber isn't draining fast enough — drop the oldest
            try:
                q.get_nowait()
                q.put_nowait(payload)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Endpoints called by the HumanPlayer (agents container)
# ---------------------------------------------------------------------------


@router.post("/{match_id}/state", response_class=JSONResponse)
async def push_state(match_id: int, request: Request):
    """HumanPlayer pushes battle state each turn."""
    body = await request.json()
    relay = _get_or_create_relay(match_id)
    relay.state = body
    relay.pending_action = None  # clear stale action from previous turn
    relay.last_state_at = time.time()
    relay.finished = bool(body.get("finished", False))
    # Fan out to all connected SSE subscribers
    _broadcast_to_subscribers(relay, body)
    return {"ok": True}


@router.get("/{match_id}/action", response_class=JSONResponse)
async def poll_action(match_id: int):
    """HumanPlayer polls for the human's submitted action."""
    relay = _get_relay(match_id)
    if relay is None or relay.pending_action is None:
        raise HTTPException(404, "No pending action")
    action = relay.pending_action
    relay.pending_action = None  # consumed
    return action


@router.delete("/{match_id}/relay", response_class=JSONResponse)
async def cleanup_relay(match_id: int):
    """HumanPlayer cleans up after the match ends."""
    relay = _relays.pop(match_id, None)
    if relay:
        relay.finished = True
        _broadcast_to_subscribers(relay, _END_SENTINEL)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Endpoints called by the browser (battle control page)
# ---------------------------------------------------------------------------


@router.get("/{match_id}/state", response_class=JSONResponse)
async def get_state(match_id: int):
    """Browser fetches the latest battle state."""
    relay = _get_relay(match_id)
    if relay is None or relay.state is None:
        raise HTTPException(404, "No battle state available yet")
    return relay.state


@router.post("/{match_id}/action", response_class=JSONResponse)
async def submit_action(match_id: int, request: Request):
    """Browser submits the human's move/switch choice."""
    relay = _get_relay(match_id)
    if relay is None or relay.state is None:
        raise HTTPException(409, "No pending battle state — nothing to act on")
    body = await request.json()
    action_type = (body.get("action_type") or "").strip().lower()
    index = body.get("index")
    if action_type not in ("move", "switch"):
        raise HTTPException(400, "action_type must be 'move' or 'switch'")
    if index is None:
        raise HTTPException(400, "index is required")
    try:
        index = int(index)
    except (TypeError, ValueError):
        raise HTTPException(400, "index must be an integer")
    if index < 1:
        raise HTTPException(400, "index must be >= 1")
    # Validate against current state
    state = relay.state
    if action_type == "move":
        n = len(state.get("available_moves", []))
        if index > n:
            raise HTTPException(400, f"move index {index} out of range (max {n})")
    else:
        n = len(state.get("available_switches", []))
        if index > n:
            raise HTTPException(400, f"switch index {index} out of range (max {n})")
    relay.pending_action = {"action_type": action_type, "index": index}
    return {"ok": True}


@router.get("/{match_id}/stream")
async def state_stream(match_id: int, request: Request):
    """SSE stream — pushes battle state updates to the browser."""

    async def event_generator():
        relay = _get_or_create_relay(match_id)
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        relay.subscribers.append(queue)
        try:
            # Send current state immediately if available
            if relay.state is not None:
                yield f"data: {json.dumps(relay.state)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                # Wait for next broadcast; send keepalive on timeout.
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if payload is _END_SENTINEL:
                    yield 'event: match_end\ndata: {"finished": true}\n\n'
                    break
                yield f"data: {json.dumps(payload)}\n\n"
                if isinstance(payload, dict) and payload.get("finished"):
                    yield 'event: match_end\ndata: {"finished": true}\n\n'
                    break
        finally:
            try:
                relay.subscribers.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Stale relay cleanup
# ---------------------------------------------------------------------------


async def cleanup_stale_relays() -> None:
    """Remove relays that haven't had a state update in a while."""
    now = time.time()
    stale = [
        mid
        for mid, r in _relays.items()
        if r.last_state_at is not None
        and (now - r.last_state_at) > _STALE_RELAY_SECONDS
    ]
    for mid in stale:
        _relays.pop(mid, None)
    if stale:
        _log.info("Cleaned up %d stale battle relays", len(stale))
