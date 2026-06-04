"""
Pure intraday VWAP math (G-3).

VWAP = Σ(typical_price · volume) / Σ(volume), where the per-bar typical price is
the standard (high + low + close) / 3.  This module is deliberately I/O-free so
it stays inside the engine-purity guard (``tests/test_calculator.py::
test_no_io_in_engine``); the network 1-minute bar fetch that feeds it lives in
``adapters.yfinance_fallback.approx_intraday_vwap``.

The value computed here from yfinance 1-minute bars is an **approximation** of
true intraday VWAP and is distinct from ATP's exact intraday VWAP (which is
streamed directly).  It is good enough for the VWAP-relative rules (sell 6/7,
buy 4/5) to fire on the ``--source yfinance`` path.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence


@dataclass(frozen=True)
class Bar:
    """One intraday OHLCV bar (e.g. a yfinance 1-minute candle).

    ``volume`` defaults to 0.0 so ``typical_price`` can be exercised on a
    price-only bar; a 0-volume bar contributes nothing to ``vwap_from_bars``.
    """

    high: float
    low: float
    close: float
    volume: float = 0.0


def typical_price(bar: Bar) -> float:
    """Standard typical price: (high + low + close) / 3."""
    return (bar.high + bar.low + bar.close) / 3.0


def vwap_from_bars(bars: Iterable[Bar]) -> Optional[float]:
    """Volume-weighted average price across the given bars.

    Returns None when there is no usable volume (sum of volume ≤ 0) or no bars,
    so callers can treat "VWAP unavailable" uniformly (None) rather than 0.0.
    Bars with non-positive volume are skipped.
    """
    pv_sum = 0.0
    vol_sum = 0.0
    for bar in bars:
        vol = float(bar.volume)
        if vol <= 0:
            continue
        pv_sum += typical_price(bar) * vol
        vol_sum += vol
    if vol_sum <= 0:
        return None
    return pv_sum / vol_sum


def vwap_from_columns(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
) -> Optional[float]:
    """VWAP from parallel column sequences (the shape yfinance history returns).

    All four sequences must be the same length; extra trailing values are
    ignored beyond the shortest sequence.  Returns None when volume is absent.
    """
    n = min(len(highs), len(lows), len(closes), len(volumes))
    bars = (
        Bar(high=float(highs[i]), low=float(lows[i]),
            close=float(closes[i]), volume=float(volumes[i]))
        for i in range(n)
    )
    return vwap_from_bars(bars)
