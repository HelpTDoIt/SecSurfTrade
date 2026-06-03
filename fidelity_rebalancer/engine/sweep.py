"""
EOD-sweep predicate shared by the live-recompute trigger (F-1) and the
absorption scheduler (S-1).

``should_sweep`` answers one question: given the wall-clock position in the
session and the fraction of an order book still unfilled, has the moment
arrived to stop waiting and act?  It fires when EITHER

  • the session clock has reached the sweep cutoff (default 15:00 ET), OR
  • the unfilled fraction has crossed the configured threshold.

Pure function — no I/O, no clock access (the caller supplies ``now_minutes``).
The two consumers wire it differently:

  • F-1 (live recompute) passes ``unfilled_frac=0.0`` so only the clock half can
    fire — recompute is gated on sells going *terminal* OR the clock, never on
    how much is still open.
  • S-1 (absorption) passes a live unfilled fraction so a stalled book can be
    swept before the clock if too little has filled.
"""
from __future__ import annotations

import logging
from typing import Optional

_log = logging.getLogger(__name__)


def should_sweep(
    now_minutes: Optional[int],
    unfilled_frac: float,
    *,
    sweep_time_minutes: int,
    sweep_unfilled_frac: float,
) -> bool:
    """Return True when the sweep cutoff is reached OR enough remains unfilled.

    Parameters
    ----------
    now_minutes : int | None
        Minutes since market open (9:30 ET).  None = outside the session or
        unknown → the clock half never fires.
    unfilled_frac : float
        Fraction of the tracked order quantity still unfilled (0.0–1.0).  Pass
        0.0 to disable the fill-fraction half (clock-only trigger).
    sweep_time_minutes : int
        Cutoff in minutes since open; at or past this the clock half fires.
    sweep_unfilled_frac : float
        Threshold the unfilled fraction must reach for the fill half to fire.
    """
    clock = now_minutes is not None and now_minutes >= sweep_time_minutes
    frac = unfilled_frac >= sweep_unfilled_frac
    fire = clock or frac
    _log.debug(
        "should_sweep: now_min=%s clock=%s (cutoff=%d) frac=%.3f>=%.3f -> %s -> %s",
        now_minutes,
        clock,
        sweep_time_minutes,
        unfilled_frac,
        sweep_unfilled_frac,
        frac,
        fire,
    )
    return fire
