"""
Reusable async WebSocket state-sync client (B-15).

A thin, framework-agnostic client for the local relay hub in ``server.py``.  It
is deliberately NOT coupled to Textual so it can be driven from a test, a
script, or — later — mounted into a live TUI screen (the deferred-TUI seam).

Protocol (schema-agnostic, matching the hub): every frame is a JSON object.
State-sync frames look like::

    {"type": "state", "state": {...}, "origin": "tui"|"browser", "ts": <iso>}

This client relays *state* only.  It never sends or interprets orders — there
is no order/command path anywhere in this app.
"""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Optional, Union

# Callback invoked with each decoded inbound message (a dict).  May be sync or
# return an awaitable; both are supported.
MessageCallback = Callable[[dict], Union[None, Awaitable[None]]]


class StateSyncClient:
    """Minimal async WebSocket client for the state-sync relay.

    Usage::

        client = await StateSyncClient(url, on_message=cb).connect()
        await client.send({"type": "state", "state": {...}, "origin": "tui"})
        await client.close()

    or as an async context manager::

        async with StateSyncClient(url, on_message=cb) as client:
            ...
    """

    def __init__(self, url: str, on_message: Optional[MessageCallback] = None) -> None:
        self.url = url
        self._on_message = on_message
        self._ws = None
        self._recv_task: Optional[asyncio.Task] = None

    async def connect(self) -> "StateSyncClient":
        """Open the connection and start the background receive loop."""
        from websockets.asyncio.client import connect

        self._ws = await connect(self.url)
        self._recv_task = asyncio.create_task(self._recv_loop())
        return self

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if self._on_message is None:
                    continue
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue  # ignore non-JSON frames
                result = self._on_message(msg)
                if asyncio.iscoroutine(result):
                    await result
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # connection closed / dropped — stop the loop quietly

    async def send(self, payload: dict) -> None:
        """Send a JSON-serialisable payload (typically a state-sync frame)."""
        if self._ws is None:
            raise RuntimeError("StateSyncClient.send() called before connect()")
        await self._ws.send(json.dumps(payload))

    async def close(self) -> None:
        """Cancel the receive loop and close the underlying socket."""
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def __aenter__(self) -> "StateSyncClient":
        return await self.connect()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
