"""Tests for the I/O-free helpers in cli.preflight.

The interactive loop / subprocess parts are verified by hand (they need a live
FT+); these tests lock the pure data-derivation that feeds the L2 plan.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from adapters import WatchlistRow
from cli import preflight
from state.schema import BuyAllocationRecord, SellRecord


def _row(sym: str, adv: int) -> WatchlistRow:
    return WatchlistRow(
        symbol=sym,
        last=10.0,
        bid=9.99,
        ask=10.01,
        bid_size=100,
        ask_size=100,
        volume=adv,
        prev_close=10.0,
        avg_vol_10d=adv,
        avg_vol_90d=adv,
        div_ex_date="",
        div_local=0.0,
        vwap=10.0,
        ts=datetime.now(tz=timezone.utc),
    )


class _FakeState:
    """Minimal stand-in exposing only what the helpers read."""

    class _Computed:
        def __init__(self, sells, buys):
            self.sells = sells
            self.buy_allocations = buys

    def __init__(self, sells, buys):
        self.computed = self._Computed(sells, buys)


def _sell(ticker, shares):
    return SellRecord(
        account="A",
        strategy="S",
        ticker=ticker,
        shares=shares,
        limit_price=10.0,
        est_proceeds=shares * 10.0,
    )


def _buy(ticker, shares):
    return BuyAllocationRecord(
        account="A",
        strategy="S",
        ticker=ticker,
        dollar_target=shares * 10.0,
        limit_price=10.0,
        share_target=shares,
        est_cost=shares * 10.0,
    )


def test_needed_tickers_dedupes_and_sorts():
    state = _FakeState(
        sells=[_sell("SPY", 5), _sell("AAA", 5)],
        buys=[_buy("AAA", 3), _buy("BBB", 3)],
    )
    assert preflight._needed_tickers(state) == ["AAA", "BBB", "SPY"]


def test_thin_pairs_maps_triples_to_ticker_pct():
    # AAA order is 50% of ADV (thin); SPY is 0.005% (not thin).
    state = _FakeState(sells=[_sell("AAA", 500), _sell("SPY", 5)], buys=[])
    watchlist = {"AAA": _row("AAA", 1000), "SPY": _row("SPY", 100_000)}
    pairs = preflight._thin_pairs(state, watchlist)
    assert ("AAA", 50.0) in pairs
    assert all(sym != "SPY" for sym, _ in pairs)


def test_thin_pairs_empty_when_no_adv():
    state = _FakeState(sells=[_sell("AAA", 500)], buys=[])
    assert preflight._thin_pairs(state, {}) == []
