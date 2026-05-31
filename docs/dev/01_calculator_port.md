# Chunk 1 — Calculator Port + Engine Scaffold

**Suggested model:** Sonnet 4.6 (this is mechanical translation work; reasoning load is light)
**Depends on:** none — this is the first chunk
**Estimated effort:** 1.5–2 hours

---

## Goal

Scaffold the `fidelity_rebalancer` project and port the **rebalance calculation logic** from the React calculator (`rebalance_calculator.html`) to a pure-Python engine package. Achieve byte-identical numerical parity with the React calculator on the Feb 27 regression fixture.

## Read first

1. `ARCHITECTURE.md` (in repo root after this chunk creates it) — for module structure and constraints
2. `rebalance_calculator.html` — the source of truth for calculation logic. Specifically these JS functions:
   - `parseCSV` and `consolidate`
   - `calcTrades` (main trade calculation, including the `cashOk` threshold)
   - `allocBuys` (budget allocation across strategies)
   - The drift-minimizing optimizer in `liveBuys` (proportional + greedy)
   - `buildSellChunks` (share-driven, round-to-100)
   - `buildBuyChunks` (budget-constrained, share-driven)
3. The Feb 27 test data embedded in the HTML — copy it into `tests/fixtures/feb27.json`

## Scope

**In scope:**
- Project scaffold: `pyproject.toml`, package layout per `ARCHITECTURE.md`, `__init__.py` files, basic logging setup
- `engine/calculator.py`: port of `parseCSV`, `consolidate`, `calcTrades`, `allocBuys`
- `engine/optimizer.py`: drift-minimizing allocator (proportional then greedy)
- `engine/chunker.py`: **basic** chunker only — direct port of the existing $100K dollar-based rule and round-to-100. The book-relative chunker and ex-div check are chunk 4.
- `state/schema.py`: minimal Pydantic models sufficient for chunk 1's outputs (`Position`, `AccountPortfolio`, `Signal`, `Sell`, `BuyAllocation`, `OrderChunk`). The full schema with `execution_state` is chunk 2.
- `adapters/csv_reader.py`: parse Fidelity CSV exports into `AccountPortfolio` objects
- `tests/test_calculator.py`, `tests/test_optimizer.py`, `tests/test_chunker.py`
- `tests/fixtures/feb27.json` and `tests/fixtures/feb27_expected.json` (the latter is the React calc's known-good output for the same inputs — capture this from the running React calc)

**Out of scope:**
- ATP adapters (chunk 3)
- Strategy reasoning, book-relative chunking, ex-div check (chunk 4)
- TUI (chunk 5)
- Monitor loop (chunk 6)
- The full state JSON with `execution_state` (chunk 2)

## Constraints

- Engine is **pure**. No file I/O, no network, no `print` statements outside CLI entry points. Functions take dataclasses/Pydantic models in, return dataclasses/Pydantic models out.
- Floating-point math: replicate the React calculator's exact arithmetic. If the React calc rounds at a particular step, the Python port rounds at the same step. **Do not "improve" the math.** Parity is the goal; correctness improvements come after parity is proven.
- Use `Decimal` only if the React calculator uses it (it doesn't — it uses JS `Number`). Stick with `float` for parity.
- Account names, strategy names, ticker symbols are case-sensitive throughout.

## Deliverables

```
fidelity_rebalancer/
├── pyproject.toml
├── ARCHITECTURE.md                  # copy from the plan folder
├── engine/
│   ├── __init__.py
│   ├── calculator.py
│   ├── optimizer.py
│   └── chunker.py
├── state/
│   ├── __init__.py
│   └── schema.py
├── adapters/
│   ├── __init__.py
│   └── csv_reader.py
├── tests/
│   ├── __init__.py
│   ├── fixtures/
│   │   ├── feb27.json
│   │   ├── feb27_expected.json
│   │   ├── roth_ira.csv             # sample CSVs from real exports
│   │   ├── rollover_ira.csv
│   │   └── individual_tod.csv
│   ├── test_calculator.py
│   ├── test_optimizer.py
│   └── test_chunker.py
└── logs/
    └── .gitkeep
```

## Acceptance criteria

1. `pip install -e .` succeeds.
2. `pytest` runs all tests and they pass.
3. `tests/test_calculator.py` includes a parity test:
   - Loads `feb27.json` (inputs)
   - Runs `engine.calculator.calc_trades(inputs)`
   - Asserts the output matches `feb27_expected.json` exactly for: `cash_ok`, `one_share_total`, every sell's `shares`, every buy_allocation's `share_target` and `dollar_target`, every chunk's `shares` and `limit_price`.
4. `tests/test_optimizer.py` exercises the proportional + greedy drift-minimizer with at least three contrived cases including: (a) integer-only result, (b) ties broken by index order, (c) budget exactly equal to one-share total.
5. The CSV reader correctly handles the SMH-in-both-Cash-and-Margin consolidation case (Individual-TOD).
6. No I/O happens inside `engine/` modules. Verify by grep: `grep -r "open\|requests\|print" engine/` should return nothing meaningful.

## Notes for the porting work

- The React calc's `liveBuys` function is the trickiest port. Read it carefully end-to-end before writing Python. The two-phase logic (proportional floor, then greedy one-share assignment to whichever strategy has the largest remaining drift) must be replicated exactly.
- Tie-breaking matters. JavaScript object key order is insertion order; the equivalent in Python is preserving the order in which strategies appear in the input. Use `dict` (Python 3.7+ preserves insertion order) — do not sort.
- The `cashOk` threshold (`oneShare = sum of one share of each strategy's ETF at close price`; `cashOk = deployable_cash > oneShare`) determines whether the optimizer runs across all strategies or only signal-changing ones. Get this right or every downstream number will drift.

## When done

Print a one-line summary of which tests passed and stop. The next chunk will build on this foundation.
