"""
Export OCR-captured fills from the journal to a JSON file consumable by
the rebalance calculator's "Import fills" button.

Usage (from repo root):
    python -m fidelity_rebalancer.cli.export_fills
    python -m fidelity_rebalancer.cli.export_fills --journal logs/journal.jsonl --out fills.json
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export OCR-captured fills from the journal to JSON."
    )
    parser.add_argument(
        "--journal",
        default="logs/journal.jsonl",
        help="Path to the append-only journal JSONL (default: logs/journal.jsonl)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output file path (default: stdout)",
    )
    return parser.parse_args(argv)


def read_fills(journal_path: Path) -> list[dict]:
    """Read fill entries from a JSONL journal file.

    Returns a list of raw fill payload dicts.  Missing or empty journals
    return an empty list.  Malformed lines are skipped with a warning.
    """
    if not journal_path.exists():
        return []

    raw_fills: list[dict] = []
    with journal_path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                warnings.warn(
                    f"Skipping malformed line {lineno} in {journal_path}: {exc}",
                    stacklevel=2,
                )
                continue
            if entry.get("event_type") != "fill":
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                warnings.warn(
                    f"Skipping fill entry on line {lineno}: missing or non-dict payload",
                    stacklevel=2,
                )
                continue
            raw_fills.append(payload)
    return raw_fills


def aggregate_fills(raw_fills: list[dict]) -> list[dict]:
    """Aggregate raw fill payloads by (symbol, side).

    For each (symbol, side) pair:
      - qty  = sum of all delta values
      - price = limit_price of the most recent fill
      - prices = [{qty, price}, ...] breakdown (one entry per raw fill)

    The `prices` breakdown preserves all partial fills so no data is lost
    when a symbol trades at multiple limit prices.
    """
    groups: dict[tuple[str, str], list[dict]] = {}

    for payload in raw_fills:
        symbol = payload.get("symbol", "")
        side = payload.get("side", "")
        delta = float(payload.get("delta", 0.0))
        price = float(payload.get("limit_price", 0.0))
        groups.setdefault((symbol, side), []).append({"qty": delta, "price": price})

    result: list[dict] = []
    for (symbol, side), entries in groups.items():
        result.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": sum(e["qty"] for e in entries),
                "price": entries[-1]["price"],
                "prices": entries,
            }
        )
    return result


def build_output(fills: list[dict]) -> dict:
    return {
        "schema_version": "fills/1",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "fills": fills,
    }


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    journal_path = Path(args.journal).resolve()
    raw_fills = read_fills(journal_path)
    aggregated = aggregate_fills(raw_fills)
    output = build_output(aggregated)

    text = json.dumps(output, indent=2)

    if args.out is None:
        print(text)
    else:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")

    # Operational summary — no dollar amounts or account names
    print(
        f"Processed {len(raw_fills)} fill row(s) → "
        f"{len(aggregated)} aggregated fill(s) exported.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
