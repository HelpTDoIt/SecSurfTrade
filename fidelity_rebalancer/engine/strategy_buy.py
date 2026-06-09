"""
Buy-side strategy generator.

`generate_buy_strategy(buy, quote, l2, vol5min, ...)` returns a tuple of
(BuyStrategy, list[ChunkRecord]).  The first decision rule that matches wins.

Rules (in priority order):
  1. Tight spread (<5 bps) + healthy volume (rel_vol > 1.0)
       → LIMIT at ask, urgency=normal.
  2. Wide spread (>10 bps)
       → LIMIT at midpoint, urgency=patient.
  3. Large position (>3% ADV)
       → LIMIT at ask−1 tick, smaller chunks (½ depth cap), urgency=patient.
  Default: LIMIT at ask, urgency=normal.

Buy-side budgets are bounded by `buy.dollar_target`; chunk shares are
rounded so the total cost across all chunks ≤ budget.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from adapters import Level2Snapshot, QuoteSnapshot
from engine import observability
from engine.chunker import build_buy_chunks, round_to_tick, tick
from engine.decision_context import DecisionContext
from engine.escalation import _escalate
from engine.size_context import PositionSizeContext
from engine.spread_context import SpreadContext
from engine.strategy_sell import get_adv  # cached per-symbol; reuse
from state.schema import BuyAllocationRecord, BuyStrategy, ChunkRecord

_log = logging.getLogger(__name__)


@dataclass
class _Features:
    spread_bps: float
    midpoint: float
    rel_vol: Optional[float]
    pct_of_adv: Optional[float]
    px_tick: float
    vwap: Optional[float] = None
    imbalance: Optional[float] = None


def _features(
    buy: BuyAllocationRecord,
    quote: QuoteSnapshot,
    l2: Level2Snapshot,
    adv: Optional[float],
    vwap: Optional[float] = None,
) -> _Features:
    midpoint = (quote.bid + quote.ask) / 2.0 if (quote.bid and quote.ask) else 0.0
    spread = max(0.0, quote.ask - quote.bid)
    spread_bps = (spread / midpoint) * 10000.0 if midpoint > 0 else 0.0
    rel_vol = (quote.volume / adv) if (adv and adv > 0) else None
    shares_target = float(buy.share_target or 0)
    pct_of_adv = (shares_target / adv * 100.0) if (adv and adv > 0) else None
    px_tick = tick(quote.ask or quote.bid or quote.last or 1.0)
    total_bid = sum(getattr(b, 'size', 0) for b in getattr(l2, 'bids', []))
    total_ask = sum(getattr(a, 'size', 0) for a in getattr(l2, 'asks', []))
    total = total_bid + total_ask
    imbalance = (total_bid / total) if total > 0 else None

    return _Features(
        imbalance=imbalance,
        spread_bps=spread_bps,
        midpoint=midpoint,
        rel_vol=rel_vol,
        pct_of_adv=pct_of_adv,
        px_tick=px_tick,
        vwap=vwap if (vwap and vwap > 0) else None,
    )


def _spread_bullet(f: _Features) -> str:
    return f"Spread is {f.spread_bps:.1f} bps."


def _adv_bullet(f: _Features, buy: BuyAllocationRecord) -> str:
    if f.pct_of_adv is None:
        return "ADV: unknown — sizing assumes book depth alone."
    return f"Order is {f.pct_of_adv:.2f}% of ADV ({buy.share_target} sh)."


def _vol_bullet(f: _Features) -> str:
    if f.rel_vol is None:
        return "Recent session volume vs ADV: unknown."
    return f"Session volume is {f.rel_vol:.2f}× ADV."


def _decide(
    f: _Features,
    buy: BuyAllocationRecord,
    quote: QuoteSnapshot,
    spread_ctx: Optional[SpreadContext] = None,
    size_ctx: Optional[PositionSizeContext] = None,
) -> tuple[str, str, float, list[str], float]:
    """
    Returns (rule, urgency, limit_price, reasoning, depth_pct_override).
    `depth_pct_override` is the per-chunk top-3-depth cap (default 0.25).
    size_ctx: per-class %ADV cutoffs (G-5).  None = legacy 3% default.
    """
    sc = spread_ctx or SpreadContext.default()
    zc = size_ctx or PositionSizeContext.default()
    tight = f.spread_bps < sc.tight_bps
    wide  = f.spread_bps > sc.wide_bps
    healthy_vol = f.rel_vol is not None and f.rel_vol > 1.0
    large_pos   = f.pct_of_adv is not None and f.pct_of_adv > zc.buy_large_pct

    # Rule 1: tight spread + good volume → ask
    if tight and healthy_vol:
        limit = round_to_tick(quote.bid, quote.bid)
        return ("tight_spread_good_volume", "patient", limit, [
            _spread_bullet(f),
            _vol_bullet(f),
            _adv_bullet(f, buy),
            f"LIMIT at bid ${limit:.4f} — patient entry.",
        ], 0.25)

    # Rule 2: wide spread → midpoint
    if wide:
        limit = round_to_tick(quote.bid + f.px_tick, quote.bid)
        return ("wide_spread", "patient", limit, [
            _spread_bullet(f),
            "Wide spread — avoid crossing.",
            f"LIMIT at bid+1 tick ${limit:.4f}.",
            _adv_bullet(f, buy),
        ], 0.25)

    # Rule 3: large position → ask−1 tick + smaller chunks
    if large_pos:
        limit = round_to_tick(quote.bid, quote.bid)
        return ("large_position", "patient", limit, [
            _spread_bullet(f),
            _adv_bullet(f, buy),
            f"Large position (>{zc.buy_large_pct:g}% ADV) — smaller chunks to limit market impact.",
            f"LIMIT at bid ${limit:.4f}.",
        ], 0.125)   # half the default depth cap

    # Rule 3.5: Order book strongly bid-heavy (> 0.80) -> adverse for buyer
    if f.imbalance is not None and f.imbalance > 0.80:
        limit = round_to_tick(quote.bid + f.px_tick, quote.bid)
        return ("bid_heavy_book", "aggressive", limit, [
            _spread_bullet(f),
            f"Order book is heavily bid-skewed (imbalance {f.imbalance:.2f}) — expecting upward price pressure.",
            f"LIMIT at bid+1 tick ${limit:.4f} to secure fill.",
        ], 0.25)

    # Rule 3.6: Order book strongly ask-heavy (< 0.20) -> favorable for buyer
    if f.imbalance is not None and f.imbalance < 0.20:
        limit = round_to_tick(quote.bid - f.px_tick, quote.bid)
        return ("ask_heavy_book", "patient", limit, [
            _spread_bullet(f),
            f"Order book is heavily ask-skewed (imbalance {f.imbalance:.2f}) — expecting downward price pressure.",
            f"LIMIT at bid-1 tick ${limit:.4f} to wait for drop.",
        ], 0.25)

    # Rule 4: buying below VWAP → favorable, take the fill at ask
    if f.vwap and quote.last < f.vwap * 0.999:
        limit = round_to_tick(quote.bid, quote.bid)
        return ("below_vwap", "patient", limit, [
            _spread_bullet(f),
            f"Last ${quote.last:.4f} is below VWAP ${f.vwap:.4f} — favorable entry.",
            f"LIMIT at bid ${limit:.4f}.",
        ], 0.25)

    # Rule 5: buying above VWAP → paying up, be patient
    if f.vwap and quote.last > f.vwap * 1.002:
        limit = round_to_tick(quote.bid, quote.bid)
        return ("above_vwap", "patient", limit, [
            _spread_bullet(f),
            f"Last ${quote.last:.4f} is above VWAP ${f.vwap:.4f} — paying up, be patient.",
            f"LIMIT at bid ${limit:.4f}.",
        ], 0.25)

    # Default: ask, normal
    limit = round_to_tick(quote.bid or quote.last or quote.ask, quote.bid or quote.ask)
    return ("default", "normal", limit, [
        _spread_bullet(f),
        _adv_bullet(f, buy),
        f"No special condition — LIMIT at bid ${limit:.4f}.",
    ], 0.25)


# ── Buy-side urgency escalation ──────────────────────────────────────────
# The time-of-day ramp lives in the side-aware engine.escalation._escalate
# (buy nudges the limit toward the ask).  _escalate_buy is a thin shim that
# delegates there; it is kept only for its stable name and the buy-parity test.


def _escalate_buy(
    urgency: str,
    limit_price: float,
    reasoning: list[str],
    quote: QuoteSnapshot,
    px_tick: float,
    market_minutes: Optional[int],
    cumulative_volume: Optional[float] = None,
    adv: Optional[float] = None,
) -> tuple[str, float, list[str]]:
    """Escalate buy urgency by time-of-day (delegates to engine.escalation).

    Thin wrapper over ``_escalate("buy", ...)``: the buy side nudges the limit
    toward the ask.  Returns ``(urgency, limit, reasoning)``.
    """
    return _escalate(
        "buy", urgency, limit_price, reasoning, quote, px_tick, market_minutes, cumulative_volume, adv
    )


def _chunk_id(account: str, ticker: str, idx: int) -> str:
    slug = account.replace(" ", "_").replace("-", "").replace("__", "_")
    return f"b_{slug}_{ticker}_{idx}"


def generate_buy_strategy(
    buy: BuyAllocationRecord,
    quote: QuoteSnapshot,
    l2: Level2Snapshot,
    vol5min: float,
    *,
    ctx: DecisionContext,
    today: Optional[date] = None,
    max_pct_of_top3_depth: float = 0.25,
    max_pct_of_5min_volume: float = 0.15,
) -> tuple[BuyStrategy, list[ChunkRecord]]:
    """Generate a buy strategy + chunk records for one buy allocation.

    Market inputs (adv, spread_ctx, vwap, market_minutes) arrive bundled in the
    required ``ctx`` (engine.decision_context.DecisionContext).
    """
    adv = ctx.adv if ctx.adv is not None else get_adv(buy.ticker)

    feats = _features(buy, quote, l2, adv, vwap=ctx.vwap)
    rule, urgency, limit_price, reasoning, depth_override = _decide(
        feats, buy, quote, ctx.spread_ctx, size_ctx=ctx.size_ctx,
    )

    urgency, limit_price, reasoning = _escalate_buy(
        urgency, limit_price, reasoning, quote, feats.px_tick, ctx.market_minutes, quote.volume, adv
    )

    # Use the strategy-chosen limit_price to size the budget.
    chunk_dicts = build_buy_chunks(
        buy.dollar_target, limit_price, l2.asks, vol5min,
        max_pct_of_top3_depth=depth_override if rule == "large_position" else max_pct_of_top3_depth,
        max_pct_of_5min_volume=max_pct_of_5min_volume,
    )

    chunks: list[ChunkRecord] = []
    chunk_ids: list[str] = []
    for cd in chunk_dicts:
        cid = _chunk_id(buy.account, buy.ticker, cd["idx"])
        chunk_ids.append(cid)
        chunks.append(ChunkRecord(
            chunk_id=cid,
            account=buy.account,
            strategy=buy.strategy,
            ticker=buy.ticker,
            idx=cd["idx"],
            shares=cd["shares"],
            limit_price=cd["limit_price"],
            cost=cd["cost"],
        ))

    # Data-quality WARNINGs (ticker is allowed at WARNING; share counts are not).
    if adv is None:
        _log.warning("buy %s: ADV unavailable — sizing from book depth only", buy.ticker)
    if not (quote.ask or quote.bid):
        _log.warning("buy %s: no live bid/ask — limit derived from fallback price", buy.ticker)
    # rule/urgency/limit_price below are post-escalation (the entered values).
    _log.debug(
        "buy %s: rule=%s urgency=%s limit=$%.4f chunks=%d "
        "[spread=%.1fbps rel_vol=%s pct_adv=%s]",
        buy.ticker, rule, urgency, limit_price, len(chunks),
        feats.spread_bps,
        f"{feats.rel_vol:.2f}" if feats.rel_vol is not None else "n/a",
        f"{feats.pct_of_adv:.2f}" if feats.pct_of_adv is not None else "n/a",
    )
    observability.record("strategy_decision", {
        "side": "buy",
        "account": buy.account,
        "strategy": buy.strategy,
        "ticker": buy.ticker,
        "share_target": buy.share_target,
        "rule": rule,
        "urgency": urgency,
        "limit_price": limit_price,
        "n_chunks": len(chunks),
        "adv": adv,
        "features": {
            "spread_bps": round(feats.spread_bps, 3),
            "midpoint": feats.midpoint,
            "rel_vol": feats.rel_vol,
            "pct_of_adv": feats.pct_of_adv,
            "vwap": feats.vwap,
        },
    })

    strategy = BuyStrategy(
        account=buy.account,
        strategy=buy.strategy,
        ticker=buy.ticker,
        order_type="LIMIT",
        limit_price=limit_price,
        urgency=urgency,
        rule=rule,
        reasoning=reasoning,
        chunk_ids=chunk_ids,
    )
    return strategy, chunks
