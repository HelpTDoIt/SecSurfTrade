"""
Fidelity Active Trader Pro — Watchlist adapter.

STATUS: UIA-BLOCKED (confirmed 2026-05-01)
------------------------------------------
ATP renders its content panels via DirectX/GPU (Telerik WPF with Viewport3D
compositing). A full UIA descendants() search on the live app with the
Watchlist open returned 0 Table, 0 DataGrid, and 0 DataItem controls.
The content panels are as UIA-opaque as Fidelity Trader+'s Telerik MAUI grids.

This means OCR (PrintWindow → RapidOCR) would be required for ATP, the same
as Fidelity Trader+.  Since FT+ OCR adapters are already working and tested,
there is no advantage to using ATP for programmatic data access.

This module is kept as a scaffold in case Telerik releases a WPF accessibility
fix, but FATWatchlistAdapter is expected to raise LookupError on every call
until the UIA hierarchy improves.

Recommendation: use Fidelity Trader+ with the existing OCR adapters.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from adapters import WatchlistRow
from adapters._atp_parse import parse_price, parse_size
from adapters.fatp_connect import get_fatp_app, with_fatp_retry

_TICKER_RE = re.compile(r'^[A-Z]{1,6}(\.[A-Z]{1,2})?$')

# Map ATP column header text (lower) → WatchlistRow field name
_COL_MAP: dict[str, str] = {
    "symbol":           "symbol",
    "last":             "last",
    "last trade":       "last",
    "bid":              "bid",
    "ask":              "ask",
    "bid size":         "bid_size",
    "ask size":         "ask_size",
    "volume":           "volume",
    "previous close":   "prev_close",
    "prev close":       "prev_close",
    "close":            "prev_close",
    "dividend":         "div_local",
    "div":              "div_local",
    "ex-date":          "div_ex_date",
    "ex date":          "div_ex_date",
    "10-day avg vol":   "avg_vol_10d",
    "10d avg vol":      "avg_vol_10d",
    "10 day avg vol":   "avg_vol_10d",
    "90-day avg vol":   "avg_vol_90d",
    "90d avg vol":      "avg_vol_90d",
    "90 day avg vol":   "avg_vol_90d",
    "vwap":             "vwap",
}


def _find_watchlist_window(app):
    """
    Return the Watchlist window/pane from the ATP application.

    ATP may present the Watchlist as:
    - A separate top-level window titled "Watchlist"
    - A docked pane within the main "Active Trader Pro" window
    We try both.
    """
    # Try separate watchlist window first
    try:
        for win in app.windows():
            title = (win.window_text() or "").lower()
            if "watchlist" in title:
                return win
    except Exception:
        pass

    # Fall back: search for a Table/DataGrid inside the main window
    try:
        main = app.top_window()
        tables = main.descendants(control_type="Table")
        if tables:
            return main
        # Also try DataGrid (ATP may use a custom control type)
        grids = main.descendants(control_type="DataGrid")
        if grids:
            return main
    except Exception:
        pass

    raise LookupError(
        "Could not find Watchlist window in Fidelity Active Trader Pro. "
        "Ensure the Watchlist panel is open (Account → Watchlists)."
    )


def _read_watchlist_uia() -> dict[str, WatchlistRow]:
    app = get_fatp_app()
    win = _find_watchlist_window(app)

    # Enumerate Table or DataGrid controls
    tables: list = []
    try:
        tables = win.descendants(control_type="Table")
    except Exception:
        pass
    if not tables:
        try:
            tables = win.descendants(control_type="DataGrid")
        except Exception:
            pass
    if not tables:
        raise LookupError(
            "No Table/DataGrid control found in the Watchlist window. "
            "Java Access Bridge may not be enabled — run 'jabswitch -enable' "
            "as Administrator and restart Active Trader Pro."
        )

    table = tables[0]

    # Read column headers from the first Header child
    col_names: list[str] = []
    try:
        header_items = table.descendants(control_type="Header")
        if header_items:
            for hdr_item in header_items[0].children():
                try:
                    col_names.append(hdr_item.window_text().strip())
                except Exception:
                    col_names.append("")
    except Exception:
        pass

    if not col_names:
        # No headers accessible — JAB likely not working
        raise LookupError(
            "Watchlist table header not accessible via UIA. "
            "Confirm JAB is enabled: run 'jabswitch -enable' as admin "
            "and fully restart Active Trader Pro."
        )

    # Map column index → WatchlistRow field
    idx_to_field: dict[int, str] = {}
    for i, name in enumerate(col_names):
        field = _COL_MAP.get(name.lower().strip())
        if field:
            idx_to_field[i] = field

    # Read data rows (DataItem children of the table)
    results: dict[str, WatchlistRow] = {}
    try:
        rows = table.children(control_type="DataItem")
    except Exception:
        rows = []

    for row in rows:
        try:
            cells = row.children()
        except Exception:
            continue

        cell_values = []
        for cell in cells:
            try:
                cell_values.append(cell.window_text().strip())
            except Exception:
                cell_values.append("")

        # Build a field dict from the column mapping
        field_values: dict[str, str] = {}
        for i, val in enumerate(cell_values):
            if i in idx_to_field:
                field_values[idx_to_field[i]] = val

        symbol = field_values.get("symbol", "").strip().upper()
        if not symbol or not _TICKER_RE.match(symbol):
            continue

        def fp(key: str) -> float:
            return parse_price(field_values.get(key, ""))

        def fi(key: str) -> int:
            return parse_size(field_values.get(key, ""))

        results[symbol] = WatchlistRow(
            symbol=symbol,
            last=fp("last"),
            bid=fp("bid"),
            ask=fp("ask"),
            bid_size=fi("bid_size"),
            ask_size=fi("ask_size"),
            volume=fi("volume"),
            prev_close=fp("prev_close"),
            avg_vol_10d=fi("avg_vol_10d"),
            avg_vol_90d=fi("avg_vol_90d"),
            div_ex_date=field_values.get("div_ex_date", ""),
            div_local=fp("div_local"),
            vwap=fp("vwap"),
            ts=datetime.now(tz=timezone.utc),
        )

    if not results:
        raise LookupError(
            "Watchlist table found and headers read, but no ticker rows returned. "
            "The Watchlist may be empty or row DataItems are not accessible. "
            "Try scrolling the Watchlist in ATP to trigger a refresh."
        )

    return results


class FATWatchlistAdapter:
    """
    Reads Watchlist data from Fidelity Active Trader Pro via UIA + Java Access Bridge.

    Requires:
    - ATP running and logged in
    - Watchlist panel open (Account → Watchlists)
    - Java Access Bridge enabled ('jabswitch -enable' as admin, then restart ATP)
    """

    def get_watchlist(self) -> dict[str, WatchlistRow]:
        return with_fatp_retry(_read_watchlist_uia, label="FATP Watchlist UIA")
