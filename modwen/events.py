"""Thread-safe event bus: background threads publish, WebSocket clients subscribe (asyncio queues)."""

from __future__ import annotations

import asyncio
import threading
from collections import deque


class EventBus:
    def __init__(self, history: int = 200):
        self._subs: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        self._lock = threading.Lock()
        self.recent: deque[dict] = deque(maxlen=history)

    def subscribe(self) -> asyncio.Queue:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subs.add((loop, q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = {(lp, qq) for lp, qq in self._subs if qq is not q}

    def publish(self, event: dict) -> None:
        self.recent.append(event)
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(self._put, q, event)
            except RuntimeError:  # loop closed
                self.unsubscribe(q)

    @staticmethod
    def _put(q: asyncio.Queue, event: dict) -> None:
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(event)
