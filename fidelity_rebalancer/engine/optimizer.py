"""
Port of the React calculator's liveBuys drift-minimizing allocator.
Two-phase: proportional floor, then greedy one-share assignment.
Pure function — no I/O.
"""
from __future__ import annotations

import math


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
    return active
