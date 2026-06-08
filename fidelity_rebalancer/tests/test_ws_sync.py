"""
Automated relay test for the B-15 WebSocket state-sync hub + reusable client.

Exercises the two behaviors the calculator <-> TUI sync depends on:
  1. relay     — a state frame from client A is delivered to client B, and is
                 NOT echoed back to the sender A.
  2. snapshot  — a client C that connects *after* a state was published
                 immediately receives the cached snapshot (snapshot-on-connect).

Framework-agnostic: the test body is a plain async coroutine driven by
``asyncio.run()``, so it does not depend on pytest-asyncio / anyio mode config
(the suite has no conftest pinning an async backend).  The hub binds
127.0.0.1:0 (ephemeral) and is torn down by the ``async with`` block.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# server.py lives at the repo root (two levels above this tests/ dir); tui.sync
# lives under fidelity_rebalancer/.  Make both importable regardless of cwd.
_TESTS_DIR = Path(__file__).resolve().parent
_FR_DIR = _TESTS_DIR.parent          # .../fidelity_rebalancer
_REPO_ROOT = _FR_DIR.parent          # .../SecSurfTrade  (has server.py)
for _p in (str(_FR_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from server import RelayHub  # noqa: E402
from tui.sync import StateSyncClient  # noqa: E402


class _Collector:
    """Records inbound messages and signals arrival via an asyncio.Event."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.arrived = asyncio.Event()

    def __call__(self, msg: dict) -> None:
        self.messages.append(msg)
        self.arrived.set()


async def _relay_and_snapshot_scenario() -> None:
    from websockets.asyncio.server import serve

    hub = RelayHub()
    async with serve(hub.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}"

        a_inbox, b_inbox = _Collector(), _Collector()
        client_a = await StateSyncClient(url, a_inbox).connect()
        client_b = await StateSyncClient(url, b_inbox).connect()
        # Let both per-connection handlers register before we publish.
        await asyncio.sleep(0.1)

        state_frame = {
            "type": "state",
            "state": {"marker": "rebalance-1", "value": 42},
            "origin": "tui",
            "ts": "2026-06-08T00:00:00Z",
        }
        await client_a.send(state_frame)

        # 1a. relay A -> B
        await asyncio.wait_for(b_inbox.arrived.wait(), timeout=2.0)
        assert b_inbox.messages[0]["state"]["marker"] == "rebalance-1"
        assert b_inbox.messages[0]["state"]["value"] == 42
        assert b_inbox.messages[0]["origin"] == "tui"

        # 1b. ...and NOT echoed back to the sender A
        await asyncio.sleep(0.2)
        assert a_inbox.messages == [], "sender must not receive its own frame"

        # 2. snapshot-on-connect: a late client C gets the cached state
        c_inbox = _Collector()
        client_c = await StateSyncClient(url, c_inbox).connect()
        await asyncio.wait_for(c_inbox.arrived.wait(), timeout=2.0)
        assert c_inbox.messages[0]["state"]["marker"] == "rebalance-1"

        await client_a.close()
        await client_b.close()
        await client_c.close()


def test_relay_and_snapshot_on_connect() -> None:
    """Hub relays to other clients (not the sender) and snapshots late joiners."""
    asyncio.run(_relay_and_snapshot_scenario())
