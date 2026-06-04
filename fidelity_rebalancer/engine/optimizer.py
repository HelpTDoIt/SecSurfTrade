"""
Port of the React calculator's liveBuys drift-minimizing allocator.
Two-phase: proportional floor, then greedy one-share assignment.
Pure function — no I/O.

Also hosts ``recompute_buys`` (F-1): the live-monitor entry point that re-runs
``live_buys`` against an account's *realized* sell proceeds and returns revised
buy allocations.  Still pure — it computes a plan; it never places orders.
"""
from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from engine.calculator import calc_trades
from state.schema import BuyAllocationRecord

if TYPE_CHECKING:
    from state.schema import RebalanceState

_log = logging.getLogger(__name__)


def live_buys(
    candidates: list[dict],
    actual_avail: float,
    total_pool: float,
    strategies: dict[str, float],
) -> list[dict]:
    """
    Port of JS liveBuys optimizer.

    Each candidate dict must contain:
        strategy   str    strategy name (key into `strategies`)
        ticker     str
        limit_price  float
        deficit    float  target_val - current_val
        current_val  float  current position value (0 for trading strategies)
        is_rebalance bool
        target_val   float  strategies[s] * total_pool

    candidates are mutated in-place (shares, est_cost updated).
    Returns the final list with dollarTarget added, filtered to shares > 0.
    """
    if not candidates or actual_avail <= 0:
        return []

    # ── Phase 1: proportional floor ────────────────────────────────────────
    total_deficit = sum(c["deficit"] for c in candidates)
    for c in candidates:
        prop_budget = (
            (c["deficit"] / total_deficit) * actual_avail
            if total_deficit > 0
            else actual_avail / len(candidates)
        )
        c["shares"] = math.floor(min(prop_budget, c["deficit"]) / c["limit_price"])
        c["est_cost"] = c["shares"] * c["limit_price"]

    # ── Phase 2: greedy one-share to maximally reduce drift ────────────────
    budget_left = actual_avail - sum(c["est_cost"] for c in candidates)
    safety = 0
    while budget_left > 0 and safety < 500:
        safety += 1
        best_idx = -1
        best_reduction = float("-inf")
        for i, c in enumerate(candidates):
            if c["limit_price"] > budget_left:
                continue
            # Do not over-allocate beyond deficit + 0.5 share tolerance
            if c["est_cost"] + c["limit_price"] > c["deficit"] + c["limit_price"] * 0.5:
                continue
            cur_drift = abs(
                (c["current_val"] + c["est_cost"]) / total_pool
                - strategies[c["strategy"]]
            )
            new_drift = abs(
                (c["current_val"] + c["est_cost"] + c["limit_price"]) / total_pool
                - strategies[c["strategy"]]
            )
            reduction = cur_drift - new_drift
            if reduction > best_reduction:
                best_reduction = reduction
                best_idx = i
        if best_idx < 0 or best_reduction <= 0:
            break
        candidates[best_idx]["shares"] += 1
        candidates[best_idx]["est_cost"] = (
            candidates[best_idx]["shares"] * candidates[best_idx]["limit_price"]
        )
        budget_left = actual_avail - sum(c["est_cost"] for c in candidates)

    # ── Build final output ──────────────────────────────────────────────────
    active = [c for c in candidates if c["shares"] > 0]
    total_est_cost = sum(c["est_cost"] for c in active)
    for c in active:
        c["dollar_target"] = (
            (c["est_cost"] / total_est_cost) * actual_avail
            if total_est_cost > 0
            else 0.0
        )
    _log.debug(
        "live_buys: %d candidate(s) -> %d funded leg(s), spend=%.2f of avail=%.2f",
        len(candidates),
        len(active),
        total_est_cost,
        actual_avail,
    )
    return active


def recompute_buys(
    state: "RebalanceState",
    account: str,
    actual_proceeds: float,
) -> list[BuyAllocationRecord]:
    """Re-run the drift allocator for one account against its *realized* pool.

    Called by the live monitor (F-1) once an account's sells reach a terminal
    state — or the EOD-sweep clock fires — so the realized sell proceeds are
    known.  Reconstructs the same total_pool / deployable-cash / per-strategy
    snapshot the initial compute used (via the pure ``calc_trades``), rebuilds
    ``live_buys`` candidates from the stored buy allocations, and re-minimizes
    drift against ``actual_proceeds + deployable_cash``.

    Returns a fresh list of ``BuyAllocationRecord`` (active legs only,
    share_target > 0).  Pure: no chunking, no I/O, no order placement — the
    caller decides what to do with the revised allocations (update state +
    journal).  Realized proceeds below the estimate shrink the available pool,
    so share targets fall and drift is re-minimized over the smaller budget.
    """
    acct = next((a for a in state.inputs.accounts if a.name == account), None)
    if acct is None:
        # Generic (no account name) so the default log stays non-sensitive.
        _log.warning("recompute_buys: account not found in state — no allocations")
        return []

    strategies = acct.strategy_allocations
    cfg = {"strategies": strategies, "cashReserve": acct.cash_reserve}
    positions = {
        p.symbol: {
            "symbol": p.symbol,
            "quantity": p.quantity,
            "value": p.value,
            "price": p.price,
        }
        for p in acct.positions
    }
    signals = {
        s.strategy: {"current": s.current_ticker, "new": s.new_ticker}
        for s in state.inputs.signals
        if s.account == account
    }

    snap = calc_trades(
        cfg, positions, signals, state.inputs.prev_closes, acct.pending_activity
    )
    total_pool = snap["total_pool"]
    depl_cash = snap["depl_cash"]
    s_pos = snap["s_pos"]

    # A strategy is "trading" when its new ticker differs from its current one —
    # it sold its old position, so it holds 0 of the ticker it is now buying.
    # A non-trading (rebalance) leg keeps its holding, which counts toward target.
    trading = {
        s
        for s, sig in signals.items()
        if sig.get("new") and sig["new"] != sig.get("current", "")
    }

    candidates: list[dict] = []
    for ba in state.computed.buy_allocations:
        if ba.account != account or ba.limit_price <= 0:
            continue
        weight = strategies.get(ba.strategy, 0.0)
        target_val = weight * total_pool
        current_val = (
            0.0 if ba.strategy in trading else s_pos.get(ba.strategy, {}).get("value", 0.0)
        )
        deficit = target_val - current_val
        if deficit <= 0:
            continue
        candidates.append(
            {
                "strategy": ba.strategy,
                "ticker": ba.ticker,
                "limit_price": ba.limit_price,
                "target_val": target_val,
                "current_val": current_val,
                "deficit": deficit,
                "is_rebalance": ba.is_rebalance,
                "shares": 0,
                "est_cost": 0.0,
            }
        )

    actual_avail = actual_proceeds + depl_cash
    # Account name + dollars are position-revealing -> DEBUG; INFO keeps counts.
    _log.debug(
        "recompute_buys[%s]: proceeds=%.2f depl_cash=%.2f total_pool=%.2f "
        "-> %d candidate leg(s)",
        account,
        actual_proceeds,
        depl_cash,
        total_pool,
        len(candidates),
    )
    active = live_buys(candidates, actual_avail, total_pool, strategies)
    _log.info(
        "recompute_buys: %d candidate(s) -> %d funded leg(s)",
        len(candidates),
        len(active),
    )

    return [
        BuyAllocationRecord(
            account=account,
            strategy=c["strategy"],
            ticker=c["ticker"],
            dollar_target=c["dollar_target"],
            limit_price=c["limit_price"],
            share_target=int(c["shares"]),
            est_cost=c["est_cost"],
            is_rebalance=c["is_rebalance"],
            target_value=c["target_val"],
        )
        for c in active
    ]
