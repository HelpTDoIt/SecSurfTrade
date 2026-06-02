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

    # Two INDEPENDENT requirements per ticker:
    #   • Watchlist membership — needed for every traded ticker (quote/ADV).
    #   • An open L2 window     — needed only for THIN tickers (live depth).
    # A thin ticker can satisfy one and still be missing the other, so these are
    # reported as distinct statements; a ticker missing from both gets both.
    for ticker in presence.missing_watchlist:
        instructions.append(
            f"Add {ticker} to the FT+ Watchlist "
            f"(missing from Watchlist — required for every traded ticker's "
            f"quote/ADV data; separate from its L2 window)."
        )

    for ticker in presence.missing_l2:
        instructions.append(
            f"Open an L2 window for {ticker} "
            f"(missing from L2 — thin ticker needs live depth; "
            f"this is separate from being in the Watchlist)."
        )

    # When the human must open L2 windows, spell out which currently-open panels
    # are safe to close — so they don't accidentally close one we still need for
    # depth.  "Safe" = open panels whose ticker is NOT in the assigned set.
    if presence.missing_l2:
        assigned_norm = {t.strip().upper() for t in plan.l2_assigned}
        visible = list(presence.visible_l2)
        keep_open = sorted(t for t in visible if t.strip().upper() in assigned_norm)
        closeable = sorted(t for t in visible if t.strip().upper() not in assigned_norm)
        if closeable:
            instructions.append(
                "L2 windows currently open: "
                f"{', '.join(visible)}. "
                "Safe to close to free a slot: "
                f"{', '.join(closeable)}. "
                "Do NOT close (still needed for depth): "
                f"{', '.join(keep_open) if keep_open else 'none'}."
            )
        elif plan.cap > 0 and len(visible) >= plan.cap:
            # All slots are occupied by tickers we actually need — closing any
            # would drop a needed panel.  The only safe moves are raising the cap
            # or trading fewer thin tickers at once.
            instructions.append(
                f"All {plan.cap} L2 window slot(s) are in use by needed tickers "
                f"({', '.join(keep_open)}). Raise --cap or reduce the number of "
                "thin tickers traded at once to add more depth."
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


@dataclass(frozen=True)
class WatchlistGuidance:
    """What to ADD / REMOVE in the FT+ Watchlist before sizing.

    `ok` is True only when every needed ticker is present.  `add` is blocking
    (sizing needs a quote/ADV for every traded ticker); `remove` is advisory —
    extra tickers don't break sizing, but trimming them keeps the OCR read fast
    and the window readable.
    """

    ok: bool
    add: list[str]
    remove: list[str]


def evaluate_watchlist(needed: list[str], watchlist_syms) -> WatchlistGuidance:
    """Compare the needed tickers against what the Watchlist OCR actually saw."""
    needed_n = {t.strip().upper() for t in needed}
    have_n = {t.strip().upper() for t in watchlist_syms}
    add = sorted(needed_n - have_n)
    remove = sorted(have_n - needed_n)
    return WatchlistGuidance(ok=not add, add=add, remove=remove)


@dataclass(frozen=True)
class L2PriorityGuidance:
    """Whether the open L2 panels hold the highest-impact (top-cap) tickers.

    `ok` is True when every top-`cap` priority ticker already has an open panel
    (nothing to open).  This gate is advisory: a missing panel only means that
    ticker falls back to POV sizing, so the human may proceed anyway.

    `use`      — top-cap priority tickers whose panel is open (will get live L2).
    `to_open`  — top-cap priority tickers WITHOUT a panel (open for better chunking).
    `to_close` — open panels NOT among the top-cap priority set (safe to close to
                 free a slot for a `to_open` ticker).
    """

    ok: bool
    use: list[str]
    to_open: list[str]
    to_close: list[str]


def evaluate_l2_priorities(
    ranked_syms: list[str],
    open_panels,
    cap: int,
) -> L2PriorityGuidance:
    """Decide which open panels serve the highest-impact orders.

    `ranked_syms` is the ticker priority high→low (from cli.strategy's
    _rank_l2_candidates); `open_panels` is the set of L2 panels currently open.
    Mirrors exactly the selection cli.strategy will make at sizing time, so the
    human can fix the panels BEFORE sizing instead of seeing the warning after.
    """
    from cli.strategy import _select_l2_symbols  # noqa: PLC0415

    use, to_open = _select_l2_symbols(ranked_syms, set(open_panels), cap=cap)
    use_n = {t.upper() for t in use}
    to_close = sorted(p for p in {s.upper() for s in open_panels} if p not in use_n)
    return L2PriorityGuidance(
        ok=not to_open,
        use=list(use),
        to_open=list(to_open),
        to_close=to_close,
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
