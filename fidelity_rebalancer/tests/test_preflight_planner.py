from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from preflight.planner import L2WindowPlan, plan_l2_windows


def test_empty_inputs():
    plan = plan_l2_windows([], [])
    assert plan.watchlist == []
    assert plan.l2_assigned == []
    assert plan.l2_overflow == []
    assert plan.cap == 7


def test_needed_only_no_thin():
    plan = plan_l2_windows(["SPY", "AAPL", "MSFT"], [])
    assert plan.watchlist == ["AAPL", "MSFT", "SPY"]
    assert plan.l2_assigned == []
    assert plan.l2_overflow == []


def test_thin_under_cap_all_assigned_pct_desc():
    thin = [("AAA", 1.0), ("BBB", 5.0), ("CCC", 3.0)]
    plan = plan_l2_windows([], thin)
    assert plan.l2_assigned == ["BBB", "CCC", "AAA"]
    assert plan.l2_overflow == []
    assert plan.watchlist == ["AAA", "BBB", "CCC"]


def test_thin_over_cap_splits_assigned_and_overflow():
    # 9 thin tickers, cap 7 -> 7 highest in assigned, lowest 2 in overflow
    thin = [
        ("T1", 9.0),
        ("T2", 8.0),
        ("T3", 7.0),
        ("T4", 6.0),
        ("T5", 5.0),
        ("T6", 4.0),
        ("T7", 3.0),
        ("T8", 2.0),
        ("T9", 1.0),
    ]
    plan = plan_l2_windows([], thin, cap=7)
    assert plan.l2_assigned == ["T1", "T2", "T3", "T4", "T5", "T6", "T7"]
    assert plan.l2_overflow == ["T8", "T9"]
    assert plan.cap == 7


def test_equal_pct_alphabetical_tiebreak():
    thin = [("CCC", 2.0), ("AAA", 2.0), ("BBB", 2.0)]
    plan = plan_l2_windows([], thin)
    assert plan.l2_assigned == ["AAA", "BBB", "CCC"]


def test_duplicate_ticker_keeps_highest_pct():
    # SPY appears twice; highest pct (8.0) retained, affecting ranking
    thin = [("SPY", 1.0), ("QQQ", 5.0), ("SPY", 8.0)]
    plan = plan_l2_windows([], thin)
    # SPY now ranks above QQQ
    assert plan.l2_assigned == ["SPY", "QQQ"]
    # watchlist deduped
    assert plan.watchlist == ["QQQ", "SPY"]


def test_normalization_uppercase_and_strip():
    thin = [(" dfen ", 3.0), ("spy", 5.0)]
    plan = plan_l2_windows([" aapl "], thin)
    assert plan.l2_assigned == ["SPY", "DFEN"]
    assert "AAPL" in plan.watchlist
    assert "SPY" in plan.watchlist
    assert "DFEN" in plan.watchlist
    assert plan.watchlist == ["AAPL", "DFEN", "SPY"]


def test_thin_ticker_not_in_needed_appears_in_watchlist():
    plan = plan_l2_windows(["AAPL"], [("XYZ", 4.0)])
    assert "XYZ" in plan.watchlist
    assert plan.watchlist == ["AAPL", "XYZ"]


def test_cap_zero_all_overflow():
    thin = [("AAA", 1.0), ("BBB", 5.0)]
    plan = plan_l2_windows([], thin, cap=0)
    assert plan.l2_assigned == []
    assert plan.l2_overflow == ["BBB", "AAA"]
    assert plan.cap == 0


def test_returns_frozen_dataclass():
    plan = plan_l2_windows([], [])
    assert isinstance(plan, L2WindowPlan)
