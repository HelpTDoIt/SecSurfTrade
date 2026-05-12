# Chunk 3 — ATP Read-Only Adapters (Quote, Level 2, Orders)

**Suggested model:** Sonnet 4.6
**Depends on:** none — can run in parallel with chunks 1–2
**Estimated effort:** 2–2.5 hours (the long pole; pywinauto element discovery is iterative)

---

## Goal

Build pywinauto adapters that **read** from Active Trader Pro: live quote (bid/ask/last), Level 2 depth-of-book, and the Orders panel. Plus a mock ATP for testing the rest of the system without ATP running. **No order placement** in this chunk — read-only.

## Read first

1. `ARCHITECTURE.md`, especially the **Key constraints** section (Windows + ATP running, single monitor, market hours).
2. Microsoft's Inspect.exe documentation (`Inspect.exe` ships with the Windows SDK; use it to enumerate ATP's UIA tree). If Inspect.exe isn't available, `pywinauto`'s `print_control_identifiers()` is a fallback.

## Pre-requisites the human must do before testing

- ATP launched and logged in
- The following ATP windows/panels open and visible (not minimized, not occluded):
  - A **Quote** window for the test ticker
  - A **Level II** window for the test ticker
  - The **Orders** panel
- A test ticker chosen — recommend running smoke tests against both **SPY** (liquid) and **JMAC** (thin) to cover both ends of the depth spectrum.

## Scope

**In scope:**
- `adapters/atp_quote.py` — connect to ATP, locate the Quote window for a given symbol, return `QuoteSnapshot(symbol, bid, bid_size, ask, ask_size, last, prev_close, volume, ts)`.
- `adapters/atp_level2.py` — locate the Level II window for a given symbol, return `Level2Snapshot(symbol, bids: list[Level], asks: list[Level], ts)` where `Level = (price, size, mpid)` and at least the top 5 levels are returned.
- `adapters/atp_orders.py` — locate the Orders panel, return `list[OrderRow]` where each `OrderRow` has `(account, symbol, side, qty, filled_qty, limit_price, status, placed_at, last_update_at)`. Status values map to a Pydantic enum: `Open | PartiallyFilled | Filled | Cancelled | Rejected`.
- `adapters/mock_atp.py` — in-memory simulator implementing the same interfaces. Configurable to return arbitrary quotes, L2 books, and order rows; can simulate partial-fill progression over time for monitor testing.
- `adapters/yfinance_fallback.py` — quote-only fallback (no L2) for off-hours testing.
- A **common `Protocol`** (or ABC) for each adapter so the engine and TUI never depend on pywinauto directly. The engine talks to `QuoteAdapter`, `Level2Adapter`, `OrdersAdapter` interfaces.
- `tests/test_atp_adapters.py` — tests run against the **mock**, not against ATP. ATP smoke tests are a separate manual script (see below).
- `scripts/atp_smoke.py` — manual smoke script that runs against live ATP and prints quote, L2, and Orders for a user-supplied ticker. Not part of pytest.

**Out of scope:**
- Order placement (Phase B)
- Positions panel scraping (CSV is the source of truth today)
- Account selection / dropdown manipulation in ATP
- Reconnection logic if ATP crashes (log and exit; Phase B concern)

## Implementation notes

- Use pywinauto's `uia` backend, not `win32`. ATP's controls are WPF and only the UIA backend exposes them reliably.
- Cache the top-level ATP `Application` object across adapter calls. Window/control lookups should be lazy and re-resolved each read — control handles can go stale when ATP redraws panels.
- Wrap every read in a short retry (e.g. 3 attempts, 200ms apart) before raising. ATP redraws are common.
- Numeric parsing: ATP displays `1,234.56` for prices and `1.2M` for volume. Write small parsing helpers; cover them with unit tests.
- For L2: ATP's Level II window typically shows bids and asks side by side. Identify the two grids by their relative position or accessible name; do not rely on absolute control IDs (they vary by ATP version).
- Provide a `--debug-tree` flag on the smoke script that dumps the UIA tree to a file for the human to inspect when control discovery fails.

## Acceptance criteria

1. `pytest tests/test_atp_adapters.py` passes against the mock ATP, including:
   - quote round-trip (set bid/ask/last in mock → adapter returns same values)
   - L2 with 5 levels per side
   - Orders with all five status values
   - Stalled partial-fill scenario (`last_update_at` is configurable in the mock)
2. `python scripts/atp_smoke.py SPY` against a live ATP returns a sensible quote, L2 snapshot (≥5 levels each side), and the current Orders list. Same script run with `JMAC` works (may have thinner L2; that's fine).
3. The adapters' interfaces are `Protocol`s in `adapters/__init__.py` (or similar). The mock implements them. The pywinauto adapters implement them. No other module imports `pywinauto` directly.
4. `from engine import calculator; calculator` does **not** trigger any pywinauto import. Verify by `python -c "import sys; from engine import calculator; print('pywinauto' in sys.modules)"` → prints `False`.

## Smoke script template

`scripts/atp_smoke.py` should output something like:

```
$ python scripts/atp_smoke.py SPY
Connected to ATP (PID 12345)
Quote SPY @ 2026-04-30 10:14:32-04:00
  Bid: 568.42 × 1200    Ask: 568.45 × 800    Last: 568.44
  PrevClose: 567.91     Volume: 12.4M

Level II SPY (top 5)
  BID                       ASK
  568.42  1200  NSDQ        568.45   800  ARCA
  568.41   500  ARCA        568.46  1500  NSDQ
  568.40  2000  EDGX        568.47   300  BATS
  ...

Orders panel (3 rows)
  Roth IRA   EEM   SELL  1655   PartialFill (800 filled @ 62.39)
  ...
```

If it can't find a panel, it prints which panel is missing with a hint to open it.

## When done

Capture the smoke script output for SPY and one thin ticker. Stop.
