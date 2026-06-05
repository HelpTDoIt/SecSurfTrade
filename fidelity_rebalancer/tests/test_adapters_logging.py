"""
Validates B-9 — the OCR / watchlist / yfinance adapters emit through the stdlib
``logging`` module instead of raw ``print()``.

Guarantees:
  1. No ``print(`` survives in the three refactored adapter source files.
  2. Each adapter exposes a module-level ``logging.Logger`` named for the module.
  3. The yfinance batch adapter emits a WARNING (previously a print) when an
     individual symbol fails — proving the print->logging swap preserved the
     level and message, not merely deleted the prints.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from adapters import atp_ocr, atp_watchlist, yfinance_fallback

_ADAPTERS_DIR = Path(__file__).parent.parent / "adapters"
_REFACTORED = ("atp_ocr.py", "atp_watchlist.py", "yfinance_fallback.py")


def test_no_print_in_refactored_adapters():
    """No raw print() survives in the adapters B-9 converted to logging."""
    for name in _REFACTORED:
        src = (_ADAPTERS_DIR / name).read_text(encoding="utf-8")
        offenders = [
            line
            for line in src.splitlines()
            if "print(" in line and not line.strip().startswith("#")
        ]
        assert not offenders, f"{name} still contains print(): {offenders}"


@pytest.mark.parametrize("module", [atp_ocr, atp_watchlist, yfinance_fallback])
def test_adapter_exposes_module_logger(module):
    """Each adapter has a module-level stdlib logger named for the module."""
    short_name = module.__name__.split(".")[-1]
    assert isinstance(module._log, logging.Logger)
    assert module._log.name.split(".")[-1] == short_name


def test_yfinance_per_symbol_failure_logs_warning(monkeypatch):
    """A failed symbol is skipped with a WARNING log record (was a bare print)."""

    class _FakeTicker:
        @property
        def info(self):
            raise RuntimeError("synthetic fetch failure")

    class _FakeTickers:
        def __init__(self, _spec):
            self.tickers = {}

    class _FakeYF:
        Tickers = _FakeTickers

        @staticmethod
        def Ticker(_symbol):
            return _FakeTicker()

    monkeypatch.setattr(yfinance_fallback, "_require_yf", lambda: _FakeYF)

    # Capture directly off the adapter's logger so the assertion does not depend
    # on root-logger propagation / pytest caplog handler placement.
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = yfinance_fallback._log
    handler = _Capture(level=logging.WARNING)
    logger.addHandler(handler)
    prev_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        result = yfinance_fallback.YFinanceWatchlistAdapter().get_watchlist(["SYNTH1"])
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)

    assert result == {}  # failing symbol skipped, not raised
    assert any(
        r.levelno == logging.WARNING and "SYNTH1" in r.getMessage() for r in records
    ), f"expected a WARNING naming SYNTH1, got: {[r.getMessage() for r in records]}"
