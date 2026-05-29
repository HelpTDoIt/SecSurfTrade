"""
Stall-detection rehearsal — exercises the stall engine and monitor
rendering before Monday's trading window. No real ATP connection required.

Tests:
  1. detect_stalls() correctly identifies a PartiallyFilled order that has
     not progressed past stall_threshold_seconds (300s).
  2. recommend_requote() produces the correct new limit for both sell and
     buy sides, matching the architecture doc rules:
       sell: new_limit = max(bid + 1tick, orig - 5ticks)
       buy:  new_limit = min(ask - 1tick, orig + 5ticks)
  3. MonitorApp._render_stalls() produces a non-empty text panel containing
     the stall ticker, chunk id, and suggested re-quote price.

Usage (from repo root):
    python scripts/stall_rehearsal.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# make engine/adapters/tui importable from repo root
_REBALANCER = Path(__file__).resolve().parent.parent / "fidelity_rebalancer"
sys.path.insert(0, str(_REBALANCER))

from rich.console import Console

from adapters import OrderRow, OrderStatus, QuoteSnapshot
from adapters.mock_atp import MockATP
from engine.stall import StallEvent, RequoteSuggestion, detect_stalls, recommend_requote
from engine.chunker import tick
from state.schema import (
    AccountInput,
    BuyAllocationRecord,
    ChunkRecord,
    Computed,
    EngineConfig,
    Inputs,
    PlanOutput,
    RebalanceState,
    SellRecord,
    SignalInput,
)

console = Console(highlight=False)

_PASS = 0
_FAIL = 0
THRESHOLD = 300  # default stall_threshold_seconds


def _ok(msg: str) -> None:
    global _PASS
    _PASS += 1
    console.print(f"[green]OK[/green]  {msg}")


def _fail(msg: str) -> None:
    global _FAIL
    _FAIL += 1
    console.print(f"[red]FAIL[/red] {msg}")


def _check(label: str, actual, expected, tol: float | None = None) -> None:
    if tol is not None:
        ok = abs(actual - expected) <= tol
    else:
        ok = actual == expected
    if ok:
        _ok(f"{label}: {actual!r}")
    else:
        _fail(f"{label}: got {actual!r}, expected {expected!r}")


# ── Minimal PlanOutput fixture ────────────────────────────────────────────────


def _make_plan() -> PlanOutput:
    """Build the smallest valid PlanOutput that exercises the monitor path."""
    now = datetime.now(tz=timezone.utc)

    sell_chunk = ChunkRecord(
        chunk_id="s1",
        account="Test Account A",
        strategy="Strategy A",
        ticker="EEM",
        idx=0,
        shares=800,
        limit_price=62.50,
        cost=50_000,
    )
    buy_chunk = ChunkRecord(
        chunk_id="b1",
        account="Test Account A",
        strategy="Strategy A",
        ticker="EWY",
        idx=0,
        shares=700,
        limit_price=44.50,
        cost=31_150,
    )
    state = RebalanceState(
        generated_at=now,
        generator="engine",
        inputs=Inputs(
            accounts=[
                AccountInput(
                    name="Test Account A",
                    type="retirement",
                    positions=[],
                    cash_spaxx=100.0,
                    strategy_allocations={"Strategy A": 1.0},
                )
            ],
            signals=[
                SignalInput(
                    account="Test Account A",
                    strategy="Strategy A",
                    current_ticker="EEM",
                    new_ticker="EWY",
                )
            ],
            config=EngineConfig(stall_threshold_seconds=THRESHOLD),
        ),
        computed=Computed(
            cash_ok={"Test Account A": True},
            one_share_total={"Test Account A": 107.0},
            sells=[
                SellRecord(
                    account="Test Account A",
                    strategy="Strategy A",
                    ticker="EEM",
                    shares=800,
                    limit_price=62.50,
                    est_proceeds=50_000,
                )
            ],
            buy_allocations=[
                BuyAllocationRecord(
                    account="Test Account A",
                    strategy="Strategy A",
                    ticker="EWY",
                    dollar_target=31_150,
                    limit_price=44.50,
                    share_target=700,
                    est_cost=31_150,
                )
            ],
            sell_chunks=[sell_chunk],
            buy_chunks=[buy_chunk],
        ),
    )
    return PlanOutput(generated_at=now, state=state)


# ── Test 1: stall detection ───────────────────────────────────────────────────

console.print("\n[bold]== Test 1: Stall detection ==\n[/bold]")

fill_time = datetime(2026, 6, 1, 9, 35, 0, tzinfo=timezone.utc)
now_time = fill_time + timedelta(seconds=400)  # 400s > 300s threshold

mock = MockATP()
mock.set_quote("EEM", bid=62.00, ask=62.02, last=62.01, prev_close=62.50)
mock.set_quote("EWY", bid=44.48, ask=44.52, last=44.50, prev_close=44.50)

sell_row = OrderRow(
    account="Test Account A",
    symbol="EEM",
    side="SELL",
    qty=800,
    filled_qty=400,
    limit_price=62.50,
    status=OrderStatus.PartiallyFilled,
    placed_at=fill_time,
    last_update_at=fill_time,
    order_id="s1",
)
mock.add_order(sell_row)

stalls = detect_stalls(mock.get_orders(), THRESHOLD, now_time)
_check("stall count", len(stalls), 1)
if stalls:
    s = stalls[0]
    _check("stall.chunk_id", s.chunk_id, "s1")
    _check("stall.filled_qty", s.filled_qty, 400.0)
    _check("stall.remaining_qty", s.remaining_qty, 400.0)
    _ok(f"stall detected at t={s.seconds_stalled:.0f}s (threshold={THRESHOLD}s)")

# Control: order that hasn't stalled yet (only 200s elapsed)
mock.clear_orders()
fresh_row = OrderRow(
    account="Test Account A",
    symbol="EEM",
    side="SELL",
    qty=800,
    filled_qty=400,
    limit_price=62.50,
    status=OrderStatus.PartiallyFilled,
    placed_at=fill_time,
    last_update_at=fill_time + timedelta(seconds=200),
    order_id="s1",
)
mock.add_order(fresh_row)
fresh_stalls = detect_stalls(mock.get_orders(), THRESHOLD, now_time)
_check("no stall when elapsed < threshold (200s)", len(fresh_stalls), 0)

# ── Test 2: re-quote suggestions ──────────────────────────────────────────────

console.print("\n[bold]== Test 2: Re-quote suggestions ==\n[/bold]")

stall = StallEvent(
    chunk_id="s1",
    original_limit=62.50,
    filled_qty=400,
    remaining_qty=400,
    seconds_stalled=400,
)

# Sell side: bid=62.00, orig=62.50
# candidate_bid_plus = 62.00 + 0.01 = 62.01
# candidate_chase    = 62.50 - 5*0.01 = 62.45
# new_limit = max(62.01, 62.45) = 62.45
sell_quote = mock.get_quote("EEM")  # bid=62.00
sell_sugg = recommend_requote(stall, "sell", sell_quote)
_check("sell re-quote new_limit", sell_sugg.new_limit, 62.45, tol=1e-9)
_ok(f"sell rationale: {sell_sugg.rationale[-1]}")

buy_stall = StallEvent(
    chunk_id="b1",
    original_limit=44.50,
    filled_qty=0,
    remaining_qty=700,
    seconds_stalled=350,
)
# Buy side: ask=44.52, orig=44.50
# candidate_ask_minus = 44.52 - 0.01 = 44.51
# candidate_chase     = 44.50 + 5*0.01 = 44.55
# new_limit = min(44.51, 44.55) = 44.51
buy_quote = mock.get_quote("EWY")  # ask=44.52
buy_sugg = recommend_requote(buy_stall, "buy", buy_quote)
_check("buy re-quote new_limit", buy_sugg.new_limit, 44.51, tol=1e-9)
_ok(f"buy rationale: {buy_sugg.rationale[-1]}")

# ── Test 3: monitor _render_stalls() ─────────────────────────────────────────

console.print("\n[bold]== Test 3: Monitor render ==\n[/bold]")

# Build a minimal MonitorApp by bypassing __init__ and setting only the
# attributes needed by _render_stalls().
from tui.monitor import MonitorApp

plan = _make_plan()
app = object.__new__(MonitorApp)
# Set the attributes _render_stalls relies on
app._stalls = [stall]
app._suggestions = [sell_sugg]
app._ticker_for_chunk = {"s1": "EEM", "b1": "EWY"}

rendered = app._render_stalls()

if rendered:
    lines = [l for l in rendered.split("\n") if l.strip()]
    _ok(f"monitor panel rendered ({len(lines)} non-blank lines)")
    _check("panel contains ticker", "EEM" in rendered, True)
    _check(
        "panel contains suggested price", f"{sell_sugg.new_limit:.4f}" in rendered, True
    )
    # Echo the rendered panel (Rich markup stripped) for visual confirmation
    console.print("\n[dim]-- Rendered stall panel (raw markup) --[/dim]")
    sys.stdout.buffer.write(rendered.encode("utf-8", "replace") + b"\n")
    console.print("[dim]------------------------------------------[/dim]")
else:
    _fail("_render_stalls() returned empty string")

# ── Summary ───────────────────────────────────────────────────────────────────

console.print()
if _FAIL == 0:
    console.print(f"[bold green]All {_PASS} checks passed[/bold green]")
    sys.exit(0)
else:
    console.print(f"[bold red]{_FAIL} check(s) failed[/bold red] ({_PASS} passed)")
    sys.exit(1)
