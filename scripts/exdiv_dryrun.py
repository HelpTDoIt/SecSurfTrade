"""
Ex-dividend dry-run for the upcoming 1st-of-month trading window.

Tests the engine-level adjust_prev_close_for_exdiv() function with a
synthetic 2026-06-01 calendar (mirroring Monday's date) and against the
committed test fixture (2026-05-01 SPY/QQQ entries).

The FT+ Watchlist "Div Ex-Date" source (atp_watchlist.py) is exercised
separately by the live T1 smoke run — this script targets only the pure
engine function.

Usage (from repo root):
    python scripts/exdiv_dryrun.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# make engine importable from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fidelity_rebalancer"))

from engine.chunker import adjust_prev_close_for_exdiv
from rich.console import Console

console = Console(highlight=False)

_PASS = 0
_FAIL = 0


def _ok(msg: str) -> None:
    global _PASS
    _PASS += 1
    console.print(f"[green]OK[/green]  {msg}")


def _fail(msg: str) -> None:
    global _FAIL
    _FAIL += 1
    console.print(f"[red]FAIL[/red] {msg}")


def _check(label: str, actual: float, expected: float, tol: float = 1e-9) -> None:
    if abs(actual - expected) <= tol:
        _ok(f"{label}: {actual:.4f} == {expected:.4f}")
    else:
        _fail(f"{label}: got {actual:.4f}, expected {expected:.4f}")


# ── Synthetic 2026-06-01 calendar ────────────────────────────────────────────

SYNTHETIC_CALENDAR = {
    "SPY": {"2026-06-01": 1.50},
    "JMAC": {"2026-06-01": 0.20},
}

console.print("\n[bold]== Synthetic 2026-06-01 calendar (Monday) ==\n[/bold]")

# Case 1 — substitution fires on 2026-06-01
for sym, div in [("SPY", 1.50), ("JMAC", 0.20)]:
    prev = 500.00 if sym == "SPY" else 25.00
    result = adjust_prev_close_for_exdiv(
        sym, prev, date(2026, 6, 1), calendar=SYNTHETIC_CALENDAR
    )
    _check(f"ex-div fires for {sym} on 2026-06-01 (synthetic)", result, prev - div)

# Case 2 — today (2026-05-29): not the 1st, no substitution
for sym, prev in [("SPY", 500.00), ("JMAC", 25.00)]:
    result = adjust_prev_close_for_exdiv(
        sym, prev, date(2026, 5, 29), calendar=SYNTHETIC_CALENDAR
    )
    _check(f"no substitution for {sym} on 2026-05-29 (not 1st)", result, prev)

# Case 3 — 2026-06-02: past the 1st, no substitution
for sym, prev in [("SPY", 500.00), ("JMAC", 25.00)]:
    result = adjust_prev_close_for_exdiv(
        sym, prev, date(2026, 6, 2), calendar=SYNTHETIC_CALENDAR
    )
    _check(f"no substitution for {sym} on 2026-06-02 (not 1st)", result, prev)

# Case 4 — symbol absent from synthetic calendar on 2026-06-01
result = adjust_prev_close_for_exdiv(
    "EEM", 60.00, date(2026, 6, 1), calendar=SYNTHETIC_CALENDAR
)
_check("no substitution for unlisted symbol EEM on 2026-06-01", result, 60.00)

# ── Real fixture (2026-05-01 SPY/QQQ) ────────────────────────────────────────

console.print(
    "\n[bold]== Real fixture (tests/fixtures/exdiv_calendar.json) ==\n[/bold]"
)

# calendar=None → function loads the committed fixture automatically
result = adjust_prev_close_for_exdiv("SPY", 500.00, date(2026, 5, 1), calendar=None)
_check("ex-div fires for SPY on 2026-05-01 (real fixture, $1.50)", result, 498.50)

result = adjust_prev_close_for_exdiv("QQQ", 400.00, date(2026, 5, 1), calendar=None)
_check("ex-div fires for QQQ on 2026-05-01 (real fixture, $0.65)", result, 399.35)

result = adjust_prev_close_for_exdiv("VTI", 250.00, date(2026, 5, 1), calendar=None)
_check("no substitution for VTI on 2026-05-01 (wrong date in fixture)", result, 250.00)

# ── Summary ───────────────────────────────────────────────────────────────────

console.print()
if _FAIL == 0:
    console.print(f"[bold green]All {_PASS} checks passed[/bold green]")
    sys.exit(0)
else:
    console.print(f"[bold red]{_FAIL} check(s) failed[/bold red] ({_PASS} passed)")
    sys.exit(1)
