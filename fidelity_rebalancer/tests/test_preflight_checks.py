from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

import pytest

from preflight.checks import (
    FtRunningResult,
    TickerPresenceResult,
    check_ft_running,
    check_tickers_present,
)
from preflight.planner import plan_l2_windows


# ── check_ft_running ───────────────────────────────────────────────────────


def test_ft_running_success():
    sentinel = object()
    res = check_ft_running(connect=lambda: sentinel)
    assert isinstance(res, FtRunningResult)
    assert res.running is True


def test_ft_running_failure_carries_message():
    msg = "Cannot connect to 'Fidelity Trader+.exe'."

    def boom():
        raise RuntimeError(msg)

    res = check_ft_running(connect=boom)
    assert res.running is False
    assert msg in res.detail


# ── check_tickers_present ──────────────────────────────────────────────────


def _watchlist(*symbols):
    """Build a fake get_watchlist() returning a dict keyed by symbol."""
    return lambda: {s: object() for s in symbols}


def test_all_present_ok():
    plan = plan_l2_windows(["SPY", "AAPL"], [("AAA", 5.0), ("BBB", 3.0)])
    # watchlist == AAA, AAPL, BBB, SPY ; l2_assigned == AAA, BBB
    res = check_tickers_present(
        plan,
        read_watchlist=_watchlist("AAA", "AAPL", "BBB", "SPY"),
        enumerate_l2=lambda: {"AAA", "BBB"},
    )
    assert isinstance(res, TickerPresenceResult)
    assert res.ok is True
    assert res.missing_watchlist == []
    assert res.missing_l2 == []
    assert res.present_watchlist == ["AAA", "AAPL", "BBB", "SPY"]
    assert res.present_l2 == ["AAA", "BBB"]


def test_missing_watchlist_sorted():
    plan = plan_l2_windows(["SPY", "AAPL", "MSFT"], [])
    res = check_tickers_present(
        plan,
        read_watchlist=_watchlist("SPY"),
        enumerate_l2=lambda: set(),
    )
    assert res.ok is False
    assert res.missing_watchlist == ["AAPL", "MSFT"]
    assert res.missing_l2 == []
    assert res.present_watchlist == ["SPY"]


def test_missing_l2_sorted():
    plan = plan_l2_windows([], [("AAA", 5.0), ("BBB", 3.0), ("CCC", 1.0)])
    # all in watchlist, but only AAA visible in L2
    res = check_tickers_present(
        plan,
        read_watchlist=_watchlist("AAA", "BBB", "CCC"),
        enumerate_l2=lambda: {"AAA"},
    )
    assert res.ok is False
    assert res.missing_watchlist == []
    assert res.missing_l2 == ["BBB", "CCC"]
    assert res.present_l2 == ["AAA"]


def test_case_and_whitespace_insensitive():
    plan = plan_l2_windows(["SPY", "AAPL"], [("AAA", 5.0)])
    # OCR returns lowercase / padded symbols
    res = check_tickers_present(
        plan,
        read_watchlist=_watchlist(" spy ", "aapl", "AAA"),
        enumerate_l2=lambda: {" aaa "},
    )
    assert res.ok is True
    assert res.missing_watchlist == []
    assert res.missing_l2 == []
    # reported values are normalized + sorted
    assert res.present_watchlist == ["AAA", "AAPL", "SPY"]
    assert res.present_l2 == ["AAA"]


def test_empty_plan_ok():
    plan = plan_l2_windows([], [])
    res = check_tickers_present(
        plan,
        read_watchlist=_watchlist(),
        enumerate_l2=lambda: set(),
    )
    assert res.ok is True
    assert res.missing_watchlist == []
    assert res.missing_l2 == []
    assert res.present_watchlist == []
    assert res.present_l2 == []


def test_extra_watchlist_tickers_no_false_missing():
    # watchlist has many tickers; plan needs only a subset.
    plan = plan_l2_windows(["AAPL"], [("AAA", 5.0)])
    res = check_tickers_present(
        plan,
        read_watchlist=_watchlist("AAPL", "AAA", "ZZZ", "QQQ", "MSFT"),
        enumerate_l2=lambda: {"AAA", "ZZZ", "QQQ"},
    )
    assert res.ok is True
    assert res.missing_watchlist == []
    assert res.missing_l2 == []
    # present_* reflects only what the plan needed and was found
    assert res.present_watchlist == ["AAA", "AAPL"]
    assert res.present_l2 == ["AAA"]
    # visible_l2 is the FULL set of open panels, normalized + sorted, including
    # ones the plan did not need (so the CLI can offer them as safe-to-close).
    assert res.visible_l2 == ["AAA", "QQQ", "ZZZ"]


def test_visible_l2_normalized_and_sorted():
    plan = plan_l2_windows([], [("AAA", 5.0)])
    res = check_tickers_present(
        plan,
        read_watchlist=_watchlist("AAA"),
        enumerate_l2=lambda: {" bbb ", "aaa", "CCC"},
    )
    # All open panels reported, upper-cased, stripped, sorted — regardless of
    # whether they were needed.
    assert res.visible_l2 == ["AAA", "BBB", "CCC"]


def test_no_heavy_imports_pulled_in():
    # Importing preflight.checks and exercising it with fakes must not import
    # rapidocr or pywinauto.
    assert "rapidocr_onnxruntime" not in sys.modules
    assert "pywinauto" not in sys.modules
