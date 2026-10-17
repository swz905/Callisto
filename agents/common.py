"""Shared device-agent plumbing: a reconnecting WebSocket client to the hub.

Deliberately standalone (only needs `websockets`) so it can run on the phone under Termux.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Awaitable, Callable

Handler = Callable[[dict], Awaitable[None]]


def envelope(user_id: str, device_id: str, type_: str, source: str, payload: dict, ts: int | None = None) -> dict:
    return {"kind": "event", "event": {"event_id": str(uuid.uuid4()), "user_id": user_id, "device_id": device_id,
                                       "type": type_, "ts": ts or int(time.time() * 1000), "source": source,
                                       "payload": payload}}


def http_base(ws_url: str) -> str:
    """ws://host:8000 -> http://host:8000 (for uploads to the hub)."""
    return ws_url.replace("wss://", "https://").replace("ws://", "http://").rstrip("/")


class HubClient:
    def __init__(self, hub: str, device_id: str, token: str, on_message: Handler):
        self.url = f"{hub.rstrip('/')}/ws/{device_id}?token={token}"
        self.on_message = on_message
        self.outbox: asyncio.Queue = asyncio.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None

    def send_threadsafe(self, msg: dict) -> None:
        """Call from any thread (watchdog, stdin, pollers)."""
        assert self.loop is not None
        self.loop.call_soon_threadsafe(self.outbox.put_nowait, msg)

    async def send(self, msg: dict) -> None:
        await self.outbox.put(msg)

    async def run_forever(self) -> None:
        import websockets

        self.loop = asyncio.get_running_loop()
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.url, max_size=2**22) as ws:
                    print(f"connected to {self.url.split('?')[0]}")
                    backoff = 1
                    await asyncio.gather(self._pump(ws), self._receive(ws))
            except Exception as exc:  # hub restart, network blip
                print(f"hub connection lost ({exc!r}); retrying in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _pump(self, ws) -> None:
        while True:
            msg = await self.outbox.get()
            await ws.send(json.dumps(msg))

    async def _receive(self, ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            try:
                await self.on_message(msg)
            except Exception as exc:
                print(f"handler error for {msg.get('kind')}: {exc!r}")
