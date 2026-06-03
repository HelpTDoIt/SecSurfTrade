"""
Stall detection and re-quote suggestion engine.

Pure functions — no I/O.

detect_stalls(orders, threshold_seconds, now) -> list[StallEvent]
recommend_requote(stall, side, quote, ...)    -> RequoteSuggestion
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Optional

from adapters import Level, Level2Snapshot, OrderRow, OrderStatus, QuoteSnapshot
from engine.decision_context import DecisionContext
from state.schema import BuyAllocationRecord, SellRecord


@dataclass
class StallEvent:
    chunk_id: str          # matched via order_id on OrderRow
    original_limit: float
    filled_qty: float
    remaining_qty: float
    seconds_stalled: float


@dataclass
class RequoteSuggestion:
    chunk_id: str
    new_limit: float
    remaining_qty: float
    rationale: list[str] = field(default_factory=list)


def detect_stalls(
    orders: list[OrderRow],
    threshold_seconds: int,
    now: datetime,
) -> list[StallEvent]:
    """
    Return one StallEvent for each order that is PartiallyFilled and has
    not progressed for at least `threshold_seconds`.
    """
    stalls: list[StallEvent] = []
    for row in orders:
        if row.status != OrderStatus.PartiallyFilled:
            continue
        elapsed = (now - row.last_update_at).total_seconds()
        if elapsed >= threshold_seconds:
            stalls.append(
                StallEvent(
                    chunk_id=row.order_id,
                    original_limit=row.limit_price,
                    filled_qty=row.filled_qty,
                    remaining_qty=row.qty - row.filled_qty,
                    seconds_stalled=elapsed,
                )
            )
    return stalls


def _book_from_quote(quote: QuoteSnapshot) -> Level2Snapshot:
    """Synthesize a one-level book from the quote's top-of-book.

    The stall advisor only has a top-of-book quote, not a full L2 snapshot, so
    we present the best bid/ask as a single deep level.  Depth is set generous
    enough that the chunker's depth caps never re-price the clip below the
    rule-chosen limit — re-quote pricing is driven by the *rule*, not by sizing.
    """
    big = max(int(quote.bid_size or 0), int(quote.ask_size or 0), 1_000_000)
    bid = quote.bid or quote.last or quote.ask
    ask = quote.ask or quote.last or quote.bid
    return Level2Snapshot(
        symbol=quote.symbol,
        bids=[Level(price=bid, size=big, mpid="STALL")],
        asks=[Level(price=ask, size=big, mpid="STALL")],
        ts=quote.ts,
    )


def recommend_requote(
    stall: StallEvent,
    side: Literal["buy", "sell"],
    quote: QuoteSnapshot,
    *,
    ctx: Optional[DecisionContext] = None,
    l2: Optional[Level2Snapshot] = None,
    vol5min: float = 1_000_000.0,
    today: Optional[date] = None,
) -> RequoteSuggestion:
    """
    Propose a new limit price for a stalled clip by **re-running full rule
    selection** against the fresh quote (F-6).  The winning rule may differ from
    the one that priced the original order — e.g. a book that widened tight→wide
    re-prices off the wide-spread rule.

    The new limit comes straight from the regenerated strategy; the rationale is
    the chosen rule's reasoning, prefixed with the stall context.  This replaces
    the old standalone ±5-tick clamp.

    Inputs beyond the quote are optional so the live-monitor call site
    ``recommend_requote(stall, side, quote)`` (which only has a top-of-book
    quote) keeps working: a one-level book is synthesized from the quote and a
    default :class:`DecisionContext` (no ADV/VWAP) drives a purely
    spread-and-price-action decision.  Callers that have richer context (a real
    L2 book, calibrated spread thresholds, VWAP) may pass ``ctx``/``l2`` to
    sharpen the decision.
    """
    # Lazy imports keep this module import-light and avoid any import cycle
    # between the generators and the stall engine.
    from engine.strategy_buy import generate_buy_strategy
    from engine.strategy_sell import generate_sell_strategy

    ctx = ctx or DecisionContext()
    book = l2 or _book_from_quote(quote)
    qty = max(stall.remaining_qty, 0.0)

    if side == "sell":
        record = SellRecord(
            account="",
            strategy="",
            ticker=quote.symbol,
            shares=qty,
            limit_price=stall.original_limit,
            est_proceeds=qty * stall.original_limit,
        )
        strategy, _chunks = generate_sell_strategy(
            record, quote, book, vol5min, ctx=ctx, today=today,
        )
    else:
        # Budget the regenerated buy off the remaining shares × current ask so
        # the chunker can actually size the clip; pricing still comes from the
        # rule, not the budget.
        ref_px = quote.ask or quote.last or quote.bid or stall.original_limit
        record = BuyAllocationRecord(
            account="",
            strategy="",
            ticker=quote.symbol,
            dollar_target=qty * ref_px,
            limit_price=stall.original_limit,
            share_target=int(qty),
            est_cost=qty * ref_px,
        )
        strategy, _chunks = generate_buy_strategy(
            record, quote, book, vol5min, ctx=ctx, today=today,
        )

    rationale = [
        f"Stall re-quote ({side}): re-ran rule selection on the fresh quote.",
        f"Original limit: ${stall.original_limit:.4f}",
        f"Current bid: ${quote.bid:.4f}  ask: ${quote.ask:.4f}",
        f"Selected rule: {strategy.rule} (urgency={strategy.urgency}).",
        f"New limit: ${strategy.limit_price:.4f}",
        *strategy.reasoning,
    ]

    return RequoteSuggestion(
        chunk_id=stall.chunk_id,
        new_limit=strategy.limit_price,
        remaining_qty=stall.remaining_qty,
        rationale=rationale,
    )
