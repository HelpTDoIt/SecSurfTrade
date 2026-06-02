"""
F-4a: ATP Orders OCR parser — pattern-based row extraction.

These cases are built from the REAL FT+ Orders-grid OCR structure captured live
on 2026-06-02 (the live LT-1 smoke), with the account mask sanitized to *0000
and synthetic order IDs — no real account data is committed.

The FT+ Orders grid renders NO visible "Symbol" column header, so the old
header-anchored parser returned zero rows. The new parser identifies and parses
each order row by per-cell semantics (ticker, Buy/Sell, "N / M" fill fraction,
status text, "Limit at $X", account mask, rightmost order-id token).

Each tuple below is (x_center, text) for one clustered row; `_row(...)` turns it
into the `_Cell` list the parser consumes.
"""

from __future__ import annotations

from adapters import OrderStatus
from adapters.atp_ocr import (
    _Cell,
    _extract_order_from_row,
    _orders_crop_box,
    _parse_orders_from_rows,
)


def _row(cells: list[tuple[float, str]], y: float = 1000.0) -> list[_Cell]:
    return [_Cell(x=x, y=y, text=t) for x, t in cells]


# Real captured layout (sanitized). Columns:
# symbol | side | amount | "filled / total" | status | "Limit at $X" | account
# | order-time | bid | ask | last | $chg | value | TIF | order-id
FILLED_BUY = [
    (210, "DFIV"),
    (310, "Buy"),
    (412, "100"),
    (476, "100 / 100"),
    (584, "Filled at $55.93"),
    (842, "Limit at $55.93"),
    (990, "Test IRA *0000"),
    (1184, "12:06:00 PM ET Jun-02-2026"),
    (1344, "55.84"),
    (1536, "55.85"),
    (1635, "55.835"),
    (1709, "0.71"),
    (1798, "$5,593.00"),
    (1867, "GTC"),
    (1939, "27AAAA01"),
]
CANCELED_NOFILL = [
    (210, "DFIV"),
    (309, "Buy"),
    (412, "100"),
    (594, "Verified Canceled"),
    (842, "Limit at $55.92"),
    (991, "Test IRA *0000"),
    (1184, "12:09:08 PM ET Jun-02-2026"),
    (1813, "$0.00"),
    (1867, "GTC"),
    (1940, "27BBBB02"),
]
CANCELED_PARTIAL = [
    (212, "AVUV"),
    (310, "Buy"),
    (412, "100"),
    (467, "1 / 100"),
    (650, "Verified Cancelled/Partially Filled"),
    (846, "Limit at $121.33"),
    (991, "Test IRA *0000"),
    (1803, "$121.33"),
    (1867, "GTC"),
    (1936, "27CCCC03"),
]
OPEN_SELL = [
    (210, "QQQ"),
    (310, "Sell"),
    (412, "35"),
    (467, "0 / 35"),
    (588, "Open"),
    (846, "Limit at $750.00"),
    (990, "Test IRA *0000"),
    (1867, "GTC"),
    (1940, "27DDDD04"),
]
FILLED_NOSPACE = [
    (210, "ICLN"),
    (310, "Buy"),
    (411, "623"),
    (476, "623/623"),
    (584, "Filled at $23.45"),
    (842, "Limit at $23.45"),
    (991, "Test IRA *0000"),
    (1798, "$14,609.35"),
    (1868, "GTC"),
    (1938, "27EEEE05"),
]


def test_filled_buy_with_fraction():
    o = _extract_order_from_row(_row(FILLED_BUY))
    assert o is not None
    assert o.symbol == "DFIV"
    assert o.side == "BUY"
    assert o.qty == 100
    assert o.filled_qty == 100
    assert o.limit_price == 55.93
    assert o.status == OrderStatus.Filled
    assert o.order_id == "27AAAA01"
    assert o.account == "Test IRA *0000"


def test_canceled_with_no_fill_uses_amount_column():
    o = _extract_order_from_row(_row(CANCELED_NOFILL))
    assert o is not None
    assert o.symbol == "DFIV"
    assert o.qty == 100  # from the plain amount cell (no "N / M" present)
    assert o.filled_qty == 0
    assert o.status == OrderStatus.Cancelled
    assert o.limit_price == 55.92
    assert o.order_id == "27BBBB02"


def test_canceled_after_partial_fill_is_terminal_cancelled():
    # "Verified Cancelled/Partially Filled": cancel wins over the partial substring,
    # but the 1/100 fill is still captured.
    o = _extract_order_from_row(_row(CANCELED_PARTIAL))
    assert o is not None
    assert o.status == OrderStatus.Cancelled
    assert o.filled_qty == 1
    assert o.qty == 100
    assert o.limit_price == 121.33


def test_open_sell_order():
    o = _extract_order_from_row(_row(OPEN_SELL))
    assert o is not None
    assert o.symbol == "QQQ"
    assert o.side == "SELL"
    assert o.qty == 35
    assert o.filled_qty == 0
    assert o.status == OrderStatus.Open
    assert o.limit_price == 750.00
    assert o.order_id == "27DDDD04"


def test_filled_fraction_without_spaces():
    o = _extract_order_from_row(_row(FILLED_NOSPACE))
    assert o is not None
    assert o.qty == 623
    assert o.filled_qty == 623
    assert o.status == OrderStatus.Filled


def test_non_order_fragment_rejected():
    # A header/overflow fragment (status + time, no ticker, no account) -> None.
    frag = _row([(584, "Filled at $55.93"), (1184, "12:09:53 PM ET Jun-02-2026")])
    assert _extract_order_from_row(frag) is None


def test_row_without_status_or_account_rejected():
    # A bare ticker+price row (e.g. an L2/watchlist line) -> None.
    l2 = _row([(210, "AVUV"), (412, "121.29"), (600, "100")])
    assert _extract_order_from_row(l2) is None


def test_panel_chrome_row_rejected():
    # F-4b: the Orders panel's tab/title + account-dropdown row now falls inside
    # the second-pass crop. It matches a ticker-shaped word ("Orders") and an
    # account mask, but has no order-type cell and no order-id -> must reject.
    chrome = _row(
        [(354, "Orders"), (436, "Saved orders"), (177, "Rollover I... *6131")]
    )
    assert _extract_order_from_row(chrome) is None


def test_market_order_without_limit_is_accepted():
    # A Market order has no "Limit at" cell; the "Market" order-type + order-id
    # must still satisfy the identity guard.
    mkt = _row(
        [
            (210, "DFIV"),
            (310, "Buy"),
            (412, "1"),
            (476, "1 / 1"),
            (584, "Filled at $55.93"),
            (814, "Market"),
            (991, "Test IRA *0000"),
            (1939, "27FFFF06"),
        ]
    )
    o = _extract_order_from_row(mkt)
    assert o is not None
    assert o.symbol == "DFIV"
    assert o.order_id == "27FFFF06"
    assert o.status == OrderStatus.Filled


def test_parse_orders_from_rows_skips_headers_and_fragments():
    header = _row([(210, "Symbol"), (310, "Action"), (584, "Status")])
    frag = _row([(584, "Filled at $1.00")])
    rows = [header, _row(FILLED_BUY), frag, _row(OPEN_SELL)]
    orders = _parse_orders_from_rows(rows)
    assert [o.order_id for o in orders] == ["27AAAA01", "27DDDD04"]


# ── F-4b: Orders-panel crop-box locator ─────────────────────────────────────
# _orders_crop_box derives the second-pass crop from the order-id cells that
# survive the (downscaled) first full-window OCR pass. Real capture is
# 3862x2110; the order-id column sits at x~1920.


def _cell(x: float, y: float, text: str) -> _Cell:
    return _Cell(x=x, y=y, text=text)


def test_crop_box_from_detected_order_ids():
    # Lower rows survive pass 1 at the id column (~1920); upper rows are missing.
    cells = [_cell(1928, y, oid) for y, oid in [(1066, "27D1KLY9"), (1796, "27D05K9B")]]
    # Distractors that must NOT move the right/bottom edge:
    cells += [
        _cell(50, 200, "AVUV"),  # watchlist ticker, far left
        _cell(2363, 1355, "27ZZZZ9"),  # L2/ticket col, beyond grid boundary
        _cell(1849, 1066, "GTC"),  # TIF token (not id-shaped)
        _cell(1790, 1066, "$5,593.00"),  # value cell (not id-shaped)
        _cell(1182, 1066, "12:09:53PMET"),  # order-time (not id-shaped)
    ]
    box = _orders_crop_box(cells, img_w=3862, img_h=2110)
    assert box is not None
    x0, y0, x1, y1 = box
    assert (x0, y0) == (0, 0)  # left-docked grid, short history → top of window
    assert x1 == 1928 + 110  # right edge from the id column, not the L2 col
    assert y1 == 1796 + 40  # bottom edge from the lowest detected order row


def test_crop_box_none_when_no_order_rows():
    # Only watchlist + L2 noise, no id-shaped cell in the grid x-band → None.
    cells = [
        _cell(50, 200, "AVUV"),
        _cell(2363, 1355, "27ZZZZ9"),  # id-shaped but beyond grid boundary
        _cell(1849, 1066, "GTC"),
    ]
    assert _orders_crop_box(cells, img_w=3862, img_h=2110) is None


def test_crop_box_tall_history_anchors_to_bottom():
    # A long order history: the lowest id is far down the window. The crop must
    # anchor at the bottom and trim its height so it never re-triggers downscale.
    cells = [_cell(1920, 600, "27D1AAA1"), _cell(1920, 2600, "27D0ZZZ9")]
    box = _orders_crop_box(cells, img_w=3862, img_h=3000)
    assert box is not None
    x0, y0, x1, y1 = box
    assert y1 == 2600 + 40
    assert y0 == (2600 + 40) - 2300  # _ORDERS_CROP_MAX_H trim, bottom-anchored
    assert y1 - y0 == 2300
