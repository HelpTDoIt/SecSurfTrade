# 09 — Consolidated Roadmap

**Status as of 2026-06-08.** Single source of truth for outstanding features,
changes, design requirements, and roadmap items across the `fidelity_rebalancer`
project. Supersedes the scattered backlog in `07_backlog.md` for prioritization
purposes.

**Hard rule (non-negotiable):** the app NEVER places, modifies, or cancels
orders. It only produces a plan a human enters manually in ATP.
`accounts.json` / `state.json` / `signals.json` / `plans/` stay gitignored and
are never committed. The B-15 state-sync hub binds `127.0.0.1` only and relays
**state JSON only** — there is no order/command channel anywhere in the app.

**How to read this doc**

1. **Shipped / in production** — what is live in the working tree today.
2. **Active / in progress** — the engine arc and the live-test gates.
3. **Backlog, by theme** — open work grouped by the goal it serves
   (manual-effort reduction · trading outcomes · human UI & clarity ·
   maintenance & robustness · write-automation).
4. **Prioritization & scoring** — the rubric and the single globally-ranked table.
5. **Sequencing** — the phased execution plan.

---

## 1. Shipped / in production

### 1.1 Foundation (chunks 1–6)

The original build is complete and is the engine's load-bearing base.

- **Engine + state + parity (chunks 1–2).** React calculator ported to a pure
  Python engine (`engine/calculator.py`), the canonical state JSON
  (`state/schema.py`), and the `compute` + `compare` CLIs. **Parity gate met:**
  `cli.compute` produces byte-identical `computed` output to the React calc on
  the regression fixture, so the engine is the source of truth.
- **Read adapters + strategy + TUI + monitor (chunks 3–6).** ATP read-only
  adapters (UIA where it works, OCR/vision fallback for the Telerik-MAUI Level II
  and Orders panels), the sell/buy strategy generators + chunkers, the Textual
  approval presenter, and the live order monitor (~45 s polling, stall detection,
  re-quote).
- **Daily-workflow tooling (all read-only).** Morning preflight (`cli.preflight`
  + `preflight/`), buy progress tracker (`cli.progress`), and EOD trade-journal
  report (`cli.eod_report`).

### 1.2 Execution scheduler — section A (A-1..A-6, G-2) ✅

Phased order release across the trading day with account-type / settlement
gating. Detailed design: `08_execution_scheduler.md`.

- **A-1** — `ChunkRecord` scheduler fields: `phase` (premarket/main/sweep),
  `earliest_entry`, `funded_by`, `account_type`.
- **A-2** — `engine/scheduler.py` (`build_day_schedule`), exposed via the
  `--schedule` flag on `cli.strategy`.
- **A-3** — Buys + account-type gating (retirement waits for settled proceeds;
  margin uses buying power immediately).
- **A-4** — End-of-day ramp / sweep tranche logic.
- **A-5** — Phased schedule surfaced in the TXT checklist and TUI.
- **A-6** — Event-gated IRA funding.
- **G-2** — Gap-capture (sell rule 0) generalized into the premarket/main phasing.

### 1.3 Quantitative engine upgrades (G-3..G-6, F-5, F-6) ✅

- **G-3** — Approximate VWAP for the yfinance path (VWAP rules no longer no-op
  off-ATP).
- **G-4** — Symmetric sell-side escalation.
- **G-5 / G-6** — Per-asset-class %ADV thresholds and unified ADV source.
- **F-5** — Phase-aware stall handling: the stall timer scales with volume
  (more patient on thin tickers, tightens to 30 s in the sweep tranche).
- **F-6** — Stall re-quote via full rule re-selection.
- Odd-lot support (100-share minimum removed) with a 15-order cap; dynamic
  realized volatility (`engine/volatility.py`, yfinance 20-day σ with
  asset-class / ATP-day-range fallback); full-depth Level 2 order-book imbalance
  (bid-heavy > 0.80 / ask-heavy < 0.20 rules); cumulative volume-exhaustion VWAP
  escalation ramps (> 0.25 / > 0.75).

### 1.4 Live state sync — B-15 ✅ **(new this snapshot)**

Loopback WebSocket relay hub in `server.py` (`RelayHub`, `serve_ws`,
`start_ws_hub_thread`; default port **7825**) that keeps the browser calculator
and any engine/TUI client in sync live, removing the manual export/import step
(manual Import/Export retained as a fallback).

- Schema-agnostic relay: forwards rebalance **state JSON** and caches the last
  `state` message so a late-joining client gets an immediate snapshot.
- Reusable async client `tui/sync.py` (`StateSyncClient`) for non-browser clients.
- Browser side connects to `ws://127.0.0.1:7825`, debounces/broadcasts edits,
  auto-reconnects, and shows a status pill ("Sync on" / "Reconnecting…").
- **Security:** Origin allowlist on the handshake (CSWSH hardening),
  `127.0.0.1`-only bind, state-JSON-only relay.

### 1.5 UI / visibility / confidence (C-8, C-9, C-11, B-2/G-1, D-5) ✅

- **C-8** — Calculator `OverrideBadge`: a manual limit-price edit shows a signed
  diff against the engine price, mirroring the TUI override diff.
- **C-9** — Dark / low-contrast trading-day theme (theme switcher).
- **C-11** — TUI presenter `original_limit_price` override diff
  (e.g. `override: +$0.0400 from engine $62.3900`).
- **B-2 / G-1** — Strategy reasoning promoted from DEBUG-only into `state.json`
  and the Entry UI.
- **D-5** — Month-over-month strategy sanity diff (warns on anomalous allocation
  shifts in the CLI).

### 1.6 Manual-step reducers, refactors & fixes ✅

- **D-1** — OCR fill auto-logging (base `d1b5b26`) + `cli/export_fills.py` bridge.
- **B-7** — Margin buying-power number via manual preflight input.
- **B-9 / B-11** — Adapters and TUI moved to standard `logging`.
- **B-14** — Uniform absolute-`Path` handling across adapters/CLIs/TUIs.
- **C-1** — localStorage silent-restore fix (`46912d2`) with new-day auto-discard.
- **C-3** — Per-row "Copy as ATP ticket" button (implemented, then removed — no
  utility).
- **E-4** — Execution-slippage tracking in the EOD report.
- **Margin / cash-gate fix** (`c460934`) — account-level `margin: bool` suppresses
  the `CASH_NOT_OK` yellow for margin accounts; branched help text for
  retirement vs cash accounts.
- **Phase -1 batch** — `cli/export_fills.py` bridge, adapter logging, path
  handling, executed by parallel agents.

---

## 2. Active / in progress

### 2.1 Engine arc

- **DecisionContext refactor** — unify the scattered timing/funding/liquidity
  inputs into one context object. Pure refactor, no behavior change; keystone for
  the items below.
- **S-1 — scheduler absorption.** Fold the standalone `engine/scheduler.py`
  (shipped A-1..A-6) natively into the strategy engine's `DecisionContext` so the
  six design decisions of doc 08 §7 (premarket mechanics, capture aggressiveness,
  EOD ramp trigger, IRA funding gate, label vocabulary, config home) become engine
  inputs rather than a separate stage.

### 2.2 Live-test gates

- **LT-1 — live-test the monitor.** Open → PartiallyFilled → Stall → Re-quote
  against MockATP (done), plus a read-only live ATP smoke (`--plan plans/plan_*.json`,
  FT+ open). Capture `logs/journal.jsonl` as evidence. (`../test_plan_atp_integration.md`)
- **LT-2 — live-test the strategy engine.** Run `cli.strategy --source atp
  --l2-symbols …` and `--source yfinance`; confirm rule branches fire (σ, ADV,
  L2 depth, VWAP). Gate before trusting engine behavior. (`../test_plan_trading_window.md`)

---

## 3. Backlog, by theme

Open items grouped by the goal each serves. IDs are stable across docs.

### 3.1 Manual-effort reduction

- **B-17** *(new)* — **Engine → browser live push** over the B-15 hub. The engine
  emits a `state` message after compute and the browser auto-applies it, closing
  the last manual-import gap (today B-15 syncs browser↔browser; the engine still
  exports a file the user imports).
- **B-18** *(new)* — **Always-on TUI sync screen.** Wire the already-built
  `tui/sync.py` `StateSyncClient` into a live Textual screen so the terminal
  reflects calculator edits in real time (the deferred B-15 seam).
- **B-3** — Calculator imports a recommended-orders JSON override.
- **B-8** — `_find_downloads_csvs` auto-detect fix. `cli/compute.py:227-240`
  matches column 0 (Account *Number*) against `ACCOUNTS_CONFIG` keys (Account
  *Name*, column 1), so auto-detect silently returns `None` and `--inputs <dir>`
  is effectively required every run. Read the Account-Name column (as
  `scripts/morning-prep.ps1` / `engine/calculator.py:consolidate` do) or drop
  auto-detect and require `--inputs`.
- **D-2** — Filesystem watcher on `~/Downloads`: detect a new Fidelity CSV and
  trigger recompute instantly.
- **D-4** — Auto-advance Entry round once the current round's fills cross a
  completion threshold.
- **D-6** — Scraper short-circuit: detect "nothing to do this month" and email an
  alert instead of building a plan.

### 3.2 Trading outcomes

- **F-1** — **Live budget recompute is unbuilt.** Chunk-6 criterion #2 requires
  `optimizer.recompute_buys(state, actual_proceeds)`; `_do_poll`'s trigger only
  logs proceeds and marks the account — no buy plan is recalculated.
- **F-2** — **`[C]` re-quote action does not persist.** It should cancel the old
  chunk, create a new one at the suggested limit with remaining qty, journal it,
  and re-export the TXT checklist; today it only logs and flips the in-memory
  order to Cancelled.
- **F-4** — `ATPOrdersAdapter` row access is fragile (Telerik MAUI blocks UIA →
  OCR → MockATP). Track ATP UI changes.
- **B-16** — Replace the RapidOCR engine with Surya OCR in `atp_ocr.py` to fix
  character substitution / parsing failures on dense numeric grids
  (`12_ocr_engine_migration.md`).
- **C-5** — Live spread strip per Entry row (bid/ask from FT+ OCR every 10–15 s).
- **D-7** *(new)* — Surface the **D-5 month-over-month allocation diff inside the
  calculator** (today it only warns in the CLI), so anomalous shifts are visible
  before trading.
- **E-5** — Drift-attribution dashboard (each strategy's contribution to drift
  over time).
- **E-7** — Backtest harness: replay historical scraper signals through the engine.
- **E-8** *(new)* — EOD slippage dashboard built on the already-tracked E-4
  execution-slippage metric.

### 3.3 Human UI & clarity

- **C-2** — Keyboard-driven Entry tab (`j/k/c` vim-style navigation/confirm).
- **C-4** — Sticky chunk-table headers + sticky round-nav on long order lists.
- **C-6** — Sell-proceeds running total in the IRA banner (dynamic counter
  replacing the static advisory).
- **C-7** — Virtualized Trades-tab fill rows (`react-window`) for hundreds of fills.
- **C-10** — Real-time allocation-drift visualizer (target vs current vs projected
  weights in the TUI presenter).
- **E-6** — iPad / second-monitor companion view (responsive layout).

### 3.4 Maintenance & robustness

- **B-4** — Consolidate "next steps" guidance across CLIs into one place.
- **B-10** — Align preflight logging boundaries (orchestrator handles interactive
  prompts; modules don't use standard logging).
- **B-12** — Global win32/pywinauto crash boundary in the TUI so a mid-scan Win32
  error can't crash Textual and corrupt terminal state.
- **B-13** — Configurable CSV column-header map in `EngineConfig` (today the
  Fidelity headers are hardcoded; an export rename fails silently).
- **C-12** — Asynchronous OCR workers: offload RapidOCR capture/inference to a
  background pool to stop Textual UI freezes during polls.
- **C-13** — Localized region caching: cache L2 grid offsets after first lookup,
  crop subsequent frames directly to avoid CPU quadrant scans.
- **M-1** *(new)* — **CI via GitHub Actions** running `pytest` on push, to guard
  the 467-test suite against regression.
- **M-2** *(new)* — **Repo hygiene:** delete the corrupt `PROJECT.md` stub and the
  abandoned `state/patch.py` one-off script; prune stray debug artifacts
  (`debug_calc.py`, `test_b14.py`, `verify_b14_paths.py`, `test_resolve_colon.py`,
  `script.jsx`, `*_wl_crop.png`, the ` - production`/` - superceeded` doc copies).

### 3.5 Write-automation (deferred)

Deferred until at least two full trading windows have completed manually. The
engine and monitor keep running unchanged; only the *execution adapter* swaps:
manual entry → **Phase B** (ATP pre-fill, human clicks Preview + Submit) →
**Phase C** (full automation + kill switch). When any write-side automation lands,
cap order entry at **< 2 orders/min** to stay under Fidelity abuse detection.

- **D-3** — ATP account-selector write adapter (minimal: select the correct
  account dropdown).
- **E-1** — Phase B execution adapter (pre-fill the order ticket, leaving only the
  final Preview + Submit click to the human).

---

## 4. Prioritization & scoring

Each open item is rated in five categories:

1. **Difficulty to implement** (time, bug odds, AI budget): `low` = 5.0, `med` = 2.5, `hi` = 1.0.
2. **UI improvement** (less manual intervention, better visibility, less friction): `hi` = 5.0, `med` = 2.5, `low` = 1.0.
3. **Efficiency** (speed, reliability, accuracy): `hi` = 5.0, `med` = 2.5, `low` = 1.0.
4. **Performance** (better trading outcomes, less human error): `hi` = 5.0, `med` = 2.5, `low` = 1.0.
5. **Maintenance** (less complexity, more readability, fewer future errors): `hi` = 5.0, `med` = 2.5, `low` = 1.0.

Total = sum of the five (max 25.0, min 5.0).

### Prioritized table (global rank)

Rank is global and contiguous, ordered by total score (ties broken by goal
impact, then ID). The Theme column maps to the §3 grouping; *new* marks the
proactively-added items.

| Rank | Item ID | Title | Theme | Diff | UI | Eff | Perf | Maint | Total |
|:---:|:---|:---|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| 1 | **C-6** | Sell-proceeds running total in IRA banner | UI & clarity | 5.0 | 5.0 | 5.0 | 2.5 | 5.0 | **22.5** |
| 2 | **LT-2** | Live-test the strategy engine | Live-test | 5.0 | 1.0 | 5.0 | 5.0 | 5.0 | **21.0** |
| 3 | **F-2** | `[C]` re-quote action does not persist | Trading | 2.5 | 5.0 | 5.0 | 5.0 | 2.5 | **20.0** |
| 4 | **B-16** | Replace RapidOCR with Surya OCR | Trading | 2.5 | 5.0 | 5.0 | 5.0 | 2.5 | **20.0** |
| 5 | **C-5** | Live spread strip per Entry row | Trading | 2.5 | 5.0 | 5.0 | 5.0 | 2.5 | **20.0** |
| 6 | **B-12** | Global win32/pywinauto crash boundary in TUI | Maintenance | 2.5 | 2.5 | 5.0 | 5.0 | 5.0 | **20.0** |
| 7 | **C-2** | Keyboard-driven Entry tab (`j/k/c`) | UI & clarity | 5.0 | 5.0 | 5.0 | 2.5 | 2.5 | **20.0** |
| 8 | **LT-1** | Live-test the monitor (live ATP smoke) | Live-test | 5.0 | 1.0 | 2.5 | 5.0 | 5.0 | **18.5** |
| 9 | **F-1** | Live budget recompute (`optimizer.recompute_buys`) | Trading | 2.5 | 2.5 | 5.0 | 5.0 | 2.5 | **17.5** |
| 10 | **D-7** *(new)* | Surface D-5 month-over-month diff in calculator | Trading | 2.5 | 5.0 | 2.5 | 5.0 | 2.5 | **17.5** |
| 11 | **B-17** *(new)* | Engine → browser live push over B-15 hub | Manual-effort | 2.5 | 5.0 | 5.0 | 2.5 | 2.5 | **17.5** |
| 12 | **B-18** *(new)* | Always-on TUI sync screen (`tui/sync.py`) | Manual-effort | 2.5 | 5.0 | 5.0 | 2.5 | 2.5 | **17.5** |
| 13 | **D-2** | Filesystem watcher on `~/Downloads` | Manual-effort | 2.5 | 5.0 | 5.0 | 2.5 | 2.5 | **17.5** |
| 14 | **D-4** | Auto-advance Entry round | Manual-effort | 2.5 | 5.0 | 5.0 | 2.5 | 2.5 | **17.5** |
| 15 | **B-8** | `_find_downloads_csvs` auto-detect fix | Manual-effort | 5.0 | 2.5 | 5.0 | 2.5 | 2.5 | **17.5** |
| 16 | **B-3** | Calculator imports recommended-orders JSON | Manual-effort | 5.0 | 5.0 | 2.5 | 2.5 | 2.5 | **17.5** |
| 17 | **C-10** | Real-time allocation-drift visualizer | UI & clarity | 2.5 | 5.0 | 2.5 | 5.0 | 2.5 | **17.5** |
| 18 | **C-12** | Asynchronous OCR workers (non-blocking UI) | Maintenance | 2.5 | 5.0 | 5.0 | 2.5 | 2.5 | **17.5** |
| 19 | **D-3** | ATP account-selector write adapter | Write-automation | 1.0 | 5.0 | 5.0 | 5.0 | 1.0 | **17.0** |
| 20 | **E-1** | Phase B execution adapter (ATP pre-fill) | Write-automation | 1.0 | 5.0 | 5.0 | 5.0 | 1.0 | **17.0** |
| 21 | **F-4** | `ATPOrdersAdapter` fragile row access | Trading | 1.0 | 2.5 | 5.0 | 5.0 | 2.5 | **16.0** |
| 22 | **D-6** | Scraper short-circuit + email notification | Manual-effort | 2.5 | 5.0 | 5.0 | 1.0 | 2.5 | **16.0** |
| 23 | **B-4** | Consolidate "next steps" guidance across CLIs | Maintenance | 5.0 | 2.5 | 1.0 | 2.5 | 5.0 | **16.0** |
| 24 | **C-4** | Sticky chunk-table headers + round-nav | UI & clarity | 5.0 | 2.5 | 2.5 | 1.0 | 5.0 | **16.0** |
| 25 | **E-5** | Drift-attribution dashboard | Trading | 2.5 | 5.0 | 2.5 | 2.5 | 2.5 | **15.0** |
| 26 | **E-8** *(new)* | EOD slippage dashboard (builds on E-4) | Trading | 2.5 | 5.0 | 2.5 | 2.5 | 2.5 | **15.0** |
| 27 | **B-13** | Configurable CSV column-header map | Maintenance | 5.0 | 1.0 | 1.0 | 2.5 | 5.0 | **14.5** |
| 28 | **M-1** *(new)* | CI via GitHub Actions (guard 467-test suite) | Maintenance | 5.0 | 1.0 | 2.5 | 1.0 | 5.0 | **14.5** |
| 29 | **B-10** | Preflight interactive prompts & log boundaries | Maintenance | 2.5 | 2.5 | 1.0 | 2.5 | 5.0 | **13.5** |
| 30 | **C-7** | Virtualized Trades-tab fill rows | UI & clarity | 2.5 | 5.0 | 2.5 | 1.0 | 2.5 | **13.5** |
| 31 | **E-6** | iPad / second-monitor companion view | UI & clarity | 2.5 | 5.0 | 2.5 | 1.0 | 2.5 | **13.5** |
| 32 | **M-2** *(new)* | Repo hygiene (prune debris + stub docs) | Maintenance | 5.0 | 1.0 | 1.0 | 1.0 | 5.0 | **13.0** |
| 33 | **C-13** | Localized region caching (OCR crop optimization) | Maintenance | 2.5 | 1.0 | 5.0 | 1.0 | 2.5 | **12.0** |
| 34 | **E-7** | Backtest harness | Trading | 1.0 | 1.0 | 2.5 | 5.0 | 2.5 | **12.0** |

---

## 5. Sequencing

Two tracks run loosely in parallel: **Track A** keeps the daily workflow sharp
(UI/UX + manual-step reducers — low-risk, isolated); **Track B** is the engine
arc (`10_strategy_engine.md` §6) plus the monitor recompute. They barely compete
— A is front-end/ops, B is the engine.

**`When` tags** — what each item needs to execute/validate:
**`desk`** = pure dev, no FT+ or market · **`FT+ open`** = Trader+ running, any
day (no market hours or signals needed) · **`live window`** = market open, ideally
a SectorSurfer rotation day. A **`desk → …`** pair is coded at the desk but only
fully validated under the second condition.

**Phase 0 — quick independent wins** *(before the next trading window)*

1. **C-6** Sell-proceeds running total in the IRA banner (highest-scoring, isolated). — *`desk`*
2. **LT-1** read-only live ATP smoke (mock half done); capture the journal trail. — *`FT+ open`*
3. **M-2** repo hygiene — cheap, removes confusion before deeper work. — *`desk`*
4. **B-8** `_find_downloads_csvs` auto-detect fix — drops the per-run `--inputs` requirement. — *`desk`*

**Phase 1 — engine foundation** *(LT-2 on the window day; refactor at the desk after)*

5. **LT-2** live-test the engine (`--source atp` and `--source yfinance`) — gate
   before any refactor. — *`live window`*
6. **DecisionContext refactor** — keystone; unifies timing/funding/liquidity inputs. — *`desk`*
7. **S-1** scheduler absorption into the engine context. — *`desk`*

**Phase 2 — monitor & OCR upgrades** *(build between windows; validate on the following window)*

8. **F-2** `[C]` re-quote persistence. — *`desk → live window`*
9. **B-16** Surya OCR; **C-12** async OCR workers; **C-13** region caching. — *`desk → FT+ open`*
10. **F-1** live budget recompute; **F-4** `ATPOrdersAdapter` robustness. — *`desk → live window`*
11. **B-12** TUI crash boundary. — *`desk`*

**Phase 3 — UX polish & UI sync** *(build between windows; mostly desk-validated)*

12. **B-17** engine → browser live push; **B-18** always-on TUI sync screen
    (both extend the shipped B-15 hub to close the last manual-import gaps). — *`desk`*
13. **B-3** calculator imports a recommended-orders JSON (file-import counterpart to the B-17 live push). — *`desk`*
14. **D-7** month-over-month diff in the calculator; **C-10** drift visualizer. — *`desk`*
15. **C-5** live spread strip — *`desk → FT+ open`*; **C-2** keyboard Entry tab — *`desk`*.
16. **D-2 & D-4** Downloads watcher & auto-advance Entry round. — *`desk`*
17. **M-1** CI (GitHub Actions) once the workflow is stable. — *`desk`*

**Phase 4 — write automation** *(deferred until ≥ 2 manual windows complete)*

18. **D-3** ATP account-selector write adapter. — *`desk → live window`*
19. **E-1** Phase B execution adapter (ATP pre-fill). — *`desk → live window`*

**Deferred / opportunistic:** B-4, B-10, B-13, C-4, C-7, D-6, E-5, E-6, E-7, E-8
— pulled forward when they unblock a daily pain point.
