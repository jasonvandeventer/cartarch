"""In-process pub/sub for companion-mode live game state (SSE fan-out).

Each game_id has a set of subscriber ``asyncio.Queue``s (one per open SSE
stream). ``publish`` drops the full state JSON on every subscriber; the stream
route forwards it. State is always the FULL blob (not deltas) so a reconnecting
client self-heals.

ponytail: single-replica only. Subscribers live in this process's memory, so a
publish reaches only streams connected to THIS pod. Cartarch runs 1 pod, so this
is sufficient; at >1 replica this needs an external bus (Redis pub/sub, Postgres
LISTEN/NOTIFY) — the upgrade path if the deployment ever scales out.

``publish`` is called from SYNC service code (apply_live_action /
start_live_game), which FastAPI runs in a threadpool for ``def`` routes. Putting
onto an asyncio.Queue is not thread-safe, so publish schedules the put on the
captured event loop via ``call_soon_threadsafe`` — correct whether the caller is
on the loop thread or a worker thread.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

_subscribers: dict[int, set[asyncio.Queue]] = {}
_loop: asyncio.AbstractEventLoop | None = None


def _remember_loop() -> None:
    """Capture the running event loop so cross-thread publishes can target it."""
    global _loop
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:  # no running loop (e.g. a purely sync unit test)
        pass


@asynccontextmanager
async def subscribe(game_id: int):
    """Register a subscriber queue for ``game_id`` for the life of the stream."""
    _remember_loop()
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(game_id, set()).add(queue)
    try:
        yield queue
    finally:
        subs = _subscribers.get(game_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                _subscribers.pop(game_id, None)


def publish(game_id: int, state_json: str) -> None:
    """Fan ``state_json`` out to every subscriber of ``game_id`` (best-effort)."""
    subs = _subscribers.get(game_id)
    if not subs:
        return
    loop = _loop
    for queue in list(subs):
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(queue.put_nowait, state_json)
        else:
            # No loop captured yet / not running (sync test path): put directly.
            try:
                queue.put_nowait(state_json)
            except asyncio.QueueFull:  # unbounded queue — unreachable, defensive
                pass


def subscriber_count(game_id: int) -> int:
    """Open-stream count for a game (used by tests)."""
    return len(_subscribers.get(game_id, ()))
