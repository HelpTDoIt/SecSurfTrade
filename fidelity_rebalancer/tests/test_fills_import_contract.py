
from __future__ import annotations

import pytest

from fidelity_rebalancer.cli.export_fills import aggregate_fills, build_output

def simulate_html_apply_imported_fills(results, doc_fills):
    '''
    A genuine Python mock simulation of the applyImportedFills mapping logic
    found in rebalance_calculator.html.
    '''
    chunk_map = {}
    sc = 0
    bc = 0
    
    for acct, res in results.items():
        for sell in res.get("sells", []):
            # simulate buildSellChunks returning 1 chunk
            chunks = [{"shares": sell["quantity"]}]
            for ch in chunks:
                sc += 1
                chunk_map[f"{acct}:s{sc}"] = {"strategy": sell["strategy"], "side": "sell"}
                
        for buy in res.get("buys", []):
            # simulate buildBuyChunks returning 1 chunk
            chunks = [{"shares": 10}] # dummy
            for ch in chunks:
                bc += 1
                chunk_map[f"{acct}:b{bc}"] = {"strategy": buy["strategy"], "side": "buy"}
                
    new_fills = {}
    unmatched = []
    
    for fill in doc_fills:
        acct = fill.get("account")
        chunk_id = fill.get("chunk_id")
        fill_qty = fill.get("qty", 0)
        fill_price = fill.get("price", 0)
        
        mapping = chunk_map.get(f"{acct}:{chunk_id}")
        if mapping:
            strategy = mapping["strategy"]
            side = mapping["side"]
            if acct not in new_fills:
                new_fills[acct] = {}
            if strategy not in new_fills[acct]:
                new_fills[acct][strategy] = {"sells": [], "buys": []}
            
            type_key = "sells" if side == "sell" else "buys"
            new_fills[acct][strategy][type_key].append({
                "price": fill_price,
                "qty": fill_qty,
            })
        else:
            unmatched.append(fill)
            
    return new_fills, unmatched

def test_export_and_import_matching_logic():
    # Mock React results state
    results = {
        "Roth": {
            "sells": [{"strategy": "CORE", "quantity": 100, "limitPrice": 50}],
            "buys": [{"strategy": "SATELLITE", "dollarTarget": 500, "limitPrice": 50}]
        }
    }
    
    # 1 sell chunk -> Roth:s1
    # 1 buy chunk -> Roth:b1
    
    # Raw payload simulating OCR fills matching the generated orders
    raw_fills = [
        {"account": "Roth", "order_id": "s1", "symbol": "XYZ", "side": "SELL", "delta": 50, "limit_price": 50.0},
        {"account": "Roth", "order_id": "s1", "symbol": "XYZ", "side": "SELL", "delta": 50, "limit_price": 50.0},
        {"account": "Roth", "order_id": "b1", "symbol": "ABC", "side": "BUY", "delta": 10, "limit_price": 49.5},
        # Unmatched fill
        {"account": "Roth", "order_id": "", "symbol": "UNMATCHED", "side": "BUY", "delta": 5, "limit_price": 10.0},
    ]
    
    aggregated = aggregate_fills(raw_fills)
    output = build_output(aggregated)
    doc_fills = output["fills"]
    
    # Run simulation
    new_fills, unmatched = simulate_html_apply_imported_fills(results, doc_fills)
    
    # Verify matched fills
    assert "Roth" in new_fills
    
    core_sells = new_fills["Roth"]["CORE"]["sells"]
    assert len(core_sells) == 1  # 2 raw fills aggregated into 1
    assert core_sells[0]["qty"] == 100.0
    assert core_sells[0]["price"] == 50.0
    
    satellite_buys = new_fills["Roth"]["SATELLITE"]["buys"]
    assert len(satellite_buys) == 1
    assert satellite_buys[0]["qty"] == 10.0
    assert satellite_buys[0]["price"] == 49.5
    
    # Verify unmatched fills
    assert len(unmatched) == 1
    assert unmatched[0]["chunk_id"] == ""
    assert unmatched[0]["symbol"] == "UNMATCHED"
