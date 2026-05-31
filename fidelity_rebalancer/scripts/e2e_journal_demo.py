"""
End-to-end journal demo: poll → stall → re-quote → fill → recompute.
Run from fidelity_rebalancer/:  python scripts/e2e_journal_demo.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters import OrderRow, OrderStatus
from adapters.mock_atp import MockATP
from engine.stall import detect_stalls, recommend_requote
from tui.monitor import Journal, _actual_proceeds, _all_sells_terminal

placed = datetime(2026, 2, 27, 10, 0, 0, tzinfo=timezone.utc)
mock = MockATP()
mock.set_quote("EEM", bid=62.39, ask=62.42, last=62.41)

mock.add_order(
    OrderRow(
        account="Demo Account",
        symbol="EEM",
        side="SELL",
        qty=1600,
        filled_qty=0,
        limit_price=62.39,
        status=OrderStatus.Open,
        placed_at=placed,
        last_update_at=placed,
        order_id="s1",
    )
)
mock.add_order(
    OrderRow(
        account="Demo Account",
        symbol="EEM",
        side="SELL",
        qty=55,
        filled_qty=0,
        limit_price=62.39,
        status=OrderStatus.Open,
        placed_at=placed,
        last_update_at=placed,
        order_id="s2",
    )
)

jpath = Path("logs/journal_e2e_demo.jsonl")
j = Journal(jpath)

# t+60s: s1 fills fully, s2 partial 30/55
mock.advance(seconds=60, fills={"s1": 1600, "s2": 30})
orders = mock.get_orders()
j.write("poll", {"t": "+60s", "s1": "Filled", "s2": "PartiallyFilled 30/55"})
print("t+60:  s1 FILLED (1600 sh), s2 PartiallyFilled (30/55 sh)")

# t+360s: check stalls (s2 last_update_at is still at t+60, 300s ago)
now_sim = placed + timedelta(seconds=360)
stalls = detect_stalls(orders, 300, now_sim)
j.write(
    "stall_detected",
    {
        "chunk_id": stalls[0].chunk_id,
        "seconds_stalled": stalls[0].seconds_stalled,
        "remaining_qty": stalls[0].remaining_qty,
    },
)
print(
    f"t+360: STALL on {stalls[0].chunk_id} — {stalls[0].seconds_stalled:.0f}s stalled, "
    f"{stalls[0].remaining_qty:.0f} sh remaining"
)

# Bid moves to 62.37; get re-quote suggestion
mock.set_quote("EEM", bid=62.37, ask=62.45, last=62.38)
quote = mock.get_quote("EEM")
sugg = recommend_requote(stalls[0], "sell", quote)
j.write(
    "requote_suggested",
    {
        "chunk_id": sugg.chunk_id,
        "original_limit": stalls[0].original_limit,
        "new_limit": sugg.new_limit,
        "rationale": sugg.rationale,
    },
)
print(f"       Re-quote suggested: ${sugg.new_limit:.4f}  (bid=${quote.bid:.4f})")

# User presses C — confirmed
j.write(
    "requote_confirmed",
    {
        "original_chunk": "s2",
        "new_chunk": "s2b",
        "new_limit": sugg.new_limit,
        "remaining_qty": sugg.remaining_qty,
    },
)
print(
    f"       Confirmed: new chunk s2b — {sugg.remaining_qty:.0f} sh @ ${sugg.new_limit:.4f}"
)

# s2b fills (simulate: advance s2 to fully filled)
mock.advance(seconds=30, fills={"s2": 55})
orders2 = mock.get_orders()
j.write("poll", {"t": "+390s", "s2": "Filled (re-quoted and filled)"})
print("t+390: s2 FILLED (55 sh)")

order_map = {r.order_id: r for r in orders2}
if _all_sells_terminal("Demo Account", order_map, ["s1", "s2"]):
    proceeds = _actual_proceeds("Demo Account", order_map, ["s1", "s2"])
    j.write(
        "recompute_trigger", {"account": "Demo Account", "actual_proceeds": proceeds}
    )
    print(
        f"       All sells complete — recompute triggered. Proceeds: ${proceeds:,.2f}"
    )

print()
print("=== Journal (logs/journal_e2e_demo.jsonl) ===")
import json

for line in jpath.read_text(encoding="utf-8").splitlines():
    entry = json.loads(line)
    print(
        f"  [{entry['ts'][11:19]}] {entry['event_type']:25s} {json.dumps(entry['payload'])}"
    )
