# Chunk 5 — TUI Presenter (Approval Flow)

**Suggested model:** Sonnet 4.6
**Depends on:** chunk 4 complete
**Estimated effort:** 1.5–2 hours

---

## Goal

Build the Textual TUI that presents each generated trade strategy for human approval, with the box-drawing display from the original prompt. Output: an approved trade plan saved to disk, ready for the human to enter into ATP.

## Read first

1. `ARCHITECTURE.md`
2. The "Trade Plan Presentation" mockup in the original `claude_code_prompt.md` Phase 3 — that's the target visual.
3. Textual docs: https://textual.textualize.io/

## Scope

**In scope:**
- `tui/app.py` — Textual `App` entry point. Loads a state JSON (with computed strategies from chunk 4), iterates through sell and buy strategies, presents each one for approval.
- `tui/presenter.py` — the per-strategy approval screen. Renders the box-style summary including:
  - Header (account, strategy, side, ticker, total shares)
  - Market Conditions block (last, prev close, bid×size, ask×size, spread bps, volume vs ADV, % of ADV)
  - Execution Strategy block (order type, limit price, chunks list with shares × price = cost)
  - Reasoning bullets
  - Footer with action keys: `[A]pprove  [M]odify  [S]kip  [Q]uit`
- Modify flow: `M` opens an inline edit screen allowing changes to limit price, order type (Limit/Market with confirmation), per-chunk shares, urgency. Changes update the in-memory plan and re-render.
- Skip flow: `S` marks the strategy as skipped (records the reason if user provides one) and moves on.
- Quit flow: `Q` exits cleanly, saving the partial plan.
- On full approval pass complete: save the approved plan as `plans/plan_{YYYYMMDD}_{HHMM}.json`. Schema is the same state JSON with each strategy annotated `approval_status: Literal["approved","modified","skipped"]` and modified strategies retaining their original alongside the override.
- Plain-text export: also write `plans/plan_{YYYYMMDD}_{HHMM}.txt` — a copy-paste-friendly checklist for entering orders into ATP. Format:
  ```
  [ ] Roth IRA — SELL EEM 1,600 shs LIMIT $62.39 DAY     (Prismatic Prudence, chunk s1)
  [ ] Roth IRA — SELL EEM 55 shs LIMIT $62.39 DAY        (Prismatic Prudence, chunk s2)
  ...
  ```
- Tests: `tests/test_presenter.py` using Textual's pilot/snapshot helpers. At minimum: render a fixture strategy and assert key strings appear.

**Out of scope:**
- The monitor view (chunk 6) — separate Textual screen
- Order placement
- Live re-computation when sells fill (chunk 6)
- Real-time quote refresh inside the approval screen — strategies are presented with the snapshot they were generated with; if they're stale, the human re-runs `compute`

## UX details

- Use Textual's `Static` widgets inside a `Container` for the box layout. Don't try to draw box-drawing characters by hand — use Textual's `Panel` or `rich.panel.Panel` rendered via `Static`.
- Color coding: green for tight-spread/low-impact strategies, yellow for patient/wide-spread, red for aggressive/up-day-strength. Color the urgency badge, not the whole panel.
- Footer key bar uses Textual's `Footer` widget with bound `Binding` actions.
- The Modify screen should **not** allow market orders without an explicit second confirmation ("Type MARKET to confirm"). Per `ARCHITECTURE.md`: never market orders without explicit override.
- Numeric input for the limit price should validate against the price sanity check: `abs(new_limit - last) / last < 0.05`. If outside, warn and require confirmation.

## Acceptance criteria

1. `python -m tui.app --plan tests/fixtures/feb27_with_strategies.json` opens the TUI, displays the first strategy, accepts keypresses, advances to the next strategy on `A`, exits on `Q`.
2. After approving all strategies, the app writes `plans/plan_{YYYYMMDD}_{HHMM}.json` and `plan_{YYYYMMDD}_{HHMM}.txt`. Both files validate (JSON against schema; TXT contains all approved chunks).
3. Modify flow: changing a limit price persists into the saved plan and TXT. Changing to market order requires the typed "MARKET" confirmation.
4. Skip flow: skipped strategies appear in the JSON with `approval_status: "skipped"` and are absent from the TXT checklist.
5. Quit-with-partial saves the partial plan and the TXT. Re-running with `--resume` picks up where it left off (deferred-OK if tight on time, but at minimum the partial save must work).
6. `pytest tests/test_presenter.py` passes.

## Notes

- Build the Modify flow last. The straight Approve/Skip/Quit path is the must-have. Modify is nice-to-have if time runs short — defer to tomorrow if so. Update the chunk completion summary noting whether Modify shipped.
- The TXT export is more important than the JSON polish for today's manual-entry workflow. Prioritize that.

## When done

Run a full TUI session against a real Feb 27–style state JSON, approve all strategies, and confirm both output files exist and are correct. Stop.
