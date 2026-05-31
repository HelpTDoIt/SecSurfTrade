"""
Morning preflight — interactive readiness gate + order sizing walkthrough.

Runs AFTER cli.compute has produced state.json (positions + sell/buy
allocations) and BEFORE the human enters orders.  Sequence:

  1. Readiness gate (loops until GREEN):
       - Confirm Fidelity Trader+ is running.
       - Read the FT+ Watchlist (OCR), detect thin tickers, build an L2 window
         plan against the window cap, and verify every needed ticker is in the
         Watchlist and every thin ticker has an L2 window.
       - If anything is missing, print exactly what to add in FT+ and wait.
  2. Order sizing: run `cli.strategy --source atp --strict-atp --l2-symbols`.
       - On an OCR shortfall (strict stop), pause and require the human to
         choose: [R]etry / [Y]es-fall-back-to-yfinance / [A]bort.  The yfinance
         fallback (sizes without live L2 depth) only happens after explicit
         confirmation.
  3. Post-sizing sanity gate on the now-sized state.
       - RED blocks.  YELLOW pauses for confirmation.  GREEN proceeds.
  4. Spoonfeed the exact next steps through to order entry.

This module is the interactive shell; all testable decision logic lives in
preflight.orchestrator (and preflight.checks / .planner / .sanity).  OCR
adapters are imported lazily so importing this module stays cheap.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from cli import resolve_path
from preflight.checks import (
    TickerPresenceResult,
    check_ft_running,
    check_tickers_present,
)
from preflight.orchestrator import (
    build_sizing_command,
    classify_sizing_outcome,
    evaluate_readiness,
    extra_sanity_warnings,
)
from preflight.planner import L2WindowPlan, plan_l2_windows
from preflight.sanity import SanityFinding, check_sanity
from state.importer import load_state


# ── Console helpers ──────────────────────────────────────────────────────────


def _hr() -> None:
    print("  " + "-" * 60)


def _step(msg: str) -> None:
    print(f"\n  >> {msg}")


def _info(msg: str) -> None:
    print(f"     {msg}")


def _ok(msg: str) -> None:
    print(f"     OK   {msg}")


def _warn(msg: str) -> None:
    print(f"     WARN {msg}")


def _err(msg: str) -> None:
    print(f"     ERR  {msg}", file=sys.stderr)


def _prompt(msg: str) -> str:
    """Thin wrapper over input() so the shell stays the only I/O surface."""
    try:
        return input(msg).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "a"  # treat as abort


# ── Needed / thin ticker derivation ──────────────────────────────────────────


def _needed_tickers(state) -> list[str]:
    return sorted(
        {
            *(s.ticker for s in state.computed.sells),
            *(b.ticker for b in state.computed.buy_allocations),
        }
    )


def _thin_pairs(state, watchlist) -> list[tuple[str, float]]:
    """Reuse cli.strategy's thin-ticker detection, collapsed to (ticker, pct)."""
    from cli.strategy import _detect_thin_tickers  # noqa: PLC0415

    triples = _detect_thin_tickers(
        state.computed.sells, state.computed.buy_allocations, watchlist
    )
    return [(sym, pct) for sym, _side, pct in triples]


# ── Readiness gate ────────────────────────────────────────────────────────────


def _read_watchlist_once():
    """Live FT+ Watchlist OCR read.  Lazy import keeps module import cheap."""
    from adapters.atp_watchlist import OCRWatchlistAdapter  # noqa: PLC0415

    return OCRWatchlistAdapter().get_watchlist()


def _enumerate_l2():
    from adapters.atp_ocr import enumerate_l2_symbols  # noqa: PLC0415

    return enumerate_l2_symbols()


def _print_readiness(report) -> None:
    for line in report.overflow_warnings:
        _warn(line)
    for line in report.instructions:
        _info("- " + line)


def run_readiness_gate(state, cap: int):
    """Loop until the environment is GREEN.  Returns (plan, watchlist_rows).

    watchlist_rows is the FT+ read taken when ready (used later for ADV-based
    warnings); empty dict if readiness was forced/aborted.
    """
    needed = _needed_tickers(state)
    _info(f"{len(needed)} ticker(s) needed: {', '.join(needed)}")

    while True:
        ft = check_ft_running()
        watchlist_rows: dict = {}

        if ft.running:
            _ok("Fidelity Trader+ is running.")
            try:
                watchlist_rows = _read_watchlist_once()
            except Exception as exc:  # OCR/setup failure — treat as not-ready
                _err(f"Could not read the FT+ Watchlist via OCR: {exc}")
                watchlist_rows = {}

            thin = _thin_pairs(state, watchlist_rows) if watchlist_rows else []
            plan = plan_l2_windows(needed, thin, cap=cap)
            presence = check_tickers_present(
                plan,
                read_watchlist=lambda: watchlist_rows,
                enumerate_l2=_enumerate_l2,
            )
        else:
            _warn("Fidelity Trader+ is not running.")
            plan = plan_l2_windows(needed, [], cap=cap)
            # Placeholder: not-ready, no per-ticker detail until FT+ is up.
            presence = TickerPresenceResult(
                ok=False,
                missing_watchlist=[],
                missing_l2=[],
                present_watchlist=[],
                present_l2=[],
                visible_l2=[],
            )

        report = evaluate_readiness(ft, presence, plan)

        # Echo what THIS fresh OCR pass actually saw.  The watchlist + L2 reads
        # are re-taken every loop, so after the human changes FT+ they can
        # confirm here whether the change registered — instead of the gate
        # silently re-prompting for something they believe they already fixed.
        if ft.running:
            if watchlist_rows:
                _info(
                    f"Watchlist read (fresh OCR): {len(watchlist_rows)} ticker(s) "
                    f"detected — {', '.join(sorted(watchlist_rows))}"
                )
            else:
                _warn("Watchlist read (fresh OCR): no tickers detected.")
            detected_l2 = presence.visible_l2
            _info(
                "L2 panels detected (fresh OCR): "
                + (", ".join(detected_l2) if detected_l2 else "none")
            )
            if presence.missing_l2:
                _info(
                    "  (L2 panels are read by OCR from the RIGHT-HAND side of the "
                    "FT+ window; keep each panel's 'Level 2 <SYM>' title visible "
                    "on the right half or it won't be detected.)"
                )

        _print_readiness(report)

        if report.ready:
            _ok("Environment ready — all needed tickers present.")
            return plan, watchlist_rows

        _hr()
        ans = _prompt(
            "     Fix the items above in FT+, then press Enter to take a fresh "
            "OCR snapshot and re-check (or 'q' to abort): "
        )
        if ans.lower() in ("q", "a", "quit", "abort"):
            _err("Preflight aborted by user before readiness.")
            sys.exit(1)


# ── Order sizing ───────────────────────────────────────────────────────────────


def _run_sizing(argv: list[str]) -> tuple[int, str]:
    """Run the sizing subprocess; stdout streams live, stderr is captured."""
    _info("Running: " + " ".join(argv))
    proc = subprocess.run(argv, stderr=subprocess.PIPE, text=True)
    stderr = proc.stderr or ""
    if stderr:
        print(stderr, file=sys.stderr, end="")
    return proc.returncode, stderr


def run_order_sizing(state_path: str, confirmed_proceeds_path: str | None) -> bool:
    """Drive sizing with the strict gate + human-confirmed fallback.

    Returns True if sizing completed (strict or confirmed-fallback), having
    set the module-level fallback flag.  Returns False only via sys.exit.
    """
    used_fallback = False
    strict = True

    while True:
        argv = build_sizing_command(
            state_path,
            strict=strict,
            l2_auto=True,
            confirmed_proceeds_path=confirmed_proceeds_path,
        )
        rc, stderr = _run_sizing(argv)
        outcome = classify_sizing_outcome(rc, stderr)

        if outcome == "ok":
            _ok("Order sizing completed.")
            return used_fallback

        if outcome == "ocr_shortfall":
            _hr()
            _warn(
                "Order sizing stopped: live FT+ data was incomplete "
                "(a watchlist ticker missing or an L2 fetch failed)."
            )
            _info("[R] Retry the live OCR read (after fixing FT+).")
            _info(
                "[Y] Fall back to yfinance — sizes WITHOUT live L2 depth "
                "(standard legacy_dollar chunking)."
            )
            _info("[A] Abort the preflight.")
            ans = _prompt("     Choose [R/Y/A]: ").lower()
            if ans.startswith("r"):
                strict = True
                continue
            if ans.startswith("y"):
                confirm = _prompt(
                    "     Confirm yfinance fallback (sizes without live "
                    "L2 depth)? Type 'yes' to proceed: "
                ).lower()
                if confirm == "yes":
                    strict = False
                    used_fallback = True
                    _warn("Proceeding with yfinance fallback by user confirmation.")
                    continue
                _info("Fallback not confirmed — returning to choices.")
                continue
            _err("Preflight aborted by user during sizing.")
            sys.exit(1)

        # outcome == "error"
        _hr()
        _err(f"Order sizing failed (exit {rc}). See the error output above.")
        ans = _prompt("     [R]etry or [A]bort? ").lower()
        if ans.startswith("r"):
            continue
        sys.exit(1)


# ── Sanity gate ─────────────────────────────────────────────────────────────


def _print_findings(findings: list[SanityFinding]) -> None:
    reds = [f for f in findings if f.severity == "RED"]
    yellows = [f for f in findings if f.severity == "YELLOW"]
    for f in reds:
        _err(f"RED  [{f.code}] {f.message}")
    for f in yellows:
        _warn(f"[{f.code}] {f.message}")


def run_sanity_gate(
    state_path, plan: L2WindowPlan, watchlist_rows, used_fallback, adv_pct
):
    state = load_state(resolve_path(state_path))
    report = check_sanity(state)
    extra = extra_sanity_warnings(
        state,
        watchlist_rows,
        plan,
        used_yfinance_fallback=used_fallback,
        adv_pct=adv_pct,
    )
    all_findings = list(report.findings) + extra
    _print_findings(all_findings)

    has_red = any(f.severity == "RED" for f in all_findings)
    has_yellow = any(f.severity == "YELLOW" for f in all_findings)

    if has_red:
        _err("Sanity gate: RED — do NOT enter these orders. Fix and re-run.")
        sys.exit(2)
    if has_yellow:
        _hr()
        ans = _prompt(
            "     Sanity gate: YELLOW (warnings above). Proceed anyway? [y/N]: "
        ).lower()
        if not ans.startswith("y"):
            _err("Stopped at YELLOW sanity gate by user.")
            sys.exit(1)
        _warn("Proceeding past YELLOW warnings by user confirmation.")
    else:
        _ok("Sanity gate: GREEN.")


# ── Spoonfeed next steps ───────────────────────────────────────────────────────


def print_next_steps(state_path: str) -> None:
    guide = _ROOT.parent / "USER_GUIDE.md"
    _hr()
    print("  Preflight complete — the state is sized and sanity-checked.\n")
    print("  Next steps (manual order entry — the app never places orders):")
    _info("1. Review the sized chunks in the calculator or your state file:")
    _info(f"     {state_path}")
    _info("2. Enter each order manually in Fidelity Trader+, sell round first,")
    _info("   then buys, following the limit prices in the sized state.")
    _info("3. After fills, run the EOD report to capture the session:")
    _info("     Set-Location <root>\\fidelity_rebalancer; $env:PYTHONPATH = '.'")
    _info("     python -m cli.eod_report")
    print()
    _info(f"Full daily workflow: see Section 3 of {guide}")
    _hr()


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Morning preflight: readiness gate -> order sizing -> "
        "sanity gate -> next steps."
    )
    parser.add_argument(
        "--state", required=True, help="State JSON from cli.compute (sized in place)."
    )
    parser.add_argument(
        "--cap", type=int, default=7, help="Max L2 windows available (default 7)."
    )
    parser.add_argument(
        "--adv-pct",
        type=float,
        default=10.0,
        help="Flag a chunk as oversized when it exceeds this %% of 10-day ADV.",
    )
    parser.add_argument(
        "--confirmed-proceeds",
        default=None,
        metavar="JSON",
        help="Passed through to cli.strategy (actual sell proceeds per account).",
    )
    args = parser.parse_args()

    state_path = args.state
    state = load_state(resolve_path(state_path))

    print("\n  Morning Preflight")
    _hr()

    _step("Step 1/3 — Readiness gate")
    plan, watchlist_rows = run_readiness_gate(state, cap=args.cap)

    _step("Step 2/3 — Order sizing")
    used_fallback = run_order_sizing(state_path, args.confirmed_proceeds)

    _step("Step 3/3 — Pre-trade sanity gate")
    run_sanity_gate(state_path, plan, watchlist_rows, used_fallback, args.adv_pct)

    print_next_steps(state_path)


if __name__ == "__main__":
    main()
