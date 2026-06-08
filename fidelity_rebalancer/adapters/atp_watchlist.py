"""
Fidelity Trader+ Watchlist adapter.

Strategy
--------
1. UIA attempt — navigates to the Watchlist RadMauiScrollView and checks whether
   PART_ScrollViewer exposes any child controls.  In practice Telerik MAUI blocks
   all child access (same limitation as L2 and Orders panels), so this path always
   raises LookupError and is kept only to document the attempt and to act as a
   future hook if Telerik exposes accessibility in a later release.

2. OCR fallback — captures the full ATP window buffer via PrintWindow
   (PW_RENDERFULLCONTENT=2, works regardless of z-order), runs RapidOCR, finds
   the Watchlist header row, calibrates column x-positions, and parses each
   visible data row.

Visible rows only
-----------------
OCR reads whatever is currently scrolled into view.  If the Watchlist is longer
than one screen, the user must scroll it to capture remaining tickers.
All required tickers should be visible before running cli.strategy.

Columns parsed
--------------
Symbol | Last | Bid | Ask | Bid Size | Ask Size | Volume | Close (prev_close)
Div Ex-Date | Div Local | 10D Avg Vol | 90D Avg Vol | VWAP

Install
-------
  pip install rapidocr-onnxruntime pillow pywin32
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

from adapters import WatchlistRow
from adapters._atp_connect import get_app, with_retry
from adapters._atp_parse import parse_price, parse_size, parse_volume
from adapters._atp_ui import get_panel_container, sv_children

# Ticker pattern: 1–6 uppercase letters, optional dot suffix (e.g. BRK.B)
_TICKER_RE = re.compile(r'^[A-Z]{1,6}(\.[A-Z]{1,2})?$')

# Canonical watchlist column names (lowercase for matching)
_WL_COLS = [
    "symbol", "last", "bid", "ask", "bid size", "ask size",
    "volume", "close", "div ex-date", "div pay date",
    "div local", "10d avg vol", "90d avg vol", "vwap",
    "% chg", "open", "ext hrs last", "ext hrs % chg", "day range"
]
# Substrings that only appear in Watchlist headers (not Orders headers)
_WL_MARKERS = {"close", "vwap", "10d", "90d", "div ex", "div local", "ext hrs", "range"}


# ── UIA attempt (documents why it fails) ──────────────────────────────────

def _uia_watchlist(sv_controls: list) -> dict[str, WatchlistRow]:
    """
    Try to read Watchlist rows from UIA.  The Watchlist uses Telerik
    RadMauiScrollView whose PART_ScrollViewer never exposes child controls
    via UIA — the same limitation as the L2 and Orders grids.

    We locate the control to confirm it is present and reachable, then
    raise LookupError so the caller falls through to OCR.
    """
    found_export = False
    for ctrl in sv_controls:
        try:
            text  = ctrl.window_text().strip()
            ctype = ctrl.element_info.control_type
            auto_id = ctrl.element_info.automation_id
        except Exception:
            continue

        # The Watchlist section ends with an Export text then a Custom (RadMauiScrollView)
        if text == "Export" and not found_export:
            found_export = True
            continue

        if found_export and ctype == "Custom":
            # Found the Watchlist RadMauiScrollView
            try:
                sv_child = ctrl.child_window(auto_id="PART_ScrollViewer")
                children = sv_child.children()
                if children:
                    # Unexpected: Telerik has exposed rows — log and fall through
                    raise LookupError(
                        f"Watchlist PART_ScrollViewer has {len(children)} children "
                        "but no parser for raw UIA rows is implemented. "
                        "Falling back to OCR."
                    )
            except LookupError:
                raise
            except Exception:
                pass
            raise LookupError(
                "Watchlist RadMauiScrollView (Telerik MAUI) PART_ScrollViewer "
                "exposes no accessible children — UIA blocked by MAUI rendering. "
                "Falling back to OCR."
            )

    raise LookupError(
        "Watchlist section (Export button + RadMauiScrollView) not found in "
        "the UIA flat-children list.  Ensure the Watchlist panel is open and "
        "visible in Fidelity Trader+."
    )


# ── OCR helpers (re-use machinery from atp_ocr) ───────────────────────────

def _ocr_cells(full_img):
    """Return _Cell list from a full-window numpy image."""
    from adapters.atp_ocr import _ocr_engine, _ocr_to_cells, _Cell  # local import avoids circular
    ocr = _ocr_engine()
    result, _ = ocr(full_img)
    return _ocr_to_cells(result or [])


def _cluster_rows(cells, y_tol: int = 8):
    from adapters.atp_ocr import _cluster_rows as _cr
    return _cr(cells, y_tol)


# ── Watchlist OCR parsing ─────────────────────────────────────────────────

def _is_wl_header(row_cells) -> bool:
    """A watchlist header row contains 'symbol' AND a watchlist-specific marker."""
    texts = {c.text.lower() for c in row_cells}
    combined = " ".join(c.text.lower() for c in row_cells)
    if "symbol" not in texts:
        return False
    return any(m in combined for m in _WL_MARKERS)


def _calibrate_cols(header_row) -> dict[str, float]:
    """
    Map canonical column name → x-center for each header cell.

    Uses best-match-wins scoring to prevent short column names (e.g. 'ask')
    from being overwritten by compound names that contain them ('ask size').
    Scoring: exact match = len(col)+1000; col⊂token = len(col); token⊂col = len(token).
    An existing entry is only overwritten if the new match has a strictly higher score.
    """
    sorted_cells = sorted(header_row, key=lambda c: c.x)

    # Merge only very close OCR fragments (same word split by OCR, e.g. "10D" + "Avg" + "Vol")
    # Use a conservative 40px threshold to avoid merging separate column headers.
    # Never merge two tokens that each independently match a known column name
    # (e.g. "Symbol" and "Last" are adjacent but separate columns).
    wl_cols_set = set(_WL_COLS)
    tokens: list[tuple[str, float]] = []
    for cell in sorted_cells:
        if tokens and abs(cell.x - tokens[-1][1]) < 40:
            prev_exact = tokens[-1][0].strip().lower() in wl_cols_set
            curr_exact = cell.text.strip().lower() in wl_cols_set
            if prev_exact and curr_exact:
                tokens.append((cell.text, cell.x))
                continue
            merged_text = tokens[-1][0] + " " + cell.text
            merged_x = (tokens[-1][1] + cell.x) / 2
            tokens[-1] = (merged_text, merged_x)
        else:
            tokens.append((cell.text, cell.x))

    col_map: dict[str, float] = {}
    col_quality: dict[str, int] = {}

    for raw_text, xc in tokens:
        norm = raw_text.strip().lower()
        best_col: str | None = None
        best_q = -1

        for col in _WL_COLS:
            if norm == col:
                q = len(col) + 1000        # exact — highest priority
            elif col in norm:
                q = len(col)               # column name is contained in token text
            elif norm in col:
                q = len(norm)              # token text is contained in column name
            else:
                continue
            if q > best_q:
                best_col = col
                best_q = q

        if best_col is not None and best_q > col_quality.get(best_col, -1):
            col_map[best_col] = xc
            col_quality[best_col] = best_q

    return col_map


def _nearest_col(x: float, col_map: dict[str, float]) -> str | None:
    if not col_map:
        return None
    return min(col_map, key=lambda k: abs(col_map[k] - x))


# Known non-ticker symbol values that appear in the Watchlist (e.g. summary rows)
_SKIP_SYMBOLS = {"TOTALS", "TOTAL", "CASH", "SPAXX", "CORE", "FILLED", "SEARCH",
                  "EDIT", "EXPORT", "STATUS", "ACTION", "SYMBOL",
                  "AMOUNT", "ORDER", "DETAILS", "DAY", "ROUTE", "SAVE",
                  "PREVIEW", "MARKET", "LIMIT", "ACCOUNT", "AUTO", "NONE",
                  "GTC", "OPEN", "CANCEL"}


def _constrain_cells(row_cells, col_map: dict[str, float], margin: float = 60.0):
    """Keep only cells whose x falls within the calibrated column range."""
    min_x = min(col_map.values()) - margin
    max_x = max(col_map.values()) + margin
    return [c for c in row_cells if min_x <= c.x <= max_x]


def _parse_wl_data_row(row_cells, col_map: dict[str, float]) -> WatchlistRow | None:
    """Parse one data row using calibrated column x-positions."""
    if not row_cells:
        return None

    # Use the cell nearest the "symbol" column, not the leftmost cell overall.
    # Order notifications and UI buttons can appear further left than the
    # watchlist data area and would otherwise steal the symbol slot.
    sym_x = col_map.get("symbol")
    if sym_x is None:
        return None
    sym_cell = min(row_cells, key=lambda c: abs(c.x - sym_x))
    if abs(sym_cell.x - sym_x) > 80:
        return None
    sym_upper = sym_cell.text.strip().upper()
    if sym_upper in _SKIP_SYMBOLS:
        return None
    if not _TICKER_RE.match(sym_upper):
        return None

    cells_by_col: dict[str, list[str]] = {k: [] for k in _WL_COLS}
    for cell in row_cells:
        col = _nearest_col(cell.x, col_map)
        if col:
            cells_by_col[col].append(cell.text)

    def first(col: str) -> str:
        vals = cells_by_col.get(col, [])
        return vals[0] if vals else ""

    def fp(col: str) -> float:
        return parse_price(first(col))

    def fi(col: str) -> int:
        return parse_size(first(col))

    def fpct(col: str) -> float:
        """Parse a percentage value (may have % suffix)."""
        raw = first(col).replace("%", "").strip()
        return parse_price(raw)

    day_range_raw = first("day range")
    day_range_low = 0.0
    day_range_high = 0.0
    if day_range_raw:
        m = re.search(r'([\d\.]+)\s*—◆—\s*([\d\.]+)', day_range_raw)
        if m:
            day_range_low = parse_price(m.group(1))
            day_range_high = parse_price(m.group(2))

    symbol = sym_upper
    return WatchlistRow(
        symbol=symbol,
        last=fp("last"),
        bid=fp("bid"),
        ask=fp("ask"),
        bid_size=fi("bid size"),
        ask_size=fi("ask size"),
        volume=fi("volume"),
        prev_close=fp("close"),
        avg_vol_10d=fi("10d avg vol"),
        avg_vol_90d=fi("90d avg vol"),
        div_ex_date=first("div ex-date"),
        div_local=fp("div local"),
        vwap=fp("vwap"),
        ts=datetime.now(tz=timezone.utc),
        pct_chg=fpct("% chg"),
        open_price=fp("open"),
        ext_hrs_last=fp("ext hrs last"),
        ext_hrs_pct_chg=fpct("ext hrs % chg"),
        day_range_low=day_range_low,
        day_range_high=day_range_high,
    )


def _read_watchlist_ocr() -> dict[str, WatchlistRow]:
    from adapters.atp_ocr import _capture_full_window, _debug_save
    full = _capture_full_window()
    img_h, img_w = full.shape[:2]

    # Crop to Watchlist data region:
    #   Watchlist now occupies the upper-left quadrant (no order-entry sidebar).
    #   Right 50% boundary excludes L2 panels; top 40% excludes Orders panel.
    wl_x = 0
    wl_w = int(img_w * 0.50)
    wl_h = int(img_h * 0.40)
    wl_img = full[:wl_h, wl_x:wl_w, :]

    dest = _debug_save(wl_img, "wl_crop")
    if dest:
        _log.debug("[WL DEBUG] crop x=%d..%d (%dx%dpx) -> %s", wl_x, wl_w, wl_w - wl_x, wl_h, dest)
    else:
        _log.debug("[WL DEBUG] crop x=%d..%d (%dx%dpx)", wl_x, wl_w, wl_w - wl_x, wl_h)

    all_cells = _ocr_cells(wl_img)
    wl_cells = all_cells

    _log.debug("[WL DEBUG] OCR returned %d cells", len(wl_cells))
    for c in wl_cells:
        _log.debug("  x=%6.0f  y=%6.0f  %r", c.x, c.y, c.text)

    rows = _cluster_rows(wl_cells)

    _log.debug("[WL DEBUG] clustered into %d rows", len(rows))

    header_idx: int | None = None
    for i, row in enumerate(rows):
        if _is_wl_header(row):
            header_idx = i
            break

    if header_idx is None:
        raise RuntimeError(
            "Watchlist column header row not found in OCR output. "
            "Ensure the Watchlist panel header (Symbol, Last, Close, etc.) "
            "is fully visible in Fidelity Trader+ and not scrolled out of view."
        )

    col_map = _calibrate_cols(rows[header_idx])
    if not col_map:
        raise RuntimeError(
            "Could not calibrate Watchlist column positions from the header row. "
            "Header cells found but none matched known column names."
        )

    _log.debug("[WL DEBUG] header at row %d, col_map: %s", header_idx, col_map)
    _log.debug("[WL DEBUG] %d data rows to parse", len(rows) - header_idx - 1)

    results: dict[str, WatchlistRow] = {}
    for row in rows[header_idx + 1:]:
        constrained = _constrain_cells(row, col_map)
        if not constrained:
            _log.debug("  [WL DEBUG] row dropped by constrain: %s", [c.text for c in row])
            continue
        # "Totals" row marks the end of watchlist data; everything below is
        # the Orders panel or other UI elements.
        if any(c.text.strip().lower() in ("totals", "total") for c in constrained):
            break
        entry = _parse_wl_data_row(constrained, col_map)
        if entry:
            results[entry.symbol] = entry
        else:
            _log.debug("  [WL DEBUG] row rejected by parse: %s", [c.text for c in constrained])

    if not results:
        raise RuntimeError(
            "Watchlist header found but no data rows were parsed. "
            "Ensure at least one ticker row is visible in the Watchlist panel."
        )

    return results


# ── Public adapters ────────────────────────────────────────────────────────

class UIAWatchlistAdapter:
    """
    Attempts to read Watchlist via UIA.  Currently always falls through to
    raise LookupError because Telerik RadMauiScrollView blocks child access.
    Kept as a documented attempt and future hook.
    """

    def get_watchlist(self) -> dict[str, WatchlistRow]:
        app = get_app()
        _, sv, _ = get_panel_container(app)
        controls = sv_children(sv)
        return _uia_watchlist(controls)  # raises LookupError


class OCRWatchlistAdapter:
    """Reads Watchlist data via RapidOCR screenshot of the Fidelity Trader+ window."""

    def get_watchlist(self) -> dict[str, WatchlistRow]:
        return with_retry(_read_watchlist_ocr, label="OCR Watchlist")


class ATPWatchlistAdapter:
    """
    Tries UIA first; falls back to OCR automatically.
    This is the adapter to use from cli.strategy and the smoke test.
    """

    def get_watchlist(self) -> dict[str, WatchlistRow]:
        try:
            return UIAWatchlistAdapter().get_watchlist()
        except LookupError:
            pass  # expected — Telerik blocks UIA
        return OCRWatchlistAdapter().get_watchlist()
