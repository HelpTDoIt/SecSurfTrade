# Chunk 2 — State JSON Schema, Compute/Compare CLIs, React Export Button

**Suggested model:** Sonnet 4.6
**Depends on:** chunk 1 complete
**Estimated effort:** 1.5–2 hours

---

## Goal

Define the full **state JSON schema** that bridges the Python engine and the React calculator. Build CLI commands `compute` and `compare`. Add an **Export State** button to the React calculator so it can dump its current state in the same schema. This enables calculator-in-the-loop parity testing.

## Read first

1. `ARCHITECTURE.md` — the **State JSON schema (high level)** section is the contract. This chunk turns it into Pydantic models.
2. `rebalance_calculator.html` — to find the React component state that needs to be exported.

## Scope

**In scope:**
- `state/schema.py`: full Pydantic v2 models for the state JSON, including the optional `execution_state` block. Replace the minimal models from chunk 1.
- `state/importer.py`: load/save state JSON; normalize the React calc's export format if its field names differ from the canonical schema.
- `state/compare.py`: structural diff between two state JSONs, scoped to the `computed` block. Output:
  - `✓ {field} match` (green)
  - `✗ {dotted.path}: engine={a} calc={b}` (red, with absolute and relative diff for numerics)
  - Tolerance: exact equality for share counts and chunk sizes; `abs(a-b) < 1e-6` for monetary floats (sells proceeds, dollar targets) to absorb FP noise from JS↔Python.
- `cli/compute.py`: `python -m cli.compute --inputs ./csvs --signals signals.json --export engine_state.json`
- `cli/compare.py`: `python -m cli.compare --engine engine_state.json --calc calc_export.json`
- **React calc modification:** add an "Export State" button that downloads a JSON file matching the canonical schema. The button goes near the existing CSV import / signal entry controls. Use a `Blob` + `URL.createObjectURL` pattern; no external libraries needed.
- `tests/test_compare.py`: round-trip identity test (export from engine, re-import, re-export, diff = empty). Mismatch tests with seeded diffs.
- `tests/fixtures/calc_export_feb27.json`: capture from the modified React calc using the Feb 27 test data.

**Out of scope:**
- React calc **Import** State button (deferred — not on today's critical path)
- Live sync between engine and React calc (Phase 2 work)
- Schema versioning beyond the `schema_version: "1.0"` field (no migrations needed yet)

## Schema details (Pydantic models)

Mirror the JSON shape in `ARCHITECTURE.md`. Specific notes:

- `schema_version: Literal["1.0"]`
- `generator: Literal["engine", "react_calc"]`
- All datetimes are ISO-8601 with timezone offset.
- `inputs.config` should have sensible defaults so a caller can omit it: `ex_div_check=True`, `polling_seconds=45`, `stall_threshold_seconds=300`.
- `computed.sell_chunks` and `computed.buy_chunks` each have `chunk_id` strings unique within their list (`"s1"`, `"s2"`, `"b1"`, ...). The chunk_id is the join key for `execution_state.fills`.
- `execution_state` is `Optional`. When `None`, the engine treats sells as estimated proceeds. When present, the engine recomputes buy allocations using `actual_proceeds_by_account` instead of estimates — but **don't implement that recompute path in this chunk**; just allow the field to deserialize. The recompute is wired in chunk 6.

## React calc Export button — implementation hints

The React calculator already holds all the state in component state hooks. The Export button needs to:
1. Build an object matching the canonical schema using current state.
2. Set `generator: "react_calc"` and `schema_version: "1.0"`.
3. Set `generated_at` to `new Date().toISOString()`.
4. `JSON.stringify(state, null, 2)` → `Blob([json], {type: 'application/json'})` → `URL.createObjectURL(blob)` → trigger download with filename `calc_export_{YYYYMMDD}_{HHMM}.json`.

If the React state's internal field names don't match the canonical schema, do the rename in the export function — do **not** rename React's internal state. The canonical schema is the contract; the React calc adapts to it on export.

## Acceptance criteria

1. `state/schema.py` validates the JSON in `ARCHITECTURE.md`'s example block (use it as a Pydantic round-trip test).
2. `python -m cli.compute --inputs tests/fixtures/ --signals tests/fixtures/signals.json --export /tmp/engine_state.json` produces a valid state JSON.
3. The React calc has an **Export State** button visible in the UI. Clicking it downloads a JSON file. That file passes Pydantic validation against the canonical schema.
4. `python -m cli.compare --engine /tmp/engine_state.json --calc tests/fixtures/calc_export_feb27.json` runs without errors and reports either all-match or specific diffs.
5. `pytest tests/test_compare.py` passes, including round-trip and seeded-diff cases.
6. **Parity gate:** running compare on the Feb 27 fixture yields zero diffs in the `computed` block. If any diffs remain, they must be debugged and fixed in `engine/` before this chunk is considered done — the whole point of calculator-in-the-loop is to lock parity here.

## Notes

- If parity diffs surface during this chunk, fix them in the engine, **not** the comparator. Loose tolerances mask bugs.
- Keep the comparator's output human-scannable. A wall of green checkmarks with one red line in the middle should be obvious. Use `rich` for formatting.
- The React Export button is a one-time UI add. Test it manually after editing the HTML by loading it in a browser, populating Feb 27 data, and clicking Export.

## When done

Run `python -m cli.compare` against the Feb 27 fixture and paste the all-green output as confirmation. Stop.
