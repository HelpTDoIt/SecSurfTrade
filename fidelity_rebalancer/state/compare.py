"""
Structural diff of two RebalanceState objects, scoped to the `computed` block.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .schema import RebalanceState

FLOAT_TOL = 1e-6


@dataclass
class Diff:
    path: str
    engine_val: Any
    calc_val: Any
    abs_diff: float | None = None
    rel_diff: float | None = None


def _fdiff(a: float, b: float) -> tuple[float, float]:
    ad = abs(a - b)
    rd = ad / max(abs(b), 1e-12)
    return ad, rd


def compare_states(engine: RebalanceState, calc: RebalanceState) -> list[Diff]:
    """
    Compare the `computed` blocks of two RebalanceState objects.
    Returns a list of Diff records — empty means all fields match.

    Matching strategy:
    - cash_ok / one_share_total: keyed by account name
    - sells / buy_allocations: keyed by (account, strategy, ticker)
    - sell_chunks / buy_chunks: keyed by (account, strategy, ticker, idx)
      so chunk ordering differences don't cause false failures
    """
    diffs: list[Diff] = []

    # ── cash_ok ────────────────────────────────────────────────────────────
    for acct, e_val in engine.computed.cash_ok.items():
        c_val = calc.computed.cash_ok.get(acct)
        if c_val is None:
            diffs.append(Diff(f"cash_ok.{acct}", e_val, "<missing>"))
        elif e_val != c_val:
            diffs.append(Diff(f"cash_ok.{acct}", e_val, c_val))

    # ── one_share_total ────────────────────────────────────────────────────
    for acct, e_val in engine.computed.one_share_total.items():
        c_val = calc.computed.one_share_total.get(acct)
        if c_val is None:
            diffs.append(Diff(f"one_share_total.{acct}", e_val, "<missing>"))
        elif abs(e_val - c_val) >= FLOAT_TOL:
            ad, rd = _fdiff(e_val, c_val)
            diffs.append(Diff(f"one_share_total.{acct}", e_val, c_val, ad, rd))

    # ── sells ──────────────────────────────────────────────────────────────
    e_sells = {(s.account, s.strategy, s.ticker): s for s in engine.computed.sells}
    c_sells = {(s.account, s.strategy, s.ticker): s for s in calc.computed.sells}
    for key, e_s in e_sells.items():
        acct, strat, tkr = key
        base = f"sells[{acct}/{strat}/{tkr}]"
        c_s = c_sells.get(key)
        if c_s is None:
            diffs.append(Diff(base, "present", "<missing>"))
            continue
        if e_s.shares != c_s.shares:
            diffs.append(Diff(f"{base}.shares", e_s.shares, c_s.shares))
        if abs(e_s.limit_price - c_s.limit_price) >= FLOAT_TOL:
            ad, rd = _fdiff(e_s.limit_price, c_s.limit_price)
            diffs.append(Diff(f"{base}.limit_price", e_s.limit_price, c_s.limit_price, ad, rd))
        if abs(e_s.est_proceeds - c_s.est_proceeds) >= FLOAT_TOL:
            ad, rd = _fdiff(e_s.est_proceeds, c_s.est_proceeds)
            diffs.append(Diff(f"{base}.est_proceeds", e_s.est_proceeds, c_s.est_proceeds, ad, rd))

    # ── buy_allocations ────────────────────────────────────────────────────
    e_buys = {(b.account, b.strategy, b.ticker): b for b in engine.computed.buy_allocations}
    c_buys = {(b.account, b.strategy, b.ticker): b for b in calc.computed.buy_allocations}
    for key, e_b in e_buys.items():
        acct, strat, tkr = key
        base = f"buy_allocations[{acct}/{strat}/{tkr}]"
        c_b = c_buys.get(key)
        if c_b is None:
            diffs.append(Diff(base, "present", "<missing>"))
            continue
        if abs(e_b.dollar_target - c_b.dollar_target) >= FLOAT_TOL:
            ad, rd = _fdiff(e_b.dollar_target, c_b.dollar_target)
            diffs.append(Diff(f"{base}.dollar_target", e_b.dollar_target, c_b.dollar_target, ad, rd))
        if e_b.share_target != c_b.share_target:
            diffs.append(Diff(f"{base}.share_target", e_b.share_target, c_b.share_target))
        if abs(e_b.est_cost - c_b.est_cost) >= FLOAT_TOL:
            ad, rd = _fdiff(e_b.est_cost, c_b.est_cost)
            diffs.append(Diff(f"{base}.est_cost", e_b.est_cost, c_b.est_cost, ad, rd))

    # ── sell_chunks — match by (account, strategy, ticker, idx) ───────────
    e_sc = {(c.account, c.strategy, c.ticker, c.idx): c for c in engine.computed.sell_chunks}
    c_sc = {(c.account, c.strategy, c.ticker, c.idx): c for c in calc.computed.sell_chunks}
    for key, e_c in e_sc.items():
        acct, strat, tkr, idx = key
        base = f"sell_chunks[{acct}/{strat}/{tkr}#{idx}]"
        c_c = c_sc.get(key)
        if c_c is None:
            diffs.append(Diff(base, "present", "<missing>"))
            continue
        if e_c.shares != c_c.shares:
            diffs.append(Diff(f"{base}.shares", e_c.shares, c_c.shares))
        if abs(e_c.limit_price - c_c.limit_price) >= FLOAT_TOL:
            ad, rd = _fdiff(e_c.limit_price, c_c.limit_price)
            diffs.append(Diff(f"{base}.limit_price", e_c.limit_price, c_c.limit_price, ad, rd))
        if abs(e_c.cost - c_c.cost) >= FLOAT_TOL:
            ad, rd = _fdiff(e_c.cost, c_c.cost)
            diffs.append(Diff(f"{base}.cost", e_c.cost, c_c.cost, ad, rd))

    # ── buy_chunks — match by (account, strategy, ticker, idx) ────────────
    e_bc = {(c.account, c.strategy, c.ticker, c.idx): c for c in engine.computed.buy_chunks}
    c_bc = {(c.account, c.strategy, c.ticker, c.idx): c for c in calc.computed.buy_chunks}
    for key, e_c in e_bc.items():
        acct, strat, tkr, idx = key
        base = f"buy_chunks[{acct}/{strat}/{tkr}#{idx}]"
        c_c = c_bc.get(key)
        if c_c is None:
            diffs.append(Diff(base, "present", "<missing>"))
            continue
        if e_c.shares != c_c.shares:
            diffs.append(Diff(f"{base}.shares", e_c.shares, c_c.shares))
        if abs(e_c.limit_price - c_c.limit_price) >= FLOAT_TOL:
            ad, rd = _fdiff(e_c.limit_price, c_c.limit_price)
            diffs.append(Diff(f"{base}.limit_price", e_c.limit_price, c_c.limit_price, ad, rd))
        if abs(e_c.cost - c_c.cost) >= FLOAT_TOL:
            ad, rd = _fdiff(e_c.cost, c_c.cost)
            diffs.append(Diff(f"{base}.cost", e_c.cost, c_c.cost, ad, rd))

    return diffs
