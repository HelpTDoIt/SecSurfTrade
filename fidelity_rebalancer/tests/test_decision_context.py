"""
Tests for engine.decision_context.DecisionContext.

Verifies:
  1. The dataclass can be constructed and is frozen (immutable).
  2. sigma_bps is an optional carried field that defaults to None and does
     NOT affect strategy outputs.
  3. Passing ctx= to generate_sell_strategy produces bit-identical results
     to the equivalent legacy flat-kwargs call (pinned expected values).
  4. Passing ctx= to generate_buy_strategy produces bit-identical results
     to the equivalent legacy flat-kwargs call (pinned expected values).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from adapters import Level, Level2Snapshot, QuoteSnapshot
from engine.decision_context import DecisionContext
from engine.spread_context import SpreadContext
from engine.strategy_buy import generate_buy_strategy
from engine.strategy_sell import generate_sell_strategy
from state.schema import BuyAllocationRecord, SellRecord


# ── Fixtures ──────────────────────────────────────────────────────────────

def _quote(
    symbol: str,
    *,
    bid: float,
    ask: float,
    last: float,
    prev_close: float,
    volume: int = 1_000_000,
) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=symbol,
        bid=bid,
        bid_size=500,
        ask=ask,
        ask_size=500,
        last=last,
        prev_close=prev_close,
        volume=volume,
        ts=datetime.now(tz=timezone.utc),
    )


def _l2(
    symbol: str,
    bids: list[tuple[float, int]],
    asks: list[tuple[float, int]],
) -> Level2Snapshot:
    return Level2Snapshot(
        symbol=symbol,
        bids=[Level(price=p, size=s, mpid="ARCX") for p, s in bids],
        asks=[Level(price=p, size=s, mpid="ARCX") for p, s in asks],
        ts=datetime.now(tz=timezone.utc),
    )


# ── Unit tests for DecisionContext itself ─────────────────────────────────

def test_decision_context_construction():
    """DecisionContext can be constructed with the four required fields."""
    sc = SpreadContext.default()
    ctx = DecisionContext(
        market_minutes=45,
        spread_ctx=sc,
        vwap=500.25,
        adv=1_000_000.0,
    )
    assert ctx.market_minutes == 45
    assert ctx.spread_ctx is sc
    assert ctx.vwap == pytest.approx(500.25)
    assert ctx.adv == pytest.approx(1_000_000.0)
    assert ctx.sigma_bps is None  # default


def test_decision_context_sigma_bps_carried():
    """sigma_bps can be set and retrieved; it defaults to None."""
    sc = SpreadContext.default()
    ctx_no_sigma = DecisionContext(market_minutes=None, spread_ctx=sc, vwap=None, adv=None)
    ctx_with_sigma = DecisionContext(market_minutes=None, spread_ctx=sc, vwap=None, adv=None, sigma_bps=42.5)
    assert ctx_no_sigma.sigma_bps is None
    assert ctx_with_sigma.sigma_bps == pytest.approx(42.5)


def test_decision_context_is_frozen():
    """DecisionContext is immutable — attribute assignment must raise TypeError."""
    sc = SpreadContext.default()
    ctx = DecisionContext(market_minutes=None, spread_ctx=sc, vwap=None, adv=None)
    with pytest.raises((AttributeError, TypeError)):
        ctx.market_minutes = 10  # type: ignore[misc]


def test_decision_context_none_fields_accepted():
    """All four mutable fields accept None (common when data is unavailable)."""
    ctx = DecisionContext(
        market_minutes=None,
        spread_ctx=SpreadContext.default(),
        vwap=None,
        adv=None,
    )
    assert ctx.market_minutes is None
    assert ctx.vwap is None
    assert ctx.adv is None


# ── Pinned sell strategy via ctx= ────────────────────────────────────────
#
# These expected values were captured from the generator BEFORE the refactor
# by running the identical inputs through the legacy kwargs API.  If any
# value below changes, the refactor has introduced a behavior change — fix
# the refactor, not the test.

def test_sell_via_ctx_matches_legacy():
    """
    Sell: tight spread + small position, SPY.
    Pinned: rule='tight_spread_small_position', urgency='normal',
            limit=$500.00, 1 chunk.
    """
    sell = SellRecord(
        account="Test *0000",
        strategy="Strategy Gamma",
        ticker="SPY",
        shares=1000,
        limit_price=500.0,
        est_proceeds=500_000,
    )
    quote = _quote(
        "SPY",
        bid=499.99,
        ask=500.01,
        last=500.00,
        prev_close=499.50,
        volume=150_000_000,
    )
    book = _l2("SPY", [(499.99, 5000)] * 3, [(500.01, 5000)] * 3)

    sc = SpreadContext.from_bid_ask(499.99, 500.01)
    ctx = DecisionContext(
        market_minutes=None,
        spread_ctx=sc,
        vwap=None,
        adv=100_000_000,
    )
    strat, chunks = generate_sell_strategy(
        sell,
        quote,
        book,
        vol5min=500_000.0,
        today=date(2026, 4, 15),
        ctx=ctx,
    )

    # Pinned expected values
    assert strat.rule == "tight_spread_small_position"
    assert strat.urgency == "normal"
    assert strat.limit_price == pytest.approx(500.00)
    assert len(chunks) == 1


def test_sell_via_ctx_sigma_bps_does_not_change_output():
    """sigma_bps on the ctx does NOT change strategy rule, limit, or urgency."""
    sell = SellRecord(
        account="Test *0000",
        strategy="Strategy Gamma",
        ticker="SPY",
        shares=1000,
        limit_price=500.0,
        est_proceeds=500_000,
    )
    quote = _quote("SPY", bid=499.99, ask=500.01, last=500.00, prev_close=499.50, volume=150_000_000)
    book = _l2("SPY", [(499.99, 5000)] * 3, [(500.01, 5000)] * 3)

    sc = SpreadContext.from_bid_ask(499.99, 500.01)
    ctx_no_sigma = DecisionContext(market_minutes=None, spread_ctx=sc, vwap=None, adv=100_000_000)
    ctx_with_sigma = DecisionContext(market_minutes=None, spread_ctx=sc, vwap=None, adv=100_000_000, sigma_bps=50.0)

    strat1, chunks1 = generate_sell_strategy(sell, quote, book, vol5min=500_000.0, today=date(2026, 4, 15), ctx=ctx_no_sigma)
    strat2, chunks2 = generate_sell_strategy(sell, quote, book, vol5min=500_000.0, today=date(2026, 4, 15), ctx=ctx_with_sigma)

    assert strat1.rule == strat2.rule
    assert strat1.urgency == strat2.urgency
    assert strat1.limit_price == pytest.approx(strat2.limit_price)
    assert len(chunks1) == len(chunks2)


# ── Pinned buy strategy via ctx= ────────────────────────────────────────
#
# Expected values are pinned from the pre-refactor legacy kwargs run.

def test_buy_via_ctx_matches_legacy():
    """
    Buy: tight spread + healthy volume, SPY.
    Pinned: rule='tight_spread_good_volume', urgency='normal',
            limit=$500.01, 1 chunk.
    """
    buy = BuyAllocationRecord(
        account="Test *0000",
        strategy="Strategy Gamma",
        ticker="SPY",
        dollar_target=50_000,
        limit_price=500.0,
        share_target=100,
        est_cost=50_000,
    )
    quote = _quote(
        "SPY",
        bid=499.99,
        ask=500.01,
        last=500.00,
        prev_close=500.00,
        volume=150_000_000,
    )
    book = _l2("SPY", [(499.99, 5000)] * 3, [(500.01, 5000)] * 3)

    sc = SpreadContext.from_bid_ask(499.99, 500.01)
    ctx = DecisionContext(
        market_minutes=None,
        spread_ctx=sc,
        vwap=None,
        adv=100_000_000,
    )
    strat, chunks = generate_buy_strategy(
        buy,
        quote,
        book,
        vol5min=500_000.0,
        ctx=ctx,
    )

    # Pinned expected values
    assert strat.rule == "tight_spread_good_volume"
    assert strat.urgency == "normal"
    assert strat.limit_price == pytest.approx(500.01)
    assert len(chunks) == 1
    total_cost = sum(c.cost for c in chunks)
    assert total_cost <= 50_000.0 + 1e-6
