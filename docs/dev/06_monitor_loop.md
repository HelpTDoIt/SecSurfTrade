# Chunk 6 — Monitor Loop with Stall Detection and Re-Quote Suggestions

**Suggested model:** Sonnet 4.6
**Depends on:** chunk 3 complete (chunk 4 helpful but not strictly required if you bypass strategy display)
**Estimated effort:** 2 hours

---

## Goal

Build the live monitor: a Textual screen that polls ATP Orders every 30–60 seconds, displays fill progress, detects stalled clips, and surfaces re-quote suggestions. When sells complete, the buy budget is recomputed from realized proceeds and the updated plan is shown.

## Read first

1. `ARCHITECTURE.md`, especially **Stall detection and re-quote suggestion** and **Polling cadence**.
2. The status display mockup in the original `claude_code_prompt.md` Phase 5.

## Scope

**In scope:**
- `engine/stall.py` — pure-logic stall detector. Function:
  ```python
  def detect_stalls(orders: list[OrderRow], threshold_seconds: int, now: datetime
                  ) -> list[StallEvent]
  ```
  Returns one `StallEvent` per stalled clip with: `chunk_id`, `original_limit`, `filled_qty`, `remaining_qty`, `seconds_stalled`. Stall = `status == PartiallyFilled` AND `(now - last_progress_at) >= threshold_seconds`.
- `engine/stall.py` also has:
  ```python
  def recommend_requote(stall: StallEvent, side: Literal["buy","sell"],
                        quote: QuoteSnapshot) -> RequoteSuggestion
  ```
  Logic:
  - **Sell side**: new_limit = max(quote.bid + tick(quote.bid), original_limit − 5×tick). I.e. tighten toward the new bid but don't chase too aggressively.
  - **Buy side**: new_limit = min(quote.ask − tick(quote.ask), original_limit + 5×tick).
  - Returns `RequoteSuggestion(chunk_id, new_limit, remaining_qty, rationale: list[str])`. Rationale includes original limit, current bid/ask, why the new limit was chosen.
- `tui/monitor.py` — Textual screen. Layout matches the Phase 5 mockup:
  ```
  EXECUTION STATUS — 10:35 AM                    [Q = QUIT]
  ─────────────────────────────────────────────────────────
  ROTH IRA
    SELLS
    ├─ EEM  1,600/1,655 filled (97%)  avg $62.41  proceeds $99,856.00
    │       Order #1: 1,600 shs FILLED @ $62.41
    │       Order #2:    55 shs OPEN limit $62.39 (bid $62.37)
    └─ AOR  0/1,843 filled (0%)
            Order #1: 1,500 shs OPEN limit $67.29 (bid $67.25)
    BUYS  (Budget: $99,889.88 = $99,856.00 funds + $33.88 cash)
    ├─ EWY  WAITING — sells not complete
    └─ SPY  n/a — recalculates when all sells done
  ─────────────────────────────────────────────────────────
  Next check: 10:35:45    Press R to refresh now    Q to quit
  ```
- The monitor polls `OrdersAdapter.read_orders()` every `polling_seconds` (default 45). Optional manual refresh on `R` keypress.
- When a stall is detected, the row turns yellow and the bottom of the screen shows:
  ```
  ⚠  STALL: Roth IRA EEM clip #2 has been PartialFill for 5m 12s
            Original limit $62.39  Current bid $62.37
            Suggested re-quote: $62.38  (bid + 1 tick)
            [C] Mark cancelled & re-quoted    [I] Ignore
  ```
- `[C]` action: marks the original chunk cancelled in state, creates a new chunk record at the suggested limit with the remaining qty, writes a journal entry, and updates the TXT checklist (re-export). The human cancels in ATP and enters the new order. The next poll will pick up the new order if/when it appears.
- `[I]` action: snoozes the stall alert for that chunk for 60 seconds.
- **Live budget recompute**: when all sells for an account reach `Filled` (or `Cancelled`), the monitor calls back into the engine's `optimizer.recompute_buys(state, actual_proceeds)`. The buy plan's `dollar_target`s are recalculated from realized proceeds. Updated buy chunks display in the BUYS section.
- Journal: every poll, every state change, every stall event, every re-quote action appends a JSON line to `logs/journal.jsonl`. Format: `{ts, event_type, payload}`.
- Tests:
  - `tests/test_stall.py` — stall detector against fixture order rows with various `last_update_at` values.
  - Re-quote suggestion math (sell and buy sides, including the 5×tick clamp).
  - End-to-end mock test: feed the mock ATP a sequence of order updates over simulated time; assert the monitor logic transitions correctly through Open → PartiallyFilled → Stall → Re-quote → Filled.

**Out of scope:**
- Auto-cancellation in ATP (Phase B+)
- Auto-re-placement in ATP (Phase B+)
- Account-level kill switch (no orders being placed by us today)
- Multi-account concurrent display polish — must show all 3 accounts but layout can be vertical-stacked, no need for tabs

## Implementation notes

- Use Textual's `set_interval` for the polling loop, not raw `asyncio.sleep` in a worker. This keeps the UI responsive and the loop cancellable.
- Cap journal writes at one event per state change, not one per poll. A poll where nothing changed produces no journal entry beyond a periodic heartbeat (every 5 minutes).
- The recompute callback is the most error-prone part. Lock it carefully:
  1. Only fire when **all** sell chunks for an account are terminal (`Filled` or `Cancelled`).
  2. Sum `actual_proceeds` from filled chunks only.
  3. Pass `actual_proceeds_by_account` into the existing optimizer, which already supports an override (per chunk 2's schema work).
  4. Fire exactly once per account; flag in state to prevent re-fire.
- The mock ATP needs a "tick the clock and advance fills" helper for tests. Add it as `mock.advance(seconds=N, fills={...})`.

## Acceptance criteria

1. `pytest tests/test_stall.py` passes including: detection threshold edges, re-quote math for sell and buy, recompute trigger fires once per account.
2. End-to-end mock test:
   - 2 sells (chunk_id s1=1600 shs limit 62.39, s2=55 shs limit 62.39)
   - Mock fills s1 fully at 62.41, s2 partial 30/55 at 62.39 then stalls
   - After 5 simulated minutes, monitor flags stall with suggested limit $62.38 (bid moved to 62.37)
   - User presses C; state updated; new chunk s2b created for 25 shs at 62.38
   - Mock fills s2b; monitor reports all sells complete
   - Recompute fires; buy plan shows updated dollar_target using actual proceeds
3. Journal contains a complete event trail for the above run.
4. `python -m tui.monitor --plan plans/plan_*.json` against a live ATP smoke-tests successfully (read-only; with no real orders, just shows an empty Orders panel and "no open orders" status).
5. Polling interval is configurable via `--poll-seconds N` flag and via the state JSON's `inputs.config.polling_seconds`.

## When done

Capture the journal output from the end-to-end mock run as evidence of the full lifecycle (poll → stall detect → re-quote → cancel/replace → recompute). Stop.

---

## Day-of-trading workflow with all chunks complete

For the human's reference, the daily flow once all 6 chunks are done:

1. Morning: export Fidelity CSVs for the 3 accounts. Open ATP and the React calc.
2. Run the React calc as a sanity check (existing workflow). Click Export State to capture a calc baseline.
3. Run `python -m cli.compute --inputs ./csvs --signals signals.json --export today.json`.
4. Run `python -m cli.compare --engine today.json --calc calc_export.json`. If diffs, debug. If clean, proceed.
5. Run `python -m tui.app --plan today.json`. Approve/modify each strategy. Save plan.
6. Open `plan_*.txt`. Manually enter each sell order in ATP, checking off as you go.
7. Run `python -m tui.monitor --plan plan_*.json`. Watch fills. Respond to stall alerts as they appear.
8. When all sells complete, the monitor recomputes buys. Manually enter buy orders from the updated plan.
9. End of day: review `logs/journal.jsonl` for any anomalies. Re-export Fidelity CSVs and run a closing reconciliation against the morning state JSON.
