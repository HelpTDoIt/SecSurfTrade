"""
Connection helper for Fidelity Active Trader Pro (ATP).

Active Trader Pro is a WPF/C# desktop application using Telerik WPF controls
(RadMenu, RadGridView, RadDocking, etc.).  It is NOT a Java application —
Java Access Bridge (JAB) is irrelevant.  WPF has native UIA support, and
Telerik WPF's RadGridView exposes data rows as UIA DataItem controls,
making them directly accessible via pywinauto without OCR.

This is the key advantage over Fidelity Trader+ (a WinUI/MAUI app):
Telerik WPF RadGridView = accessible rows via UIA.
Telerik MAUI RadMauiScrollView = opaque, UIA-blocked.

Prerequisites
-------------
1. Fidelity Active Trader Pro installed and running, logged in.
2. The panel you want to read must be OPEN AND VISIBLE:
   - Watchlist: Quotes & Watch List → Watch Lists
   - Level 2:   Quotes & Watch List → Level 2
   - Orders:    Trade & Orders → Orders

Connection
----------
Connects by window title pattern "Active Trader Pro".
Uses UIA backend (WPF's native accessibility layer).
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

_fatp_app = None  # cached connection

_FATP_TITLE_RE = r".*Active Trader Pro.*"
_RETRY_COUNT   = 3
_RETRY_DELAY   = 0.20


def get_fatp_app():
    """
    Return (and cache) a pywinauto Application connected to Active Trader Pro.
    Raises RuntimeError if ATP is not running or not connectable.
    """
    global _fatp_app
    if _fatp_app is not None:
        return _fatp_app

    from pywinauto import Application  # noqa: PLC0415

    # ATP is a WPF app — UIA backend gives full control access natively.
    try:
        _fatp_app = Application(backend="uia").connect(title_re=_FATP_TITLE_RE)
        return _fatp_app
    except Exception as exc:
        raise RuntimeError(
            "Cannot connect to 'Fidelity Active Trader Pro'. "
            "Ensure the application is running and logged in."
        ) from exc


def reset_fatp_connection() -> None:
    """Force a fresh connection on the next get_fatp_app() call."""
    global _fatp_app
    _fatp_app = None


T = TypeVar("T")


def with_fatp_retry(fn: Callable[[], T], label: str = "FATP read") -> T:
    last_exc: Exception | None = None
    for attempt in range(1, _RETRY_COUNT + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < _RETRY_COUNT:
                time.sleep(_RETRY_DELAY)
    assert last_exc is not None
    raise RuntimeError(f"{label} failed after {_RETRY_COUNT} attempts") from last_exc
