# Chunk 8 — Time-of-Day + Account-Type Execution Scheduler

**Status:** DESIGN ONLY — not yet built. Multi-session effort.
**Suggested model:** Opus (heaviest reasoning load in the project — adds a time/funding dimension to every order).
**Depends on:** chunks 4 (chunker) + 5 (preflight) complete; ties to roadmap item #1 (margin/settlement gate) and item #3 (calculator imports a recommended-orders JSON).
**Source:** user spec captured live during morning-prep on 2026-06-01 (see `07_prewindow_2026-05-29.md`, "Under Consideration" item #5). This doc supersedes that bullet.

> **SAFETY (unchanged, non-negotiable):** SecSurfTrade NEVER places, modifies, or cancels orders. Every order is entered by a human. This scheduler produces a _plan_ — "enter this chunk, at this limit, in this time window" — that the human reads and acts on. It does not connect to any order-entry API. Nothing in this chunk may add an execution path.

---

## 1. Problem

The chunker (`engine/chunker.py`) decides _how big_ each chunk is and _how many_ chunks an order splits into, but it has **no concept of _when_ in the day a chunk should be entered**, and no awareness of **account type / cash settlement**. The only time-awareness today is `vol_profile_multiplier()` — an intraday U-shape that nudges chunk _size_, not _timing_.

Consequences observed in live use:

- No premarket "capture-the-naive-order" tranche (sells into early overpricing; taxable buys into early underpricing).
- No gating that stops an IRA buy from being entered before its funding sells have settled.
- No end-of-day ramp, so the human can be left holding unfilled size near the close with no guidance on when to get aggressive.

The desired model treats a single rebalance order as a **sequence of tranches across the trading day**, each with its own time window, limit-price posture, and (for buys) a funding precondition.

## 2. Target execution model (user spec, 2026-06-01)

### Sells

| Tranche                    | Window                  | Limit posture                           | Notes                                                                                                                                        |
| -------------------------- | ----------------------- | --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Premarket "capture stupid" | premarket / at the bell | **above** fair value                    | Only fills if naive market orders overpay in the first ~30 min. A _portion_ of the order; the unfilled remainder rolls into the main window. |
| Main chunked window        | ~10:00–~13:00           | book-relative / POV (existing chunkers) | The bulk. High-quality-execution band — matches the `(10:00,11:30)=1.1x` "best execution" row already in `_VOL_PROFILE`.                     |
| Completion sweep           | late day (see ramp)     | priced to **bid**                       | Guarantees same-day fill on whatever is left.                                                                                                |

### Buys

| Tranche                                       | Window                                       | Limit posture        | Notes                                                                                                                                                               |
| --------------------------------------------- | -------------------------------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Premarket "capture stupid" — **TAXABLE ONLY** | premarket / at the bell                      | **below** fair value | Catch early underpricing. Retirement excluded premarket (no settled cash).                                                                                          |
| Main chunked window — **taxable**             | ~10:00 onward                                | book-relative / POV  |                                                                                                                                                                     |
| Main chunked window — **retirement (IRA)**    | ~noon onward **AND** funding sells available | book-relative / POV  | Buys are funded by the day's sells. Should ideally be sequenced _after the matching sells fill_, not merely after a clock time. Same constraint as roadmap item #1. |
| Completion sweep                              | late day                                     | priced to **ask**    | Same-day completion.                                                                                                                                                |

### End-of-day completion + "best outcome"

- As the session progresses, **ramp aggression** so every order completes **within one trading day** (no overnight carry).
- Bias toward **price early** (capture-stupid + patient mid-window L2 chunks) and toward **certainty-of-fill late** (sweep), with a smooth handoff so the human isn't stranded with unfilled size at 15:30.
- This generalizes `build_gap_capture_chunks`' existing 3-phase pattern (gap_capture / standard / sweep) from "market-open gap-up only" to "all orders, all day."

## 3. Existing building blocks to reuse / extend

- **`engine/chunker.py`**
  - `_VOL_PROFILE` + `vol_profile_multiplier(hour, minute)` — intraday bands already defined (open 1.8x, mid-morning 1.1x, lunch 0.6x, afternoon 1.2x, close 1.5x). The scheduler's time windows should align to these band boundaries.
  - `build_gap_capture_chunks(total_shares, gap_price, standard_price, sweep_price)` — already returns chunk dicts carrying a `"phase"` field (`gap_capture`/`standard`/`sweep`) with a `_GAP_PHASE_SPLIT = (0.30, 0.50, 0.20)`. **Generalize this into the full-day schedule** rather than writing a parallel mechanism.
  - `build_chunks_pov` / `build_sell_chunks` / `build_buy_chunks` — the per-window _sizing_ primitives. The scheduler decides _how much_ goes into each tranche; these decide _how it is chunked within_ that tranche.
- **`cli/strategy.py`** — the per-order dispatch loop. Gains a "tranche / time-phase" dimension: each order produces multiple `ChunkRecord` sets tagged by intended **entry window** and (for buys) **funding state**.
- **Schema (`state/schema.py`)** — `ChunkRecord` needs new fields (see §4).
- **`cli/preflight.py` + calculator** — present the phased schedule; ties to item #3 (a recommended-orders JSON the calculator imports and renders by tranche).

## 4. Schema changes (`state/schema.py`)

Add to `ChunkRecord` (all optional / defaulted so existing state files and tests keep loading):

```python
phase: Literal["premarket", "main", "sweep"] = "main"
# Account funding/timing gate for this chunk:
earliest_entry: str | None = None     # e.g. "premarket", "10:00", "12:00"
funded_by: list[str] | None = None    # for IRA buys: chunk_ids of sells that fund it
account_type: Literal["taxable", "retirement", "margin"] | None = None
```

`accounts.json` needs a per-account type/flag (this is **roadmap item #1** — do it as part of, or just before, this chunk):

```json
{ "name": "...", "type": "retirement" } // or "taxable" / "margin": true
```

`account_type` flows from there into each `ChunkRecord`.

> Note `build_gap_capture_chunks` already emits `"phase"` in its dict form; reconcile the label vocabulary (gap_capture/standard/sweep) with the new tranche vocabulary (premarket/main/sweep) so there is one set of names end-to-end.

## 5. Proposed architecture

Keep the existing sizing primitives; add a **scheduler layer** above them.

```
engine/scheduler.py   (NEW — pure, testable, no I/O)
    build_day_schedule(order, *, account_type, now, vol_profile, ...) -> list[Tranche]
        - splits the order into premarket / main / sweep tranches (% split by
          account_type + side), assigns each a time window + limit posture
        - calls the existing build_*_chunks to chunk WITHIN each tranche
        - returns Tranche objects -> flattened to ChunkRecord[] with phase tags
```

- **Pure and deterministic:** `now` (and any market-time inputs) are injected, never read from the clock inside the function — so tests pin behavior at premarket / 10:05 / 12:30 / 15:45 without monkeypatching.
- `cli/strategy.py` calls `build_day_schedule` per order instead of a single `build_*_chunks` call, then writes the tagged chunks into `state.computed.{sell,buy}_chunks`.
- The `_reconcile_records_to_chunks` step (already in `cli/strategy.py`) keeps the record totals equal to the sum of all tranche chunks — verify it still holds across phases.

## 6. Phased implementation plan

Build incrementally; each phase ships independently and leaves the tool usable.

1. **Schema + account types.** Add `accounts.json` account type/`margin` flag (roadmap #1) and the new `ChunkRecord` fields. No behavior change yet; everything defaults to `phase="main"`. Migrate/validate existing state.
2. **Scheduler skeleton (sells only, time-agnostic split).** `engine/scheduler.py` producing premarket/main/sweep tranches for sells with fixed % splits, reusing `build_sell_chunks`. Wire into `cli/strategy.py` behind a flag (e.g. `--schedule`) so the old single-window path stays default until proven.
3. **Buys + account-type gating.** Add taxable-premarket and retirement-noon rules. Clock-gate first (simpler); event-gating (`funded_by`) in a later phase.
4. **End-of-day ramp / sweep.** Generalize `build_gap_capture_chunks` Phase 3; add the time-or-fill-rate-triggered sweep.
5. **Surfacing.** Preflight order-book + calculator render chunks grouped by tranche/window (ties to item #3). The human sees "premarket: …", "10:00–13:00: …", "sweep after 15:00: …".
6. **Event-gated IRA funding (optional, last).** Sequence retirement buys after matching sells fill — requires fill data, which only exists once the human logs fills (ties to the OCR fill-auto-logging reducer).

## 7. Open design decisions (resolve before coding)

1. **Premarket order mechanics.** Does Fidelity Trader+ support resting extended-hours limit orders, or does "capture stupid" mean orders _entered at the 9:30 bell_? Confirm what ATP supports and exactly how the human enters them. This determines whether the premarket tranche is a real pre-open resting order or a 9:30 instruction.
2. **Capture-limit aggressiveness + size.** How far above/below fair value is the capture limit, and what % of the order goes premarket vs. held back? (Defaults: needs a config knob, e.g. `premarket_pct`, `capture_offset_bps`.)
3. **EOD ramp trigger.** Time-triggered (start sweeping at 15:00) or fill-rate-triggered (sweep whatever is unfilled by X)? Likely **both, whichever comes first.**
4. **Retirement-buy funding.** Clock-gate (noon) vs. event-gate (matching sells filled) vs. both. Event-gate is correct but needs fill data → defer to phase 6.
5. **Label vocabulary.** Reconcile `build_gap_capture_chunks`' `gap_capture/standard/sweep` with the new `premarket/main/sweep`.
6. **Config home.** New knobs (`premarket_pct`, `capture_offset_bps`, sweep trigger time) belong in `inputs.config` alongside the existing chunker config, not hard-coded.

## 8. Testing

- `tests/test_scheduler.py` (NEW): pin `now` at premarket / 10:05 / 12:30 / 15:45 and assert the tranche split, time windows, limit postures, and `phase` tags per side and per account type.
- Funding: assert IRA buys carry `funded_by` referencing the correct sell chunk_ids, and never schedule before the gate.
- Invariants: sum of tranche chunk shares == record share total (reconciliation holds); no chunk priced for overnight carry (every order has a same-day sweep tranche).
- Regression: with `--schedule` off, output is byte-identical to the current single-window path.

## 9. Out of scope (explicitly)

- Any automated order entry, cancel, or modify. (Hard rule.)
- Real-time fill polling beyond the human-logged fills already captured by the EOD report / future OCR reducer.
- Tax-lot selection (separate Phase B item).
