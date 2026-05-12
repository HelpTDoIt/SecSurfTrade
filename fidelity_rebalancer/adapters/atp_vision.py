"""
Vision-based adapters for Fidelity Trader+ data not accessible via UIA.

Fidelity Trader+ uses Telerik MAUI controls whose row data is not exposed
through the Windows UIA accessibility API.  This module captures a screenshot
of the relevant panel region and sends it to Claude claude-haiku-4-5 to extract
the structured data.

Requirements:
  pip install anthropic pillow
  ANTHROPIC_API_KEY must be set in the environment.

Typical cost: < $0.001 per read (claude-haiku-4-5, small image region).
"""
from __future__ import annotations

import base64
import io
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from adapters import Level, Level2Snapshot, OrderRow, OrderStatus
from adapters._atp_connect import get_app, with_retry
from adapters._atp_parse import parse_price, parse_size
from adapters._atp_ui import get_panel_container, sv_children

# Model — haiku is fast and cheap; upgrade to sonnet if accuracy suffers
_VISION_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 1024


# ── Screenshot helpers ─────────────────────────────────────────────────────

def _rect_to_bbox(rect) -> tuple[int, int, int, int]:
    return (rect.left, rect.top, rect.right, rect.bottom)


def _capture_rect(rect) -> bytes:
    """Capture the given RECT (pywinauto) as PNG bytes."""
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=_rect_to_bbox(rect), all_screens=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _capture_main_window() -> bytes:
    """Capture the full Fidelity Trader+ window as PNG bytes."""
    app = get_app()
    rect = app.top_window().rectangle()
    return _capture_rect(rect)


def _find_l2_grid_ctrl(controls: list, symbol: str):
    """
    Find the RadMauiScrollView control inside the Level 2 panel.
    It is the last Custom control in sv_children (after the L2 edit & shortcuts).
    Returns the control, or None.
    """
    sym = symbol.upper()
    in_l2 = False
    last_custom = None
    for ctrl in controls:
        try:
            ctype = ctrl.element_info.control_type
            text  = ctrl.window_text().strip()
        except Exception:
            continue
        if ctype == "Edit" and text.upper() == sym:
            # Check for Level 2 label nearby by checking next window_text calls
            # We set in_l2 provisionally; confirmed when 'Level 2' text follows
            in_l2 = True
        if in_l2 and ctype == "Text" and text == "Level 2":
            in_l2 = True   # confirmed
        if in_l2 and ctype == "Custom":
            last_custom = ctrl   # keep updating; last Custom in L2 section
        if in_l2 and ctype == "Text" and text == "Exch":
            break   # column header after L2 grid → we're done
    return last_custom


def _find_orders_grid_ctrl(controls: list):
    """
    Find the RadMauiScrollView control for the Orders panel.
    It is the first Custom after the [Text] 'Orders' header.
    """
    found_orders = False
    for ctrl in controls:
        try:
            ctype = ctrl.element_info.control_type
            text  = ctrl.window_text().strip()
        except Exception:
            continue
        if ctype == "Text" and text == "Orders" and not found_orders:
            found_orders = True
        elif found_orders and ctype == "Custom":
            return ctrl
    return None


# ── Claude vision calls ────────────────────────────────────────────────────

def _claude_client():
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. "
            "Export it in your environment to use vision-based adapters."
        )
    return anthropic.Anthropic(api_key=api_key)


def _vision_call(image_bytes: bytes, prompt: str) -> str:
    client = _claude_client()
    b64 = base64.standard_b64encode(image_bytes).decode()
    resp = client.messages.create(
        model=_VISION_MODEL,
        max_tokens=_MAX_TOKENS,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64",
                            "media_type": "image/png",
                            "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return resp.content[0].text.strip()


# ── Level 2 extraction ─────────────────────────────────────────────────────

_L2_PROMPT = """\
This is a screenshot of Fidelity Trader+ showing the Level 2 depth-of-book table \
for the symbol {symbol}.

The table has columns: Exch | Size | Bid || Ask | Size | Exch
(left side = bids, right side = asks, separated at the middle).

Extract all visible rows and return ONLY a JSON object with this structure:
{{
  "bids": [{{"price": 24.24, "size": 500, "mpid": "ARCA"}}, ...],
  "asks": [{{"price": 24.25, "size": 300, "mpid": "NSDQ"}}, ...]
}}

Rules:
- Include only rows where the price is a valid positive number.
- Convert size strings like "1.2K" to 1200, "1M" to 1000000.
- If a field is blank or "--", omit the row.
- Return ONLY the JSON — no explanation, no markdown fences.
"""


def _parse_l2_json(text: str, symbol: str) -> Level2Snapshot:
    # Strip any accidental markdown fences
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data: dict[str, Any] = json.loads(text)

    bids = [
        Level(price=float(r["price"]), size=int(r.get("size", 0)), mpid=r.get("mpid", ""))
        for r in data.get("bids", []) if float(r.get("price", 0)) > 0
    ]
    asks = [
        Level(price=float(r["price"]), size=int(r.get("size", 0)), mpid=r.get("mpid", ""))
        for r in data.get("asks", []) if float(r.get("price", 0)) > 0
    ]
    bids = sorted(bids, key=lambda l: l.price, reverse=True)
    asks = sorted(asks, key=lambda l: l.price)
    return Level2Snapshot(symbol=symbol.upper(), bids=bids, asks=asks,
                          ts=datetime.now(tz=timezone.utc))


def _read_l2_vision(symbol: str) -> Level2Snapshot:
    app = get_app()
    _, sv, _ = get_panel_container(app)
    controls = sv_children(sv)

    grid_ctrl = _find_l2_grid_ctrl(controls, symbol)
    if grid_ctrl is not None:
        try:
            image_bytes = _capture_rect(grid_ctrl.rectangle())
        except Exception:
            image_bytes = _capture_main_window()
    else:
        image_bytes = _capture_main_window()

    prompt = _L2_PROMPT.format(symbol=symbol.upper())
    raw = _vision_call(image_bytes, prompt)

    try:
        return _parse_l2_json(raw, symbol)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        raise RuntimeError(
            f"Claude vision returned unparseable response for L2 {symbol}: {raw!r}"
        ) from exc


class VisionL2Adapter:
    """
    Reads Level 2 depth-of-book via Claude vision (screenshot of ATP panel).
    Falls back to capturing the full window if the grid region cannot be located.
    """

    def get_level2(self, symbol: str) -> Level2Snapshot:
        return with_retry(lambda: _read_l2_vision(symbol), label=f"VisionL2 {symbol}")


# ── Orders extraction ──────────────────────────────────────────────────────

_ORDERS_PROMPT = """\
This is a screenshot of Fidelity Trader+ showing the Orders panel.

The table has columns that may include: Symbol | Action (Buy/Sell) | Amount (qty) \
| Order Type | Status | Filled | Account | Order Time.

Extract all visible order rows and return ONLY a JSON object:
{{
  "orders": [
    {{
      "symbol": "TYD",
      "side": "BUY",
      "qty": 100,
      "filled_qty": 0,
      "limit_price": 24.50,
      "status": "open",
      "account": "Z12345678",
      "order_time": "12:08:55 PM ET Apr-29-2026"
    }},
    ...
  ]
}}

Rules:
- "side" must be "BUY" or "SELL" (uppercase).
- "status" must be one of: open, filled, partiallyfilled, cancelled, rejected.
- "limit_price" is 0.0 for market orders.
- "account" is the account number string (may be masked like "...1234").
- "order_time" is the raw text from the Order Time column; use "" if not visible.
- If a row cannot be clearly read, omit it.
- Return ONLY the JSON — no explanation, no markdown fences.
"""

_STATUS_MAP: dict[str, OrderStatus] = {
    "open":            OrderStatus.Open,
    "partiallyfilled": OrderStatus.PartiallyFilled,
    "partial":         OrderStatus.PartiallyFilled,
    "filled":          OrderStatus.Filled,
    "cancelled":       OrderStatus.Cancelled,
    "canceled":        OrderStatus.Cancelled,
    "rejected":        OrderStatus.Rejected,
}

_TIME_IMPORT_RE = __import__("re").compile(
    r"(\d{1,2}:\d{2}(?::\d{2})?)\s*(AM|PM)?\s*(?:ET|EST|EDT)?\s*([A-Za-z]{3}-\d{2}-\d{4})?",
    __import__("re").IGNORECASE,
)


def _parse_orders_json(text: str) -> list[OrderRow]:
    from datetime import date, timedelta
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data: dict[str, Any] = json.loads(text)

    rows: list[OrderRow] = []
    for r in data.get("orders", []):
        try:
            symbol = str(r.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            side       = str(r.get("side", "BUY")).upper()
            qty        = float(r.get("qty", 0))
            filled_qty = float(r.get("filled_qty", 0))
            limit_price = float(r.get("limit_price", 0.0))
            status_key  = str(r.get("status", "open")).lower().replace(" ", "")
            status      = _STATUS_MAP.get(status_key, OrderStatus.Open)
            account     = str(r.get("account", ""))
            placed_at   = datetime.now(tz=timezone.utc)   # parse order_time if present

            rows.append(OrderRow(
                account=account,
                symbol=symbol,
                side=side,
                qty=qty,
                filled_qty=filled_qty,
                limit_price=limit_price,
                status=status,
                placed_at=placed_at,
                last_update_at=placed_at,
            ))
        except Exception:
            continue
    return rows


def _read_orders_vision() -> list[OrderRow]:
    app = get_app()
    _, sv, _ = get_panel_container(app)
    controls = sv_children(sv)

    grid_ctrl = _find_orders_grid_ctrl(controls)
    if grid_ctrl is not None:
        try:
            image_bytes = _capture_rect(grid_ctrl.rectangle())
        except Exception:
            image_bytes = _capture_main_window()
    else:
        image_bytes = _capture_main_window()

    raw = _vision_call(image_bytes, _ORDERS_PROMPT)

    try:
        return _parse_orders_json(raw)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        raise RuntimeError(
            f"Claude vision returned unparseable response for Orders: {raw!r}"
        ) from exc


class VisionOrdersAdapter:
    """
    Reads order rows via Claude vision (screenshot of the Orders panel).
    Falls back to capturing the full window if the grid region cannot be located.
    """

    def get_orders(self) -> list[OrderRow]:
        return with_retry(_read_orders_vision, label="VisionOrders")
