# Test Plan: Trading Window Validation (Phases 1–5)

**Date prepared:** 2026-05-07
**Last pre-window validation:** 2026-05-12 — S-2, T-1.1, T-1.2 (pre-market), T-1.3, T-2.1, T-3.2 all pass
**Execute during:** Next trading day with SectorSurfer signals (monthly rotation, typically 1st trading day of month)
**Pre-requisites:** Fidelity Trader+ open and logged in, SectorSurfer signals available

---

## Overview

This plan validates all Phase 1–5 optimizations end-to-end during a live trading session.  Each test has a **PASS criteria** and **capture instructions** so results are reproducible.

---

## Pre-Market Setup (before 9:30 ET)

### S-1: Generate signals and compute state
```bash
cd fidelity_rebalancer
python scripts/sectorsurfer_signals.py
python -m cli.compute --inputs ./csvs --signals signals.json --export today.json
```
**Capture:** Save `signals.json` and `today.json` as `tests/fixtures/YYYYMMDD_signals.json` and `tests/fixtures/YYYYMMDD_state.json`.

### S-2: Run strategy generation (yfinance, pre-market)
```bash
python -m cli.strategy --state today.json --export today_premarket.json --source yfinance
```
**PASS:** Completes without error. Terminal output shows:
- [ ] Realized volatility computed for each ticker (sigma values printed)
- [ ] Volume profile multiplier printed (expect 1.0 pre-market)
- [ ] Thin-ticker detection block printed **only if** any ticker exceeds 3% ADV; no output if all are liquid (both outcomes are valid)
- [ ] Each sell/buy strategy shows `rule=`, `pov=` tier label, `prev_close=`, `adv10=`

**Capture:** Save terminal output to `logs/YYYYMMDD_premarket_strategy.txt`.

---

## Phase 1 Validation: Quantitative Foundations

### T-1.1: Realized Volatility
**What:** Verify per-symbol sigma values are reasonable (not all 100 bps default).
**How:** In the strategy generation output, check the volatility block:
```
Computing realized volatility for N ticker(s)...
  SPY     sigma=148 bps
  DFEN    sigma=512 bps (leveraged -> higher)
  EIS     sigma=210 bps
```
**PASS:** (sigma values are **daily** bps: 100 bps = 1% daily vol)
- [ ] At least 80% of tickers show non-default sigma (not "100 bps (default)")
- [ ] Leveraged ETFs (DFEN, BULZ, TQQQ, etc.) show sigma > 300 bps
- [ ] Large-cap ETFs (SPY, QQQ, EEM) show sigma between 80-250 bps

### T-1.2: Volume Profile Multiplier
**What:** Verify the multiplier changes based on time of day.
**How:** Run strategy generation at different times and compare:
```bash
# Pre-market (before 9:30): expect 1.0x
python -m cli.strategy --state today.json --export /dev/null --source yfinance
# Opening (9:35): expect 1.8x
# Lunch (12:00): expect 0.6x
# Close (15:45): expect 1.5x
```
**PASS:**
- [ ] Pre-market shows `Volume profile multiplier: 1.0x (outside market hours)`
- [ ] At 9:35 shows `1.8x`
- [ ] At 12:00 shows `0.6x`

### T-1.3: Spread Calibration (SpreadContext)
**What:** Verify spread thresholds are per-symbol, not hardcoded 5/10 bps.
**How:** Open `today_premarket.json`, check strategy reasoning bullets:
```python
import json
with open("today_premarket.json") as f:
    state = json.load(f)
for s in state["computed"]["sell_strategies"]:
    print(f"{s['ticker']:6s} rule={s['rule']:<28s} reasoning={s['reasoning'][:2]}")
```
**PASS:**
- [ ] Leveraged ETFs (DFEN, BULZ) are NOT classified as `wide_spread` despite having ~20 bps spreads (their typical spread is ~20 bps, so 20 bps is normal for them)
- [ ] Large-cap ETFs (SPY, EEM) with 2-3 bps spreads trigger `tight_spread_*` rules

### T-1.4: VWAP Benchmark
**What:** Verify VWAP rules fire when appropriate during market hours.
**How:** Run strategy generation after 10:00 AM when VWAP is available from yfinance/ATP.
**PASS:**
- [ ] At least one strategy shows `rule=above_vwap` or `rule=below_vwap`
- [ ] VWAP reasoning bullets show actual VWAP values (not N/A)
- [ ] Pre-market run shows NO VWAP rules (VWAP=0 pre-market)

---

## Phase 2 Validation: L2 Depth

### T-2.1: Thin-Ticker Detection
**What:** Verify tickers with large orders relative to ADV are flagged.
**How:** Check the strategy generation output for the thin-ticker block:
```
Thin-ticker detection (order > 3% ADV):
  SELL DFEN    5.2% of ADV — open L2 window in ATP
  BUY  EIS     3.8% of ADV — open L2 window in ATP
```
**PASS:**
- [ ] At least one ticker is flagged (or all tickers are liquid and none flagged — both are valid)
- [ ] Flagged tickers match manual calculation: `shares / avg_vol_10d * 100 > 3.0`

### T-2.2: L2 OCR Integration (requires ATP)
**What:** Verify `--l2-symbols` fetches real L2 data via OCR.
**How:** Open L2 windows in FT+ for the flagged thin tickers, then:
```bash
# Auto-detect thin tickers
python -m cli.strategy --state today.json --export today_l2.json --source atp --l2-symbols

# Or explicit symbols
python -m cli.strategy --state today.json --export today_l2.json --source atp --l2-symbols DFEN EIS
```
**PASS:**
- [ ] L2 fetch output shows `L2 OK (N bids, M asks)` for each symbol
- [ ] Strategies for L2-fetched tickers use `book_relative` chunks (not POV rechunking)
- [ ] Chunk sizes reflect actual book depth (not the flat ADV/78 estimate)

**FAIL recovery:** If L2 OCR fails, check:
1. L2 panel is fully visible (not occluded by other windows)
2. The "Exch" column header is visible in the L2 panel
3. Run `python scripts/atp_smoke.py DFEN` for debug output

---

## Phase 3 Validation: Execution Quality

### T-3.1: Opening Gap Capture
**What:** Verify gap capture rule fires for gap-up stocks at market open.
**How:** Run strategy generation between 9:30–10:00 AM. Look for:
```
  SELL SPY     rule=gap_capture              limit=$XXX.XX  3 chunk(s)
```
**PASS:**
- [ ] If any sell ticker gapped up >0.5% from prev_close, `gap_capture` rule fires
- [ ] Gap capture strategy produces exactly 3 chunks with different limit prices:
  - Chunk 0 (gap_capture): ~30% of shares at prev_close × 0.99
  - Chunk 1 (standard): ~50% of shares at midpoint
  - Chunk 2 (sweep): ~20% of shares at bid
- [ ] After 10:00 AM, re-running shows NO gap capture (market_minutes > 30)

**How to test without a gap:** Manually verify the rule logic:
```python
from engine.strategy_sell import generate_sell_strategy
# Create a synthetic gap scenario with market_minutes=10
# (see tests/test_optimizations.py::TestGapCapture for examples)
```

### T-3.2: Chunk Ordering (Largest First)
**What:** Verify chunks within each ticker are ordered largest → smallest.
**How:** Inspect the output JSON:
```python
import json
with open("today_premarket.json") as f:
    state = json.load(f)
for side in ["sell_chunks", "buy_chunks"]:
    # Group by (ticker, account) — NOT ticker alone.
    # Multiple accounts produce independent strategies for the same ticker;
    # mixing their chunks by ticker produces false FAIL on the ordering check.
    by_strat = {}
    for c in state["computed"][side]:
        key = c["ticker"] + "|" + c["account"]
        by_strat.setdefault(key, []).append(c)
    for key, cks in sorted(by_strat.items()):
        sizes = [c["shares"] for c in sorted(cks, key=lambda x: x["idx"])]
        ok = all(sizes[i] >= sizes[i+1] for i in range(len(sizes)-1))
        print(f"  {side} {key}  sizes={sizes}  largest_first={'PASS' if ok else 'FAIL'}")
```
**PASS:**
- [ ] All tickers (except gap_capture) have chunks ordered largest → smallest by idx
- [ ] Gap capture tickers maintain phase ordering (gap/standard/sweep)

---

## Phase 4 Validation: Same-Day Completion

### T-4.1: Buy-Side Urgency Escalation
**What:** Verify urgency escalates as the day progresses.
**How:** Run strategy generation at multiple times:
```bash
# 10:00 AM (30 min in): no escalation expected
python -m cli.strategy --state today.json --export today_10am.json
# 12:00 PM (150 min in): patient → normal
python -m cli.strategy --state today.json --export today_12pm.json
# 2:30 PM (300 min in): → aggressive
python -m cli.strategy --state today.json --export today_230pm.json
```
**PASS:**
- [ ] 10:00 AM: buy urgencies match the base rule (no escalation reasoning bullets)
- [ ] 12:00 PM: any `patient` buys escalated to `normal`, reasoning includes "Urgency escalation: 150 min"
- [ ] 2:30 PM: all buys show `urgency=aggressive`, limit prices at or above ask

### T-4.2: Sell-Before-Buy with Confirmed Proceeds
**What:** Verify buy budgets adjust from actual sell proceeds.
**How:** After sells fill, record actual proceeds and re-run:
```bash
# Suppose Roth IRA sells filled for $12,500 (estimated was $12,000)
python -m cli.strategy --state today.json --export today_adjusted.json \
  --confirmed-proceeds '{"Roth IRA": 12500.00}'
```
**PASS:**
- [ ] Output shows `Confirmed proceeds for Roth IRA: $12,500.00 (est $12,000.00, ratio 1.0417)`
- [ ] Buy dollar_target values in output JSON are scaled by the ratio
- [ ] share_target recalculated to match new dollar_target / limit_price

### T-4.3: Portfolio Buy Progress Tracker
**What:** Verify the progress tracker reports completion vs time.
**How:**
```bash
python -m cli.progress --state today.json
```
**PASS:**
- [ ] Shows trading day progress % matching current time
- [ ] Shows per-buy progress (0% if no fills recorded yet)
- [ ] "Behind schedule" flags appear for buys with 0% filled when >50% of day elapsed

---

## End-of-Day Validation

### E-1: Full Pipeline Sanity Check
**What:** Complete end-to-end pipeline produces valid state.
**How:** After all trades execute:
```python
import json
with open("today.json") as f:
    state = json.load(f)

# Verify all expected fields populated
assert state["computed"]["sell_strategies"]
assert state["computed"]["buy_strategies"]
assert state["computed"]["sell_chunks"]
assert state["computed"]["buy_chunks"]

# Verify chunk IDs cross-reference correctly
all_chunk_ids = {c["chunk_id"] for c in state["computed"]["sell_chunks"] + state["computed"]["buy_chunks"]}
strat_chunk_ids = set()
for s in state["computed"]["sell_strategies"] + state["computed"]["buy_strategies"]:
    strat_chunk_ids.update(s["chunk_ids"])
assert strat_chunk_ids == all_chunk_ids, f"Orphaned chunks: {all_chunk_ids - strat_chunk_ids}"
```

### E-2: Capture Test Artifacts
Save the following for regression testing:
- [ ] `tests/fixtures/YYYYMMDD_state.json` — compute output
- [ ] `tests/fixtures/YYYYMMDD_strategies.json` — strategy output
- [ ] `logs/YYYYMMDD_premarket_strategy.txt` — pre-market terminal output
- [ ] `logs/YYYYMMDD_market_strategy.txt` — in-market terminal output
- [ ] Screenshot of FT+ at each test point

---

## Use Cases Summary

| ID | Use Case | Optimization Tested | When to Test |
|----|----------|-------------------|-------------|
| T-1.1 | Per-symbol vol drives different chunk sizes | Realized vol | Any time |
| T-1.2 | Chunk sizes adjust for time of day | Volume profile | Multiple times |
| T-1.3 | Leveraged ETFs don't all trigger "wide spread" | SpreadContext | Any time |
| T-1.4 | VWAP rules improve limit pricing | VWAP benchmark | After 10:00 AM |
| T-2.1 | Thin tickers flagged for L2 monitoring | Thin detection | Pre-market |
| T-2.2 | Real L2 depth improves chunk sizing | L2 OCR | During market |
| T-3.1 | Gap-up stocks get aggressive opening pricing | Gap capture | 9:30–10:00 AM |
| T-3.2 | Largest chunks execute first (most liquid) | Chunk ordering | Any time |
| T-4.1 | Buy urgency increases as day progresses | Escalation | 10am/12pm/2:30pm |
| T-4.2 | Actual proceeds improve buy budgets | Confirmed proceeds | After sells fill |
| T-4.3 | Behind-schedule buys are identified | Progress tracker | Mid-day |
