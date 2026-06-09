import sys
from pathlib import Path
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

import pytest
from cli.export_fills import aggregate_fills

def test_aggregate_fills_zero_qty_ignored():
    raw = [
        {"account": "acc1", "order_id": "s1", "symbol": "SYNTH1", "side": "SELL", "delta": 0.0, "limit_price": 50.00},
    ]
    result = aggregate_fills(raw)
    assert result == []

def test_aggregate_fills_negative_qty_ignored():
    raw = [
        {"account": "acc1", "order_id": "s1", "symbol": "SYNTH1", "side": "SELL", "delta": -10.0, "limit_price": 50.00},
    ]
    result = aggregate_fills(raw)
    assert result == []

def test_aggregate_fills_missing_fields_default():
    raw = [
        {"delta": 10.0, "limit_price": 100.00},
    ]
    result = aggregate_fills(raw)
    assert len(result) == 1
    assert result[0]["account"] == "UNKNOWN"
    assert result[0]["chunk_id"] == ""
    assert result[0]["symbol"] == ""
    assert result[0]["side"] == ""
    assert result[0]["qty"] == 10.0
    assert result[0]["price"] == 100.0

def test_aggregate_fills_float_precision_prices():
    raw = [
        {"account": "acc1", "order_id": "s1", "symbol": "SYNTH1", "side": "SELL", "delta": 1.0, "limit_price": 10.12345},
        {"account": "acc1", "order_id": "s1", "symbol": "SYNTH1", "side": "SELL", "delta": 1.0, "limit_price": 10.12355},
    ]
    result = aggregate_fills(raw)
    assert len(result) == 1
    assert result[0]["price"] == pytest.approx(10.1235)

def test_aggregate_fills_mixed_signs():
    raw = [
        {"account": "acc1", "order_id": "s1", "symbol": "SYNTH1", "side": "SELL", "delta": 10.0, "limit_price": 50.00},
        {"account": "acc1", "order_id": "s1", "symbol": "SYNTH1", "side": "SELL", "delta": -5.0, "limit_price": 50.00},
    ]
    result = aggregate_fills(raw)
    assert len(result) == 1
    assert result[0]["qty"] == 5.0
    assert result[0]["price"] == 50.0

def test_aggregate_fills_zero_sum():
    raw = [
        {"account": "acc1", "order_id": "s1", "symbol": "SYNTH1", "side": "SELL", "delta": 10.0, "limit_price": 50.00},
        {"account": "acc1", "order_id": "s1", "symbol": "SYNTH1", "side": "SELL", "delta": -10.0, "limit_price": 50.00},
    ]
    result = aggregate_fills(raw)
    assert result == []
