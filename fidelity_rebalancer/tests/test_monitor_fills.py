"""
D-1: OCR fill auto-logging — tests for detect_and_log_fills.

Tests are driven against the module-level ``detect_and_log_fills`` function
exported from ``tui.monitor``.  No Textual app or plan object is needed.
The Journal writes to a tmp_path JSONL file so assertions are on real
persisted events.

First-seen-fill behaviour under test:
    An order first seen with filled_qty > 0 emits a ``fill`` for that amount.
    Re-polling with no new fill must NOT emit a duplicate ``fill``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adapters import OrderRow, OrderStatus
from adapters.mock_atp import MockATP
from tui.monitor import Journal, detect_and_log_fills


# ── Helpers ────────────────────────────────────────────────────────────────

T0 = datetime(2026, 6, 2, 14, 30, tzinfo=timezone.utc)


def _row(
    order_id: str,
    qty: float,
    filled: float,
    status: OrderStatus,
    limit: float = 50.00,
    symbol: str = "VOO",
    side: str = "BUY",
) -> OrderRow:
    return OrderRow(
        account="Test",
        symbol=symbol,
        side=side,
        qty=qty,
        filled_qty=filled,
        limit_price=limit,
        status=status,
        placed_at=T0,
        last_update_at=T0,
        order_id=order_id,
    )


def _snap(*rows: OrderRow) -> dict[str, OrderRow]:
    return {r.order_id: r for r in rows}


def _read_events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _fill_events(path: Path) -> list[dict]:
    return [e for e in _read_events(path) if e["event_type"] == "fill"]


# ── Tests ──────────────────────────────────────────────────────────────────


def test_no_fill_on_open_order(tmp_path: Path):
    """Polling an Open order with 0 filled_qty should not emit any fill event."""
    journal = Journal(tmp_path / "journal.jsonl")
    last_filled: dict[str, float] = {}

    detect_and_log_fills(
        _snap(_row("ord1", 100, 0.0, OrderStatus.Open)), last_filled, journal
    )

    assert _fill_events(tmp_path / "journal.jsonl") == []
    assert last_filled["ord1"] == 0.0


def test_partial_fill_emits_fill_event(tmp_path: Path):
    """A partial fill first seen with filled_qty > 0 emits one fill event."""
    journal = Journal(tmp_path / "journal.jsonl")
    last_filled: dict[str, float] = {}

    detect_and_log_fills(
        _snap(_row("ord1", 100, 40.0, OrderStatus.PartiallyFilled)),
        last_filled,
        journal,
    )

    fills = _fill_events(tmp_path / "journal.jsonl")
    assert len(fills) == 1
    p = fills[0]["payload"]
    assert p["order_id"] == "ord1"
    assert p["delta"] == pytest.approx(40.0)
    assert p["filled_qty"] == pytest.approx(40.0)
    assert p["status"] == "PartiallyFilled"


def test_no_duplicate_fill_on_repoll(tmp_path: Path):
    """Re-polling the same snapshot with no new fill must not emit a duplicate."""
    journal = Journal(tmp_path / "journal.jsonl")
    last_filled: dict[str, float] = {}
    snap = _snap(_row("ord1", 100, 40.0, OrderStatus.PartiallyFilled))

    detect_and_log_fills(snap, last_filled, journal)  # first observation
    detect_and_log_fills(snap, last_filled, journal)  # identical re-poll

    fills = _fill_events(tmp_path / "journal.jsonl")
    assert len(fills) == 1


def test_incremental_fill_then_full_fill(tmp_path: Path):
    """Open -> partial 40 -> partial 70 -> Filled 100 should produce 3 fill events."""
    journal = Journal(tmp_path / "journal.jsonl")
    last_filled: dict[str, float] = {}

    detect_and_log_fills(
        _snap(_row("ord1", 100, 0.0, OrderStatus.Open)), last_filled, journal
    )
    detect_and_log_fills(
        _snap(_row("ord1", 100, 40.0, OrderStatus.PartiallyFilled)),
        last_filled,
        journal,
    )
    detect_and_log_fills(
        _snap(_row("ord1", 100, 70.0, OrderStatus.PartiallyFilled)),
        last_filled,
        journal,
    )
    detect_and_log_fills(
        _snap(_row("ord1", 100, 100.0, OrderStatus.Filled)), last_filled, journal
    )

    fills = _fill_events(tmp_path / "journal.jsonl")
    assert len(fills) == 3

    deltas = [f["payload"]["delta"] for f in fills]
    assert deltas == [pytest.approx(40.0), pytest.approx(30.0), pytest.approx(30.0)]

    cumulative = [f["payload"]["filled_qty"] for f in fills]
    assert cumulative == [
        pytest.approx(40.0),
        pytest.approx(70.0),
        pytest.approx(100.0),
    ]

    assert fills[-1]["payload"]["status"] == "Filled"


def test_fill_event_payload_fields(tmp_path: Path):
    """All required D-1 fields are present in the fill event payload."""
    journal = Journal(tmp_path / "journal.jsonl")
    last_filled: dict[str, float] = {}

    row = _row(
        "ord2",
        50,
        25.0,
        OrderStatus.PartiallyFilled,
        limit=499.75,
        symbol="SPY",
        side="SELL",
    )
    detect_and_log_fills({"ord2": row}, last_filled, journal)

    fills = _fill_events(tmp_path / "journal.jsonl")
    assert len(fills) == 1
    p = fills[0]["payload"]

    assert p["order_id"] == "ord2"
    assert p["symbol"] == "SPY"
    assert p["side"] == "SELL"
    assert p["delta"] == pytest.approx(25.0)
    assert p["filled_qty"] == pytest.approx(25.0)
    assert p["limit_price"] == pytest.approx(499.75)
    assert p["status"] == "PartiallyFilled"
    # Every journal entry must carry a top-level timestamp
    assert "ts" in fills[0]


def test_multiple_orders_independent_tracking(tmp_path: Path):
    """Two orders advancing at different rates each get their own fill trail."""
    journal = Journal(tmp_path / "journal.jsonl")
    last_filled: dict[str, float] = {}

    # Poll 1: ord_a gets a partial; ord_b stays open
    detect_and_log_fills(
        _snap(
            _row("ord_a", 200, 100.0, OrderStatus.PartiallyFilled),
            _row("ord_b", 50, 0.0, OrderStatus.Open),
        ),
        last_filled,
        journal,
    )

    # Poll 2: ord_a unchanged; ord_b fills fully
    detect_and_log_fills(
        _snap(
            _row("ord_a", 200, 100.0, OrderStatus.PartiallyFilled),
            _row("ord_b", 50, 50.0, OrderStatus.Filled),
        ),
        last_filled,
        journal,
    )

    fills = _fill_events(tmp_path / "journal.jsonl")
    assert len(fills) == 2

    ids = {f["payload"]["order_id"] for f in fills}
    assert ids == {"ord_a", "ord_b"}

    a_p = next(f["payload"] for f in fills if f["payload"]["order_id"] == "ord_a")
    b_p = next(f["payload"] for f in fills if f["payload"]["order_id"] == "ord_b")
    assert a_p["delta"] == pytest.approx(100.0)
    assert b_p["delta"] == pytest.approx(50.0)
    assert b_p["status"] == "Filled"


def test_fill_with_mock_atp_advance(tmp_path: Path):
    """Integration: use MockATP.advance() to drive fills and assert journal trail."""
    journal = Journal(tmp_path / "journal.jsonl")
    last_filled: dict[str, float] = {}

    mock = MockATP()
    mock.add_order(_row("c1", 100, 0.0, OrderStatus.Open))
    mock.add_order(_row("c2", 80, 0.0, OrderStatus.Open))

    # Poll 1: both Open, no fills
    snap0 = {r.order_id: r for r in mock.get_orders()}
    detect_and_log_fills(snap0, last_filled, journal)
    assert _fill_events(tmp_path / "journal.jsonl") == []

    # Advance: c1 partial 60, c2 still 0
    mock.advance(seconds=10, fills={"c1": 60})
    snap1 = {r.order_id: r for r in mock.get_orders()}
    detect_and_log_fills(snap1, last_filled, journal)

    fills_after_1 = _fill_events(tmp_path / "journal.jsonl")
    assert len(fills_after_1) == 1
    assert fills_after_1[0]["payload"]["order_id"] == "c1"
    assert fills_after_1[0]["payload"]["delta"] == pytest.approx(60.0)

    # Re-poll identical state: no new fill
    detect_and_log_fills(snap1, last_filled, journal)
    assert len(_fill_events(tmp_path / "journal.jsonl")) == 1

    # Advance: c1 full (100), c2 partial 50
    mock.advance(seconds=10, fills={"c1": 100, "c2": 50})
    snap2 = {r.order_id: r for r in mock.get_orders()}
    detect_and_log_fills(snap2, last_filled, journal)

    fills_final = _fill_events(tmp_path / "journal.jsonl")
    assert (
        len(fills_final) == 3
    )  # c1 delta-40, c2 delta-50 (plus the earlier c1 delta-60)

    c1_fills = [f["payload"] for f in fills_final if f["payload"]["order_id"] == "c1"]
    assert len(c1_fills) == 2
    assert c1_fills[0]["filled_qty"] == pytest.approx(60.0)
    assert c1_fills[0]["delta"] == pytest.approx(60.0)
    assert c1_fills[1]["filled_qty"] == pytest.approx(100.0)
    assert c1_fills[1]["delta"] == pytest.approx(40.0)  # 100 - 60

    c2_fills = [f["payload"] for f in fills_final if f["payload"]["order_id"] == "c2"]
    assert len(c2_fills) == 1
    assert c2_fills[0]["filled_qty"] == pytest.approx(50.0)
    assert c2_fills[0]["delta"] == pytest.approx(50.0)


def test_journal_none_does_not_raise(tmp_path: Path):
    """Passing journal=None should silently update last_filled without error."""
    last_filled: dict[str, float] = {}

    detect_and_log_fills(
        _snap(_row("ord1", 100, 30.0, OrderStatus.PartiallyFilled)),
        last_filled,
        journal=None,
    )

    assert last_filled["ord1"] == pytest.approx(30.0)
