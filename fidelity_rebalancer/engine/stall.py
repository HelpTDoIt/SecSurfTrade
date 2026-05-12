"""
Stall detection and re-quote suggestion engine.

Pure functions — no I/O.

detect_stalls(orders, threshold_seconds, now) -> list[StallEvent]
recommend_requote(stall, side, quote)         -> RequoteSuggestion
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from adapters import OrderRow, OrderStatus, QuoteSnapshot
from engine.chunker import tick


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


def recommend_requote(
    stall: StallEvent,
    side: Literal["buy", "sell"],
    quote: QuoteSnapshot,
) -> RequoteSuggestion:
    """
    Propose a new limit price for a stalled clip.

    Sell side:
        new_limit = max(quote.bid + tick(quote.bid), original_limit − 5×tick)
        (nudge toward the new bid, but don't chase more than 5 ticks)

    Buy side:
        new_limit = min(quote.ask − tick(quote.ask), original_limit + 5×tick)
        (nudge toward the new ask, but don't chase more than 5 ticks)
    """
    orig = stall.original_limit
    rationale: list[str] = []

    if side == "sell":
        bid = quote.bid
        t = tick(bid)
        candidate_bid_plus = bid + t
        candidate_chase = orig - 5 * t
        new_limit = round(max(candidate_bid_plus, candidate_chase) / t) * t
        rationale = [
            f"Original limit: ${orig:.4f}",
            f"Current bid: ${bid:.4f}  ask: ${quote.ask:.4f}",
            f"Candidate bid+1tick = ${candidate_bid_plus:.4f}",
            f"Candidate orig−5ticks = ${candidate_chase:.4f}",
            f"New limit (max of above): ${new_limit:.4f}",
        ]
    else:
        ask = quote.ask
        t = tick(ask)
        candidate_ask_minus = ask - t
        candidate_chase = orig + 5 * t
        new_limit = round(min(candidate_ask_minus, candidate_chase) / t) * t
        rationale = [
            f"Original limit: ${orig:.4f}",
            f"Current bid: ${quote.bid:.4f}  ask: ${ask:.4f}",
            f"Candidate ask−1tick = ${candidate_ask_minus:.4f}",
            f"Candidate orig+5ticks = ${candidate_chase:.4f}",
            f"New limit (min of above): ${new_limit:.4f}",
        ]

    return RequoteSuggestion(
        chunk_id=stall.chunk_id,
        new_limit=new_limit,
        remaining_qty=stall.remaining_qty,
        rationale=rationale,
    )
