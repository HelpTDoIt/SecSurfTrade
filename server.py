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

import json
import sys
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


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7824
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    sys.stderr.write(f"Proxy server on 127.0.0.1:{port}\n")
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nProxy stopped.\n")
        sys.stderr.flush()
