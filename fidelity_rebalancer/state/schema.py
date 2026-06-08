"""
Pydantic v2 models for the canonical rebalance state JSON (chunk 2).
The minimal backward-compat models from chunk 1 are retained at the bottom
for adapters/csv_reader.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ── Canonical state JSON schema ────────────────────────────────────────────


class PositionInput(BaseModel):
    symbol: str
    quantity: float
    price: float
    value: float
    lot_type: str = "Cash"


class AccountInput(BaseModel):
    name: str
    type: Literal["retirement", "taxable"] = "retirement"
    margin: bool = False
    margin_buying_power: Optional[float] = None
    cash_reserve: float = 0.0
    positions: list[PositionInput]
    cash_spaxx: float = 0.0
    # Signed pending activity from the Fidelity CSV (unsettled buys/sells/
    # transfers). Negative = committed funds, positive = unsettled incoming.
    # Used as cash = cash_spaxx + pending_activity - cash_reserve in the gate.
    pending_activity: float = 0.0
    strategy_allocations: dict[str, float]


class SignalInput(BaseModel):
    account: str
    strategy: str
    current_ticker: str
    new_ticker: str


class ChunkerConfig(BaseModel):
    max_pct_of_top3_depth: float = 0.25
    max_pct_of_5min_volume: float = 0.15


class EngineConfig(BaseModel):
    ex_div_check: bool = True
    polling_seconds: int = 45
    stall_threshold_seconds: int = 300
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    # EOD-sweep predicate inputs, shared by the live-recompute trigger (F-1)
    # and the absorption scheduler (S-1).  sweep_time_minutes is minutes since
    # market open (9:30 ET): 330 = 15:00 ET.  sweep_unfilled_frac is the
    # unfilled-fraction threshold for the fill-based half of the predicate.
    sweep_time_minutes: int = 330
    sweep_unfilled_frac: float = 0.5
    premarket_pct: float = 0.4
    capture_offset_pct: float = 0.01


class Inputs(BaseModel):
    accounts: list[AccountInput]
    signals: list[SignalInput]
    prev_closes: dict[str, float] = Field(default_factory=dict)
    config: EngineConfig = Field(default_factory=EngineConfig)


class SellRecord(BaseModel):
    account: str
    strategy: str
    ticker: str
    shares: float
    limit_price: float
    limit_price_basis: str = "prev_close"
    est_proceeds: float


class BuyAllocationRecord(BaseModel):
    account: str
    strategy: str
    ticker: str
    dollar_target: float
    limit_price: float
    share_target: int
    est_cost: float
    is_rebalance: bool = False
    target_value: float = 0.0


class ChunkRecord(BaseModel):
    chunk_id: str
    account: str
    strategy: str
    ticker: str
    idx: int
    shares: float
    limit_price: float
    cost: float
    phase: Literal["premarket", "main", "sweep"] = "main"
    earliest_entry: Optional[str] = None
    funded_by: Optional[list[str]] = None
    account_type: Optional[Literal["taxable", "retirement", "margin"]] = None
    original_limit_price: Optional[float] = None


Urgency = Literal["normal", "aggressive", "patient"]
ApprovalStatus = Literal["approved", "modified", "skipped"]


class SellStrategy(BaseModel):
    account: str
    strategy: str
    ticker: str
    order_type: Literal["LIMIT"] = "LIMIT"
    limit_price: float
    urgency: Urgency
    rule: str
    reasoning: list[str]
    chunk_ids: list[str] = Field(default_factory=list)
    original_limit_price: Optional[float] = None


class BuyStrategy(BaseModel):
    account: str
    strategy: str
    ticker: str
    order_type: Literal["LIMIT"] = "LIMIT"
    limit_price: float
    urgency: Urgency
    rule: str
    reasoning: list[str]
    chunk_ids: list[str] = Field(default_factory=list)
    original_limit_price: Optional[float] = None


class DriftSnapshot(BaseModel):
    before: dict[str, float] = Field(default_factory=dict)
    after_target: dict[str, float] = Field(default_factory=dict)


class Computed(BaseModel):
    cash_ok: dict[str, bool]
    one_share_total: dict[str, float]
    sells: list[SellRecord]
    buy_allocations: list[BuyAllocationRecord]
    sell_chunks: list[ChunkRecord]
    buy_chunks: list[ChunkRecord]
    sell_strategies: list[SellStrategy] = Field(default_factory=list)
    buy_strategies: list[BuyStrategy] = Field(default_factory=list)
    drift: dict[str, DriftSnapshot] = Field(default_factory=dict)


class FillRecord(BaseModel):
    chunk_id: str
    filled_shares: float
    remaining: float
    avg_price: float
    status: Literal["Open", "PartiallyFilled", "Filled", "Cancelled"]
    last_progress_at: datetime


class ExecutionState(BaseModel):
    fills: list[FillRecord] = Field(default_factory=list)
    actual_proceeds_by_account: dict[str, float] = Field(default_factory=dict)


class RebalanceState(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    generated_at: datetime
    generator: Literal["engine", "react_calc"]
    inputs: Inputs
    computed: Computed
    execution_state: Optional[ExecutionState] = None


class StrategyDecision(BaseModel):
    side: Literal["sell", "buy"]
    idx: int
    approval_status: ApprovalStatus
    approved_limit_price: float
    approved_order_type: Literal["LIMIT", "MARKET"] = "LIMIT"
    original_limit_price: Optional[float] = None
    skip_reason: Optional[str] = None


class PlanOutput(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    generated_at: datetime
    state: RebalanceState
    decisions: list[StrategyDecision] = Field(default_factory=list)


# ── Backward-compat models (used by adapters/csv_reader.py) ───────────────


class Position(BaseModel):
    symbol: str
    quantity: float
    value: float
    price: float


class AccountPortfolio(BaseModel):
    account_name: str
    positions: dict[str, Position]
    pending_activity: float = 0.0


class Signal(BaseModel):
    current: str
    new: str


class Sell(BaseModel):
    strategy: str
    ticker: str
    quantity: float
    limit_price: float
    est_proceeds: float


class BuyAllocation(BaseModel):
    strategy: str
    ticker: str
    dollar_target: float
    limit_price: float
    is_rebalance: bool
    target_value: float
    shares: int
    est_cost: float


class OrderChunk(BaseModel):
    idx: int
    shares: float
    limit_price: float
    cost: float
    phase: Literal["premarket", "main", "sweep"] = "main"
    earliest_entry: Optional[str] = None
    funded_by: Optional[list[str]] = None
    account_type: Optional[Literal["taxable", "retirement", "margin"]] = None
    original_limit_price: Optional[float] = None
