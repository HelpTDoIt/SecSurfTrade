"""
Per-symbol spread calibration for strategy rule thresholds.

Instead of hardcoded 5 bps (tight) and 10 bps (wide), thresholds are derived
from each symbol's typical spread so that leveraged ETFs (typical ~20 bps)
aren't permanently classified as "wide_spread" and large-cap ETFs (~2 bps)
aren't permanently "tight".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SpreadContext:
    """Spread thresholds calibrated to a specific symbol's typical spread."""
    typical_bps: float
    tight_bps: float
    wide_bps: float

    @classmethod
    def from_typical(cls, typical_bps: float) -> SpreadContext:
        return cls(
            typical_bps=typical_bps,
            tight_bps=typical_bps * 0.7,
            wide_bps=typical_bps * 1.5,
        )

    @classmethod
    def default(cls) -> SpreadContext:
        """Legacy behavior: tight < 5 bps, wide > 10 bps."""
        return cls(typical_bps=7.0, tight_bps=5.0, wide_bps=10.0)

    @classmethod
    def from_bid_ask(cls, bid: float, ask: float) -> SpreadContext:
        """Build from a single observed bid/ask snapshot (used as typical estimate)."""
        mid = (bid + ask) / 2.0 if (bid and ask) else 0.0
        if mid <= 0:
            return cls.default()
        spread_bps = max(0.0, (ask - bid) / mid * 10_000)
        if spread_bps < 1.0:
            return cls.default()
        return cls.from_typical(spread_bps)


# Asset-class buckets used when no live spread data is available.
_ASSET_CLASS_TYPICAL: dict[str, float] = {
    "large_cap":     3.0,    # SPY, QQQ, IWY, VGK, EEM
    "sector":        5.0,    # XLE, XLK, XLI, IBB, IYT
    "international": 8.0,    # VWO, ICOW, EWD, ACWX, EPOL
    "leveraged":    20.0,    # DFEN, BULZ, MIDU, FAS, TQQQ
    "fixed_income":  4.0,    # IEF, TIPZ, BTAL
}

_TICKER_CLASS: dict[str, str] = {
    "SPY": "large_cap", "QQQ": "large_cap", "IWY": "large_cap",
    "VGK": "large_cap", "EEM": "large_cap", "ILF": "large_cap",
    "IPAC": "large_cap", "FRDM": "international",
    "VWO": "international", "ICOW": "international", "EWD": "international",
    "EIS": "international", "DFIV": "international", "SCZ": "international",
    "EIDO": "international", "INDA": "international", "EWS": "international",
    "EWC": "international", "EWG": "international", "EPOL": "international",
    "ACWX": "international", "IXN": "international", "EWY": "international",
    "XLE": "sector", "XLK": "sector", "XLI": "sector", "XLP": "sector",
    "XHB": "sector", "IBB": "sector", "IYT": "sector", "VCR": "sector",
    "BOTZ": "sector", "AVUV": "sector", "SMH": "sector", "RSP": "sector",
    "XLB": "sector", "SOXX": "sector", "IDU": "sector", "COLO": "sector",
    "ITB": "sector", "ICLN": "sector", "AIRR": "sector", "UTES": "sector",
    "IYZ": "sector",
    "DFEN": "leveraged", "BULZ": "leveraged", "MIDU": "leveraged",
    "FAS": "leveraged", "TQQQ": "leveraged", "EURL": "leveraged",
    "EDC": "leveraged", "PILL": "leveraged", "UTSL": "leveraged",
    "TPOR": "leveraged",
    "IEF": "fixed_income", "TIPZ": "fixed_income", "BTAL": "fixed_income",
    "TYD": "fixed_income", "GLD": "fixed_income", "HYG": "fixed_income",
    "CORP": "fixed_income", "SH": "fixed_income", "MNA": "sector",
    "MYY": "sector", "DBMF": "sector", "KBWP": "sector",
    "SRUUF": "international", "TRMSS": "international",
}


def spread_context_for(
    symbol: str,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
) -> SpreadContext:
    """
    Best-effort SpreadContext for a symbol.
    Uses live bid/ask if available, falls back to asset-class typical.
    """
    if bid and ask and bid > 0 and ask > bid:
        return SpreadContext.from_bid_ask(bid, ask)
    cls = _TICKER_CLASS.get(symbol.upper(), "sector")
    typical = _ASSET_CLASS_TYPICAL.get(cls, 7.0)
    return SpreadContext.from_typical(typical)
