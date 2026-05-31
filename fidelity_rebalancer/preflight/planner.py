from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable


@dataclass(frozen=True)
class L2WindowPlan:
    watchlist: list[str]  # ALL needed tickers, normalized, deduped, sorted ascending
    l2_assigned: list[
        str
    ]  # thin tickers assigned to L2 windows; highest pct-of-ADV first; length <= cap
    l2_overflow: list[str]  # thin tickers beyond the cap; highest pct-of-ADV first
    cap: int


def _normalize(ticker: str) -> str:
    return ticker.strip().upper()


def plan_l2_windows(
    needed: Iterable[str],
    thin: Iterable[tuple[str, float]],
    cap: int = 7,
) -> L2WindowPlan:
    # Normalize + dedupe thin, keeping the HIGHEST pct per ticker.
    best_pct: dict[str, float] = {}
    for raw_ticker, pct in thin:
        ticker = _normalize(raw_ticker)
        if ticker not in best_pct or pct > best_pct[ticker]:
            best_pct[ticker] = pct

    # Sort by pct DESCENDING, tie-break by ticker ASCENDING.
    ranked = sorted(best_pct.items(), key=lambda kv: (-kv[1], kv[0]))
    ranked_tickers = [ticker for ticker, _ in ranked]

    # watchlist = sorted-ascending, deduped union of needed and thin tickers.
    needed_norm = {_normalize(t) for t in needed}
    watchlist = sorted(needed_norm | set(best_pct.keys()))

    # Assignment vs overflow.
    if cap <= 0:
        l2_assigned: list[str] = []
        l2_overflow = list(ranked_tickers)
    else:
        l2_assigned = ranked_tickers[:cap]
        l2_overflow = ranked_tickers[cap:]

    return L2WindowPlan(
        watchlist=watchlist,
        l2_assigned=l2_assigned,
        l2_overflow=l2_overflow,
        cap=cap,
    )
