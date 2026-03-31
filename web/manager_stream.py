"""
Server-Sent Events for manager UI: queue / tournament / series refresh hints.

Payload shape: ``{"seq": int, "queue": bool, "tournament_ids": int[], "series_ids": int[]}``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable

from fastapi import Request
from starlette.responses import StreamingResponse

_subscribers: set[asyncio.Queue[str]] = set()
_seq = 0
_seq_lock = asyncio.Lock()
_debounce_lock = asyncio.Lock()
_publish_debounce: asyncio.Task[None] | None = None
_pending_queue = False
_pending_tournament_ids: set[int] = set()
_pending_series_ids: set[int] = set()

DEBOUNCE_S = 0.04
SSE_QUEUE_MAX = 4
KEEPALIVE_S = 15.0


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
    global _pending_queue, _pending_tournament_ids, _pending_series_ids
    if not _pending_queue and not _pending_tournament_ids and not _pending_series_ids:
        return
    use_queue = _pending_queue
    use_tids = sorted(_pending_tournament_ids)
    use_sids = sorted(_pending_series_ids)
    _pending_queue = False
    _pending_tournament_ids = set()
    _pending_series_ids = set()
    seq = await _next_seq()
    body = {
        "seq": seq,
        "queue": use_queue,
        "tournament_ids": use_tids,
        "series_ids": use_sids,
    }
    line = f"data: {json.dumps(body, separators=(',', ':'))}\n\n"
    await _emit_line_to_subscribers(line)


async def _sleep_and_publish() -> None:
    try:
        await asyncio.sleep(DEBOUNCE_S)
        await _run_publish()
    except asyncio.CancelledError:
        raise


async def notify_manager_events(
    *,
    queue: bool = False,
    tournament_ids: Iterable[int] | None = None,
    series_ids: Iterable[int] | None = None,
) -> None:
    """Coalesce rapid DB-side changes into one debounced SSE message."""
    global _publish_debounce, _pending_queue, _pending_tournament_ids
    global _pending_series_ids
    if tournament_ids:
        for tid in tournament_ids:
            try:
                _pending_tournament_ids.add(int(tid))
            except (TypeError, ValueError):
                pass
    if series_ids:
        for sid in series_ids:
            try:
                _pending_series_ids.add(int(sid))
            except (TypeError, ValueError):
                pass
    if queue:
        _pending_queue = True
    if not _pending_queue and not _pending_tournament_ids and not _pending_series_ids:
        return
    async with _debounce_lock:
        if _publish_debounce is not None and not _publish_debounce.done():
            _publish_debounce.cancel()
            try:
                await _publish_debounce
            except asyncio.CancelledError:
                pass
        _publish_debounce = asyncio.create_task(_sleep_and_publish())


async def manager_sse(request: Request) -> StreamingResponse:
    async def gen():
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=SSE_QUEUE_MAX)
        _subscribers.add(queue)
        try:
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
