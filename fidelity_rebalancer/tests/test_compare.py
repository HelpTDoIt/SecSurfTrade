"""
Tests for state.compare — round-trip identity and seeded-diff detection.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from state.compare import FLOAT_TOL, compare_states
from state.importer import load_state
from state.schema import (
    AccountInput,
    BuyAllocationRecord,
    ChunkRecord,
    Computed,
    EngineConfig,
    Inputs,
    PositionInput,
    RebalanceState,
    SellRecord,
    SignalInput,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ── helpers ────────────────────────────────────────────────────────────────

def _make_minimal_state(generator: str = "engine") -> RebalanceState:
    """Build a small but complete RebalanceState for parametric tests."""
    return RebalanceState(
        generated_at=datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc),
        generator=generator,
        inputs=Inputs(
            accounts=[
                AccountInput(
                    name="Roth IRA",
                    type="retirement",
                    cash_reserve=0.0,
                    positions=[
                        PositionInput(symbol="EEM", quantity=200.0, price=62.71, value=12542.0),
                    ],
                    cash_spaxx=100.0,
                    strategy_allocations={"Prismatic Prudence": 1.0},
                )
            ],
            signals=[
                SignalInput(
                    account="Roth IRA",
                    strategy="Prismatic Prudence",
                    current_ticker="EEM",
                    new_ticker="EWY",
                )
            ],
            prev_closes={"EEM": 62.71, "EWY": 55.0},
            config=EngineConfig(),
        ),
        computed=Computed(
            cash_ok={"Roth IRA": False},
            one_share_total={"Roth IRA": 55.0},
            sells=[
                SellRecord(
                    account="Roth IRA",
                    strategy="Prismatic Prudence",
                    ticker="EEM",
                    shares=200.0,
                    limit_price=62.71,
                    est_proceeds=12542.0,
                )
            ],
            buy_allocations=[
                BuyAllocationRecord(
                    account="Roth IRA",
                    strategy="Prismatic Prudence",
                    ticker="EWY",
                    dollar_target=12642.0,
                    limit_price=55.0,
                    share_target=229,
                    est_cost=12595.0,
                )
            ],
            sell_chunks=[
                ChunkRecord(
                    chunk_id="s1",
                    account="Roth IRA",
                    strategy="Prismatic Prudence",
                    ticker="EEM",
                    idx=0,
                    shares=200.0,
                    limit_price=62.71,
                    cost=12542.0,
                )
            ],
            buy_chunks=[
                ChunkRecord(
                    chunk_id="b1",
                    account="Roth IRA",
                    strategy="Prismatic Prudence",
                    ticker="EWY",
                    idx=0,
                    shares=229,
                    limit_price=55.0,
                    cost=12595.0,
                )
            ],
        ),
    )


# ── round-trip identity ────────────────────────────────────────────────────

def test_round_trip_identity_no_diffs(tmp_path):
    """Export engine state, reload it, compare with itself → zero diffs."""
    state = _make_minimal_state()
    out = tmp_path / "state.json"
    out.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    reloaded = load_state(out)
    diffs = compare_states(state, reloaded)
    assert diffs == [], f"Expected no diffs, got: {diffs}"


def test_round_trip_with_calc_export_fixture():
    """Engine state computed from feb27 CSVs matches calc_export_feb27 fixture."""
    calc = load_state(FIXTURES / "calc_export_feb27.json")

    # Build engine state programmatically from the same inputs
    from cli.compute import _build_state

    signals = {
        "World Try -Top":     {"current": "EIS",  "new": "EIS"},
        "SPDR Respectable":   {"current": "SMH",  "new": "SMH"},
        "Prismatic Prudence": {"current": "EEM",  "new": "EWY"},
        "Future Theme + CAPE":{"current": "AOR",  "new": "AOR"},
        "Leverage":           {"current": "PILL", "new": "PILL"},
    }
    closes = {"EIS": 28.50, "SMH": 200.0, "EEM": 62.71, "AOR": 45.0, "EWY": 55.0, "PILL": 30.0}

    from adapters.csv_reader import read_fidelity_csv
    accounts_raw = {}
    for csv_path in sorted((FIXTURES).glob("*.csv")):
        p = read_fidelity_csv(csv_path)
        from cli.compute import ACCOUNTS_CONFIG
        if p.account_name in ACCOUNTS_CONFIG:
            accounts_raw[p.account_name] = {
                "positions": {sym: pos.model_dump() for sym, pos in p.positions.items()}
            }

    engine = _build_state(accounts_raw, signals, closes)
    diffs = compare_states(engine, calc)
    assert diffs == [], f"Feb27 parity failures:\n" + "\n".join(
        f"  {d.path}: engine={d.engine_val} calc={d.calc_val}" for d in diffs
    )


# ── schema round-trip validation ───────────────────────────────────────────

def test_architecture_example_validates():
    """The JSON snippet from ARCHITECTURE.md must pass Pydantic validation."""
    calc = load_state(FIXTURES / "calc_export_feb27.json")
    assert calc.schema_version == "1.0"
    assert calc.generator == "react_calc"
    assert len(calc.computed.sells) == 2
    assert len(calc.computed.buy_allocations) == 2


# ── seeded diff detection ──────────────────────────────────────────────────

def test_diff_detected_wrong_shares():
    engine = _make_minimal_state()
    calc = _make_minimal_state(generator="react_calc")
    calc.computed.sells[0].shares = 199.0  # seed diff

    diffs = compare_states(engine, calc)
    paths = [d.path for d in diffs]
    assert any("shares" in p for p in paths), f"Expected shares diff, got: {paths}"


def test_diff_detected_wrong_dollar_target():
    engine = _make_minimal_state()
    calc = _make_minimal_state(generator="react_calc")
    calc.computed.buy_allocations[0].dollar_target = 12000.0

    diffs = compare_states(engine, calc)
    paths = [d.path for d in diffs]
    assert any("dollar_target" in p for p in paths)


def test_diff_detected_wrong_share_target():
    engine = _make_minimal_state()
    calc = _make_minimal_state(generator="react_calc")
    calc.computed.buy_allocations[0].share_target = 228  # off by 1

    diffs = compare_states(engine, calc)
    paths = [d.path for d in diffs]
    assert any("share_target" in p for p in paths)


def test_diff_detected_wrong_chunk_shares():
    engine = _make_minimal_state()
    calc = _make_minimal_state(generator="react_calc")
    calc.computed.sell_chunks[0].shares = 100.0

    diffs = compare_states(engine, calc)
    paths = [d.path for d in diffs]
    assert any("sell_chunks" in p and "shares" in p for p in paths)


def test_diff_detected_wrong_buy_chunk_cost():
    engine = _make_minimal_state()
    calc = _make_minimal_state(generator="react_calc")
    calc.computed.buy_chunks[0].cost = 999.0

    diffs = compare_states(engine, calc)
    paths = [d.path for d in diffs]
    assert any("buy_chunks" in p and "cost" in p for p in paths)


def test_diff_detected_wrong_cash_ok():
    engine = _make_minimal_state()
    calc = _make_minimal_state(generator="react_calc")
    calc.computed.cash_ok["Roth IRA"] = True

    diffs = compare_states(engine, calc)
    paths = [d.path for d in diffs]
    assert any("cash_ok" in p for p in paths)


def test_float_tolerance_not_flagged():
    """Differences smaller than FLOAT_TOL should not produce a diff."""
    engine = _make_minimal_state()
    calc = _make_minimal_state(generator="react_calc")
    calc.computed.sells[0].est_proceeds = 12542.0 + FLOAT_TOL * 0.5

    diffs = compare_states(engine, calc)
    proceeds_diffs = [d for d in diffs if "est_proceeds" in d.path]
    assert proceeds_diffs == []


def test_missing_account_flagged():
    """A sell present in engine but absent in calc should be flagged."""
    engine = _make_minimal_state()
    calc = _make_minimal_state(generator="react_calc")
    calc.computed.sells = []  # remove all sells from calc

    diffs = compare_states(engine, calc)
    assert any("present" in str(d.engine_val) for d in diffs)
