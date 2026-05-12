"""
Tests for Phase 1–4 optimizations:
  - Spread calibration (SpreadContext)
  - VWAP rules (sell above/below, buy above/below)
  - Volume profile multiplier
  - Gap capture rule + multi-phase chunks
  - Buy-side urgency escalation
  - Thin-ticker detection
  - Chunk reordering (largest first)
  - Gap capture chunker
  - Buy progress tracker
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pytest

from adapters import Level, Level2Snapshot, QuoteSnapshot
from engine.chunker import (
    _DAILY_SIGMA_BPS,
    build_gap_capture_chunks,
    estimate_impact_bps,
    vol_profile_multiplier,
)
from engine.spread_context import SpreadContext, spread_context_for
from engine.strategy_buy import _escalate_buy, generate_buy_strategy
from engine.strategy_sell import generate_sell_strategy
from state.schema import BuyAllocationRecord, SellRecord


# ── Helpers ──────────────────────────────────────────────────────────────

def _quote(symbol, *, bid, ask, last, prev_close, volume=1_000_000):
    return QuoteSnapshot(
        symbol=symbol, bid=bid, bid_size=500, ask=ask, ask_size=500,
        last=last, prev_close=prev_close, volume=volume,
        ts=datetime.now(tz=timezone.utc),
    )


def _l2(symbol, bids=None, asks=None):
    return Level2Snapshot(
        symbol=symbol,
        bids=[Level(price=p, size=s, mpid="ARCX") for p, s in (bids or [])],
        asks=[Level(price=p, size=s, mpid="ARCX") for p, s in (asks or [])],
        ts=datetime.now(tz=timezone.utc),
    )


# ── SpreadContext ────────────────────────────────────────────────────────

class TestSpreadContext:
    def test_default_thresholds(self):
        sc = SpreadContext.default()
        assert sc.tight_bps == 5.0
        assert sc.wide_bps == 10.0

    def test_from_typical(self):
        sc = SpreadContext.from_typical(20.0)
        assert sc.tight_bps == pytest.approx(14.0)
        assert sc.wide_bps == pytest.approx(30.0)

    def test_from_bid_ask(self):
        sc = SpreadContext.from_bid_ask(99.90, 100.10)
        assert sc.typical_bps == pytest.approx(20.0, rel=0.05)
        assert sc.tight_bps < sc.wide_bps

    def test_from_bid_ask_zero_mid(self):
        sc = SpreadContext.from_bid_ask(0.0, 0.0)
        assert sc == SpreadContext.default()

    def test_spread_context_for_live(self):
        sc = spread_context_for("SPY", 499.99, 500.01)
        assert sc.tight_bps < sc.wide_bps
        assert sc.typical_bps > 0

    def test_spread_context_for_fallback_known_ticker(self):
        sc = spread_context_for("DFEN")  # leveraged, no bid/ask
        assert sc.typical_bps == pytest.approx(20.0)

    def test_spread_context_for_fallback_unknown_ticker(self):
        sc = spread_context_for("ZZZZZ")  # unknown → sector default
        assert sc.typical_bps == pytest.approx(5.0)


# ── VWAP rules ───────────────────────────────────────────────────────────

class TestVWAPRules:
    def test_sell_above_vwap(self):
        sell = SellRecord(
            account="Roth IRA", strategy="World Try -Top", ticker="SPY",
            shares=200, limit_price=100.0, est_proceeds=20_000,
        )
        quote = _quote("SPY", bid=100.50, ask=100.55, last=100.52,
                        prev_close=100.40, volume=200_000)
        book = _l2("SPY", [(100.50, 500)] * 3, [(100.55, 500)] * 3)

        strat, _ = generate_sell_strategy(
            sell, quote, book, vol5min=20_000.0,
            today=date(2026, 4, 15), adv=1_000_000, vwap=100.20,
        )
        assert strat.rule == "above_vwap"
        assert strat.urgency == "aggressive"

    def test_sell_below_vwap(self):
        sell = SellRecord(
            account="Roth IRA", strategy="World Try -Top", ticker="SPY",
            shares=200, limit_price=100.0, est_proceeds=20_000,
        )
        quote = _quote("SPY", bid=99.50, ask=99.55, last=99.52,
                        prev_close=100.00, volume=200_000)
        book = _l2("SPY", [(99.50, 500)] * 3, [(99.55, 500)] * 3)

        strat, _ = generate_sell_strategy(
            sell, quote, book, vol5min=20_000.0,
            today=date(2026, 4, 15), adv=1_000_000, vwap=100.00,
        )
        assert strat.rule == "below_vwap"
        assert strat.urgency == "patient"

    def test_buy_below_vwap(self):
        buy = BuyAllocationRecord(
            account="Roth IRA", strategy="World Try -Top", ticker="SPY",
            dollar_target=10_000, limit_price=100.0,
            share_target=100, est_cost=10_000,
        )
        quote = _quote("SPY", bid=99.50, ask=99.55, last=99.52,
                        prev_close=100.00, volume=200_000)
        book = _l2("SPY", [(99.50, 500)] * 3, [(99.55, 500)] * 3)

        strat, _ = generate_buy_strategy(
            buy, quote, book, vol5min=20_000.0,
            adv=10_000_000, vwap=100.00,
        )
        assert strat.rule == "below_vwap"
        assert strat.urgency == "normal"

    def test_buy_above_vwap(self):
        buy = BuyAllocationRecord(
            account="Roth IRA", strategy="World Try -Top", ticker="SPY",
            dollar_target=10_000, limit_price=100.0,
            share_target=100, est_cost=10_000,
        )
        quote = _quote("SPY", bid=100.50, ask=100.55, last=100.52,
                        prev_close=100.00, volume=200_000)
        book = _l2("SPY", [(100.50, 500)] * 3, [(100.55, 500)] * 3)

        strat, _ = generate_buy_strategy(
            buy, quote, book, vol5min=20_000.0,
            adv=10_000_000, vwap=100.00,
        )
        assert strat.rule == "above_vwap"
        assert strat.urgency == "patient"

    def test_vwap_none_does_not_trigger(self):
        sell = SellRecord(
            account="Roth IRA", strategy="World Try -Top", ticker="JKL",
            shares=200, limit_price=100.0, est_proceeds=20_000,
        )
        quote = _quote("JKL", bid=99.965, ask=100.035, last=100.00,
                        prev_close=100.00, volume=200_000)
        book = _l2("JKL", [(99.96, 500)] * 3, [(100.03, 500)] * 3)

        strat, _ = generate_sell_strategy(
            sell, quote, book, vol5min=20_000.0,
            today=date(2026, 4, 15), adv=1_000_000, vwap=None,
        )
        assert strat.rule == "default"


# ── Volume profile ───────────────────────────────────────────────────────

class TestVolumeProfile:
    def test_opening_boost(self):
        assert vol_profile_multiplier(9, 45) == 1.8

    def test_lunch_dip(self):
        assert vol_profile_multiplier(12, 0) == 0.6

    def test_closing_spike(self):
        assert vol_profile_multiplier(15, 45) == 1.5

    def test_premarket_is_1x(self):
        assert vol_profile_multiplier(8, 0) == 1.0

    def test_after_hours_is_1x(self):
        assert vol_profile_multiplier(17, 0) == 1.0

    def test_midmorning(self):
        assert vol_profile_multiplier(10, 30) == 1.1

    def test_afternoon(self):
        assert vol_profile_multiplier(14, 0) == 1.2


# ── Realized vol impact model ────────────────────────────────────────────

class TestImpactModel:
    def test_impact_with_custom_sigma(self):
        low_vol = estimate_impact_bps(1000, 100_000, sigma_bps=50.0)
        high_vol = estimate_impact_bps(1000, 100_000, sigma_bps=200.0)
        assert high_vol > low_vol
        assert high_vol == pytest.approx(low_vol * 4.0)

    def test_impact_zero_adv(self):
        assert estimate_impact_bps(1000, 0) == 0.0
        assert estimate_impact_bps(1000, None) == 0.0

    def test_impact_default_sigma(self):
        impact = estimate_impact_bps(1000, 100_000)
        expected = 0.5 * _DAILY_SIGMA_BPS * math.sqrt(1000 / 100_000)
        assert impact == pytest.approx(expected)


# ── Gap capture ──────────────────────────────────────────────────────────

class TestGapCapture:
    def test_gap_capture_rule_fires_at_open(self):
        sell = SellRecord(
            account="Roth IRA", strategy="World Try -Top", ticker="SPY",
            shares=1000, limit_price=100.0, est_proceeds=100_000,
        )
        # Stock gapped up 1.5% from prev_close
        quote = _quote("SPY", bid=101.40, ask=101.50, last=101.45,
                        prev_close=100.00, volume=500_000)
        book = _l2("SPY")

        strat, chunks = generate_sell_strategy(
            sell, quote, book, vol5min=50_000.0,
            today=date(2026, 4, 15), adv=10_000_000,
            market_minutes=10,
        )
        assert strat.rule == "gap_capture"
        assert strat.urgency == "aggressive"
        assert len(chunks) == 3
        assert chunks[0].limit_price == pytest.approx(99.00)  # gap capture: prev×0.99
        assert chunks[0].shares == pytest.approx(300, abs=100)

    def test_gap_capture_does_not_fire_after_30min(self):
        sell = SellRecord(
            account="Roth IRA", strategy="World Try -Top", ticker="SPY",
            shares=1000, limit_price=100.0, est_proceeds=100_000,
        )
        quote = _quote("SPY", bid=101.40, ask=101.50, last=101.45,
                        prev_close=100.00, volume=500_000)
        book = _l2("SPY")

        strat, _ = generate_sell_strategy(
            sell, quote, book, vol5min=50_000.0,
            today=date(2026, 4, 15), adv=10_000_000,
            market_minutes=45,
        )
        assert strat.rule != "gap_capture"

    def test_gap_capture_does_not_fire_on_flat_open(self):
        sell = SellRecord(
            account="Roth IRA", strategy="World Try -Top", ticker="SPY",
            shares=1000, limit_price=100.0, est_proceeds=100_000,
        )
        quote = _quote("SPY", bid=100.00, ask=100.02, last=100.01,
                        prev_close=100.00, volume=500_000)
        book = _l2("SPY")

        strat, _ = generate_sell_strategy(
            sell, quote, book, vol5min=50_000.0,
            today=date(2026, 4, 15), adv=10_000_000,
            market_minutes=10,
        )
        assert strat.rule != "gap_capture"


class TestGapCaptureChunker:
    def test_three_phases(self):
        chunks = build_gap_capture_chunks(1000, 99.00, 100.00, 98.50)
        assert len(chunks) == 3
        total = sum(c["shares"] for c in chunks)
        assert total == 1000
        assert chunks[0]["phase"] == "gap_capture"
        assert chunks[1]["phase"] == "standard"
        assert chunks[2]["phase"] == "sweep"
        assert chunks[0]["limit_price"] == 99.00
        assert chunks[1]["limit_price"] == 100.00
        assert chunks[2]["limit_price"] == 98.50

    def test_zero_shares(self):
        assert build_gap_capture_chunks(0, 99.0, 100.0, 98.5) == []

    def test_small_order_still_splits(self):
        chunks = build_gap_capture_chunks(200, 99.00, 100.00, 98.50)
        total = sum(c["shares"] for c in chunks)
        assert total == 200


# ── Buy urgency escalation ──────────────────────────────────────────────

class TestBuyUrgencyEscalation:
    def test_no_escalation_before_90min(self):
        quote = _quote("SPY", bid=100.0, ask=100.02, last=100.01,
                        prev_close=100.0)
        urg, lim, reasoning = _escalate_buy(
            "patient", 100.00, [], quote, 0.01, market_minutes=60,
        )
        assert urg == "patient"
        assert lim == 100.00

    def test_escalation_at_120min(self):
        quote = _quote("SPY", bid=100.0, ask=100.02, last=100.01,
                        prev_close=100.0)
        urg, lim, reasoning = _escalate_buy(
            "patient", 100.00, [], quote, 0.01, market_minutes=120,
        )
        assert urg == "normal"
        assert lim > 100.00

    def test_escalation_at_240min(self):
        quote = _quote("SPY", bid=100.0, ask=100.02, last=100.01,
                        prev_close=100.0)
        urg, lim, reasoning = _escalate_buy(
            "patient", 99.90, [], quote, 0.01, market_minutes=240,
        )
        assert urg == "aggressive"

    def test_escalation_at_350min(self):
        quote = _quote("SPY", bid=100.0, ask=100.02, last=100.01,
                        prev_close=100.0)
        urg, lim, reasoning = _escalate_buy(
            "normal", 100.00, [], quote, 0.01, market_minutes=350,
        )
        assert urg == "aggressive"
        assert lim == pytest.approx(100.03)  # ask + 1 tick

    def test_no_escalation_outside_market(self):
        quote = _quote("SPY", bid=100.0, ask=100.02, last=100.01,
                        prev_close=100.0)
        urg, lim, _ = _escalate_buy(
            "patient", 100.00, [], quote, 0.01, market_minutes=None,
        )
        assert urg == "patient"

    def test_already_aggressive_no_downgrade(self):
        quote = _quote("SPY", bid=100.0, ask=100.02, last=100.01,
                        prev_close=100.0)
        urg, lim, _ = _escalate_buy(
            "aggressive", 100.02, [], quote, 0.01, market_minutes=120,
        )
        assert urg == "aggressive"

    def test_escalation_wired_through_generate(self):
        buy = BuyAllocationRecord(
            account="Roth IRA", strategy="World Try -Top", ticker="SPY",
            dollar_target=10_000, limit_price=100.0,
            share_target=100, est_cost=10_000,
        )
        quote = _quote("SPY", bid=99.965, ask=100.035, last=100.00,
                        prev_close=100.00, volume=200_000)
        book = _l2("SPY", [(99.96, 500)] * 3, [(100.03, 500)] * 3)

        strat_early, _ = generate_buy_strategy(
            buy, quote, book, vol5min=20_000.0,
            adv=10_000_000, market_minutes=30,
        )
        strat_late, _ = generate_buy_strategy(
            buy, quote, book, vol5min=20_000.0,
            adv=10_000_000, market_minutes=350,
        )
        assert strat_late.urgency == "aggressive"


# ── Thin-ticker detection ───────────────────────────────────────────────

class TestThinTickerDetection:
    def test_detects_thin_sell(self):
        from adapters import WatchlistRow
        from cli.strategy import _detect_thin_tickers

        sells = [SellRecord(
            account="Roth IRA", strategy="S", ticker="THIN",
            shares=5000, limit_price=10.0, est_proceeds=50_000,
        )]
        buys = []
        watchlist = {
            "THIN": WatchlistRow(
                symbol="THIN", last=10.0, bid=9.99, ask=10.01,
                bid_size=100, ask_size=100, volume=5000,
                prev_close=10.0, avg_vol_10d=100_000, avg_vol_90d=100_000,
                div_ex_date="", div_local=0.0, vwap=10.0,
                ts=datetime.now(tz=timezone.utc),
            ),
        }
        thin = _detect_thin_tickers(sells, buys, watchlist)
        assert len(thin) == 1
        assert thin[0][0] == "THIN"
        assert thin[0][1] == "SELL"
        assert thin[0][2] == pytest.approx(5.0)  # 5000/100000*100

    def test_no_thin_for_liquid_ticker(self):
        from adapters import WatchlistRow
        from cli.strategy import _detect_thin_tickers

        sells = [SellRecord(
            account="Roth IRA", strategy="S", ticker="SPY",
            shares=100, limit_price=500.0, est_proceeds=50_000,
        )]
        watchlist = {
            "SPY": WatchlistRow(
                symbol="SPY", last=500.0, bid=499.99, ask=500.01,
                bid_size=5000, ask_size=5000, volume=50_000_000,
                prev_close=500.0, avg_vol_10d=100_000_000, avg_vol_90d=100_000_000,
                div_ex_date="", div_local=0.0, vwap=500.0,
                ts=datetime.now(tz=timezone.utc),
            ),
        }
        thin = _detect_thin_tickers(sells, [], watchlist)
        assert len(thin) == 0


# ── Progress tracker ────────────────────────────────────────────────────

class TestBuyProgress:
    def test_time_elapsed_pct(self):
        from cli.progress import _time_elapsed_pct

        # Market open: 0%
        assert _time_elapsed_pct(datetime(2026, 5, 6, 9, 30)) == pytest.approx(0.0)
        # Mid-day (12:45 = 195 min)
        assert _time_elapsed_pct(datetime(2026, 5, 6, 12, 45)) == pytest.approx(50.0, abs=0.5)
        # Market close: 100%
        assert _time_elapsed_pct(datetime(2026, 5, 6, 16, 0)) == pytest.approx(100.0)
        # Pre-market: 0%
        assert _time_elapsed_pct(datetime(2026, 5, 6, 8, 0)) == pytest.approx(0.0)
        # After hours: 100%
        assert _time_elapsed_pct(datetime(2026, 5, 6, 17, 0)) == pytest.approx(100.0)
