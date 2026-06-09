"""
Regression tests for the Phase 3 multi-tranche scheduler activated by the
`--schedule` CLI flag (cli.strategy).

Two layers are guarded:
  * the engine substance — build_day_schedule() splits an order into the
    premarket / main / sweep tranches with the documented retirement gating;
  * the wiring — `--schedule` is actually exposed by the cli.strategy argparse
    (a `--help` smoke check), so the flag can't silently disappear.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

_FR_DIR = Path(__file__).resolve().parent.parent  # .../fidelity_rebalancer
if str(_FR_DIR) not in sys.path:
    sys.path.insert(0, str(_FR_DIR))

from engine.scheduler import build_day_schedule  # noqa: E402
from state.schema import EngineConfig  # noqa: E402

# A premarket timestamp: 09:35 ET -> mkt_minutes = 5 (< 30) so the capture
# (premarket) tranche is eligible.
_PREMARKET_NOW = datetime(2026, 6, 8, 9, 35)


def _ctx(adv: float = 5_000_000.0) -> SimpleNamespace:
    # build_day_schedule only reads ctx.adv (passed through to the chunker).
    return SimpleNamespace(adv=adv)


def _quote(prev_close: float, last: float) -> SimpleNamespace:
    return SimpleNamespace(prev_close=prev_close, bid=last - 0.05, ask=last + 0.05)


def test_build_day_schedule_taxable_sell_has_all_three_tranches() -> None:
    record = SimpleNamespace(
        account="Test Taxable", strategy="MOMENTUM", ticker="AOR", shares=1000
    )
    chunks = build_day_schedule(
        record,
        "sell",
        ctx=_ctx(),
        now=_PREMARKET_NOW,
        config=EngineConfig(),
        base_limit_price=100.0,
        quote=_quote(prev_close=99.0, last=100.0),
        account_type="taxable",
    )
    phases = {c.phase for c in chunks}
    assert phases == {"premarket", "main", "sweep"}

    # The sweep tranche is clock-gated to 15:00 ET (sweep_time_minutes=330).
    sweep = [c for c in chunks if c.phase == "sweep"]
    assert sweep and all(c.earliest_entry == "15:00:00" for c in sweep)


def test_build_day_schedule_retirement_buy_skips_premarket_and_gates_main() -> None:
    record = SimpleNamespace(
        account="Test Retirement", strategy="ROTATION", ticker="EEM", share_target=1000
    )
    chunks = build_day_schedule(
        record,
        "buy",
        ctx=_ctx(),
        now=_PREMARKET_NOW,
        config=EngineConfig(),
        base_limit_price=50.0,
        quote=_quote(prev_close=49.0, last=50.0),
        account_type="retirement",
    )
    phases = [c.phase for c in chunks]
    # Retirement buys must NOT enter premarket (they are clock-gated anyway).
    assert "premarket" not in phases
    # ...and the main tranche is gated to midday for a retirement buy.
    main_chunks = [c for c in chunks if c.phase == "main"]
    assert main_chunks
    assert all(c.earliest_entry == "12:00:00" for c in main_chunks)


def test_build_day_schedule_zero_shares_is_empty() -> None:
    record = SimpleNamespace(account="Test Taxable", strategy="X", ticker="AOR", shares=0)
    chunks = build_day_schedule(
        record,
        "sell",
        ctx=_ctx(),
        now=_PREMARKET_NOW,
        config=EngineConfig(),
        base_limit_price=100.0,
        quote=_quote(prev_close=99.0, last=100.0),
        account_type="taxable",
    )
    assert chunks == []


def test_schedule_flag_present_in_cli_help() -> None:
    """Guard the wiring: `--schedule` must be a real cli.strategy argparse flag."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_FR_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "cli.strategy", "--help"],
        cwd=str(_FR_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert "--schedule" in out, f"--schedule missing from cli.strategy help:\n{out}"
