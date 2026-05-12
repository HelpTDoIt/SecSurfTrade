"""
Adapter interfaces (Protocols) for ATP read-only data.

All pywinauto imports are isolated inside adapters/atp_*.py.
The engine and TUI depend only on these Protocols and the data classes below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable


# ── Data classes ───────────────────────────────────────────────────────────

@dataclass
class QuoteSnapshot:
    symbol: str
    bid: float
    bid_size: int
    ask: float
    ask_size: int
    last: float
    prev_close: float
    volume: int          # raw share count
    ts: datetime


@dataclass
class Level:
    price: float
    size: int
    mpid: str            # market participant ID, e.g. "NSDQ"


@dataclass
class Level2Snapshot:
    symbol: str
    bids: list[Level]    # sorted best-first (descending price)
    asks: list[Level]    # sorted best-first (ascending price)
    ts: datetime


class OrderStatus(str, Enum):
    Open            = "Open"
    PartiallyFilled = "PartiallyFilled"
    Filled          = "Filled"
    Cancelled       = "Cancelled"
    Rejected        = "Rejected"


@dataclass
class OrderRow:
    account: str
    symbol: str
    side: str            # "BUY" | "SELL"
    qty: float
    filled_qty: float
    limit_price: float
    status: OrderStatus
    placed_at: datetime
    last_update_at: datetime
    order_id: str = ""   # ATP internal reference; empty when unavailable
    last_price: float = 0.0   # current last price from Orders panel
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0
    tif: str = ""             # time in force (DAY, GTC, etc.)


# ── Protocols ──────────────────────────────────────────────────────────────

@runtime_checkable
class QuoteAdapter(Protocol):
    def get_quote(self, symbol: str) -> QuoteSnapshot: ...


@runtime_checkable
class Level2Adapter(Protocol):
    def get_level2(self, symbol: str) -> Level2Snapshot: ...


@runtime_checkable
class OrdersAdapter(Protocol):
    def get_orders(self) -> list[OrderRow]: ...


@dataclass
class WatchlistRow:
    symbol: str
    last: float
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    volume: int
    prev_close: float    # "Close" column (yesterday's close)
    avg_vol_10d: int     # "10D Avg Vol"
    avg_vol_90d: int     # "90D Avg Vol"
    div_ex_date: str     # "Div Ex-Date" raw string, "" if unavailable
    div_local: float     # "Div Local" dividend amount
    vwap: float          # "VWAP"
    ts: datetime
    pct_chg: float = 0.0        # "% Chg" — intraday % change
    open_price: float = 0.0     # "Open" — today's opening price
    ext_hrs_last: float = 0.0   # "Ext Hrs Last" — extended hours last price
    ext_hrs_pct_chg: float = 0.0  # "Ext Hrs % Chg" — extended hours % change


@runtime_checkable
class WatchlistAdapter(Protocol):
    def get_watchlist(self) -> dict[str, WatchlistRow]: ...
