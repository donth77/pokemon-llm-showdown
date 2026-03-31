"""
Server-Sent Events fanout for full scoreboard snapshots (broadcast hub).

Uses a monotonic seq, debounced publish, and mtime polling on current_battle.json
so bind-mounted volumes behave reliably without extra dependencies.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import Request
from starlette.responses import StreamingResponse

from web_debug import web_debug_enabled

_log = logging.getLogger("uvicorn.error")

_build: Callable[[], Awaitable[dict]] | None = None
_subscribers: set[asyncio.Queue[str]] = set()
_seq = 0
_last_snap: tuple[int, dict] | None = None
_seq_lock = asyncio.Lock()
_snap_lock = asyncio.Lock()
_debounce_lock = asyncio.Lock()
_publish_serial = asyncio.Lock()
_publish_debounce: asyncio.Task[None] | None = None

DEBOUNCE_S = 0.04
SSE_QUEUE_MAX = 4
KEEPALIVE_S = 15.0
STATE_POLL_S = 0.25


def set_scoreboard_payload_builder(fn: Callable[[], Awaitable[dict]]) -> None:
    global _build
    _build = fn


async def _next_seq() -> int:
    global _seq
    async with _seq_lock:
        _seq += 1
        return _seq


def _queue_put_limited(q: asyncio.Queue[str], line: str) -> None:
    try:
        q.put_nowait(line)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            q.put_nowait(line)
        except asyncio.QueueFull:
            pass


async def _emit_line_to_subscribers(line: str) -> None:
    for q in list(_subscribers):
        try:
            _queue_put_limited(q, line)
        except Exception:
            _subscribers.discard(q)


async def _run_publish() -> None:
    """One publish at a time so seq order matches build completion order.

    Without this, concurrent builds (state-file poll + debounced HTTP + follow-up)
    can finish out of order while ``seq`` is assigned after ``await _build()``,
    so clients accept a stale payload with a higher seq and stop updating.
    """
    global _last_snap
    if _build is None:
        return
    async with _publish_serial:
        try:
            payload = await _build()
        except Exception:
            return
        seq = await _next_seq()
        _last_snap = (seq, payload)
        msg = json.dumps({"seq": seq, "payload": payload}, separators=(",", ":"))
        line = f"data: {msg}\n\n"
        await _emit_line_to_subscribers(line)
        if web_debug_enabled():
            _log.info(
                "scoreboard_sse published seq=%s subscriber_queues=%s",
                seq,
                len(_subscribers),
            )


async def _sleep_and_publish() -> None:
    try:
        await asyncio.sleep(DEBOUNCE_S)
        await _run_publish()
    except asyncio.CancelledError:
        raise


async def _publish_after_ms(delay: float) -> None:
    """Used to re-read SQLite after idle hits the state file (written before POST /complete)."""
    try:
        await asyncio.sleep(delay)
        await _run_publish()
    except asyncio.CancelledError:
        raise


def _state_file_battle_status(path: Path) -> str | None:
    try:
        raw = path.read_text(encoding="utf-8")
        st = json.loads(raw)
        if isinstance(st, dict):
            v = st.get("status")
            return str(v) if v is not None else None
    except Exception:
        pass
    return None


async def request_scoreboard_publish() -> None:
    """Coalesce rapid triggers into a single debounced snapshot publish."""
    global _publish_debounce
    async with _debounce_lock:
        if _publish_debounce is not None and not _publish_debounce.done():
            _publish_debounce.cancel()
            try:
                await _publish_debounce
            except asyncio.CancelledError:
                pass
        _publish_debounce = asyncio.create_task(_sleep_and_publish())


async def _snapshot_for_new_client() -> tuple[int, dict]:
    """First line sent on each SSE connection; reuses last published snap if any."""
    global _last_snap
    if _last_snap is not None:
        return _last_snap
    async with _snap_lock:
        if _last_snap is not None:
            return _last_snap
        if _build is None:
            return (0, {})
        try:
            payload = await _build()
        except Exception:
            return (0, {})
        seq = await _next_seq()
        _last_snap = (seq, payload)
        return _last_snap


async def scoreboard_sse(request: Request) -> StreamingResponse:
    async def gen():
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=SSE_QUEUE_MAX)
        _subscribers.add(queue)
        try:
            seq, payload = await _snapshot_for_new_client()
            init = json.dumps({"seq": seq, "payload": payload}, separators=(",", ":"))
            yield f"data: {init}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
                    yield line
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _subscribers.discard(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _state_file_poll_loop(state_path: Path) -> None:
    last_key: tuple[int | None, int | None] | None = None
    while True:
        await asyncio.sleep(STATE_POLL_S)
        mtime_ns: int | None
        size: int | None
        try:
            if state_path.exists():
                st = state_path.stat()
                mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
                size = int(st.st_size)
            else:
                mtime_ns, size = None, None
        except OSError:
            mtime_ns, size = None, None
        key = (mtime_ns, size)
        if key != last_key:
            last_key = key
            # Publish immediately on each distinct (mtime, size). Debounced
            # request_scoreboard_publish can coalesce a rapid live→idle into one
            # snapshot that only ever shows idle, so the battle iframe never
            # enters the outro path (wasLive stays false).
            await _run_publish()
            # Agents write status=idle before POST .../complete updates SQLite, so the
            # snapshot above often lacks the new last_match / total_matches. Without a
            # follow-up publish the victory modal waits until the next file change
            # (e.g. series merge or the following match's starting), which feels like a
            # long hang. Refresh once after typical complete latency.
            if _state_file_battle_status(state_path) == "idle":
                asyncio.create_task(_publish_after_ms(0.2))


def start_scoreboard_state_poll(state_path: Path) -> asyncio.Task[None]:
    return asyncio.create_task(
        _state_file_poll_loop(state_path), name="scoreboard_state_poll"
    )
