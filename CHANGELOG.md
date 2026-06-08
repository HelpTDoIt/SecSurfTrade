# Changelog

All notable changes to SecSurfTrade are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This is an internal, personal tool, so entries are dated working-tree snapshots
rather than published semantic-version releases.

---

## [Unreleased] — 2026-06-08

Working-tree snapshot since the last commit (`bb793a8`, 2026-06-05). The headline
change is **B-15**, a loopback WebSocket state-sync hub that keeps the browser
calculator and any engine/TUI client in sync live, removing the manual
export/import step. This snapshot also lands the Phase-3 execution scheduler,
dynamic realized-volatility sizing, and limit-price override visibility on both
the TUI and the calculator.

### Added

- **B-15 live state sync** — a loopback WebSocket relay hub in `server.py`
  (`RelayHub`, `serve_ws`, `start_ws_hub_thread`; default port **7825**, binds
  `127.0.0.1` only). It is a schema-agnostic relay: it forwards rebalance
  **state JSON** between connected clients and caches the last `state` message so
  a late-joining client receives an immediate snapshot. There is **no
  order/command channel** — it relays state only.
- **Reusable async sync client** — `fidelity_rebalancer/tui/sync.py`
  (`StateSyncClient`): connect / receive-loop / send / async-context-manager
  helpers for any non-browser client, with no Textual coupling. Relays state
  only; there is no order path anywhere in it.
- **Browser live-sync** — the React calculator connects to `ws://127.0.0.1:7825`,
  debounces and broadcasts local edits, auto-reconnects, and shows a
  connection-status pill (**"Sync on"** / **"Reconnecting…"** / **"Connecting…"**).
  Manual Import/Export is retained as a fallback.
- **Execution scheduler** — `engine/scheduler.py` (`build_day_schedule`) and a
  `--schedule` flag on `cli.strategy` that splits an order into
  premarket / main / sweep tranches instead of a single execution window.
- **Dynamic realized volatility** — `engine/volatility.py`
  (`get_realized_volatility`): a yfinance 20-day return-σ estimate with
  asset-class and ATP day-range fallbacks, wired into `cli.strategy` sizing.
- **Order-book imbalance rules** — the buy and sell strategy generators now
  compute L2 imbalance (`total_bid / total`) and add bid-heavy (> 0.80) and
  ask-heavy (< 0.20) limit-placement rules.
- **Limit-price override visibility** — the TUI presenter (**C-11**) renders a
  manual override as a signed diff against the engine price
  (e.g. `override: +$0.0400 from engine $62.3900`); the calculator mirrors it
  with an **`OverrideBadge`** (**C-8**) so an accidental fat-finger is obvious
  before the order is typed into ATP.
- **Schema fields** — `ChunkRecord` / `OrderChunk` gain `phase`
  (`premarket`/`main`/`sweep`), `earliest_entry`, `funded_by`, `account_type`,
  and `original_limit_price`; `SellStrategy` / `BuyStrategy` gain
  `original_limit_price`; `EngineConfig` gains `premarket_pct` and
  `capture_offset_pct`; `AccountInput` gains `margin_buying_power`.
- **Tests** — new suites `test_ws_sync.py`, `test_scheduler.py`,
  `test_mom_sanity.py`, and `test_export_fills_edge.py`. Full suite is
  **467 passing**.

### Changed

- **`server.py` is now dual-role** — the existing Yahoo Finance close-price proxy
  (port 7824) plus the new WebSocket state-sync hub (port 7825), started on a
  daemon thread so a hub failure (e.g. `websockets` missing) never takes the
  proxy down.
- **`websockets` dependency** — added to `pyproject.toml` (`websockets>=13`) and
  to the `run.ps1` core-package import check.
- **Strategy tuning** — volume-exhaustion escalation (cumulative-volume / ADV
  ramps the touch fraction above 0.25 and goes aggressive above 0.75);
  phase-aware stall timer (sweep tranche tightens to 30 s, more patient on thin
  tickers); sell-side tranche pricing refined (gap capture at ask + 1 tick,
  sweep at bid).

### Fixed

- **`engine/chunker.py` tuple-unpack crash** — `_chunk_count` returned a bare
  `int` while the call site unpacked `n, capped = _chunk_count(...)`. Its
  signature is now `tuple[int, bool]` and the tier-1 path returns `(1, False)`.
  The empty-candidates fallback also drops from 100 to 1 chunk.

### Security

- **WebSocket Origin allowlist (CSWSH hardening)** — the relay handshake accepts
  only `http://127.0.0.1:7823`, `http://localhost:7823`, and `None` (non-browser
  clients send no Origin header). WebSocket connections are exempt from the
  same-origin policy and send no CORS preflight, so without this any web page the
  user visited could open `ws://127.0.0.1:7825` and read relayed portfolio state;
  the allowlist closes that cross-site-WebSocket-hijacking gap.
- **Reaffirmed boundaries** — the hub binds **`127.0.0.1` only** and relays
  **state JSON only**. The app still never places, modifies, or cancels orders.
