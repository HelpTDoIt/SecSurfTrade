from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from datetime import datetime

from preflight.sanity import FINDING_HELP, check_sanity, explain
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


# ── Minimal valid, fully-GREEN state ───────────────────────────────────────


def make_green_state() -> RebalanceState:
    """A minimal but fully valid GREEN state.

    Two accounts. One sell (TICKA, 10 sh @ 100) split into 2 chunks of 5,
    one buy (TICKB, 8 sh @ 50) in a single chunk. prev_closes present and
    near the limits. cash_ok all True. Every chunk referenced by a strategy.
    """
    accounts = [
        AccountInput(
            name="ACCT1",
            positions=[
                PositionInput(symbol="TICKA", quantity=10, price=100.0, value=1000.0),
            ],
            strategy_allocations={"STRAT1": 1.0},
        ),
        AccountInput(
            name="ACCT2",
            positions=[
                PositionInput(symbol="TICKB", quantity=0, price=50.0, value=0.0),
            ],
            strategy_allocations={"STRAT2": 1.0},
        ),
    ]
    signals = [
        SignalInput(
            account="ACCT1",
            strategy="STRAT1",
            current_ticker="TICKA",
            new_ticker="TICKB",
        ),
    ]
    inputs = Inputs(
        accounts=accounts,
        signals=signals,
        prev_closes={"TICKA": 100.0, "TICKB": 50.0},
    )

    sells = [
        SellRecord(
            account="ACCT1",
            strategy="STRAT1",
            ticker="TICKA",
            shares=10.0,
            limit_price=100.0,
            est_proceeds=1000.0,
        ),
    ]
    buys = [
        BuyAllocationRecord(
            account="ACCT2",
            strategy="STRAT2",
            ticker="TICKB",
            dollar_target=400.0,
            limit_price=50.0,
            share_target=8,
            est_cost=400.0,
        ),
    ]
    sell_chunks = [
        ChunkRecord(
            chunk_id="S1",
            account="ACCT1",
            strategy="STRAT1",
            ticker="TICKA",
            idx=0,
            shares=5.0,
            limit_price=100.0,
            cost=500.0,
        ),
        ChunkRecord(
            chunk_id="S2",
            account="ACCT1",
            strategy="STRAT1",
            ticker="TICKA",
            idx=1,
            shares=5.0,
            limit_price=100.0,
            cost=500.0,
        ),
    ]
    buy_chunks = [
        ChunkRecord(
            chunk_id="B1",
            account="ACCT2",
            strategy="STRAT2",
            ticker="TICKB",
            idx=0,
            shares=8.0,
            limit_price=50.0,
            cost=400.0,
        ),
    ]
    sell_strategies = [
        SellStrategy(
            account="ACCT1",
            strategy="STRAT1",
            ticker="TICKA",
            limit_price=100.0,
            urgency="normal",
            rule="r",
            reasoning=["x"],
            chunk_ids=["S1", "S2"],
        ),
    ]
    buy_strategies = [
        BuyStrategy(
            account="ACCT2",
            strategy="STRAT2",
            ticker="TICKB",
            limit_price=50.0,
            urgency="normal",
            rule="r",
            reasoning=["x"],
            chunk_ids=["B1"],
        ),
    ]
    computed = Computed(
        cash_ok={"ACCT1": True, "ACCT2": True},
        one_share_total={"ACCT1": 100.0, "ACCT2": 50.0},
        sells=sells,
        buy_allocations=buys,
        sell_chunks=sell_chunks,
        buy_chunks=buy_chunks,
        sell_strategies=sell_strategies,
        buy_strategies=buy_strategies,
    )
    return RebalanceState(
        generated_at=datetime(2026, 5, 31, 12, 0, 0),
        generator="engine",
        inputs=inputs,
        computed=computed,
    )


def codes(report) -> set[str]:
    return {f.code for f in report.findings}


# ── Clean state ─────────────────────────────────────────────────────────────


def test_clean_state_is_green():
    report = check_sanity(make_green_state())
    assert report.verdict == "GREEN"
    assert report.findings == []
    assert report.ok is True


# ── RED rules ───────────────────────────────────────────────────────────────


def test_red_non_positive_shares_chunk():
    state = make_green_state()
    state.computed.sell_chunks[0].shares = 0.0
    # keep sum correct-ish irrelevant: this rule fires regardless
    report = check_sanity(state)
    assert report.verdict == "RED"
    assert report.ok is False
    assert "NON_POSITIVE_SHARES" in codes(report)


def test_red_non_positive_shares_sell_record():
    state = make_green_state()
    state.computed.sells[0].shares = -1.0
    report = check_sanity(state)
    assert report.verdict == "RED"
    assert "NON_POSITIVE_SHARES" in codes(report)


def test_red_non_positive_share_target_buy():
    state = make_green_state()
    state.computed.buy_allocations[0].share_target = 0
    report = check_sanity(state)
    assert report.verdict == "RED"
    assert "NON_POSITIVE_SHARES" in codes(report)


def test_red_chunk_sum_mismatch():
    state = make_green_state()
    state.computed.sell_chunks[0].shares = 4.0  # 4 + 5 = 9 != 10
    report = check_sanity(state)
    assert report.verdict == "RED"
    assert "CHUNK_SUM_MISMATCH" in codes(report)


def test_red_chunk_sum_mismatch_no_chunks_for_nonzero_target():
    state = make_green_state()
    state.computed.buy_chunks = []  # buy target is 8 but no chunks
    report = check_sanity(state)
    assert report.verdict == "RED"
    assert "CHUNK_SUM_MISMATCH" in codes(report)


def test_red_dangling_chunk_id():
    state = make_green_state()
    state.computed.sell_strategies[0].chunk_ids = ["S1", "NOPE"]
    report = check_sanity(state)
    assert report.verdict == "RED"
    assert "DANGLING_CHUNK_ID" in codes(report)


def test_yellow_cash_not_ok():
    # CASH_NOT_OK is a YELLOW warning, not a blocker: during an IRA rebalance the
    # buys are funded by sells that have not yet settled, so unsettled-cash is
    # expected. The human confirms; it must not hard-block.
    state = make_green_state()
    state.computed.cash_ok["ACCT2"] = False
    report = check_sanity(state)
    assert report.verdict == "YELLOW"
    assert report.ok is True
    assert "CASH_NOT_OK" in codes(report)


def test_red_non_positive_limit():
    state = make_green_state()
    state.computed.buy_allocations[0].limit_price = 0.0
    report = check_sanity(state)
    assert report.verdict == "RED"
    assert "NON_POSITIVE_LIMIT" in codes(report)


def test_red_limit_far_from_prevclose():
    state = make_green_state()
    # prev_close 100, set limit to 130 -> 30% > 25% default
    state.computed.sells[0].limit_price = 130.0
    report = check_sanity(state)
    assert report.verdict == "RED"
    assert "LIMIT_FAR_FROM_PREVCLOSE" in codes(report)


# ── YELLOW rules ────────────────────────────────────────────────────────────


def test_yellow_cost_arithmetic_drift():
    state = make_green_state()
    state.computed.sell_chunks[0].cost = 600.0  # should be 500
    report = check_sanity(state)
    assert report.verdict == "YELLOW"
    assert report.ok is True
    assert "COST_ARITHMETIC_DRIFT" in codes(report)


def test_yellow_missing_prevclose():
    state = make_green_state()
    del state.inputs.prev_closes["TICKB"]
    report = check_sanity(state)
    assert report.verdict == "YELLOW"
    assert report.ok is True
    assert "MISSING_PREVCLOSE" in codes(report)


def test_yellow_orphan_chunk():
    state = make_green_state()
    # strategy references only S1, leaving S2 orphaned (but sum still ok)
    state.computed.sell_strategies[0].chunk_ids = ["S1", "S2"]
    # add an extra orphan chunk that nothing references; keep sum intact by
    # making it a separate ticker with its own sell record + matching chunk
    orphan = ChunkRecord(
        chunk_id="ORPH",
        account="ACCT1",
        strategy="STRAT1",
        ticker="TICKC",
        idx=0,
        shares=3.0,
        limit_price=10.0,
        cost=30.0,
    )
    state.computed.sells.append(
        SellRecord(
            account="ACCT1",
            strategy="STRAT1",
            ticker="TICKC",
            shares=3.0,
            limit_price=10.0,
            est_proceeds=30.0,
        )
    )
    state.inputs.prev_closes["TICKC"] = 10.0
    state.computed.sell_chunks.append(orphan)
    report = check_sanity(state)
    assert report.verdict == "YELLOW"
    assert report.ok is True
    assert "ORPHAN_CHUNK" in codes(report)
    assert "CHUNK_SUM_MISMATCH" not in codes(report)


# ── Combined / boundary ──────────────────────────────────────────────────────


def test_red_dominates_yellow():
    state = make_green_state()
    state.computed.sell_chunks[0].cost = 600.0  # YELLOW drift
    state.computed.sells[0].shares = -1.0  # RED: NON_POSITIVE_SHARES
    report = check_sanity(state)
    assert report.verdict == "RED"
    assert report.ok is False
    assert "NON_POSITIVE_SHARES" in codes(report)
    assert "COST_ARITHMETIC_DRIFT" in codes(report)


def test_multi_chunk_sum_matches_no_mismatch():
    state = make_green_state()
    # already 5 + 5 = 10; split differently and confirm still clean
    state.computed.sell_chunks[0].shares = 3.0
    state.computed.sell_chunks[0].cost = 300.0
    state.computed.sell_chunks[1].shares = 7.0
    state.computed.sell_chunks[1].cost = 700.0
    report = check_sanity(state)
    assert "CHUNK_SUM_MISMATCH" not in codes(report)
    assert report.verdict == "GREEN"


def test_limit_deviation_boundary_not_flagged():
    state = make_green_state()
    # exactly 25% over prev_close 100 -> 125; threshold is 25.0, not > so ok
    state.computed.sells[0].limit_price = 125.0
    report = check_sanity(state)
    assert "LIMIT_FAR_FROM_PREVCLOSE" not in codes(report)
    assert report.verdict == "GREEN"


def test_limit_deviation_just_over_is_flagged():
    state = make_green_state()
    state.computed.sells[0].limit_price = 125.01  # just over 25%
    report = check_sanity(state)
    assert "LIMIT_FAR_FROM_PREVCLOSE" in codes(report)
    assert report.verdict == "RED"


# ── Plain-English gloss (explain / FINDING_HELP) ────────────────────────────

# Every finding code that check_sanity OR extra_sanity_warnings can emit must
# carry a "what it means / what to do" gloss so the CLI output stands alone.
_ALL_FINDING_CODES = {
    "NON_POSITIVE_SHARES",
    "CHUNK_SUM_MISMATCH",
    "DANGLING_CHUNK_ID",
    "CASH_NOT_OK",
    "NON_POSITIVE_LIMIT",
    "LIMIT_FAR_FROM_PREVCLOSE",
    "ORPHAN_CHUNK",
    "MISSING_PREVCLOSE",
    "COST_ARITHMETIC_DRIFT",
    "THIN_NO_L2",
    "OVERSIZED_VS_ADV",
}


def test_every_finding_code_has_help_text():
    for code in _ALL_FINDING_CODES:
        assert code in FINDING_HELP, f"{code} missing from FINDING_HELP"
        assert FINDING_HELP[code].strip(), f"{code} has empty help"


def test_explain_returns_gloss_for_known_code():
    assert explain("CHUNK_SUM_MISMATCH") == FINDING_HELP["CHUNK_SUM_MISMATCH"]
    assert "do not enter" in explain("CHUNK_SUM_MISMATCH").lower()


def test_explain_has_fallback_for_unknown_code():
    out = explain("NOT_A_REAL_CODE")
    assert out and isinstance(out, str)
