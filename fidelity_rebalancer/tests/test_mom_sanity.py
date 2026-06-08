"""
Regression tests for the D-5 month-over-month sanity diff
(cli.compute._check_mom_sanity_diff).

The check compares the current plan's per-strategy dollar allocation share
against the most recent sibling plan/state JSON in the same directory and warns
on stderr when any strategy's share moves more than 5 percentage points — a
guard against an accidental signal/allocation flip between runs.

The current state is duck-typed (the function only reads
``computed.sells[*].{strategy,est_proceeds}`` and
``computed.buy_allocations[*].{strategy,target_value}``); the previous plan is
plain JSON written to a temp dir.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

_FR_DIR = Path(__file__).resolve().parent.parent  # .../fidelity_rebalancer
if str(_FR_DIR) not in sys.path:
    sys.path.insert(0, str(_FR_DIR))

from cli.compute import _check_mom_sanity_diff  # noqa: E402


def _current_state(sells, buys) -> SimpleNamespace:
    """sells/buys are [(strategy, dollar_value), ...]."""
    return SimpleNamespace(
        computed=SimpleNamespace(
            sells=[SimpleNamespace(strategy=s, est_proceeds=v) for s, v in sells],
            buy_allocations=[
                SimpleNamespace(strategy=s, target_value=v) for s, v in buys
            ],
        )
    )


def _write_prev_plan(plans_dir: Path, sells, buys) -> Path:
    data = {
        "computed": {
            "sells": [{"strategy": s, "est_proceeds": v} for s, v in sells],
            "buy_allocations": [{"strategy": s, "target_value": v} for s, v in buys],
        }
    }
    p = plans_dir / "state_prev.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_mom_warns_on_large_allocation_shift(tmp_path, capsys) -> None:
    # Previous month: MOMENTUM 50% / ROTATION 50%.
    _write_prev_plan(tmp_path, sells=[("MOMENTUM", 5000.0)], buys=[("ROTATION", 5000.0)])
    # This month: MOMENTUM ~9% / ROTATION ~91% — a >5pt swing.
    state = _current_state(sells=[("MOMENTUM", 1000.0)], buys=[("ROTATION", 10000.0)])

    _check_mom_sanity_diff(state, tmp_path / "state_new.json")

    err = capsys.readouterr().err
    assert "MoM Sanity Diff" in err
    assert "MOMENTUM" in err or "ROTATION" in err


def test_mom_silent_within_tolerance(tmp_path, capsys) -> None:
    _write_prev_plan(tmp_path, sells=[("MOMENTUM", 5000.0)], buys=[("ROTATION", 5000.0)])
    # ~1pt swing — under the 5pt threshold, so no warning.
    state = _current_state(sells=[("MOMENTUM", 5100.0)], buys=[("ROTATION", 4900.0)])

    _check_mom_sanity_diff(state, tmp_path / "state_new.json")

    err = capsys.readouterr().err
    assert "MoM Sanity Diff" not in err


def test_mom_no_prior_plan_is_silent(tmp_path, capsys) -> None:
    # No sibling plan/state file at all -> nothing to compare, no warning.
    state = _current_state(sells=[("MOMENTUM", 5000.0)], buys=[("ROTATION", 5000.0)])

    _check_mom_sanity_diff(state, tmp_path / "state_new.json")

    err = capsys.readouterr().err
    assert "MoM Sanity Diff" not in err
