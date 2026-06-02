"""
Morning preflight — interactive readiness gate + order sizing walkthrough.

Runs AFTER cli.compute has produced state.json (positions + sell/buy
allocations) and BEFORE the human enters orders.  Sequence:

  1. Readiness gate (environment only; loops until ready):
       - Confirm Fidelity Trader+ is running and logged in.  Nothing can be
         OCR'd until it is, so this is a pure environment check — Watchlist and
         L2 placement are verified in step 2, where they are read live.
  2. Pre-sizing checks + order sizing:
       - (a) Watchlist completeness — every traded ticker must be in the FT+
             Watchlist; show what to ADD (blocking) / REMOVE (advisory), then
             re-OCR until complete.
       - (b) L2 priority — the open L2 panels should hold the highest-impact
             orders (top-cap by %ADV); show which to OPEN / CLOSE, then
             re-detect.  Advisory: a missing panel just falls back to POV.
       - Then run `cli.strategy --source atp --strict-atp --l2-symbols`.  On an
         OCR shortfall (strict stop), pause and require the human to choose:
         [R]etry / [Y]es-fall-back-to-yfinance / [A]bort.  The yfinance fallback
         (sizes without live L2 depth) only happens after explicit confirmation.
       - Watchlist + L2 are each checked EXACTLY ONCE here (not also in step 1),
         right before sizing, so the human is never prompted about the same
         thing twice.
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
from preflight.checks import check_ft_running
from preflight.orchestrator import (
    build_sizing_command,
    classify_sizing_outcome,
    evaluate_l2_priorities,
    evaluate_watchlist,
    extra_sanity_warnings,
)
from preflight.planner import L2WindowPlan, plan_l2_windows
from preflight.sanity import SanityFinding, check_sanity, explain
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


def run_readiness_gate(state) -> None:
    """Loop until Fidelity Trader+ is running and connectable.

    This is the ENVIRONMENT gate only: nothing can be OCR'd until FT+ is up and
    logged in.  Watchlist completeness and L2-window placement are verified in
    step 2 (run_presizing_checks), which reads the live FT+ window directly — so
    each is checked exactly once, right before sizing, instead of here and again
    there (which used to double-prompt the human about the same L2 windows).
    """
    needed = _needed_tickers(state)
    _info(f"{len(needed)} ticker(s) will be traded: {', '.join(needed)}")

    while True:
        ft = check_ft_running()
        if ft.running:
            _ok("Fidelity Trader+ is running and connectable.")
            return

        _warn("Fidelity Trader+ is not running.")
        _info(f"- Start Fidelity Trader+ and log in, then retry. ({ft.detail})")
        _hr()
        ans = _prompt(
            "     Start FT+ and log in, then press Enter to re-check "
            "(or 'q' to abort): "
        )
        if ans.lower() in ("q", "a", "quit", "abort"):
            _err("Preflight aborted by user before readiness.")
            sys.exit(1)


# ── Pre-sizing confirmation (watchlist + L2 priority) ────────────────────────


def run_presizing_checks(state, cap: int) -> None:
    """Confirm the FT+ environment is right BEFORE order sizing runs.

    Sizing is about to call cli.strategy with live OCR (--source atp) and L2
    auto-detect.  Two things are far cheaper to fix now than to discover
    mid-sizing, so each gets a pause-and-recheck loop reading LIVE FT+ via OCR:

      (a) Watchlist completeness — every traded ticker must be in the FT+
          Watchlist (sizing reads its quote/ADV there).  Shows what to ADD
          (blocking) and what extra can be REMOVED (advisory), then re-OCRs.
      (b) L2 priority placement — the open L2 panels should hold the
          highest-impact orders (top-`cap` by %ADV: bigger order vs. daily
          volume => fill quality depends most on real book depth).  Shows which
          to OPEN and which are safe to CLOSE, then re-detects.  Advisory: a
          ticker without a panel just falls back to POV sizing.

    Decision logic lives in preflight.orchestrator (evaluate_watchlist /
    evaluate_l2_priorities); this function is only the OCR + prompt shell.

    Returns (plan, watchlist_rows): the L2WindowPlan (thin-ticker assignment,
    rebuilt from the watchlist read taken here) and the final FT+ Watchlist OCR
    rows.  Both feed the step-3 sanity gate (THIN_NO_L2 + OVERSIZED_VS_ADV).
    """
    from cli.strategy import _rank_l2_candidates  # noqa: PLC0415

    needed = _needed_tickers(state)
    _info(f"Pre-sizing check: {len(needed)} ticker(s) will be sized.")

    # ── (a) Watchlist completeness ─────────────────────────────────────────
    watchlist_rows: dict = {}
    while True:
        try:
            watchlist_rows = _read_watchlist_once()
        except Exception as exc:  # OCR/setup failure — treat as empty read
            _err(f"Could not read the FT+ Watchlist via OCR: {exc}")
            watchlist_rows = {}

        wl = evaluate_watchlist(needed, watchlist_rows.keys())
        if wl.remove:
            _info(
                "Watchlist tickers NOT needed today (optional — remove for a "
                "cleaner/faster read): " + ", ".join(wl.remove)
            )
        if wl.ok:
            _ok(f"Watchlist complete — all {len(needed)} needed ticker(s) present.")
            break

        _warn(
            "Watchlist is MISSING needed ticker(s) — sizing needs a quote/ADV for each:"
        )
        _info("  ADD to the FT+ Watchlist: " + ", ".join(wl.add))
        _hr()
        ans = _prompt(
            "     Add the ticker(s) above in FT+, then press Enter to re-check "
            "(or 'q' to abort): "
        )
        if ans.lower() in ("q", "a", "quit", "abort"):
            _err("Preflight aborted by user during the watchlist check.")
            sys.exit(1)

    # ── (b) L2 priority placement ──────────────────────────────────────────
    ranked = _rank_l2_candidates(
        state.computed.sells, state.computed.buy_allocations, watchlist_rows
    )
    ranked_syms = [t for t, _ in ranked]
    while True:
        try:
            open_panels = {s.upper() for s in _enumerate_l2()}
        except Exception as exc:
            _err(f"Could not enumerate L2 panels via OCR: {exc}")
            open_panels = set()

        guide = evaluate_l2_priorities(ranked_syms, open_panels, cap)
        _info(
            "Open L2 panels (fresh OCR): "
            + (", ".join(sorted(open_panels)) if open_panels else "none")
        )
        _info(
            f"  Top {cap} by %ADV that WILL get live L2 (panel open): "
            + (", ".join(guide.use) if guide.use else "none")
        )
        if guide.ok:
            _ok("L2 panels cover the highest-impact orders.")
            break

        _warn(
            "Higher-impact ticker(s) have NO open L2 panel "
            "(they will fall back to POV sizing — coarser chunking):"
        )
        _info("  OPEN an L2 window in FT+ for: " + ", ".join(guide.to_open))
        if guide.to_close:
            _info(
                "  Safe to CLOSE to free a slot (not in the top priority set): "
                + ", ".join(guide.to_close)
            )
        _hr()
        ans = _prompt(
            "     [O]pen the panels above then re-check, [P]roceed with POV "
            "fallback for those, or [A]bort: "
        ).lower()
        if ans.startswith("a"):
            _err("Preflight aborted by user during the L2 priority check.")
            sys.exit(1)
        if ans.startswith("p"):
            _warn(
                "Proceeding — the listed ticker(s) will be sized with POV "
                "(no live L2 depth)."
            )
            break
        # default / 'o' → loop and re-detect

    # Build the L2 plan for the step-3 sanity gate (THIN_NO_L2 over thin tickers
    # that exceed the window cap).  Same computation the old readiness gate did,
    # using the watchlist read taken above.
    thin = _thin_pairs(state, watchlist_rows) if watchlist_rows else []
    plan = plan_l2_windows(needed, thin, cap=cap)
    return plan, watchlist_rows


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


def print_order_book(state) -> None:
    """Show the suggested orders the gate is about to validate.

    Issue (a): the human must SEE the recommended orders on screen BEFORE the
    rules run, so a RED/YELLOW finding can be tied to a concrete line.  Records
    have been reconciled to their chunks in cli.strategy, so the per-ticker
    total equals the sum of the chunks below it.

    Each order is printed CHUNK-BY-CHUNK — one indented line per chunk — because
    the human enters one Fidelity order per chunk at market open.  This
    intentionally prints per-trade detail (ticker / limit / shares) to the
    console, overriding the default DEBUG-only suppression, because this is the
    interactive human-verification surface.
    """
    sells = state.computed.sells
    buys = state.computed.buy_allocations
    _step("Suggested order book (verify before the gate runs)")
    _info(
        "These are the EXACT orders you will key into Fidelity. They are read "
        "from the 'computed' block of state.json (sells, buy_allocations, and "
        "the sell_chunks/buy_chunks just sized) — nothing here is live; it is a"
    )
    _info(
        "snapshot of the file. Each ticker shows its total, then one indented "
        "line per chunk = one Fidelity order, in entry order."
    )

    if not sells and not buys:
        _info("No orders in state. Nothing to verify.")
        return

    # Chunk lookup keyed by (account, strategy, ticker), ordered by idx — this is
    # the entry order. These are what actually get keyed into Fidelity.
    chunk_index: dict[tuple, list] = {}
    for c in (*state.computed.sell_chunks, *state.computed.buy_chunks):
        chunk_index.setdefault((c.account, c.strategy, c.ticker), []).append(c)
    for v in chunk_index.values():
        v.sort(key=lambda c: c.idx)

    def _emit(
        side: str,
        account: str,
        strategy: str,
        ticker: str,
        total_shares: float,
        limit: float,
        est: float,
    ) -> None:
        chunks = chunk_index.get((account, strategy, ticker), [])
        n = len(chunks)
        _info(
            f"    {side:<4} {ticker:<6} total {total_shares:>12,.0f} sh "
            f"@ ${limit:>10,.4f}  ~${est:>14,.2f}  [{strategy}]  "
            f"({n} chunk{'s' if n != 1 else ''})"
        )
        if n == 0:
            _warn(f"         NO CHUNKS for this order — it cannot be entered as-is.")
            return
        for c in chunks:
            _info(
                f"         #{c.idx + 1:<2} {c.shares:>10,.0f} sh "
                f"@ ${c.limit_price:>10,.4f}  ~${c.cost:>14,.2f}"
            )

    by_acct: dict[str, dict[str, list]] = {}
    for s in sells:
        by_acct.setdefault(s.account, {"SELL": [], "BUY": []})["SELL"].append(s)
    for b in buys:
        by_acct.setdefault(b.account, {"SELL": [], "BUY": []})["BUY"].append(b)

    grand_sell = grand_buy = 0.0
    for account in sorted(by_acct):
        groups = by_acct[account]
        _info(f"{account}:")
        acct_sell = acct_buy = 0.0
        for s in sorted(groups["SELL"], key=lambda x: (x.strategy, x.ticker)):
            acct_sell += s.est_proceeds
            _emit(
                "SELL",
                s.account,
                s.strategy,
                s.ticker,
                s.shares,
                s.limit_price,
                s.est_proceeds,
            )
        for b in sorted(groups["BUY"], key=lambda x: (x.strategy, x.ticker)):
            acct_buy += b.est_cost
            _emit(
                "BUY",
                b.account,
                b.strategy,
                b.ticker,
                float(b.share_target),
                b.limit_price,
                b.est_cost,
            )
        _info(
            f"    -- account totals: sell ~${acct_sell:,.2f}  "
            f"buy ~${acct_buy:,.2f}  net ~${acct_sell - acct_buy:,.2f}"
        )
        grand_sell += acct_sell
        grand_buy += acct_buy
    _hr()
    _info(
        f"All accounts: sell ~${grand_sell:,.2f}  buy ~${grand_buy:,.2f}  "
        f"net ~${grand_sell - grand_buy:,.2f}"
    )


def _print_findings(findings: list[SanityFinding]) -> None:
    reds = [f for f in findings if f.severity == "RED"]
    yellows = [f for f in findings if f.severity == "YELLOW"]
    if reds:
        _err("RED findings BLOCK trading — they must be fixed before any order:")
    for f in reds:
        _err(f"RED  [{f.code}] {f.message}")
        _err(f"          why: {explain(f.code)}")
    if yellows:
        _warn("YELLOW findings are warnings — review, then you may proceed:")
    for f in yellows:
        _warn(f"[{f.code}] {f.message}")
        _warn(f"     why: {explain(f.code)}")


def run_sanity_gate(
    state_path, plan: L2WindowPlan, watchlist_rows, used_fallback, adv_pct
):
    state = load_state(resolve_path(state_path))
    _info(f"Sanity gate reads the freshly-sized state file: {resolve_path(state_path)}")
    _info(
        "It re-loads that file, prints the order book below, then runs the "
        "rule checks. RED = do not trade; YELLOW = review then confirm; "
        "GREEN = clear. Every finding points at a line in the order book."
    )
    print_order_book(state)
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
    parser.add_argument(
        "--no-next-steps",
        action="store_true",
        help="Suppress the trailing 'Next steps' block. Used when a wrapper "
        "(morning-prep.ps1) prints one consolidated next-steps block at the "
        "very end of its run instead.",
    )
    args = parser.parse_args()

    state_path = args.state
    state = load_state(resolve_path(state_path))

    print("\n  Morning Preflight")
    _hr()

    _step("Step 1/3 — Readiness gate (Fidelity Trader+ running)")
    run_readiness_gate(state)

    _step("Step 2/3 — Order sizing (watchlist + L2 checks, then size)")
    plan, watchlist_rows = run_presizing_checks(state, cap=args.cap)
    used_fallback = run_order_sizing(state_path, args.confirmed_proceeds)

    _step("Step 3/3 — Pre-trade sanity gate")
    run_sanity_gate(state_path, plan, watchlist_rows, used_fallback, args.adv_pct)

    if not args.no_next_steps:
        print_next_steps(state_path)


if __name__ == "__main__":
    main()
