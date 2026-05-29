"""
Pre-trade config validator.

Checks accounts.json and (optionally) signals.json for common configuration
errors before running the engine.

Usage (from repo root):
    python scripts/validate_config.py
    python scripts/validate_config.py --accounts fidelity_rebalancer/accounts.json
    python scripts/validate_config.py --accounts fidelity_rebalancer/accounts.json --signals signals.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console

console = Console(highlight=False)

_ALLOC_TOL = 0.001  # allowable rounding error on strategy allocations


def _ok(msg: str) -> None:
    console.print(f"[green]OK[/green]  {msg}")


def _fail(msg: str) -> None:
    console.print(f"[red]FAIL[/red] {msg}")


def _warn(msg: str) -> None:
    console.print(f"[yellow]WARN[/yellow] {msg}")


def validate_accounts(accounts: dict) -> list[str]:
    """Return a list of failure messages (empty = all passed)."""
    failures: list[str] = []

    for acct_name, acct in accounts.items():
        strategies: dict = acct.get("strategies", {})

        # Check 1 — allocations sum to 1.0 ± tolerance
        total = sum(strategies.values())
        if abs(total - 1.0) > _ALLOC_TOL:
            failures.append(
                f"{acct_name}: strategy allocations sum to {total:.6f} "
                f"(expected 1.0 ± {_ALLOC_TOL})"
            )

        # Check 2 — no duplicate strategy names (dict keys are unique by
        # definition in JSON, but catch case-insensitive duplicates)
        lower_names: list[str] = [s.lower() for s in strategies]
        seen: set[str] = set()
        for name in lower_names:
            if name in seen:
                failures.append(
                    f"{acct_name}: duplicate strategy name (case-insensitive): {name!r}"
                )
            seen.add(name)

    return failures


def validate_signals(signals_data: dict, accounts: dict) -> list[str]:
    """Validate signals.json against accounts.json. Return failure messages."""
    failures: list[str] = []
    signals: dict = signals_data.get("signals", {})

    all_account_strategies: set[str] = set()
    for acct in accounts.values():
        all_account_strategies.update(acct.get("strategies", {}).keys())

    for strategy, tickers in signals.items():
        current = (tickers.get("current") or "").strip()
        new = (tickers.get("new") or "").strip()

        # Check 3 — both tickers populated
        if not current:
            failures.append(f"signals[{strategy!r}]: 'current' ticker is empty")
        if not new:
            failures.append(f"signals[{strategy!r}]: 'new' ticker is empty")

    # Check 4 — every strategy in signals exists in at least one account
    for strategy in signals:
        if strategy not in all_account_strategies:
            failures.append(
                f"signals strategy {strategy!r} not found in any account's "
                f"strategy list (typo in STRATEGY_MAP or accounts.json?)"
            )

    # Soft check — strategies in accounts not present in signals (warn only)
    missing_from_signals = all_account_strategies - set(signals.keys())
    for strategy in sorted(missing_from_signals):
        _warn(
            f"Account strategy {strategy!r} has no signal in signals.json "
            f"(expected if SectorSurfer didn't return it this month)"
        )

    return failures


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate accounts.json and signals.json before trading"
    )
    parser.add_argument(
        "--accounts",
        default="fidelity_rebalancer/accounts.json",
        help="Path to accounts.json (default: fidelity_rebalancer/accounts.json)",
    )
    parser.add_argument(
        "--signals",
        default=None,
        help="Path to signals.json (optional; skips signal checks if omitted)",
    )
    args = parser.parse_args()

    accounts_path = Path(args.accounts)
    if not accounts_path.exists():
        console.print(f"[red]ERROR[/red] accounts file not found: {accounts_path}")
        sys.exit(1)

    accounts: dict = json.loads(accounts_path.read_text(encoding="utf-8"))
    console.print(
        f"\nValidating [bold]{accounts_path}[/bold] ({len(accounts)} account(s))..."
    )

    acct_failures = validate_accounts(accounts)

    if acct_failures:
        for f in acct_failures:
            _fail(f)
    else:
        _ok(f"All {len(accounts)} account(s): allocations sum to 1.0, no duplicates")

    signals_failures: list[str] = []
    if args.signals:
        signals_path = Path(args.signals)
        if not signals_path.exists():
            console.print(f"[red]ERROR[/red] signals file not found: {signals_path}")
            sys.exit(1)

        signals_data: dict = json.loads(signals_path.read_text(encoding="utf-8"))
        n_signals = len(signals_data.get("signals", {}))
        console.print(
            f"\nValidating [bold]{signals_path}[/bold] ({n_signals} signal(s))..."
        )

        signals_failures = validate_signals(signals_data, accounts)

        if signals_failures:
            for f in signals_failures:
                _fail(f)
        else:
            _ok(f"All {n_signals} signal(s): tickers populated and matched to accounts")

    console.print()
    total_failures = len(acct_failures) + len(signals_failures)
    if total_failures == 0:
        console.print("[bold green]All checks passed[/bold green]")
        sys.exit(0)
    else:
        console.print(f"[bold red]{total_failures} check(s) failed[/bold red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
