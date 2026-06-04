"""
Side-aware time-of-day urgency escalation for the strategy generators.

As the close nears, an unfilled order's posture ramps
``patient → normal → aggressive`` and its limit is nudged toward the *taking*
side of the book:

  * **buy**  → toward the **ask**  (pay up to get filled)
  * **sell** → toward the **bid**  (give up edge to get filled)

The checkpoints below are time-based (minutes since the 9:30 ET open) and are
the symmetric generalisation of the original buy-only ramp.  This module is now
the single implementation: ``strategy_buy.py::_escalate_buy`` is a thin shim
that delegates here with ``side="buy"`` (kept only for its stable name and the
buy-parity test in ``tests/test_strategy.py``).

Pure module: no file/network/print I/O (enforced by
``tests/test_calculator.py::test_no_io_in_engine``).
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from adapters import QuoteSnapshot
from engine.chunker import round_to_tick

_log = logging.getLogger(__name__)

Side = Literal["buy", "sell"]

# Time-based checkpoints (the single source of truth for the urgency ramp).
#   0-90 min  (9:30-11:00): use the rule's urgency as-is
#  90-210 min (11:00-1:00): patient → normal, nudge limit 75% toward the touch
# 210-330 min  (1:00-3:00): any → aggressive, limit at the touch
# 330+ min     (3:00-4:00): aggressive, limit one tick PAST the touch
_ESCALATION_CHECKPOINTS = [
    (90,  "normal",     0.75),   # 75% toward the touch from the current limit
    (210, "aggressive", 1.0),    # at the touch (ask for buy / bid for sell)
    (330, "aggressive", 1.0),    # one tick past the touch (handled specially)
]

_URGENCY_RANK = {"patient": 0, "normal": 1, "aggressive": 2}


def _escalate(
    side: Side,
    urgency: str,
    limit_price: float,
    reasoning: list[str],
    quote: QuoteSnapshot,
    px_tick: float,
    market_minutes: Optional[int],
) -> tuple[str, float, list[str]]:
    """Escalate urgency/limit by time-of-day for ``side``.

    Returns ``(urgency, limit, reasoning)``.  When no checkpoint applies (early
    session, market_minutes unknown, or the chosen rule is already at/above the
    checkpoint's posture) the inputs are returned unchanged.

    ``buy`` nudges the limit toward the **ask**; ``sell`` toward the **bid**.
    """
    if market_minutes is None or market_minutes < 0:
        return urgency, limit_price, reasoning

    # The "touch" we ramp toward, and the per-side tick direction past it.
    if side == "buy":
        touch = quote.ask or quote.last or quote.bid
        past_sign = +1          # past the last checkpoint: ask + 1 tick
        touch_label = "ask"
    else:
        touch = quote.bid or quote.last or quote.ask
        past_sign = -1          # past the last checkpoint: bid − 1 tick
        touch_label = "bid"

    if not touch or touch <= 0:
        return urgency, limit_price, reasoning

    # Pick the last checkpoint whose threshold has been reached.
    target_urgency = None
    touch_frac = 0.0
    for threshold, t_urg, frac in _ESCALATION_CHECKPOINTS:
        if market_minutes >= threshold:
            target_urgency = t_urg
            touch_frac = frac
        else:
            break

    if target_urgency is None:
        return urgency, limit_price, reasoning

    # Past the last checkpoint: one tick beyond the touch to force a same-day fill.
    last_threshold = _ESCALATION_CHECKPOINTS[-1][0]
    if market_minutes >= last_threshold:
        new_limit = round_to_tick(touch + past_sign * px_tick, touch)
        sign_label = "+" if past_sign > 0 else "−"
        return "aggressive", new_limit, reasoning + [
            f"Urgency escalation: {market_minutes} min into session "
            f"(≥{last_threshold} min). LIMIT moved to {touch_label}{sign_label}1 tick "
            f"${new_limit:.4f} to ensure same-day fill.",
        ]

    # Never downgrade a rule that already chose an equal-or-stronger posture.
    if _URGENCY_RANK.get(target_urgency, 0) <= _URGENCY_RANK.get(urgency, 0):
        return urgency, limit_price, reasoning

    new_limit = round_to_tick(
        limit_price + (touch - limit_price) * touch_frac, touch
    )
    return target_urgency, new_limit, reasoning + [
        f"Urgency escalation: {market_minutes} min into session → {target_urgency}. "
        f"LIMIT adjusted to ${new_limit:.4f}.",
    ]
