"""
Manual smoke test against a live ATP instance.

Usage:
    python scripts/atp_smoke.py SPY
    python scripts/atp_smoke.py JMAC
    python scripts/atp_smoke.py --debug-tree SPY    # dumps full UIA tree

Pre-requisites (human must do before running):
  1. ATP launched and logged in
  2. Quote window open for the test ticker
  3. Level II window open for the test ticker
  4. Orders panel open (Ctrl+3 or View -> Orders)
  5. All panels visible (not minimized, not occluded)

This script is NOT part of the pytest suite -- run it manually.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# Allow running from the project root or from the scripts/ dir
_PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT))


# -- Formatting helpers -------------------------------------------------------

def _fmt_vol(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_ts(dt: datetime) -> str:
    try:
        local = dt.astimezone()
        return local.strftime("%Y-%m-%d %H:%M:%S%z")
    except Exception:
        return str(dt)


# -- Debug tree dump ----------------------------------------------------------

def _dump_ctrl(ctrl, buf, indent: int = 0, max_depth: int = 8) -> None:
    if indent > max_depth:
        return
    prefix = "  " * indent
    try:
        ctrl_type = ctrl.element_info.control_type or "?"
    except Exception:
        ctrl_type = "?"
    try:
        title = ctrl.window_text() or ""
    except Exception:
        title = ""
    try:
        auto_id = ctrl.element_info.automation_id or ""
    except Exception:
        auto_id = ""
    try:
        class_name = ctrl.element_info.class_name or ""
    except Exception:
        class_name = ""
    parts = [f"{prefix}[{ctrl_type}]"]
    if title:
        parts.append(f"title={title!r}")
    if auto_id:
        parts.append(f"id={auto_id!r}")
    if class_name:
        parts.append(f"class={class_name!r}")
    print(" ".join(parts), file=buf)
    try:
        for child in ctrl.children():
            _dump_ctrl(child, buf, indent + 1, max_depth)
    except Exception:
        pass


def dump_tree(symbol: str, out_path: Path, app=None) -> None:
    import io
    if app is None:
        from adapters._atp_connect import get_app
        app = get_app()

    buf = io.StringIO()
    print(f"=== UIA Tree for '{symbol}' -- {datetime.now()} ===\n", file=buf)

    for win in app.windows():
        title = win.window_text() or "<no title>"
        print(f"[Window] {title!r}", file=buf)
        _dump_ctrl(win, buf, indent=1, max_depth=12)
        print(file=buf)

    # Descendants search -- finds controls that children() traversal misses
    # (e.g. virtualized items, off-screen panels, GPU-composited layers)
    print("=== Descendants search (control types that expose data) ===\n", file=buf)
    try:
        win = app.top_window()
        for ct in ["Table", "DataGrid", "DataItem", "ListItem", "TreeItem",
                   "Custom", "Pane", "Edit", "Text"]:
            try:
                found = win.descendants(control_type=ct)
                if not found:
                    print(f"{ct:12}: 0", file=buf)
                    continue
                print(f"{ct:12}: {len(found)}", file=buf)
                for ctrl in found[:5]:
                    try:
                        t = ctrl.window_text().strip()
                        cname = ctrl.element_info.class_name or ""
                        aid = ctrl.element_info.automation_id or ""
                        print(f"  title={t!r:30} class={cname!r:30} id={aid!r}", file=buf)
                    except Exception:
                        pass
                if len(found) > 5:
                    print(f"  ... and {len(found) - 5} more", file=buf)
            except Exception as e:
                print(f"{ct:12}: error -- {e}", file=buf)
    except Exception as e:
        print(f"Descendants search failed: {e}", file=buf)

    out_path.write_text(buf.getvalue(), encoding="utf-8")
    print(f"UIA tree written to: {out_path}")


# -- Quote section ------------------------------------------------------------

def show_quote(symbol: str) -> bool:
    from adapters.atp_quote import ATPQuoteAdapter
    adapter = ATPQuoteAdapter()
    try:
        q = adapter.get_quote(symbol)
    except LookupError as exc:
        print(f"\n[QUOTE] {exc}")
        print("  Hint: open a Quote window for this symbol in ATP.")
        return False
    except Exception as exc:
        print(f"\n[QUOTE] Error: {exc}")
        return False

    spread = q.ask - q.bid
    print(f"\nQuote {q.symbol} @ {_fmt_ts(q.ts)}")
    print(f"  Bid: {q.bid:.4f} x {q.bid_size:,}    Ask: {q.ask:.4f} x {q.ask_size:,}    Last: {q.last:.4f}")
    print(f"  PrevClose: {q.prev_close:.4f}    Spread: {spread:.4f}    Volume: {_fmt_vol(q.volume)}")
    return True


# -- Level II section ---------------------------------------------------------

def show_level2(symbol: str, n_levels: int = 5) -> bool:
    from adapters.atp_level2 import ATPLevel2Adapter

    snap = None
    # Try UIA first (fast, free); fall back to OCR
    try:
        snap = ATPLevel2Adapter().get_level2(symbol)
    except LookupError:
        pass
    except Exception as exc:
        print(f"\n[LEVEL II] UIA error: {exc}")

    if snap is None:
        print(f"\n[LEVEL II] UIA not available -- trying OCR...")
        try:
            from adapters.atp_ocr import OCRLevel2Adapter
            snap = OCRLevel2Adapter().get_level2(symbol)
        except Exception as exc:
            print(f"[LEVEL II] OCR error: {exc}")
            return False

    bids = snap.bids[:n_levels]
    asks = snap.asks[:n_levels]
    rows = max(len(bids), len(asks))

    print(f"\nLevel II {snap.symbol} (top {n_levels})")
    print(f"  {'BID':^30}  {'ASK':^30}")
    print(f"  {'Price':>8}  {'Size':>6}  {'MPID':<6}    {'Price':>8}  {'Size':>6}  {'MPID':<6}")
    print(f"  {'-'*30}  {'-'*30}")
    for i in range(rows):
        b_str = f"{bids[i].price:>8.4f}  {bids[i].size:>6,}  {bids[i].mpid:<6}" if i < len(bids) else " " * 24
        a_str = f"{asks[i].price:>8.4f}  {asks[i].size:>6,}  {asks[i].mpid:<6}" if i < len(asks) else ""
        print(f"  {b_str}    {a_str}")
    return True


# -- Orders section -----------------------------------------------------------

def show_orders() -> bool:
    from adapters.atp_orders import ATPOrdersAdapter
    adapter = ATPOrdersAdapter()

    rows = None
    try:
        rows = adapter.get_orders()
    except LookupError:
        pass
    except Exception as exc:
        print(f"\n[ORDERS] UIA error: {exc}")

    if rows is None:
        print(f"\n[ORDERS] UIA not available -- trying OCR...")
        try:
            from adapters.atp_ocr import OCROrdersAdapter
            rows = OCROrdersAdapter().get_orders()
        except Exception as exc:
            print(f"[ORDERS] OCR error: {exc}")
            return False

    print(f"\nOrders panel ({len(rows)} row{'s' if len(rows) != 1 else ''})")
    if not rows:
        print("  (no orders)")
        return True

    for r in rows:
        fill_info = ""
        if r.filled_qty > 0:
            fill_info = f" ({r.filled_qty:,.0f} filled)"
        age = (datetime.now(r.last_update_at.tzinfo) - r.last_update_at).total_seconds()
        age_str = f"{int(age)}s ago"
        print(
            f"  {r.account:<18} {r.symbol:<6} {r.side:<5} "
            f"{r.qty:>8,.0f} @ {r.limit_price:.4f}  "
            f"{r.status.value}{fill_info}  [{age_str}]"
        )
    return True


# -- Watchlist section --------------------------------------------------------

def show_watchlist(app_name: str) -> bool:
    """
    Read Watchlist data from the selected app.
    app_name: "trader_plus" | "active_trader_pro"
    """
    rows: dict | None = None

    if app_name == "trader_plus":
        print("\n[WATCHLIST] Trying UIA (Fidelity Trader+)...")
        try:
            from adapters.atp_watchlist import UIAWatchlistAdapter
            rows = UIAWatchlistAdapter().get_watchlist()
        except LookupError as exc:
            print(f"  UIA: {exc}")
            print("[WATCHLIST] Falling back to OCR...")
            try:
                from adapters.atp_watchlist import OCRWatchlistAdapter
                rows = OCRWatchlistAdapter().get_watchlist()
            except Exception as exc2:
                print(f"[WATCHLIST] OCR error: {exc2}")
                if exc2.__cause__:
                    print(f"  Cause: {exc2.__cause__}")
                return False

    elif app_name == "active_trader_pro":
        print("\n[WATCHLIST] Trying UIA (Fidelity Active Trader Pro + JAB)...")
        try:
            from adapters.fatp_watchlist import FATWatchlistAdapter
            rows = FATWatchlistAdapter().get_watchlist()
        except LookupError as exc:
            print(f"  UIA: {exc}")
            return False
        except Exception as exc:
            print(f"[WATCHLIST] Error: {exc}")
            return False

    if not rows:
        print("[WATCHLIST] No rows returned.")
        return False

    print(f"\nWatchlist ({len(rows)} ticker{'s' if len(rows) != 1 else ''})")
    hdr = (f"  {'Symbol':<8} {'Last':>8} {'Bid':>8} {'Ask':>8}  "
           f"{'PrevClose':>10}  {'10D AvgVol':>12}  {'90D AvgVol':>12}  {'DivEx':<12}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for sym, r in sorted(rows.items()):
        adv10 = f"{r.avg_vol_10d / 1_000:.0f}K" if r.avg_vol_10d >= 1000 else str(r.avg_vol_10d)
        adv90 = f"{r.avg_vol_90d / 1_000:.0f}K" if r.avg_vol_90d >= 1000 else str(r.avg_vol_90d)
        print(
            f"  {sym:<8} {r.last:>8.4f} {r.bid:>8.4f} {r.ask:>8.4f}  "
            f"{r.prev_close:>10.4f}  {adv10:>12}  {adv90:>12}  {r.div_ex_date or '--':<12}"
        )
    return True


# -- Comparison table ---------------------------------------------------------

def show_comparison_table() -> None:
    """Print a side-by-side comparison of Fidelity Trader+ vs Active Trader Pro."""
    print("\n")
    print("=" * 86)
    print("  DATA RETRIEVAL METHOD COMPARISON")
    print("  Fidelity Trader+ (FT+)  vs  Fidelity Active Trader Pro (ATP)")
    print("=" * 86)

    rows = [
        # (Data field, FT+ method, FT+ status, ATP method, ATP status, Notes)
        (
            "Quote bid/ask/last",
            "OCR (L2 panel header)", "CONFIRMED -- L2 panel header has bid/ask/last",
            "OCR (PrintWindow)",     "POSSIBLE -- same PrintWindow approach would work",
            "FT+ UIA Quote panel partial; L2 header OCR more reliable for all tickers",
        ),
        (
            "Prev close (Close)",
            "OCR (Watchlist)",     "CONFIRMED -- all tickers at once",
            "OCR (PrintWindow)",   "POSSIBLE -- same approach; not yet implemented",
            "Both apps: content panels are DirectX/GPU rendered, UIA-opaque",
        ),
        (
            "L2 depth (bid/ask book)",
            "OCR (L2 panel)",      "CONFIRMED -- 1 L2 panel open at a time",
            "OCR (PrintWindow)",   "POSSIBLE -- same approach; not yet implemented",
            "Screen real estate: FT+ needs 1 L2 panel per ticker; ATP floating windows stack more easily",
        ),
        (
            "Orders",
            "OCR (Orders panel)",  "CONFIRMED -- all orders in one panel",
            "OCR (PrintWindow)",   "POSSIBLE -- same approach; not yet implemented",
            "FT+ OCR already working; ATP would need separate OCR adapter",
        ),
        (
            "Watchlist (all tickers)",
            "OCR (Watchlist)",     "CONFIRMED -- all visible tickers in one pass",
            "OCR (PrintWindow)",   "POSSIBLE -- same approach; not yet implemented",
            "FT+ preferred: Watchlist OCR already working; must scroll if > 1 screen",
        ),
        (
            "ADV (10D / 90D avg vol)",
            "OCR (Watchlist)",     "CONFIRMED -- same OCR pass as prev_close",
            "OCR (PrintWindow)",   "POSSIBLE -- same approach; not yet implemented",
            "Only available from Watchlist panel in both apps",
        ),
        (
            "Div ex-date",
            "OCR (Watchlist)",     "CONFIRMED -- same OCR pass",
            "OCR (PrintWindow)",   "POSSIBLE -- same approach; not yet implemented",
            "Required for dividend-aware strategy rules",
        ),
        (
            "UIA data access",
            "BLOCKED",             "Telerik MAUI RadMauiScrollView is DirectX-rendered",
            "BLOCKED",             "Telerik WPF panels also DirectX-rendered (Viewport3D)",
            "Both apps confirmed UIA-opaque for content; 0 Table/DataItem/DataGrid found",
        ),
        (
            "OCR blocks monitoring",
            "NO",                  "PrintWindow reads GPU buffer regardless of z-order",
            "NO",                  "PrintWindow would work the same way",
            "Neither app requires the window to be in front for OCR capture",
        ),
        (
            "Screen real estate for L2",
            "1 panel per ticker",  "HIGH -- each ticker needs its own L2 panel open",
            "Floating windows",    "MEDIUM -- smaller floating L2 windows can be stacked",
            "Practical advantage for ATP if trading many tickers simultaneously",
        ),
        (
            "Recommendation",
            "USE THIS",            "OCR adapters confirmed working; no extra app",
            "Not yet worth it",    "Same OCR needed; extra complexity; no UIA advantage",
            "Stick with Fidelity Trader+ -- ATP offers no accessibility benefit",
        ),
    ]

    def _row(f, m1, s1, m2, s2, note=""):
        print(f"\n  [{f}]")
        print(f"    FT+  method : {m1}")
        print(f"    FT+  status : {s1}")
        print(f"    ATP  method : {m2}")
        print(f"    ATP  status : {s2}")
        if note:
            print(f"    Note        : {note}")

    for r in rows:
        _row(*r)

    print("\n" + "=" * 86)
    print("  LEGEND")
    print("  CONFIRMED = tested against live app session (2026-05-01)")
    print("  POSSIBLE  = technically feasible but not yet implemented")
    print("  BLOCKED   = confirmed inaccessible via UIA (DirectX/GPU rendering)")
    print("  Note: ATP content panels confirmed UIA-opaque via descendants() search")
    print("        (0 Table/DataGrid/DataItem found). Both apps require OCR for data.")
    print("=" * 86)


# -- Main ---------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smoke test: read Level II and Watchlist from live Fidelity Trader+"
    )
    parser.add_argument("symbol", help="Ticker symbol to read (e.g. SPY, FRDM)")
    parser.add_argument(
        "--app",
        choices=("trader_plus", "active_trader_pro"),
        default="trader_plus",
        help="Which Fidelity app to connect to (default: trader_plus)",
    )
    parser.add_argument(
        "--debug-tree", metavar="FILE",
        nargs="?", const="atp_tree.txt",
        help="Dump full UIA control tree to FILE (default: atp_tree.txt) then exit"
    )
    parser.add_argument("--levels", type=int, default=7, help="Number of L2 levels to display")
    parser.add_argument(
        "--debug-ocr", action="store_true",
        help="Save captured screenshots and print OCR detections for debugging"
    )
    parser.add_argument(
        "--skip-l2", action="store_true",
        help="Skip Level II panel (use when no L2 window is open for this symbol)"
    )
    parser.add_argument(
        "--compare", action="store_true",
        help="Print the Fidelity Trader+ vs Active Trader Pro comparison table and exit"
    )
    args = parser.parse_args()

    if args.compare:
        show_comparison_table()
        return

    if args.debug_ocr:
        from adapters.atp_ocr import enable_debug
        enable_debug()
        print("OCR debug mode ON -- images will be saved to current directory")

    symbol = args.symbol.upper()

    # Connect to the selected application
    if args.app == "trader_plus":
        try:
            from adapters._atp_connect import get_app
            app = get_app()
            pid = app.process
            print(f"Connected to Fidelity Trader+ (PID {pid})")
        except RuntimeError as exc:
            print(f"ERROR: {exc}")
            sys.exit(1)
    else:
        try:
            from adapters.fatp_connect import get_fatp_app
            app = get_fatp_app()
            pid = app.process
            print(f"Connected to Fidelity Active Trader Pro (PID {pid})")
        except RuntimeError as exc:
            print(f"ERROR: {exc}")
            sys.exit(1)

    if args.debug_tree:
        dump_tree(symbol, Path(args.debug_tree), app=app)
        sys.exit(0)

    if args.app == "trader_plus":
        ok_l2 = show_level2(symbol, n_levels=args.levels) if not args.skip_l2 else None
        ok_wl = show_watchlist("trader_plus")
    else:
        ok_l2 = show_level2_fatp(symbol, n_levels=args.levels) if not args.skip_l2 else None
        ok_wl = show_watchlist("active_trader_pro")

    ok_orders = show_orders()

    print()
    checks = [("Watchlist", ok_wl), ("Orders", ok_orders)]
    if ok_l2 is not None:
        checks.insert(0, ("Level II", ok_l2))
    failed = [name for name, ok in checks if not ok]
    if failed:
        print(f"FAILED panels: {', '.join(failed)}")
        sys.exit(1)
    print("Smoke test PASSED")


# -- Active Trader Pro stubs --------------------------------------------------

def show_quote_fatp(symbol: str) -> bool:
    """Placeholder: read Quote from Active Trader Pro via UIA."""
    print(f"\n[QUOTE] Active Trader Pro quote reader not yet implemented.")
    print(f"  When JAB is enabled, the Quotes window exposes a Table with")
    print(f"  rows for each subscribed symbol.  Add FATQuoteAdapter to")
    print(f"  adapters/fatp_quote.py following the same pattern as fatp_watchlist.py.")
    return False


def show_level2_fatp(symbol: str, n_levels: int = 5) -> bool:
    """Placeholder: read Level 2 from Active Trader Pro via UIA."""
    print(f"\n[LEVEL II] Active Trader Pro L2 reader not yet implemented.")
    print(f"  Open a Level 2 window for {symbol} in ATP.  With JAB enabled,")
    print(f"  the L2 grid should expose DataItem rows for each bid/ask level.")
    print(f"  Add FATLevel2Adapter to adapters/fatp_level2.py.")
    return False


def show_orders_fatp() -> bool:
    """Placeholder: read Orders from Active Trader Pro via UIA."""
    print(f"\n[ORDERS] Active Trader Pro orders reader not yet implemented.")
    print(f"  The Active Orders panel in ATP uses a JTable accessible via JAB.")
    print(f"  Add FATOrdersAdapter to adapters/fatp_orders.py.")
    return False


if __name__ == "__main__":
    main()