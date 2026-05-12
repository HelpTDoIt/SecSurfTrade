"""
Portfolio-level buy progress tracker.

Compares buy fill completion (filled $ / total buy budget) against elapsed
trading time.  Flags buys that are behind schedule.

Usage (from SecSurfTrade/ or fidelity_rebalancer/):
    python -m fidelity_rebalancer.cli.progress --state today.json
    python -m cli.progress --state today.json
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from cli import resolve_path
from state.importer import load_state

_MARKET_OPEN_HOUR, _MARKET_OPEN_MIN = 9, 30
_MARKET_MINUTES = 390  # 9:30 → 16:00


def _time_elapsed_pct(now: datetime | None = None) -> float:
    now = now or datetime.now()
    mins = (now.hour - _MARKET_OPEN_HOUR) * 60 + (now.minute - _MARKET_OPEN_MIN)
    if mins < 0:
        return 0.0
    if mins > _MARKET_MINUTES:
        return 100.0
    return mins / _MARKET_MINUTES * 100.0


def compute_buy_progress(state) -> list[dict]:
    """Return per-buy progress dicts with pct_complete and behind_schedule flag."""
    fills_by_chunk: dict[str, float] = {}
    if state.execution_state:
        for fill in state.execution_state.fills:
            fills_by_chunk[fill.chunk_id] = fill.filled_shares

    results = []
    for buy in state.computed.buy_allocations:
        strat = next(
            (s for s in state.computed.buy_strategies
             if s.account == buy.account and s.ticker == buy.ticker),
            None,
        )
        if not strat:
            continue

        total_shares = 0.0
        filled_shares = 0.0
        for cid in strat.chunk_ids:
            chunk = next((c for c in state.computed.buy_chunks if c.chunk_id == cid), None)
            if chunk:
                total_shares += chunk.shares
                filled_shares += fills_by_chunk.get(cid, 0.0)

        pct_complete = (filled_shares / total_shares * 100.0) if total_shares > 0 else 0.0
        time_pct = _time_elapsed_pct()

        results.append({
            "account": buy.account,
            "ticker": buy.ticker,
            "total_shares": total_shares,
            "filled_shares": filled_shares,
            "pct_complete": pct_complete,
            "time_elapsed_pct": time_pct,
            "behind_schedule": time_pct > 0 and pct_complete < time_pct * 0.5,
            "urgency": strat.urgency,
        })
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Buy progress tracker")
    parser.add_argument("--state", required=True, help="Engine state JSON")
    args = parser.parse_args()

    state = load_state(resolve_path(args.state))
    time_pct = _time_elapsed_pct()
    now = datetime.now()
    print(f"Trading day progress: {time_pct:.1f}% ({now.strftime('%H:%M')})")

    progress = compute_buy_progress(state)
    if not progress:
        print("No buy strategies found in state.")
        return

    total_filled = sum(p["filled_shares"] for p in progress)
    total_target = sum(p["total_shares"] for p in progress)
    portfolio_pct = (total_filled / total_target * 100.0) if total_target > 0 else 0.0
    print(f"Portfolio buy completion: {portfolio_pct:.1f}% "
          f"({total_filled:.0f}/{total_target:.0f} shares)")

    behind = [p for p in progress if p["behind_schedule"]]
    if behind:
        print(f"\n{len(behind)} buy(s) behind schedule:")
        for p in behind:
            print(f"  {p['ticker']:6s}  {p['pct_complete']:5.1f}% filled "
                  f"(time {p['time_elapsed_pct']:.1f}% elapsed)  "
                  f"urgency={p['urgency']}")
    else:
        print("All buys on track.")

    print()
    for p in progress:
        flag = " ⚠ BEHIND" if p["behind_schedule"] else ""
        print(f"  {p['account']:20s}  {p['ticker']:6s}  "
              f"{p['filled_shares']:>8.0f}/{p['total_shares']:<8.0f} shares  "
              f"{p['pct_complete']:5.1f}%{flag}")


if __name__ == "__main__":
    main()
