"""
Tests for engine.optimizer — proportional + greedy drift-minimizer.
Three required cases per spec:
  (a) integer-only result
  (b) ties broken by index order (first candidate wins)
  (c) budget exactly equal to one-share total
"""
from __future__ import annotations

import pytest

from engine.optimizer import live_buys


def _make_candidate(strategy, ticker, limit_price, target_alloc, current_val, total_pool):
    target_val = target_alloc * total_pool
    return {
        "strategy": strategy,
        "ticker": ticker,
        "limit_price": limit_price,
        "target_val": target_val,
        "current_val": current_val,
        "deficit": target_val - current_val,
        "is_rebalance": True,
        "shares": 0,
        "est_cost": 0.0,
    }


# ── (a) Integer-only result ────────────────────────────────────────────────

def test_integer_only_result():
    """
    Two strategies, equal deficit, proportional split gives exact integer shares.
    No greedy phase needed (budget exhausted after phase 1).
    """
    total_pool = 1000.0
    strategies = {"A": 0.50, "B": 0.50}
    candidates = [
        _make_candidate("A", "AA", 50.0, 0.50, 0.0, total_pool),
        _make_candidate("B", "BB", 50.0, 0.50, 0.0, total_pool),
    ]
    actual_avail = 1000.0

    result = live_buys(candidates, actual_avail, total_pool, strategies)

    assert len(result) == 2
    shares = {r["strategy"]: r["shares"] for r in result}
    # Each gets propBudget=500, shares=floor(500/50)=10
    assert shares["A"] == 10
    assert shares["B"] == 10
    # Budget fully consumed
    total_cost = sum(r["est_cost"] for r in result)
    assert total_cost == pytest.approx(actual_avail)


# ── (b) Tie-breaking by index order ───────────────────────────────────────

def test_tie_broken_by_index_order():
    """
    Two candidates with identical drift reduction in the greedy phase.
    The first candidate (index 0) must receive the extra share, not index 1.
    Strict `>` in comparison means a tie does NOT replace the current best.
    """
    total_pool = 1000.0
    strategies = {"A": 0.50, "B": 0.50}
    # Each gets 450 in phase 1; budget_left = 50 → one more share of price=50
    # Both have identical drift situation → A (index 0) wins
    candidates = [
        _make_candidate("A", "AA", 50.0, 0.50, 0.0, total_pool),
        _make_candidate("B", "BB", 50.0, 0.50, 0.0, total_pool),
    ]
    actual_avail = 950.0  # phase 1 gives 9 shares each (cost=900), budget_left=50

    result = live_buys(candidates, actual_avail, total_pool, strategies)

    shares = {r["strategy"]: r["shares"] for r in result}
    # A=10 shares (got the extra), B=9 shares
    assert shares["A"] == 10
    assert shares["B"] == 9


# ── (c) Budget exactly equal to one-share total ───────────────────────────

def test_budget_equals_one_share_total():
    """
    actual_avail == sum of one share per strategy → each strategy gets exactly 1 share.
    """
    total_pool = 1000.0
    strategies = {"A": 0.50, "B": 0.50}
    # Limit prices: A=$30, B=$70 → one-share total = $100
    candidates = [
        _make_candidate("A", "AA", 30.0, 0.50, 0.0, total_pool),
        _make_candidate("B", "BB", 70.0, 0.50, 0.0, total_pool),
    ]
    actual_avail = 100.0  # exactly 1 share of A + 1 share of B

    result = live_buys(candidates, actual_avail, total_pool, strategies)

    shares = {r["strategy"]: r["shares"] for r in result}
    assert shares["A"] == 1
    assert shares["B"] == 1


# ── Additional edge cases ──────────────────────────────────────────────────

def test_empty_candidates_returns_empty():
    assert live_buys([], 1000.0, 1000.0, {}) == []


def test_zero_avail_returns_empty():
    total_pool = 1000.0
    strategies = {"A": 1.0}
    candidates = [_make_candidate("A", "AA", 50.0, 1.0, 0.0, total_pool)]
    assert live_buys(candidates, 0.0, total_pool, strategies) == []


def test_over_deficit_tolerance_prevents_overallocation():
    """
    Tolerance check: est_cost + limit_price > deficit + limit_price * 0.5 → skip.

    Setup: deficit=100, limit_price=100, avail=250.
      Phase 1: shares=floor(min(250,100)/100)=1, est_cost=100, budget_left=150.
      Greedy: 100+100=200 > 100+50=150 → True → blocked.
    Result: exactly 1 share (phase 1 only, greedy cannot add a 2nd).
    """
    total_pool = 1000.0
    strategies = {"A": 0.10}
    candidates = [_make_candidate("A", "AA", 100.0, 0.10, 0.0, total_pool)]
    result = live_buys(candidates, 250.0, total_pool, strategies)
    assert len(result) == 1
    assert result[0]["shares"] == 1


def test_greedy_picks_largest_drift_reduction():
    """
    Greedy phase assigns the share to whichever candidate reduces drift most.
    """
    total_pool = 2000.0
    strategies = {"A": 0.70, "B": 0.30}
    # A under-allocated (large deficit), B fully funded
    candidates = [
        _make_candidate("A", "AA", 100.0, 0.70, 0.0, total_pool),   # deficit=1400
        _make_candidate("B", "BB", 100.0, 0.30, 600.0, total_pool), # deficit=0 → filtered out
    ]
    candidates = [c for c in candidates if c["deficit"] > 0]
    result = live_buys(candidates, 500.0, total_pool, strategies)
    assert len(result) == 1
    assert result[0]["strategy"] == "A"
    assert result[0]["shares"] == 5
