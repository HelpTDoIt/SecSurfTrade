"""
Tests for engine.optimizer — proportional + greedy drift-minimizer.
Three required cases per spec:
  (a) integer-only result
  (b) ties broken by index order (first candidate wins)
  (c) budget exactly equal to one-share total
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from engine.optimizer import live_buys, recompute_buys
from state.schema import (
    AccountInput,
    BuyAllocationRecord,
    Computed,
    Inputs,
    PositionInput,
    RebalanceState,
    SignalInput,
)


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


# ── recompute_buys (F-1): live re-allocation against realized proceeds ──────


def _state(
    *,
    strategy_allocations: dict[str, float],
    positions: list[tuple[str, float, float, float]],  # (symbol, qty, price, value)
    signals: list[tuple[str, str, str]],  # (strategy, current, new)
    buys: list[tuple[str, str, float, int]],  # (strategy, ticker, limit_price, share_target)
    prev_closes: dict[str, float],
    cash_reserve: float = 0.0,
    account: str = "Acct",
) -> RebalanceState:
    """Build a minimal single-account RebalanceState for recompute_buys."""
    acct = AccountInput(
        name=account,
        type="retirement",
        cash_reserve=cash_reserve,
        positions=[
            PositionInput(symbol=s, quantity=q, price=p, value=v)
            for (s, q, p, v) in positions
        ],
        cash_spaxx=next((v for (s, q, p, v) in positions if s == "SPAXX**"), 0.0),
        strategy_allocations=strategy_allocations,
    )
    sigs = [
        SignalInput(account=account, strategy=st, current_ticker=cur, new_ticker=new)
        for (st, cur, new) in signals
    ]
    buy_recs = [
        BuyAllocationRecord(
            account=account,
            strategy=st,
            ticker=tk,
            dollar_target=lim * sh,
            limit_price=lim,
            share_target=sh,
            est_cost=lim * sh,
        )
        for (st, tk, lim, sh) in buys
    ]
    computed = Computed(
        cash_ok={account: True},
        one_share_total={account: 0.0},
        sells=[],
        buy_allocations=buy_recs,
        sell_chunks=[],
        buy_chunks=[],
    )
    return RebalanceState(
        generated_at=datetime.now(tz=timezone.utc),
        generator="engine",
        inputs=Inputs(accounts=[acct], signals=sigs, prev_closes=prev_closes),
        computed=computed,
    )


def _two_trading_state() -> RebalanceState:
    """Two trading strategies (each sells one ETF, buys another), 50/50 split.

    total_pool = 200_000 (EEM 100k + SMH 100k), no deployable cash.
    Initial buy targets are 1000 shares each at $100 (full-estimate plan).
    """
    return _state(
        strategy_allocations={"Alpha": 0.5, "Beta": 0.5},
        positions=[
            ("EEM", 1000, 100.0, 100_000.0),
            ("SMH", 500, 200.0, 100_000.0),
            ("SPAXX**", 0, 1.0, 0.0),
        ],
        signals=[("Alpha", "EEM", "VOO"), ("Beta", "SMH", "QQQ")],
        buys=[("Alpha", "VOO", 100.0, 1000), ("Beta", "QQQ", 100.0, 1000)],
        prev_closes={"VOO": 100.0, "QQQ": 100.0, "EEM": 100.0, "SMH": 200.0},
    )


def test_recompute_full_proceeds_matches_estimate():
    """Realized proceeds == estimate → share targets unchanged (1000 each)."""
    state = _two_trading_state()
    revised = recompute_buys(state, "Acct", 200_000.0)
    by_strat = {r.strategy: r for r in revised}
    assert by_strat["Alpha"].share_target == 1000
    assert by_strat["Beta"].share_target == 1000


def test_recompute_short_proceeds_shrinks_targets():
    """Realized proceeds below estimate → targets shrink and re-minimize drift.

    180_000 / $100 split 50/50 → 900 shares each (down from 1000).
    """
    state = _two_trading_state()
    revised = recompute_buys(state, "Acct", 180_000.0)
    by_strat = {r.strategy: r.share_target for r in revised}
    assert by_strat == {"Alpha": 900, "Beta": 900}
    # Total cost never exceeds the realized pool (hard budget).
    assert sum(r.est_cost for r in revised) <= 180_000.0 + 1e-6


def test_recompute_rebalance_leg_uses_held_value_as_current():
    """A non-trading (rebalance) leg keeps its holding, so deficit = target − held.

    Hold strategy holds $50k of SMH; target is $100k (total_pool 100k @ 100%).
    Deploying $50k cash buys the $50k deficit → 250 shares (NOT 500).
    """
    state = _state(
        strategy_allocations={"Hold": 1.0},
        positions=[
            ("SMH", 250, 200.0, 50_000.0),
            ("SPAXX**", 0, 1.0, 50_000.0),
        ],
        signals=[("Hold", "SMH", "SMH")],  # new == current → not trading
        buys=[("Hold", "SMH", 200.0, 250)],
        prev_closes={"SMH": 200.0},
    )
    revised = recompute_buys(state, "Acct", 0.0)  # no sells; deploy cash only
    assert len(revised) == 1
    assert revised[0].strategy == "Hold"
    assert revised[0].share_target == 250


def test_recompute_skips_leg_already_at_target():
    """A rebalance leg already at/above its target has deficit ≤ 0 → dropped."""
    state = _state(
        strategy_allocations={"Hold": 1.0},
        positions=[
            ("SMH", 500, 200.0, 100_000.0),  # already == target
            ("SPAXX**", 0, 1.0, 0.0),
        ],
        signals=[("Hold", "SMH", "SMH")],
        buys=[("Hold", "SMH", 200.0, 0)],
        prev_closes={"SMH": 200.0},
    )
    assert recompute_buys(state, "Acct", 0.0) == []


def test_recompute_unknown_account_returns_empty():
    state = _two_trading_state()
    assert recompute_buys(state, "Nonexistent", 100_000.0) == []
