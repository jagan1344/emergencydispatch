"""WebSocket connection manager. Thread-safe broadcast: any thread may call broadcast_threadsafe()."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket

log = logging.getLogger("app.ws")


class ConnectionManager:
    def __init__(self):
        self.connections: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue | None = None
        self.sent = 0

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue = asyncio.Queue(maxsize=10_000)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.connections.discard(ws)

    def broadcast_threadsafe(self, event_type: str, data: dict) -> None:
        if self.loop is None or self.queue is None or self.loop.is_closed():
            return
        msg = json.dumps({"type": event_type, "data": data}, default=str)

        def _put():
            try:
                self.queue.put_nowait(msg)
            except asyncio.QueueFull:  # drop under extreme load rather than block producers
                log.warning("ws queue full")

        self.loop.call_soon_threadsafe(_put)

    async def sender(self) -> None:
        """Background task draining the queue to all clients."""
        assert self.queue is not None
        while True:
            msg = await self.queue.get()
            dead = []
            for ws in list(self.connections):
                try:
                    await ws.send_text(msg)
                    self.sent += 1
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.disconnect(ws)
