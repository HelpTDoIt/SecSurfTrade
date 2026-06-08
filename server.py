#!/usr/bin/env python3
"""
Yahoo Finance close-price proxy for the Fidelity Rebalancer.

Listens on a dedicated port (default 7824) and handles:
  GET /fetch_closes?tickers=AOR,EEM,...  -> JSON {closes, errors}
  OPTIONS /fetch_closes                  -> CORS preflight

Using a separate port avoids any conflict with the static file server
on port 7823 (whether that is our own python -m http.server or the
Claude MCP preview server during development sessions).

Usage (via run.ps1 -- do not start directly):
    python server.py [port]
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


# ── Yahoo Finance proxy ────────────────────────────────────────────────────────

_YF_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
)
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def _fetch_close(ticker: str) -> float | None:
    """Return the most recent closing price for *ticker*, or None on any error."""
    url = _YF_URL.format(ticker=urllib.parse.quote(ticker))
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        closes = (
            data.get("chart", {})
            .get("result", [{}])[0]
            .get("indicators", {})
            .get("quote", [{}])[0]
            .get("close", [])
        )
        last = next((c for c in reversed(closes) if c is not None), None)
        return round(float(last), 4) if last is not None else None
    except Exception as exc:
        sys.stderr.write(f"  _fetch_close({ticker}): {exc}\n")
        sys.stderr.flush()
        return None


# ── Request handler ────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    """Minimal handler: /fetch_closes proxy only."""

    def _send_cors_preflight(self) -> None:
        self.send_response(204)
        for k, v in _CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_cors_preflight()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/fetch_closes" or self.path.startswith("/fetch_closes?"):
            self._handle_fetch_closes()
        else:
            self._send_json(404, {"error": "Not found"})

    def _handle_fetch_closes(self) -> None:
        try:
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            raw = params.get("tickers", [""])[0]
            tickers = [t.strip() for t in raw.split(",") if t.strip()]
            sys.stderr.write(f"  /fetch_closes tickers={tickers}\n")
            sys.stderr.flush()

            closes: dict[str, float] = {}
            errors: list[str] = []

            for ticker in tickers:
                price = _fetch_close(ticker)
                if price is not None:
                    closes[ticker] = price
                else:
                    errors.append(ticker)

            self._send_json(200, {"closes": closes, "errors": errors})
            sys.stderr.write(f"  -> 200 closes={list(closes.keys())} errors={errors}\n")
            sys.stderr.flush()
        except Exception as exc:
            sys.stderr.write(f"  ERROR: {exc}\n")
            sys.stderr.flush()
            self._send_json(500, {"closes": {}, "errors": [], "error": str(exc)})

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in _CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:  # type: ignore[override]
        pass  # Logging handled by explicit stderr writes above


# ── WebSocket state-sync relay hub (B-15) ───────────────────────────────────────
#
# A loopback-only relay that lets the browser calculator and any future TUI
# client share rebalance *state* live, removing the manual export/import step.
# It is a SCHEMA-AGNOSTIC relay: it forwards JSON messages between connected
# clients and caches the last `state` message so a client joining late gets an
# immediate snapshot.  It never interprets or acts on message content beyond
# peeking at "type" for the cache — there is NO order/command channel, and it
# binds 127.0.0.1 only.  Message shape (not validated here, forward-compatible):
#   {"type": "state", "state": <RebalanceState-shaped>, "origin": "browser"|"tui", "ts": <iso>}

DEFAULT_WS_PORT = 7825

# Origin allowlist for the WS handshake.  WebSocket connections are exempt from
# the same-origin policy (no CORS preflight), so a page on ANY origin could open
# ws://127.0.0.1:7825 and read the relayed portfolio state.  Browsers do send an
# Origin header on the handshake, so we reject anything not served from our own
# static file server (7823).  `None` permits non-browser clients (the Python
# tui/sync client and the test harness send no Origin header at all).
_ALLOWED_WS_ORIGINS = ["http://127.0.0.1:7823", "http://localhost:7823", None]


class RelayHub:
    """In-process pub/sub relay for state-sync messages.

    A single asyncio event loop owns all hub state, so the client set and the
    cached snapshot need no locking — every mutation happens inside a handler
    coroutine on that one loop.
    """

    def __init__(self) -> None:
        self._clients: set = set()
        self._last_state: str | None = None  # raw JSON of the last type=="state" msg

    async def handler(self, ws) -> None:
        """Per-connection coroutine: register, snapshot-on-connect, then relay."""
        self._clients.add(ws)
        try:
            if self._last_state is not None:
                await ws.send(self._last_state)
            async for raw in ws:
                await self._relay(ws, raw)
        except Exception:
            pass  # client dropped / malformed frame — just unregister below
        finally:
            self._clients.discard(ws)

    async def _relay(self, sender, raw) -> None:
        # Peek only at "type" to cache the latest snapshot — never act on content.
        try:
            msg = json.loads(raw)
        except Exception:
            return  # ignore non-JSON; never forward garbage we can't parse
        if isinstance(msg, dict) and msg.get("type") == "state":
            self._last_state = raw
        # Rebroadcast verbatim to every OTHER client (never echo to the sender).
        others = [c for c in self._clients if c is not sender]
        if others:
            await asyncio.gather(
                *(self._safe_send(c, raw) for c in others),
                return_exceptions=True,
            )

    @staticmethod
    async def _safe_send(client, raw) -> None:
        try:
            await client.send(raw)
        except Exception:
            pass  # a slow/closed peer must not break the broadcast to others


async def serve_ws(
    host: str = "127.0.0.1", port: int = DEFAULT_WS_PORT, hub: "RelayHub | None" = None
) -> None:
    """Run the relay hub until cancelled (binds host:port, 127.0.0.1 by default)."""
    from websockets.asyncio.server import serve

    hub = hub or RelayHub()
    async with serve(hub.handler, host, port, origins=_ALLOWED_WS_ORIGINS):
        await asyncio.Future()  # run forever


def start_ws_hub_thread(
    host: str = "127.0.0.1", port: int = DEFAULT_WS_PORT
) -> threading.Thread:
    """Start the WS relay hub on its own daemon thread + event loop."""

    def _run() -> None:
        try:
            asyncio.run(serve_ws(host, port))
        except Exception as exc:  # pragma: no cover - defensive
            sys.stderr.write(f"WS hub error: {exc}\n")
            sys.stderr.flush()

    t = threading.Thread(target=_run, name="ws-relay-hub", daemon=True)
    t.start()
    return t


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7824
    ws_port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_WS_PORT

    # Start the loopback WS state-sync relay alongside the HTTP proxy.  A failure
    # here (e.g. websockets missing) must not take down the proxy.
    try:
        start_ws_hub_thread("127.0.0.1", ws_port)
        sys.stderr.write(f"WS state-sync hub on ws://127.0.0.1:{ws_port}\n")
    except Exception as exc:
        sys.stderr.write(f"WS hub failed to start ({exc}); proxy continues.\n")
    sys.stderr.flush()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    sys.stderr.write(f"Proxy server on 127.0.0.1:{port}\n")
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nProxy stopped.\n")
        sys.stderr.flush()
