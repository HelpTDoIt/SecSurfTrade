"""
Tests for ATP adapters — run entirely against MockATP (no live ATP required).
Covers: quote round-trip, L2 with 5 levels per side, all five order statuses,
stalled partial-fill scenario, parse helpers, and Protocol conformance checks.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from adapters import (
    Level,
    Level2Adapter,
    Level2Snapshot,
    OrderRow,
    OrderStatus,
    OrdersAdapter,
    QuoteAdapter,
    QuoteSnapshot,
)
from adapters._atp_parse import parse_price, parse_size, parse_volume
from adapters.mock_atp import MockATP


# ── parse helper tests ─────────────────────────────────────────────────────

class TestParsePrice:
    def test_plain_float(self):
        assert parse_price("62.71") == pytest.approx(62.71)

    def test_comma_separated(self):
        assert parse_price("1,234.56") == pytest.approx(1234.56)

    def test_dollar_prefix(self):
        assert parse_price("$568.42") == pytest.approx(568.42)

    def test_empty(self):
        assert parse_price("") == 0.0

    def test_non_numeric(self):
        assert parse_price("N/A") == 0.0

    def test_plus_sign(self):
        assert parse_price("+0.50") == pytest.approx(0.50)


class TestParseSize:
    def test_plain_int(self):
        assert parse_size("1200") == 1200

    def test_comma_separated(self):
        assert parse_size("1,200") == 1200

    def test_suffix_k(self):
        assert parse_size("1.2K") == 1200

    def test_suffix_m(self):
        assert parse_size("1.2M") == 1_200_000

    def test_suffix_b(self):
        assert parse_size("2B") == 2_000_000_000

    def test_empty(self):
        assert parse_size("") == 0

    def test_non_numeric(self):
        assert parse_size("--") == 0

    def test_volume_alias(self):
        assert parse_volume("12.4M") == 12_400_000


# ── MockATP — quote round-trip ─────────────────────────────────────────────

class TestMockATPQuote:
    def setup_method(self):
        self.mock = MockATP()
        self.mock.set_quote(
            "EEM",
            bid=62.39, ask=62.41, last=62.40,
            prev_close=62.71,
            bid_size=500, ask_size=300,
            volume=1_200_000,
        )

    def test_get_quote_returns_snapshot(self):
        snap = self.mock.get_quote("EEM")
        assert isinstance(snap, QuoteSnapshot)

    def test_symbol_normalized_uppercase(self):
        snap = self.mock.get_quote("eem")
        assert snap.symbol == "EEM"

    def test_bid_ask_last(self):
        snap = self.mock.get_quote("EEM")
        assert snap.bid  == pytest.approx(62.39)
        assert snap.ask  == pytest.approx(62.41)
        assert snap.last == pytest.approx(62.40)

    def test_prev_close(self):
        snap = self.mock.get_quote("EEM")
        assert snap.prev_close == pytest.approx(62.71)

    def test_sizes(self):
        snap = self.mock.get_quote("EEM")
        assert snap.bid_size == 500
        assert snap.ask_size == 300

    def test_volume(self):
        snap = self.mock.get_quote("EEM")
        assert snap.volume == 1_200_000

    def test_timestamp_is_set(self):
        snap = self.mock.get_quote("EEM")
        assert isinstance(snap.ts, datetime)
        assert snap.ts.tzinfo is not None

    def test_unknown_symbol_raises(self):
        with pytest.raises(KeyError):
            self.mock.get_quote("UNKNOWN")

    def test_returns_copy_not_same_object(self):
        s1 = self.mock.get_quote("EEM")
        s2 = self.mock.get_quote("EEM")
        assert s1 is not s2

    def test_quote_hook_called(self):
        calls = []
        self.mock.set_quote_hook(lambda sym: calls.append(sym))
        self.mock.get_quote("EEM")
        assert calls == ["EEM"]


# ── MockATP — Level 2 ──────────────────────────────────────────────────────

_SPY_BIDS = [
    (568.42, 1200, "NSDQ"),
    (568.41,  500, "ARCA"),
    (568.40, 2000, "EDGX"),
    (568.39,  800, "BATS"),
    (568.38,  400, "MEMX"),
]
_SPY_ASKS = [
    (568.45,  800, "ARCA"),
    (568.46, 1500, "NSDQ"),
    (568.47,  300, "BATS"),
    (568.48,  700, "EDGX"),
    (568.49,  200, "IEXG"),
]


class TestMockATPLevel2:
    def setup_method(self):
        self.mock = MockATP()
        self.mock.set_level2("SPY", bids=_SPY_BIDS, asks=_SPY_ASKS)

    def test_returns_l2_snapshot(self):
        snap = self.mock.get_level2("SPY")
        assert isinstance(snap, Level2Snapshot)

    def test_symbol_normalized(self):
        snap = self.mock.get_level2("spy")
        assert snap.symbol == "SPY"

    def test_five_bid_levels(self):
        snap = self.mock.get_level2("SPY")
        assert len(snap.bids) == 5

    def test_five_ask_levels(self):
        snap = self.mock.get_level2("SPY")
        assert len(snap.asks) == 5

    def test_bids_sorted_descending(self):
        snap = self.mock.get_level2("SPY")
        prices = [l.price for l in snap.bids]
        assert prices == sorted(prices, reverse=True)

    def test_asks_sorted_ascending(self):
        snap = self.mock.get_level2("SPY")
        prices = [l.price for l in snap.asks]
        assert prices == sorted(prices)

    def test_best_bid(self):
        snap = self.mock.get_level2("SPY")
        assert snap.bids[0].price == pytest.approx(568.42)
        assert snap.bids[0].size  == 1200
        assert snap.bids[0].mpid  == "NSDQ"

    def test_best_ask(self):
        snap = self.mock.get_level2("SPY")
        assert snap.asks[0].price == pytest.approx(568.45)
        assert snap.asks[0].size  == 800
        assert snap.asks[0].mpid  == "ARCA"

    def test_level_fields(self):
        snap = self.mock.get_level2("SPY")
        for lvl in snap.bids + snap.asks:
            assert isinstance(lvl, Level)
            assert lvl.price > 0
            assert lvl.size >= 0
            assert isinstance(lvl.mpid, str)

    def test_unknown_symbol_raises(self):
        with pytest.raises(KeyError):
            self.mock.get_level2("UNKNOWN")

    def test_thin_etf_single_level(self):
        self.mock.set_level2("JMAC", bids=[(25.10, 100, "ARCA")], asks=[(25.15, 50, "NSDQ")])
        snap = self.mock.get_level2("JMAC")
        assert len(snap.bids) == 1
        assert len(snap.asks) == 1


# ── MockATP — Orders panel ─────────────────────────────────────────────────

def _make_order(
    order_id: str,
    symbol: str = "EEM",
    side: str = "SELL",
    qty: float = 200.0,
    filled_qty: float = 0.0,
    status: OrderStatus = OrderStatus.Open,
    minutes_ago: int = 5,
) -> OrderRow:
    now = datetime.now(tz=timezone.utc)
    placed = now - timedelta(minutes=minutes_ago)
    return OrderRow(
        account="Roth IRA",
        symbol=symbol,
        side=side,
        qty=qty,
        filled_qty=filled_qty,
        limit_price=62.71,
        status=status,
        placed_at=placed,
        last_update_at=placed,
        order_id=order_id,
    )


class TestMockATPOrders:
    def setup_method(self):
        self.mock = MockATP()

    def test_empty_orders(self):
        assert self.mock.get_orders() == []

    def test_single_open_order(self):
        self.mock.add_order(_make_order("o1", status=OrderStatus.Open))
        rows = self.mock.get_orders()
        assert len(rows) == 1
        assert rows[0].status == OrderStatus.Open

    def test_all_five_statuses(self):
        for i, status in enumerate(OrderStatus):
            self.mock.add_order(_make_order(f"o{i}", status=status))
        rows = self.mock.get_orders()
        statuses = {r.status for r in rows}
        assert statuses == set(OrderStatus)

    def test_partial_fill_status(self):
        self.mock.add_order(_make_order("o1", qty=200, filled_qty=100, status=OrderStatus.PartiallyFilled))
        rows = self.mock.get_orders()
        assert rows[0].status == OrderStatus.PartiallyFilled
        assert rows[0].filled_qty == pytest.approx(100)

    def test_returns_copy(self):
        self.mock.add_order(_make_order("o1"))
        r1 = self.mock.get_orders()
        r2 = self.mock.get_orders()
        assert r1 is not r2
        assert r1[0] is not r2[0]

    def test_clear_orders(self):
        self.mock.add_order(_make_order("o1"))
        self.mock.clear_orders()
        assert self.mock.get_orders() == []

    def test_set_order_status(self):
        self.mock.add_order(_make_order("o1", status=OrderStatus.Open))
        self.mock.set_order_status("o1", OrderStatus.Filled)
        rows = self.mock.get_orders()
        assert rows[0].status == OrderStatus.Filled

    def test_set_order_status_unknown_raises(self):
        with pytest.raises(KeyError):
            self.mock.set_order_status("nope", OrderStatus.Filled)

    def test_orders_hook_called(self):
        calls = []
        self.mock.set_orders_hook(lambda: calls.append(1))
        self.mock.add_order(_make_order("o1"))
        self.mock.get_orders()
        assert calls == [1]


# ── Stalled partial-fill scenario ─────────────────────────────────────────

class TestStallDetection:
    """
    Verify the mock can represent the stall scenario the monitor loop checks:
    a PartiallyFilled order whose last_update_at is older than the threshold.
    """

    def test_simulate_partial_fill_sets_status(self):
        mock = MockATP()
        mock.add_order(_make_order("o1", qty=200, status=OrderStatus.Open))
        mock.simulate_partial_fill("o1", filled_qty=100)
        rows = mock.get_orders()
        assert rows[0].status == OrderStatus.PartiallyFilled
        assert rows[0].filled_qty == pytest.approx(100)

    def test_simulate_full_fill_sets_filled(self):
        mock = MockATP()
        mock.add_order(_make_order("o1", qty=200, status=OrderStatus.Open))
        mock.simulate_partial_fill("o1", filled_qty=200)
        rows = mock.get_orders()
        assert rows[0].status == OrderStatus.Filled

    def test_stale_last_update_at(self):
        mock = MockATP()
        stale_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=400)
        mock.add_order(_make_order("o1", qty=200, status=OrderStatus.Open))
        mock.simulate_partial_fill("o1", filled_qty=100, ts=stale_ts)
        rows = mock.get_orders()
        row = rows[0]
        # Stall threshold is 300s; 400s gap should register as stalled
        age = (datetime.now(tz=timezone.utc) - row.last_update_at).total_seconds()
        assert age >= 300, f"Expected age >= 300s, got {age:.0f}s"
        assert row.status == OrderStatus.PartiallyFilled

    def test_fresh_partial_fill_not_stalled(self):
        mock = MockATP()
        mock.add_order(_make_order("o1", qty=200, status=OrderStatus.Open))
        mock.simulate_partial_fill("o1", filled_qty=100)
        rows = mock.get_orders()
        age = (datetime.now(tz=timezone.utc) - rows[0].last_update_at).total_seconds()
        assert age < 5


# ── Protocol conformance ───────────────────────────────────────────────────

class TestProtocolConformance:
    """MockATP must satisfy all three runtime-checkable Protocols."""

    def test_mock_is_quote_adapter(self):
        mock = MockATP()
        assert isinstance(mock, QuoteAdapter)

    def test_mock_is_level2_adapter(self):
        mock = MockATP()
        assert isinstance(mock, Level2Adapter)

    def test_mock_is_orders_adapter(self):
        mock = MockATP()
        assert isinstance(mock, OrdersAdapter)


# ── Engine isolation check ─────────────────────────────────────────────────

def test_engine_does_not_import_pywinauto():
    """
    Importing engine.calculator must NOT load pywinauto into sys.modules.
    This ensures the engine stays pure and portable.
    """
    import sys
    # Reload to ensure clean state
    import importlib
    import engine.calculator  # noqa: F401
    importlib.reload(engine.calculator)
    assert "pywinauto" not in sys.modules, (
        "engine.calculator imported pywinauto — keep ATP imports inside adapters/atp_*.py only"
    )
