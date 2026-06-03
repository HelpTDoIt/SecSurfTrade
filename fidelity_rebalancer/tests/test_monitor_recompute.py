"""
F-1: live-recompute trigger + allocation update — tests for the monitor glue.

Covers the three seams added to ``tui.monitor``:
  • ``_market_minutes``      — local-time → minutes-since-open (ET convention)
  • ``_should_recompute``    — terminal-OR-clock trigger decision (per account)
  • ``_recompute_account``   — re-allocate on realized proceeds, update state,
                               emit a ``recompute_buys`` journal event

The MonitorApp is constructed directly (no Textual harness needed) because the
methods under test touch only ``self._state`` and the Journal, never widgets.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adapters import OrderRow, OrderStatus
from adapters.mock_atp import MockATP
from state.schema import (
    AccountInput,
    BuyAllocationRecord,
    ChunkRecord,
    Computed,
    EngineConfig,
    Inputs,
    PlanOutput,
    PositionInput,
    RebalanceState,
    SignalInput,
)
from tui.monitor import Journal, MonitorApp, _market_minutes

ACCOUNT = "Acct"
T0 = datetime(2026, 6, 3, 14, 30, tzinfo=timezone.utc)


# ── State / app builders ───────────────────────────────────────────────────


def _build_state() -> RebalanceState:
    """Two trading strategies, 50/50, total_pool 200k, initial 1000 sh each."""
    acct = AccountInput(
        name=ACCOUNT,
        type="retirement",
        cash_reserve=0.0,
        positions=[
            PositionInput(symbol="EEM", quantity=1000, price=100.0, value=100_000.0),
            PositionInput(symbol="SMH", quantity=500, price=200.0, value=100_000.0),
            PositionInput(symbol="SPAXX**", quantity=0, price=1.0, value=0.0),
        ],
        cash_spaxx=0.0,
        strategy_allocations={"Alpha": 0.5, "Beta": 0.5},
    )
    signals = [
        SignalInput(account=ACCOUNT, strategy="Alpha", current_ticker="EEM", new_ticker="VOO"),
        SignalInput(account=ACCOUNT, strategy="Beta", current_ticker="SMH", new_ticker="QQQ"),
    ]
    buy_allocations = [
        BuyAllocationRecord(
            account=ACCOUNT, strategy="Alpha", ticker="VOO",
            dollar_target=100_000.0, limit_price=100.0, share_target=1000,
            est_cost=100_000.0,
        ),
        BuyAllocationRecord(
            account=ACCOUNT, strategy="Beta", ticker="QQQ",
            dollar_target=100_000.0, limit_price=100.0, share_target=1000,
            est_cost=100_000.0,
        ),
    ]
    sell_chunks = [
        ChunkRecord(chunk_id="s1", account=ACCOUNT, strategy="Alpha", ticker="EEM",
                    idx=0, shares=1000, limit_price=100.0, cost=100_000.0),
        ChunkRecord(chunk_id="s2", account=ACCOUNT, strategy="Beta", ticker="SMH",
                    idx=0, shares=500, limit_price=200.0, cost=100_000.0),
    ]
    computed = Computed(
        cash_ok={ACCOUNT: True},
        one_share_total={ACCOUNT: 0.0},
        sells=[],
        buy_allocations=buy_allocations,
        sell_chunks=sell_chunks,
        buy_chunks=[],
    )
    return RebalanceState(
        generated_at=T0,
        generator="engine",
        inputs=Inputs(
            accounts=[acct],
            signals=signals,
            prev_closes={"VOO": 100.0, "QQQ": 100.0, "EEM": 100.0, "SMH": 200.0},
            config=EngineConfig(),  # sweep_time_minutes=330, sweep_unfilled_frac=0.5
        ),
        computed=computed,
    )


def _app(tmp_path: Path) -> MonitorApp:
    state = _build_state()
    plan = PlanOutput(generated_at=T0, state=state)
    journal = Journal(tmp_path / "journal.jsonl")
    return MonitorApp(plan=plan, orders_adapter=MockATP(), journal=journal, poll_seconds=999)


def _sell_row(order_id: str, status: OrderStatus, filled: float, limit: float) -> OrderRow:
    return OrderRow(
        account=ACCOUNT, symbol="EEM", side="SELL", qty=filled, filled_qty=filled,
        limit_price=limit, status=status, placed_at=T0, last_update_at=T0,
        order_id=order_id,
    )


def _read_events(tmp_path: Path) -> list[dict]:
    text = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ── _market_minutes ────────────────────────────────────────────────────────


def test_market_minutes_open():
    assert _market_minutes(datetime(2026, 6, 3, 9, 30)) == 0


def test_market_minutes_three_pm():
    assert _market_minutes(datetime(2026, 6, 3, 15, 0)) == 330


def test_market_minutes_close():
    assert _market_minutes(datetime(2026, 6, 3, 16, 0)) == 390


def test_market_minutes_before_open_is_none():
    assert _market_minutes(datetime(2026, 6, 3, 9, 0)) is None


def test_market_minutes_after_close_is_none():
    assert _market_minutes(datetime(2026, 6, 3, 16, 1)) is None


# ── _should_recompute (terminal OR clock) ──────────────────────────────────


def test_should_recompute_fires_when_all_terminal(tmp_path):
    app = _app(tmp_path)
    order_map = {
        "s1": _sell_row("s1", OrderStatus.Filled, 1000, 100.0),
        "s2": _sell_row("s2", OrderStatus.Filled, 500, 200.0),
    }
    fire, reason = app._should_recompute(ACCOUNT, ["s1", "s2"], order_map, mkt_minutes=120)
    assert fire is True
    assert reason == "all_sells_terminal"


def test_should_recompute_fires_on_clock_when_open(tmp_path):
    app = _app(tmp_path)
    order_map = {"s1": _sell_row("s1", OrderStatus.Open, 0, 100.0)}
    fire, reason = app._should_recompute(ACCOUNT, ["s1", "s2"], order_map, mkt_minutes=330)
    assert fire is True
    assert reason == "eod_sweep_clock"


def test_should_recompute_silent_before_clock_and_open(tmp_path):
    app = _app(tmp_path)
    order_map = {"s1": _sell_row("s1", OrderStatus.Open, 0, 100.0)}
    fire, reason = app._should_recompute(ACCOUNT, ["s1", "s2"], order_map, mkt_minutes=120)
    assert fire is False
    assert reason == ""


def test_should_recompute_skips_already_recomputed(tmp_path):
    app = _app(tmp_path)
    app._recomputed_accounts.add(ACCOUNT)
    order_map = {
        "s1": _sell_row("s1", OrderStatus.Filled, 1000, 100.0),
        "s2": _sell_row("s2", OrderStatus.Filled, 500, 200.0),
    }
    fire, _ = app._should_recompute(ACCOUNT, ["s1", "s2"], order_map, mkt_minutes=330)
    assert fire is False


def test_should_recompute_skips_account_without_sells(tmp_path):
    app = _app(tmp_path)
    fire, _ = app._should_recompute(ACCOUNT, [], {}, mkt_minutes=330)
    assert fire is False


# ── _recompute_account (state update + journal) ────────────────────────────


def test_recompute_account_shrinks_targets_and_journals(tmp_path):
    app = _app(tmp_path)
    # Realized 180k (estimate was 200k) → 900 shares each, down from 1000.
    app._recompute_account(ACCOUNT, 180_000.0, "all_sells_terminal")

    # In-memory buy_allocations replaced for this account.
    by_strat = {
        ba.strategy: ba.share_target
        for ba in app._state.computed.buy_allocations
        if ba.account == ACCOUNT
    }
    assert by_strat == {"Alpha": 900, "Beta": 900}

    # Journal carries the revised plan for the human.
    events = _read_events(tmp_path)
    rb = [e for e in events if e["event_type"] == "recompute_buys"]
    assert len(rb) == 1
    payload = rb[0]["payload"]
    assert payload["account"] == ACCOUNT
    assert payload["trigger"] == "all_sells_terminal"
    assert payload["proceeds"] == pytest.approx(180_000.0)
    assert payload["before"] == {"Alpha": 1000, "Beta": 1000}
    assert payload["after"] == {"Alpha": 900, "Beta": 900}


def test_recompute_account_does_not_rechunk(tmp_path):
    """F-1 updates allocations only — buy_chunks are left untouched."""
    app = _app(tmp_path)
    before_chunks = list(app._state.computed.buy_chunks)
    app._recompute_account(ACCOUNT, 180_000.0, "all_sells_terminal")
    assert app._state.computed.buy_chunks == before_chunks
