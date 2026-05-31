"""
OCR-based adapters for Fidelity Trader+ data not accessible via UIA.

Uses rapidocr-onnxruntime (PaddleOCR models via ONNX — works on Python 3.14+,
no separate C++ runtime required beyond onnxruntime).

Algorithm:
  1. Capture the screen region of the UIA control (bounding rect is always
     available even when children are opaque).
  2. Run RapidOCR to get (bbox, text, confidence) for every text element.
  3. Cluster detections into rows by vertical center proximity.
  4. Within each row sort left-to-right to get ordered cell values.
  5. Identify the header row to calibrate column x-boundaries, then parse
     each data row into the target dataclass.

Install:
  pip install rapidocr-onnxruntime pillow
"""

from __future__ import annotations

import io
import os
from datetime import datetime, date, timezone, timedelta
from functools import lru_cache
from typing import NamedTuple

import numpy as np
from PIL import Image, ImageGrab

# Flip to True at runtime via enable_debug() to save images and print OCR hits
_DEBUG = False


def enable_debug() -> None:
    """Call before adapters run to enable image saving and OCR hit printing."""
    global _DEBUG
    _DEBUG = True


from adapters import Level, Level2Snapshot, OrderRow, OrderStatus
from adapters._atp_connect import get_app, with_retry
from adapters._atp_parse import parse_price, parse_size
from adapters._atp_ui import get_panel_container, sv_children

# ── OCR engine (singleton) ────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _ocr_engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR(det_model_path=None, det_limit_side_len=2400, det_limit_type="max")


# ── Screenshot helpers ─────────────────────────────────────────────────────


def _capture_full_window() -> np.ndarray:
    """
    Capture the Fidelity Trader+ window using PrintWindow(PW_RENDERFULLCONTENT=2).

    PrintWindow with flag=2 captures the actual window buffer regardless of z-order,
    which is required for:
    - WinUI/MAUI apps that render via DirectX/GPU (content invisible to ImageGrab
      when another window is on top)
    - Windows that are maximised but behind the calling terminal
    """
    import ctypes
    import win32gui
    import win32ui

    app = get_app()
    hwnd = app.top_window().handle

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mem_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mem_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mem_dc, w, h)
    save_dc.SelectObject(bmp)

    # PW_RENDERFULLCONTENT = 2  →  captures DWM-composited / GPU-rendered frames
    ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)

    raw = bmp.GetBitmapBits(True)  # bytes in BGRA order
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
    arr = arr[:, :, [2, 1, 0]].copy()  # BGRA → RGB

    win32gui.DeleteObject(bmp.GetHandle())
    save_dc.DeleteDC()
    mem_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)

    if _DEBUG:
        Image.fromarray(arr).save("debug_full_window.png")
        print(f"[OCR DEBUG] PrintWindow capture {w}x{h}px -> debug_full_window.png")
    return arr


def _run_ocr(img: np.ndarray, label: str = "ocr") -> list[_Cell]:
    """Run RapidOCR and return _Cell list; saves debug image and prints hits if enabled."""
    if _DEBUG:
        Image.fromarray(img).save(f"debug_{label}.png")
        print(
            f"[OCR DEBUG] {label}: image {img.shape[1]}x{img.shape[0]}px"
            f" -> debug_{label}.png"
        )
    ocr = _ocr_engine()
    result, _ = ocr(img)
    cells = _ocr_to_cells(result or [])
    if _DEBUG:
        print(f"[OCR DEBUG] {label}: {len(cells)} detections")
        for c in cells:
            print(f"  x={c.x:6.0f}  y={c.y:6.0f}  {c.text!r}")
    return cells


def _crop_section(
    img: np.ndarray,
    cells: list[_Cell],
    start_label: str,
    end_label: str | None,
    pad_top: int = 0,
    pad_bottom: int = 400,
) -> np.ndarray:
    """
    Crop *img* vertically to the section bounded by OCR-detected labels.
    start_label: text that marks the top of the section (e.g. 'Level 2').
    end_label:   text that marks the bottom (None = use pad_bottom from start).
    """
    start_y: float | None = None
    end_y: float | None = None

    for cell in cells:
        if start_label.lower() in cell.text.lower() and start_y is None:
            start_y = cell.y
        if end_label and end_label.lower() in cell.text.lower() and start_y is not None:
            if cell.y > start_y + 10:  # must be below start
                end_y = cell.y
                break

    if start_y is None:
        return img  # can't find section; return full image

    h = img.shape[0]
    top = max(0, int(start_y) - pad_top)
    bottom = min(h, int(end_y) if end_y else int(start_y) + pad_bottom)
    return img[top:bottom, :]


# ── Row clustering ────────────────────────────────────────────────────────


class _Cell(NamedTuple):
    x: float  # horizontal center
    y: float  # vertical center
    text: str


def _ocr_to_cells(result) -> list[_Cell]:
    """Flatten RapidOCR result into a list of _Cell."""
    cells: list[_Cell] = []
    if not result:
        return cells
    for item in result:
        # RapidOCR: item = (bbox, text, score)
        # bbox shape: 4 x 2  [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]
        try:
            bbox, text, _score = item
            pts = np.array(bbox, dtype=float)
            xc = pts[:, 0].mean()
            yc = pts[:, 1].mean()
            t = str(text).strip()
            if t:
                cells.append(_Cell(x=xc, y=yc, text=t))
        except Exception:
            continue
    return cells


def _cluster_rows(cells: list[_Cell], y_tol: int = 8) -> list[list[_Cell]]:
    """
    Group cells into rows by vertical proximity, then sort each row left→right.
    y_tol: max pixel distance between two cells to be considered the same row.
    """
    if not cells:
        return []
    cells = sorted(cells, key=lambda c: c.y)
    rows: list[list[_Cell]] = []
    cur: list[_Cell] = [cells[0]]
    cur_y = cells[0].y

    for cell in cells[1:]:
        if abs(cell.y - cur_y) <= y_tol:
            cur.append(cell)
            cur_y = sum(c.y for c in cur) / len(cur)
        else:
            rows.append(sorted(cur, key=lambda c: c.x))
            cur = [cell]
            cur_y = cell.y

    rows.append(sorted(cur, key=lambda c: c.x))
    return rows


def _texts(row: list[_Cell]) -> list[str]:
    return [c.text for c in row]


# ── Column calibration ────────────────────────────────────────────────────


def _find_col_x(rows: list[list[_Cell]], header_labels: list[str]) -> dict[str, float]:
    """
    Find the x-center of each named column by scanning for a header row.
    Returns {label: x_center} for matched labels.
    """
    norm = [h.lower() for h in header_labels]
    for row in rows:
        txts = [c.text.lower() for c in row]
        matches = sum(1 for h in norm if any(h in t for t in txts))
        if matches >= len(header_labels) // 2:
            result: dict[str, float] = {}
            for cell in row:
                for label in header_labels:
                    if label.lower() in cell.text.lower():
                        result[label] = cell.x
            return result
    return {}


def _assign_col(x: float, col_xs: list[float], col_names: list[str]) -> int:
    """Return the index of the closest column for a given x-coordinate."""
    if not col_xs:
        return 0
    return min(range(len(col_xs)), key=lambda i: abs(col_xs[i] - x))


# ── L2 parsing ────────────────────────────────────────────────────────────
# Expected header: Exch | Size | Bid | Ask | Size | Exch
_L2_HEADERS = ["Exch", "Size", "Bid", "Ask"]
_L2_SKIP_TEXTS = {
    "exch",
    "size",
    "bid",
    "ask",
    "level 2",
    "level2",
    "export",
    "search",
    "edit",
    "totals",
    "total",
}


def _is_l2_header(row: list[_Cell]) -> bool:
    low = {c.text.lower() for c in row}
    return bool(low & {"bid", "ask", "exch"})


def _parse_l2_from_rows(
    rows: list[list[_Cell]], img_width: int
) -> tuple[list[Level], list[Level]]:
    bids: list[Level] = []
    asks: list[Level] = []

    # Estimate midpoint between bid and ask columns from header, or use image center
    col_xs = _find_col_x(rows, _L2_HEADERS)
    mid_x = img_width / 2
    if "Bid" in col_xs and "Ask" in col_xs:
        mid_x = (col_xs["Bid"] + col_xs["Ask"]) / 2

    # Skip rows before the first header — panel title/price display is not data
    header_seen = False
    for row in rows:
        if _is_l2_header(row):
            header_seen = True
            continue
        if not header_seen:
            continue
        filtered = [c for c in row if c.text.strip().lower() not in _L2_SKIP_TEXTS]
        txts = _texts(filtered)
        if len(txts) < 2:
            continue

        # Split row cells into bid side (x < mid_x) and ask side (x >= mid_x)
        bid_cells = [c for c in filtered if c.x < mid_x]
        ask_cells = [c for c in filtered if c.x >= mid_x]

        def _extract_side(cells: list[_Cell]) -> tuple[float, int, str]:
            """Return (price, size, mpid) from a set of cells on one side."""
            price = size = 0.0
            mpid = ""
            for c in cells:
                v = c.text.replace(",", "").strip()
                try:
                    f = float(v)
                    if 0.01 < f < 100_000 and "." in c.text:
                        price = f
                    elif f >= 1 and "." not in c.text:
                        size = int(f)
                except ValueError:
                    # Non-numeric → exchange code
                    if v and not v.startswith(("+", "-", "(")):
                        mpid = v
            return price, int(size), mpid

        bp, bs, be = _extract_side(bid_cells)
        ap, as_, ae = _extract_side(ask_cells)

        if bp > 0 and bs > 0:
            bids.append(Level(price=bp, size=bs, mpid=be))
        if ap > 0 and as_ > 0:
            asks.append(Level(price=ap, size=as_, mpid=ae))

    return bids, asks


def _find_l2_grid_ctrl(controls: list, symbol: str):
    """Last Custom control in the L2 panel section (the RadMauiScrollView)."""
    sym = symbol.upper()
    in_l2 = False
    last_custom = None
    for ctrl in controls:
        try:
            ctype = ctrl.element_info.control_type
            text = ctrl.window_text().strip()
        except Exception:
            continue
        if ctype == "Edit" and text.upper() == sym:
            in_l2 = True
        if in_l2 and ctype == "Text" and text == "Level 2":
            in_l2 = True  # confirmed
        if in_l2 and ctype == "Custom":
            last_custom = ctrl
        if in_l2 and ctype == "Text" and text == "Exch":
            break
    return last_custom


_L2_TICKER_RE = __import__("re").compile(r"^[A-Z]{1,6}$")


def _find_l2_panels(full: np.ndarray) -> dict[str, dict]:
    """
    Identify L2 panel bounding boxes by OCR-ing top-right and bottom-right
    quadrants separately.  Full-image OCR misses small panel title text due
    to downscaling; quadrant crops maintain enough resolution.

    Returns {SYMBOL: {'x_min', 'x_max', 'data_y', 'quadrant': 'top'|'bottom'}}.
    """
    img_h, img_w = full.shape[:2]
    mid_y, mid_x = img_h // 2, img_w // 2
    ocr = _ocr_engine()

    panels: dict[str, dict] = {}

    for q_name, y_off, crop in [
        ("top", 0, full[:mid_y, mid_x:, :]),
        ("bottom", mid_y, full[mid_y:, mid_x:, :]),
    ]:
        result, _ = ocr(crop)
        cells = [
            _Cell(c.x + mid_x, c.y + y_off, c.text) for c in _ocr_to_cells(result or [])
        ]

        l2_labels = [c for c in cells if "level" in c.text.lower() and "2" in c.text]
        if not l2_labels:
            continue

        l2_labels.sort(key=lambda c: c.x)
        row_panels: list[tuple[str, float, float]] = []  # (ticker, l2_x, l2_y)

        for lbl in l2_labels:
            near = [
                c
                for c in cells
                if _L2_TICKER_RE.match(c.text.strip().upper())
                and len(c.text.strip()) >= 2
                and abs(c.y - lbl.y) < 20
                and lbl.x - 400 < c.x < lbl.x + 50
                and c.text.strip().upper() not in ("LEVEL",)
            ]
            if near:
                best = min(near, key=lambda c: abs(c.x - lbl.x))
                row_panels.append((best.text.strip().upper(), lbl.x, lbl.y))

        for i, (sym, x, y) in enumerate(row_panels):
            x_min = mid_x if i == 0 else (row_panels[i - 1][1] + x) / 2
            x_max = (
                img_w if i == len(row_panels) - 1 else (x + row_panels[i + 1][1]) / 2
            )
            panels[sym] = {
                "x_min": x_min,
                "x_max": x_max,
                "data_y": y + 60,
                "quadrant": q_name,
            }

    return panels


def _read_l2_ocr(symbol: str) -> Level2Snapshot:
    full = _capture_full_window()
    img_h, img_w = full.shape[:2]
    mid_y, mid_x = img_h // 2, img_w // 2

    panels = _find_l2_panels(full)
    sym = symbol.upper()

    if sym not in panels:
        available = sorted(panels.keys())
        raise LookupError(
            f"No Level 2 panel open for '{sym}'. "
            f"Open panels: {', '.join(available) if available else 'none detected'}"
        )

    panel = panels[sym]
    if panel["quadrant"] == "top":
        y_off = 0
        full_half = full[:mid_y, :, :]
    else:
        y_off = mid_y
        full_half = full[mid_y:, :, :]

    # Crop tightly to this panel x-range and scale 2x before OCR.
    # The L2 Size column contains single-digit values (~8px wide) that fall
    # below the OCR detection threshold at native resolution.  Scaling 2x
    # brings them to ~16px, well above the minimum detectable size, without
    # downscaling (panel crop ~620x1055 -> 1240x2110, under det_limit_side_len=2400).
    px0 = max(0, int(panel["x_min"]))
    px1 = min(full_half.shape[1], int(panel["x_max"]))
    panel_crop = full_half[:, px0:px1, :]
    scale = 2
    panel_crop = np.repeat(np.repeat(panel_crop, scale, axis=0), scale, axis=1)

    if _DEBUG:
        ph, pw = panel_crop.shape[:2]
        Image.fromarray(panel_crop).save(f"debug_l2_{sym}_crop.png")
        print(f"[L2 DEBUG] {sym} panel x={px0}..{px1} scaled {pw}x{ph}px (2x)")

    ocr = _ocr_engine()
    result, _ = ocr(panel_crop)
    # Divide scaled coordinates by 2 then offset to full-image space
    all_cells = [
        _Cell(c.x / scale + px0, c.y / scale + y_off, c.text)
        for c in _ocr_to_cells(result or [])
    ]

    # Filter to below the data header row
    panel_cells = [c for c in all_cells if c.y >= panel["data_y"]]

    if _DEBUG:
        print(
            f"[L2 DEBUG] {sym} data_y={panel['data_y']:.0f}  {len(panel_cells)} cells"
        )
        for c in sorted(panel_cells, key=lambda c: (c.y, c.x)):
            print(f"  x={c.x:7.0f}  y={c.y:6.0f}  {c.text!r}")

    rows = _cluster_rows(panel_cells)
    # img_width/2 is the fallback bid/ask split; pass 2*panel_center so the
    # fallback lands at the panel's x-midpoint.
    panel_mid_x2 = int(panel["x_min"] + panel["x_max"])
    bids, asks = _parse_l2_from_rows(rows, img_width=panel_mid_x2)

    bids = sorted(bids, key=lambda l: l.price, reverse=True)
    asks = sorted(asks, key=lambda l: l.price)

    if not bids and not asks:
        raise RuntimeError(
            f"OCR found no Level 2 rows for '{sym}'. "
            "Ensure the Level 2 panel has live data (not all zeroes)."
        )

    return Level2Snapshot(
        symbol=sym,
        bids=bids,
        asks=asks,
        ts=datetime.now(tz=timezone.utc),
    )


class OCRLevel2Adapter:
    """Reads Level 2 depth via RapidOCR (PaddleOCR models, no GPU required)."""

    def get_level2(self, symbol: str) -> Level2Snapshot:
        return with_retry(lambda: _read_l2_ocr(symbol), label=f"OCR L2 {symbol}")


def enumerate_l2_symbols() -> set[str]:
    """Return the set of ticker symbols currently visible in FT+ L2 panels."""
    full = _capture_full_window()
    return set(_find_l2_panels(full).keys())


# ── Orders parsing ────────────────────────────────────────────────────────
# Key columns we care about (others are ignored)
_ORDER_HEADERS = [
    "Symbol",
    "Action",
    "Amount",
    "Order Type",
    "Status",
    "Filled",
    "Last",
    "$Chg",
    "%Chg",
    "Bid",
    "Account",
    "Mid",
    "Ask",
    "TIF",
    "Conditions",
    "Destination",
    "Order Time",
]
_STATUS_MAP: dict[str, OrderStatus] = {
    "open": OrderStatus.Open,
    "partiallyfilled": OrderStatus.PartiallyFilled,
    "partial": OrderStatus.PartiallyFilled,
    "filled": OrderStatus.Filled,
    "cancelled": OrderStatus.Cancelled,
    "canceled": OrderStatus.Cancelled,
    "rejected": OrderStatus.Rejected,
}
_TICKER_RE = __import__("re").compile(r"^[A-Z]{1,6}(\.\w+)?$")
_LIMIT_RE = __import__("re").compile(r"\$?\s*([\d,]+\.?\d*)")


def _is_order_header(row: list[_Cell]) -> bool:
    low = {c.text.lower() for c in row}
    return bool(low & {"symbol", "action", "status", "amount"})


def _find_orders_grid_ctrl(controls: list):
    found_orders = False
    for ctrl in controls:
        try:
            ctype = ctrl.element_info.control_type
            text = ctrl.window_text().strip()
        except Exception:
            continue
        if ctype == "Text" and text == "Orders" and not found_orders:
            found_orders = True
        elif found_orders and ctype == "Custom":
            return ctrl
    return None


def _parse_orders_from_rows(rows: list[list[_Cell]]) -> list[OrderRow]:
    # Find header to calibrate column x-positions
    col_xs: list[float] = []
    col_names: list[str] = []
    for row in rows:
        if _is_order_header(row):
            col_xs = [c.x for c in row]
            col_names = [c.text for c in row]
            break

    order_rows: list[OrderRow] = []
    for row in rows:
        if _is_order_header(row):
            continue
        txts = _texts(row)
        if not txts:
            continue

        if col_xs:
            # Map each cell to its nearest header column
            row_dict: dict[str, str] = {}
            for cell in row:
                idx = _assign_col(cell.x, col_xs, col_names)
                col = col_names[idx] if idx < len(col_names) else str(idx)
                row_dict[col] = cell.text
        else:
            # Fallback: positional (Symbol=0, Action=1, Amount=2, OrderType=3, Status=4, ...)
            names = [
                "Symbol",
                "Action",
                "Amount",
                "Order Type",
                "Status",
                "Filled",
                "Last",
                "$Chg",
                "%Chg",
                "Bid",
                "Account",
                "Mid",
                "Ask",
                "TIF",
                "Conditions",
                "Destination",
                "Order Time",
            ]
            row_dict = {names[i]: txts[i] for i in range(min(len(names), len(txts)))}

        symbol = row_dict.get("Symbol", "").strip().upper()
        if not symbol or not _TICKER_RE.match(symbol):
            continue

        side = row_dict.get("Action", "BUY").upper()
        qty = float(parse_size(row_dict.get("Amount", "0")) or 0)
        status_txt = row_dict.get("Status", "open").lower().replace(" ", "")
        status = _STATUS_MAP.get(status_txt, OrderStatus.Open)

        filled_qty = float(parse_size(row_dict.get("Filled", "0")) or 0)

        order_type = row_dict.get("Order Type", "")
        m = _LIMIT_RE.search(order_type)
        limit_price = parse_price(m.group(1)) if m else 0.0

        account = row_dict.get("Account", "")
        placed_at = datetime.now(tz=timezone.utc)

        last_px = parse_price(row_dict.get("Last", "0"))
        bid_px = parse_price(row_dict.get("Bid", "0"))
        ask_px = parse_price(row_dict.get("Ask", "0"))
        mid_px = parse_price(row_dict.get("Mid", "0"))
        tif = row_dict.get("TIF", "").strip()

        order_rows.append(
            OrderRow(
                account=account,
                symbol=symbol,
                side=side,
                qty=qty,
                filled_qty=filled_qty,
                limit_price=limit_price,
                status=status,
                placed_at=placed_at,
                last_update_at=placed_at,
                last_price=last_px,
                bid=bid_px,
                ask=ask_px,
                mid=mid_px,
                tif=tif,
            )
        )

    return order_rows


def _read_orders_ocr() -> list[OrderRow]:
    full = _capture_full_window()
    all_cells = _run_ocr(full, label="orders_full")

    # Find the Orders column-header row by locating the "Symbol" header cell.
    # The data rows follow immediately below; stop ~300px down to avoid
    # picking up the L2 panel that starts further below.
    symbol_headers = [c for c in all_cells if c.text.lower() == "symbol"]
    if not symbol_headers:
        return []  # orders panel not visible

    header_y = min(c.y for c in symbol_headers)
    orders_cells = [c for c in all_cells if header_y - 5 <= c.y <= header_y + 300]

    rows = _cluster_rows(orders_cells)
    return _parse_orders_from_rows(rows)


class OCROrdersAdapter:
    """Reads order rows via RapidOCR screenshot of the Orders panel."""

    def get_orders(self) -> list[OrderRow]:
        return with_retry(_read_orders_ocr, label="OCR Orders")
