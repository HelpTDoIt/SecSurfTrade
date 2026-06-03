"""
Tests for engine.observability — the engine's neutral logging seam.

Two mechanisms under test:
  * setup_logging        — file always, console only when verbose; DEBUG gated
                           at the root so sensitive per-ticker detail stays out
                           of the default log; idempotent across calls.
  * enable/record/disable — structured JSONL decision log, inert until enabled
                           so pure engine functions stay side-effect-free.

Plus an integration check that the real sell/buy generators emit a
``strategy_decision`` record when (and only when) the log is enabled.

The autouse fixture restores global logging state AND disables the decision log
after every test.  This is not optional: the generators call
``observability.record`` unconditionally, so a decision log left enabled (and
pointing at a now-deleted tmp_path) would make every later strategy test in the
session raise on write.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from adapters import Level, Level2Snapshot, QuoteSnapshot
from engine import observability
from engine.decision_context import DecisionContext
from engine.strategy_buy import generate_buy_strategy
from engine.strategy_sell import generate_sell_strategy
from state.schema import BuyAllocationRecord, SellRecord


@pytest.fixture(autouse=True)
def _isolate_observability():
    """Snapshot/restore the root logger and always disable the decision log."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        yield
    finally:
        for h in root.handlers[:]:
            if h not in saved_handlers:
                h.close()  # release the file lock (Windows) before tmp cleanup
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        observability.disable_decision_log()


# ── Fixtures mirroring tests/test_strategy.py ──────────────────────────────


def _quote(symbol: str, *, bid, ask, last, prev_close, volume=1_000_000) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=symbol, bid=bid, bid_size=500, ask=ask, ask_size=500,
        last=last, prev_close=prev_close, volume=volume,
        ts=datetime.now(tz=timezone.utc),
    )


def _l2(symbol, bids, asks) -> Level2Snapshot:
    return Level2Snapshot(
        symbol=symbol,
        bids=[Level(price=p, size=s, mpid="ARCX") for p, s in bids],
        asks=[Level(price=p, size=s, mpid="ARCX") for p, s in asks],
        ts=datetime.now(tz=timezone.utc),
    )


# ── setup_logging ──────────────────────────────────────────────────────────


def test_setup_logging_creates_file_and_returns_path(tmp_path: Path):
    path = observability.setup_logging(tmp_path, verbose=False)
    assert path == tmp_path / "strategy.log"
    assert path.exists()


def test_setup_logging_custom_filename(tmp_path: Path):
    path = observability.setup_logging(tmp_path, verbose=False, filename="engine.log")
    assert path.name == "engine.log"


def test_setup_logging_idempotent_no_duplicate_handlers(tmp_path: Path):
    observability.setup_logging(tmp_path, verbose=False)
    observability.setup_logging(tmp_path, verbose=False)
    # Cleared-then-readded each call: exactly one (file) handler, not two.
    assert len(logging.getLogger().handlers) == 1


def test_setup_logging_default_level_is_info(tmp_path: Path):
    observability.setup_logging(tmp_path, verbose=False)
    assert logging.getLogger().level == logging.INFO
    assert len(logging.getLogger().handlers) == 1  # file only, no console


def test_setup_logging_verbose_adds_console_and_debug(tmp_path: Path):
    observability.setup_logging(tmp_path, verbose=True)
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) == 2  # file + console


def test_debug_is_gated_out_of_default_log(tmp_path: Path):
    """A DEBUG line (the sensitive per-ticker detail) must NOT reach the file
    unless verbose — it's filtered at the root level."""
    path = observability.setup_logging(tmp_path, verbose=False)
    log = logging.getLogger("engine.probe")
    log.info("operational-info")
    log.debug("sensitive-debug-detail")
    text = path.read_text(encoding="utf-8")
    assert "operational-info" in text
    assert "sensitive-debug-detail" not in text


def test_debug_reaches_file_when_verbose(tmp_path: Path):
    path = observability.setup_logging(tmp_path, verbose=True)
    logging.getLogger("engine.probe").debug("sensitive-debug-detail")
    assert "sensitive-debug-detail" in path.read_text(encoding="utf-8")


# ── decision log lifecycle ─────────────────────────────────────────────────


def test_record_inert_until_enabled(tmp_path: Path):
    assert observability.decision_log_enabled() is False
    # No destination set → no file written, no error.
    observability.record("strategy_decision", {"ticker": "ZZZ"})
    assert not (tmp_path / "decisions.jsonl").exists()


def test_enable_record_writes_jsonl(tmp_path: Path):
    path = tmp_path / "decisions.jsonl"
    assert observability.enable_decision_log(path) == path
    assert observability.decision_log_enabled() is True

    observability.record("strategy_decision", {"ticker": "AAA", "rule": "default"})
    observability.record("strategy_decision", {"ticker": "BBB", "rule": "wide_spread"})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event_type"] == "strategy_decision"
    assert first["payload"]["ticker"] == "AAA"
    assert "ts" in first  # every record carries a timestamp


def test_disable_makes_record_noop(tmp_path: Path):
    path = tmp_path / "decisions.jsonl"
    observability.enable_decision_log(path)
    observability.record("strategy_decision", {"ticker": "AAA"})
    observability.disable_decision_log()
    assert observability.decision_log_enabled() is False
    observability.record("strategy_decision", {"ticker": "BBB"})  # dropped
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # only the pre-disable record survived


# ── generators emit strategy_decision (integration) ────────────────────────


def test_sell_generator_emits_decision_when_enabled(tmp_path: Path):
    path = tmp_path / "decisions.jsonl"
    observability.enable_decision_log(path)

    sell = SellRecord(
        account="Test Retirement", strategy="Strategy Gamma", ticker="SPY",
        shares=1000, limit_price=500.0, est_proceeds=500_000,
    )
    quote = _quote("SPY", bid=499.99, ask=500.01, last=500.00,
                   prev_close=499.50, volume=150_000_000)
    book = _l2("SPY", [(499.99, 5000)] * 3, [(500.01, 5000)] * 3)
    strat, _ = generate_sell_strategy(
        sell, quote, book, vol5min=500_000.0,
        today=date(2026, 4, 15), ctx=DecisionContext(adv=100_000_000),
    )

    events = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    decisions = [e for e in events if e["event_type"] == "strategy_decision"]
    assert len(decisions) == 1
    p = decisions[0]["payload"]
    assert p["side"] == "sell"
    assert p["ticker"] == "SPY"
    assert p["rule"] == strat.rule
    assert p["n_chunks"] >= 1
    assert "spread_bps" in p["features"]


def test_buy_generator_emits_decision_when_enabled(tmp_path: Path):
    path = tmp_path / "decisions.jsonl"
    observability.enable_decision_log(path)

    buy = BuyAllocationRecord(
        account="Test Retirement", strategy="Strategy Gamma", ticker="SPY",
        dollar_target=50_000, limit_price=500.0, share_target=100, est_cost=50_000,
    )
    quote = _quote("SPY", bid=499.99, ask=500.01, last=500.00,
                   prev_close=500.00, volume=150_000_000)
    book = _l2("SPY", [(499.99, 5000)] * 3, [(500.01, 5000)] * 3)
    strat, _ = generate_buy_strategy(
        buy, quote, book, vol5min=500_000.0, ctx=DecisionContext(adv=100_000_000),
    )

    events = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    decisions = [e for e in events if e["event_type"] == "strategy_decision"]
    assert len(decisions) == 1
    assert decisions[0]["payload"]["side"] == "buy"
    assert decisions[0]["payload"]["rule"] == strat.rule


def test_generator_silent_when_decision_log_disabled(tmp_path: Path):
    """With the log disabled (the default), the pure generator writes nothing."""
    path = tmp_path / "decisions.jsonl"  # never enabled
    sell = SellRecord(
        account="Test Retirement", strategy="Strategy Gamma", ticker="SPY",
        shares=1000, limit_price=500.0, est_proceeds=500_000,
    )
    quote = _quote("SPY", bid=499.99, ask=500.01, last=500.00,
                   prev_close=499.50, volume=150_000_000)
    book = _l2("SPY", [(499.99, 5000)] * 3, [(500.01, 5000)] * 3)
    generate_sell_strategy(
        sell, quote, book, vol5min=500_000.0,
        today=date(2026, 4, 15), ctx=DecisionContext(adv=100_000_000),
    )
    assert not path.exists()
