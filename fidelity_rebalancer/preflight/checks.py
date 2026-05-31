"""
Pre-flight readiness checks.

These functions auto-verify the environment before a rebalance run:
  * check_ft_running       — is Fidelity Trader+ connectable?
  * check_tickers_present  — are all needed tickers visible in the Watchlist,
                             and all L2-assigned tickers visible in L2 panels?

Both functions use dependency injection: the real adapter is the default, but
it is resolved LAZILY inside the function body so that importing this module
never drags in OCR / pywinauto / rapidocr.  Tests pass trivial fakes/callables.

There is no human-confirmation loop here — these checks only compute and report.
A later CLI decides what to do when something is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from preflight.planner import L2WindowPlan


@dataclass(frozen=True)
class FtRunningResult:
    running: bool
    detail: str  # human-readable; e.g. the RuntimeError message when not running


@dataclass(frozen=True)
class TickerPresenceResult:
    ok: bool
    missing_watchlist: list[str]  # needed-but-absent watchlist tickers, sorted asc
    missing_l2: list[str]  # l2_assigned tickers absent from L2 windows, sorted asc
    present_watchlist: list[str]  # needed tickers found in watchlist, sorted asc
    present_l2: list[str]  # l2_assigned tickers found in L2 windows, sorted asc
    # ALL L2-panel symbols the OCR pass actually detected, normalized + sorted —
    # including panels for tickers we do NOT need (so the CLI can tell the human
    # which open panels are safe to close to free a window slot).  Defaulted so
    # older keyword constructions keep working.
    visible_l2: list[str] = field(default_factory=list)


def _norm(ticker: str) -> str:
    return ticker.strip().upper()


def check_ft_running(connect=None) -> FtRunningResult:
    """
    Verify Fidelity Trader+ is connectable.

    `connect` defaults to adapters._atp_connect.get_app (imported lazily).
    Calls connect(); on success running=True; on RuntimeError running=False
    carrying the error message in `detail`.
    """
    if connect is None:
        from adapters._atp_connect import get_app  # noqa: PLC0415

        connect = get_app

    try:
        connect()
    except RuntimeError as exc:
        return FtRunningResult(running=False, detail=str(exc))
    return FtRunningResult(running=True, detail="Fidelity Trader+ is connectable.")


def check_tickers_present(
    plan: L2WindowPlan,
    *,
    read_watchlist=None,  # defaults to OCRWatchlistAdapter().get_watchlist (lazy)
    enumerate_l2=None,  # defaults to adapters.atp_ocr.enumerate_l2_symbols (lazy)
) -> TickerPresenceResult:
    """
    Compare the plan against what is currently visible in FT+.

      * plan.watchlist   vs the watchlist dict KEYS
      * plan.l2_assigned vs the enumerate_l2() symbol set

    Both sides are normalized (upper/strip) before comparing — OCR casing and
    stray whitespace must not produce false negatives.
    ok = (no missing_watchlist) and (no missing_l2).
    """
    if read_watchlist is None:
        from adapters.atp_watchlist import OCRWatchlistAdapter  # noqa: PLC0415

        read_watchlist = OCRWatchlistAdapter().get_watchlist
    if enumerate_l2 is None:
        from adapters.atp_ocr import enumerate_l2_symbols  # noqa: PLC0415

        enumerate_l2 = enumerate_l2_symbols

    visible_watchlist = {_norm(s) for s in read_watchlist().keys()}
    visible_l2 = {_norm(s) for s in enumerate_l2()}

    needed_watchlist = {_norm(s) for s in plan.watchlist}
    needed_l2 = {_norm(s) for s in plan.l2_assigned}

    missing_watchlist = sorted(needed_watchlist - visible_watchlist)
    missing_l2 = sorted(needed_l2 - visible_l2)
    present_watchlist = sorted(needed_watchlist & visible_watchlist)
    present_l2 = sorted(needed_l2 & visible_l2)

    ok = not missing_watchlist and not missing_l2

    return TickerPresenceResult(
        ok=ok,
        missing_watchlist=missing_watchlist,
        missing_l2=missing_l2,
        present_watchlist=present_watchlist,
        present_l2=present_l2,
        visible_l2=sorted(visible_l2),
    )
