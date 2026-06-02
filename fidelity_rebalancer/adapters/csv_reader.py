"""
Fidelity CSV → AccountPortfolio adapter.
File I/O lives here; the parsing logic is in engine.calculator.
"""

from __future__ import annotations

from pathlib import Path

from engine.calculator import consolidate, parse_csv
from state.schema import AccountPortfolio, Position


def read_fidelity_csv(path: str | Path) -> AccountPortfolio:
    """Read a Fidelity positions CSV and return a validated AccountPortfolio."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = Path(path).read_text(encoding="cp1252")
    rows = parse_csv(text)
    raw = consolidate(rows)
    return AccountPortfolio(
        account_name=raw["account_name"],
        positions={
            sym: Position(
                symbol=sym,
                quantity=data["quantity"],
                value=data["value"],
                price=data["price"],
            )
            for sym, data in raw["positions"].items()
        },
        pending_activity=raw.get("pending_activity", 0.0),
    )
