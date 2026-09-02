"""Tiny in-process async broadcaster for WebSocket fan-out.

Channel-keyed: subscribers only receive events published to the channel they
asked for. Publishers never block on a slow consumer — if a subscriber's queue
is full the oldest event is dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

_QUEUE_MAXSIZE = 1000


class Broadcaster:
    def __init__(self) -> None:
        self._channels: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, channel: str, event: Any) -> None:
        for queue in list(self._channels.get(channel, ())):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    @contextlib.asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        async with self._lock:
            self._channels.setdefault(channel, set()).add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                subs = self._channels.get(channel)
                if subs is not None:
                    subs.discard(queue)
                    if not subs:
                        self._channels.pop(channel, None)


# Process-wide singleton.
broadcaster = Broadcaster()

# Channel names.
CH_JOBS = "jobs"  # every job progress event


def job_channel(job_id: int) -> str:
    return f"job:{job_id}"


def server_console_channel(server_id: int) -> str:
    return f"server:{server_id}:console"
