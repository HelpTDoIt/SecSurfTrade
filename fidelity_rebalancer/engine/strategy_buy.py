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

from dataclasses import dataclass
from datetime import date
from typing import Optional

from adapters import Level2Snapshot, QuoteSnapshot
from engine.chunker import build_buy_chunks, round_to_tick, tick
from engine.decision_context import DecisionContext
from engine.spread_context import SpreadContext
from engine.strategy_sell import get_adv  # cached per-symbol; reuse
from state.schema import BuyAllocationRecord, BuyStrategy, ChunkRecord


@dataclass
class _Features:
    spread_bps: float
    midpoint: float
    rel_vol: Optional[float]
    pct_of_adv: Optional[float]
    px_tick: float
    vwap: Optional[float] = None


def _features(
    buy: BuyAllocationRecord,
    quote: QuoteSnapshot,
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
    return _Features(
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
    return f"Order is {f.pct_of_adv:.2f}% of 30-day ADV ({buy.share_target} sh)."


def _vol_bullet(f: _Features) -> str:
    if f.rel_vol is None:
        return "Recent session volume vs ADV: unknown."
    return f"Session volume is {f.rel_vol:.2f}× ADV."


def _decide(
    f: _Features,
    buy: BuyAllocationRecord,
    quote: QuoteSnapshot,
    spread_ctx: Optional[SpreadContext] = None,
) -> tuple[str, str, float, list[str], float]:
    """
    Returns (rule, urgency, limit_price, reasoning, depth_pct_override).
    `depth_pct_override` is the per-chunk top-3-depth cap (default 0.25).
    """
    sc = spread_ctx or SpreadContext.default()
    tight = f.spread_bps < sc.tight_bps
    wide  = f.spread_bps > sc.wide_bps
    healthy_vol = f.rel_vol is not None and f.rel_vol > 1.0
    large_pos   = f.pct_of_adv is not None and f.pct_of_adv > 3.0

    # Rule 1: tight spread + good volume → ask
    if tight and healthy_vol:
        limit = round_to_tick(quote.ask, quote.ask)
        return ("tight_spread_good_volume", "normal", limit, [
            _spread_bullet(f),
            _vol_bullet(f),
            _adv_bullet(f, buy),
            f"LIMIT at ask ${limit:.4f} — likely to fill quickly.",
        ], 0.25)

    # Rule 2: wide spread → midpoint
    if wide:
        limit = round_to_tick(f.midpoint, quote.ask or quote.bid)
        return ("wide_spread", "patient", limit, [
            _spread_bullet(f),
            "Wide spread — split the difference.",
            f"LIMIT at midpoint ${limit:.4f}.",
            _adv_bullet(f, buy),
        ], 0.25)

    # Rule 3: large position → ask−1 tick + smaller chunks
    if large_pos:
        limit = round_to_tick(quote.ask - f.px_tick, quote.ask)
        return ("large_position", "patient", limit, [
            _spread_bullet(f),
            _adv_bullet(f, buy),
            "Large position (>3% ADV) — smaller chunks to limit market impact.",
            f"LIMIT at ask−1 tick ${limit:.4f}.",
        ], 0.125)   # half the default depth cap

    # Rule 4: buying below VWAP → favorable, take the fill at ask
    if f.vwap and quote.last < f.vwap * 0.999:
        limit = round_to_tick(quote.ask, quote.ask)
        return ("below_vwap", "normal", limit, [
            _spread_bullet(f),
            f"Last ${quote.last:.4f} is below VWAP ${f.vwap:.4f} — favorable entry.",
            f"LIMIT at ask ${limit:.4f}.",
        ], 0.25)

    # Rule 5: buying above VWAP → paying up, be patient
    if f.vwap and quote.last > f.vwap * 1.002:
        limit = round_to_tick(f.midpoint, quote.ask or quote.bid)
        return ("above_vwap", "patient", limit, [
            _spread_bullet(f),
            f"Last ${quote.last:.4f} is above VWAP ${f.vwap:.4f} — paying up, be patient.",
            f"LIMIT at midpoint ${limit:.4f}.",
        ], 0.25)

    # Default: ask, normal
    limit = round_to_tick(quote.ask or quote.last or quote.bid, quote.ask or quote.bid)
    return ("default", "normal", limit, [
        _spread_bullet(f),
        _adv_bullet(f, buy),
        f"No special condition — LIMIT at ask ${limit:.4f}.",
    ], 0.25)


# ── Buy-side urgency escalation ──────────────────────────────────────────
# Time-based checkpoints (buy side only — sells don't consistently hit thresholds).
#   0-90 min  (9:30-11:00): use rule's urgency as-is
#  90-210 min (11:00-1:00): patient → normal, nudge limit toward ask
# 210-330 min  (1:00-3:00): any → aggressive, limit at ask
# 330+ min     (3:00-4:00): aggressive, limit at ask+1 tick

_ESCALATION_CHECKPOINTS = [
    (90,  "normal",     0.75),   # 75% toward ask from current limit
    (210, "aggressive", 1.0),    # at ask
    (330, "aggressive", 1.0),    # at ask + 1 tick (handled specially)
]


def _escalate_buy(
    urgency: str,
    limit_price: float,
    reasoning: list[str],
    quote: QuoteSnapshot,
    px_tick: float,
    market_minutes: Optional[int],
) -> tuple[str, float, list[str]]:
    """Escalate buy urgency based on time-of-day.  Returns (urgency, limit, reasoning)."""
    if market_minutes is None or market_minutes < 0:
        return urgency, limit_price, reasoning

    ask = quote.ask or quote.last or quote.bid
    if not ask or ask <= 0:
        return urgency, limit_price, reasoning

    # Find which checkpoint applies (the last one whose threshold ≤ market_minutes)
    target_urgency = None
    ask_frac = 0.0
    for threshold, t_urg, frac in _ESCALATION_CHECKPOINTS:
        if market_minutes >= threshold:
            target_urgency = t_urg
            ask_frac = frac
        else:
            break

    if target_urgency is None:
        return urgency, limit_price, reasoning

    # Past last checkpoint: ask + 1 tick
    if market_minutes >= _ESCALATION_CHECKPOINTS[-1][0]:
        new_limit = round_to_tick(ask + px_tick, ask)
        return "aggressive", new_limit, reasoning + [
            f"Urgency escalation: {market_minutes} min into session (≥{_ESCALATION_CHECKPOINTS[-1][0]} min). "
            f"LIMIT raised to ask+1 tick ${new_limit:.4f} to ensure same-day fill.",
        ]

    _URGENCY_RANK = {"patient": 0, "normal": 1, "aggressive": 2}
    if _URGENCY_RANK.get(target_urgency, 0) <= _URGENCY_RANK.get(urgency, 0):
        return urgency, limit_price, reasoning

    new_limit = round_to_tick(
        limit_price + (ask - limit_price) * ask_frac, ask
    )
    return target_urgency, new_limit, reasoning + [
        f"Urgency escalation: {market_minutes} min into session → {target_urgency}. "
        f"LIMIT adjusted to ${new_limit:.4f}.",
    ]


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

    feats = _features(buy, quote, adv, vwap=ctx.vwap)
    rule, urgency, limit_price, reasoning, depth_override = _decide(feats, buy, quote, ctx.spread_ctx)

    urgency, limit_price, reasoning = _escalate_buy(
        urgency, limit_price, reasoning, quote, feats.px_tick, ctx.market_minutes,
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
