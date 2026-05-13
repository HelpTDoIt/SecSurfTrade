# Fidelity Rebalance Pilot — Architecture & Plan

## Goals

**Today's interim end state.** A Python *recommendation engine + monitoring component* that reads from ATP (Active Trader Pro) and Fidelity CSV exports, generates trade strategies with full reasoning, presents them via a terminal UI for human approval, and monitors order status in a live polling loop. **The app does not place orders.** The human enters every order manually in ATP. The engine output is validated against the existing React rebalance calculator via a shared JSON state file (calculator-in-the-loop parity testing).

**Future end state.** The same engine and monitor, with the *execution backend* swappable from "manual entry" → "ATP pre-fill, human submits" (Phase B) → "ATP full automation with kill switch" (Phase C). Engine code does not change between phases. The execution adapter interface is what changes.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    ENGINE (pure, no I/O)                    │
│  calculator → optimizer → strategy_gen → chunker → stall    │
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

The engine is pure logic and consumes/produces a state JSON. That same state JSON is the bridge for parity testing against the existing React calculator and for any future execution-side automation. Every I/O concern is behind an adapter interface.

## Module structure

```
fidelity_rebalancer/
├── pyproject.toml
├── accounts.json               # gitignored — your real account names, types, allocations
├── accounts.example.json       # committed template with placeholder names
├── 00_ARCHITECTURE.md          # this doc (lives at project root, not inside fidelity_rebalancer/)
├── engine/                     # pure logic, no I/O
│   ├── calculator.py           # port of React calcTrades / allocBuys
│   ├── optimizer.py            # drift-minimizing allocator (proportional + greedy)
│   ├── chunker.py              # book-relative chunking, ex-div check
│   ├── strategy_sell.py        # sell-side reasoning generator
│   ├── strategy_buy.py         # buy-side reasoning generator
│   └── stall.py                # stall detection + re-quote suggestion rules
├── state/
│   ├── schema.py               # Pydantic models for the state JSON
│   ├── importer.py             # state JSON ↔ React calc export adapter
│   └── compare.py              # diff engine output vs calc export
├── adapters/
│   ├── csv_reader.py           # Fidelity CSV → portfolio
│   ├── atp_quote.py            # pywinauto: read bid / ask / last
│   ├── atp_level2.py           # pywinauto: read L2 depth-of-book
│   ├── atp_orders.py           # pywinauto: read Orders panel
│   ├── atp_ocr.py              # OCR fallback for L2 (Telerik MAUI blocks UIA)
│   ├── atp_vision.py           # vision-based L2 reader (screen capture)
│   ├── atp_watchlist.py        # ATP watchlist scraper
│   ├── fatp_connect.py         # Fidelity ATP connection helper
│   ├── fatp_watchlist.py       # Fidelity watchlist adapter
│   ├── _atp_connect.py         # internal: pywinauto setup and caching
│   ├── _atp_parse.py           # internal: numeric parsing helpers
│   ├── _atp_ui.py              # internal: UIA navigation helpers
│   ├── yfinance_fallback.py    # quote fallback for off-hours testing
│   └── mock_atp.py             # in-memory simulator for tests
│
│   NOTE: The OCR/vision adapters and internal helpers were added because
│   ATP's Level II and Orders panels use Telerik MAUI RadMauiScrollView
│   controls that block UIA element access. UIA is used where it works;
│   OCR screen-capture is the automatic fallback.
├── tui/
│   ├── app.py                  # Textual entry point
│   ├── presenter.py            # plan approval screens
│   └── monitor.py              # live order monitor view + stall alerts
├── cli/
│   ├── compute.py              # python -m cli.compute --inputs ... --export state.json
│   ├── strategy.py             # python -m cli.strategy --state state.json --export state.json
│   └── compare.py              # python -m cli.compare --engine state.json --calc calc_export.json
│                               #   --engine: path to engine output JSON (from cli.compute)
│                               #   --calc:   path to React calc export JSON (from Export State button)
│
│   NOTE: strategy generation was split into a separate CLI pass. compute.py
│   produces sells/buys/chunks from CSVs alone (no live data needed). strategy.py
│   then fetches live quotes and embeds limit prices + reasoning bullets into the
│   same state file. This keeps the pure engine logic independent of live market reads.
├── tests/
│   ├── fixtures/               # Feb 27 test data, mock L2 books, calc exports
│   ├── test_calculator.py
│   ├── test_optimizer.py
│   ├── test_chunker.py
│   ├── test_strategy.py
│   ├── test_stall.py
│   ├── test_compare.py
│   ├── test_presenter.py       # TUI approval screen unit tests
│   └── test_atp_adapters.py    # OCR/UIA adapter tests (uses mock ATP)
└── logs/
    └── journal.jsonl           # append-only audit log of session events
```

## State JSON schema (high level)

The full Pydantic models are in chunk 2 (`02_state_schema_and_compare.md`). High-level shape:

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-04-30T09:32:14-04:00",
  "generator": "engine|react_calc",

  "inputs": {
    "accounts": [
      {
        "name": "Roth IRA",
        "type": "retirement",
        "cash_reserve": 0,
        "positions": [
          {"symbol": "EEM", "quantity": 1655.0, "price": 62.71,
           "value": 103805.05, "lot_type": "Cash"}
        ],
        "cash_spaxx": 33.88,
        "strategy_allocations": {
          "Prismatic Prudence": 0.20,
          "World Try -Top": 0.25
        }
      }
    ],
    "signals": [
      {"account": "Roth IRA", "strategy": "Prismatic Prudence",
       "current_ticker": "EEM", "new_ticker": "EWY"}
    ],
    "config": {
      "ex_div_check": true,
      "polling_seconds": 45,
      "stall_threshold_seconds": 300,
      "chunker": {"max_pct_of_top3_depth": 0.25,
                  "max_pct_of_5min_volume": 0.15}
    }
  },

  "computed": {
    "cash_ok": true,
    "one_share_total": 412.83,
    "sells": [
      {"account": "Roth IRA", "strategy": "Prismatic Prudence",
       "ticker": "EEM", "shares": 1655.0,
       "limit_price_basis": "prev_close", "est_proceeds": 103805.05}
    ],
    "buy_allocations": [
      {"account": "Roth IRA", "strategy": "Prismatic Prudence",
       "ticker": "EWY", "dollar_target": 99889.88, "share_target": 1315}
    ],
    "sell_chunks":  [{"chunk_id": "s1", "...": "..."}],
    "buy_chunks":   [{"chunk_id": "b1", "...": "..."}],
    "drift": {"before": {"...": "..."}, "after_target": {"...": "..."}}
  },

  "execution_state": {
    "fills": [
      {"chunk_id": "s1", "filled_shares": 800, "remaining": 800,
       "avg_price": 62.39, "status": "PartiallyFilled",
       "last_progress_at": "2026-04-30T09:48:11-04:00"}
    ],
    "actual_proceeds_by_account": {"Roth IRA": 49912.00}
  }
}
```

`execution_state` is optional. Without it, the engine treats sell proceeds as estimates. With it, the engine recomputes buy allocations using realized proceeds.

## Key libraries

| Library | Version | Used for |
|---|---|---|
| Python | 3.12+ | language |
| pywinauto | 0.6.8+ | ATP UIA scraping (read-only today) |
| pydantic | 2.6+ | state JSON schema validation |
| pandas | 2.2+ | CSV parsing, drift math |
| textual | 0.50+ | terminal UI (approval + monitor) |
| rich | 13.7+ | text formatting |
| loguru | 0.7+ | structured logging to journal.jsonl |
| yfinance | 0.2.36+ | quote fallback for off-hours dev/testing only |
| keyring | 24+ | OS credential storage (Windows Credential Manager) |
| pytest | latest | test runner |

**Language**: Python 3.12+ throughout. No build step for the React calculator (continues to use CDN-hosted React/ReactDOM/Babel as today).

## Key constraints

1. **Windows + ATP running and logged in.** pywinauto reads ATP windows via UIA. ATP must be visible (not minimized) and the relevant panels (Quote, Level II, Orders) must be open and not occluded. Single-monitor setup is recommended for the pilot.
2. **L2 only via ATP.** Fidelity Web has no Level 2 depth-of-book. ATP is non-negotiable as the L2 source. Replacing it with Playwright-on-Web is not an option for this app.
3. **Read-only ATP today.** No write-side automation. The TUI tells the human exactly what to enter; human enters it manually in ATP.
4. **Market hours for live data.** ATP quotes are delayed or frozen outside regular trading hours unless an extended-hours subscription is active. Tests must use the mock adapter outside RTH.
5. **Rate limit (Phase B/C only).** When write automation is added, cap at <2 orders/min to avoid Fidelity abuse-detection triggers.
6. **Calculator parity is gating.** Before the engine is trusted, it must produce byte-identical `computed` output to the React calculator on the Feb 27 regression fixture and at least one live snapshot.

## Order chunking rule

The original $100K dollar-based chunking rule is replaced with **book-relative chunking**:

- `max_chunk_shares = min( max_pct_of_top3_depth × sum_of_top3_levels_at_side, max_pct_of_5min_volume × trailing_5min_volume )`
- Default `max_pct_of_top3_depth = 0.25`, `max_pct_of_5min_volume = 0.15`. Both configurable.
- Chunk shares rounded down to nearest 100 (even-lot preference).
- For a liquid ETF (IEF, JAAA): the formula naturally yields one big chunk. For a thin ETF (JMAC, JSMD, MNA): yields several small chunks.
- Liquidity tiers from the third-party review are **emergent from the formula**, not hardcoded thresholds.

**Sequential iceberg.** Chunks for the same symbol fire sequentially, not on a time schedule. Clip N+1 must wait for clip N to reach `Filled` before being placed. If clip N is `PartiallyFilled` and stalls (see below), the monitor surfaces a re-quote suggestion before continuing.

**Ex-dividend check.** On the 1st of any month, the chunker checks each ticker for an ex-dividend event today. If found, the limit-price basis substitutes `prev_close - dividend_amount` for `prev_close`. This prevents the calculator from sizing sells against a stale price.

## Stall detection and re-quote suggestion

A clip is **stalled** when:
- status is `PartiallyFilled`
- AND `now - last_progress_at >= stall_threshold_seconds` (default 300s / 5 min)

When stalled, the monitor displays:
- the original limit and the current bid/ask/spread
- the recommended new limit (sell side: bid+1¢ at the new bid; buy side: ask−1¢ at the new ask)
- the remaining shares
- a one-keystroke "mark cancelled and re-quoted" action that updates state and creates a new chunk record

The human cancels and re-enters in ATP manually. The app records the action and resumes monitoring.

## Polling cadence

The monitor polls ATP Orders every **30–60 seconds** (configurable, default 45s). The original 5-minute interval is too slow for thin ETFs where bids walk away in seconds. Quote/L2 reads are on-demand, not continuous.

## Calculator-in-the-loop validation workflow

1. Run the React calculator as today (CSV imports, signal entry).
2. Click **Export State** in the React calc → downloads `calc_export_YYYYMMDD_HHMM.json`.
3. Run `python -m cli.compute --inputs ./csvs --signals signals.json --export engine_state.json`.
4. Run `python -m cli.compare --engine engine_state.json --calc calc_export_YYYYMMDD_HHMM.json`.
5. Diff prints `✓ sells match`, `✓ buy_allocations match`, or specific mismatches like `✗ buy_chunks[2].shares: engine=143 calc=144`.
6. Iterate engine until diffs are zero on the Feb 27 fixture and at least one live snapshot.
7. Once parity is proven, the engine output is the source of truth. The calc remains as a spot-check tool.

## Development chunks (today's interim end state)

Each chunk is a separate Claude Code prompt file. Each is independently runnable, has acceptance criteria, and includes its own tests. Order matters where dependencies exist.

| # | File | Scope | Depends on | Suggested model |
|---|------|-------|------------|-----------------|
| 1 | `01_calculator_port.md` | Port React calculator to Python engine; regression tests | none | Sonnet 4.6 |
| 2 | `02_state_schema_and_compare.md` | Full JSON schema; `compute` + `compare` CLIs; React Export + Import State buttons | 1 | Sonnet 4.6 | **Complete.** Schema, `compute`, `compare` CLIs, Export State button, and Import State button all done. |
| 3 | `03_atp_read_only.md` | pywinauto adapters: quote, L2, Orders panel; mock ATP | none (parallel-safe) | Sonnet 4.6 |
| 4 | `04_strategy_generator.md` | Sell/buy strategy with reasoning; book-relative chunker; ex-div check | 1, 3 | Opus 4.7 |
| 5 | `05_tui_presenter.md` | Textual approval flow | 4 | Sonnet 4.6 |
| 6 | `06_monitor_loop.md` | 30–60s polling, stall detection, re-quote suggestion | 3 | Sonnet 4.6 |

Recommended execution order: **1 → 2 → 3 → 4 → 5 → 6**. If you have a second window, chunk 3 can run in parallel with chunks 1–2.

## Out of scope today

- ATP write-side automation (Phase B/C work)
- React calculator **Import** State button — built; restores positions/signals/closes from a state JSON
- Live bidirectional sync between engine and React calc (re-export to checkpoint)
- Account-level kill-switch hotkey (no orders being placed by the app)
- Formatted post-session trade journal reports (events are logged but not formatted)
- Positions scraping from ATP (CSV import is the source of truth today)
- Phase B order pre-fill — even though the architecture supports it, no execution adapter beyond "print to terminal" is built today

## Definition of done for today

- All six chunk prompts executed; their acceptance criteria pass.
- `python -m cli.compute` on Feb 27 fixture produces output that diffs cleanly against `react_calc_export_feb27.json` (zero `computed` diffs).
- A dry-run end-to-end pass: CSV import → engine compute → TUI approval → monitor view watching mock ATP fills, including a stall detection event with a correct re-quote suggestion.
- Smoke test: live ATP read of bid/ask/L2 for one liquid ticker (e.g. SPY) and one thin ticker (e.g. JMAC), and a read of the Orders panel, both succeed.

## Security model

This repo is designed to be safe to make public. Key controls:

| Concern | Control |
|---|---|
| Real account names | Stored in `accounts.json` (gitignored). Committed template is `accounts.example.json` with placeholder names. |
| SectorSurfer credentials | Stored in the OS keyring (Windows Credential Manager via DPAPI). Never written to disk in plaintext. `run.ps1` checks for stored credentials on startup and prints instructions to run the scraper once if none are found — credential entry happens entirely in Python via `getpass`. |
| Per-trade detail (tickers, limit prices) | `cli.strategy` routes these to `logging.DEBUG`, suppressed by default. Use `--verbose` / `-v` to show on stderr. |
| Audit journal | `logs/journal.jsonl` is created with `mode=0o600` (owner-read/write only). The `logs/` directory is gitignored. |
| API key | `ANTHROPIC_API_KEY` read from env var only — never in source. |
| Fidelity position CSVs | `fidelity_rebalancer/csvs/` and `~/Downloads/*.csv` — never committed (`*.csv` not in repo). |
| Session cookies | `.browser_profile/` is gitignored. |

## Current status

All six chunks are complete. The parity gate in the Definition of Done is now unblocked:

1. Run the React calculator, populate Feb 27 data, click **Export State** → downloads `calc_export_YYYYMMDD_HHMM.json`.
2. Run `python -m cli.compare --engine engine_state.json --calc calc_export_YYYYMMDD_HHMM.json`.
3. Iterate engine until zero diffs. Once clean, the engine is the source of truth.

**Import State** (`rebalance_calculator.html` → Setup tab → Import State button) loads a previously exported state JSON back into the calculator — restoring positions, signals, and prev closes — so you can resume a session or cross-check without re-entering CSV data.
