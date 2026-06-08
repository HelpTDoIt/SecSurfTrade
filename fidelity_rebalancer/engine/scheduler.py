from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from state.schema import ChunkRecord, EngineConfig
from engine.decision_context import DecisionContext
from engine.chunker import build_chunks_pov, build_sell_chunks, build_buy_chunks, _DAILY_SIGMA_BPS

class Tranche(BaseModel):
    phase: Literal["premarket", "main", "sweep"]
    shares: float
    limit_price: float
    earliest_entry: str | None = None

def should_sweep(now: datetime, unfilled_frac: float, config: EngineConfig) -> bool:
    mkt_minutes = (now.hour - 9) * 60 + (now.minute - 30)
    if mkt_minutes >= config.sweep_time_minutes:
        return True
    if unfilled_frac >= config.sweep_unfilled_frac:
        return True
    return False

def build_day_schedule(
    record,  # SellRecord or BuyAllocationRecord
    side: Literal["sell", "buy"],
    ctx: DecisionContext,
    now: datetime,
    config: EngineConfig,
    base_limit_price: float,
    quote,
    account_type: Literal["taxable", "retirement", "margin"] | None = None,
    sigma_bps: float = _DAILY_SIGMA_BPS,
) -> list[ChunkRecord]:
    total_shares = getattr(record, "shares", getattr(record, "share_target", 0.0))
    if total_shares <= 0:
        return []

    total_int = int(math.floor(total_shares))
    mkt_minutes = (now.hour - 9) * 60 + (now.minute - 30)
    
    premarket_shares = 0
    premarket_limit = base_limit_price
    
    # 1. Capture-stupid Phase (Premarket)
    if mkt_minutes < 30 and getattr(quote, "prev_close", 0) > 0:
        if side == "sell":
            premarket_limit = quote.prev_close * (1 + config.capture_offset_pct)
        else:
            premarket_limit = quote.prev_close * (1 - config.capture_offset_pct)
            
        premarket_limit = round(premarket_limit, 2)
        # Size = min(premarket_pct, volume-relative cap)
        # To simplify, we just allocate up to premarket_pct to the premarket tranche.
        # The chunker will slice it if it exceeds volume caps.
        premarket_shares = int(math.floor(total_int * config.premarket_pct))
        
        # Don't capture-stupid for retirement buys (as they are clock-gated anyway)
        if side == "buy" and account_type == "retirement":
            premarket_shares = 0

    # 2. Sweep Phase
    sweep_shares = int(math.floor(total_int * 0.20))
    
    # 3. Main Phase
    main_shares = total_int - premarket_shares - sweep_shares
    if main_shares < 0:
        main_shares = 0
        sweep_shares = total_int - premarket_shares

    tranches = []
    if premarket_shares > 0:
        tranches.append(Tranche(
            phase="premarket",
            shares=premarket_shares,
            limit_price=premarket_limit
        ))
    if main_shares > 0:
        earliest = None
        if side == "buy" and account_type == "retirement":
            earliest = "12:00:00"
        tranches.append(Tranche(
            phase="main",
            shares=main_shares,
            limit_price=base_limit_price,
            earliest_entry=earliest
        ))
    if sweep_shares > 0:
        sweep_time_h = 9 + (config.sweep_time_minutes + 30) // 60
        sweep_time_m = (config.sweep_time_minutes + 30) % 60
        tranches.append(Tranche(
            phase="sweep",
            shares=sweep_shares,
            limit_price=base_limit_price,
            earliest_entry=f"{sweep_time_h:02d}:{sweep_time_m:02d}:00"
        ))

    chunks: list[ChunkRecord] = []
    chunk_idx = 0
    
    for t in tranches:
        # Size within each tranche using build_chunks_pov
        spread_bps = 0.0
        if quote.bid > 0 and quote.ask > 0:
            mid = (quote.bid + quote.ask) / 2.0
            spread_bps = (quote.ask - quote.bid) / mid * 10000.0

        chunk_dicts, _ = build_chunks_pov(
            total_shares=t.shares,
            limit_price=t.limit_price,
            adv=ctx.adv,
            spread_bps=spread_bps,
            side=side,
            sigma_bps=sigma_bps
        )
        
        # If build_chunks_pov returns nothing (e.g. limits), ensure at least one chunk
        if not chunk_dicts:
            chunk_dicts = [{"idx": 0, "shares": t.shares, "limit_price": t.limit_price, "cost": t.shares * t.limit_price}]

        for cd in chunk_dicts:
            cid = f"{side[0]}_{record.account.replace(' ', '_')}_{record.ticker}_{chunk_idx}"
            chunks.append(
                ChunkRecord(
                    chunk_id=cid,
                    account=record.account,
                    strategy=record.strategy,
                    ticker=record.ticker,
                    idx=chunk_idx,
                    shares=cd["shares"],
                    limit_price=cd["limit_price"],
                    cost=cd["cost"],
                    phase=t.phase,
                    earliest_entry=t.earliest_entry,
                    account_type=account_type
                )
            )
            chunk_idx += 1

    return chunks
