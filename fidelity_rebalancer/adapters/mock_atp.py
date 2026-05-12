"""
In-memory ATP simulator.

Implements QuoteAdapter, Level2Adapter, and OrdersAdapter for use in tests
and dry-run sessions — no ATP process required.

Usage:
    mock = MockATP()
    mock.set_quote("EEM", bid=62.39, ask=62.41, last=62.40, prev_close=62.71,
                   bid_size=500, ask_size=300, volume=1_200_000)
    mock.set_level2("EEM", bids=[...], asks=[...])
    mock.add_order(OrderRow(...))

    quote = mock.get_quote("EEM")
    book  = mock.get_level2("EEM")
    rows  = mock.get_orders()
"""
from __future__ import annotations

import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Callable

from adapters import (
    Level,
    Level2Snapshot,
    OrderRow,
    OrderStatus,
    QuoteSnapshot,
)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class MockATP:
    """
    Configurable in-memory ATP simulator.
    Thread-safe reads; mutations are not guarded (test use only).
    """

    def __init__(self) -> None:
        self._quotes: dict[str, QuoteSnapshot] = {}
        self._books: dict[str, Level2Snapshot] = {}
        self._orders: list[OrderRow] = []
        # Optional hooks called before each read, for dynamic simulation
        self._quote_hook: Callable[[str], None] | None = None
        self._orders_hook: Callable[[], None] | None = None

    # ── Configuration helpers ──────────────────────────────────────────────

    def set_quote(
        self,
        symbol: str,
        bid: float,
        ask: float,
        last: float,
        prev_close: float = 0.0,
        bid_size: int = 100,
        ask_size: int = 100,
        volume: int = 0,
        ts: datetime | None = None,
    ) -> None:
        self._quotes[symbol.upper()] = QuoteSnapshot(
            symbol=symbol.upper(),
            bid=bid,
            bid_size=bid_size,
            ask=ask,
            ask_size=ask_size,
            last=last,
            prev_close=prev_close,
            volume=volume,
            ts=ts or _now(),
        )

    def set_level2(
        self,
        symbol: str,
        bids: list[tuple[float, int, str]],
        asks: list[tuple[float, int, str]],
        ts: datetime | None = None,
    ) -> None:
        """
        bids / asks: list of (price, size, mpid) tuples.
        They are stored sorted best-first automatically.
        """
        self._books[symbol.upper()] = Level2Snapshot(
            symbol=symbol.upper(),
            bids=sorted(
                [Level(p, s, m) for p, s, m in bids],
                key=lambda l: l.price,
                reverse=True,
            ),
            asks=sorted(
                [Level(p, s, m) for p, s, m in asks],
                key=lambda l: l.price,
            ),
            ts=ts or _now(),
        )

    def add_order(self, row: OrderRow) -> None:
        self._orders.append(row)

    def clear_orders(self) -> None:
        self._orders.clear()

    def set_order_status(self, order_id: str, status: OrderStatus, filled_qty: float | None = None) -> None:
        """Update a specific order's status (identified by order_id)."""
        for row in self._orders:
            if row.order_id == order_id:
                row.status = status
                row.last_update_at = _now()
                if filled_qty is not None:
                    row.filled_qty = filled_qty
                return
        raise KeyError(f"order_id {order_id!r} not found")

    def set_quote_hook(self, hook: Callable[[str], None] | None) -> None:
        """Optional callable invoked before get_quote; can mutate self._quotes."""
        self._quote_hook = hook

    def set_orders_hook(self, hook: Callable[[], None] | None) -> None:
        """Optional callable invoked before get_orders; can mutate self._orders."""
        self._orders_hook = hook

    # ── Adapter interface ──────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> QuoteSnapshot:
        sym = symbol.upper()
        if self._quote_hook:
            self._quote_hook(sym)
        if sym not in self._quotes:
            raise KeyError(f"MockATP: no quote configured for {sym!r}")
        return deepcopy(self._quotes[sym])

    def get_level2(self, symbol: str) -> Level2Snapshot:
        sym = symbol.upper()
        if sym not in self._books:
            raise KeyError(f"MockATP: no Level 2 book configured for {sym!r}")
        return deepcopy(self._books[sym])

    def get_orders(self) -> list[OrderRow]:
        if self._orders_hook:
            self._orders_hook()
        return deepcopy(self._orders)

    # ── Time-travel / scenario helpers ────────────────────────────────────

    def advance(
        self,
        seconds: float = 0.0,
        fills: dict[str, float] | None = None,
    ) -> None:
        """
        Advance simulated time and apply partial fills.

        `fills` maps order_id → filled_qty.  The `last_update_at` for each
        affected order is set to (original last_update_at + seconds) so stall
        tests can control exactly how stale an order appears.

        Orders not in `fills` have their timestamps left unchanged (they are
        NOT advanced), so they appear older relative to the new "now".
        """
        from datetime import timedelta
        fills = fills or {}
        for row in self._orders:
            if row.order_id in fills:
                filled = fills[row.order_id]
                row.filled_qty = filled
                row.status = (
                    OrderStatus.Filled
                    if filled >= row.qty - 1e-9
                    else OrderStatus.PartiallyFilled
                )
                # Timestamp the fill at original + seconds
                row.last_update_at = row.last_update_at + timedelta(seconds=seconds)

    # ── Partial-fill simulation ────────────────────────────────────────────

    def simulate_partial_fill(
        self,
        order_id: str,
        filled_qty: float,
        ts: datetime | None = None,
    ) -> None:
        """
        Advance an order to PartiallyFilled with the given filled_qty.
        Sets last_update_at to ts (or now) to support stall-detection testing.
        """
        for row in self._orders:
            if row.order_id == order_id:
                row.filled_qty = filled_qty
                row.status = (
                    OrderStatus.Filled
                    if filled_qty >= row.qty - 1e-9
                    else OrderStatus.PartiallyFilled
                )
                row.last_update_at = ts or _now()
                return
        raise KeyError(f"order_id {order_id!r} not found")
