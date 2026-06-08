# Fidelity Rebalance Pilot — Architecture & Plan

## Goals

A Python _recommendation engine + monitoring component_ that reads from ATP (Active Trader Pro) and Fidelity CSV exports, generates trade strategies with full reasoning, presents them via a terminal UI for human approval, and monitors order status in a live polling loop. **The app does not place, modify, or cancel orders.** The human enters every order manually in ATP. The engine output is validated against the existing React rebalance calculator via a shared JSON state file (calculator-in-the-loop parity testing).

> This document describes the system **as it is built today**. The forward-looking view — the swappable execution backend (Phase B pre-fill, Phase C full automation, engine code unchanged) and the build status of the original six chunks — lives in `docs/dev/09_roadmap.md` (sections **Foundation (shipped)** and **E. Future / Phase B+**).

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    ENGINE (pure, no I/O)                    │
│  calculator → optimizer → strategy → chunker → stall/sweep  │
└──────────────▲──────────────────────────▲───────────────────┘
               │                          │
        reads from                  writes via
               │                          │
┌──────────────┴───────────┐  ┌───────────┴───────────────────┐
│   READ ADAPTERS          │  │  EXEC ADAPTERS (pluggable)    │
│  • CSV import            │  │  TODAY: print/copy            │
│  • ATP quote scraper     │  │  PHASE B: ATP pre-fill, human │
│  • ATP L2 scraper        │  │     clicks Preview + Submit   │
│  • ATP Orders scraper    │  │  PHASE C: ATP full automation │
│  • Mock ATP / yfinance   │  │     + kill switch             │
│    (testing only)        │  │                               │
└──────────────────────────┘  └───────────────────────────────┘
                    │                      │
                    └──────┬───────────────┘
                           ▼
              ┌────────────────────────┐
              │  Textual TUI:          │
              │  • approval presenter  │
              │  • monitor view        │
              └────────────────────────┘
                           ▲
                           │
              ┌────────────────────────┐
              │ React calc (parity     │
              │ check via state JSON)  │
              └────────────────────────┘
```

The engine is pure logic and consumes/produces a state JSON. That same state JSON is the bridge for parity testing against the existing React calculator and for any future execution-side automation. Every I/O concern is behind an adapter interface. The same state JSON also flows **live** over a loopback WebSocket relay (`server.py`, **B-15**) so the browser calculator and any TUI client stay in sync without a manual export/import round-trip — see **Local servers** below.

## Module structure

```
fidelity_rebalancer/
├── pyproject.toml
├── accounts.json               # gitignored — your real account names, types, allocations
├── accounts.example.json       # committed template with placeholder names
├── 00_ARCHITECTURE.md          # this doc (lives at project root, not inside fidelity_rebalancer/)
├── engine/                     # pure logic, no I/O (except observability.py)
│   ├── scheduler.py            # Phase 3: builds day-schedule with premarket/main/sweep tranches
│   ├── calculator.py           # port of React calcTrades / allocBuys (sells, buys, cash gate)
│   ├── optimizer.py            # drift-minimizing buy allocator; recompute_buys (F-1) for the monitor
│   ├── chunker.py              # book-relative / POV / gap-capture / legacy chunkers (odd-lots + 15 max cap)
│   ├── volatility.py           # dynamic realized volatility estimator (yfinance / asset class / day-range)
│   ├── strategy_sell.py        # sell-side rule selection + reasoning (rules 0–7 + default)
│   ├── strategy_buy.py         # buy-side rule selection + reasoning (rules 1–5 + default)
│   ├── escalation.py           # side-aware time-of-day and volume-exhaustion urgency ramp
│   ├── decision_context.py     # per-decision market-input bundle (DecisionContext)
│   ├── spread_context.py       # per-symbol spread thresholds (tight / wide)
│   ├── size_context.py         # per-asset-class %ADV cutoffs (G-5 / G-6)
│   ├── vwap.py                 # pure intraday VWAP math (G-3)
│   ├── sweep.py                # EOD-sweep predicate: clock OR unfilled-fraction (F-1 / S-1)
│   ├── stall.py                # stall detection + re-quote via full rule selection (F-6)
│   └── observability.py        # stdlib logging setup + decisions.jsonl (only I/O-bearing engine module)
├── state/
│   ├── schema.py               # Pydantic v2 models for the state JSON
│   ├── importer.py             # load/save state JSON ↔ React calc export
│   └── compare.py              # diff engine output vs calc export
├── adapters/                   # every I/O concern behind an interface
│   ├── csv_reader.py           # Fidelity CSV → portfolio
│   ├── atp_quote.py            # UIA: bid / ask / last
│   ├── atp_level2.py           # UIA: L2 depth-of-book
│   ├── atp_orders.py           # Orders panel (UIA blocked by Telerik MAUI → OCR is the live path)
│   ├── atp_ocr.py              # RapidOCR reader for L2 / Orders / Watchlist (the working fallback)
│   ├── atp_vision.py           # vision-based L2 reader (screen capture)
│   ├── atp_watchlist.py        # ATP Watchlist OCR: quote, prev_close, 10-day ADV, VWAP, ex-div, day range
│   ├── fatp_connect.py         # Fidelity ATP connection helper
│   ├── fatp_watchlist.py       # Fidelity watchlist adapter
│   ├── yfinance_fallback.py    # off-ATP path: quotes, ADV, approx intraday VWAP
│   ├── mock_atp.py             # in-memory simulator for tests
│   ├── _atp_connect.py         # internal: connect + 3× retry helper
│   ├── _atp_parse.py           # internal: numeric parsing helpers
│   └── _atp_ui.py              # internal: UIA navigation helpers
│
│   NOTE: the OCR/vision adapters exist because ATP's Level II and Orders panels
│   use Telerik MAUI RadMauiScrollView controls that block UIA element access.
│   UIA is used where it works; RapidOCR screen-capture is the automatic fallback.
├── tui/                        # Textual terminal UI (alternate workflow to React calc)
│   ├── app.py                  # Entry point for interactive strategy approval (python -m tui.app)
│   ├── presenter.py            # Screens for approval/modification, highlights manual overrides (C-11)
│   ├── monitor.py              # Live polling loop (python -m tui.monitor), stalls, journal.jsonl writer
│   └── sync.py                 # B-15: reusable async WebSocket state-sync client (no Textual coupling)
├── preflight/                  # morning readiness gate (pure decision logic + interactive shell)
│   ├── checks.py               # FT+-running + ticker-presence (Watchlist / L2) checks
│   ├── planner.py              # L2-window plan for thin tickers against the window cap
│   ├── sanity.py               # pre-trade sanity findings (RED / YELLOW / GREEN), account-type-aware
│   └── orchestrator.py         # readiness eval, sizing-command builder, outcome classifier
├── cli/
│   ├── compute.py              # python -m cli.compute --inputs ... --export state.json
│   ├── strategy.py             # python -m cli.strategy --state state.json --export state.json
│   ├── preflight.py            # python -m cli.preflight --state state.json (readiness -> sizing -> sanity)
│   ├── progress.py             # python -m cli.progress --state state.json (buy fill vs. time-elapsed pace)
│   ├── export_fills.py         # python -m cli.export_fills (reads journal.jsonl -> JSON for React calc)
│   ├── eod_report.py           # python -m cli.eod_report [--since today|all|YYYY-MM-DD] (post-session summary, today by default)
│   └── compare.py              # python -m cli.compare --engine state.json --calc calc_export.json
│
│   NOTE: strategy generation is a separate CLI pass. compute.py produces
│   sells/buys/chunks from CSVs alone (no live data); strategy.py then fetches
│   live quotes and embeds limit prices + reasoning into the same state file.
│   This keeps the pure engine logic independent of live market reads.
├── tests/                      # pytest; run from fidelity_rebalancer/ with PYTHONPATH=.
│   ├── fixtures/               # regression data, mock L2 books, calc exports
│   ├── test_calculator.py · test_optimizer.py · test_optimizations.py
│   ├── test_chunker.py · test_strategy.py · test_stall.py · test_sweep.py
│   ├── test_vwap.py · test_decision_context.py · test_compare.py
│   ├── test_presenter.py · test_atp_adapters.py · test_atp_ocr_orders.py
│   ├── test_monitor_fills.py · test_monitor_recompute.py · test_monitor_e2e.py
│   ├── test_observability.py · test_eod_report.py · test_strict_atp.py
│   ├── test_ws_sync.py · test_scheduler.py · test_mom_sanity.py · test_export_fills_edge.py
│   ├── test_adapters_logging.py · test_cli_path_resolution.py · test_monitor_logging_setup.py
│   └── test_preflight_{checks,planner,sanity,orchestrator,cli}.py
└── logs/                       # gitignored
    ├── journal.jsonl           # monitor: append-only session/fill audit log
    ├── strategy.log            # engine: leveled run log (INFO; DEBUG with -v)
    └── decisions.jsonl         # engine: structured per-ticker decision trail
```

## Local servers (`server.py`)

`server.py` lives at the repo root (outside the package) and runs two small
loopback services, started together by `run.ps1`:

- **Yahoo Finance proxy (HTTP, port 7824).** A CORS bridge so the React
  calculator's *⬇ Fetch from Yahoo Finance* button can pull previous closes
  without browser cross-origin errors. `GET /fetch_closes?tickers=…` → JSON
  `{closes, errors}`.
- **State-sync relay hub (WebSocket, port 7825) — B-15.** A schema-agnostic
  pub/sub relay (`RelayHub` / `serve_ws` / `start_ws_hub_thread`) that lets the
  browser calculator and any future TUI client share rebalance **state JSON**
  live, removing the manual export/import step. It caches the last `state`
  message and replays it to clients that join late (snapshot-on-connect), then
  rebroadcasts each inbound frame verbatim to every *other* client.

**Hard boundary:** the relay forwards **state JSON only** — there is no order or
command channel, and it never interprets message content beyond peeking at
`"type"` to cache snapshots. Both services **bind `127.0.0.1` only**, and the WS
handshake is further restricted by an **Origin allowlist** (`http://127.0.0.1:7823`
/ `http://localhost:7823`, plus `None` for non-browser clients such as
`tui/sync.py` and the test harness) to block cross-site WebSocket hijacking.

The reusable client is `tui/sync.py::StateSyncClient` (async, framework-agnostic).
The browser opens `ws://127.0.0.1:7825`, shows a connection-status pill
(**"Sync on"** / **"Reconnecting…"**), debounces local edits before broadcasting,
and keeps the manual Import/Export buttons as a fallback. Mounting the client
into a live always-on TUI screen is deferred — the current `RebalanceApp` is a
short-lived approval wizard — tracked as **B-18** in `docs/dev/09_roadmap.md`.

## State JSON schema (high level)

The full Pydantic models are in chunk 2 (`docs/dev/02_state_schema_and_compare.md`). High-level shape:

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-04-30T09:32:14-04:00",
  "generator": "engine|react_calc",

  "inputs": {
    "accounts": [
      {
        "name": "My Retirement",
        "type": "retirement",
        "margin": false,
        "cash_reserve": 0,
        "positions": [
          {
            "symbol": "EEM",
            "quantity": 1655.0,
            "price": 62.71,
            "value": 103805.05,
            "lot_type": "Cash"
          }
        ],
        "cash_spaxx": 33.88,
        "pending_activity": 0.0,
        "strategy_allocations": {
          "Strategy Alpha": 0.2,
          "World Try -Top": 0.25
        }
      }
    ],
    "signals": [
      {
        "account": "My Retirement",
        "strategy": "Strategy Alpha",
        "current_ticker": "EEM",
        "new_ticker": "EWY"
      }
    ],
    "config": {
      "ex_div_check": true,
      "polling_seconds": 45,
      "stall_threshold_seconds": 300,
      "chunker": {
        "max_pct_of_top3_depth": 0.25,
        "max_pct_of_5min_volume": 0.15
      },
      "sweep_time_minutes": 330,
      "sweep_unfilled_frac": 0.5
    }
  },

  "computed": {
    "cash_ok": true,
    "one_share_total": 412.83,
    "sells": [
      {
        "account": "My Retirement",
        "strategy": "Strategy Alpha",
        "ticker": "EEM",
        "shares": 1655.0,
        "limit_price_basis": "prev_close",
        "est_proceeds": 103805.05
      }
    ],
    "buy_allocations": [
      {
        "account": "My Retirement",
        "strategy": "Strategy Alpha",
        "ticker": "EWY",
        "dollar_target": 99889.88,
        "share_target": 1315
      }
    ],
    "sell_chunks": [{ "chunk_id": "s1", "...": "..." }],
    "buy_chunks": [{ "chunk_id": "b1", "...": "..." }],
    "drift": { "before": { "...": "..." }, "after_target": { "...": "..." } }
  },

  "execution_state": {
    "fills": [
      {
        "chunk_id": "s1",
        "filled_shares": 800,
        "remaining": 800,
        "avg_price": 62.39,
        "status": "PartiallyFilled",
        "last_progress_at": "2026-04-30T09:48:11-04:00"
      }
    ],
    "actual_proceeds_by_account": { "My Retirement": 49912.0 }
  }
}
```

`execution_state` is optional. Without it, the engine treats sell proceeds as estimates. With it, the engine recomputes buy allocations using realized proceeds.

## Key libraries

| Library   | Version | Used for                                           |
| --------- | ------- | -------------------------------------------------- |
| Python    | 3.12+   | language (developed and run on 3.14)               |
| pydantic  | 2.6+    | state JSON schema validation                       |
| textual   | 0.50+   | terminal UI (approval + monitor)                   |
| rich      | 13.7+   | text formatting                                    |
| yfinance  | 0.2.36+ | off-ATP quotes / 10-day ADV / approximate VWAP     |
| pandas    | 2.2+    | yfinance DataFrames (ADV / realized-vol math)      |
| numpy     | —       | realized-vol math + OCR pixel arrays               |
| rapidocr-onnxruntime + Pillow | — | OCR of ATP L2 / Orders / Watchlist (Telerik MAUI blocks UIA) |
| pywinauto | 0.6.8+  | ATP UIA scraping where it works (read-only)        |
| keyring   | 24+     | OS credential storage (Windows Credential Manager) |
| websockets| 12+     | loopback state-sync relay hub + reusable async client (B-15) |
| pytest    | latest  | test runner                                        |

**Logging** uses the Python **stdlib `logging`** module via `engine/observability.py` (leveled `logs/strategy.log` + structured `logs/decisions.jsonl`); the live monitor writes `logs/journal.jsonl`. `loguru` is still declared in `pyproject.toml` but is no longer imported.

**Language**: Python 3.12+ (the OCR stack needs 3.14-compatible wheels, so the project is run on 3.14). No build step for the React calculator (CDN-hosted React/ReactDOM/Babel as today).

## Key constraints

1. **Windows + ATP running and logged in.** pywinauto reads ATP windows via UIA. ATP must be visible (not minimized) and the relevant panels (Quote, Level II, Orders) must be open and not occluded. Single-monitor setup is recommended for the pilot.
2. **L2 only via ATP.** Fidelity Web has no Level 2 depth-of-book. ATP is non-negotiable as the L2 source. Replacing it with Playwright-on-Web is not an option for this app.
3. **Read-only ATP today.** No write-side automation. The TUI tells the human exactly what to enter; human enters it manually in ATP.
4. **Market hours for live data.** ATP quotes are delayed or frozen outside regular trading hours unless an extended-hours subscription is active. Tests must use the mock adapter outside RTH.
5. **Rate limit (Phase B/C only).** When write automation is added, cap at <2 orders/min to avoid Fidelity abuse-detection triggers.
6. **Calculator parity is gating.** Before the engine is trusted, it must produce byte-identical `computed` output to the React calculator on the Feb 27 regression fixture and at least one live snapshot.

## Order rules

This is the human-followable account of **how an order's size, its lots
(chunks), its limit price, and its timing are decided** — for both buys and
sells, across taxable and retirement accounts. Plain English first, then the
math. Backlog items that would change a rule are tagged inline by ID (see
`docs/dev/09_roadmap.md`).

### Inputs — where every number comes from

| Input | Source | Feeds |
| --- | --- | --- |
| Positions, SPAXX cash, pending activity | Fidelity CSV → `adapters/csv_reader.py` → `engine/calculator.py` | sizing (shares/dollars), cash gate |
| Account type / margin / cash reserve / target weights | `accounts.json` | account-type gating, allocation |
| Signals (current → new ticker per strategy) + prev closes | `signals.json` | what to sell/buy, limit basis |
| Live bid / ask / last / volume / prev_close / VWAP / **10-day ADV** | ATP Watchlist OCR (`--source atp`) or yfinance (`--source yfinance`) | price rules, %ADV sizing |
| Level 2 depth (top-of-book ladders) | ATP OCR (`--l2-symbols`) | book-relative lots |
| Realized volatility (σ, daily bps) | yfinance daily closes (`cli/strategy.py::_realized_vol_bps`) | POV impact estimate |
| Minutes since 9:30 ET open (clock) | wall clock (`cli/strategy.py`) | gap-capture, escalation, sweep |

**One ADV definition end-to-end (G-6):** `%ADV = order shares ÷ 10-day ADV × 100`.
The 30-day yfinance `get_adv()` is only an in-generator fallback when no
watchlist row exists.

### Flow — two passes over one state file

1. **Allocation pass — `cli.compute` (no live data).** From CSVs + signals,
   `engine/calculator.py` decides *what* trades and *how much*: which strategies
   are trading vs holding, the dollar/share target of each sell and buy, and the
   per-account cash gate. Output: `sells`, `buy_allocations`, placeholder chunks,
   `cash_ok`.
2. **Pricing pass — `cli.strategy` (live data).** For each sell/buy,
   `engine/strategy_sell.py` / `strategy_buy.py` pick a **rule** → a **limit
   price** and **urgency**; the **chunker** splits the order into **lots**;
   `engine/escalation.py` ramps urgency by time of day; records are reconciled
   down to their chunks (the chunks are what a human actually enters).

Human-in-the-loop throughout: the app emits a plan; the human enters every order
in ATP. It never places, modifies, or cancels (hard rule).

### Sizing — how many shares / dollars (allocation pass)

Plain English:
- Free cash = settled SPAXX **plus** pending activity, minus your cash reserve;
  never negative.
- The pool to redeploy = the value of the strategies that are changing **plus**
  that free cash.
- A strategy whose signal changed is **trading** → sell the whole current
  position. A strategy whose signal is unchanged is **holding** → only top it up
  toward target weight if cash allows.
- Spread the available money (sale proceeds + free cash) across the new buys by
  target weight. If the buys add up to more than the money available, scale them
  all down by the same ratio so the plan never spends money you don't have.
- Shares = floor(dollars ÷ limit price) — whole shares only.

Math (`engine/calculator.py::calc_trades` / `_alloc_buys`):
```
effective_cash = SPAXX_value + pending_activity
depl_cash      = max(0, effective_cash − cash_reserve)
total_pool     = Σ(value of trading-strategy positions) + depl_cash
avail          = Σ(sell est_proceeds) + depl_cash

sells:  full position of every trading strategy (qty > 0); limit basis = prev_close
buys:   per strategy, dollar_target = weight × total_pool  (holding: minus current
        value), assigned greedily up to (avail − spent)
scale:  if Σ dollar_target > avail:  dollar_target ×= avail / Σ dollar_target
shares: floor(dollar_target / limit_price)
```

### Account types — taxable vs retirement vs margin

Plain English:
- The cash gate (`cash_ok`) is the same arithmetic for every account: is the free
  cash bigger than one share of each strategy's ETF? If not, the buys lean on
  **today's** unsettled sale proceeds.
- What differs is the **warning**, not the math:
  - **Margin (taxable):** no warning — same-session buy+sell is funded by buying
    power, not settled cash. The `CASH_NOT_OK` finding is suppressed.
  - **Retirement / IRA:** soft warning that this is the **expected** timing gap —
    buys funded by today's not-yet-settled sells. Confirm coverage (or pass
    `--confirmed-proceeds`).
  - **Cash (non-margin taxable):** genuine shortfall — wait for settlement or trim
    the buys.
- Settlement timing is sell-before-buy: `--confirmed-proceeds` rescales each
  account's buy budget to the *actual* realized proceeds.

Math / gate (`engine/calculator.py`, `preflight/sanity.py`):
```
cash_ok = depl_cash > Σ(one_share_price per strategy)
CASH_NOT_OK (YELLOW) raised only when cash_ok is False AND account is not margin:
    margin       → suppressed
    retirement   → "expected IRA timing gap" wording
    cash account → "genuine shortfall" wording
```
Backlog: **B-7** — the scale-down still caps buys at `proceeds + cash`
*regardless of margin*, so a margin account can under-buy vs true buying power;
loosening needs a parsed buying-power number. **A / S-1** — account-type +
settlement gating becomes phase-aware (`funded_by` = proceeds/cash/buying_power)
when the scheduler is absorbed into the engine.

### Lots — how each order is split into chunks (pricing pass)

Plain English — pick the smallest "fingerprint" the book can absorb:
- **Book-relative (default, when L2 depth is present):** each clip is the smaller
  of a quarter of visible top-3 depth and 15% of the last 5 minutes' volume,
  rounded down to a round lot (100), never below 100.
- **POV fallback (no L2):** size from %ADV via a square-root impact model; tiny
  orders go as one invisible clip, bigger ones split into more clips with ±15%
  jitter so the pattern isn't obvious.
- **Premarket / Gap-capture (sell/buy):** captures gap up for sells or gap down for buys in early tranches.
- **Legacy $100K dollar chunker:** the compute-pass placeholder used before live
  prices exist.
- Clips for one symbol are entered **largest-first**, sequentially (iceberg) —
  clip N+1 waits for clip N to fill.

Math (`engine/chunker.py`):
```
book-relative:
    max_chunk = min(0.25 × Σtop-3 depth, 0.15 × vol_5min) → floor to 100, min 100
    vol_5min  = (10-day ADV ÷ 78 five-minute slices) × volume-profile multiplier
                open 1.8× · mid-morning 1.1× · lunch 0.6× · afternoon 1.2× · close 1.5×
POV (build_chunks_pov):
    impact_bps = 0.5 × σ × √(Q / ADV)            # square-root law, σ default 100 bps
    tiers by %ADV: <1% invisible · <5% standard · <10% aggressive · else market_moving
    chunk count: ceil(pov) → 1.5× → 2× by tier; sells +1; ±15% jitter; round 100; cap 20
gap-capture: 30% / 50% / 20% across gap / standard / sweep prices
tick: $0.0001 if price < $1 else $0.01
```
Large buys (rule 3) halve the depth cap (0.25 → 0.125) for smaller clips.
**Ex-dividend (chunker):** on the 1st of any month, if a ticker goes ex-div
today the sell's limit basis substitutes `prev_close − dividend` for `prev_close`
so sizing isn't against a stale price.

### Price & urgency — the rule ladders (first match wins)

**Sells** (`engine/strategy_sell.py`; prev_close ex-div-adjusted on the 1st):

| # | Condition (plain) | Limit | Urgency |
| --- | --- | --- | --- |
| 0 | Gapped up > 0.5% in first 30 min | 3-phase gap capture | aggressive |
| 1 | Tight spread + volume + small (< %ADV) | midpoint | normal |
| 2 | Tight spread + large (> %ADV) | bid (drip in) | patient |
| 3 | Wide spread | bid + 1 tick | patient |
| 4 | Down day (< −2% vs prev close) | prev_close × 0.99 | patient |
| 5 | Up day (> +2%) | bid (sell into strength) | aggressive |
| 6 | Above VWAP | last | aggressive |
| 7 | Below VWAP | bid + 1 tick | patient |
| — | default | midpoint | normal |

**Buys** (`engine/strategy_buy.py`):

| # | Condition (plain) | Limit | Urgency |
| --- | --- | --- | --- |
| 1 | Tight spread + volume | ask | normal |
| 2 | Wide spread | midpoint | patient |
| 3 | Large (> %ADV) | ask − 1 tick, ½ depth cap | patient |
| 4 | Below VWAP (favorable) | ask | normal |
| 5 | Above VWAP (paying up) | midpoint | patient |
| — | default | ask | normal |

Thresholds are per-symbol, not hardcoded:
- **Spread** tight / wide = 0.8× / 1.5× the symbol's typical spread
  (`engine/spread_context.py`); live bid/ask preferred, else asset-class typical
  (large_cap ~3 bps … leveraged ~20 bps).
- **%ADV** small/large cutoffs by asset class (**G-5 / G-6**,
  `engine/size_context.py`): e.g. large_cap sell 3/8 buy 5; leveraged 1/2.5 buy
  1.5; unknown tickers keep the legacy 2/5 sell, 3 buy.
- **VWAP** rules (sell 6/7, buy 4/5) need a VWAP: exact on ATP, approximate from
  1-min bars on yfinance (**G-3**, `engine/vwap.py`).

### Timing & lifecycle

- **Intraday urgency ramp** (`engine/escalation.py`, symmetric buy/sell **G-4**),
  minutes since the 9:30 open:
  - 0–90: use the rule's urgency as-is.
  - 90–210: → normal; nudge the limit 75% toward the touch (buy→ask, sell→bid).
  - 210–330: → aggressive; limit at the touch.
  - 330+ (after 3:00): one tick **past** the touch to force a same-day fill.
- **VWAP Time Backstop:** VWAP urgency jumps to aggressive at 2:45pm (315 minutes).
- **EOD sweep predicate** (`engine/sweep.py`): act when the clock reaches the
  sweep cutoff (default 330 min = 15:00 ET) **or** the unfilled fraction crosses
  its threshold (default 0.5). Used by the live recompute (**F-1**, clock-only)
  and, later, scheduler absorption (**A / S-1**, fill-aware).
- **Live recompute** (`engine/optimizer.py::recompute_buys`, **F-1**): when sells
  go terminal or the clock fires, buy allocations are recomputed from *realized*
  proceeds — still a plan; no orders placed.
- **Monitor polling & stall/re-quote** (`tui/monitor.py`, `engine/stall.py`): ATP
  Orders polled every ~45 s (configurable; the old 5-min interval was too slow
  for thin ETFs). A clip is **stalled** when `PartiallyFilled` and
  `now − last_progress_at ≥ stall_threshold` (default 300 s); the monitor then
  re-runs **full rule selection** on the remaining shares (**F-6 / Step 6**) — not
  a fixed bid+1¢ — and offers a one-key "cancel + re-quote" that the human
  performs in ATP.

Reasoning bullets for every decision are recorded to `logs/decisions.jsonl` and
gated to DEBUG / `--verbose` on the console; promoting them to always-visible is
**B-2 / G-1**.

## Calculator-in-the-loop validation workflow

1. Run the React calculator as today (CSV imports, signal entry).
2. Click **Export State** in the React calc → downloads `calc_export_YYYYMMDD_HHMM.json`.
3. Run `python -m cli.compute --inputs ./csvs --signals signals.json --export engine_state.json`.
4. Run `python -m cli.compare --engine engine_state.json --calc calc_export_YYYYMMDD_HHMM.json`.
5. Diff prints `✓ sells match`, `✓ buy_allocations match`, or specific mismatches like `✗ buy_chunks[2].shares: engine=143 calc=144`.
6. Iterate engine until diffs are zero on the Feb 27 fixture and at least one live snapshot.
7. Once parity is proven, the engine output is the source of truth. The calc remains as a spot-check tool.

## Security model

This repo is designed to be safe to make public. Key controls (plus the loopback-server controls in **Local servers** above — the **B-15** WebSocket relay binds `127.0.0.1`, relays **state JSON only, never orders or commands**, and gates its handshake with an Origin allowlist):

| Concern                                  | Control                                                                                                                                                                                                                                                                                |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Real account names                       | Stored in `accounts.json` (gitignored). Committed template is `accounts.example.json` with placeholder names.                                                                                                                                                                          |
| SectorSurfer credentials                 | Stored in the OS keyring (Windows Credential Manager via DPAPI). Never written to disk in plaintext. `run.ps1` checks for stored credentials on startup and prints instructions to run the scraper once if none are found — credential entry happens entirely in Python via `getpass`. |
| Per-trade detail (tickers, limit prices) | `cli.strategy` routes these to `logging.DEBUG`, suppressed by default. Use `--verbose` / `-v` to show on stderr.                                                                                                                                                                       |
| Audit journal                            | `logs/journal.jsonl` is created with `mode=0o600` (owner-read/write only). The `logs/` directory is gitignored.                                                                                                                                                                        |
| API key                                  | `ANTHROPIC_API_KEY` read from env var only — never in source.                                                                                                                                                                                                                          |
| Fidelity position CSVs                   | `fidelity_rebalancer/csvs/` and `~/Downloads/*.csv` — never committed (`*.csv` not in repo).                                                                                                                                                                                           |
| Session cookies                          | `.browser_profile/` is gitignored.                                                                                                                                                                                                                                                     |
