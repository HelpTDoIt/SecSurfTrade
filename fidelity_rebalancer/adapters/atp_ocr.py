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
import logging
import os
import re
import time
from datetime import datetime, date, timezone, timedelta
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

_log = logging.getLogger(__name__)

import numpy as np
from PIL import Image, ImageGrab

# Flip to True at runtime via enable_debug() to save images and print OCR hits.
# _DEBUG_DIR: where timestamped PNGs are written (None → current directory).
_DEBUG = False
_DEBUG_DIR: Path | None = None


def enable_debug(save_dir: Path | str | None = None) -> None:
    """Enable image saving and OCR hit printing.

    Args:
        save_dir: Directory to write timestamped PNG snapshots.  Created if it
                  does not exist.  Defaults to the current working directory.
    """
    global _DEBUG, _DEBUG_DIR
    _DEBUG = True
    if save_dir is not None:
        _DEBUG_DIR = Path(save_dir).resolve().resolve()
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def _debug_save(img, label: str) -> None:
    """Save *img* (numpy array or PIL Image) to the debug directory with a timestamp."""
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"{ts}_{label}.png"
    dest = (_DEBUG_DIR / fname) if _DEBUG_DIR else Path(fname).resolve().resolve()
    if hasattr(img, "save"):
        img.save(dest)
    else:
        Image.fromarray(img).save(dest)
    return dest


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

    If the window is minimised Windows parks it at an off-screen position and stops
    maintaining a full GPU render buffer — PrintWindow returns a tiny (≈158×26px)
    thumbnail.  OCR on a thumbnail finds the title-bar text but no order rows, so
    _orders_crop_box returns None and get_orders() silently returns [].  We restore
    the window first so DWM composites a full frame before we capture.
    """
    import ctypes
    import win32gui
    import win32ui
    import win32con

    app = get_app()
    try:
        hwnd = app.top_window().handle
    except Exception:
        # top_window() raises when FT+ is minimised / parked off-screen because
        # pywinauto considers those windows "not visible".  windows() enumerates
        # ALL top-level windows for the process regardless of minimised state.
        wins = app.windows()
        if not wins:
            raise RuntimeError(
                "FT+ has no accessible windows — "
                "ensure Fidelity Trader+ is running and logged in."
            )
        hwnd = wins[0].handle

    # Restore window if minimised or parked off-screen.  ShowWindow(SW_RESTORE)
    # is idempotent when the window is already normal/maximised.
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.5)  # let DWM composite a fresh frame

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top

    # Guard: a suspiciously small window means we captured a thumbnail, not a full
    # render.  Raise so with_retry fires rather than silently returning no orders.
    if w < 400 or h < 200:
        raise RuntimeError(
            f"FT+ window captured at {w}×{h}px — too small to contain an Orders "
            "panel; window may still be restoring from minimised state"
        )

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
        dest = _debug_save(arr, "full_window")
        _log.debug("[OCR DEBUG] PrintWindow capture %dx%dpx -> %s", w, h, dest)
    return arr


def _run_ocr(img: np.ndarray, label: str = "ocr") -> list[_Cell]:
    """Run RapidOCR and return _Cell list; saves debug image and prints hits if enabled."""
    if _DEBUG:
        dest = _debug_save(img, label)
        _log.debug("[OCR DEBUG] %s: image %dx%dpx -> %s", label, img.shape[1], img.shape[0], dest)
    ocr = _ocr_engine()
    result, _ = ocr(img)
    cells = _ocr_to_cells(result or [])
    _log.debug("[OCR DEBUG] %s: %d detections", label, len(cells))
    for c in cells:
        _log.debug("  x=%6.0f  y=%6.0f  %r", c.x, c.y, c.text)
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
    Identify L2 panel bounding boxes by OCR-ing the window in quadrants.

    Full-image OCR misses small panel title text due to downscaling, so we OCR
    the left and right halves of each vertical band separately (preserving
    resolution) and merge the detected cells before locating panels.  This
    covers the FULL window width — an L2 panel whose "Level 2 <SYM>" title sits
    in the LEFT half is detected just as reliably as one on the right.

    Returns {SYMBOL: {'x_min', 'x_max', 'data_y', 'quadrant': 'top'|'bottom'}}.
    """
    img_h, img_w = full.shape[:2]
    mid_y, mid_x = img_h // 2, img_w // 2
    ocr = _ocr_engine()

    panels: dict[str, dict] = {}

    bands = [("top", 0, mid_y), ("bottom", mid_y, img_h)]
    for q_name, y0, y1 in bands:
        # OCR left and right halves of the band separately to keep small title
        # text above the OCR detection threshold, then merge into one cell set.
        cells: list[_Cell] = []
        for x_off, x0, x1 in [(0, 0, mid_x), (mid_x, mid_x, img_w)]:
            result, _ = ocr(full[y0:y1, x0:x1, :])
            cells.extend(
                _Cell(c.x + x_off, c.y + y0, c.text)
                for c in _ocr_to_cells(result or [])
            )

        l2_labels = [c for c in cells if "level" in c.text.lower() and "2" in c.text]
        if not l2_labels:
            continue

        l2_labels.sort(key=lambda c: c.x)
        row_panels: list[tuple[str, float, float]] = []  # (ticker, l2_x, l2_y)
        seen: set[str] = set()

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
                sym = best.text.strip().upper()
                # A title near the mid-x seam can be picked up in both halves;
                # keep only the first occurrence per symbol within this band.
                if sym in seen:
                    continue
                seen.add(sym)
                row_panels.append((sym, lbl.x, lbl.y))

        for i, (sym, x, y) in enumerate(row_panels):
            x_min = 0 if i == 0 else (row_panels[i - 1][1] + x) / 2
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
        dest = _debug_save(panel_crop, f"l2_{sym}_crop")
        _log.debug(
            "[L2 DEBUG] %s panel x=%d..%d scaled %dx%dpx (2x) -> %s",
            sym, px0, px1, pw, ph, dest,
        )

    ocr = _ocr_engine()
    result, _ = ocr(panel_crop)
    # Divide scaled coordinates by 2 then offset to full-image space
    all_cells = [
        _Cell(c.x / scale + px0, c.y / scale + y_off, c.text)
        for c in _ocr_to_cells(result or [])
    ]

    # Filter to below the data header row
    panel_cells = [c for c in all_cells if c.y >= panel["data_y"]]

    _log.debug(
        "[L2 DEBUG] %s data_y=%.0f  %d cells",
        sym, panel["data_y"], len(panel_cells),
    )
    for c in sorted(panel_cells, key=lambda c: (c.y, c.x)):
        _log.debug("  x=%7.0f  y=%6.0f  %r", c.x, c.y, c.text)

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
_TICKER_RE = re.compile(r"^[A-Z]{1,6}(\.\w+)?$")
_LIMIT_RE = re.compile(r"\$?\s*([\d,]+\.?\d*)")

# F-4a pattern anchors. The FT+ Orders grid in current builds renders NO visible
# column-header row (the old parser anchored on a "Symbol" header that does not
# exist, so it returned zero rows even though the panel was full of readable
# orders). Instead we identify and parse each order row by per-cell semantics.
_FILLED_FRAC_RE = re.compile(
    r"(\d[\d,]*)\s*/\s*(\d[\d,]*)"
)  # "66 / 100" -> filled/total
_ORDERID_RE = re.compile(r"^[A-Z\d][0-9A-Z]{5,9}$")  # e.g. "27D1N8R8" or "F038HKMM"
_ACCT_RE = re.compile(r"\*\s*\d{3,5}")  # account masks like "*6131"
_INT_RE = re.compile(r"^\d[\d,]*$")  # a plain integer share-count cell
_TIF_SET = {"GTC", "DAY", "GTD", "FOK", "IOC", "EXT", "GTX", "OPG"}
# x-coordinate boundary separating the left Orders grid from the right-hand
# Level-2 / watchlist panel. Cells beyond this are dropped BEFORE row clustering
# so a watchlist ticker at the same y doesn't merge into an order row.
# NOTE (F-4): layout/resolution-specific — revisit if the FT+ window layout changes.
_ORDERS_GRID_MAX_X = 2050.0
# F-4b: max height (px) of the Orders-panel crop fed to the second OCR pass.
# Kept safely under the OCR engine's det_limit_side_len (2400) so the crop is
# never downscaled — that downscale is exactly what drops the top grid rows.
# For tall histories the crop is anchored at the grid's BOTTOM and trimmed to
# this height (covers ~70+ rows), so the most-recent orders are always read.
_ORDERS_CROP_MAX_H = 2300
# Status keywords (checked in priority order: cancel/reject are terminal and win
# over a partial-fill substring, e.g. "Verified Cancelled/Partially Filled").
_STATUS_KEYWORDS = ("cancel", "reject", "partial", "fill", "open", "working", "queue")


def _is_order_header(row: list[_Cell]) -> bool:
    low = {c.text.lower() for c in row}
    return bool(low & {"symbol", "action", "status", "amount"})


def _status_from_text(text: str) -> OrderStatus:
    """Map a free-text status cell to an OrderStatus (terminal states win)."""
    t = text.lower()
    if "cancel" in t:
        return OrderStatus.Cancelled
    if "reject" in t:
        return OrderStatus.Rejected
    if "partial" in t:
        return OrderStatus.PartiallyFilled
    if "fill" in t:  # "Filled at $X"
        return OrderStatus.Filled
    if "open" in t or "working" in t or "queue" in t:
        return OrderStatus.Open
    return OrderStatus.Open


def _extract_order_from_row(row: list[_Cell]) -> "OrderRow | None":
    """Extract one OrderRow from a clustered row of OCR cells by pattern.

    Returns None when the row is not a recognizable order (no ticker, or no
    status/account signal) — this is how header fragments, blank rows, and
    stray L2/watchlist cells get filtered out without a column-header anchor.
    """
    cells = sorted(row, key=lambda c: c.x)
    texts = [c.text.strip() for c in cells]

    # Symbol = leftmost ticker-shaped cell.
    symbol = ""
    for c in cells:
        t = c.text.strip().upper()
        if _TICKER_RE.match(t):
            symbol = t
            break
    if not symbol:
        return None

    # Side.
    side = "BUY"
    for t in texts:
        tl = t.lower()
        if tl == "buy" or tl.startswith("buy "):
            side = "BUY"
            break
        if tl == "sell" or tl.startswith("sell "):
            side = "SELL"
            break

    # Account (mask like "Rollover IRA *6131").
    account = ""
    for t in texts:
        tl = t.lower()
        if _ACCT_RE.search(t) or "ira" in tl or "individual" in tl or "roth" in tl:
            account = t
            break

    # Status.
    status: OrderStatus | None = None
    for t in texts:
        tl = t.lower()
        if any(k in tl for k in _STATUS_KEYWORDS):
            status = _status_from_text(t)
            break

    # A real order row must carry a status or an account; otherwise reject.
    if status is None and not account:
        return None

    # Limit price + order-type presence from the "Order Type" cell
    # ("Limit at $55.93" / "Market"). has_order_type doubles as an order-identity
    # signal below — panel chrome (tab titles, filter bar) has no order-type cell.
    limit_price = 0.0
    has_order_type = False
    for t in texts:
        tl = t.lower()
        if "limit" in tl:
            has_order_type = True
            m = _LIMIT_RE.search(t)
            if m:
                limit_price = parse_price(m.group(1)) or 0.0
            break
        if "market" in tl:
            has_order_type = True
            break

    # Quantity + filled. Prefer the "filled / total" fraction cell; otherwise
    # fall back to the plain share-count cell in the amount column (left of the
    # status column at x~580).
    qty = 0.0
    filled = 0.0
    frac = None
    for t in texts:
        m = _FILLED_FRAC_RE.search(t)
        if m:
            frac = m
            break
    if frac:
        filled = float(frac.group(1).replace(",", ""))
        qty = float(frac.group(2).replace(",", ""))
    else:
        for c in cells:
            t = c.text.strip()
            if c.x < 580 and _INT_RE.match(t):
                qty = float(t.replace(",", ""))
                break
        if status == OrderStatus.Filled:
            filled = qty

    # Order id = rightmost alnum token starting with a digit (the FT+ order #).
    order_id = ""
    for c in sorted(cells, key=lambda c: -c.x):
        t = c.text.strip().upper()
        if _ORDERID_RE.match(t) and t not in _TIF_SET and t != symbol:
            order_id = c.text.strip()
            break
    has_real_id = bool(order_id)
    if not order_id:  # synthetic stable-ish fallback when the id didn't OCR
        order_id = f"{symbol}-{side}-{limit_price:.2f}"

    # Order-identity guard (F-4b): a real order row carries an order-id token or
    # an order-type cell. Panel chrome that now falls inside the second-pass crop
    # — the "Orders"/"Saved orders" tabs, the account dropdown, the filter bar —
    # can match a ticker + account but never both of these, so reject it here.
    if not has_real_id and not has_order_type:
        return None

    if status is None:
        status = OrderStatus.Open

    placed_at = datetime.now(tz=timezone.utc)
    return OrderRow(
        account=account,
        symbol=symbol,
        side=side,
        qty=qty,
        filled_qty=filled,
        limit_price=limit_price,
        status=status,
        placed_at=placed_at,
        last_update_at=placed_at,
        order_id=order_id,
    )


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
    """Pattern-parse clustered OCR rows into OrderRows (F-4a).

    Header rows (if any) and non-order rows are skipped automatically by
    ``_extract_order_from_row`` returning None.
    """
    order_rows: list[OrderRow] = []
    for row in rows:
        if _is_order_header(row):
            continue
        order = _extract_order_from_row(row)
        if order is not None:
            order_rows.append(order)
    return order_rows


def _orders_crop_box(
    cells: list[_Cell], img_w: int, img_h: int
) -> tuple[int, int, int, int] | None:
    """
    F-4b: from a full-window OCR pass, derive a crop box around the Orders grid.

    The full window (~3800px wide) exceeds the OCR detector's size limit, so it is
    downscaled and the smallest/topmost grid text drops out. The order-id column
    (rightmost grid cell) is large enough that the *lower* rows still survive, and
    those are enough to locate the grid's right and bottom edges. The left edge is
    the window edge (the grid is left-docked) and the box is anchored at the grid
    BOTTOM, trimmed to ``_ORDERS_CROP_MAX_H`` so it never re-triggers a downscale.

    Returns ``(x0, y0, x1, y1)`` for ``img[y0:y1, x0:x1]``, or None if no order
    rows were detected (caller falls back to parsing the full-window cells).
    """
    id_cells = [
        c
        for c in cells
        if 1500 < c.x < _ORDERS_GRID_MAX_X and _ORDERID_RE.match(c.text)
    ]
    if not id_cells:
        return None
    x1 = min(img_w, int(max(c.x for c in id_cells)) + 110)
    y1 = min(img_h, int(max(c.y for c in id_cells)) + 40)
    y0 = max(0, y1 - _ORDERS_CROP_MAX_H)
    return (0, y0, x1, y1)


def _read_orders_ocr() -> list[OrderRow]:
    full = _capture_full_window()
    img_h, img_w = full.shape[:2]

    # Pass 1 (locate): OCR the whole window. An image this large is downscaled
    # below the detector's limit, so small grid text — the topmost order rows in
    # particular — drops out non-deterministically. But the lower rows that do
    # survive are enough to locate the grid's right/bottom bounds.
    locate_cells = _run_ocr(full, label="orders_locate")

    # An entirely empty locate pass means PrintWindow returned a blank buffer —
    # this happens transiently when FT+ is initialising or its GPU compositor
    # hasn't rendered a frame yet.  Raise so with_retry fires rather than
    # silently returning [] and masking the transient failure.
    if not locate_cells:
        raise RuntimeError(
            "OCR locate pass returned no cells — "
            "capture buffer may be blank (FT+ initialising or GPU flush pending)"
        )

    box = _orders_crop_box(locate_cells, img_w, img_h)

    if box is None:
        # OCR ran and found text, but no order-id-shaped cells in the grid
        # x-band — treat as a valid empty grid (no orders placed yet).
        grid_cells = [c for c in locate_cells if c.x < _ORDERS_GRID_MAX_X]
        return _parse_orders_from_rows(_cluster_rows(grid_cells))

    # Pass 2 (read): crop to the panel so its (small) text gets the full detector
    # budget at native resolution — every row, including the top of the grid, now
    # detects. The crop origin's x is 0, so its column x-coords already match the
    # full image; only y is offset back for clustering/debug consistency.
    x0, y0, x1, y1 = box
    crop = full[y0:y1, x0:x1]
    crop_cells = _run_ocr(crop, label="orders_crop")
    cells = [_Cell(c.x, c.y + y0, c.text) for c in crop_cells]

    # Drop the right-hand L2/watchlist panel so a watchlist ticker at the same y
    # can't merge into an order row during clustering, then pattern-parse rows.
    grid_cells = [c for c in cells if c.x < _ORDERS_GRID_MAX_X]
    rows = _cluster_rows(grid_cells)
    return _parse_orders_from_rows(rows)


class OCROrdersAdapter:
    """Reads order rows via RapidOCR screenshot of the Orders panel."""

    def get_orders(self) -> list[OrderRow]:
        return with_retry(_read_orders_ocr, label="OCR Orders")
