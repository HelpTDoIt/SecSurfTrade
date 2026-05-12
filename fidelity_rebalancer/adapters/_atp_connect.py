"""
Shared ATP application handle and retry logic.

The Application object is created once and cached. Window/control handles
are intentionally NOT cached because ATP redraws can stale them.
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

# pywinauto is imported lazily so the engine never drags it in at module load.
_app = None  # type: ignore[var-annotated]

_ATP_EXE = "Fidelity Trader+.exe"
_RETRY_COUNT = 3
_RETRY_DELAY = 0.20  # seconds between attempts


def get_app():
    """
    Return (and cache) a pywinauto Application connected to ATP via UIA.
    Raises RuntimeError if ATP is not running.
    """
    global _app
    if _app is not None:
        return _app

    from pywinauto import Application  # noqa: PLC0415

    try:
        _app = Application(backend="uia").connect(path=_ATP_EXE)
    except Exception as exc:
        raise RuntimeError(
            f"Cannot connect to '{_ATP_EXE}'. "
            "Ensure Fidelity Trader+ is running, logged in, and the relevant panels are visible."
        ) from exc
    return _app


T = TypeVar("T")


def with_retry(fn: Callable[[], T], label: str = "ATP read") -> T:
    """
    Call fn() up to _RETRY_COUNT times, sleeping _RETRY_DELAY between attempts.
    Raises the last exception if all attempts fail.
    """
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
