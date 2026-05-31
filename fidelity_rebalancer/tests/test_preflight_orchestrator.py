from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from datetime import datetime

from adapters import WatchlistRow
from cli.strategy import OCR_SHORTFALL_EXIT, OCR_SHORTFALL_MARKER
from preflight.checks import FtRunningResult, TickerPresenceResult
from preflight.orchestrator import (
    ReadinessReport,
    build_sizing_command,
    classify_sizing_outcome,
    evaluate_readiness,
    extra_sanity_warnings,
)
from preflight.planner import L2WindowPlan, plan_l2_windows
from state.schema import (
    AccountInput,
    BuyAllocationRecord,
    BuyStrategy,
    ChunkRecord,
    Computed,
    Inputs,
    PositionInput,
    RebalanceState,
    SellRecord,
    SellStrategy,
    SignalInput,
)


# ── Builders ────────────────────────────────────────────────────────────────


def make_presence(
    ok=True, missing_watchlist=None, missing_l2=None
) -> TickerPresenceResult:
    return TickerPresenceResult(
        ok=ok,
        missing_watchlist=missing_watchlist or [],
        missing_l2=missing_l2 or [],
        present_watchlist=[],
        present_l2=[],
    )


def make_wl_row(symbol: str, avg_vol_10d: int) -> WatchlistRow:
    return WatchlistRow(
        symbol=symbol,
        last=10.0,
        bid=9.99,
        ask=10.01,
        bid_size=1,
        ask_size=1,
        volume=1000,
        prev_close=10.0,
        avg_vol_10d=avg_vol_10d,
        avg_vol_90d=avg_vol_10d,
        div_ex_date="",
        div_local=0.0,
        vwap=10.0,
        ts=datetime(2026, 5, 31, 9, 0, 0),
    )


def make_state_with_chunks(sell_chunks, buy_chunks) -> RebalanceState:
    inputs = Inputs(
        accounts=[
            AccountInput(
                name="ACCT1",
                positions=[
                    PositionInput(
                        symbol="TICKA", quantity=10, price=100.0, value=1000.0
                    )
                ],
                strategy_allocations={"STRAT1": 1.0},
            )
        ],
        signals=[
            SignalInput(
                account="ACCT1",
                strategy="STRAT1",
                current_ticker="TICKA",
                new_ticker="TICKB",
            )
        ],
        prev_closes={},
    )
    computed = Computed(
        cash_ok={"ACCT1": True},
        one_share_total={"ACCT1": 100.0},
        sells=[],
        buy_allocations=[],
        sell_chunks=sell_chunks,
        buy_chunks=buy_chunks,
        sell_strategies=[],
        buy_strategies=[],
    )
    return RebalanceState(
        generated_at=datetime(2026, 5, 31, 12, 0, 0),
        generator="engine",
        inputs=inputs,
        computed=computed,
    )


def chunk(chunk_id, ticker, shares, limit_price=10.0) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        account="ACCT1",
        strategy="STRAT1",
        ticker=ticker,
        idx=0,
        shares=shares,
        limit_price=limit_price,
        cost=shares * limit_price,
    )


# ── evaluate_readiness ────────────────────────────────────────────────────────


def test_all_clear_ready_no_instructions():
    ft = FtRunningResult(running=True, detail="ok")
    presence = make_presence(ok=True)
    plan = plan_l2_windows(["SPY"], [])
    report = evaluate_readiness(ft, presence, plan)
    assert isinstance(report, ReadinessReport)
    assert report.ready is True
    assert report.instructions == []
    assert report.overflow_warnings == []


def test_ft_down_not_ready_instruction_mentions_starting_ft():
    ft = FtRunningResult(running=False, detail="Could not connect to FT+.")
    presence = make_presence(ok=True)
    plan = plan_l2_windows(["SPY"], [])
    report = evaluate_readiness(ft, presence, plan)
    assert report.ready is False
    joined = " ".join(report.instructions)
    assert "Fidelity Trader+" in joined
    assert "Could not connect to FT+." in joined


def test_missing_watchlist_tickers_not_ready_with_lines():
    ft = FtRunningResult(running=True, detail="ok")
    presence = make_presence(ok=False, missing_watchlist=["SPY", "QQQ"])
    plan = plan_l2_windows(["SPY", "QQQ"], [])
    report = evaluate_readiness(ft, presence, plan)
    assert report.ready is False
    joined = " ".join(report.instructions)
    assert "Add SPY to the FT+ Watchlist" in joined
    assert "Add QQQ to the FT+ Watchlist" in joined


def test_missing_l2_tickers_not_ready_with_lines():
    ft = FtRunningResult(running=True, detail="ok")
    presence = make_presence(ok=False, missing_l2=["DFEN"])
    plan = plan_l2_windows([], [("DFEN", 4.0)])
    report = evaluate_readiness(ft, presence, plan)
    assert report.ready is False
    joined = " ".join(report.instructions)
    assert "Open an L2 window for DFEN" in joined


def test_overflow_present_but_nothing_missing_is_ready():
    ft = FtRunningResult(running=True, detail="ok")
    presence = make_presence(ok=True)
    # cap 1, two thin tickers -> one overflow
    plan = plan_l2_windows([], [("AAA", 5.0), ("BBB", 1.0)], cap=1)
    assert plan.l2_overflow == ["BBB"]
    report = evaluate_readiness(ft, presence, plan)
    assert report.ready is True
    assert report.overflow_warnings != []
    joined = " ".join(report.overflow_warnings)
    assert "BBB" in joined
    assert str(plan.cap) in joined


# ── build_sizing_command ──────────────────────────────────────────────────────


def test_build_command_defaults_strict_l2auto():
    argv = build_sizing_command("state.json", python_exe="py")
    assert argv == [
        "py",
        "-m",
        "cli.strategy",
        "--state",
        "state.json",
        "--export",
        "state.json",
        "--source",
        "atp",
        "--strict-atp",
        "--l2-symbols",
    ]


def test_build_command_no_strict_omits_flag():
    argv = build_sizing_command("s.json", python_exe="py", strict=False)
    assert "--strict-atp" not in argv


def test_build_command_no_l2auto_omits_flag():
    argv = build_sizing_command("s.json", python_exe="py", l2_auto=False)
    assert "--l2-symbols" not in argv


def test_build_command_l2_symbols_has_no_symbol_token():
    argv = build_sizing_command("s.json", python_exe="py")
    i = argv.index("--l2-symbols")
    # Either last token, or the next token is itself another flag (none here).
    assert i == len(argv) - 1


def test_build_command_confirmed_proceeds_appends_two_tokens():
    argv = build_sizing_command(
        "s.json", python_exe="py", confirmed_proceeds_path="cp.json"
    )
    i = argv.index("--confirmed-proceeds")
    assert argv[i + 1] == "cp.json"


# ── classify_sizing_outcome ───────────────────────────────────────────────────


def test_classify_zero_is_ok():
    assert classify_sizing_outcome(0, "") == "ok"


def test_classify_exit_code_is_ocr_shortfall():
    assert classify_sizing_outcome(OCR_SHORTFALL_EXIT, "") == "ocr_shortfall"


def test_classify_marker_in_stderr_other_code_is_ocr_shortfall():
    stderr = f"something {OCR_SHORTFALL_MARKER}: l2_failed=2"
    assert classify_sizing_outcome(1, stderr) == "ocr_shortfall"


def test_classify_other_nonzero_is_error():
    assert classify_sizing_outcome(1, "boom") == "error"


# ── extra_sanity_warnings ─────────────────────────────────────────────────────


def test_thin_no_l2_for_overflow_tickers():
    state = make_state_with_chunks([], [])
    plan = plan_l2_windows([], [("AAA", 5.0), ("BBB", 1.0)], cap=1)
    assert plan.l2_overflow == ["BBB"]
    findings = extra_sanity_warnings(state, {}, plan, used_yfinance_fallback=False)
    codes = [f.code for f in findings]
    assert codes.count("THIN_NO_L2") == 1
    assert all(f.severity == "YELLOW" for f in findings)
    assert any("BBB" in f.message for f in findings)


def test_thin_no_l2_yfinance_fallback_includes_assigned_deduped():
    state = make_state_with_chunks([], [])
    # AAA assigned, BBB overflow
    plan = plan_l2_windows([], [("AAA", 5.0), ("BBB", 1.0)], cap=1)
    assert plan.l2_assigned == ["AAA"]
    assert plan.l2_overflow == ["BBB"]
    findings = extra_sanity_warnings(state, {}, plan, used_yfinance_fallback=True)
    thin = [f for f in findings if f.code == "THIN_NO_L2"]
    tickers = {f.ref for f in thin}
    assert tickers == {"AAA", "BBB"}
    # de-dupe: one per ticker
    assert len(thin) == 2


def test_oversized_vs_adv_fires_over_threshold():
    state = make_state_with_chunks([chunk("S1", "AAA", shares=200.0)], [])
    rows = {"AAA": make_wl_row("AAA", avg_vol_10d=1000)}  # 200/1000 = 20% > 10
    findings = extra_sanity_warnings(
        state, rows, plan_l2_windows([], []), used_yfinance_fallback=False
    )
    over = [f for f in findings if f.code == "OVERSIZED_VS_ADV"]
    assert len(over) == 1
    assert over[0].severity == "YELLOW"
    assert "S1" in over[0].message
    assert "AAA" in over[0].message


def test_oversized_vs_adv_not_fired_under_threshold():
    state = make_state_with_chunks([chunk("S1", "AAA", shares=50.0)], [])
    rows = {"AAA": make_wl_row("AAA", avg_vol_10d=1000)}  # 50/1000 = 5% < 10
    findings = extra_sanity_warnings(
        state, rows, plan_l2_windows([], []), used_yfinance_fallback=False
    )
    assert [f for f in findings if f.code == "OVERSIZED_VS_ADV"] == []


def test_oversized_vs_adv_skips_missing_watchlist_row():
    state = make_state_with_chunks([chunk("S1", "ZZZ", shares=999.0)], [])
    findings = extra_sanity_warnings(
        state, {}, plan_l2_windows([], []), used_yfinance_fallback=False
    )
    assert [f for f in findings if f.code == "OVERSIZED_VS_ADV"] == []


def test_oversized_vs_adv_skips_nonpositive_adv():
    state = make_state_with_chunks([chunk("S1", "AAA", shares=999.0)], [])
    rows = {"AAA": make_wl_row("AAA", avg_vol_10d=0)}
    findings = extra_sanity_warnings(
        state, rows, plan_l2_windows([], []), used_yfinance_fallback=False
    )
    assert [f for f in findings if f.code == "OVERSIZED_VS_ADV"] == []


def test_oversized_vs_adv_covers_buy_chunks_too():
    state = make_state_with_chunks([], [chunk("B1", "AAA", shares=300.0)])
    rows = {"AAA": make_wl_row("AAA", avg_vol_10d=1000)}
    findings = extra_sanity_warnings(
        state, rows, plan_l2_windows([], []), used_yfinance_fallback=False
    )
    over = [f for f in findings if f.code == "OVERSIZED_VS_ADV"]
    assert len(over) == 1
    assert "B1" in over[0].message
