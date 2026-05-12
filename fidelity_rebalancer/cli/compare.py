"""
Compare engine state JSON against a calc export JSON.

Usage (from SecSurfTrade/ or fidelity_rebalancer/):
    python -m fidelity_rebalancer.cli.compare --engine engine_state.json --calc calc_export.json
    python -m cli.compare --engine engine_state.json --calc calc_export.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from rich.console import Console

from cli import resolve_path
from state.compare import compare_states
from state.importer import load_state

console = Console(highlight=False)


def _checked_fields(state) -> list[str]:
    """Build a flat list of field-path labels that the comparator checks."""
    fields = []
    fields += [f"cash_ok.{a}" for a in state.computed.cash_ok]
    fields += [f"one_share_total.{a}" for a in state.computed.one_share_total]
    fields += [
        f"sells[{s.account}/{s.strategy}/{s.ticker}]"
        for s in state.computed.sells
    ]
    fields += [
        f"buy_allocations[{b.account}/{b.strategy}/{b.ticker}]"
        for b in state.computed.buy_allocations
    ]
    fields += [
        f"sell_chunks[{c.account}/{c.strategy}/{c.ticker}#{c.idx}]"
        for c in state.computed.sell_chunks
    ]
    fields += [
        f"buy_chunks[{c.account}/{c.strategy}/{c.ticker}#{c.idx}]"
        for c in state.computed.buy_chunks
    ]
    return fields


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare engine state vs React calc export")
    parser.add_argument("--engine", required=True, help="Path to engine state JSON")
    parser.add_argument("--calc", required=True, help="Path to calc export JSON")
    args = parser.parse_args()

    engine_state = load_state(resolve_path(args.engine))
    calc_state = load_state(resolve_path(args.calc))

    diffs = compare_states(engine_state, calc_state)
    diff_paths = {d.path for d in diffs}

    checked = _checked_fields(engine_state)
    for field_label in checked:
        # A field label matches diffs whose path starts with the label
        field_diffs = [d for d in diffs if d.path == field_label or d.path.startswith(field_label + ".")]
        if not field_diffs:
            console.print(f"[green]OK[/green] {field_label} match")
        else:
            for d in field_diffs:
                if d.abs_diff is not None:
                    console.print(
                        f"[red]DIFF[/red] {d.path}: "
                        f"engine={d.engine_val} calc={d.calc_val} "
                        f"(|Δ|={d.abs_diff:.6g})"
                    )
                else:
                    console.print(
                        f"[red]DIFF[/red] {d.path}: engine={d.engine_val} calc={d.calc_val}"
                    )

    console.print()
    if not diffs:
        console.print("[bold green]All fields match[/bold green]")
        sys.exit(0)
    else:
        console.print(f"[bold red]{len(diffs)} diff(s) found[/bold red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
