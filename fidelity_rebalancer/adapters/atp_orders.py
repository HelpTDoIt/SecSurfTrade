"""
Fidelity Trader+ Orders adapter.

IMPORTANT — UIA accessibility limitation:
  Fidelity Trader+ uses a Telerik MAUI RadMauiScrollView for the orders grid.
  As of Apr-2026, the control only exposes the currently-visible horizontal
  column through UIA.  The Orders panel scrolls horizontally; the Status column
  appears to be the only column surfaced: each row shows as [Text] 'Open' or
  [Text] 'Filled' — symbol, qty, price, and account are NOT accessible.

  This adapter raises LookupError to indicate the data cannot be read.

  Alternative: click the [Button]+[Text 'Export'] on the orders toolbar to
  export to clipboard or file, then parse that text.  That export path is NOT
  yet implemented here.

What IS readable via UIA (for informational purposes only):
  [Edit] title='Open  2 - Filled  0' id='RadComboBoxTextInput'
  → aggregate status counts (2 open, 0 filled)

Run scripts/atp_smoke.py --debug-tree to confirm the current layout.
"""
from __future__ import annotations

import re

from adapters import OrderRow, OrderStatus
from adapters._atp_connect import get_app, with_retry
from adapters._atp_ui import get_panel_container, sv_children

_STATUS_COUNT_RE = re.compile(
    r"Open\s+(\d+)\s*-\s*Filled\s+(\d+)", re.IGNORECASE
)


def _read_status_counts(controls: list) -> tuple[int, int]:
    """
    Parse the status-filter dropdown to get aggregate open/filled counts.
    Returns (open_count, filled_count).
    """
    for ctrl in controls:
        try:
            if ctrl.element_info.automation_id == "RadComboBoxTextInput":
                text = ctrl.window_text().strip()
                m = _STATUS_COUNT_RE.search(text)
                if m:
                    return int(m.group(1)), int(m.group(2))
        except Exception:
            continue
    return 0, 0


def _read_orders() -> list[OrderRow]:
    app = get_app()
    _, sv, _ = get_panel_container(app)
    controls = sv_children(sv)

    open_count, filled_count = _read_status_counts(controls)

    raise LookupError(
        f"Orders data is NOT accessible via Windows UIA in Fidelity Trader+. "
        f"Status filter reports: {open_count} open, {filled_count} filled — "
        "but per-order symbol/qty/price/account rows are not exposed by the "
        "Telerik MAUI RadMauiScrollView control. "
        "Use the Orders panel Export button to read order details, "
        "or track order state internally in the engine."
    )


class ATPOrdersAdapter:
    """
    Attempts to read order rows from Fidelity Trader+ via UIA.
    Currently raises LookupError — see module docstring for explanation.
    """

    def get_orders(self) -> list[OrderRow]:
        return with_retry(_read_orders, label="Orders panel")
