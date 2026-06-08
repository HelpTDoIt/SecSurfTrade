"""
Export OCR-captured fills from the journal to a JSON file consumable by
the rebalance calculator's "Import fills" button.

Reads the raw fills, aggregates them by symbol and side, and formats
them to match the manual entry structure.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import _PKG_ROOT, resolve_output_path, resolve_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--journal",
        type=str,
        default=str(_PKG_ROOT / "logs" / "journal.jsonl"),
        help="Path to the journal.jsonl file",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=str(_PKG_ROOT / "logs" / "fills.json"),
        help="Path to save the aggregated fills JSON",
    )
    return parser.parse_args(argv)


def read_fills(journal_path: Path) -> list[dict]:
    if not journal_path.exists():
        return []

    fills = []
    text = journal_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            import warnings
            warnings.warn("malformed json line")
            continue
        if record.get("event_type") == "fill" and "payload" in record:
            fills.append(record["payload"])
    return fills


def aggregate_fills(raw_fills: list[dict]) -> list[dict]:
    """Aggregate raw fill payloads by (account, order_id)."""
    groups: dict[tuple, dict] = {}

    for payload in raw_fills:
        account = payload.get("account", "UNKNOWN")
        order_id = payload.get("order_id", "")
        symbol = payload.get("symbol", "")
        side = payload.get("side", "")
        delta = float(payload.get("delta", 0.0))
        price = float(payload.get("limit_price", 0.0))
        
        if not order_id:
            key = (account, order_id, symbol, side)
        else:
            key = (account, order_id)
            
        if key not in groups:
            groups[key] = {"qty": 0.0, "value": 0.0, "symbol": symbol, "side": side}
            
        groups[key]["qty"] += delta
        groups[key]["value"] += delta * price
        groups[key]["symbol"] = symbol
        groups[key]["side"] = side

    result: list[dict] = []
    for key_tuple, data in groups.items():
        account = key_tuple[0]
        order_id = key_tuple[1]
        qty = round(data["qty"], 4)
        if qty > 0:
            avg_price = data["value"] / data["qty"]
            result.append(
                {
                    "account": account,
                    "chunk_id": order_id,
                    "qty": qty,
                    "price": round(avg_price, 4),
                    "symbol": data["symbol"],
                    "side": data["side"],
                }
            )
    return result


def build_output(fills: list[dict]) -> dict:
    return {
        "schema_version": "fills/1.0",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "fills": fills,
    }


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    journal_path = resolve_path(args.journal)
    raw_fills = read_fills(journal_path)
    aggregated = aggregate_fills(raw_fills)
    out_data = build_output(aggregated)

    text = json.dumps(out_data, indent=2) + "\n"
    if args.out == "-" or args.out is None:
        print(text)
    else:
        out_path = resolve_output_path(args.out)
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
