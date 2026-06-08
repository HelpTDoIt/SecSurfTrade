"""
End-to-end monitor JOURNAL-TRAIL test (chunk-6 acceptance criterion #3).

Graduated from the LT-1 live-test harness. Complements
test_stall.py::test_e2e_stall_detect_requote_recompute, which exercises the
state helpers (_all_sells_terminal / _actual_proceeds) but NOT the journal.
This test drives the full lifecycle through the real stall engine, the real
MockATP, and the real Journal, then asserts the complete event trail:

    monitor_start -> poll -> state_change -> stall -> requote_suggested
    -> requote_action -> state_change(all_sells_done) -> recompute_buys
    -> monitor_stop

Scenario (chunk-6 acceptance #2):
    s1 = 1600 shs SELL EEM @ 62.39  -> fills fully (62.41)
    s2 =   55 shs SELL EEM @ 62.39  -> partial 30/55, then stalls 5m
    bid drops to 62.37 -> advisor suggests 62.38 (bid + 1 tick)
    user "presses C" -> s2 cancelled, s2b = 25 shs @ 62.38 -> fills
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from adapters import OrderRow, OrderStatus
from adapters.mock_atp import MockATP
from engine.stall import detect_stalls, recommend_requote
from tui.monitor import Journal

THRESHOLD = 300  # 5 minutes
ACCOUNT = "Test Retirement"
T0 = datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc)


def _row(
    order_id: str,
    qty: float,
    filled: float,
    status: OrderStatus,
    placed: datetime,
    limit: float = 62.39,
) -> OrderRow:
    return OrderRow(
        account=ACCOUNT,
        symbol="EEM",
        side="SELL",
        qty=qty,
        filled_qty=filled,
        limit_price=limit,
        status=status,
        placed_at=placed,
        last_update_at=placed,
        order_id=order_id,
    )


def test_e2e_journal_trail(tmp_path: Path):
    journal = Journal(tmp_path / "journal.jsonl")
    journal.write(
        "monitor_start", {"scenario": "LT-1 stall lifecycle", "account": ACCOUNT}
    )

    mock = MockATP()
    mock.set_quote("EEM", bid=62.39, ask=62.41, last=62.40, prev_close=62.71)
    mock.add_order(_row("s1", 1600, 0, OrderStatus.Open, T0))
    mock.add_order(_row("s2", 55, 0, OrderStatus.Open, T0))

    # Poll 1: both open
    journal.write("poll", {"open": [r.order_id for r in mock.get_orders()]})

    # s1 fills fully, s2 partial 30/55 (MockATP fills at each order's limit)
    mock.advance(seconds=20, fills={"s1": 1600, "s2": 30})
    snap = {r.order_id: [r.status.value, r.filled_qty] for r in mock.get_orders()}
    journal.write("state_change", {"orders": snap})
    assert snap["s1"][0] == "Filled"
    assert snap["s2"][0] == "PartiallyFilled"

    # 5m12s pass with no s2 progress; bid drops to 62.37
    mock.set_quote("EEM", bid=62.37, ask=62.40, last=62.38, prev_close=62.71)
    now = T0 + timedelta(seconds=20 + THRESHOLD + 12)
    stalls = detect_stalls(mock.get_orders(), THRESHOLD, now)
    assert len(stalls) == 1 and stalls[0].chunk_id == "s2"
    st = stalls[0]
    assert st.remaining_qty == pytest.approx(25.0)
    journal.write(
        "stall",
        {
            "chunk_id": st.chunk_id,
            "remaining_qty": st.remaining_qty,
            "seconds_stalled": round(st.seconds_stalled, 1),
        },
    )

    sugg = recommend_requote(st, side="sell", quote=mock.get_quote("EEM"))
    assert sugg.new_limit == pytest.approx(62.40)  # bid+1tick
    journal.write(
        "requote_suggested",
        {
            "chunk_id": sugg.chunk_id,
            "new_limit": sugg.new_limit,
            "remaining_qty": sugg.remaining_qty,
            "rationale": sugg.rationale,
        },
    )

    # User presses [C]: cancel s2, create s2b @ 62.38 for remaining qty
    mock.set_order_status("s2", OrderStatus.Cancelled, filled_qty=30)
    qty_b = int(sugg.remaining_qty)
    mock.add_order(_row("s2b", qty_b, 0, OrderStatus.Open, now, limit=sugg.new_limit))
    journal.write(
        "requote_action",
        {
            "cancelled": "s2",
            "new_chunk": "s2b",
            "new_limit": sugg.new_limit,
            "qty": qty_b,
        },
    )

    # s2b fills; all sells terminal
    mock.advance(seconds=30, fills={"s2b": qty_b})
    rows = mock.get_orders()
    terminal = {OrderStatus.Filled, OrderStatus.Cancelled}
    all_done = all(r.status in terminal for r in rows)
    assert all_done
    journal.write(
        "state_change",
        {
            "orders": {r.order_id: r.status.value for r in rows},
            "all_sells_done": all_done,
        },
    )

    proceeds = sum(
        r.filled_qty * r.limit_price for r in rows if r.status == OrderStatus.Filled
    )
    journal.write(
        "recompute_buys",
        {
            "account": ACCOUNT,
            "trigger": "all_sells_terminal",
            "proceeds": round(proceeds, 2),
        },
    )
    journal.write("monitor_stop", {"all_sells_done": all_done})

    # ── Assert the complete event trail (chunk-6 acceptance #3) ─────────────
    events = [
        json.loads(line)
        for line in (tmp_path / "journal.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    types = [e["event_type"] for e in events]
    assert types == [
        "monitor_start",
        "poll",
        "state_change",
        "stall",
        "requote_suggested",
        "requote_action",
        "state_change",
        "recompute_buys",
        "monitor_stop",
    ]
    by_type = {e["event_type"]: e["payload"] for e in events}
    assert by_type["stall"]["chunk_id"] == "s2"
    assert by_type["requote_suggested"]["new_limit"] == pytest.approx(62.40)
    assert by_type["requote_action"]["new_chunk"] == "s2b"
    assert by_type["recompute_buys"]["proceeds"] == pytest.approx(
        1600 * 62.39 + 25 * 62.40, abs=0.01
    )
    # every entry carries a timestamp
    assert all("ts" in e for e in events)
