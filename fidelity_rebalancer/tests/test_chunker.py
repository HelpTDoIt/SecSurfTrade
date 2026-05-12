"""
Tests for engine.chunker — legacy $100K chunker, book-relative chunker,
tick helpers, and ex-dividend adjustment.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from engine.chunker import (
    _round_to_100,
    adjust_prev_close_for_exdiv,
    build_buy_chunks,
    build_buy_chunks_legacy,
    build_sell_chunks,
    build_sell_chunks_legacy,
    round_to_tick,
    tick,
)


@dataclass
class _Lvl:
    price: float
    size: float


# ── round_to_100 ──────────────────────────────────────────────────────────

def test_round_to_100_basic():
    assert _round_to_100(150) == 200
    assert _round_to_100(149) == 100
    assert _round_to_100(250) == 300
    assert _round_to_100(200) == 200


def test_round_to_100_zero():
    assert _round_to_100(0) == 0


# ── tick / round_to_tick ──────────────────────────────────────────────────

def test_tick_sub_dollar():
    assert tick(0.5) == 0.0001
    assert tick(0.9999) == 0.0001


def test_tick_dollar_and_above():
    assert tick(1.0) == 0.01
    assert tick(123.45) == 0.01


def test_round_to_tick_uses_ref_price():
    # ref_price < 1 → 0.0001 tick
    assert round_to_tick(0.12348, 0.5) == pytest.approx(0.1235)
    # ref_price >= 1 → 0.01 tick
    assert round_to_tick(123.458, 50.0) == pytest.approx(123.46)


def test_round_to_tick_default_ref():
    assert round_to_tick(50.123) == pytest.approx(50.12)


# ── Legacy $100K chunker (parity with React calc) ─────────────────────────

def test_legacy_sell_no_chunk_needed():
    chunks = build_sell_chunks_legacy(200.0, 62.71)
    assert len(chunks) == 1
    assert chunks[0]["shares"] == 200.0
    assert chunks[0]["limit_price"] == pytest.approx(62.71)
    assert chunks[0]["cost"] == pytest.approx(200 * 62.71)


def test_legacy_sell_chunking_large_order():
    chunks = build_sell_chunks_legacy(2000.0, 100.0)
    assert len(chunks) == 2
    assert chunks[0]["shares"] == 1000
    assert chunks[1]["shares"] == 1000


def test_legacy_sell_round_to_100():
    chunks = build_sell_chunks_legacy(3000.0, 62.71)
    assert chunks[0]["shares"] == 1600


def test_legacy_sell_zero_remaining():
    assert build_sell_chunks_legacy(0.0, 62.71) == []


def test_legacy_buy_no_chunk_needed():
    chunks = build_buy_chunks_legacy(12642.0, 55.0)
    assert len(chunks) == 1
    assert chunks[0]["shares"] == 229
    assert chunks[0]["cost"] == pytest.approx(229 * 55.0)


def test_legacy_buy_chunking_large_budget():
    chunks = build_buy_chunks_legacy(200_000.0, 100.0)
    assert len(chunks) == 2
    assert chunks[0]["shares"] == 1000
    assert chunks[1]["shares"] == 1000


def test_legacy_buy_zero_budget():
    assert build_buy_chunks_legacy(0.0, 55.0) == []


def test_legacy_buy_zero_limit():
    assert build_buy_chunks_legacy(10_000.0, 0.0) == []


def test_legacy_buy_total_cost_within_budget():
    chunks = build_buy_chunks_legacy(175_000.0, 73.45)
    total = sum(ch["cost"] for ch in chunks)
    assert total <= 175_000.0 + 0.01


# ── Book-relative chunker ─────────────────────────────────────────────────

def test_book_sell_depth_caps_chunk():
    """
    top-3 bid depth = 1000+1000+1000 = 3000 shares
    cap_by_depth = floor(0.25 * 3000 / 100) * 100 = 700
    cap_by_vol   = floor(0.15 * 100_000 / 100) * 100 = 15000
    cap = min(700, 15000) = 700
    1500 shares → 3 chunks of 700, 700, 100
    """
    bids = [_Lvl(100.0, 1000), _Lvl(99.99, 1000), _Lvl(99.98, 1000)]
    chunks = build_sell_chunks(1500.0, 100.0, bids, vol5min=100_000.0)
    assert chunks[0]["shares"] == 700
    assert chunks[1]["shares"] == 700
    assert chunks[2]["shares"] == 100
    assert sum(c["shares"] for c in chunks) == 1500


def test_book_sell_volume_caps_chunk():
    """
    Tiny vol5min → cap = floor(0.15 * 1000 / 100) * 100 = 100.
    """
    bids = [_Lvl(100.0, 100_000)]  # huge depth
    chunks = build_sell_chunks(500.0, 100.0, bids, vol5min=1000.0,
                                max_pct_of_top3_depth=0.25,
                                max_pct_of_5min_volume=0.15)
    assert chunks[0]["shares"] == 100
    assert sum(c["shares"] for c in chunks) == 500


def test_book_sell_no_book_data_falls_to_minimum():
    """Empty book + zero volume → 100-share floor so we still progress."""
    chunks = build_sell_chunks(250.0, 100.0, [], vol5min=0.0)
    assert chunks[0]["shares"] == 100
    assert sum(c["shares"] for c in chunks) == 250


def test_book_buy_budget_respected():
    """
    asks: top-3 = 500+500+500 = 1500 shares
    cap_by_depth = floor(0.25 * 1500 / 100) * 100 = 300
    Budget $50_000 @ $100 → 500 shares max by budget.
    Expect chunks of 300, 200 with total cost ≤ 50000.
    """
    asks = [_Lvl(100.0, 500), _Lvl(100.01, 500), _Lvl(100.02, 500)]
    chunks = build_buy_chunks(50_000.0, 100.0, asks, vol5min=1_000_000.0)
    assert chunks[0]["shares"] == 300
    assert sum(c["cost"] for c in chunks) <= 50_000.0 + 1e-6


def test_book_buy_zero_budget():
    asks = [_Lvl(100.0, 500)]
    assert build_buy_chunks(0.0, 100.0, asks, vol5min=10_000.0) == []


# ── Ex-dividend adjustment ────────────────────────────────────────────────

def test_exdiv_first_of_month_with_calendar_match():
    cal = {"SPY": {"2026-05-01": 1.50}}
    adj = adjust_prev_close_for_exdiv("SPY", 500.00, date(2026, 5, 1), calendar=cal)
    assert adj == pytest.approx(498.50)


def test_exdiv_first_of_month_no_calendar_match():
    cal = {"SPY": {"2026-04-01": 1.25}}  # different date
    adj = adjust_prev_close_for_exdiv("SPY", 500.00, date(2026, 5, 1), calendar=cal)
    assert adj == pytest.approx(500.00)


def test_exdiv_not_first_of_month_skips():
    """Outside 1st-of-month, helper is a no-op."""
    cal = {"SPY": {"2026-05-15": 1.50}}
    adj = adjust_prev_close_for_exdiv("SPY", 500.00, date(2026, 5, 15), calendar=cal)
    assert adj == pytest.approx(500.00)


def test_exdiv_case_insensitive_symbol_lookup():
    cal = {"QQQ": {"2026-05-01": 0.65}}
    adj = adjust_prev_close_for_exdiv("qqq", 400.00, date(2026, 5, 1), calendar=cal)
    assert adj == pytest.approx(399.35)


def test_exdiv_unknown_symbol():
    cal = {"SPY": {"2026-05-01": 1.50}}
    adj = adjust_prev_close_for_exdiv("XYZ", 100.00, date(2026, 5, 1), calendar=cal)
    assert adj == pytest.approx(100.00)
