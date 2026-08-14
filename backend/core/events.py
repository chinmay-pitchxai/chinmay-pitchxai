"""Async pub/sub event bus — feeds SSE to dashboard clients."""

from __future__ import annotations

import asyncio
import json


class EventBus:
    """Simple pub/sub bus. One queue per subscriber (SSE connection)."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[str]] = []

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def publish(self, event_type: str, **data: object) -> None:
        payload = json.dumps({"type": event_type, **data})
        dead: list[asyncio.Queue[str]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)


_bus = EventBus()


def get_event_bus() -> EventBus:
    return _bus
