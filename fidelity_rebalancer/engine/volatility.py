"""
Dynamic Realized Volatility estimator.
Primary: yfinance 20-day standard deviation of daily returns.
Secondary: Asset class defaults.
Tertiary: ATP intraday proxy (Day Range).
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from engine.spread_context import _TICKER_CLASS

_log = logging.getLogger(__name__)

_ASSET_CLASS_VOL_BPS = {
    "large_cap":     75.0,
    "sector":        120.0,
    "international": 100.0,
    "leveraged":     300.0,
    "fixed_income":  40.0,
}

def get_realized_volatility(
    symbol: str,
    day_range_low: float = 0.0,
    day_range_high: float = 0.0,
) -> float:
    """
    Returns the annualized realized volatility proxy in basis points (bps).
    100 bps = 1% daily volatility.
    """
    # 1. Primary: yfinance
    try:
        import yfinance as yf
        import pandas as pd
        hist = yf.Ticker(symbol).history(period="30d")
        if not hist.empty and len(hist) >= 20:
            # Calculate standard deviation of daily percentage returns over last 20 days
            closes = hist['Close'].tail(20)
            returns = closes.pct_change().dropna()
            std_dev = returns.std()
            if not pd.isna(std_dev) and std_dev > 0:
                vol_bps = std_dev * 10000.0
                _log.debug("Volatility for %s from yfinance: %.1f bps", symbol, vol_bps)
                return float(vol_bps)
    except Exception as e:
        _log.warning("yfinance volatility fetch failed for %s: %s", symbol, e)

    # 2. Secondary: Asset Class Default
    cls = _TICKER_CLASS.get(symbol.upper())
    if cls and cls in _ASSET_CLASS_VOL_BPS:
        vol_bps = _ASSET_CLASS_VOL_BPS[cls]
        _log.debug("Volatility for %s from Asset Class '%s': %.1f bps", symbol, cls, vol_bps)
        return float(vol_bps)

    # 3. Tertiary: ATP Intraday Proxy
    if day_range_low > 0 and day_range_high > day_range_low:
        parkinson_variance = (1.0 / (4.0 * math.log(2))) * (math.log(day_range_high / day_range_low) ** 2)
        parkinson_vol = math.sqrt(parkinson_variance)
        vol_bps = parkinson_vol * 10000.0
        _log.debug("Volatility for %s from Day Range Proxy: %.1f bps", symbol, vol_bps)
        return float(vol_bps)

    # 4. Final Fallback
    _log.debug("Volatility for %s defaulting to 100 bps", symbol)
    return 100.0
