# 10 — Strategy Engine: Design, Inputs, Logic, and Roadmap

**Status:** 2026-06-02. Design reference for the order-recommendation brain of
`fidelity_rebalancer`. Written to support the decision (next-window priority #4)
to grow the strategy engine into the system's single decision layer — the place
that reviews every input and recommends every order, **subsuming the execution
scheduler (chunk 8)** rather than running it as a separate stage.

**Hard rule (unchanged):** the engine NEVER places, modifies, or cancels
orders. It produces a plan a human enters manually.

---

## 1. What the strategy engine is

The strategy engine answers **"how do I trade this?"** It sits between the
**calculator** (which decides _what_ to trade — dollar/share targets) and the
**TUI / monitor** (review, approval, fill tracking). For each order the
calculator hands it, the engine:

1. reads the live market for that symbol,
2. derives features (spread, liquidity, momentum, time of day),
3. picks a pricing rule → a **limit price + urgency + plain-English reasoning**,
4. slices the order into **chunks** ("clips") sized to avoid moving the market.

Code map:

| Layer               | File                                 | Responsibility                                                             |
| ------------------- | ------------------------------------ | -------------------------------------------------------------------------- |
| What to trade       | `engine/calculator.py`               | Sells (liquidate rotated-out tickers) + buy dollar targets; `cash_ok` gate |
| Drift allocator     | `engine/optimizer.py`                | `live_buys` — minimize tracking-error vs target weights                    |
| How to price (sell) | `engine/strategy_sell.py`            | 8 sell rules → limit + urgency                                             |
| How to price (buy)  | `engine/strategy_buy.py`             | 5 buy rules + time-of-day escalation                                       |
| Spread calibration  | `engine/spread_context.py`           | Per-symbol "tight/wide" thresholds                                         |
| How to slice        | `engine/chunker.py`                  | Book-relative / POV / gap-capture chunkers; tick + vol-profile helpers     |
| Orchestration       | `cli/strategy.py`                    | Fetch market data, loop orders, call generators, write back to state JSON  |
| Fill monitor        | `engine/stall.py` + `tui/monitor.py` | Stall detection + re-quote advice                                          |

---

## 2. Inputs

### 2.1 Configuration / static (gitignored)

- **`accounts.json`** → per account: `strategy_allocations` (target weights),
  `type` (retirement/taxable), `margin` (bool), `cash_reserve`, `cash_spaxx`.
- **`signals.json`** → SectorSurfer rotations per strategy (`current` →
  `new` ticker) plus `prev_closes` fallback prices.
- **Fidelity CSV exports** → current positions (symbol, qty, value, price),
  SPAXX cash. Parsed by `calculator.parse_csv` / `consolidate`.

### 2.2 Live market data (per symbol)

Fetched by `cli/strategy.py` from one of two sources:

- **yfinance (default)** — bid/ask, last, prev_close, volume, ADV (10d/90d),
  div ex-date. **No Level 2 depth, no VWAP.**
- **ATP OCR (`--source atp`)** — Fidelity Trader+ Watchlist via OCR: adds
  **Level 2 depth book** and **intraday VWAP**. `--strict-atp` stops (exit 3)
  rather than silently falling back when OCR is incomplete.

### 2.3 Derived features

- `spread_bps` and a per-symbol **`SpreadContext`** (tight = 0.7×typical,
  wide = 1.5×typical; typical from live bid/ask or an asset-class bucket —
  large_cap 3 / sector 5 / international 8 / leveraged 20 / fixed_income 4 bps).
- `rel_vol` (session volume ÷ ADV), `pct_of_adv` (order shares ÷ ADV).
- `day_change_pct` vs **ex-dividend-adjusted** prev close.
- `vwap` (ATP only).
- `market_minutes` (minutes since 9:30 ET) → time-of-day logic.
- realized daily **sigma** (bps) → square-root-law market-impact estimate.
- `vol_profile_multiplier(hour, minute)` → intraday volume U-curve scaler.

### 2.4 Data-source ladder — the engine always runs, only depth changes

A common misconception: "no ATP (or no VWAP) means no strategy engine." **Not
so.** Once invoked, the engine always produces a fully priced, fully chunked
plan; thinner data just means some rules stay dormant and it falls back to a
coarser chunker. yfinance is **not** a VWAP fallback — VWAP comes _only_ from
ATP; without ATP there is simply no VWAP.

| Data available                     | What it enables                                 | What degrades                          | Engine runs?     |
| ---------------------------------- | ----------------------------------------------- | -------------------------------------- | ---------------- |
| **ATP OCR** (L2 + VWAP)            | all ~13 rules; book-relative chunker            | —                                      | **Yes**          |
| **yfinance** (no L2, no VWAP)      | ~11 rules; **POV / sqrt-law** chunker (no book) | VWAP rules (sell 6/7, buy 4/5) dormant | **Yes**          |
| **prev_close only** (signals.json) | default rule, zero-spread quote                 | most feature rules; POV chunking       | **Yes** (coarse) |

**Two gates are the _only_ ways "no engine ran" happens — neither is a data
requirement:**

1. **Invocation gate (workflow).** The engine = `cli.strategy`. In the daily
   flow it sits behind morning-prep **Step 5** (`Run order-sizing preflight
now? [Y/n]`). Answer `n` → sizing skipped → plan stays _unsized_ (dollar
   targets only, no rule-based limits/chunks). About _whether_ you run sizing,
   not about data.
2. **Strict halt (`--strict-atp`, one wrapper only).** Morning-prep → preflight
   builds `cli.strategy … --source atp --strict-atp`. `--strict-atp` means _stop
   (exit 3) if the live OCR read is incomplete_ — a deliberate "pause and fix
   FT+" safety choice, **not** an engine limitation. The same engine called
   without `--strict-atp` degrades (per the table) instead of stopping.

Precisely: _if invoked and not under `--strict-atp`, the engine always runs to
completion and only input depth differs._

---

## 3. The logic, in plain terms (trading best practices)

### 3.1 Budget — _what and how much_ (`calculator.py`)

- **Sells:** for each strategy whose signal rotated to a new ticker, sell the
  **entire** current position. Limit = prev close (refined later by pricing).
- **Buys:** deploy `sell proceeds + deployable cash` into the rotated-in
  tickers. Four funding branches:
  1. no rotations + enough cash → pure rebalance toward target weights;
  2. rotations + **not** enough cash → split available funds across the new
     tickers by weight;
  3. rotations + enough cash → fund new tickers to target, then top up holders;
  4. nothing fits → no buys.
- **Cash gate (`cash_ok`)** = deployable cash can afford ≥1 share of every
  strategy's ETF. Margin (taxable) accounts are _not_ blocked by this — buying
  power covers same-window buys (the 2026-06-02 fix).
- Final **scale-down**: if buy targets exceed available funds, shrink
  proportionally, then floor to whole shares.
  _Known limitation (B-7): the scale-down caps to `proceeds + cash` even for
  margin accounts, so they can under-buy vs. true buying power._

### 3.2 Drift allocator (`optimizer.py`)

`live_buys` does a two-phase allocation: a proportional floor, then a greedy
"add one share where it most reduces drift from target weight" loop. This is
the engine's tracking-error minimizer and the intended home for the **live
recompute** once sells actually fill (see gap F-1).

### 3.3 Sell pricing rules (first match wins)

0. **Gap-capture** (gapped up >0.5% in first 30 min): 3-phase exit —
   30% near prev_close×0.99, 50% standard, 20% sweep at bid. _Sell into the
   pop without dumping._
1. **Tight spread + healthy volume + small position** → midpoint. _Cheap, liquid
   — split the spread._
2. **Tight spread + large position (>5% ADV)** → sit at the bid, more chunks.
   _Don't push the market down._
3. **Wide spread** → bid + 1 tick, patient. _Don't pay up to cross._
4. **Down day (< −2%)** → prev_close×0.99, patient. _Wait for a bounce._
5. **Up day (> +2%)** → hit the bid, aggressive. _Sell into strength._
   6/7. **Above / below VWAP** → take the fill above VWAP; be patient below. _(ATP only.)_
   Default → midpoint, normal.

### 3.4 Buy pricing rules + time escalation

1. **Tight + liquid** → take the ask (fill fast).
2. **Wide spread** → midpoint, patient.
3. **Large position (>3% ADV)** → ask − 1 tick, half-size chunks.
   4/5. **Below / above VWAP** → favorable at ask / patient at midpoint.
   Default → ask.

- **Time-of-day escalation** (buy side only): as the session runs out the limit
  ratchets toward — then past — the ask (90 min → normal, 210 min → at ask,
  330 min → ask + 1 tick) to guarantee a same-day fill. _Sells have no symmetric
  escalation — they rely on day-change thresholds only (gap G-4 below)._

### 3.5 Chunking (`chunker.py`)

Each clip is capped so it is **never more than ~25% of visible top-3 depth**
nor **~15% of recent 5-minute volume**, scaled by the intraday volume profile,
and rounded to 100-share lots. Three chunkers:

- **book-relative** (default, when L2 depth is available);
- **POV / square-root-law** (`build_chunks_pov`, when L2 is missing — e.g.
  yfinance source) — picks clip count from a participation tier + impact-bps;
- **gap-capture** (the 3-phase split for sell rule 0).

---

## 4. Where it sits — workflow

```
            accounts.json   signals.json   Fidelity CSVs
                    \            |            /
                     v           v           v
        ┌──────────────────────────────────────────────┐
        │ cli/compute.py  →  calculator.calc_trades     │  WHAT to trade
        │   sells, buy dollar targets, cash_ok          │  (state.json)
        └──────────────────────────────────────────────┘
                                |
                                v
        ┌──────────────────────────────────────────────┐
        │ cli/strategy.py  → strategy_sell / strategy_buy│  HOW to trade
        │   live data (yfinance | ATP OCR) → rules →     │  (limit, urgency,
        │   limit + urgency + reasoning → chunker        │   chunks, reasoning)
        └──────────────────────────────────────────────┘
                                |
                                v
        ┌──────────────────────────────────────────────┐
        │ cli/preflight.py (sanity gate)  →  tui/app.py  │  REVIEW & approve
        │   human approves → plan_*.json + plan_*.txt    │
        └──────────────────────────────────────────────┘
                                |
                                v
        ┌──────────────────────────────────────────────┐
        │ Human enters orders in ATP                     │  EXECUTE (manual)
        │ tui/monitor.py + engine/stall.py watch fills,  │
        │   suggest re-quotes, recompute buys            │
        └──────────────────────────────────────────────┘
```

The engine is the **second stage** today. The forward design (section 6) pulls
all _temporal / funding / urgency_ decisions — currently scattered across
gap-capture, buy escalation, vol-profile, POV tiers, and the proposed chunk-8
scheduler — **into the strategy engine** so there is one decision layer.

**How the engine is actually reached in the daily flow.** `cli.compute`
(stage 1) does _not_ touch the engine — it only produces dollar/share targets.
The engine runs only when `cli.strategy` is called, which in practice happens
via **`scripts/morning-prep.ps1` Step 5 → `cli.preflight` →
`preflight/orchestrator.build_sizing_command` →
`cli.strategy … --source atp --strict-atp --l2-symbols`** (subprocess).
Consequences worth remembering: (a) it is _opt-in_ — Step 5's `[Y/n]` prompt can
skip sizing entirely; (b) the daily path is **ATP-sourced**, so L2 + VWAP are
present (this is why G-3's VWAP dormancy doesn't bite normal use — see §2.4);
(c) `--strict-atp` will _halt_ on incomplete OCR rather than degrade. A bare
`cli.strategy --source yfinance` run hits none of these — it just degrades.

---

## 5. Why the engine should subsume the scheduler

Timing logic is already in the engine, just fragmented:

- **sell rule 0** = a premarket/opening tranche policy;
- **`_escalate_buy` checkpoints** = an intraday EOD-ramp policy;
- **`vol_profile_multiplier`** = an intraday participation policy;
- **POV tiers** = a "spread this across the day/days" policy.

The chunk-8 scheduler (`08_execution_scheduler.md`) proposes a _separate_ stage
for premarket/main/sweep tranches and settlement gating. Splitting timing across
two stages would duplicate the time-of-day and funding concepts. **Recommendation:
fold the scheduler's tranche/settlement/EOD-ramp concerns into the strategy
engine as a unified "decision context"** (time of day + account funding state +
liquidity), and let the rules emit phased chunks with `earliest_entry` /
`funded_by` fields directly.

---

## 6. Plan of execution for updates

Ordered, each step shippable and testable on its own:

1. **Unify the decision context.** Introduce a single `DecisionContext`
   (market_minutes, session phase, account type+margin, settlement state,
   spread_ctx, sigma, vwap). Thread it through both generators in place of the
   current loose kwargs. _No behavior change; pure refactor + tests._
2. **Surface reasoning (B-2/G-1).** Promote the `reasoning` bullets from DEBUG
   to a user-visible field in the TUI and TXT checklist. Cheap, high value.
3. **Symmetric sell escalation (G-4).** Give sells the same time-of-day ramp the
   buys have (patient → normal → aggressive as the close approaches).
4. **Calibrate position-size thresholds (G-5).** Replace hardcoded 2/5/3 %-ADV
   cutoffs with per-asset-class values (mirror `SpreadContext`).
5. **Live recompute (F-1).** Implement `optimizer.recompute_buys(state,
actual_proceeds)` and wire the monitor's recompute trigger to it.
6. **Re-quote via the rules (F-6).** Make `recommend_requote` call back into the
   pricing rules with a fresh quote instead of the standalone ±5-tick clamp, so
   re-quotes stay consistent with the original strategy.
7. **Absorb the scheduler (section 5).** Add `phase` / `earliest_entry` /
   `funded_by` to `ChunkRecord`; emit phased chunks from the engine; settlement
   gating becomes a `DecisionContext` input. Retire the standalone chunk-8 stage.
8. **VWAP source (G-3).** Decide whether VWAP-dependent rules are ATP-only by
   contract, or compute an approximate VWAP for the yfinance source.

---

## 7. Gap analysis & recommendations

| #                            | Gap                                                                                                                                                                                                              | Impact                                                                 | Recommendation                                                 |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- | -------------------------------------------------------------- |
| **G-1/B-2**                  | Reasoning bullets routed to DEBUG; user never sees _why_ a price was chosen                                                                                                                                      | Trust/auditability                                                     | Surface in TUI + TXT (exec step 2)                             |
| **G-3** _(low daily impact)_ | VWAP rules (sell 6/7, buy 4/5) only fire on ATP; yfinance returns `None`. **But the daily flow is ATP-sourced (§4), so VWAP is present in normal use** — dormancy only hits a standalone `--source yfinance` run | Clarity, not daily fills                                               | Make ATP-only explicit, or approximate VWAP from intraday bars |
| **G-4**                      | Buys escalate by time of day; **sells do not** (only day-change thresholds)                                                                                                                                      | Asymmetric end-of-day fill risk on sells                               | Add symmetric sell escalation (exec step 3)                    |
| **G-5**                      | %-ADV cutoffs (2/5/3%) hardcoded; not calibrated per asset class like spread is                                                                                                                                  | Mis-sizes leveraged vs large-cap                                       | Per-class thresholds (exec step 4)                             |
| **G-6**                      | Two ADV notions coexist: `get_adv` (30d yfinance, lru_cache) vs watchlist `avg_vol_10d` passed by the CLI                                                                                                        | Inconsistent `pct_of_adv`                                              | Pick one ADV definition; document it                           |
| **F-1**                      | `optimizer.recompute_buys` does not exist; monitor's recompute trigger only logs proceeds                                                                                                                        | Buy targets stay static after sells fill — chunk-6 acceptance #2 unmet | Implement + wire (exec step 5)                                 |
| **F-6**                      | `recommend_requote` uses a standalone ±5-tick clamp, ignoring the rule that priced the original order                                                                                                            | Re-quote can contradict strategy                                       | Re-price via the rules (exec step 6)                           |
| **S-1**                      | Scheduler timing logic fragmented across 4 modules + a proposed separate stage                                                                                                                                   | Duplication, drift                                                     | Subsume scheduler into the engine (exec step 7)                |
| **T-1**                      | No tax awareness for taxable accounts (wash-sale, lot selection)                                                                                                                                                 | Real tax cost on the TOD account                                       | Track as future (Phase B+); out of current scope               |
| **C-1**                      | No cross-order / cross-account coordination — every order priced independently                                                                                                                                   | No portfolio-level sequencing beyond settlement                        | Address as part of the unified decision context (step 1/7)     |
| **B-7**                      | `calculator.py` scale-down caps margin buys to `proceeds+cash`                                                                                                                                                   | Margin accounts under-buy                                              | Needs a buying-power figure (declined for now)                 |

**Live-test before building (LT-2):** run `cli.strategy --source atp
--l2-symbols …` and `--source yfinance` and confirm which rule branches fire,
that sigma/ADV/L2/VWAP populate as expected, and that VWAP branches correctly
no-op on yfinance — to refresh ground truth before the exec-plan changes.

---

## 8. Cross-references

- `08_execution_scheduler.md` — the standalone scheduler design this doc
  recommends absorbing (section 5).
- `09_roadmap.md` — sections F (monitor) and G (strategy) track these items;
  next-window priority #4 is this engine.
- `06_monitor_loop.md` — defines the recompute + re-quote contract (F-1, F-6).
