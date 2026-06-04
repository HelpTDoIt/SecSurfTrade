"""
yfinance adapters — for off-hours development and production use when the
Fidelity Trader+ OCR adapters are unavailable or not needed.

QuoteAdapter  : YFinanceQuoteAdapter   — single-ticker QuoteSnapshot
WatchlistAdapter: YFinanceWatchlistAdapter — batch fetch of WatchlistRow for
                  all strategy tickers in one call (much faster than per-ticker)

Data quality
------------
yfinance pulls from Yahoo Finance which aggregates from multiple exchanges.
Bid/ask reflects the most recent NBBO snapshot (typically < 60 s stale during
market hours).  prev_close, ADV 10D/90D, and ex-dividend date are reliable.
VWAP is not a watchlist field (rows return 0.0); ``approx_intraday_vwap`` below
reconstructs an APPROXIMATE intraday VWAP from 1-minute bars so the VWAP rules
can fire on the yfinance path (distinct from ATP's exact streamed VWAP).

Install
-------
    pip install yfinance
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

from adapters import QuoteSnapshot, WatchlistRow

# Trading day has 6.5 hours = 390 minutes = 78 five-minute periods.
_PERIODS_PER_DAY = 78


def _require_yf():
    try:
        import yfinance as yf  # noqa: PLC0415
        return yf
    except ImportError as exc:
        raise ImportError(
            "yfinance is not installed. Install with: pip install yfinance"
        ) from exc


# ── Single-ticker quote ────────────────────────────────────────────────────

class YFinanceQuoteAdapter:
    """
    Single-ticker QuoteSnapshot from yfinance.
    Prefer YFinanceWatchlistAdapter for batch fetching multiple tickers.
    """

    def get_quote(self, symbol: str) -> QuoteSnapshot:
        yf = _require_yf()
        info = yf.Ticker(symbol.upper()).info or {}
        bid  = float(info.get("bid") or info.get("regularMarketPrice") or 0.0)
        ask  = float(info.get("ask") or bid)
        last = float(info.get("regularMarketPrice") or info.get("previousClose") or 0.0)
        return QuoteSnapshot(
            symbol=symbol.upper(),
            bid=bid,
            bid_size=int(info.get("bidSize") or 0),
            ask=ask,
            ask_size=int(info.get("askSize") or 0),
            last=last,
            prev_close=float(info.get("previousClose") or 0.0),
            volume=int(info.get("regularMarketVolume") or info.get("volume") or 0),
            ts=datetime.now(tz=timezone.utc),
        )


# ── Batch watchlist ────────────────────────────────────────────────────────

def _parse_ex_date(raw) -> str:
    """Convert yfinance exDividendDate (Unix int or None) → 'YYYY-MM-DD' string."""
    if not raw:
        return ""
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return str(raw)


def _info_to_watchlist_row(sym: str, info: dict) -> WatchlistRow:
    bid  = float(info.get("bid") or info.get("regularMarketPrice") or 0.0)
    ask  = float(info.get("ask") or bid)
    last = float(info.get("regularMarketPrice") or info.get("previousClose") or 0.0)

    # yfinance field names vary slightly across versions
    adv10 = int(
        info.get("averageVolume10days")
        or info.get("averageDailyVolume10Day")
        or 0
    )
    adv90 = int(info.get("averageVolume") or 0)  # Yahoo's "average volume" ≈ 3-month

    return WatchlistRow(
        symbol=sym,
        last=last,
        bid=bid,
        ask=ask,
        bid_size=int(info.get("bidSize") or 0),
        ask_size=int(info.get("askSize") or 0),
        volume=int(info.get("regularMarketVolume") or info.get("volume") or 0),
        prev_close=float(info.get("previousClose") or 0.0),
        avg_vol_10d=adv10,
        avg_vol_90d=adv90,
        div_ex_date=_parse_ex_date(info.get("exDividendDate")),
        div_local=float(
            info.get("lastDividendValue") or info.get("dividendsPerShare") or 0.0
        ),
        vwap=0.0,  # not provided by yfinance
        ts=datetime.now(tz=timezone.utc),
    )


class YFinanceWatchlistAdapter:
    """
    Batch-fetch WatchlistRow for a list of tickers.

    Uses yf.Tickers() to download all symbols in one HTTP round-trip,
    then pulls .info for each.  On failure for an individual symbol the
    entry is skipped and a warning is printed; other symbols still succeed.

    Usage:
        rows = YFinanceWatchlistAdapter().get_watchlist(["FRDM", "EEM", "XLE"])
        prev_close = rows["FRDM"].prev_close
        adv_10d    = rows["FRDM"].avg_vol_10d
    """

    def get_watchlist(self, symbols: list[str]) -> dict[str, WatchlistRow]:
        yf = _require_yf()
        syms = [s.upper() for s in symbols]
        results: dict[str, WatchlistRow] = {}

        # yf.Tickers downloads all at once; individual .info calls are cached
        batch = yf.Tickers(" ".join(syms))

        for sym in syms:
            try:
                ticker = batch.tickers.get(sym) or yf.Ticker(sym)
                info = ticker.info or {}
                if not info:
                    raise ValueError("empty info dict")
                results[sym] = _info_to_watchlist_row(sym, info)
            except Exception as exc:
                _log.warning("yfinance failed for %s: %s", sym, exc)

        return results


def watchlist_row_to_quote(row: WatchlistRow) -> QuoteSnapshot:
    """Convert a WatchlistRow to a QuoteSnapshot for use in strategy generation."""
    return QuoteSnapshot(
        symbol=row.symbol,
        bid=row.bid,
        bid_size=row.bid_size,
        ask=row.ask,
        ask_size=row.ask_size,
        last=row.last,
        prev_close=row.prev_close,
        volume=row.volume,
        ts=row.ts,
    )


def adv_to_vol5min(avg_vol_10d: int) -> float:
    """
    Approximate 5-minute volume from 10-day average daily volume.
    Used as the vol5min parameter in strategy generation.
    Assumes uniform volume distribution across 78 five-minute periods per day.
    """
    if avg_vol_10d <= 0:
        return 0.0
    return avg_vol_10d / _PERIODS_PER_DAY


# ── Approximate intraday VWAP (G-3) ────────────────────────────────────────

def approx_intraday_vwap(symbol: str, *, period: str = "1d") -> float | None:
    """Approximate intraday VWAP for ``symbol`` from yfinance 1-minute bars.

    yfinance does not expose VWAP directly (the watchlist row carries 0.0), so
    the VWAP-relative strategy rules cannot fire on the ``--source yfinance``
    path.  This reconstructs an APPROXIMATE intraday VWAP =
    Σ(typical_price · volume) / Σ(volume) over today's 1-minute candles, using
    the pure math in ``engine.vwap``.

    This is explicitly an approximation and is NOT the same as ATP's exact
    streamed intraday VWAP — bar granularity, Yahoo's consolidated tape, and the
    (high+low+close)/3 typical-price proxy all introduce small differences.

    Returns the VWAP float, or None when yfinance is unavailable, the history is
    empty, or there is no usable volume (so callers treat it uniformly as
    "VWAP unavailable").  Performs no mutation and only reads market data.
    """
    from engine.vwap import vwap_from_columns

    try:
        yf = _require_yf()
    except ImportError:
        return None
    try:
        hist = yf.Ticker(symbol.upper()).history(period=period, interval="1m")
        if hist is None or hist.empty:
            return None
        return vwap_from_columns(
            hist["High"].tolist(),
            hist["Low"].tolist(),
            hist["Close"].tolist(),
            hist["Volume"].tolist(),
        )
    except Exception:
        return None
