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
    L2PriorityGuidance,
    ReadinessReport,
    WatchlistGuidance,
    build_sizing_command,
    classify_sizing_outcome,
    evaluate_l2_priorities,
    evaluate_readiness,
    evaluate_watchlist,
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
    ok=True, missing_watchlist=None, missing_l2=None, visible_l2=None
) -> TickerPresenceResult:
    return TickerPresenceResult(
        ok=ok,
        missing_watchlist=missing_watchlist or [],
        missing_l2=missing_l2 or [],
        present_watchlist=[],
        present_l2=[],
        visible_l2=visible_l2 or [],
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


def test_watchlist_and_l2_messages_are_distinct():
    # The two requirements must read as clearly different statements.
    ft = FtRunningResult(running=True, detail="ok")
    presence = make_presence(ok=False, missing_watchlist=["DFEN"], missing_l2=["DFEN"])
    plan = plan_l2_windows(["DFEN"], [("DFEN", 4.0)])
    report = evaluate_readiness(ft, presence, plan)
    wl_lines = [i for i in report.instructions if "Watchlist" in i and "Add DFEN" in i]
    l2_lines = [i for i in report.instructions if "L2 window for DFEN" in i]
    assert len(wl_lines) == 1
    assert len(l2_lines) == 1
    # Distinct wording, and each names which list it is about.
    assert wl_lines[0] != l2_lines[0]
    assert "missing from Watchlist" in wl_lines[0]
    assert "missing from L2" in l2_lines[0]


def test_thin_ticker_missing_from_both_gets_both_statements():
    # A thin ticker absent from BOTH watchlist and L2 must produce two separate
    # actionable lines — one for each requirement.
    ft = FtRunningResult(running=True, detail="ok")
    presence = make_presence(ok=False, missing_watchlist=["EWY"], missing_l2=["EWY"])
    plan = plan_l2_windows(["EWY"], [("EWY", 4.0)])
    report = evaluate_readiness(ft, presence, plan)
    joined = " ".join(report.instructions)
    assert "Add EWY to the FT+ Watchlist" in joined
    assert "Open an L2 window for EWY" in joined


def test_missing_l2_lists_safe_to_close_panels():
    # Need DFEN's depth (assigned); SPY and QQQ L2 panels are open but not needed.
    ft = FtRunningResult(running=True, detail="ok")
    presence = make_presence(ok=False, missing_l2=["DFEN"], visible_l2=["QQQ", "SPY"])
    plan = plan_l2_windows([], [("DFEN", 4.0)])
    report = evaluate_readiness(ft, presence, plan)
    joined = " ".join(report.instructions)
    assert "Open an L2 window for DFEN" in joined
    # Names the open panels that are safe to close, and that none must be kept.
    assert "Safe to close to free a slot: QQQ, SPY" in joined
    assert "Do NOT close (still needed for depth): none" in joined


def test_missing_l2_keeps_needed_panels_out_of_closeable():
    # AAA is assigned AND already open; BBB still missing; ZZZ open but not needed.
    ft = FtRunningResult(running=True, detail="ok")
    presence = make_presence(ok=False, missing_l2=["BBB"], visible_l2=["AAA", "ZZZ"])
    plan = plan_l2_windows([], [("AAA", 5.0), ("BBB", 3.0)])
    report = evaluate_readiness(ft, presence, plan)
    joined = " ".join(report.instructions)
    assert "Safe to close to free a slot: ZZZ" in joined
    # AAA is needed depth -> must be listed as keep, never as closeable.
    assert "Do NOT close (still needed for depth): AAA" in joined
    assert "Safe to close to free a slot: AAA" not in joined


def test_missing_l2_all_slots_needed_suggests_cap():
    # cap 2, both slots open by needed (assigned) tickers, a third thin ticker
    # is missing its window -> no panel is safe to close; suggest raising cap.
    ft = FtRunningResult(running=True, detail="ok")
    presence = make_presence(ok=False, missing_l2=["CCC"], visible_l2=["AAA", "BBB"])
    plan = plan_l2_windows([], [("AAA", 5.0), ("BBB", 4.0), ("CCC", 3.0)], cap=2)
    assert plan.l2_assigned == ["AAA", "BBB"]
    report = evaluate_readiness(ft, presence, plan)
    joined = " ".join(report.instructions)
    assert "All 2 L2 window slot(s) are in use" in joined
    assert "Raise --cap" in joined


def test_no_closeable_guidance_when_no_l2_missing():
    # Nothing missing in L2 -> no safe-to-close noise even if extra panels open.
    ft = FtRunningResult(running=True, detail="ok")
    presence = make_presence(ok=True, visible_l2=["SPY", "QQQ"])
    plan = plan_l2_windows(["SPY"], [])
    report = evaluate_readiness(ft, presence, plan)
    joined = " ".join(report.instructions)
    assert "Safe to close" not in joined


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


# ── evaluate_watchlist ────────────────────────────────────────────────────────


def test_watchlist_complete_ok_no_add():
    g = evaluate_watchlist(["SPY", "QQQ"], ["SPY", "QQQ", "IWM"])
    assert isinstance(g, WatchlistGuidance)
    assert g.ok is True
    assert g.add == []
    # IWM is held but not needed -> advisory remove.
    assert g.remove == ["IWM"]


def test_watchlist_missing_blocks_with_add_list():
    g = evaluate_watchlist(["SPY", "QQQ", "DFEN"], ["SPY"])
    assert g.ok is False
    assert g.add == ["DFEN", "QQQ"]  # sorted
    assert g.remove == []


def test_watchlist_case_insensitive():
    g = evaluate_watchlist(["spy", "qqq"], ["SPY", "Qqq"])
    assert g.ok is True
    assert g.add == []
    assert g.remove == []


def test_watchlist_reports_both_add_and_remove():
    g = evaluate_watchlist(["SPY", "DFEN"], ["SPY", "JUNK"])
    assert g.ok is False
    assert g.add == ["DFEN"]
    assert g.remove == ["JUNK"]


# ── evaluate_l2_priorities ────────────────────────────────────────────────────


def test_l2_priorities_all_open_is_ok():
    # Top-2 priority both have open panels -> nothing to open.
    g = evaluate_l2_priorities(["EIS", "DFEN"], {"EIS", "DFEN"}, cap=7)
    assert isinstance(g, L2PriorityGuidance)
    assert g.ok is True
    assert g.use == ["EIS", "DFEN"]
    assert g.to_open == []
    assert g.to_close == []


def test_l2_priorities_flags_open_and_close():
    # Priority order: EIS, DFEN, IYZ. EIS panel open; DFEN/IYZ closed.
    # JUNK panel is open but not in the priority set -> safe to close.
    g = evaluate_l2_priorities(["EIS", "DFEN", "IYZ"], {"EIS", "JUNK"}, cap=7)
    assert g.ok is False
    assert g.use == ["EIS"]
    assert g.to_open == ["DFEN", "IYZ"]
    assert g.to_close == ["JUNK"]


def test_l2_priorities_respects_cap():
    # cap 2 -> only EIS, DFEN are in the priority window; IYZ is below the cap.
    # IYZ's open panel is therefore "safe to close" (not in top-2).
    g = evaluate_l2_priorities(["EIS", "DFEN", "IYZ"], {"EIS", "IYZ"}, cap=2)
    assert g.use == ["EIS"]
    assert g.to_open == ["DFEN"]
    assert g.to_close == ["IYZ"]
    assert g.ok is False


def test_l2_priorities_no_open_panels():
    g = evaluate_l2_priorities(["EIS", "DFEN"], set(), cap=7)
    assert g.use == []
    assert g.to_open == ["EIS", "DFEN"]
    assert g.to_close == []
    assert g.ok is False


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
