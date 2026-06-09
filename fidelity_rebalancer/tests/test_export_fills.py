"""
Tests for cli.export_fills — OCR fill journal → calculator fills JSON.

Fixtures use synthetic symbols and order IDs only (s1/b1 style).
No real journal data is committed; all journals are written to tmp_path.
"""
from __future__ import annotations

import json
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from cli.export_fills import aggregate_fills, build_output, main, read_fills

# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_fill_entry(
    order_id: str,
    symbol: str,
    side: str,
    delta: float,
    limit_price: float,
) -> str:
    """Return a single JSONL line for a fill event."""
    entry = {
        "ts": "2026-06-04T14:30:00+00:00",
        "event_type": "fill",
        "payload": {
            "account": "test_account",
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "delta": delta,
            "filled_qty": delta,
            "limit_price": limit_price,
            "status": "Filled",
        },
    }
    return json.dumps(entry)


def _write_journal(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── read_fills tests ─────────────────────────────────────────────────────────


def test_read_fills_missing_journal_returns_empty(tmp_path: Path):
    missing = tmp_path / "does_not_exist.jsonl"
    result = read_fills(missing)
    assert result == []


def test_read_fills_empty_journal_returns_empty(tmp_path: Path):
    empty = tmp_path / "journal.jsonl"
    empty.write_text("", encoding="utf-8")
    result = read_fills(empty)
    assert result == []


def test_read_fills_non_fill_events_ignored(tmp_path: Path):
    journal = tmp_path / "journal.jsonl"
    lines = [
        json.dumps({"ts": "2026-06-04T14:00:00+00:00", "event_type": "poll", "payload": {}}),
        json.dumps({"ts": "2026-06-04T14:01:00+00:00", "event_type": "heartbeat", "payload": {}}),
        _make_fill_entry("s1", "SYNTH1", "SELL", 100.0, 50.00),
    ]
    _write_journal(journal, lines)
    result = read_fills(journal)
    assert len(result) == 1
    assert result[0]["symbol"] == "SYNTH1"


def test_read_fills_malformed_line_skipped(tmp_path: Path):
    journal = tmp_path / "journal.jsonl"
    lines = [
        _make_fill_entry("b1", "SYNTH2", "BUY", 50.0, 100.00),
        "NOT VALID JSON {{{",
        _make_fill_entry("b2", "SYNTH3", "BUY", 25.0, 200.00),
    ]
    _write_journal(journal, lines)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = read_fills(journal)

    assert len(result) == 2
    assert any("malformed" in str(w.message).lower() for w in caught)


def test_read_fills_collects_fill_payloads(tmp_path: Path):
    journal = tmp_path / "journal.jsonl"
    lines = [
        _make_fill_entry("s1", "SYNTH1", "SELL", 200.0, 62.39),
        _make_fill_entry("b1", "SYNTH2", "BUY", 10.0, 500.00),
    ]
    _write_journal(journal, lines)
    result = read_fills(journal)
    assert len(result) == 2
    assert result[0]["symbol"] == "SYNTH1"
    assert result[0]["delta"] == pytest.approx(200.0)
    assert result[1]["symbol"] == "SYNTH2"
    assert result[1]["limit_price"] == pytest.approx(500.00)


# ── aggregate_fills tests ────────────────────────────────────────────────────


def test_aggregate_fills_sums_delta_same_account_order():
    raw = [
        {"account": "acc1", "order_id": "s1", "symbol": "SYNTH1", "side": "SELL", "delta": 100.0, "limit_price": 50.00},
        {"account": "acc1", "order_id": "s1", "symbol": "SYNTH1", "side": "SELL", "delta": 40.0, "limit_price": 50.00},
    ]
    result = aggregate_fills(raw)
    assert len(result) == 1
    assert result[0]["symbol"] == "SYNTH1"
    assert result[0]["chunk_id"] == "s1"
    assert result[0]["qty"] == pytest.approx(140.0)

def test_aggregate_fills_different_orders_are_separate():
    raw = [
        {"account": "acc1", "order_id": "b1", "symbol": "SYNTH1", "side": "BUY", "delta": 50.0, "limit_price": 50.00},
        {"account": "acc1", "order_id": "s1", "symbol": "SYNTH1", "side": "SELL", "delta": 30.0, "limit_price": 51.00},
    ]
    result = aggregate_fills(raw)
    assert len(result) == 2

def test_aggregate_fills_price_is_weighted_average():
    raw = [
        {"account": "acc1", "order_id": "b1", "symbol": "SYNTH1", "side": "BUY", "delta": 10.0, "limit_price": 100.00},
        {"account": "acc1", "order_id": "b1", "symbol": "SYNTH1", "side": "BUY", "delta": 5.0, "limit_price": 115.00},
    ]
    # (10*100 + 5*115) / 15 = 1575 / 15 = 105.0
    result = aggregate_fills(raw)
    assert len(result) == 1
    assert result[0]["price"] == pytest.approx(105.0)

def test_aggregate_fills_empty_input():
    result = aggregate_fills([])
    assert result == []

def test_aggregate_fills_multiple_symbols():
    raw = [
        {"account": "acc1", "order_id": "s1", "symbol": "SYNTH1", "side": "SELL", "delta": 200.0, "limit_price": 60.00},
        {"account": "acc1", "order_id": "b1", "symbol": "SYNTH2", "side": "BUY", "delta": 10.0, "limit_price": 300.00},
        {"account": "acc1", "order_id": "s1", "symbol": "SYNTH1", "side": "SELL", "delta": 50.0, "limit_price": 70.00},
    ]
    result = aggregate_fills(raw)
    assert len(result) == 2
    synth1 = next(r for r in result if r["symbol"] == "SYNTH1")
    assert synth1["qty"] == pytest.approx(250.0)
    # (200*60 + 50*70) / 250 = (12000 + 3500)/250 = 15500/250 = 62.0
    assert synth1["price"] == pytest.approx(62.0)

# "?"? build_output tests "?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?"?

def test_build_output_schema_version():
    out = build_output([])
    assert out["schema_version"] == "fills/1.0"


def test_build_output_fills_list():
    fills = [{"symbol": "SYNTH1", "side": "SELL", "qty": 10, "price": 50.0, "prices": []}]
    out = build_output(fills)
    assert out["fills"] == fills


def test_build_output_generated_at_is_iso():
    out = build_output([])
    # Should be parseable ISO timestamp
    from datetime import datetime
    dt = datetime.fromisoformat(out["generated_at"])
    assert dt is not None


# ── CLI integration tests ─────────────────────────────────────────────────────


def test_cli_empty_journal_stdout(tmp_path: Path, capsys):
    """Empty journal → empty fills, exits 0."""
    journal = tmp_path / "journal.jsonl"
    journal.write_text("", encoding="utf-8")

    main(["--journal", str(journal), "--out", "-"])


    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["schema_version"] == "fills/1.0"
    assert parsed["fills"] == []


def test_cli_missing_journal_stdout(tmp_path: Path, capsys):
    """Missing journal → empty fills, exits 0."""
    missing = tmp_path / "no_such_file.jsonl"
    main(["--journal", str(missing), "--out", "-"])

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["fills"] == []


def test_cli_fills_written_to_file(tmp_path: Path):
    """--out writes JSON to file."""
    journal = tmp_path / "journal.jsonl"
    out_file = tmp_path / "fills.json"
    lines = [
        _make_fill_entry("s1", "SYNTH1", "SELL", 100.0, 62.39),
        _make_fill_entry("b1", "SYNTH2", "BUY", 10.0, 300.00),
    ]
    _write_journal(journal, lines)

    main(["--journal", str(journal), "--out", str(out_file)])

    assert out_file.exists()
    parsed = json.loads(out_file.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == "fills/1.0"
    assert len(parsed["fills"]) == 2


def test_cli_aggregation_via_main(tmp_path: Path, capsys):
    """Two fill rows for same account+order_id aggregate to one fill in output."""
    journal = tmp_path / "journal.jsonl"
    lines = [
        _make_fill_entry("s1", "SYNTH1", "SELL", 1600.0, 62.39),
        _make_fill_entry("s1", "SYNTH1", "SELL", 55.0, 62.37),
    ]
    _write_journal(journal, lines)

    main(["--journal", str(journal), "--out", "-"])


    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert len(parsed["fills"]) == 1
    f = parsed["fills"][0]
    assert f["symbol"] == "SYNTH1"
    assert f["side"] == "SELL"
    assert f["qty"] == pytest.approx(1655.0)
    assert f["price"] == pytest.approx(62.3893)



def test_cli_subprocess_smoke(tmp_path: Path):
    """Run the module via subprocess; assert exit 0 and valid JSON on stdout."""
    journal = tmp_path / "journal.jsonl"
    out_file = tmp_path / "fills.json"
    lines = [
        _make_fill_entry("s1", "SYNTH1", "SELL", 100.0, 50.00),
    ]
    _write_journal(journal, lines)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli.export_fills",
            "--journal",
            str(journal),
            "--out",
            str(out_file),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert out_file.exists()
    parsed = json.loads(out_file.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == "fills/1.0"
    assert len(parsed["fills"]) == 1
