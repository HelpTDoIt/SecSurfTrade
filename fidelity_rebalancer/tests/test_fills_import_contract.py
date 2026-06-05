"""
Validates the D-1 producer<->consumer contract that the (skipped) browser e2e
would otherwise cover: the JSON ``cli.export_fills`` emits must carry exactly the
fields ``rebalance_calculator.html``'s ``applyImportedFills`` reads, under a
schema both sides agree on.
"""
from __future__ import annotations

from pathlib import Path

from cli.export_fills import aggregate_fills, build_output

_REPO_ROOT = Path(__file__).parent.parent.parent
_CALCULATOR_HTML = _REPO_ROOT / "rebalance_calculator.html"

# Field accesses the HTML performs on each imported fill (applyImportedFills).
_CONSUMER_FIELDS = ("fill.symbol", "fill.side", "fill.qty", "fill.price")


def _sample_output():
    raw = [
        {"symbol": "SYNTH1", "side": "SELL", "delta": 100.0, "limit_price": 50.0},
        {"symbol": "SYNTH2", "side": "BUY", "delta": 10.0, "limit_price": 300.0},
    ]
    return build_output(aggregate_fills(raw))


def test_export_fill_rows_have_consumer_fields():
    """Each emitted fill carries the symbol/side/qty/price the HTML consumes."""
    out = _sample_output()
    assert out["fills"], "expected at least one aggregated fill"
    for fill in out["fills"]:
        for key in ("symbol", "side", "qty", "price"):
            assert key in fill, f"export fill missing '{key}': {fill}"


def test_export_schema_version_matches_html_guard():
    """Producer schema_version and the HTML's 'fills/' guard agree."""
    out = _sample_output()
    assert out["schema_version"].startswith("fills/")
    html = _CALCULATOR_HTML.read_text(encoding="utf-8")
    assert 'startsWith("fills/")' in html


def test_html_reads_exported_fill_fields():
    """applyImportedFills reads exactly the fields export_fills emits."""
    html = _CALCULATOR_HTML.read_text(encoding="utf-8")
    for token in _CONSUMER_FIELDS:
        assert token in html, f"calculator HTML never reads {token}"
