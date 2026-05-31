"""
Pre-flight orchestrator — pure, dependency-free logic for the morning
preflight CLI.

The interactive shell (input()/subprocess) lives elsewhere; this module only
composes the already-built pieces (planner / checks / sanity / strategy
contract) into decisions and instructions.  It imports only stdlib + project
modules and performs NO I/O, NO subprocess, NO OCR.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from adapters import WatchlistRow
from cli.strategy import OCR_SHORTFALL_EXIT, OCR_SHORTFALL_MARKER
from preflight.checks import FtRunningResult, TickerPresenceResult
from preflight.planner import L2WindowPlan
from preflight.sanity import SanityFinding
from state.schema import RebalanceState


@dataclass(frozen=True)
class ReadinessReport:
    """Outcome of the environment readiness evaluation.

    `ready` is True only when FT+ is running AND nothing is missing.
    `instructions` are human-actionable lines (empty when ready).
    `overflow_warnings` are non-blocking notes about thin tickers that
    exceeded the L2 window cap.
    """

    ready: bool
    instructions: list[str] = field(default_factory=list)
    overflow_warnings: list[str] = field(default_factory=list)


def evaluate_readiness(
    ft: FtRunningResult,
    presence: TickerPresenceResult,
    plan: L2WindowPlan,
) -> ReadinessReport:
    instructions: list[str] = []

    if not ft.running:
        instructions.append(
            f"Start Fidelity Trader+ and log in, then retry. ({ft.detail})"
        )

    for ticker in presence.missing_watchlist:
        instructions.append(f"Add {ticker} to the FT+ Watchlist.")

    for ticker in presence.missing_l2:
        instructions.append(
            f"Open an L2 window for {ticker} (thin ticker — needs depth)."
        )

    overflow_warnings = [
        f"{ticker} is thin but exceeds the L2 window cap ({plan.cap}); it will "
        f"be sized without live depth unless an L2 window is freed."
        for ticker in plan.l2_overflow
    ]

    ready = ft.running and presence.ok

    return ReadinessReport(
        ready=ready,
        instructions=instructions,
        overflow_warnings=overflow_warnings,
    )


def build_sizing_command(
    state_path: str,
    *,
    python_exe: str = sys.executable,
    strict: bool = True,
    l2_auto: bool = True,
    confirmed_proceeds_path: str | None = None,
) -> list[str]:
    """Build the argv list to run the order-sizing module.

    Flag ordering is deterministic so callers can assert on it.
    `--l2-symbols` is emitted with NO following token (auto-detect mode).
    """
    argv = [
        python_exe,
        "-m",
        "cli.strategy",
        "--state",
        state_path,
        "--export",
        state_path,
        "--source",
        "atp",
    ]
    if strict:
        argv.append("--strict-atp")
    if l2_auto:
        argv.append("--l2-symbols")
    if confirmed_proceeds_path is not None:
        argv.extend(["--confirmed-proceeds", confirmed_proceeds_path])
    return argv


def classify_sizing_outcome(returncode: int, stderr: str) -> str:
    """Map a sizing subprocess result to "ok" | "ocr_shortfall" | "error"."""
    if returncode == 0:
        return "ok"
    if returncode == OCR_SHORTFALL_EXIT or OCR_SHORTFALL_MARKER in stderr:
        return "ocr_shortfall"
    return "error"


def extra_sanity_warnings(
    state: RebalanceState,
    watchlist_rows: dict[str, WatchlistRow],
    plan: L2WindowPlan,
    *,
    used_yfinance_fallback: bool,
    adv_pct: float = 10.0,
) -> list[SanityFinding]:
    """YELLOW findings that the state alone cannot express.

    These need ADV (10-day avg volume) and knowledge of which tickers actually
    received live L2 depth — neither of which lives in RebalanceState.
    """
    findings: list[SanityFinding] = []

    # ── THIN_NO_L2 ─────────────────────────────────────────────────────────
    # Overflow tickers never got a window.  If yfinance fallback was used, the
    # sizing ran with NO live L2 at all, so the assigned tickers are equally
    # un-backed.  De-dupe by ticker.
    thin_no_l2: list[str] = list(plan.l2_overflow)
    if used_yfinance_fallback:
        thin_no_l2.extend(plan.l2_assigned)

    seen: set[str] = set()
    for ticker in thin_no_l2:
        if ticker in seen:
            continue
        seen.add(ticker)
        findings.append(
            SanityFinding(
                severity="YELLOW",
                code="THIN_NO_L2",
                message=(
                    f"Thin ticker {ticker} was sized without live L2 depth "
                    f"(no L2 window available)."
                ),
                ref=ticker,
            )
        )

    # ── OVERSIZED_VS_ADV ───────────────────────────────────────────────────
    for c in (*state.computed.sell_chunks, *state.computed.buy_chunks):
        row = watchlist_rows.get(c.ticker)
        if row is None:
            continue
        adv = row.avg_vol_10d
        if adv <= 0:
            continue
        pct = c.shares / adv * 100.0
        if pct > adv_pct:
            findings.append(
                SanityFinding(
                    severity="YELLOW",
                    code="OVERSIZED_VS_ADV",
                    message=(
                        f"Chunk {c.chunk_id} ({c.ticker}) is {pct:.2f}% of "
                        f"10-day ADV ({adv}), above the {adv_pct}% threshold."
                    ),
                    ref=c.chunk_id,
                )
            )

    return findings
