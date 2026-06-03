"""
DecisionContext — per-decision market inputs bundled into a single frozen object.

Passed as `ctx=` to `generate_sell_strategy` and `generate_buy_strategy` so that
call sites no longer need to thread four separate keyword arguments.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from engine.spread_context import SpreadContext


@dataclass(frozen=True)
class DecisionContext:
    """Immutable bundle of per-decision market inputs.

    Fields
    ------
    market_minutes : int | None
        Minutes elapsed since market open (9:30 ET).  None = unknown.
    spread_ctx : SpreadContext | None
        Symbol-calibrated spread thresholds.  None = SpreadContext.default().
    vwap : float | None
        Intraday VWAP from the live feed (ATP only).  None = unavailable.
    adv : float | None
        30-day average daily volume override.  None = let the generator call
        ``get_adv()`` as a fallback.
    sigma_bps : float | None
        Carried forward-looking field for Phase 3 (not used by current rules).
        Defaults to None; present so call sites can populate it without a
        further signature change.
    """

    market_minutes: Optional[int] = None
    spread_ctx: Optional[SpreadContext] = None
    vwap: Optional[float] = None
    adv: Optional[float] = None
    sigma_bps: Optional[float] = None
