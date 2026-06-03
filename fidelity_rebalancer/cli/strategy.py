"""
Generate sell/buy strategies from engine state JSON.

Fetches market data from one of two sources:
  yfinance  — default; works without Fidelity Trader+ open; uses Yahoo Finance
              for real bid/ask, prev_close, ADV 10D/90D, and div ex-date.
  atp       — reads from Fidelity Trader+ Watchlist via OCR (requires FT+ open
              with Watchlist visible).

Usage (from SecSurfTrade/ or fidelity_rebalancer/):
    python -m fidelity_rebalancer.cli.strategy --state today.json --export today.json
    python -m cli.strategy --state today.json --export today.json
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

import numpy as np

from cli import resolve_output_path, resolve_path
from adapters import Level2Snapshot, WatchlistRow
from adapters.yfinance_fallback import (
    YFinanceWatchlistAdapter,
    adv_to_vol5min,
    watchlist_row_to_quote,
)
from engine.chunker import _DAILY_SIGMA_BPS, build_chunks_pov, vol_profile_multiplier
from engine.decision_context import DecisionContext
from engine.spread_context import spread_context_for
from engine.strategy_buy import generate_buy_strategy
from engine.strategy_sell import generate_sell_strategy
from state.importer import load_state, save_state
from state.schema import ChunkRecord

# Strict-ATP contract: when --strict-atp is set and live FT+ OCR is incomplete
# (any watchlist ticker missing OR any L2 fetch failing), the CLI stops instead
# of silently falling back to yfinance / empty books. The orchestrator keys off
# this exact exit code and stderr marker to pause for human confirmation.
OCR_SHORTFALL_EXIT = 3
OCR_SHORTFALL_MARKER = "OCR_SHORTFALL"


class OCRShortfall(Exception):
    """Raised in strict-atp mode when live FT+ OCR data is incomplete."""


def _realized_vol_bps(symbol: str, lookback: int = 20) -> float:
    """
    Daily realized volatility in bps from yfinance daily closes.
    e.g. 100 bps = 1% daily vol.  Matches _DAILY_SIGMA_BPS units expected by
    estimate_impact_bps (square-root law uses daily sigma, not annualized).
    Returns _DAILY_SIGMA_BPS (100) as fallback if data is unavailable.
    """
    try:
        import yfinance as yf
    except ImportError:
        return _DAILY_SIGMA_BPS
    try:
        hist = yf.Ticker(symbol).history(period=f"{lookback + 5}d")
        if hist is None or len(hist) < 5:
            return _DAILY_SIGMA_BPS
        closes = hist["Close"].dropna().values
        if len(closes) < 5:
            return _DAILY_SIGMA_BPS
        log_returns = np.diff(np.log(closes))
        daily_std = float(np.std(log_returns, ddof=1))
        return daily_std * 10_000  # daily bps; do NOT annualise (sqrt(252) removed)
    except Exception:
        return _DAILY_SIGMA_BPS


def _fetch_watchlist(
    symbols: list[str], source: str, strict: bool = False
) -> dict[str, WatchlistRow]:
    """Return WatchlistRow for each symbol from the selected source.

    In strict-atp mode, any watchlist ticker missing from the OCR read raises
    OCRShortfall instead of silently falling back to yfinance.
    """
    if source == "atp":
        from adapters.atp_watchlist import ATPWatchlistAdapter

        print("Fetching market data from Fidelity Trader+ Watchlist (OCR)...")
        rows = ATPWatchlistAdapter().get_watchlist()
        missing = [s for s in symbols if s not in rows]
        if missing:
            if strict:
                raise OCRShortfall(f"watchlist_missing={missing}")
            print(
                f"  Warning: {missing} not found in Watchlist OCR — "
                "falling back to yfinance for missing tickers",
                file=sys.stderr,
            )
            yf_rows = YFinanceWatchlistAdapter().get_watchlist(missing)
            rows.update(yf_rows)
        return rows

    # Default: yfinance
    print(f"Fetching market data from yfinance for {len(symbols)} ticker(s)...")
    return YFinanceWatchlistAdapter().get_watchlist(symbols)


def _empty_l2(symbol: str) -> Level2Snapshot:
    return Level2Snapshot(
        symbol=symbol,
        bids=[],
        asks=[],
        ts=datetime.now(tz=timezone.utc),
    )


def _chunk_slug(account: str) -> str:
    return account.replace(" ", "_").replace("-", "").replace("__", "_")


def _spread_bps(quote) -> float:
    mid = (quote.bid + quote.ask) / 2.0 if (quote.bid and quote.ask) else 0.0
    if mid <= 0:
        return 0.0
    return max(0.0, (quote.ask - quote.bid) / mid * 10000.0)


_THIN_TICKER_PCT = 3.0  # order > this % of ADV → recommend L2


def _detect_thin_tickers(
    sells: list,
    buys: list,
    watchlist: dict[str, WatchlistRow],
) -> list[tuple[str, str, float]]:
    """Return [(ticker, side, pct_of_adv)] for orders exceeding _THIN_TICKER_PCT of ADV."""
    thin: list[tuple[str, str, float]] = []
    for sell in sells:
        row = watchlist.get(sell.ticker)
        if row and row.avg_vol_10d > 0:
            pct = sell.shares / row.avg_vol_10d * 100.0
            if pct > _THIN_TICKER_PCT:
                thin.append((sell.ticker, "SELL", pct))
    for buy in buys:
        row = watchlist.get(buy.ticker)
        if row and row.avg_vol_10d > 0 and buy.share_target > 0:
            pct = buy.share_target / row.avg_vol_10d * 100.0
            if pct > _THIN_TICKER_PCT:
                thin.append((buy.ticker, "BUY", pct))
    return sorted(thin, key=lambda t: t[2], reverse=True)


# Max number of tickers to fetch L2 for in auto-detect mode. ATP shows a
# limited number of L2 panels at once (~7 fit on the right-hand side), so we
# spend that budget on the highest-impact orders.
_L2_PANEL_CAP = 7


def _rank_l2_candidates(
    sells: list,
    buys: list,
    watchlist: dict[str, WatchlistRow],
) -> list[tuple[str, float]]:
    """Rank every order ticker by its largest order size as % of 10-day ADV.

    Returns [(ticker, pct_of_adv)] sorted high → low. This is the priority for
    L2 attention: the bigger an order is relative to daily volume, the more its
    fill quality depends on real book depth (book-relative chunking) rather than
    the ADV-only POV estimate. Tickers with no ADV get pct 0.0 (lowest priority).
    """
    pct_by_tkr: dict[str, float] = {}

    def _consider(ticker: str, qty: float) -> None:
        row = watchlist.get(ticker)
        pct = 0.0
        if row and getattr(row, "avg_vol_10d", 0) and qty > 0:
            pct = qty / row.avg_vol_10d * 100.0
        if pct > pct_by_tkr.get(ticker, -1.0):
            pct_by_tkr[ticker] = pct

    for s in sells:
        _consider(s.ticker, float(s.shares))
    for b in buys:
        _consider(b.ticker, float(b.share_target))
    return sorted(pct_by_tkr.items(), key=lambda kv: kv[1], reverse=True)


def _select_l2_symbols(
    ranked_syms: list[str],
    open_panels: set[str],
    cap: int = _L2_PANEL_CAP,
) -> tuple[list[str], list[str]]:
    """Decide which tickers to fetch L2 for, and which panels to recommend opening.

    `ranked_syms` is the ticker priority (high → low, from _rank_l2_candidates).
    `open_panels` is the set of L2 panels currently open in ATP.

    Returns (use, recommend_open):
      use            — the up-to-`cap` highest-priority tickers whose panel is
                       OPEN (only these can be fetched without an OCR failure).
      recommend_open — the highest-priority tickers (within the top-`cap` slots)
                       whose panel is NOT open; the human should open these for
                       better chunking, then re-run.
    """
    priority = ranked_syms[:cap]
    open_up = {s.upper() for s in open_panels}
    use = [t for t in priority if t.upper() in open_up]
    recommend_open = [t for t in priority if t.upper() not in open_up]
    return use, recommend_open


def _rechunk_sell_pov(
    sell, strat, *, adv, spread_bps, sigma_bps=_DAILY_SIGMA_BPS
) -> tuple[list[ChunkRecord], dict]:
    chunk_dicts, info = build_chunks_pov(
        total_shares=float(sell.shares),
        limit_price=strat.limit_price,
        adv=adv,
        spread_bps=spread_bps,
        side="sell",
        sigma_bps=sigma_bps,
    )
    chunks = []
    chunk_ids = []
    for cd in chunk_dicts:
        cid = f"s_{_chunk_slug(sell.account)}_{sell.ticker}_{cd['idx']}"
        chunk_ids.append(cid)
        chunks.append(
            ChunkRecord(
                chunk_id=cid,
                account=sell.account,
                strategy=sell.strategy,
                ticker=sell.ticker,
                idx=cd["idx"],
                shares=cd["shares"],
                limit_price=cd["limit_price"],
                cost=cd["cost"],
            )
        )
    strat.chunk_ids = chunk_ids
    return chunks, info


def _rechunk_buy_pov(
    buy, strat, *, adv, spread_bps, sigma_bps=_DAILY_SIGMA_BPS
) -> tuple[list[ChunkRecord], dict]:
    if strat.limit_price <= 0:
        return [], {
            "tier": 0,
            "tier_label": "unknown_adv",
            "n_chunks": 0,
            "pov_pct": None,
            "est_impact_bps": 0.0,
        }
    target_shares = float(math.floor(buy.dollar_target / strat.limit_price))
    chunk_dicts, info = build_chunks_pov(
        total_shares=target_shares,
        limit_price=strat.limit_price,
        adv=adv,
        spread_bps=spread_bps,
        side="buy",
        sigma_bps=sigma_bps,
    )
    chunks = []
    chunk_ids = []
    for cd in chunk_dicts:
        cid = f"b_{_chunk_slug(buy.account)}_{buy.ticker}_{cd['idx']}"
        chunk_ids.append(cid)
        chunks.append(
            ChunkRecord(
                chunk_id=cid,
                account=buy.account,
                strategy=buy.strategy,
                ticker=buy.ticker,
                idx=cd["idx"],
                shares=cd["shares"],
                limit_price=cd["limit_price"],
                cost=cd["cost"],
            )
        )
    strat.chunk_ids = chunk_ids
    return chunks, info


def _pov_bullets(info: dict) -> list[str]:
    """Reasoning bullets describing the POV decision — appended to strategy.reasoning."""
    label = info.get("tier_label", "unknown_adv")
    if label == "unknown_adv":
        return [f"POV: ADV unknown — split into {info.get('n_chunks', 0)} chunk(s)."]
    pov_pct = info.get("pov_pct") or 0.0
    impact = info.get("est_impact_bps") or 0.0
    n = info.get("n_chunks", 0)
    desc = {
        "invisible": "<1% ADV + tight spread — single order is invisible to the market.",
        "standard": "1-5% ADV — standard slicing to stay near 5% POV.",
        "aggressive": "5-10% ADV — aggressive slicing to limit impact and signaling.",
        "market_moving": ">10% ADV — market-moving size, slicing aggressively (consider multi-day).",
    }.get(label, label)
    return [
        f"POV: {pov_pct:.2f}% of ADV ({label}). {desc}",
        f"Estimated market impact: {impact:.1f} bps (square-root law). "
        f"Split into {n} chunk(s) with ±15% jitter.",
    ]


def _reorder_chunks_largest_first(
    chunks: list[ChunkRecord],
    skip_tickers: set[str] | None = None,
) -> list[ChunkRecord]:
    """Sort chunks within each (account, ticker) group by shares descending, re-index."""
    from itertools import groupby

    skip = skip_tickers or set()
    key_fn = lambda c: (c.account, c.ticker)
    groups = {k: list(g) for k, g in groupby(sorted(chunks, key=key_fn), key=key_fn)}
    result: list[ChunkRecord] = []
    for (acct, tkr), grp in groups.items():
        if tkr in skip:
            result.extend(grp)
            continue
        grp.sort(key=lambda c: c.shares, reverse=True)
        for new_idx, c in enumerate(grp):
            c.idx = new_idx
        result.extend(grp)
    return result


def _load_confirmed_proceeds(raw: str) -> dict[str, float]:
    """Parse --confirmed-proceeds value (JSON string or file path)."""
    import json

    path = Path(raw)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(raw)


def _adjust_buy_budgets(state, confirmed: dict[str, float]) -> None:
    """Scale buy dollar_target per account based on actual vs estimated proceeds."""
    est_by_account: dict[str, float] = {}
    for sell in state.computed.sells:
        est_by_account[sell.account] = (
            est_by_account.get(sell.account, 0.0) + sell.est_proceeds
        )

    for acct, actual in confirmed.items():
        est = est_by_account.get(acct, 0.0)
        if est <= 0:
            continue
        ratio = actual / est
        adjusted = 0
        for buy in state.computed.buy_allocations:
            if buy.account != acct:
                continue
            old_target = buy.dollar_target
            buy.dollar_target = round(old_target * ratio, 2)
            if buy.limit_price > 0:
                buy.share_target = int(math.floor(buy.dollar_target / buy.limit_price))
                buy.est_cost = round(buy.share_target * buy.limit_price, 2)
            adjusted += 1
        _log.debug(
            "Confirmed proceeds for %s: $%,.2f (est $%,.2f, ratio %.4f) — %d buy(s) adjusted",
            acct,
            actual,
            est,
            ratio,
            adjusted,
        )


def _reconcile_records_to_chunks(state) -> None:
    """Make each buy/sell record reflect the sum of its (re-priced) chunks.

    cli.compute sizes records at prev-close; cli.strategy then regenerates the
    chunks at live ATP prices (and floors sells to whole shares).  That leaves
    the record share/limit/cost out of step with the chunks — a guaranteed
    CHUNK_SUM_MISMATCH whenever the live price != prev close, and a misleading
    record even when it isn't.  Python is the source of truth and the chunks
    are what actually get entered, so the record is reconciled DOWN to its
    chunks here.

    A record with a non-zero target but *no* chunks is left untouched, so the
    sanity gate still flags that genuinely-broken case.
    """
    from collections import defaultdict

    def _agg(chunks):
        shares: dict = defaultdict(float)
        cost: dict = defaultdict(float)
        limit: dict = {}
        for c in chunks:
            k = (c.account, c.strategy, c.ticker)
            shares[k] += c.shares
            cost[k] += c.cost
            limit.setdefault(k, c.limit_price)  # chunks share one limit per ticker
        return shares, cost, limit

    s_shares, s_cost, s_limit = _agg(state.computed.sell_chunks)
    for s in state.computed.sells:
        k = (s.account, s.strategy, s.ticker)
        if k in s_shares:
            s.shares = round(s_shares[k], 6)
            s.limit_price = s_limit[k]
            s.est_proceeds = round(s_cost[k], 2)

    b_shares, b_cost, b_limit = _agg(state.computed.buy_chunks)
    for b in state.computed.buy_allocations:
        k = (b.account, b.strategy, b.ticker)
        if k in b_shares:
            b.share_target = int(round(b_shares[k]))
            b.limit_price = b_limit[k]
            b.est_cost = round(b_cost[k], 2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate strategies from engine state JSON (adds to state in-place)"
    )
    parser.add_argument(
        "--state", required=True, help="Engine state JSON from cli.compute"
    )
    parser.add_argument(
        "--export", required=True, help="Output path (may be same as --state)"
    )
    parser.add_argument(
        "--source",
        choices=("yfinance", "atp"),
        default="yfinance",
        help="Market data source: yfinance (default) or atp (Fidelity Trader+ Watchlist OCR)",
    )
    parser.add_argument(
        "--l2-symbols",
        nargs="*",
        default=None,
        metavar="SYM",
        help="Symbols to fetch Level 2 depth via OCR (requires ATP open with L2 panels). "
        f"Pass without arguments to auto-detect: use the L2 panels currently open "
        f"in ATP, spent on the highest-impact orders (up to {_L2_PANEL_CAP}, ranked by %%ADV).",
    )
    parser.add_argument(
        "--confirmed-proceeds",
        default=None,
        metavar="JSON",
        help="Actual sell proceeds per account (JSON string or file path). "
        "e.g. '{\"Account Name\": 12345.67}'. "
        "Adjusts buy budgets proportionally vs estimated proceeds.",
    )
    parser.add_argument(
        "--strict-atp",
        action="store_true",
        help="With --source atp: stop (exit 3) instead of silently falling back "
        "if any watchlist ticker is missing OR any L2 fetch fails. Used by the "
        "morning preflight to pause for human confirmation before fallback.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show per-trade detail lines (ticker, rule, limit price, chunk count). "
        "Without this flag these are suppressed to avoid capturing sensitive data in logs.",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG, format="%(message)s", stream=sys.stderr
        )

    state = load_state(resolve_path(args.state))

    # Sell-before-buy: adjust buy budgets from actual proceeds
    if args.confirmed_proceeds:
        confirmed = _load_confirmed_proceeds(args.confirmed_proceeds)
        _adjust_buy_budgets(state, confirmed)

    # Collect all unique tickers across sells and buys
    all_tickers = sorted(
        {
            *(sell.ticker for sell in state.computed.sells),
            *(buy.ticker for buy in state.computed.buy_allocations),
        }
    )

    # Batch-fetch market data once for all tickers
    try:
        watchlist = _fetch_watchlist(all_tickers, args.source, strict=args.strict_atp)
    except OCRShortfall as exc:
        print(f"{OCR_SHORTFALL_MARKER}: {exc}", file=sys.stderr)
        sys.exit(OCR_SHORTFALL_EXIT)

    # Per-symbol realized volatility for impact model
    print(f"Computing realized volatility for {len(all_tickers)} ticker(s)...")
    vol_map: dict[str, float] = {}
    for sym in all_tickers:
        sigma = _realized_vol_bps(sym)
        vol_map[sym] = sigma
        label = (
            f"{sigma:.0f} bps"
            if sigma != _DAILY_SIGMA_BPS
            else f"{sigma:.0f} bps (default)"
        )
        print(f"  {sym:6s}  sigma={label}")

    # Thin-ticker detection
    thin = _detect_thin_tickers(
        state.computed.sells, state.computed.buy_allocations, watchlist
    )
    if thin:
        print("Thin-ticker detection (order > 3% ADV):")
        for sym, side, pct in thin:
            print(f"  ⚠ {side:4s} {sym:6s}  {pct:.1f}% of ADV — open L2 window in ATP")
    thin_syms = {t[0] for t in thin}

    # Level 2 depth data: fetch via OCR.
    #   --l2-symbols SYM ...  -> exactly those tickers (explicit; user's call).
    #   --l2-symbols          -> AUTO-DETECT: use the L2 panels currently open in
    #                            ATP, spent on the highest-impact orders (cap 7).
    #                            Panels that aren't open are skipped (so we never
    #                            trip the strict-ATP OCR-failure abort on a closed
    #                            panel) but recommended to the human to open.
    l2_cache: dict[str, Level2Snapshot] = {}
    l2_symbols: set[str] = set()
    if args.l2_symbols is not None:
        if args.l2_symbols:
            l2_symbols = {s.upper() for s in args.l2_symbols}
        else:
            from adapters.atp_ocr import enumerate_l2_symbols

            open_panels = {s.upper() for s in enumerate_l2_symbols()}
            ranked = _rank_l2_candidates(
                state.computed.sells, state.computed.buy_allocations, watchlist
            )
            use, recommend_open = _select_l2_symbols(
                [t for t, _ in ranked], open_panels, cap=_L2_PANEL_CAP
            )
            l2_symbols = {s.upper() for s in use}
            print(
                "Auto-detect L2: open panels = "
                + (", ".join(sorted(open_panels)) if open_panels else "none")
            )
            print(
                f"  Using L2 for (top {_L2_PANEL_CAP} by %ADV, panel open): "
                + (", ".join(use) if use else "none")
            )
            if recommend_open:
                print(
                    "  ⚠ Higher-impact tickers WITHOUT an open L2 panel "
                    "(open these in ATP + re-run for better chunking; "
                    "they fall back to POV sizing): " + ", ".join(recommend_open)
                )
    if l2_symbols:
        from adapters.atp_ocr import OCRLevel2Adapter

        l2_adapter = OCRLevel2Adapter()
        print(f"Fetching Level 2 depth for {sorted(l2_symbols)}...")
        l2_failed: list[str] = []
        for sym in sorted(l2_symbols):
            try:
                l2_cache[sym] = l2_adapter.get_level2(sym)
                n_bids = len(l2_cache[sym].bids)
                n_asks = len(l2_cache[sym].asks)
                print(f"  {sym:6s}  L2 OK ({n_bids} bids, {n_asks} asks)")
            except Exception as exc:
                print(f"  {sym:6s}  L2 FAILED: {exc}", file=sys.stderr)
                l2_failed.append(sym)
        if args.strict_atp and l2_failed:
            print(f"{OCR_SHORTFALL_MARKER}: l2_failed={l2_failed}", file=sys.stderr)
            sys.exit(OCR_SHORTFALL_EXIT)

    def _get_l2(ticker: str) -> Level2Snapshot:
        return l2_cache.get(ticker) or _empty_l2(ticker)

    def _get_row(ticker: str) -> WatchlistRow | None:
        row = watchlist.get(ticker)
        if row is None:
            print(
                f"  Warning: no market data for {ticker} — using fallback zeros",
                file=sys.stderr,
            )
        return row

    now = datetime.now()
    vol_mult = vol_profile_multiplier(now.hour, now.minute)
    # Minutes since market open (9:30 ET) — used for gap capture rule
    mkt_minutes = (now.hour - 9) * 60 + (now.minute - 30)
    if mkt_minutes < 0 or mkt_minutes > 390:
        mkt_minutes = None  # outside market hours
    print(
        f"Volume profile multiplier: {vol_mult:.1f}x (market time {now.strftime('%H:%M')}"
        f", {mkt_minutes} min since open)"
        if mkt_minutes is not None
        else f"Volume profile multiplier: {vol_mult:.1f}x (outside market hours)"
    )

    print("Generating sell strategies...")
    sell_strategies = []
    all_sell_chunks = []
    for sell in state.computed.sells:
        row = _get_row(sell.ticker)
        if row is not None:
            quote = watchlist_row_to_quote(row)
            vol5min = adv_to_vol5min(row.avg_vol_10d) * vol_mult
            pc_label = (
                f"prev_close=${row.prev_close:.4f}"
                if row.prev_close
                else "prev_close=N/A"
            )
            adv_label = f"adv10={row.avg_vol_10d:,}"
        else:
            # Hard fallback: use prev_close from signals.json, zero spread
            prev = state.inputs.prev_closes.get(sell.ticker, 0.0)
            from adapters import QuoteSnapshot

            quote = QuoteSnapshot(
                symbol=sell.ticker,
                bid=prev,
                bid_size=0,
                ask=prev,
                ask_size=0,
                last=prev,
                prev_close=prev,
                volume=0,
                ts=datetime.now(tz=timezone.utc),
            )
            vol5min = 0.0
            pc_label = f"prev_close=${prev:.4f} (signals.json)"
            adv_label = "adv10=0"

        l2 = _get_l2(sell.ticker)
        adv_val = float(row.avg_vol_10d) if row is not None else None
        sc = spread_context_for(sell.ticker, quote.bid, quote.ask)
        vwap_val = float(row.vwap) if (row is not None and row.vwap) else None
        strat, chunks = generate_sell_strategy(
            sell,
            quote,
            l2,
            vol5min=vol5min,
            ctx=DecisionContext(
                market_minutes=mkt_minutes,
                spread_ctx=sc,
                vwap=vwap_val,
                adv=adv_val,
            ),
        )
        if not l2.bids:
            chunks, pov_info = _rechunk_sell_pov(
                sell,
                strat,
                adv=adv_val,
                spread_bps=_spread_bps(quote),
                sigma_bps=vol_map.get(sell.ticker, _DAILY_SIGMA_BPS),
            )
            strat.reasoning.extend(_pov_bullets(pov_info))
        else:
            pov_info = {
                "tier_label": "book_relative",
                "tier": 0,
                "n_chunks": len(chunks),
                "est_impact_bps": 0.0,
            }
        sell_strategies.append(strat)
        all_sell_chunks.extend(chunks)
        if pov_info.get("tier") == 4:
            print(
                f"  ⚠ SELL {sell.ticker}: market-moving order "
                f"({pov_info['pov_pct']:.1f}% of ADV) — consider splitting across days",
                file=sys.stderr,
            )
        _log.debug(
            "  SELL %-6s  rule=%-28s  limit=$%.4f  %d chunk(s)  pov=%-14s  %s  %s",
            sell.ticker,
            strat.rule,
            strat.limit_price,
            len(chunks),
            pov_info.get("tier_label", "?"),
            pc_label,
            adv_label,
        )

    print("Generating buy strategies...")
    buy_strategies = []
    all_buy_chunks = []
    for buy in state.computed.buy_allocations:
        row = _get_row(buy.ticker)
        if row is not None:
            quote = watchlist_row_to_quote(row)
            vol5min = adv_to_vol5min(row.avg_vol_10d) * vol_mult
            pc_label = (
                f"prev_close=${row.prev_close:.4f}"
                if row.prev_close
                else "prev_close=N/A"
            )
            adv_label = f"adv10={row.avg_vol_10d:,}"
        else:
            prev = state.inputs.prev_closes.get(buy.ticker, 0.0)
            from adapters import QuoteSnapshot

            quote = QuoteSnapshot(
                symbol=buy.ticker,
                bid=prev,
                bid_size=0,
                ask=prev,
                ask_size=0,
                last=prev,
                prev_close=prev,
                volume=0,
                ts=datetime.now(tz=timezone.utc),
            )
            vol5min = 0.0
            pc_label = f"prev_close=${prev:.4f} (signals.json)"
            adv_label = "adv10=0"

        l2 = _get_l2(buy.ticker)
        adv_val = float(row.avg_vol_10d) if row is not None else None
        sc = spread_context_for(buy.ticker, quote.bid, quote.ask)
        vwap_val = float(row.vwap) if (row is not None and row.vwap) else None
        strat, chunks = generate_buy_strategy(
            buy,
            quote,
            l2,
            vol5min=vol5min,
            ctx=DecisionContext(
                market_minutes=mkt_minutes,
                spread_ctx=sc,
                vwap=vwap_val,
                adv=adv_val,
            ),
        )
        if not l2.asks:
            chunks, pov_info = _rechunk_buy_pov(
                buy,
                strat,
                adv=adv_val,
                spread_bps=_spread_bps(quote),
                sigma_bps=vol_map.get(buy.ticker, _DAILY_SIGMA_BPS),
            )
            strat.reasoning.extend(_pov_bullets(pov_info))
        else:
            pov_info = {
                "tier_label": "book_relative",
                "tier": 0,
                "n_chunks": len(chunks),
                "est_impact_bps": 0.0,
            }
        buy_strategies.append(strat)
        all_buy_chunks.extend(chunks)
        if pov_info.get("tier") == 4:
            print(
                f"  ⚠ BUY  {buy.ticker}: market-moving order "
                f"({pov_info['pov_pct']:.1f}% of ADV) — consider splitting across days",
                file=sys.stderr,
            )
        _log.debug(
            "  BUY  %-6s  rule=%-28s  limit=$%.4f  %d chunk(s)  pov=%-14s  %s  %s",
            buy.ticker,
            strat.rule,
            strat.limit_price,
            len(chunks),
            pov_info.get("tier_label", "?"),
            pc_label,
            adv_label,
        )

    # Per-ticker chunk ordering: largest chunk first (except gap_capture which has phase order)
    gap_tickers = {s.ticker for s in sell_strategies if s.rule == "gap_capture"}
    all_sell_chunks = _reorder_chunks_largest_first(
        all_sell_chunks, skip_tickers=gap_tickers
    )
    all_buy_chunks = _reorder_chunks_largest_first(all_buy_chunks)

    state.computed.sell_strategies = sell_strategies
    state.computed.buy_strategies = buy_strategies
    state.computed.sell_chunks = all_sell_chunks
    state.computed.buy_chunks = all_buy_chunks

    # Records were sized at prev-close by cli.compute; chunks above are re-priced
    # at live ATP (and sells floored to whole shares).  Reconcile the records DOWN
    # to their chunks so Python stays internally consistent — the chunks are what
    # actually get entered, so they are the source of truth.
    _reconcile_records_to_chunks(state)

    save_state(state, resolve_output_path(args.export))
    print(
        f"Wrote {args.export}  ({len(sell_strategies)} sell, {len(buy_strategies)} buy strategies)"
    )


if __name__ == "__main__":
    main()
