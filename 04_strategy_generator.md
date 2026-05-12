# Chunk 4 — Strategy Generator + Book-Relative Chunker + Ex-Div Check

**Suggested model:** Opus 4.7 (this chunk has the most reasoning load — strategy decision logic + book-relative chunking math)
**Depends on:** chunks 1 and 3 complete
**Estimated effort:** 2–2.5 hours

---

## Goal

Generate sell and buy execution strategies with full human-readable reasoning, using live quotes + Level 2 depth. Replace the dollar-based $100K chunker with a **book-relative chunker**. Add the **ex-dividend check** for first-of-month rebalances.

## Read first

1. `ARCHITECTURE.md`, especially **Order chunking rule** and **Sequential iceberg**.
2. The strategy decision rules in the original `claude_code_prompt.md` Phase 2 — but with the modifications below.

## Scope

**In scope:**
- `engine/chunker.py` — replace the chunk-1 implementation with the book-relative version. Old `$100K` constant goes away. New rule:
  ```
  max_chunk_shares = min(
      config.max_pct_of_top3_depth × sum_of_top3_levels_at_side,
      config.max_pct_of_5min_volume × trailing_5min_volume
  )
  # rounded down to nearest 100
  ```
  Defaults: `max_pct_of_top3_depth=0.25`, `max_pct_of_5min_volume=0.15`. Both come from `inputs.config.chunker`.
- **Ex-dividend check.** New helper in `engine/chunker.py`:
  ```python
  def adjust_prev_close_for_exdiv(symbol, prev_close, today) -> float
  ```
  If `today` is the 1st of any month AND the symbol has an ex-div event today, returns `prev_close - dividend_amount`. Otherwise returns `prev_close` unchanged. Source for ex-div: yfinance's `Ticker.dividends` or a configured static lookup table at `tests/fixtures/exdiv_calendar.json` for tests. The chunker is the only place that calls this helper.
- `engine/strategy_sell.py` — `generate_sell_strategy(sell, quote, l2, vol5min) -> SellStrategy`. Returns a `SellStrategy` Pydantic model with `chunks: list[OrderChunk]`, `order_type`, `limit_price`, `urgency: Literal["normal","aggressive","patient"]`, and a `reasoning: list[str]` of human-readable bullets. Decision logic:
  1. **Tight spread (<5bps), healthy volume (rel_vol>1.0), small position (<2% ADV)** → LIMIT at midpoint, urgency=normal.
  2. **Tight spread but large position (>5% ADV)** → LIMIT at bid, more chunks, urgency=patient.
  3. **Wide spread (>10bps)** → LIMIT at bid+1 tick, urgency=patient. "Avoid crossing."
  4. **Down day (<−2% from prev close)** → LIMIT at prev_close × 0.99, urgency=patient. "May get a bounce fill."
  5. **Up day (>+2% from prev close)** → LIMIT at current bid, urgency=aggressive. "Selling into strength."
  Each rule generates 2–4 reasoning bullets including the actual numbers (spread bps, % ADV, etc.).
- `engine/strategy_buy.py` — `generate_buy_strategy(buy_alloc, quote, l2, vol5min) -> BuyStrategy`. Mirror logic with buy-side adaptations:
  1. **Tight spread, good volume** → LIMIT at ask, urgency=normal.
  2. **Wide spread (>10bps)** → LIMIT at mid, urgency=patient.
  3. **Large position (>3% ADV)** → smaller chunks, LIMIT at ask−1 tick.
  Buy-side budgets are constrained (`dollar_target` from buy_allocations); the strategy must not exceed that budget. Round chunk shares down so the total cost ≤ budget.
- Update `state/schema.py` to include `SellStrategy` and `BuyStrategy` models, and link strategies to chunks via `chunk_ids`.
- Tests: `tests/test_strategy.py` with snapshot-style cases for each rule branch (tight spread small position, tight spread large position, wide spread, down day, up day, large buy). Each test feeds a synthetic quote + L2 book and asserts the rule chosen, the limit price, and the chunk count.
- Tests: `tests/test_chunker.py` extended with book-relative cases:
  - liquid book (deep L2, high 5-min volume) → 1 chunk
  - thin book (shallow L2, low volume) → multiple chunks, each ≤ 25% of top-3 depth
  - 5-minute volume is the binding constraint
  - top-3 depth is the binding constraint
  - 1st-of-month ex-div: prev_close adjusted in the limit basis

**Out of scope:**
- TUI display of strategies (chunk 5)
- Live recompute when fills come in (chunk 6)
- Order placement

## Tick size

- `tick = 0.01` for prices ≥ $1.00, `tick = 0.0001` for sub-dollar (rare for ETFs but safe to handle).
- Use a `tick(price)` helper, don't hardcode `0.01`.

## ADV (average daily volume)

- ADV is needed for the "% of ADV" reasoning bullet. Source: yfinance `history(period='30d')['Volume'].mean()`, cached per-symbol per-session. If yfinance is unavailable, fall back to printing "ADV: unknown" in the reasoning rather than failing.

## Acceptance criteria

1. `pytest tests/test_strategy.py tests/test_chunker.py` passes.
2. Each of the 5 sell rules and 3 buy rules has at least one snapshot test.
3. Book-relative chunker: given a fixture L2 book where top-3 bids sum to 4,000 shares and 5-min volume = 50,000, a 10,000-share sell produces multiple chunks each ≤ 1,000 shares (whichever constraint binds).
4. The ex-div helper, given `today=2026-05-01` and `symbol=SPY` with a $1.50 dividend in fixture data, returns `prev_close - 1.50`.
5. Reasoning text includes actual computed numbers, not placeholders. Sample assertion: a generated reasoning bullet matches `r"Spread is \d+\.\d bps"`.
6. Strategies cleanly serialize into the state JSON via the schema. Round-trip test: serialize → deserialize → re-serialize → byte-identical.

## When done

Run `python -m cli.compute` end-to-end on the Feb 27 fixture (which now should populate strategies and chunks) and re-run `compare` against the calc export. The `computed.sell_chunks` and `computed.buy_chunks` should still match (same shares per chunk) — note that the React calc uses the old $100K rule, so book-relative may legitimately produce different chunk counts. **In that case, add a `--chunker=legacy_dollar` flag to the engine for parity testing and use it in compare**, while production runs use the new chunker. Document this in the chunk completion summary.

Stop after acceptance criteria pass.
