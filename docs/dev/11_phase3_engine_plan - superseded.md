# 11 — Phase 3: Strategy Engine — Development & Parallel-Execution Plan

**Status:** 2026-06-03. The build plan for growing the strategy engine into the
system's single decision layer. Executes the 8-step "Plan of execution" in
`10_strategy_engine.md §6` and the section-F/G/A items in `09_roadmap.md`.

**Hard rule (non-negotiable):** the app NEVER places, modifies, or cancels
orders. It only produces a plan a human enters manually in ATP.
`accounts.json` / `state.json` / `signals.json` / `plans/` stay gitignored and
are never committed. Fixtures use sanitized data only (account mask `*0000`,
synthetic order IDs) — no real account data in tracked files.

---

## 0. Scope & terminology (validated with owner 2026-06-03)

"Phase 3" (owner's term) = the **entire strategy-engine arc** = the 8 execution
steps of `10_strategy_engine.md §6`. This is distinct from `09_roadmap.md`'s
*internal* "Phase 0/1/2/3" sub-labels for that same arc — this document is the
single plan for the whole arc and supersedes those sub-labels.

Validated decisions:

| Decision | Choice |
| --- | --- |
| **Scope** | Full 8-step arc **including S-1** (scheduler absorption). |
| **LT-2 gate** | Do **not** block on it. Start the pure-refactor + independent items now (none need live data); slot LT-2 in to validate behavior changes before they merge, when a trading window is available. |
| **DecisionContext (step 1)** | **Pure refactor** of today's loose kwargs. No new fields, no behavior change. Scheduler fields are added later *with* S-1. |
| **Execution model** | Write the plan, then run it across **parallel agent lanes** (see §3). |

`step 2` (surface reasoning, B-2/G-1) is **already done** — `tui/presenter.py:302`
renders a Reasoning panel from `strategy.reasoning`, and `cli/strategy.py`
appends POV bullets. Remaining work there is only to confirm the TXT checklist
path also prints reasoning; folded into Lane A as a verification task.

### Resolved design decisions (the S-1 §7 forks + behavior semantics)

| # | Question | Resolution |
| --- | --- | --- |
| Premarket window | When is "capture-stupid"? | **Premarket → first 30 min (9:30–10:00)**. Owner focuses on the 9:30 open; pre-open resting is optional/secondary. |
| Capture anchor | Limit relative to what? | **Anchored to prev_close**, `prev_close × (1 − capture_offset_pct)`. Generalizes the *existing tested* `gap_capture` phase-1 (`prev_close × 0.99`). Owner practice: 0.75% min → 1.5% max below close. **Default 1.0%.** ⚠ Sign/side convention is **review-gated** (see §2 step 7). |
| Capture size | Fixed % or volume-driven? | **Volume-driven, capped.** `premarket_pct` (default max 40%) and the offset are **maximums**; actual clip sized from premarket + early-session volume via the existing chunker volume-relative caps. |
| EOD ramp trigger | When does the sweep start? | **Both, whichever first** — clock (default 15:00 ET) OR unfilled-fraction threshold. |
| IRA buy funding | How to gate? | **Clock-gate now** (noon), **event-gate later** (`funded_by` after matching sells fill, once OCR fill data is wired). |
| F-1 recompute depth | Resize or re-optimize? | **Re-run `optimizer.live_buys`** drift allocator on the realized pool (proceeds + deployable cash). |
| F-1 recompute trigger | When? | **Reuse the EOD-sweep predicate** (clock-OR-fill-fraction, whichever first) per account — unifies the temporal logic in one place. |
| F-6 re-quote | Re-price rule or re-select? | **Re-run full rule selection** against the fresh quote (the winning rule may change). |
| G-3 VWAP | ATP-only or approximate? | **Approximate VWAP** from yfinance intraday bars so VWAP rules also fire on the yfinance path. |
| Label vocab (default) | gap_capture vs premarket? | **Unify on `premarket / main / sweep`** end-to-end; map the old `gap_capture/standard/sweep` phase labels to it. |
| Config home (default) | Where do new knobs live? | **`inputs.config`** (extend `EngineConfig`) alongside the existing chunker knobs. |

---

## 1. Current code seams (verified, not assumed)

- **Generators** `engine/strategy_sell.py` / `engine/strategy_buy.py`:
  `generate_*_strategy(...)` take loose kwargs `adv`, `spread_ctx`, `vwap`,
  `market_minutes` plus chunker caps. `_decide(...)` is the first-match rule
  selector. Buys have `_escalate_buy(...)` time-of-day ramp; **sells do not**
  (G-4). %ADV cutoffs `2.0/5.0` (sell) and `3.0` (buy) are **hardcoded** (G-5).
- **Call sites** `cli/strategy.py:638` (sell) and `:720` (buy) build
  `spread_ctx = spread_context_for(...)`, `vwap`, `market_minutes=mkt_minutes`
  and pass them as kwargs. These are the seams DecisionContext consolidates.
- **Optimizer** `engine/optimizer.py`: `live_buys(candidates, actual_avail,
  total_pool, strategies)` is the two-phase drift allocator. `recompute_buys`
  **does not exist** (F-1).
- **Stall** `engine/stall.py`: `recommend_requote(stall, side, quote)` uses a
  standalone `±5-tick` clamp — does **not** call the pricing rules (F-6).
- **Spread calibration** `engine/spread_context.py`: `SpreadContext` +
  `_ASSET_CLASS_TYPICAL` + `_TICKER_CLASS` buckets. **This is the pattern G-5
  mirrors** for %ADV thresholds.
- **Chunker** `engine/chunker.py`: `build_gap_capture_chunks(...)` already emits
  a `"phase"` field and a `_GAP_PHASE_SPLIT = (0.30,0.50,0.20)`. **Generalize
  this** for S-1, don't write a parallel mechanism.
- **Schema** `state/schema.py`: `ChunkRecord` = `{chunk_id, account, strategy,
  ticker, idx, shares, limit_price, cost}` (S-1 extends it). `EngineConfig`
  (S-1 extends). `ExecutionState.actual_proceeds_by_account: dict[str,float]`
  (F-1 reads). `Computed.cash_ok` / `one_share_total` are per-account dicts.
- **Monitor** `tui/monitor.py`: `_do_poll` recompute trigger currently only
  LOGS proceeds — no buy plan is recomputed (F-1).

---

## 2. The eight steps

Each step is shippable and independently testable. Run the suite from
`fidelity_rebalancer/` with `PYTHONPATH=.` (configfile is `pyproject.toml`).

### Step 1 — `DecisionContext` refactor *(keystone)*
- **Goal:** one object carrying the per-symbol decision inputs, threaded through
  both generators in place of loose kwargs. **Pure refactor — zero behavior
  change.**
- **Files:** new `engine/decision_context.py`; edit `engine/strategy_sell.py`,
  `engine/strategy_buy.py`, `cli/strategy.py`; `tests/test_decision_context.py`.
- **Shape:** `@dataclass(frozen=True) DecisionContext(market_minutes:int|None,
  spread_ctx:SpreadContext, vwap:float|None, adv:float|None, sigma_bps:float|None)`.
  `generate_*_strategy(record, quote, l2, vol5min, *, ctx, today=None, ...chunk
  caps)`. Internally `_features`/`_decide`/`_escalate_buy` read from `ctx`.
- **Acceptance:** all existing strategy tests pass **unchanged in expected
  values**; `cli.strategy` output byte-identical on a fixture before/after.

### Step 2 — Surface reasoning (B-2/G-1) — *DONE; verify only*
- Confirm the TXT checklist exporter prints `strategy.reasoning`. If missing,
  add it. Covered as a Lane-A verification task.

### Step 3 — Symmetric sell escalation (G-4)
- **Goal:** give sells the time-of-day ramp buys have (patient→normal→aggressive
  as the close nears).
- **Files:** `engine/strategy_sell.py` (+ shared helper extracted from
  `_escalate_buy`), `tests/test_strategy.py`.
- **Approach:** generalize `_escalate_buy` into a side-aware `_escalate(side,
  ...)`; sell escalation nudges the limit toward the **bid** (mirror of buy→ask).
  Reads `ctx.market_minutes`.
- **Acceptance:** a stalled-near-close sell escalates urgency + limit; pinned
  `market_minutes` cases (90/210/330) assert the ramp; no change before 90 min.

### Step 4 — Per-class %ADV thresholds (G-5)
- **Goal:** replace hardcoded `2/5/3` %ADV cutoffs with per-asset-class values,
  mirroring `SpreadContext`.
- **Files:** new `PositionSizeContext` (in `engine/spread_context.py` or a sibling
  `engine/size_context.py`), wired into both `_decide`s; `tests/`.
- **Approach:** reuse the `_TICKER_CLASS` buckets; per class define
  small/large %ADV cutoffs. Fold in **G-6** here: pick ONE ADV definition
  (`get_adv` 30d yfinance vs watchlist `avg_vol_10d`) and document it; make both
  generators consume the same source.
- **Acceptance:** a leveraged ETF and a large-cap at the same %ADV select
  different size rules; G-6 — one ADV path, asserted.

### Step 5 — Live recompute (F-1) *(independent)*
- **Goal:** `optimizer.recompute_buys(state, actual_proceeds)` + wire the monitor.
- **Files:** `engine/optimizer.py`, `tui/monitor.py`, `tests/test_optimizer.py`,
  `tests/test_monitor*.py`.
- **Approach:** rebuild the buy candidate pool from `state` using realized
  `actual_proceeds_by_account` + deployable cash, then **re-run `live_buys`** and
  write updated buy allocations/targets back into `state`. Monitor calls it when
  the **EOD-sweep predicate** fires for an account (clock-OR-fill-fraction,
  whichever first) — implement the predicate once and share it with S-1's sweep.
- **Acceptance:** realized proceeds < estimate → buy share targets shrink and
  re-minimize drift; trigger fires per the predicate, not on every partial fill;
  hard rule intact (no order I/O).

### Step 6 — Re-quote via the rules (F-6)
- **Goal:** `recommend_requote` re-runs **full rule selection** with a fresh
  quote instead of the ±5-tick clamp.
- **Files:** `engine/stall.py`, `tests/test_stall.py`.
- **Approach:** call back into the generator rule path (`_decide` via a thin
  shared entry) with the fresh quote + `DecisionContext`; the winning rule may
  change. Keep `RequoteSuggestion` shape; populate `rationale` from the chosen
  rule's reasoning.
- **Acceptance:** a book that moved tight→wide produces a re-quote priced by the
  new rule; `test_e2e_stall_detect_requote_recompute` still green.

### Step 7 — Absorb the scheduler (S-1) *(terminal arc; multi-session)*
- **Goal:** fold premarket/main/sweep tranches + settlement gating into the
  engine; retire the standalone chunk-8 stage.
- **Sub-steps (sequential, each ships):**
  1. **Schema + config.** Add to `ChunkRecord` (all optional/defaulted):
     `phase: Literal["premarket","main","sweep"]="main"`, `earliest_entry:str|None`,
     `funded_by:list[str]|None`, `account_type:Literal["taxable","retirement","margin"]|None`.
     Extend `EngineConfig` with `premarket_pct`, `capture_offset_pct`,
     `sweep_time`, `sweep_unfilled_frac`. `account_type` flows from `accounts.json`.
  2. **Scheduler (pure).** `engine/scheduler.py: build_day_schedule(order, *, ctx,
     now, ...) -> list[Tranche]` → flatten to phase-tagged `ChunkRecord[]`.
     `now` injected (deterministic; tests pin premarket/10:05/12:30/15:45).
     Reuse `build_*_chunks` to size *within* each tranche.
  3. **Capture-stupid (generalize `build_gap_capture_chunks`).** Window
     premarket→10:00. **SELL** anchor = `prev_close × (1 − capture_offset_pct)`
     (this IS the existing phase-1 with offset=1%). **BUY (taxable)** anchor = a
     below-close dip bid. Size = `min(premarket_pct, volume-relative cap)`.
     ⚠ **Review gate:** the sell/buy sign convention is confirmed with the owner
     at PR review before merge — owner deferred exact offsets to researched
     recommendation but will sanity-check the sign.
  4. **EOD sweep.** `should_sweep(now, unfilled_frac)` = `now ≥ sweep_time OR
     unfilled_frac ≥ sweep_unfilled_frac`. **Shared with F-1's recompute trigger.**
  5. **IRA funding gate.** Retirement buys clock-gated to noon now;
     `funded_by` populated but event-gating deferred to the OCR-fill phase.
  6. **Wire + surface.** `cli/strategy.py` calls `build_day_schedule` behind a
     `--schedule` flag (old single-window path stays default until proven);
     TXT/TUI render chunks grouped by tranche. Verify `_reconcile_records_to_chunks`
     still holds across phases.
- **Acceptance:** `--schedule` off → byte-identical to today; on → tranche split,
  windows, postures, `phase` tags asserted per side/account-type; sum of tranche
  shares == record total; every order has a same-day sweep (no overnight carry).

### Step 8 — Approximate VWAP for yfinance (G-3)
- **Goal:** VWAP rules also fire on `--source yfinance`.
- **Files:** new helper (e.g. `adapters/yfinance_fallback.py` or
  `engine/vwap.py`), `cli/strategy.py` yfinance branch, tests.
- **Approach:** compute approximate intraday VWAP from yfinance 1-min bars; feed
  the existing `vwap` input. Document it as *approximate* and distinct from ATP's
  exact intraday VWAP.
- **Acceptance:** yfinance path populates a non-None VWAP; sell 6/7 / buy 4/5 can
  fire; ATP path unchanged.

---

## 3. Dependency graph & parallel execution

```
            ┌─ Lane A: Step 1 DecisionContext (keystone) ─┐
 Wave 1     │                                             │   (no file overlap)
 (parallel) └─ Lane B: Step 5 F-1 recompute_buys ─────────┘
                          │ step 1 merges to develop
                          ▼
            ┌─ Lane C: Step 3 (G-4) + Step 6 (F-6) ───────┐  (both touch generators
 Wave 2     │                                             │   + stall → one lane)
 (parallel) └─ Lane D: Step 4 (G-5+G-6) + Step 8 (G-3) ───┘  (size/adv/vwap data path)
                          │ waves 1–2 merge; LT-2 validates behavior
                          ▼
 Wave 3     └─ Lane E: Step 7 S-1 scheduler arc (sequential sub-steps) ─┘
```

- **Wave 1 is truly parallel** — Lane A edits `strategy_*`/`cli.strategy`; Lane B
  edits `optimizer.py`/`monitor.py`. Disjoint files. (Roadmap confirms F-1 "can
  run in parallel with the refactor.")
- **Waves 2 & 3 rebase onto step 1** (they consume `DecisionContext`).
- Each agent works in an **isolated git worktree**, commits to its own branch,
  runs the suite to green, and reports its branch + diff + test results. **I
  review every diff before merging to `develop`** — nothing auto-merges.
- **LT-2** runs after waves 1–2 land and before behavior changes are trusted in
  live use; it does not block the refactor/independent work.

---

## 4. Risks

- **Worktree/Windows git:** use PowerShell for git (Bash FS-overlay causes
  `.git/index.lock` ENOENT); run pytest from `fidelity_rebalancer/` with
  `PYTHONPATH=.`.
- **Capture-stupid sign** is real trading money — review-gated (§2 step 7.3).
- **`--schedule` default-off** until byte-identical regression proven, so S-1
  can't silently change today's output.
- **Shared sweep predicate** (F-1 ↔ S-1) is a coupling point: implement once,
  import in both, or Wave-3 must reconcile two copies.
