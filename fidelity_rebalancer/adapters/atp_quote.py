"""
Fidelity Trader+ Quote adapter.

All panels live inside one WinUI window. Quote data is in flat ScrollViewer children:

  [Edit]   id='RadAutoCompleteTextInput'  title=SYMBOL   ← Quote panel start
  [Button] ToolClose / other buttons
  [Text]   full company name
  [Text]   last price          e.g. '24.20'
  [Text]   change              e.g. '+0.08'
  [Text]   pct change          e.g. '(+0.33%)'
  [Text]   'B'
  [Button] bid price           e.g. '24.24'
  [Text]   'x 500'             bid size
  [Text]   'A'
  [Button] ask price           e.g. '26.00'
  [Text]   'x 600'             ask size
  [Text]   'V'
  [Text]   volume              e.g. '29,350'
  [Custom] RadMauiScrollView2  ← extended fields: Rolling Last Price, Close, ranges

Extended fields in RadMauiScrollView2 → PART_ScrollViewer children (label then value):
  'Rolling Last Price' → last-fill price
  '52W Range' → low, high
  'Day Range' → low, high
  '10-Day Volume' → value
  '90-Day Volume' → value

NOTE: prev_close and div ex-date are NOT in the Quote panel — they only appear in
the Watchlist panel.  prev_close is returned as 0.0 until a Watchlist adapter is built.

The Level 2 panel also starts with a RadAutoCompleteTextInput; it's identified by
a [Text] 'Level 2' within the next 3 siblings.

Run scripts/atp_smoke.py --debug-tree to inspect the live UIA tree.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from adapters import QuoteSnapshot
from adapters._atp_connect import get_app, with_retry
from adapters._atp_parse import parse_price, parse_size, parse_volume
from adapters._atp_ui import get_panel_container, sv_children

_PRICE_RE = re.compile(r'^\d[\d,.]*$')


def _is_plain_number(s: str) -> bool:
    return bool(_PRICE_RE.match(s))


def _find_quote_start(controls: list, symbol: str) -> int:
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
                if not is_l2:
                    return i
        except Exception:
            continue
    return -1


def _parse_main_fields(rest: list) -> dict:
    last = bid = ask = 0.0
    bid_size = ask_size = volume = 0

    # Last price: first purely-numeric Text in the first 6 controls after the Edit
    for ctrl in rest[:6]:
        try:
            t = ctrl.window_text().strip()
            if _is_plain_number(t):
                last = parse_price(t)
                break
        except Exception:
            continue

    for j, ctrl in enumerate(rest):
        try:
            text  = ctrl.window_text().strip()
            ctype = ctrl.element_info.control_type
        except Exception:
            continue

        if ctype == "Custom":
            break  # Hit RadMauiScrollView2 — end of main quote area

        if text == "B" and j + 1 < len(rest):
            bid = parse_price(rest[j + 1].window_text().strip())
            if j + 2 < len(rest):
                bid_size = parse_size(rest[j + 2].window_text().strip().lstrip("x").strip())
        elif text == "A" and j + 1 < len(rest):
            ask = parse_price(rest[j + 1].window_text().strip())
            if j + 2 < len(rest):
                ask_size = parse_size(rest[j + 2].window_text().strip().lstrip("x").strip())
        elif text == "V" and j + 1 < len(rest):
            volume = parse_volume(rest[j + 1].window_text().strip())
            break

    return dict(last=last, bid=bid, bid_size=bid_size,
                ask=ask, ask_size=ask_size, volume=volume)


def _read_quote(symbol: str) -> QuoteSnapshot:
    app = get_app()
    _, sv, _ = get_panel_container(app)
    controls = sv_children(sv)

    idx = _find_quote_start(controls, symbol)
    if idx < 0:
        raise LookupError(
            f"Quote panel for '{symbol.upper()}' not found in the main workspace. "
            "Ensure a Quote widget is visible for this symbol in Fidelity Trader+. "
            "Run --debug-tree to inspect the UIA tree."
        )

    fields = _parse_main_fields(controls[idx + 1:])

    return QuoteSnapshot(
        symbol=symbol.upper(),
        bid=fields["bid"],
        bid_size=fields["bid_size"],
        ask=fields["ask"],
        ask_size=fields["ask_size"],
        last=fields["last"],
        prev_close=0.0,   # only in Watchlist panel, not Quote
        volume=fields["volume"],
        ts=datetime.now(tz=timezone.utc),
    )


class ATPQuoteAdapter:
    """Reads live quotes from the Fidelity Trader+ Quote widget via pywinauto UIA."""

    def get_quote(self, symbol: str) -> QuoteSnapshot:
        return with_retry(lambda: _read_quote(symbol), label=f"Quote {symbol}")
