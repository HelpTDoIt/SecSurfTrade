"""
Tests for engine.stall — stall detection, re-quote math, and end-to-end mock flow.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from adapters import OrderRow, OrderStatus, QuoteSnapshot
from adapters.mock_atp import MockATP
from engine.stall import StallEvent, detect_stalls, recommend_requote


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _order(
    order_id: str,
    qty: float = 100.0,
    filled_qty: float = 0.0,
    limit_price: float = 100.0,
    status: OrderStatus = OrderStatus.Open,
    last_update_at: datetime | None = None,
    side: str = "SELL",
) -> OrderRow:
    return OrderRow(
        account="Roth IRA",
        symbol="EEM",
        side=side,
        qty=qty,
        filled_qty=filled_qty,
        limit_price=limit_price,
        status=status,
        placed_at=_now(),
        last_update_at=last_update_at or _now(),
        order_id=order_id,
    )


def _quote(bid: float, ask: float, last: float = 0.0) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol="EEM",
        bid=bid, bid_size=500,
        ask=ask, ask_size=500,
        last=last or bid,
        prev_close=0.0,
        volume=1_000_000,
        ts=_now(),
    )


# ── detect_stalls ────────────────────────────────────────────────────────

def test_no_stall_when_open():
    orders = [_order("s1", status=OrderStatus.Open)]
    now = _now()
    assert detect_stalls(orders, threshold_seconds=300, now=now) == []


def test_no_stall_when_filled():
    orders = [_order("s1", status=OrderStatus.Filled, filled_qty=100.0)]
    now = _now()
    assert detect_stalls(orders, threshold_seconds=300, now=now) == []


def test_no_stall_below_threshold():
    stale_time = _now() - timedelta(seconds=200)
    orders = [_order("s1", qty=100, filled_qty=50, status=OrderStatus.PartiallyFilled,
                     last_update_at=stale_time)]
    now = _now()
    assert detect_stalls(orders, threshold_seconds=300, now=now) == []


def test_stall_at_exact_threshold():
    stale_time = _now() - timedelta(seconds=300)
    orders = [_order("s1", qty=100, filled_qty=50, status=OrderStatus.PartiallyFilled,
                     last_update_at=stale_time)]
    now = _now()
    stalls = detect_stalls(orders, threshold_seconds=300, now=now)
    assert len(stalls) == 1
    assert stalls[0].chunk_id == "s1"
    assert stalls[0].remaining_qty == pytest.approx(50.0)
    assert stalls[0].filled_qty == pytest.approx(50.0)
    assert stalls[0].seconds_stalled >= 300.0


def test_stall_beyond_threshold():
    stale_time = _now() - timedelta(seconds=600)
    orders = [_order("s1", qty=1600, filled_qty=800, status=OrderStatus.PartiallyFilled,
                     last_update_at=stale_time)]
    now = _now()
    stalls = detect_stalls(orders, threshold_seconds=300, now=now)
    assert len(stalls) == 1
    assert stalls[0].remaining_qty == pytest.approx(800.0)
    assert stalls[0].seconds_stalled >= 600.0


def test_multiple_orders_only_stalled_flagged():
    t_stale = _now() - timedelta(seconds=400)
    orders = [
        _order("s1", qty=100, filled_qty=50, status=OrderStatus.PartiallyFilled,
               last_update_at=t_stale),
        _order("s2", qty=100, filled_qty=0, status=OrderStatus.Open),
        _order("s3", qty=100, filled_qty=100, status=OrderStatus.Filled),
    ]
    stalls = detect_stalls(orders, threshold_seconds=300, now=_now())
    assert len(stalls) == 1
    assert stalls[0].chunk_id == "s1"


# ── recommend_requote — sell side ─────────────────────────────────────────

def test_sell_requote_basic():
    """
    sell: original=$62.39, bid=$62.37
    candidate bid+1tick = 62.38
    candidate orig−5ticks = 62.34
    max(62.38, 62.34) = 62.38
    """
    stall = StallEvent("s1", original_limit=62.39, filled_qty=30, remaining_qty=25, seconds_stalled=312)
    quote = _quote(bid=62.37, ask=62.45)
    sugg = recommend_requote(stall, "sell", quote)
    assert sugg.chunk_id == "s1"
    assert sugg.new_limit == pytest.approx(62.38)
    assert sugg.remaining_qty == pytest.approx(25.0)
    assert len(sugg.rationale) > 0


def test_sell_requote_clamp_5ticks():
    """
    If bid has fallen far: bid=$62.00, orig=$62.39
    candidate bid+1tick = 62.01
    candidate orig−5ticks = 62.34
    max(62.01, 62.34) = 62.34 — clamp wins, don't chase bid too far down
    """
    stall = StallEvent("s1", original_limit=62.39, filled_qty=30, remaining_qty=25, seconds_stalled=400)
    quote = _quote(bid=62.00, ask=62.50)
    sugg = recommend_requote(stall, "sell", quote)
    assert sugg.new_limit == pytest.approx(62.34)


def test_sell_requote_rationale_contains_limits():
    stall = StallEvent("s1", original_limit=62.39, filled_qty=30, remaining_qty=25, seconds_stalled=350)
    quote = _quote(bid=62.37, ask=62.45)
    sugg = recommend_requote(stall, "sell", quote)
    combined = " ".join(sugg.rationale)
    assert "62.39" in combined
    assert "62.37" in combined


# ── recommend_requote — buy side ──────────────────────────────────────────

def test_buy_requote_basic():
    """
    buy: original=$75.50, ask=$75.53
    candidate ask−1tick = 75.52
    candidate orig+5ticks = 75.55
    min(75.52, 75.55) = 75.52
    """
    stall = StallEvent("b1", original_limit=75.50, filled_qty=50, remaining_qty=50, seconds_stalled=310)
    quote = _quote(bid=75.45, ask=75.53)
    sugg = recommend_requote(stall, "buy", quote)
    assert sugg.new_limit == pytest.approx(75.52)


def test_buy_requote_clamp_5ticks():
    """
    If ask has jumped far: ask=$76.00, orig=$75.50
    candidate ask−1tick = 75.99
    candidate orig+5ticks = 75.55
    min(75.99, 75.55) = 75.55 — clamp wins, don't chase ask too far up
    """
    stall = StallEvent("b1", original_limit=75.50, filled_qty=50, remaining_qty=50, seconds_stalled=350)
    quote = _quote(bid=75.90, ask=76.00)
    sugg = recommend_requote(stall, "buy", quote)
    assert sugg.new_limit == pytest.approx(75.55)


# ── MockATP.advance() ────────────────────────────────────────────────────

def test_mock_advance_partial_fill():
    mock = MockATP()
    mock.set_quote("EEM", bid=62.37, ask=62.45, last=62.40)
    placed = _now()
    mock.add_order(OrderRow(
        account="Roth IRA", symbol="EEM", side="SELL",
        qty=1600, filled_qty=0, limit_price=62.39,
        status=OrderStatus.Open,
        placed_at=placed, last_update_at=placed, order_id="s1",
    ))
    mock.advance(seconds=100, fills={"s1": 800})
    orders = mock.get_orders()
    assert orders[0].status == OrderStatus.PartiallyFilled
    assert orders[0].filled_qty == 800
    # last_update_at advanced
    assert (orders[0].last_update_at - placed).total_seconds() == pytest.approx(100)


def test_mock_advance_to_filled():
    mock = MockATP()
    placed = _now()
    mock.add_order(OrderRow(
        account="Roth IRA", symbol="EEM", side="SELL",
        qty=1600, filled_qty=0, limit_price=62.39,
        status=OrderStatus.Open,
        placed_at=placed, last_update_at=placed, order_id="s1",
    ))
    mock.advance(seconds=60, fills={"s1": 1600})
    orders = mock.get_orders()
    assert orders[0].status == OrderStatus.Filled


# ── End-to-end mock scenario ──────────────────────────────────────────────

def test_e2e_stall_detect_requote_recompute(tmp_path: Path):
    """
    Full lifecycle:
    - s1 = 1600 shs, fully fills at 62.41
    - s2 = 55 shs, partial 30/55 then stalls
    - After 5 simulated minutes, stall detected, re-quote suggested
    - Stall is confirmed: new chunk s2b created conceptually
    - All sells terminal → recompute trigger fires
    """
    from tui.monitor import (
        Journal, MonitorApp,
        _all_sells_terminal, _actual_proceeds,
    )
    from state.schema import (
        AccountInput, BuyAllocationRecord, BuyStrategy, ChunkRecord,
        Computed, EngineConfig, Inputs, PlanOutput, PositionInput,
        RebalanceState, SellRecord, SellStrategy, SignalInput,
    )
    from datetime import timezone

    placed = datetime(2026, 2, 27, 10, 0, 0, tzinfo=timezone.utc)

    # Build minimal state with 2 sell chunks
    inputs = Inputs(
        accounts=[AccountInput(
            name="Roth IRA", type="retirement", cash_reserve=0.0,
            positions=[PositionInput(symbol="EEM", quantity=1655, price=62.71, value=103805.05)],
            cash_spaxx=33.88,
            strategy_allocations={"Prismatic Prudence": 0.20},
        )],
        signals=[SignalInput(account="Roth IRA", strategy="Prismatic Prudence",
                             current_ticker="EEM", new_ticker="EWY")],
        config=EngineConfig(stall_threshold_seconds=300),
    )
    sell_chunks = [
        ChunkRecord(chunk_id="s1", account="Roth IRA", strategy="Prismatic Prudence",
                    ticker="EEM", idx=0, shares=1600, limit_price=62.39, cost=99824.0),
        ChunkRecord(chunk_id="s2", account="Roth IRA", strategy="Prismatic Prudence",
                    ticker="EEM", idx=1, shares=55, limit_price=62.39, cost=3431.45),
    ]
    computed = Computed(
        cash_ok={"Roth IRA": True},
        one_share_total={"Roth IRA": 338.0},
        sells=[SellRecord(account="Roth IRA", strategy="Prismatic Prudence",
                          ticker="EEM", shares=1655, limit_price=62.39, est_proceeds=103255.45)],
        buy_allocations=[BuyAllocationRecord(
            account="Roth IRA", strategy="Prismatic Prudence", ticker="EWY",
            dollar_target=99889.88, limit_price=75.50, share_target=1323,
            est_cost=99885.0,
        )],
        sell_chunks=sell_chunks,
        buy_chunks=[],
        sell_strategies=[SellStrategy(
            account="Roth IRA", strategy="Prismatic Prudence", ticker="EEM",
            limit_price=62.39, urgency="normal", rule="default",
            reasoning=["Spread is 3.2 bps."], chunk_ids=["s1", "s2"],
        )],
    )
    state = RebalanceState(
        generated_at=placed, generator="engine", inputs=inputs, computed=computed,
    )
    plan = PlanOutput(generated_at=placed, state=state)

    # Set up mock ATP
    mock = MockATP()
    mock.set_quote("EEM", bid=62.39, ask=62.42, last=62.41)

    mock.add_order(OrderRow(
        account="Roth IRA", symbol="EEM", side="SELL",
        qty=1600, filled_qty=0, limit_price=62.39,
        status=OrderStatus.Open, placed_at=placed, last_update_at=placed, order_id="s1",
    ))
    mock.add_order(OrderRow(
        account="Roth IRA", symbol="EEM", side="SELL",
        qty=55, filled_qty=0, limit_price=62.39,
        status=OrderStatus.Open, placed_at=placed, last_update_at=placed, order_id="s2",
    ))

    # Step 1: s1 fully fills, s2 partially fills at t+60s
    mock.advance(seconds=60, fills={"s1": 1600, "s2": 30})
    orders_t1 = mock.get_orders()
    assert orders_t1[0].status == OrderStatus.Filled
    assert orders_t1[1].status == OrderStatus.PartiallyFilled

    # Step 2: 5 simulated minutes pass — s2 still at 30/55
    # We don't advance s2 further; its last_update_at is still at t+60s
    # "now" is t + 360s (6 min), so s2 is stalled for 5 min
    now_simulated = placed + timedelta(seconds=360)
    stalls = detect_stalls(orders_t1, threshold_seconds=300, now=now_simulated)
    assert len(stalls) == 1, f"Expected 1 stall, got {stalls}"
    assert stalls[0].chunk_id == "s2"
    assert stalls[0].remaining_qty == pytest.approx(25.0)

    # Step 3: bid moves to 62.37, get re-quote suggestion
    mock.set_quote("EEM", bid=62.37, ask=62.45, last=62.38)
    quote = mock.get_quote("EEM")
    sugg = recommend_requote(stalls[0], "sell", quote)
    # bid+1tick=62.38, orig-5ticks=62.34 → max=62.38
    assert sugg.new_limit == pytest.approx(62.38)

    # Step 4: s2 fills (user cancels and re-enters as s2b)
    # Simulate s2 filled from partial (re-entered order fills fully)
    mock.advance(seconds=30, fills={"s2": 55})  # force fill
    orders_t2 = mock.get_orders()

    # Step 5: check all sells terminal
    order_map = {row.order_id: row for row in orders_t2}
    assert _all_sells_terminal("Roth IRA", order_map, ["s1", "s2"])

    # Step 6: proceeds computation
    proceeds = _actual_proceeds("Roth IRA", order_map, ["s1", "s2"])
    # s1: 1600 * 62.39 = 99824, s2: 55 * 62.39 = 3431.45
    assert proceeds == pytest.approx(1600 * 62.39 + 55 * 62.39, abs=0.01)


def test_journal_writes_events(tmp_path: Path):
    """Journal appends valid JSONL with the expected event types."""
    from tui.monitor import Journal
    journal_path = tmp_path / "journal.jsonl"
    j = Journal(journal_path)
    j.write("poll", {"order_count": 3, "changed": True})
    j.write("stall_detected", {"chunk_id": "s1", "seconds_stalled": 312.0})

    lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    e1 = json.loads(lines[0])
    assert e1["event_type"] == "poll"
    assert e1["payload"]["order_count"] == 3
    e2 = json.loads(lines[1])
    assert e2["event_type"] == "stall_detected"
    assert "ts" in e2


def test_monitor_app_renders_status(tmp_path: Path):
    """MonitorApp renders without errors in headless mode."""
    import asyncio
    from state.schema import (
        AccountInput, BuyAllocationRecord, ChunkRecord, Computed, EngineConfig,
        Inputs, PlanOutput, PositionInput, RebalanceState, SellRecord,
        SellStrategy, SignalInput,
    )
    from datetime import timezone

    placed = datetime(2026, 2, 27, 10, 0, 0, tzinfo=timezone.utc)
    inputs = Inputs(
        accounts=[AccountInput(
            name="Roth IRA", type="retirement", cash_reserve=0.0,
            positions=[PositionInput(symbol="EEM", quantity=1655, price=62.71, value=103805.05)],
            cash_spaxx=33.88,
            strategy_allocations={"Prismatic Prudence": 0.20},
        )],
        signals=[SignalInput(account="Roth IRA", strategy="Prismatic Prudence",
                             current_ticker="EEM", new_ticker="EWY")],
        config=EngineConfig(stall_threshold_seconds=300),
    )
    computed = Computed(
        cash_ok={"Roth IRA": True},
        one_share_total={"Roth IRA": 338.0},
        sells=[SellRecord(account="Roth IRA", strategy="Prismatic Prudence",
                          ticker="EEM", shares=1655, limit_price=62.39, est_proceeds=103255.45)],
        buy_allocations=[],
        sell_chunks=[
            ChunkRecord(chunk_id="s1", account="Roth IRA", strategy="Prismatic Prudence",
                        ticker="EEM", idx=0, shares=1600, limit_price=62.39, cost=99824.0),
        ],
        buy_chunks=[],
        sell_strategies=[SellStrategy(
            account="Roth IRA", strategy="Prismatic Prudence", ticker="EEM",
            limit_price=62.39, urgency="normal", rule="default",
            reasoning=["Spread is 3.2 bps."], chunk_ids=["s1"],
        )],
    )
    state = RebalanceState(
        generated_at=placed, generator="engine", inputs=inputs, computed=computed,
    )
    plan = PlanOutput(generated_at=placed, state=state)

    mock = MockATP()
    mock.set_quote("EEM", bid=62.37, ask=62.42, last=62.40)

    from tui.monitor import MonitorApp

    @pytest.mark.anyio
    async def _run():
        app = MonitorApp(plan=plan, orders_adapter=mock, poll_seconds=999)
        async with app.run_test(headless=True, size=(120, 40)) as pilot:
            await pilot.pause()
            text = " ".join(str(w.content) for w in app.screen.query("Static"))
            assert "EXECUTION STATUS" in text
            assert "Roth IRA" in text

    asyncio.run(_run())
