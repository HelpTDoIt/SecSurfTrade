"""
Generate tests/fixtures/feb27_with_strategies.json — a minimal RebalanceState
with synthetic sell and buy strategies for TUI presenter tests.

Run from fidelity_rebalancer/:
    python scripts/gen_strategies_fixture.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from state.schema import (
    AccountInput,
    BuyAllocationRecord,
    BuyStrategy,
    ChunkRecord,
    Computed,
    EngineConfig,
    Inputs,
    PositionInput,
    RebalanceState,
    SellRecord,
    SellStrategy,
    SignalInput,
)

FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "feb27_with_strategies.json"


def main() -> None:
    inputs = Inputs(
        accounts=[
            AccountInput(
                name="Roth IRA",
                type="retirement",
                cash_reserve=0.0,
                positions=[
                    PositionInput(symbol="EEM", quantity=200.0, price=62.71, value=12542.0),
                    PositionInput(symbol="SPAXX**", quantity=100.0, price=1.0, value=100.0),
                ],
                cash_spaxx=100.0,
                strategy_allocations={"Prismatic Prudence": 0.20, "SPDR Respectable": 0.30},
            )
        ],
        signals=[
            SignalInput(
                account="Roth IRA",
                strategy="Prismatic Prudence",
                current_ticker="EEM",
                new_ticker="EWY",
            ),
            SignalInput(
                account="Roth IRA",
                strategy="SPDR Respectable",
                current_ticker="SMH",
                new_ticker="SMH",
            ),
        ],
        prev_closes={"EEM": 62.71, "EWY": 75.50, "SMH": 200.0},
        config=EngineConfig(),
    )

    sells = [
        SellRecord(
            account="Roth IRA",
            strategy="Prismatic Prudence",
            ticker="EEM",
            shares=200.0,
            limit_price=62.39,
            limit_price_basis="prev_close",
            est_proceeds=12478.0,
        )
    ]
    buys = [
        BuyAllocationRecord(
            account="Roth IRA",
            strategy="Prismatic Prudence",
            ticker="EWY",
            dollar_target=12000.0,
            limit_price=75.50,
            share_target=158,
            est_cost=11929.0,
            is_rebalance=False,
            target_value=12000.0,
        ),
        BuyAllocationRecord(
            account="Roth IRA",
            strategy="SPDR Respectable",
            ticker="SMH",
            dollar_target=5000.0,
            limit_price=200.10,
            share_target=24,
            est_cost=4802.4,
            is_rebalance=True,
            target_value=5000.0,
        ),
    ]
    sell_chunks = [
        ChunkRecord(
            chunk_id="s_Roth_IRA_EEM_0",
            account="Roth IRA",
            strategy="Prismatic Prudence",
            ticker="EEM",
            idx=0,
            shares=100.0,
            limit_price=62.39,
            cost=6239.0,
        ),
        ChunkRecord(
            chunk_id="s_Roth_IRA_EEM_1",
            account="Roth IRA",
            strategy="Prismatic Prudence",
            ticker="EEM",
            idx=1,
            shares=100.0,
            limit_price=62.39,
            cost=6239.0,
        ),
    ]
    buy_chunks = [
        ChunkRecord(
            chunk_id="b_Roth_IRA_EWY_0",
            account="Roth IRA",
            strategy="Prismatic Prudence",
            ticker="EWY",
            idx=0,
            shares=100.0,
            limit_price=75.50,
            cost=7550.0,
        ),
        ChunkRecord(
            chunk_id="b_Roth_IRA_EWY_1",
            account="Roth IRA",
            strategy="Prismatic Prudence",
            ticker="EWY",
            idx=1,
            shares=58.0,
            limit_price=75.50,
            cost=4379.0,
        ),
        ChunkRecord(
            chunk_id="b_Roth_IRA_SMH_0",
            account="Roth IRA",
            strategy="SPDR Respectable",
            ticker="SMH",
            idx=0,
            shares=24.0,
            limit_price=200.10,
            cost=4802.4,
        ),
    ]
    sell_strategies = [
        SellStrategy(
            account="Roth IRA",
            strategy="Prismatic Prudence",
            ticker="EEM",
            order_type="LIMIT",
            limit_price=62.39,
            urgency="normal",
            rule="tight_spread_small_position",
            reasoning=[
                "Spread is 3.2 bps.",
                "Order is 0.20% of 30-day ADV (200 sh).",
                "Session volume is 1.20× ADV.",
                "LIMIT at midpoint $62.3900.",
            ],
            chunk_ids=["s_Roth_IRA_EEM_0", "s_Roth_IRA_EEM_1"],
        )
    ]
    buy_strategies = [
        BuyStrategy(
            account="Roth IRA",
            strategy="Prismatic Prudence",
            ticker="EWY",
            order_type="LIMIT",
            limit_price=75.50,
            urgency="normal",
            rule="tight_spread_good_volume",
            reasoning=[
                "Spread is 2.6 bps.",
                "Session volume is 1.50× ADV.",
                "Order is 0.16% of 30-day ADV (158 sh).",
                "LIMIT at ask $75.5000 — likely to fill quickly.",
            ],
            chunk_ids=["b_Roth_IRA_EWY_0", "b_Roth_IRA_EWY_1"],
        ),
        BuyStrategy(
            account="Roth IRA",
            strategy="SPDR Respectable",
            ticker="SMH",
            order_type="LIMIT",
            limit_price=200.10,
            urgency="patient",
            rule="wide_spread",
            reasoning=[
                "Spread is 12.0 bps.",
                "Wide spread — split the difference.",
                "LIMIT at midpoint $200.1000.",
                "Order is 0.24% of 30-day ADV (24 sh).",
            ],
            chunk_ids=["b_Roth_IRA_SMH_0"],
        ),
    ]
    computed = Computed(
        cash_ok={"Roth IRA": True},
        one_share_total={"Roth IRA": 338.0},
        sells=sells,
        buy_allocations=buys,
        sell_chunks=sell_chunks,
        buy_chunks=buy_chunks,
        sell_strategies=sell_strategies,
        buy_strategies=buy_strategies,
    )
    state = RebalanceState(
        generated_at=datetime(2026, 2, 27, 14, 0, 0, tzinfo=timezone.utc),
        generator="engine",
        inputs=inputs,
        computed=computed,
    )
    FIXTURE_PATH.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    print(f"Wrote {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
