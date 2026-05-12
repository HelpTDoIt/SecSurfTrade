"""
Snapshot-style tests for engine.strategy_sell and engine.strategy_buy.

Each rule branch gets at least one synthetic quote + L2 fixture and we
assert the rule chosen, the limit price, urgency, and chunk count.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

import pytest

from adapters import Level, Level2Snapshot, QuoteSnapshot
from engine.strategy_buy import generate_buy_strategy
from engine.strategy_sell import generate_sell_strategy
from state.schema import BuyAllocationRecord, SellRecord


# ── Helpers ───────────────────────────────────────────────────────────────

def _quote(symbol: str, *, bid: float, ask: float, last: float,
           prev_close: float, volume: int = 1_000_000) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=symbol,
        bid=bid, bid_size=500,
        ask=ask, ask_size=500,
        last=last,
        prev_close=prev_close,
        volume=volume,
        ts=datetime.now(tz=timezone.utc),
    )


def _l2(symbol: str, bids: list[tuple[float, int]],
        asks: list[tuple[float, int]]) -> Level2Snapshot:
    return Level2Snapshot(
        symbol=symbol,
        bids=[Level(price=p, size=s, mpid="ARCX") for p, s in bids],
        asks=[Level(price=p, size=s, mpid="ARCX") for p, s in asks],
        ts=datetime.now(tz=timezone.utc),
    )


# ── Sell rules ────────────────────────────────────────────────────────────

def test_sell_tight_spread_small_position():
    """Spread 2bps, rel_vol 1.5, 1% ADV → MID, normal."""
    sell = SellRecord(
        account="Roth IRA", strategy="World Try -Top", ticker="SPY",
        shares=1000, limit_price=500.0, est_proceeds=500_000,
    )
    quote = _quote("SPY", bid=499.99, ask=500.01, last=500.00,
                   prev_close=499.50, volume=150_000_000)
    book = _l2("SPY", [(499.99, 5000)] * 3, [(500.01, 5000)] * 3)

    strat, chunks = generate_sell_strategy(
        sell, quote, book, vol5min=500_000.0,
        today=date(2026, 4, 15),
        adv=100_000_000,  # 1% ADV
    )
    assert strat.rule == "tight_spread_small_position"
    assert strat.urgency == "normal"
    assert strat.limit_price == pytest.approx(500.00)
    assert len(chunks) >= 1
    assert any(re.search(r"Spread is \d+\.\d bps", b) for b in strat.reasoning)


def test_sell_tight_spread_large_position():
    """Spread 2bps, 6% ADV → BID, patient."""
    sell = SellRecord(
        account="Roth IRA", strategy="SPDR Respectable", ticker="SPY",
        shares=6_000_000, limit_price=500.0, est_proceeds=3e9,
    )
    quote = _quote("SPY", bid=499.99, ask=500.01, last=500.00,
                   prev_close=500.00, volume=10_000_000)
    book = _l2("SPY", [(499.99, 5000)] * 3, [(500.01, 5000)] * 3)

    strat, _ = generate_sell_strategy(
        sell, quote, book, vol5min=500_000.0,
        today=date(2026, 4, 15),
        adv=100_000_000,  # 6% ADV
    )
    assert strat.rule == "tight_spread_large_position"
    assert strat.urgency == "patient"
    assert strat.limit_price == pytest.approx(499.99)


def test_sell_wide_spread():
    """Spread 20bps → BID + 1 tick, patient."""
    sell = SellRecord(
        account="Roth IRA", strategy="Prismatic Prudence", ticker="ABC",
        shares=200, limit_price=100.0, est_proceeds=20_000,
    )
    quote = _quote("ABC", bid=99.90, ask=100.10, last=100.00,
                   prev_close=100.00, volume=50_000)
    book = _l2("ABC", [(99.90, 200)] * 3, [(100.10, 200)] * 3)

    strat, _ = generate_sell_strategy(
        sell, quote, book, vol5min=10_000.0,
        today=date(2026, 4, 15),
        adv=100_000,
    )
    assert strat.rule == "wide_spread"
    assert strat.urgency == "patient"
    assert strat.limit_price == pytest.approx(99.91)


def test_sell_down_day():
    """Spread 7bps (neither tight<5 nor wide>10), last −3% from prev close → 0.99 × prev."""
    sell = SellRecord(
        account="Roth IRA", strategy="Future Theme + CAPE", ticker="DEF",
        shares=200, limit_price=100.0, est_proceeds=20_000,
    )
    # Spread = 0.07, midpoint=100.005 → bps≈7.0
    quote = _quote("DEF", bid=99.97, ask=100.04, last=97.00,
                   prev_close=100.00, volume=200_000)
    book = _l2("DEF", [(99.97, 500)] * 3, [(100.04, 500)] * 3)

    strat, _ = generate_sell_strategy(
        sell, quote, book, vol5min=20_000.0,
        today=date(2026, 4, 15),
        adv=1_000_000,
    )
    assert strat.rule == "down_day"
    assert strat.urgency == "patient"
    assert strat.limit_price == pytest.approx(99.00)


def test_sell_up_day():
    """Spread 7bps, last +3% → BID, aggressive."""
    sell = SellRecord(
        account="Roth IRA", strategy="World Try -Top", ticker="GHI",
        shares=200, limit_price=100.0, est_proceeds=20_000,
    )
    quote = _quote("GHI", bid=102.97, ask=103.04, last=103.00,
                   prev_close=100.00, volume=200_000)
    book = _l2("GHI", [(102.97, 500)] * 3, [(103.04, 500)] * 3)

    strat, _ = generate_sell_strategy(
        sell, quote, book, vol5min=20_000.0,
        today=date(2026, 4, 15),
        adv=1_000_000,
    )
    assert strat.rule == "up_day"
    assert strat.urgency == "aggressive"
    assert strat.limit_price == pytest.approx(102.97)


def test_sell_default_midpoint():
    """No conditions met (spread ~7bps, day flat, mid-size) → midpoint, normal."""
    sell = SellRecord(
        account="Roth IRA", strategy="World Try -Top", ticker="JKL",
        shares=200, limit_price=100.0, est_proceeds=20_000,
    )
    quote = _quote("JKL", bid=99.965, ask=100.035, last=100.00,
                   prev_close=100.00, volume=200_000)
    book = _l2("JKL", [(99.96, 500)] * 3, [(100.03, 500)] * 3)

    strat, _ = generate_sell_strategy(
        sell, quote, book, vol5min=20_000.0,
        today=date(2026, 4, 15),
        adv=1_000_000,
    )
    assert strat.rule == "default"
    assert strat.urgency == "normal"


# ── Buy rules ─────────────────────────────────────────────────────────────

def test_buy_tight_spread_good_volume():
    """Spread 2bps, rel_vol 1.5 → ASK, normal."""
    buy = BuyAllocationRecord(
        account="Roth IRA", strategy="World Try -Top", ticker="SPY",
        dollar_target=50_000, limit_price=500.0,
        share_target=100, est_cost=50_000,
    )
    quote = _quote("SPY", bid=499.99, ask=500.01, last=500.00,
                   prev_close=500.00, volume=150_000_000)
    book = _l2("SPY", [(499.99, 5000)] * 3, [(500.01, 5000)] * 3)

    strat, chunks = generate_buy_strategy(
        buy, quote, book, vol5min=500_000.0,
        adv=100_000_000,
    )
    assert strat.rule == "tight_spread_good_volume"
    assert strat.urgency == "normal"
    assert strat.limit_price == pytest.approx(500.01)
    total_cost = sum(c.cost for c in chunks)
    assert total_cost <= 50_000.0 + 1e-6


def test_buy_wide_spread():
    """Spread 20bps → MID, patient."""
    buy = BuyAllocationRecord(
        account="Roth IRA", strategy="SPDR Respectable", ticker="MNO",
        dollar_target=10_000, limit_price=100.0,
        share_target=100, est_cost=10_000,
    )
    quote = _quote("MNO", bid=99.90, ask=100.10, last=100.00,
                   prev_close=100.00, volume=50_000)
    book = _l2("MNO", [(99.90, 200)] * 3, [(100.10, 200)] * 3)

    strat, _ = generate_buy_strategy(
        buy, quote, book, vol5min=10_000.0,
        adv=1_000_000,
    )
    assert strat.rule == "wide_spread"
    assert strat.urgency == "patient"
    assert strat.limit_price == pytest.approx(100.00)


def test_buy_large_position():
    """Spread 7bps (not tight, not wide), 4% ADV → ASK − 1 tick, patient, smaller chunks."""
    buy = BuyAllocationRecord(
        account="Roth IRA", strategy="Future Theme + CAPE", ticker="PQR",
        dollar_target=400_000, limit_price=100.0,
        share_target=4000, est_cost=400_000,
    )
    quote = _quote("PQR", bid=99.965, ask=100.035, last=100.00,
                   prev_close=100.00, volume=200_000)
    book = _l2("PQR", [(99.96, 500)] * 3, [(100.03, 500)] * 3)

    strat, chunks = generate_buy_strategy(
        buy, quote, book, vol5min=20_000.0,
        adv=100_000,  # 4% ADV
    )
    assert strat.rule == "large_position"
    assert strat.urgency == "patient"
    assert strat.limit_price == pytest.approx(100.02)  # 100.03 - 0.01
    # depth_pct halved → smaller per-chunk caps; expect more chunks
    assert len(chunks) >= 2


def test_buy_default():
    """Spread 7bps, no big position, no good volume → default ask, normal."""
    buy = BuyAllocationRecord(
        account="Roth IRA", strategy="World Try -Top", ticker="STU",
        dollar_target=10_000, limit_price=100.0,
        share_target=100, est_cost=10_000,
    )
    quote = _quote("STU", bid=99.965, ask=100.035, last=100.00,
                   prev_close=100.00, volume=200_000)
    book = _l2("STU", [(99.96, 500)] * 3, [(100.03, 500)] * 3)

    strat, _ = generate_buy_strategy(
        buy, quote, book, vol5min=20_000.0,
        adv=10_000_000,  # rel_vol 0.02 (low), pct_of_adv=0.001%
    )
    assert strat.rule == "default"
    assert strat.urgency == "normal"


# ── Round-trip serialization (acceptance criterion #6) ────────────────────

def test_strategy_round_trip_serializes_byte_identical():
    sell = SellRecord(
        account="Roth IRA", strategy="World Try -Top", ticker="SPY",
        shares=1000, limit_price=500.0, est_proceeds=500_000,
    )
    quote = _quote("SPY", bid=499.99, ask=500.01, last=500.00,
                   prev_close=499.50, volume=150_000_000)
    book = _l2("SPY", [(499.99, 5000)] * 3, [(500.01, 5000)] * 3)

    strat, _ = generate_sell_strategy(
        sell, quote, book, vol5min=500_000.0,
        today=date(2026, 4, 15), adv=100_000_000,
    )
    json1 = strat.model_dump_json()
    strat2 = type(strat).model_validate_json(json1)
    json2 = strat2.model_dump_json()
    assert json1 == json2
