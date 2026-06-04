"""
Tests for approximate intraday VWAP (G-3, Step 8).

Two layers:
  1. Pure math in ``engine.vwap`` — exercised with INJECTED synthetic bars so it
     runs without any network access (the yfinance fetch is not invoked here).
  2. The adapter ``approx_intraday_vwap`` degrades to None gracefully when
     yfinance is unavailable, and the generators fire the VWAP-relative rules
     once ``ctx.vwap`` is populated (the state the yfinance path now reaches).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from adapters import Level, Level2Snapshot, QuoteSnapshot
from engine.decision_context import DecisionContext
from engine.strategy_buy import generate_buy_strategy
from engine.strategy_sell import generate_sell_strategy
from engine.vwap import Bar, typical_price, vwap_from_bars, vwap_from_columns
from state.schema import BuyAllocationRecord, SellRecord


# ── Pure VWAP math (injected bars; no network) ────────────────────────────

def test_typical_price():
    assert typical_price(Bar(high=12.0, low=9.0, close=10.5)) == pytest.approx(
        (12.0 + 9.0 + 10.5) / 3.0
    )


def test_vwap_from_bars_volume_weighted():
    """VWAP weights by volume, not a simple average of typical prices."""
    bars = [
        Bar(high=10.0, low=10.0, close=10.0, volume=100),   # tp=10
        Bar(high=20.0, low=20.0, close=20.0, volume=300),   # tp=20
    ]
    # (10*100 + 20*300) / (100+300) = 7000/400 = 17.5
    assert vwap_from_bars(bars) == pytest.approx(17.5)


def test_vwap_from_bars_skips_zero_volume_bars():
    bars = [
        Bar(high=10.0, low=10.0, close=10.0, volume=0),     # skipped
        Bar(high=20.0, low=20.0, close=20.0, volume=50),    # only this counts
    ]
    assert vwap_from_bars(bars) == pytest.approx(20.0)


def test_vwap_from_bars_none_when_no_volume():
    assert vwap_from_bars([]) is None
    assert vwap_from_bars([Bar(high=10, low=10, close=10, volume=0)]) is None


def test_vwap_from_columns_matches_bars():
    highs = [10.0, 20.0]
    lows = [10.0, 20.0]
    closes = [10.0, 20.0]
    volumes = [100.0, 300.0]
    assert vwap_from_columns(highs, lows, closes, volumes) == pytest.approx(17.5)


def test_vwap_from_columns_ragged_uses_shortest():
    """Extra trailing values beyond the shortest column are ignored."""
    v = vwap_from_columns([10.0, 20.0, 30.0], [10.0], [10.0, 20.0], [100.0, 100.0])
    # shortest length is 1 (lows) → only the first bar (tp=10, vol=100)
    assert v == pytest.approx(10.0)


# ── Adapter degrades to None without network ──────────────────────────────

def test_approx_intraday_vwap_none_when_yf_unavailable(monkeypatch):
    """If yfinance is not importable, the adapter returns None (not a crash)."""
    import adapters.yfinance_fallback as yfb

    def _no_yf():
        raise ImportError("yfinance not installed")

    monkeypatch.setattr(yfb, "_require_yf", _no_yf)
    assert yfb.approx_intraday_vwap("SPY") is None


def test_approx_intraday_vwap_computes_from_injected_history(monkeypatch):
    """End-to-end through the adapter with a fake yfinance history (no network).

    Proves the adapter wires the 1-minute bar columns into the engine math and
    yields a non-None approximate VWAP — the value the yfinance CLI path feeds
    into DecisionContext.vwap.
    """
    import adapters.yfinance_fallback as yfb

    class _FakeSeries(list):
        def tolist(self):
            return list(self)

    class _FakeHist:
        empty = False

        def __init__(self):
            self._cols = {
                "High": _FakeSeries([10.0, 20.0]),
                "Low": _FakeSeries([10.0, 20.0]),
                "Close": _FakeSeries([10.0, 20.0]),
                "Volume": _FakeSeries([100.0, 300.0]),
            }

        def __getitem__(self, key):
            return self._cols[key]

    class _FakeTicker:
        def __init__(self, sym):
            pass

        def history(self, period="1d", interval="1m"):
            return _FakeHist()

    class _FakeYF:
        Ticker = _FakeTicker

    monkeypatch.setattr(yfb, "_require_yf", lambda: _FakeYF())
    v = yfb.approx_intraday_vwap("SPY")
    assert v == pytest.approx(17.5)  # (10*100 + 20*300)/400


# ── VWAP rules fire once ctx.vwap is populated ────────────────────────────
#
# This is the payoff of G-3: on the yfinance path ctx.vwap was always None, so
# sell rules 6/7 and buy rules 4/5 could never fire.  With an approximate VWAP
# now threaded in, they fire.  Spread is 7 bps (neither tight nor wide) and the
# day move is small (<2%) so the earlier rules fall through to the VWAP rules.

def _quote(symbol, *, bid, ask, last, prev_close, volume=1_000_000):
    return QuoteSnapshot(
        symbol=symbol, bid=bid, bid_size=500, ask=ask, ask_size=500,
        last=last, prev_close=prev_close, volume=volume,
        ts=datetime.now(tz=timezone.utc),
    )


def _l2(symbol, bids, asks):
    return Level2Snapshot(
        symbol=symbol,
        bids=[Level(price=p, size=s, mpid="ARCX") for p, s in bids],
        asks=[Level(price=p, size=s, mpid="ARCX") for p, s in asks],
        ts=datetime.now(tz=timezone.utc),
    )


def test_sell_above_vwap_rule_fires_on_yfinance_like_ctx():
    """last > vwap → sell rule 6 (above_vwap).  Without ctx.vwap this would be
    'default'; the approximate VWAP makes it fire."""
    sell = SellRecord(
        account="Test *0000", strategy="Strategy Gamma", ticker="MNO",
        shares=100, limit_price=100.0, est_proceeds=10_000,
    )
    # 7 bps spread (not tight/wide); last +0.5% (not up-day >2%).
    quote = _quote("MNO", bid=100.465, ask=100.535, last=100.50,
                   prev_close=100.00, volume=200_000)
    book = _l2("MNO", [(100.46, 500)] * 3, [(100.53, 500)] * 3)

    ctx = DecisionContext(adv=50_000_000, vwap=100.00)  # last 100.50 > vwap*1.001
    strat, _ = generate_sell_strategy(
        sell, quote, book, vol5min=20_000.0, today=date(2026, 4, 15), ctx=ctx,
    )
    assert strat.rule == "above_vwap"
    assert strat.urgency == "aggressive"


def test_sell_default_when_vwap_none():
    """Same fixture, vwap=None (the OLD yfinance behavior) → falls through to
    'default'.  Confirms G-3 is the thing that unlocks the VWAP rule."""
    sell = SellRecord(
        account="Test *0000", strategy="Strategy Gamma", ticker="MNO",
        shares=100, limit_price=100.0, est_proceeds=10_000,
    )
    quote = _quote("MNO", bid=100.465, ask=100.535, last=100.50,
                   prev_close=100.00, volume=200_000)
    book = _l2("MNO", [(100.46, 500)] * 3, [(100.53, 500)] * 3)

    ctx = DecisionContext(adv=50_000_000, vwap=None)
    strat, _ = generate_sell_strategy(
        sell, quote, book, vol5min=20_000.0, today=date(2026, 4, 15), ctx=ctx,
    )
    assert strat.rule == "default"


def test_buy_below_vwap_rule_fires_on_yfinance_like_ctx():
    """last < vwap → buy rule 4 (below_vwap)."""
    buy = BuyAllocationRecord(
        account="Test *0000", strategy="Strategy Gamma", ticker="MNO",
        dollar_target=10_000, limit_price=100.0, share_target=100, est_cost=10_000,
    )
    # 7 bps spread; last -0.5% from prev (not down-day); low rel_vol.
    quote = _quote("MNO", bid=99.465, ask=99.535, last=99.50,
                   prev_close=100.00, volume=200_000)
    book = _l2("MNO", [(99.46, 500)] * 3, [(99.53, 500)] * 3)

    ctx = DecisionContext(adv=50_000_000, vwap=100.00)  # last 99.50 < vwap*0.999
    strat, _ = generate_buy_strategy(
        buy, quote, book, vol5min=20_000.0, ctx=ctx,
    )
    assert strat.rule == "below_vwap"
