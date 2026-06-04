"""
Per-symbol position-size calibration for strategy rule thresholds (G-5).

Instead of hardcoded %ADV cutoffs (sell 2.0/5.0, buy 3.0), the small/large
position thresholds are derived from each symbol's asset class so that a thinly
traded leveraged ETF flips to "large position" pricing at a much lower %ADV than
a deeply liquid large-cap.  This mirrors ``engine.spread_context.SpreadContext``
exactly: a frozen dataclass of thresholds plus per-class typical values keyed by
the shared ``_TICKER_CLASS`` buckets.

ADV definition (G-6)
--------------------
There is exactly ONE ADV definition consumed by both generators: the value
carried on ``DecisionContext.adv``.  The call site (``cli/strategy.py``) sources
it from the watchlist's **10-day average daily volume** (``WatchlistRow.avg_vol_10d``)
— the same single market-data fetch (yfinance or ATP OCR) that feeds every other
quote field.  ``engine.strategy_sell.get_adv`` (30-day yfinance) is retained only
as an in-generator *fallback* for the no-row case (``ctx.adv is None``); it is not
a competing primary source.  %ADV everywhere in the engine therefore means
"order shares / 10-day ADV × 100".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from engine.spread_context import _TICKER_CLASS

# Legacy hardcoded cutoffs, preserved as the default-class behavior so existing
# fixtures (synthetic tickers not in _TICKER_CLASS) keep their pinned outcomes.
_DEFAULT_SELL_SMALL = 2.0   # < this %ADV → "small position"
_DEFAULT_SELL_LARGE = 5.0   # > this %ADV → "large position"
_DEFAULT_BUY_LARGE = 3.0    # > this %ADV → "large position"


@dataclass(frozen=True)
class PositionSizeContext:
    """%ADV cutoffs calibrated to a specific symbol's asset class.

    Fields
    ------
    sell_small_pct : float
        Sell order below this %ADV counts as a *small* position (rule 1 path).
    sell_large_pct : float
        Sell order above this %ADV counts as a *large* position (rule 2 path).
    buy_large_pct : float
        Buy order above this %ADV counts as a *large* position (rule 3 path).
    """

    sell_small_pct: float
    sell_large_pct: float
    buy_large_pct: float

    @classmethod
    def default(cls) -> "PositionSizeContext":
        """Legacy behavior: sell small<2%, sell large>5%, buy large>3%."""
        return cls(
            sell_small_pct=_DEFAULT_SELL_SMALL,
            sell_large_pct=_DEFAULT_SELL_LARGE,
            buy_large_pct=_DEFAULT_BUY_LARGE,
        )


# Per-asset-class %ADV cutoffs.  Liquid classes tolerate a larger %ADV before a
# position is treated as "large"; thin/leveraged classes flip much sooner so the
# generator drips them in (sell→bid, buy→ask-1tick + smaller chunks).
# `default` mirrors the legacy 2.0/5.0/3.0 so unmapped tickers are unchanged.
_ASSET_CLASS_CUTOFFS: dict[str, PositionSizeContext] = {
    "large_cap":     PositionSizeContext(sell_small_pct=3.0, sell_large_pct=8.0, buy_large_pct=5.0),
    "sector":        PositionSizeContext(sell_small_pct=2.0, sell_large_pct=5.0, buy_large_pct=3.0),
    "international": PositionSizeContext(sell_small_pct=1.5, sell_large_pct=4.0, buy_large_pct=2.5),
    "leveraged":     PositionSizeContext(sell_small_pct=1.0, sell_large_pct=2.5, buy_large_pct=1.5),
    "fixed_income":  PositionSizeContext(sell_small_pct=2.0, sell_large_pct=5.0, buy_large_pct=3.0),
}


def size_context_for(symbol: str) -> PositionSizeContext:
    """Best-effort PositionSizeContext for a symbol from its asset class.

    Unknown symbols fall back to the legacy default (sell 2/5, buy 3), so
    behavior is unchanged for any ticker not in the shared class map.
    """
    cls = _TICKER_CLASS.get(symbol.upper())
    if cls is None:
        return PositionSizeContext.default()
    return _ASSET_CLASS_CUTOFFS.get(cls, PositionSizeContext.default())
