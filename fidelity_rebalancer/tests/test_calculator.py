"""
Tests for engine.calculator — including Feb 27 parity test.
All expected values are hand-traced from the React JS source.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from engine.calculator import _alloc_buys, _fv, calc_trades, consolidate, parse_csv
from engine.chunker import build_buy_chunks_legacy, build_sell_chunks_legacy

FIXTURES = Path(__file__).parent / "fixtures"


# ── parse_csv / consolidate ────────────────────────────────────────────────


def test_parse_csv_filters_metadata_lines():
    text = '"Test Retirement"\nAccount Name,Symbol,Quantity,Last Price,Current Value\nTest Retirement,EIS,100,$28.50,$2850.00\n'
    rows = parse_csv(text)
    assert len(rows) == 1
    assert rows[0]["Symbol"] == "EIS"
    assert rows[0]["Quantity"] == "100"


def test_parse_csv_strips_dollar_and_signs():
    text = "Account Name,Symbol,Quantity,Last Price,Current Value\nTest Retirement,EEM,200,$62.71,$12542.00\n"
    rows = parse_csv(text)
    assert rows[0]["Last Price"] == "62.71"
    assert rows[0]["Current Value"] == "12542.00"


def test_fv_parses_negative_currency():
    """Fidelity 'Pending activity' rows export as '-$99105.05' (sign BEFORE $).
    Prior leading-only strip silently turned these into 0.0."""
    assert _fv("-$99105.05") == pytest.approx(-99105.05)
    assert _fv("-$216,901.85") == pytest.approx(-216901.85)
    assert _fv("$350,056.51") == pytest.approx(350056.51)
    assert _fv("+$1,245.61") == pytest.approx(1245.61)


def test_consolidate_extracts_pending_activity():
    """'Pending activity' row goes to pending_activity field, not positions."""
    rows = [
        {
            "Account Name": "Rollover IRA",
            "Symbol": "SPAXX**",
            "Quantity": "",
            "Current Value": "350056.51",
            "Last Price": "",
        },
        {
            "Account Name": "Rollover IRA",
            "Symbol": "Pending activity",
            "Quantity": "",
            "Current Value": "-216901.85",
            "Last Price": "",
        },
    ]
    result = consolidate(rows)
    assert "Pending activity" not in result["positions"]
    assert result["positions"]["SPAXX**"]["value"] == pytest.approx(350056.51)
    assert result["pending_activity"] == pytest.approx(-216901.85)


def test_consolidate_sums_duplicate_symbols():
    rows = [
        {
            "Account Name": "Test Taxable",
            "Symbol": "SMH",
            "Quantity": "30",
            "Current Value": "6000",
            "Last Price": "200",
        },
        {
            "Account Name": "Test Taxable",
            "Symbol": "SMH",
            "Quantity": "20",
            "Current Value": "4000",
            "Last Price": "200",
        },
    ]
    result = consolidate(rows)
    assert result["account_name"] == "Test Taxable"
    smh = result["positions"]["SMH"]
    assert smh["quantity"] == 50.0
    assert smh["value"] == 10000.0
    assert smh["price"] == 200.0  # price from first occurrence


def test_consolidate_price_from_first_occurrence():
    """Price must come from the first row even if second row has a different value."""
    rows = [
        {
            "Account Name": "A",
            "Symbol": "SMH",
            "Quantity": "30",
            "Current Value": "6000",
            "Last Price": "200",
        },
        {
            "Account Name": "A",
            "Symbol": "SMH",
            "Quantity": "20",
            "Current Value": "4200",
            "Last Price": "210",
        },
    ]
    result = consolidate(rows)
    assert result["positions"]["SMH"]["price"] == 200.0


def test_consolidate_from_csv_fixture():
    """SMH in both Cash and Margin lots must consolidate to 50 shares."""
    text = (FIXTURES / "individual_tod.csv").read_text(encoding="utf-8")
    from engine.calculator import parse_csv

    rows = parse_csv(text)
    result = consolidate(rows)
    smh = result["positions"]["SMH"]
    assert smh["quantity"] == 50.0
    assert smh["value"] == pytest.approx(10000.0)


# ── calc_trades (Feb 27 parity) ────────────────────────────────────────────


def _load_fixtures():
    inputs = json.loads((FIXTURES / "feb27.json").read_text())
    expected = json.loads((FIXTURES / "feb27_expected.json").read_text())
    return inputs, expected


def test_parity_cash_ok():
    inputs, expected = _load_fixtures()
    acct = "Test Retirement"
    cfg = inputs["accounts"][acct]["config"]
    positions = inputs["accounts"][acct]["positions"]
    result = calc_trades(cfg, positions, inputs["signals"], inputs["closes"])
    assert result["cash_ok"] == expected[acct]["cash_ok"]


def test_calc_trades_signed_pending_reduces_cash():
    """Negative pending activity must reduce available cash in the gate.

    Pure rebalance scenario (no signal changes) with $350K SPAXX and -$216,901.85
    pending — true available is $133K. Without the fix the gate sees the full
    $350K and oversizes buys against unsettled commitments.
    """
    cfg = {
        "strategies": {"S1": 1.0},
        "cashReserve": 0.0,
    }
    positions = {
        "SPAXX**": {"symbol": "SPAXX**", "quantity": 0, "value": 350056.51, "price": 0},
    }
    signals = {"S1": {"current": "QQQ", "new": "QQQ"}}  # HOLD (no trade)
    closes = {"QQQ": 100.0}
    # Without pending: depl_cash = $350,056.51
    r_no_pending = calc_trades(cfg, positions, signals, closes)
    assert r_no_pending["depl_cash"] == pytest.approx(350056.51)
    # With -$216,901.85 pending: effective cash = $133,154.66
    r_with_pending = calc_trades(cfg, positions, signals, closes, -216901.85)
    assert r_with_pending["depl_cash"] == pytest.approx(133154.66)
    # Positive pending adds (signed model per user choice)
    r_pos_pending = calc_trades(cfg, positions, signals, closes, 50000.0)
    assert r_pos_pending["depl_cash"] == pytest.approx(400056.51)


def test_parity_one_share_total():
    inputs, expected = _load_fixtures()
    acct = "Test Retirement"
    cfg = inputs["accounts"][acct]["config"]
    positions = inputs["accounts"][acct]["positions"]
    result = calc_trades(cfg, positions, inputs["signals"], inputs["closes"])
    assert result["one_share_total"] == pytest.approx(expected[acct]["one_share_total"])


def test_parity_sells():
    inputs, expected = _load_fixtures()
    acct = "Test Retirement"
    cfg = inputs["accounts"][acct]["config"]
    positions = inputs["accounts"][acct]["positions"]
    result = calc_trades(cfg, positions, inputs["signals"], inputs["closes"])
    exp_sells = expected[acct]["sells"]
    assert len(result["sells"]) == len(exp_sells)
    for got, exp in zip(result["sells"], exp_sells):
        assert got["strategy"] == exp["strategy"]
        assert got["ticker"] == exp["ticker"]
        assert got["quantity"] == pytest.approx(exp["quantity"])
        assert got["limit_price"] == pytest.approx(exp["limit_price"])
        assert got["est_proceeds"] == pytest.approx(exp["est_proceeds"])


def test_parity_buy_allocations():
    inputs, expected = _load_fixtures()
    acct = "Test Retirement"
    cfg = inputs["accounts"][acct]["config"]
    positions = inputs["accounts"][acct]["positions"]
    result = calc_trades(cfg, positions, inputs["signals"], inputs["closes"])
    exp_buys = expected[acct]["buy_allocations"]
    assert len(result["buys"]) == len(exp_buys)
    for got, exp in zip(result["buys"], exp_buys):
        assert got["strategy"] == exp["strategy"]
        assert got["ticker"] == exp["ticker"]
        assert got["dollar_target"] == pytest.approx(exp["dollar_target"])
        assert got["shares"] == exp["shares"]
        assert got["est_cost"] == pytest.approx(exp["est_cost"])


def test_parity_sell_chunks():
    inputs, expected = _load_fixtures()
    acct = "Test Retirement"
    cfg = inputs["accounts"][acct]["config"]
    positions = inputs["accounts"][acct]["positions"]
    result = calc_trades(cfg, positions, inputs["signals"], inputs["closes"])
    exp_sc = expected[acct]["sell_chunks"]
    for sell, exp_chunks in zip(result["sells"], exp_sc):
        chunks = build_sell_chunks_legacy(sell["quantity"], sell["limit_price"])
        assert len(chunks) == len(exp_chunks)
        for got_c, exp_c in zip(chunks, exp_chunks):
            assert got_c["shares"] == pytest.approx(exp_c["shares"])
            assert got_c["limit_price"] == pytest.approx(exp_c["limit_price"])


def test_parity_buy_chunks():
    inputs, expected = _load_fixtures()
    acct = "Test Retirement"
    cfg = inputs["accounts"][acct]["config"]
    positions = inputs["accounts"][acct]["positions"]
    result = calc_trades(cfg, positions, inputs["signals"], inputs["closes"])
    exp_bc = expected[acct]["buy_chunks"]
    for buy, exp_chunks in zip(result["buys"], exp_bc):
        chunks = build_buy_chunks_legacy(buy["dollar_target"], buy["limit_price"])
        assert len(chunks) == len(exp_chunks)
        for got_c, exp_c in zip(chunks, exp_chunks):
            assert got_c["shares"] == exp_c["shares"]
            assert got_c["limit_price"] == pytest.approx(exp_c["limit_price"])


# ── alloc_buys: pure-rebalance (no trades, cashOk=True) ───────────────────


def test_alloc_buys_rebalance_no_trade():
    """
    No trading strategies, cashOk=True: rebalance deploys cash proportionally
    to strategy deficits, scaled down to available funds.
    """
    strategies = {"A": 0.50, "B": 0.50}
    signals = {"A": {"current": "AA", "new": "AA"}, "B": {"current": "BB", "new": "BB"}}
    closes = {"AA": 10.0, "BB": 10.0}
    # A is under-target, B is at target
    s_pos = {
        "A": {"ticker": "AA", "value": 400.0, "quantity": 40, "price": 10.0},
        "B": {"ticker": "BB", "value": 500.0, "quantity": 50, "price": 10.0},
    }
    total_pool = 1100.0  # 1000 in positions + 100 cash
    # A deficit = 0.5*1100 - 400 = 150, B deficit = 0.5*1100 - 500 = 50
    # avail = 100
    buys = _alloc_buys(
        list(strategies.keys()),
        strategies,
        signals,
        closes,
        s_pos,
        [],
        ["A", "B"],
        True,
        0.0,
        100.0,
        total_pool,
    )
    tickers = {b["ticker"] for b in buys}
    assert "AA" in tickers
    assert "BB" in tickers
    total_cost = sum(b["est_cost"] for b in buys)
    assert total_cost <= 100.0 + 0.01


def test_alloc_buys_single_trade_no_cash():
    """
    One trading strategy, !cashOk: all avail goes to that strategy.
    """
    strategies = {"A": 0.50, "B": 0.50}
    signals = {"A": {"current": "AA", "new": "BB"}, "B": {"current": "BB", "new": "BB"}}
    closes = {"AA": 10.0, "BB": 10.0}
    s_pos = {
        "A": {"ticker": "AA", "value": 500.0, "quantity": 50, "price": 10.0},
        "B": {"ticker": "BB", "value": 500.0, "quantity": 50, "price": 10.0},
    }
    # sell_proceeds = 500, depl_cash = 5 (cashOk=False because oneShare>5)
    buys = _alloc_buys(
        list(strategies.keys()),
        strategies,
        signals,
        closes,
        s_pos,
        ["A"],
        ["B"],
        False,
        500.0,
        5.0,
        1005.0,
    )
    assert len(buys) == 1
    assert buys[0]["ticker"] == "BB"
    assert buys[0]["dollar_target"] == pytest.approx(505.0)
    assert buys[0]["shares"] == 50


def test_alloc_buys_no_trade_no_cash_returns_empty():
    """No trades and !cashOk → empty list."""
    strategies = {"A": 0.50}
    signals = {"A": {"current": "AA", "new": "AA"}}
    closes = {"AA": 100.0}
    s_pos = {"A": {"ticker": "AA", "value": 500.0, "quantity": 5, "price": 100.0}}
    buys = _alloc_buys(
        ["A"],
        strategies,
        signals,
        closes,
        s_pos,
        [],
        ["A"],
        False,
        0.0,
        0.0,
        500.0,
    )
    assert buys == []


# ── engine module I/O check (no open/requests/print) ──────────────────────


def test_no_io_in_engine():
    """Verify engine modules contain no file/network/print I/O."""
    engine_dir = Path(__file__).parent.parent / "engine"
    for py_file in engine_dir.glob("*.py"):
        src = py_file.read_text(encoding="utf-8")
        assert "open(" not in src, f"{py_file.name} contains open()"
        assert "requests" not in src, f"{py_file.name} imports requests"
        # allow print only if it appears inside a string literal check
        lines_with_print = [
            l
            for l in src.splitlines()
            if "print(" in l and not l.strip().startswith("#")
        ]
        assert not lines_with_print, (
            f"{py_file.name} contains print(): {lines_with_print}"
        )
