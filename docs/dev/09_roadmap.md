# 09 — Consolidated Roadmap

**Status as of 2026-06-02.** Single source of truth for outstanding features,
changes, design requirements, and roadmap items across the `fidelity_rebalancer`
project. Supersedes the scattered backlog in `07_backlog.md` for prioritization
purposes; `08_execution_scheduler.md` remains the detailed design for section A.

**Hard rule (non-negotiable):** the app NEVER places, modifies, or cancels
orders. It only produces a plan a human enters manually in ATP.
`accounts.json` / `state.json` / `signals.json` / `plans/` stay gitignored and
are never committed.

---

## Just shipped

- **Margin / cash-gate fix** (Backlog #1) — committed `c460934` on `develop`.
  Account-level `margin: bool` flag. Margin (taxable) accounts suppress the
  `CASH_NOT_OK` yellow because same-window buys are funded by buying power, not
  settled proceeds. Retirement vs cash (non-margin) accounts get branched help
  text. Validated in `validate_config.py`; documented in `accounts.example.json`.
  - **Caveat → item B-7:** `calculator.py:252` scale-down still caps buy totals
    to `proceeds + cash` regardless of margin, so margin accounts can still
    under-buy vs. true buying power. Loosening needs a buying-power number
    (declined for now — "suppress warning, no number").

---

## Foundation (shipped)

The original build — six chunk prompts in `docs/dev/01..06` — is complete and is
the engine's load-bearing base. Captured here so `00_ARCHITECTURE.md` can stay
purely descriptive (it no longer tracks build status, the old "development
chunks" table, "definition of done", or "current status").

- **Engine + state + parity (chunks 1–2).** React calculator ported to a pure
  Python engine (`engine/calculator.py`), the canonical state JSON
  (`state/schema.py`), and the `compute` + `compare` CLIs. **Parity gate met:**
  `cli.compute` produces byte-identical `computed` output to the React calc on
  the regression fixture, so the engine is the source of truth (the calc stays a
  spot-check; the React **Import/Export State** buttons round-trip the state JSON).
- **Read adapters + strategy + TUI + monitor (chunks 3–6).** ATP read-only
  adapters (UIA where it works, OCR/vision fallback for the Telerik-MAUI Level II
  and Orders panels), the sell/buy strategy generators + chunkers, the Textual
  approval presenter, and the live order monitor (~45 s polling, stall detection,
  re-quote).
- **Daily-workflow tooling (added on top, all read-only).** **Morning preflight**
  (`cli.preflight` + `preflight/`), **buy progress tracker** (`cli.progress`),
  and **EOD trade-journal report** (`cli.eod_report`, scoped to today).

---

## A. Execution Scheduler (chunk 8)

Detailed design: `08_execution_scheduler.md`. Phased order release across the
trading day with account-type / settlement gating.

- **A-1** — Extend `ChunkRecord` with scheduler fields: `phase`
  (premarket/main/sweep), `earliest_entry` (time), `funded_by`
  (proceeds/cash/buying_power), `account_type`.
- **A-2** — `engine/scheduler.py` skeleton: schedule sells into tranches;
  expose via a `--schedule` flag on the compute/plan CLI.
- **A-3** — Buys + account-type gating (retirement waits for settled proceeds;
  margin uses buying power immediately).
- **A-4** — End-of-day ramp / sweep tranche logic.
- **A-5** — Surface the phased schedule in the TXT checklist and TUI.
- **A-6** — Event-gated IRA funding (release buys only when the funding event
  fires).

**Reframed (2026-06-02): this section is being absorbed into the strategy
engine, not built as a standalone stage.** See `10_strategy_engine.md` §5–6.
The six design decisions of §7 of doc 08 (premarket mechanics, capture
aggressiveness, EOD ramp trigger, IRA funding gate, label vocabulary, config
home) are **no longer a blocker on unrelated work** — they become inputs to the
engine's `DecisionContext` design, answered during the engine arc (Phase 1/3 of
the sequencing below), not as a separate exercise. A-1 (`ChunkRecord` fields)
and A-2..A-6 land as engine steps, not as `engine/scheduler.py`.

---

## B. Backlog items

- **B-2** — Chunk-rationale surfacing (strategy reasoning is currently
  DEBUG-only; promote to user-visible). Same item as G-1.
- **B-3** — Calculator imports a recommended-orders JSON override.
- **B-4** — Consolidate "next steps" guidance across CLIs into one place.
- **B-7** — Margin buying-power number (see Just-shipped caveat). Would unblock
  loosening the `calculator.py:252` cap so margin accounts don't under-buy.
- **B-8** — `_find_downloads_csvs` auto-detect is broken. `cli/compute.py:227-240`
  inspects column 0 of each CSV row to match against `ACCOUNTS_CONFIG` keys, but
  column 0 in the Fidelity export is the Account _Number_, not the Account
  _Name_ (the name is column 1). The match never succeeds, so `_find_downloads_csvs`
  silently returns `None` and `--inputs <dir>` is effectively required. Either
  read the Account Name column (same approach as `scripts/morning-prep.ps1`'s
  `Get-CsvAccountName` and `engine/calculator.py:consolidate`) or drop the
  auto-detect and require `--inputs`. Discovered while verifying the Pending
  activity fix (commit `09fa4eb`, 2026-06-02).
- **B-9** — Adapters direct printing & logging inconsistency (2b-c). Adapters (`atp_ocr.py`, `atp_watchlist.py`, `yfinance_fallback.py`) print warnings and debug details directly to `stdout`/`stderr` using raw `print()` statements under a global `_DEBUG` flag, bypassing stdlib logging. Move to standard `logging.getLogger(__name__)` and route to `logs/strategy.log`.
- **B-10** — Preflight interactive prompts & logging boundaries (4b-c). Preflight orchestrator (`orchestrator.py`) handles interactive console prompts, whereas core checks return structured results. Preflight modules do not use standard logging, delegating output formatting to calling CLIs. Align preflight logging boundaries.
- **B-11** — TUI duplicate logging setup (5b-c). `monitor.py` implements its own duplicate `_setup_logging` function writing to `logs/monitor.log` instead of reusing the unified configuration setup in `engine/observability.py`. Consolidate logging setup to avoid duplicate handler setups.
- **B-12** — Global win32/pywinauto crash boundary in TUI. If screen capture or Win32 API interactions throw unexpected errors inside the monitor poller thread (e.g. if the user closes/minimizes ATP during a scan), the Textual app can crash, occasionally locking or corrupting terminal states. Implement a global crash-boundary wrapper inside the Textual application that safely restores terminal modes and cleanly presents target tracebacks.
- **B-13** — Configurable CSV Column Header Map. The parser strictly hardcodes required Fidelity CSV columns (e.g. "Symbol", "Quantity", "Current Value"). If Fidelity changes their export headers, the engine fails silently or throws. Move the parsed header keys into inputs.config inside EngineConfig so they can be re-mapped without modifying engine code.
- **B-14** — Uniform path handling. The project uses a mix of standard Path operators and custom path cleaners (like cli.resolve_path). Standardize on absolute resolved Path objects across all adapters, CLIs, and TUIs to prevent POSIX vs. Windows directory separator mismatch edge cases.
- **B-15** — Shared Memory/WebSocket State Sync (React Calculator Bridge). Run a minimal local WebSocket server inside `server.py` to sync state JSON changes bidirectionally between the React web UI and the Python TUI engine, eliminating manual file imports.



---

## C. UI / UX / Performance

- **C-1** — **localStorage silent-restore fix** _(next-window priority #1)_.
  Calculator restores a prior session on reload with no banner; can resurrect a
  stale/prior-day session. Add a "Restored session from <time>" banner +
  auto-discard on a new calendar day.
- **C-2 .. C-9** — Smaller UI/UX/perf polish items (carried from 07 backlog).
- **C-10** — Real-time Allocation Drift Visualizer. Render a side-by-side terminal column/gauge showing target vs. current vs. projected weights to let the trader audit allocation changes before committing.
- **C-11** — Interactive Override Diff. Highlight manual price/size overrides in a distinct color and display the deviation (e.g., `+$0.04 above recommended midpoint`) in the presenter screen.
- **C-12** — Asynchronous OCR Workers (Non-blocking UI). Offload RapidOCR window captures and model inference in [atp_ocr.py](file:///C:/Users/Jason/Documents/Code/SecSurfTrade/fidelity_rebalancer/adapters/atp_ocr.py) to a background thread/process pool to prevent Textual UI freezes during polls.
- **C-13** — Localized Region Caching. Cache the relative coordinate offsets of L2 grid panels in `atp_ocr.py` after the first successful lookup; crop subsequent frames directly using cached coordinates to avoid CPU quadrant scans.

---

## D. Manual-step reducers

- **D-1** — **OCR fill auto-logging** _(next-window priority #2)_. Auto-capture
  fills from the ATP Orders OCR read into the journal instead of manual entry.
- **D-2 .. D-6** — Other manual-step reducers (carried from 07 backlog).

---

## E. Future / Phase B+

**Execution-backend phasing (architecture vision).** The same engine and monitor
keep running unchanged; only the *execution adapter* swaps:
`manual entry (today)` → **Phase B** (ATP pre-fill, human clicks Preview +
Submit) → **Phase C** (ATP full automation + kill switch). Engine code does not
change between phases — the pluggable adapter interface is the only thing that
changes. When any write-side automation lands, cap order entry at **< 2
orders/min** to stay under Fidelity abuse-detection. (Absorbed from
`00_ARCHITECTURE.md`, which now points here for the forward-looking view.)

- **E-1 .. E-7** — Auto-cancellation, auto-re-placement, account-level kill
  switch, multi-account tabbed display, etc. Explicitly out of scope for the
  human-in-the-loop product today.

---

## F. Realtime Monitor Engine

Textual app (`tui/monitor.py`), `set_interval` polling, `engine/stall.py`,
journal JSONL at `logs/journal.jsonl`.

> **Test status (corrected 2026-06-02):** the chunk-6 end-to-end mock test DOES
> exist — `test_stall.py::test_e2e_stall_detect_requote_recompute` (state
> helpers) — and the journal event-trail (acceptance #3) is now covered by
> `test_monitor_e2e.py::test_e2e_journal_trail`, graduated from the LT-1 harness.
> An earlier note that "the e2e test was never built" was stale.

- **F-1** — **Live budget recompute is unbuilt.** Chunk 6 acceptance criterion
  #2 requires `optimizer.recompute_buys(state, actual_proceeds)`; grep confirms
  it does not exist. `_do_poll`'s recompute trigger only LOGS proceeds and marks
  the account — no buy plan is recalculated. _(The graduated test asserts the
  trigger fires and logs proceeds, and documents this gap.)_
- **F-2** — **`[C]` re-quote action does not persist.** Per doc 06 it should
  mark the old chunk cancelled, create a new chunk at the suggested limit with
  remaining qty, journal it, and re-export the TXT checklist.
  `action_confirm_requote` only logs and flips the in-memory order to Cancelled.
- **F-3** — Monitor reads ORDERS from ATP (OCR), not from the calculator/plan.
  Decide whether that's the intended source of truth.
- **F-4** — `ATPOrdersAdapter` raises `LookupError` (Telerik MAUI blocks UIA
  row access) → falls back to OCR, then MockATP. Fragile; track ATP UI changes.
- **F-5** — Scheduler tie-in: stall/sweep handling should become phase-aware
  once section A lands.

---

## G. Strategy Engine

Rule-based sell rules 0–7 (gap_capture, tight/wide spread, down/up day,
above/below VWAP, default) and buy rules.

- **G-1** — Reasoning bullets are DEBUG-only; promote to user-visible.
  Same item as B-2.
- **G-2** — Generalize gap-capture (sell rule 0) into the scheduler's
  premarket/main phasing. Folds into section A.
- **G-3** _(downgraded 2026-06-02 — low daily impact)_ — VWAP source: VWAP
  rules (sell 6/7, buy 4/5) only fire with ATP data; yfinance returns None for
  VWAP. **But the real daily workflow runs `--source atp`** (morning-prep →
  preflight Step 5 builds `cli.strategy … --source atp --strict-atp`), so VWAP
  _is_ present in normal use. The dormancy only affects a standalone
  `cli.strategy --source yfinance` run. This is a clarity/documentation item,
  not a daily-impact gap: make ATP-only explicit, or approximate VWAP for the
  yfinance path.

---

## LT. Live-test items

- **LT-1** — **Live-test the monitor.** Run `python -m tui.monitor --mock` for
  the Open → PartiallyFilled → Stall → Re-quote lifecycle against MockATP, plus
  a read-only live ATP smoke (`--plan plans/plan_*.json`, FT+ open). Capture the
  `logs/journal.jsonl` event trail as evidence.
- **LT-2** — **Live-test the strategy engine.** Run
  `cli.strategy --source atp --l2-symbols ...` and `--source yfinance`; confirm
  rule branches fire correctly (sigma, ADV, L2 depth, VWAP) and that VWAP
  branches no-op on yfinance (G-3).

---

## Prioritization and Scoring Rubric

To systematically prioritize roadmap and backlog tasks, each item is rated in five categories:
1. **Difficulty to implement** (time, odds of generating bugs, amount of AI budget): `low` = 5.0, `med` = 2.5, `hi` = 1.0.
2. **UI improvement** (reduction of manual intervention, improving human visibility, reducing friction): `hi` = 5.0, `med` = 2.5, `low` = 1.0.
3. **Efficiency** (speed, reliability, accuracy): `hi` = 5.0, `med` = 2.5, `low` = 1.0.
4. **Performance** (improving trading outcomes, reducing human error): `hi` = 5.0, `med` = 2.5, `low` = 1.0.
5. **Maintenance** (reducing code complexity, improving readability, reducing future errors over time): `hi` = 5.0, `med` = 2.5, `low` = 1.0.

Total Score = Sum of all category scores (Max: 25.0, Min: 5.0).

### Prioritized Backlog Table

| Committed Group | Rank | Item ID | Title / Description | Diff | UI | Eff | Perf | Maint | Total Score |
|:---|---|---|---|---|---|---|---|---|---|
| **Phase 0** *(Quick Wins)* | 1 | **D-1** | **OCR fill auto-logging** (in [monitor.py](file:///C:/Users/Jason/Documents/Code/SecSurfTrade/fidelity_rebalancer/tui/monitor.py)) | 2.5 | 5.0 | 5.0 | 5.0 | 2.5 | **20.0** |
| | 2 | **LT-1** | **Live-test the monitor** (Mock vs Live ATP smoke) | 5.0 | 1.0 | 2.5 | 5.0 | 5.0 | **18.5** |
| | 3 | **C-1** | **localStorage silent-restore fix** (React calc banner) | 5.0 | 2.5 | 2.5 | 5.0 | 2.5 | **17.5** |
| **Phase 1** *(Engine Foundation)*| 1 | **LT-2** | **Live-test the strategy engine** (baseline rules validation) | 5.0 | 1.0 | 5.0 | 5.0 | 5.0 | **21.0** |
| **Phase 2 / Wave 2** *(Behavior)*| 1 | **G-4 / Step 3** | **Symmetric Sell Escalation** (in [strategy_sell.py](file:///C:/Users/Jason/Documents/Code/SecSurfTrade/fidelity_rebalancer/engine/strategy_sell.py)) | 5.0 | 2.5 | 5.0 | 5.0 | 5.0 | **22.5** |
| | 2 | **F-2 / Step 6** | **`[C]` re-quote action does not persist** (stall re-pricing) | 2.5 | 5.0 | 5.0 | 5.0 | 2.5 | **20.0** |
| | 3 | **G-5 / Step 4** | **Per-class %ADV thresholds** (ADV consolidation) | 2.5 | 1.0 | 5.0 | 5.0 | 5.0 | **18.5** |
| | 4 | **G-3 / Step 8** | **yfinance VWAP approximation** (in [yfinance_fallback.py](file:///C:/Users/Jason/Documents/Code/SecSurfTrade/fidelity_rebalancer/adapters/yfinance_fallback.py)) | 2.5 | 1.0 | 5.0 | 2.5 | 2.5 | **13.5** |
| **Phase 3 / Wave 3** *(Scheduler)* | 1 | **A / S-1 (Step 7)**| **Scheduler Absorption** (Tranche sizing / gates) | 1.0 | 5.0 | 2.5 | 5.0 | 5.0 | **18.5** |
| **Backlog** *(Uncommitted)* | 1 | **C-11** | **Interactive Override Diff** (in [presenter.py](file:///C:/Users/Jason/Documents/Code/SecSurfTrade/fidelity_rebalancer/tui/presenter.py)) | 5.0 | 5.0 | 2.5 | 5.0 | 2.5 | **20.0** |
| | 2 | **B-12** | **Global win32/pywinauto crash boundary in TUI** | 2.5 | 2.5 | 5.0 | 5.0 | 5.0 | **20.0** |
| | 3 | **B-15** | **Shared Memory/WebSocket State Sync** (React Bridge) | 2.5 | 5.0 | 5.0 | 5.0 | 2.5 | **20.0** |
| | 4 | **B-3** | **Calculator imports recommended-orders JSON** | 5.0 | 5.0 | 2.5 | 2.5 | 2.5 | **17.5** |
| | 5 | **C-10** | **Real-time Allocation Drift Visualizer** (TUI presenter) | 2.5 | 5.0 | 2.5 | 5.0 | 2.5 | **17.5** |
| | 6 | **C-12** | **Asynchronous OCR Workers** (Non-blocking Textual UI) | 2.5 | 5.0 | 5.0 | 2.5 | 2.5 | **17.5** |
| | 7 | **F-3** | **Decide monitor source of truth (ATP OCR vs plan)** | 2.5 | 2.5 | 2.5 | 5.0 | 5.0 | **17.5** |
| | 8 | **B-8** | **`_find_downloads_csvs` auto-detect fix** (column index) | 5.0 | 1.0 | 5.0 | 2.5 | 2.5 | **16.0** |
| | 9 | **B-9** | **Adapters direct printing & logging inconsistency** | 5.0 | 1.0 | 2.5 | 2.5 | 5.0 | **16.0** |
| | 10 | **B-4** | **Consolidate "next steps" guidance across CLIs** | 5.0 | 2.5 | 1.0 | 2.5 | 5.0 | **16.0** |
| | 11 | **F-4** | **ATPOrdersAdapter fragile row access** (Telerik grid fixes) | 1.0 | 2.5 | 5.0 | 5.0 | 2.5 | **16.0** |
| | 12 | **B-13** | **Configurable CSV Column Header Map** | 5.0 | 1.0 | 1.0 | 2.5 | 5.0 | **14.5** |
| | 13 | **B-10** | **Preflight interactive prompts & log boundaries** | 2.5 | 2.5 | 1.0 | 2.5 | 5.0 | **13.5** |
| | 14 | **B-11** | **TUI duplicate logging setup** (monitor cleanup) | 5.0 | 1.0 | 1.0 | 1.0 | 5.0 | **13.0** |
| | 15 | **B-14** | **Uniform path handling** (`Path.resolve()` standard) | 5.0 | 1.0 | 1.0 | 1.0 | 5.0 | **13.0** |
| | 16 | **C-13** | **Localized Region Caching** (OCR crop optimization) | 2.5 | 1.0 | 5.0 | 1.0 | 2.5 | **12.0** |
| | 17 | **B-7** | **Margin buying-power number** (ATP parsing addition) | 1.0 | 2.5 | 1.0 | 5.0 | 2.5 | **12.0** |

---


## Sequencing (phased plan)


Two tracks run loosely in parallel: **Track A** keeps the daily workflow sharp
(UI/UX + manual-step reducers, low-risk, isolated); **Track B** is the engine
arc (`10_strategy_engine.md` §6) plus the monitor recompute. They barely
compete — A is front-end/ops, B is the engine.

**Phase 0 — quick independent wins (next window)**

1. **C-1** localStorage restore banner / new-day auto-discard _(owner priority #1)_.
2. **D-1** OCR fill auto-logging _(owner priority #2)_.
3. **Engine step 2 — surface reasoning (B-2/G-1), pulled forward.** Cheapest
   trust win: today the engine explains _why_ it picked each limit but routes it
   to DEBUG. Showing it in TUI/TXT is near-zero risk and makes the engine
   auditable _before_ we change its behavior. Recommended to jump the queue.
4. **LT-1** read-only live ATP smoke (mock half already done).

**Phase 1 — engine foundation** 5. **LT-2** live-test the engine (`--source atp` and `--source yfinance`) to
refresh ground truth — a gate before any refactor. 6. **Engine step 1 — `DecisionContext` refactor.** Keystone; unifies the
scattered timing/funding/liquidity inputs. Pure refactor, no behavior change. 7. **F-1** `optimizer.recompute_buys` — monitor correctness; independent, can
run in parallel with the refactor.

**Phase 2 — engine behavior** 8. Step 3 symmetric sell escalation (G-4) · Step 4 per-class %ADV thresholds
(G-5) · Step 6 / F-6 re-quote via the rules · fold in G-6 (unify the two ADV
definitions).

**Phase 3 — absorb the scheduler (section A)** 9. Answer the six chunk-8 design decisions **as `DecisionContext` inputs**, then
step 7: add `phase`/`earliest_entry`/`funded_by` to `ChunkRecord`, emit phased
chunks from the engine, retire the standalone scheduler stage. (F-5 follows.)

**Phase 4 — UI/UX Polish & OCR Process Isolation (Performance and Reliability)**
10. **C-11** Interactive Override Diff (score: 20.0). High-contrast highlighting of price/size deviations in [presenter.py](file:///C:/Users/Jason/Documents/Code/SecSurfTrade/fidelity_rebalancer/tui/presenter.py).
11. **B-12** Global win32/pywinauto crash boundary in TUI (score: 20.0). Prevents terminal corruption on Win32 errors in [app.py](file:///C:/Users/Jason/Documents/Code/SecSurfTrade/fidelity_rebalancer/tui/app.py).
12. **C-12** Asynchronous OCR Workers (score: 17.5). Offload RapidOCR to a background process/thread in [atp_ocr.py](file:///C:/Users/Jason/Documents/Code/SecSurfTrade/fidelity_rebalancer/adapters/atp_ocr.py) to prevent TUI freezing.
13. **B-15** Shared Memory/WebSocket State Sync (score: 20.0). Eliminates manual export/import loop between browser and TUI.

**Deferred:** T-1 tax-awareness, B-7 margin buying-power number, B-8 CSV
auto-detect, E-\* Phase B+.

> **Why the engine is "priority #4" yet pulled into Phase 0:** the owner ranked
> the engine 4th, but engine step 2 (surface reasoning) is the prerequisite for
> trusting everything else the engine does and is nearly free — so it jumps to
> Phase 0 while the heavier engine arc (Phases 1–3) stays after the Track-A
> quick wins.

