"""
Sell-side strategy generator.

`generate_sell_strategy(sell, quote, l2, vol5min, ...)` returns a tuple of
(SellStrategy, list[ChunkRecord]).  The first decision rule that matches wins.

Rules (in priority order):
  1. Tight spread (<5 bps) + healthy volume (rel_vol > 1.0) + small position (<2% ADV)
       → LIMIT at midpoint, urgency=normal.
  2. Tight spread + large position (>5% ADV)
       → LIMIT at bid, urgency=patient.
  3. Wide spread (>10 bps)
       → LIMIT at bid+1 tick, urgency=patient ("avoid crossing").
  4. Down day (last < prev_close × 0.98)
       → LIMIT at prev_close × 0.99, urgency=patient ("may get a bounce fill").
  5. Up day (last > prev_close × 1.02)
       → LIMIT at current bid, urgency=aggressive ("selling into strength").
  Default (none of the above): LIMIT at midpoint, urgency=normal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Optional

from adapters import Level2Snapshot, QuoteSnapshot
from engine.chunker import (
    adjust_prev_close_for_exdiv,
    build_gap_capture_chunks,
    build_sell_chunks,
    round_to_tick,
    tick,
)
from engine.decision_context import DecisionContext
from engine.spread_context import SpreadContext
from state.schema import ChunkRecord, SellRecord, SellStrategy


# ── ADV helper (cached per-symbol per-session) ────────────────────────────

@lru_cache(maxsize=None)
def get_adv(symbol: str) -> Optional[float]:
    """30-day average daily volume from yfinance.  None if unavailable."""
    try:
        import yfinance as yf  # noqa: F401
    except Exception:
        return None
    try:
        hist = yf.Ticker(symbol).history(period="30d")
        if hist is None or hist.empty:
            return None
        return float(hist["Volume"].mean())
    except Exception:
        return None


# ── Feature extraction ────────────────────────────────────────────────────

@dataclass
class _Features:
    spread_bps: float
    midpoint: float
    rel_vol: Optional[float]            # quote.volume / adv
    pct_of_adv: Optional[float]         # sell.shares / adv × 100
    day_change_pct: Optional[float]     # (last - adj_prev_close) / adj_prev_close × 100
    adj_prev_close: float
    px_tick: float
    vwap: Optional[float] = None        # intraday VWAP (ATP only; None if unavailable)


def _features(
    sell: SellRecord,
    quote: QuoteSnapshot,
    today: date,
    adv: Optional[float],
    exdiv_calendar: Optional[dict],
    vwap: Optional[float] = None,
) -> _Features:
    midpoint = (quote.bid + quote.ask) / 2.0 if (quote.bid and quote.ask) else 0.0
    spread = max(0.0, quote.ask - quote.bid)
    spread_bps = (spread / midpoint) * 10000.0 if midpoint > 0 else 0.0

    rel_vol = (quote.volume / adv) if (adv and adv > 0) else None
    pct_of_adv = (sell.shares / adv * 100.0) if (adv and adv > 0) else None

    adj_prev_close = adjust_prev_close_for_exdiv(
        sell.ticker, quote.prev_close, today, calendar=exdiv_calendar
    )
    if adj_prev_close > 0 and quote.last > 0:
        day_change_pct = (quote.last - adj_prev_close) / adj_prev_close * 100.0
    else:
        day_change_pct = None

    px_tick = tick(quote.bid or quote.ask or quote.last or 1.0)
    return _Features(
        spread_bps=spread_bps,
        midpoint=midpoint,
        rel_vol=rel_vol,
        pct_of_adv=pct_of_adv,
        day_change_pct=day_change_pct,
        adj_prev_close=adj_prev_close,
        px_tick=px_tick,
        vwap=vwap if (vwap and vwap > 0) else None,
    )


# ── Reasoning bullet helpers ──────────────────────────────────────────────

def _spread_bullet(f: _Features) -> str:
    return f"Spread is {f.spread_bps:.1f} bps."


def _adv_bullet(f: _Features, sell: SellRecord) -> str:
    if f.pct_of_adv is None:
        return f"ADV: unknown — sizing assumes book depth alone."
    return f"Order is {f.pct_of_adv:.2f}% of 30-day ADV ({sell.shares:.0f} sh)."


def _vol_bullet(f: _Features) -> str:
    if f.rel_vol is None:
        return "Recent session volume vs ADV: unknown."
    return f"Session volume is {f.rel_vol:.2f}× ADV."


# ── Rule selection ────────────────────────────────────────────────────────

def _decide(
    f: _Features,
    sell: SellRecord,
    quote: QuoteSnapshot,
    spread_ctx: Optional[SpreadContext] = None,
    market_minutes: Optional[int] = None,
) -> tuple[str, str, float, list[str]]:
    """Returns (rule, urgency, limit_price, reasoning_bullets).

    market_minutes: minutes since market open (9:30 ET).  None = unknown.
    """
    sc = spread_ctx or SpreadContext.default()
    tight  = f.spread_bps < sc.tight_bps
    wide   = f.spread_bps > sc.wide_bps
    healthy_vol = f.rel_vol is not None and f.rel_vol > 1.0
    small_pos   = f.pct_of_adv is not None and f.pct_of_adv < 2.0
    large_pos   = f.pct_of_adv is not None and f.pct_of_adv > 5.0

    # Rule 0: opening gap capture — stock gapped up, first 30 min of trading
    gap_up = (f.day_change_pct is not None and f.day_change_pct > 0.5
              and f.adj_prev_close > 0)
    in_opening = market_minutes is not None and 0 <= market_minutes <= 30
    if gap_up and in_opening and sell.shares >= 100:
        gap_price = round_to_tick(f.adj_prev_close * 0.99, quote.bid)
        standard_price = round_to_tick(f.midpoint or quote.last, quote.bid)
        sweep_price = round_to_tick(quote.bid, quote.bid)
        limit = gap_price
        return ("gap_capture", "aggressive", limit, [
            _spread_bullet(f),
            f"Gap up +{f.day_change_pct:.2f}% at open, {market_minutes} min into session.",
            f"Phase 1 (30% shares): gap capture at prev_close×0.99 = ${gap_price:.4f}.",
            f"Phase 2 (50% shares): standard at ${standard_price:.4f}.",
            f"Phase 3 (20% shares): sweep at bid ${sweep_price:.4f}.",
        ])

    # Rule 1: tight spread + healthy volume + small position → MID
    if tight and healthy_vol and small_pos:
        limit = round_to_tick(f.midpoint, quote.bid or quote.ask)
        return ("tight_spread_small_position", "normal", limit, [
            _spread_bullet(f),
            _adv_bullet(f, sell),
            _vol_bullet(f),
            f"LIMIT at midpoint ${limit:.4f}.",
        ])

    # Rule 2: tight spread + large position → BID
    if tight and large_pos:
        limit = round_to_tick(quote.bid, quote.bid)
        return ("tight_spread_large_position", "patient", limit, [
            _spread_bullet(f),
            _adv_bullet(f, sell),
            f"Position is large (>5% ADV) — drip the order in via more chunks.",
            f"LIMIT at bid ${limit:.4f} to avoid pushing the market.",
        ])

    # Rule 3: wide spread → BID + 1 tick
    if wide:
        limit = round_to_tick(quote.bid + f.px_tick, quote.bid)
        return ("wide_spread", "patient", limit, [
            _spread_bullet(f),
            "Wide spread — avoid crossing.",
            f"LIMIT at bid+1 tick ${limit:.4f}.",
            _adv_bullet(f, sell),
        ])

    # Rule 4: down day → LIMIT at prev_close × 0.99
    if f.day_change_pct is not None and f.day_change_pct < -2.0:
        limit = round_to_tick(f.adj_prev_close * 0.99, quote.bid or quote.last)
        return ("down_day", "patient", limit, [
            _spread_bullet(f),
            f"Down day: last ${quote.last:.4f} is {f.day_change_pct:.2f}% from prev close ${f.adj_prev_close:.4f}.",
            "May get a bounce fill — patient pricing.",
            f"LIMIT at prev_close × 0.99 = ${limit:.4f}.",
        ])

    # Rule 5: up day → LIMIT at current bid
    if f.day_change_pct is not None and f.day_change_pct > 2.0:
        limit = round_to_tick(quote.bid, quote.bid)
        return ("up_day", "aggressive", limit, [
            _spread_bullet(f),
            f"Up day: last ${quote.last:.4f} is +{f.day_change_pct:.2f}% from prev close ${f.adj_prev_close:.4f}.",
            "Selling into strength.",
            f"LIMIT at current bid ${limit:.4f}.",
        ])

    # Rule 6: selling above VWAP → take the fill at current last
    if f.vwap and quote.last > f.vwap * 1.001:
        limit = round_to_tick(quote.last, quote.bid or quote.last)
        return ("above_vwap", "aggressive", limit, [
            _spread_bullet(f),
            f"Last ${quote.last:.4f} is above VWAP ${f.vwap:.4f} — selling into favorable price.",
            f"LIMIT at last ${limit:.4f}.",
        ])

    # Rule 7: selling below VWAP → be patient, price near bid
    if f.vwap and quote.last < f.vwap * 0.999:
        limit = round_to_tick(quote.bid + f.px_tick, quote.bid)
        return ("below_vwap", "patient", limit, [
            _spread_bullet(f),
            f"Last ${quote.last:.4f} is below VWAP ${f.vwap:.4f} — avoid selling into weakness.",
            f"LIMIT at bid+1 tick ${limit:.4f}, patient.",
        ])

    # Default: midpoint, normal
    limit = round_to_tick(f.midpoint or quote.last or quote.bid, quote.bid or quote.last)
    return ("default", "normal", limit, [
        _spread_bullet(f),
        _adv_bullet(f, sell),
        f"No special condition triggered — LIMIT at midpoint ${limit:.4f}.",
    ])


# ── Public entry point ────────────────────────────────────────────────────

def _chunk_id(account: str, ticker: str, idx: int) -> str:
    slug = account.replace(" ", "_").replace("-", "").replace("__", "_")
    return f"s_{slug}_{ticker}_{idx}"


def generate_sell_strategy(
    sell: SellRecord,
    quote: QuoteSnapshot,
    l2: Level2Snapshot,
    vol5min: float,
    *,
    ctx: DecisionContext,
    today: Optional[date] = None,
    exdiv_calendar: Optional[dict] = None,
    max_pct_of_top3_depth: float = 0.25,
    max_pct_of_5min_volume: float = 0.15,
) -> tuple[SellStrategy, list[ChunkRecord]]:
    """
    Generate a sell strategy + matching chunk records for a single SellRecord.
    Returns (strategy, chunk_records).

    Market inputs (adv, spread_ctx, vwap, market_minutes) arrive bundled in the
    required ``ctx`` (engine.decision_context.DecisionContext).
    """
    today = today or date.today()

    adv = ctx.adv if ctx.adv is not None else get_adv(sell.ticker)

    feats = _features(sell, quote, today, adv, exdiv_calendar, vwap=ctx.vwap)
    rule, urgency, limit_price, reasoning = _decide(
        feats, sell, quote, ctx.spread_ctx, market_minutes=ctx.market_minutes,
    )

    if rule == "gap_capture":
        gap_price = round_to_tick(feats.adj_prev_close * 0.99, quote.bid)
        standard_price = round_to_tick(feats.midpoint or quote.last, quote.bid)
        sweep_price = round_to_tick(quote.bid, quote.bid)
        chunk_dicts = build_gap_capture_chunks(
            sell.shares, gap_price, standard_price, sweep_price,
        )
    else:
        chunk_dicts = build_sell_chunks(
            sell.shares, limit_price, l2.bids, vol5min,
            max_pct_of_top3_depth=max_pct_of_top3_depth,
            max_pct_of_5min_volume=max_pct_of_5min_volume,
        )

    chunks: list[ChunkRecord] = []
    chunk_ids: list[str] = []
    for cd in chunk_dicts:
        cid = _chunk_id(sell.account, sell.ticker, cd["idx"])
        chunk_ids.append(cid)
        chunks.append(ChunkRecord(
            chunk_id=cid,
            account=sell.account,
            strategy=sell.strategy,
            ticker=sell.ticker,
            idx=cd["idx"],
            shares=cd["shares"],
            limit_price=cd["limit_price"],
            cost=cd["cost"],
        ))

    strategy = SellStrategy(
        account=sell.account,
        strategy=sell.strategy,
        ticker=sell.ticker,
        order_type="LIMIT",
        limit_price=limit_price,
        urgency=urgency,
        rule=rule,
        reasoning=reasoning,
        chunk_ids=chunk_ids,
    )
    return strategy, chunks
