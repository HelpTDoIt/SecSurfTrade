"""
Fidelity Trader+ Level 2 adapter.

IMPORTANT — UIA accessibility limitation:
  Fidelity Trader+ uses Telerik MAUI RadMauiScrollView for the L2 depth grid.
  As of Apr-2026, this control does NOT expose individual row data through the
  Windows UIA accessibility API — descendants() and children() return nothing
  for the data rows.  Only the Quote bid/ask (best bid/ask) is accessible.

  This adapter raises LookupError to indicate the data cannot be read.
  Use yfinance_fallback.YFinanceQuoteAdapter as a substitute for L2 data,
  or implement screen-capture + OCR if true depth-of-book is required.

The Level 2 panel IS detectable in the UIA tree:
  ScrollViewer flat children contain:
    [Edit] id='RadAutoCompleteTextInput' title=SYMBOL
    [Text] title='Level 2'              ← distinguishes from Quote panel

The [List] ListView sibling and the [Custom] RadMauiScrollView inside the panel
are both present in the UIA tree but have empty child lists.

Run scripts/atp_smoke.py --debug-tree to confirm the current layout.
"""
from __future__ import annotations

from datetime import datetime, timezone

from adapters import Level, Level2Snapshot
from adapters._atp_connect import get_app, with_retry
from adapters._atp_parse import parse_price, parse_size
from adapters._atp_ui import get_panel_container, sv_children


def _find_l2_panel_start(controls: list, symbol: str) -> int:
    sym = symbol.upper().strip()
    for i, ctrl in enumerate(controls):
        try:
            if (ctrl.element_info.control_type == "Edit"
                    and ctrl.element_info.automation_id == "RadAutoCompleteTextInput"
                    and ctrl.window_text().strip().upper() == sym):
                is_l2 = any(
                    controls[j].window_text().strip() == "Level 2"
                    for j in range(i + 1, min(i + 4, len(controls)))
                )
                if is_l2:
                    return i
        except Exception:
            continue
    return -1


def _read_l2(symbol: str) -> Level2Snapshot:
    app = get_app()
    _, sv, _ = get_panel_container(app)
    controls = sv_children(sv)

    l2_idx = _find_l2_panel_start(controls, symbol)
    if l2_idx < 0:
        raise LookupError(
            f"Level 2 panel for '{symbol.upper()}' not found in the main workspace. "
            "Ensure a Level 2 widget is visible for this symbol in Fidelity Trader+. "
            "Run --debug-tree to inspect the UIA tree."
        )

    from adapters.atp_ocr import _read_l2_ocr
    return _read_l2_ocr(symbol)


class ATPLevel2Adapter:
    """
    Attempts to read Level 2 depth-of-book from Fidelity Trader+ via UIA.
    Currently raises LookupError — see module docstring for explanation.
    """

    def get_level2(self, symbol: str) -> Level2Snapshot:
        return with_retry(lambda: _read_l2(symbol), label=f"Level2 {symbol}")
