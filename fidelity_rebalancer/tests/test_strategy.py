"""
Snapshot-style tests for engine.strategy_sell and engine.strategy_buy.

Each rule branch gets at least one synthetic quote + L2 fixture and we
assert the rule chosen, the limit price, urgency, and chunk count.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

import pytest

from adapters import Level, Level2Snapshot, QuoteSnapshot
from engine.decision_context import DecisionContext
from engine.strategy_buy import generate_buy_strategy
from engine.strategy_sell import generate_sell_strategy
from state.schema import BuyAllocationRecord, ChunkRecord, SellRecord


# ── Helpers ───────────────────────────────────────────────────────────────


def _quote(
    symbol: str,
    *,
    bid: float,
    ask: float,
    last: float,
    prev_close: float,
    volume: int = 1_000_000,
) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=symbol,
        bid=bid,
        bid_size=500,
        ask=ask,
        ask_size=500,
        last=last,
        prev_close=prev_close,
        volume=volume,
        ts=datetime.now(tz=timezone.utc),
    )


def _l2(
    symbol: str, bids: list[tuple[float, int]], asks: list[tuple[float, int]]
) -> Level2Snapshot:
    return Level2Snapshot(
        symbol=symbol,
        bids=[Level(price=p, size=s, mpid="ARCX") for p, s in bids],
        asks=[Level(price=p, size=s, mpid="ARCX") for p, s in asks],
        ts=datetime.now(tz=timezone.utc),
    )


# ── Sell rules ────────────────────────────────────────────────────────────


def test_sell_tight_spread_small_position():
    """Spread 2bps, rel_vol 1.5, 1% ADV → MID, normal."""
    sell = SellRecord(
        account="Test Retirement",
        strategy="Strategy Gamma",
        ticker="SPY",
        shares=1000,
        limit_price=500.0,
        est_proceeds=500_000,
    )
    quote = _quote(
        "SPY",
        bid=499.99,
        ask=500.01,
        last=500.00,
        prev_close=499.50,
        volume=150_000_000,
    )
    book = _l2("SPY", [(499.99, 5000)] * 3, [(500.01, 5000)] * 3)

    strat, chunks = generate_sell_strategy(
        sell,
        quote,
        book,
        vol5min=500_000.0,
        today=date(2026, 4, 15),
        ctx=DecisionContext(adv=100_000_000),  # 1% ADV
    )
    assert strat.rule == "tight_spread_small_position"
    assert strat.urgency == "normal"
    assert strat.limit_price == pytest.approx(500.00)
    assert len(chunks) >= 1
    assert any(re.search(r"Spread is \d+\.\d bps", b) for b in strat.reasoning)


def test_sell_tight_spread_large_position():
    """Spread 2bps, 6% ADV → BID, patient."""
    sell = SellRecord(
        account="Test Retirement",
        strategy="Strategy Beta",
        ticker="SPY",
        shares=6_000_000,
        limit_price=500.0,
        est_proceeds=3e9,
    )
    quote = _quote(
        "SPY", bid=499.99, ask=500.01, last=500.00, prev_close=500.00, volume=10_000_000
    )
    book = _l2("SPY", [(499.99, 5000)] * 3, [(500.01, 5000)] * 3)

    strat, _ = generate_sell_strategy(
        sell,
        quote,
        book,
        vol5min=500_000.0,
        today=date(2026, 4, 15),
        ctx=DecisionContext(adv=100_000_000),  # 6% ADV
    )
    assert strat.rule == "tight_spread_large_position"
    assert strat.urgency == "patient"
    assert strat.limit_price == pytest.approx(499.99)


def test_sell_wide_spread():
    """Spread 20bps → BID + 1 tick, patient."""
    sell = SellRecord(
        account="Test Retirement",
        strategy="Strategy Alpha",
        ticker="ABC",
        shares=200,
        limit_price=100.0,
        est_proceeds=20_000,
    )
    quote = _quote(
        "ABC", bid=99.90, ask=100.10, last=100.00, prev_close=100.00, volume=50_000
    )
    book = _l2("ABC", [(99.90, 200)] * 3, [(100.10, 200)] * 3)

    strat, _ = generate_sell_strategy(
        sell,
        quote,
        book,
        vol5min=10_000.0,
        today=date(2026, 4, 15),
        ctx=DecisionContext(adv=100_000),
    )
    assert strat.rule == "wide_spread"
    assert strat.urgency == "patient"
    assert strat.limit_price == pytest.approx(99.91)


def test_sell_down_day():
    """Spread 7bps (neither tight<5 nor wide>10), last −3% from prev close → 0.99 × prev."""
    sell = SellRecord(
        account="Test Retirement",
        strategy="Strategy Delta",
        ticker="DEF",
        shares=200,
        limit_price=100.0,
        est_proceeds=20_000,
    )
    # Spread = 0.07, midpoint=100.005 → bps≈7.0
    quote = _quote(
        "DEF", bid=99.97, ask=100.04, last=97.00, prev_close=100.00, volume=200_000
    )
    book = _l2("DEF", [(99.97, 500)] * 3, [(100.04, 500)] * 3)

    strat, _ = generate_sell_strategy(
        sell,
        quote,
        book,
        vol5min=20_000.0,
        today=date(2026, 4, 15),
        ctx=DecisionContext(adv=1_000_000),
    )
    assert strat.rule == "down_day"
    assert strat.urgency == "patient"
    assert strat.limit_price == pytest.approx(99.00)


def test_sell_up_day():
    """Spread 7bps, last +3% → BID, aggressive."""
    sell = SellRecord(
        account="Test Retirement",
        strategy="Strategy Gamma",
        ticker="GHI",
        shares=200,
        limit_price=100.0,
        est_proceeds=20_000,
    )
    quote = _quote(
        "GHI", bid=102.97, ask=103.04, last=103.00, prev_close=100.00, volume=200_000
    )
    book = _l2("GHI", [(102.97, 500)] * 3, [(103.04, 500)] * 3)

    strat, _ = generate_sell_strategy(
        sell,
        quote,
        book,
        vol5min=20_000.0,
        today=date(2026, 4, 15),
        ctx=DecisionContext(adv=1_000_000),
    )
    assert strat.rule == "up_day"
    assert strat.urgency == "aggressive"
    assert strat.limit_price == pytest.approx(102.97)


def test_sell_default_midpoint():
    """No conditions met (spread ~7bps, day flat, mid-size) → midpoint, normal."""
    sell = SellRecord(
        account="Test Retirement",
        strategy="Strategy Gamma",
        ticker="JKL",
        shares=200,
        limit_price=100.0,
        est_proceeds=20_000,
    )
    quote = _quote(
        "JKL", bid=99.965, ask=100.035, last=100.00, prev_close=100.00, volume=200_000
    )
    book = _l2("JKL", [(99.96, 500)] * 3, [(100.03, 500)] * 3)

    strat, _ = generate_sell_strategy(
        sell,
        quote,
        book,
        vol5min=20_000.0,
        today=date(2026, 4, 15),
        ctx=DecisionContext(adv=1_000_000),
    )
    assert strat.rule == "default"
    assert strat.urgency == "normal"


# ── Buy rules ─────────────────────────────────────────────────────────────


def test_buy_tight_spread_good_volume():
    """Spread 2bps, rel_vol 1.5 → ASK, normal."""
    buy = BuyAllocationRecord(
        account="Test Retirement",
        strategy="Strategy Gamma",
        ticker="SPY",
        dollar_target=50_000,
        limit_price=500.0,
        share_target=100,
        est_cost=50_000,
    )
    quote = _quote(
        "SPY",
        bid=499.99,
        ask=500.01,
        last=500.00,
        prev_close=500.00,
        volume=150_000_000,
    )
    book = _l2("SPY", [(499.99, 5000)] * 3, [(500.01, 5000)] * 3)

    strat, chunks = generate_buy_strategy(
        buy,
        quote,
        book,
        vol5min=500_000.0,
        ctx=DecisionContext(adv=100_000_000),
    )
    assert strat.rule == "tight_spread_good_volume"
    assert strat.urgency == "normal"
    assert strat.limit_price == pytest.approx(500.01)
    total_cost = sum(c.cost for c in chunks)
    assert total_cost <= 50_000.0 + 1e-6


def test_buy_wide_spread():
    """Spread 20bps → MID, patient."""
    buy = BuyAllocationRecord(
        account="Test Retirement",
        strategy="Strategy Beta",
        ticker="MNO",
        dollar_target=10_000,
        limit_price=100.0,
        share_target=100,
        est_cost=10_000,
    )
    quote = _quote(
        "MNO", bid=99.90, ask=100.10, last=100.00, prev_close=100.00, volume=50_000
    )
    book = _l2("MNO", [(99.90, 200)] * 3, [(100.10, 200)] * 3)

    strat, _ = generate_buy_strategy(
        buy,
        quote,
        book,
        vol5min=10_000.0,
        ctx=DecisionContext(adv=1_000_000),
    )
    assert strat.rule == "wide_spread"
    assert strat.urgency == "patient"
    assert strat.limit_price == pytest.approx(100.00)


def test_buy_large_position():
    """Spread 7bps (not tight, not wide), 4% ADV → ASK − 1 tick, patient, smaller chunks."""
    buy = BuyAllocationRecord(
        account="Test Retirement",
        strategy="Strategy Delta",
        ticker="PQR",
        dollar_target=400_000,
        limit_price=100.0,
        share_target=4000,
        est_cost=400_000,
    )
    quote = _quote(
        "PQR", bid=99.965, ask=100.035, last=100.00, prev_close=100.00, volume=200_000
    )
    book = _l2("PQR", [(99.96, 500)] * 3, [(100.03, 500)] * 3)

    strat, chunks = generate_buy_strategy(
        buy,
        quote,
        book,
        vol5min=20_000.0,
        ctx=DecisionContext(adv=100_000),  # 4% ADV
    )
    assert strat.rule == "large_position"
    assert strat.urgency == "patient"
    assert strat.limit_price == pytest.approx(100.02)  # 100.03 - 0.01
    # depth_pct halved → smaller per-chunk caps; expect more chunks
    assert len(chunks) >= 2


def test_buy_default():
    """Spread 7bps, no big position, no good volume → default ask, normal."""
    buy = BuyAllocationRecord(
        account="Test Retirement",
        strategy="Strategy Gamma",
        ticker="STU",
        dollar_target=10_000,
        limit_price=100.0,
        share_target=100,
        est_cost=10_000,
    )
    quote = _quote(
        "STU", bid=99.965, ask=100.035, last=100.00, prev_close=100.00, volume=200_000
    )
    book = _l2("STU", [(99.96, 500)] * 3, [(100.03, 500)] * 3)

    strat, _ = generate_buy_strategy(
        buy,
        quote,
        book,
        vol5min=20_000.0,
        ctx=DecisionContext(adv=10_000_000),  # rel_vol 0.02 (low), pct_of_adv=0.001%
    )
    assert strat.rule == "default"
    assert strat.urgency == "normal"


# ── Round-trip serialization (acceptance criterion #6) ────────────────────


def test_strategy_round_trip_serializes_byte_identical():
    sell = SellRecord(
        account="Test Retirement",
        strategy="Strategy Gamma",
        ticker="SPY",
        shares=1000,
        limit_price=500.0,
        est_proceeds=500_000,
    )
    quote = _quote(
        "SPY",
        bid=499.99,
        ask=500.01,
        last=500.00,
        prev_close=499.50,
        volume=150_000_000,
    )
    book = _l2("SPY", [(499.99, 5000)] * 3, [(500.01, 5000)] * 3)

    strat, _ = generate_sell_strategy(
        sell,
        quote,
        book,
        vol5min=500_000.0,
        today=date(2026, 4, 15),
        ctx=DecisionContext(adv=100_000_000),
    )
    json1 = strat.model_dump_json()
    strat2 = type(strat).model_validate_json(json1)
    json2 = strat2.model_dump_json()
    assert json1 == json2


# ── _reconcile_records_to_chunks ──────────────────────────────────────────


class _FakeComputed:
    def __init__(self, sells, buy_allocations, sell_chunks, buy_chunks):
        self.sells = sells
        self.buy_allocations = buy_allocations
        self.sell_chunks = sell_chunks
        self.buy_chunks = buy_chunks


class _FakeState:
    def __init__(self, computed):
        self.computed = computed


def _chunk(account, strategy, ticker, idx, shares, limit, cost):
    return ChunkRecord(
        chunk_id=f"{ticker}{idx}",
        account=account,
        strategy=strategy,
        ticker=ticker,
        idx=idx,
        shares=shares,
        limit_price=limit,
        cost=cost,
    )


def test_reconcile_records_to_chunks_reprices_and_floors():
    """Records sized at prev-close are reconciled DOWN to their re-priced chunks.

    Covers both divergence mechanisms from the live trade:
      * BUY priced differently (record @55.68, chunks @58.93)
      * SELL floored to whole shares (record 4138.627, chunks sum to 4138)
    """
    from cli.strategy import _reconcile_records_to_chunks

    sells = [
        SellRecord(
            account="Rollover IRA",
            strategy="Prismatic Prudence",
            ticker="EEM",
            shares=4138.627,
            limit_price=68.60,
            est_proceeds=283_889.81,
        )
    ]
    buys = [
        BuyAllocationRecord(
            account="Roth IRA",
            strategy="World Try -Top",
            ticker="DFIV",
            dollar_target=133_767.04,
            limit_price=55.68,
            share_target=2402,
            est_cost=133_743.36,
        )
    ]
    sell_chunks = [
        _chunk(
            "Rollover IRA", "Prismatic Prudence", "EEM", 0, 2100.0, 68.56, 143_976.0
        ),
        _chunk(
            "Rollover IRA", "Prismatic Prudence", "EEM", 1, 2038.0, 68.56, 139_725.28
        ),
    ]
    buy_chunks = [
        _chunk("Roth IRA", "World Try -Top", "DFIV", 0, 1200.0, 58.93, 70_716.0),
        _chunk("Roth IRA", "World Try -Top", "DFIV", 1, 1069.0, 58.93, 63_006.17),
    ]
    state = _FakeState(_FakeComputed(sells, buys, sell_chunks, buy_chunks))

    _reconcile_records_to_chunks(state)

    s = state.computed.sells[0]
    assert s.shares == 4138.0  # 2100 + 2038, floored total
    assert s.limit_price == 68.56  # re-priced to the chunk limit
    assert s.est_proceeds == round(143_976.0 + 139_725.28, 2)

    b = state.computed.buy_allocations[0]
    assert b.share_target == 2269  # 1200 + 1069
    assert b.limit_price == 58.93
    assert b.est_cost == round(70_716.0 + 63_006.17, 2)


def test_reconcile_leaves_chunkless_record_untouched():
    """A record with a target but no chunks is left alone so the sanity gate
    can still flag the genuinely-broken case."""
    from cli.strategy import _reconcile_records_to_chunks

    buys = [
        BuyAllocationRecord(
            account="Roth IRA",
            strategy="World Try -Top",
            ticker="DFIV",
            dollar_target=10_000.0,
            limit_price=55.68,
            share_target=179,
            est_cost=9_966.72,
        )
    ]
    state = _FakeState(_FakeComputed([], buys, [], []))

    _reconcile_records_to_chunks(state)

    b = state.computed.buy_allocations[0]
    assert b.share_target == 179
    assert b.limit_price == 55.68
    assert b.est_cost == 9_966.72


# ── L2 auto-detect: ranking + selection ───────────────────────────────────


from types import SimpleNamespace


def _wl(avg_vol_10d):
    return SimpleNamespace(avg_vol_10d=avg_vol_10d)


def test_rank_l2_candidates_orders_by_pct_of_adv():
    """Bigger order-vs-ADV ranks higher; max across sell/buy per ticker wins;
    no-ADV tickers sink to the bottom at 0.0."""
    from cli.strategy import _rank_l2_candidates

    sells = [
        SimpleNamespace(ticker="EIS", shares=2556.0),  # 2556/85k ≈ 3.0%
        SimpleNamespace(ticker="XLE", shares=6971.0),  # 6971/10M ≈ 0.07%
    ]
    buys = [
        SimpleNamespace(ticker="ICLN", share_target=14625),  # 14625/2M ≈ 0.73%
        SimpleNamespace(ticker="NOADV", share_target=100),  # no watchlist row
    ]
    watchlist = {
        "EIS": _wl(85_000),
        "XLE": _wl(10_000_000),
        "ICLN": _wl(2_000_000),
    }
    ranked = _rank_l2_candidates(sells, buys, watchlist)
    syms = [t for t, _ in ranked]
    assert syms[0] == "EIS"  # highest %ADV
    assert syms[1] == "ICLN"
    assert syms[2] == "XLE"
    assert syms[-1] == "NOADV"  # 0.0 pct, lowest priority
    assert ranked[-1][1] == 0.0


def test_select_l2_symbols_caps_and_splits_open_vs_closed():
    """Top-`cap` by priority; only open panels are 'use', closed top-priority
    tickers are 'recommend_open'."""
    from cli.strategy import _select_l2_symbols

    ranked = ["EIS", "ICLN", "DFEN", "XLE", "EEM", "ILF", "AVUV", "BULZ", "IYZ", "DFIV"]
    open_panels = {"EIS", "DFEN", "EPOL", "SPY"}  # EPOL/SPY not in our orders
    use, recommend = _select_l2_symbols(ranked, open_panels, cap=7)

    # priority slice is the first 7; EIS+DFEN are the open ones among them
    assert use == ["EIS", "DFEN"]
    # the other 5 of the top-7 are higher-impact but have no open panel
    assert recommend == ["ICLN", "XLE", "EEM", "ILF", "AVUV"]
    # rank 8-10 (BULZ/IYZ/DFIV) are beyond the cap and ignored entirely
    assert "BULZ" not in use and "BULZ" not in recommend


def test_select_l2_symbols_all_open_within_cap():
    from cli.strategy import _select_l2_symbols

    ranked = ["AAA", "BBB", "CCC"]
    use, recommend = _select_l2_symbols(ranked, {"aaa", "BBB", "ccc"}, cap=7)
    assert use == ["AAA", "BBB", "CCC"]  # case-insensitive match
    assert recommend == []


# ── Step 4 (G-5): per-asset-class %ADV thresholds ─────────────────────────
#
# Acceptance: a leveraged ETF and a large-cap at the SAME %ADV select
# DIFFERENT size rules.  These use the real shared _TICKER_CLASS buckets
# (SPY=large_cap, TQQQ=leveraged) via PositionSizeContext.


def _size_ctx(symbol: str):
    from engine.size_context import size_context_for

    return size_context_for(symbol)


def test_sell_per_class_same_pct_adv_diverges():
    """At 4% ADV + tight spread: large-cap (SPY) stays 'default' while a
    leveraged ETF (TQQQ) flips to 'tight_spread_large_position'.

    SPY large_cap cutoffs: small<3%, large>8% → 4% is neither.
    TQQQ leveraged cutoffs: small<1%, large>2.5% → 4% is large.
    """
    # Same numeric inputs for both, only the ticker (→ asset class) differs.
    def _run(symbol: str):
        sell = SellRecord(
            account="Test *0000",
            strategy="Strategy Beta",
            ticker=symbol,
            shares=400,  # 4% of adv=10_000
            limit_price=100.0,
            est_proceeds=40_000,
        )
        quote = _quote(symbol, bid=99.99, ask=100.01, last=100.00,
                       prev_close=100.00, volume=1_000_000)
        book = _l2(symbol, [(99.99, 5000)] * 3, [(100.01, 5000)] * 3)
        strat, _ = generate_sell_strategy(
            sell, quote, book, vol5min=50_000.0,
            today=date(2026, 4, 15),
            ctx=DecisionContext(adv=10_000, size_ctx=_size_ctx(symbol)),
        )
        return strat.rule

    spy_rule = _run("SPY")
    tqqq_rule = _run("TQQQ")
    assert spy_rule != tqqq_rule
    assert spy_rule == "default"
    assert tqqq_rule == "tight_spread_large_position"


def test_buy_per_class_same_pct_adv_diverges():
    """At 4% ADV (spread 7bps, low vol): large-cap (SPY) is 'default' while a
    leveraged ETF (TQQQ) flips to 'large_position'.

    SPY large_cap buy cutoff: large>5% → 4% not large.
    TQQQ leveraged buy cutoff: large>1.5% → 4% is large.
    """
    def _run(symbol: str):
        buy = BuyAllocationRecord(
            account="Test *0000",
            strategy="Strategy Delta",
            ticker=symbol,
            dollar_target=40_000,
            limit_price=100.0,
            share_target=400,  # 4% of adv=10_000
            est_cost=40_000,
        )
        quote = _quote(symbol, bid=99.965, ask=100.035, last=100.00,
                       prev_close=100.00, volume=200_000)
        book = _l2(symbol, [(99.96, 500)] * 3, [(100.03, 500)] * 3)
        strat, _ = generate_buy_strategy(
            buy, quote, book, vol5min=20_000.0,
            ctx=DecisionContext(adv=10_000, size_ctx=_size_ctx(symbol)),
        )
        return strat.rule

    spy_rule = _run("SPY")
    tqqq_rule = _run("TQQQ")
    assert spy_rule != tqqq_rule
    assert spy_rule == "default"
    assert tqqq_rule == "large_position"


def test_size_ctx_default_preserves_legacy_cutoffs():
    """Unmapped ticker → default() → legacy 2/5 (sell), 3 (buy)."""
    from engine.size_context import PositionSizeContext, size_context_for

    d = size_context_for("ZZZ_NOT_A_TICKER")
    assert d == PositionSizeContext.default()
    assert d.sell_small_pct == 2.0
    assert d.sell_large_pct == 5.0
    assert d.buy_large_pct == 3.0


def test_size_ctx_none_falls_back_to_legacy_in_decide():
    """ctx.size_ctx=None must reproduce the legacy hardcoded behavior:
    SPY at 6% ADV + tight spread → tight_spread_large_position (>5% legacy)."""
    sell = SellRecord(
        account="Test *0000",
        strategy="Strategy Beta",
        ticker="SPY",
        shares=600,  # 6% of adv=10_000 (> legacy 5%, < per-class large_cap 8%)
        limit_price=100.0,
        est_proceeds=60_000,
    )
    quote = _quote("SPY", bid=99.99, ask=100.01, last=100.00,
                   prev_close=100.00, volume=1_000_000)
    book = _l2("SPY", [(99.99, 5000)] * 3, [(100.01, 5000)] * 3)
    strat, _ = generate_sell_strategy(
        sell, quote, book, vol5min=50_000.0,
        today=date(2026, 4, 15),
        ctx=DecisionContext(adv=10_000),  # size_ctx defaults to None → legacy
    )
    # Legacy >5% large fires; with the SPY per-class (>8%) it would NOT.
    assert strat.rule == "tight_spread_large_position"


def test_g6_single_adv_path_end_to_end(monkeypatch):
    """G-6: assert ONE ADV definition flows end-to-end.

    The generator must consume ctx.adv (the watchlist 10-day ADV the CLI
    supplies) and must NOT call get_adv() (30-day yfinance fallback) when
    ctx.adv is provided.  We poison get_adv to prove it is never reached.
    """
    import engine.strategy_sell as ss

    def _boom(symbol):  # pragma: no cover - must never run
        raise AssertionError("get_adv() fallback called despite ctx.adv set")

    monkeypatch.setattr(ss, "get_adv", _boom)

    sell = SellRecord(
        account="Test *0000",
        strategy="Strategy Beta",
        ticker="SPY",
        shares=400,
        limit_price=100.0,
        est_proceeds=40_000,
    )
    quote = _quote("SPY", bid=99.99, ask=100.01, last=100.00,
                   prev_close=100.00, volume=1_000_000)
    book = _l2("SPY", [(99.99, 5000)] * 3, [(100.01, 5000)] * 3)
    strat, _ = generate_sell_strategy(
        sell, quote, book, vol5min=50_000.0,
        today=date(2026, 4, 15),
        ctx=DecisionContext(adv=10_000, size_ctx=_size_ctx("SPY")),
    )
    # 400/10_000 = 4% of ADV → reasoning reflects the single ADV source.
    assert any("4.00% of ADV" in b for b in strat.reasoning)
