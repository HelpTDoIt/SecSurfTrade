"""
Live monitor screen — polls ATP Orders, displays fill progress, detects stalls.

Entry point (from SecSurfTrade/ or fidelity_rebalancer/):
    python -m fidelity_rebalancer.tui.monitor --plan plans/plan_YYYYMMDD_HHMM.json
    python -m tui.monitor --plan plans/plan_YYYYMMDD_HHMM.json [--poll-seconds 45]

Key bindings: R = refresh now, Q = quit, C = confirm re-quote, I = ignore stall.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

import argparse

from engine import observability

_log = logging.getLogger("monitor")


from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import Footer, Label, Static
from textual.containers import Container, Vertical

from cli import resolve_path
from adapters import OrderRow, OrderStatus, QuoteSnapshot
from engine.optimizer import recompute_buys
from engine.stall import RequoteSuggestion, StallEvent, detect_stalls, recommend_requote
from engine.sweep import should_sweep
from state.schema import (
    BuyAllocationRecord,
    ChunkRecord,
    PlanOutput,
    RebalanceState,
    StrategyDecision,
)


# ── Journal ───────────────────────────────────────────────────────────────


class Journal:
    """Append-only JSONL audit log."""

    _last_heartbeat: float = 0.0
    _HEARTBEAT_INTERVAL = 300.0  # 5 minutes

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(mode=0o600, exist_ok=True)

    def write(self, event_type: str, payload: dict) -> None:
        entry = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "event_type": event_type,
            "payload": payload,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def poll_heartbeat(self) -> None:
        import time

        now = time.monotonic()
        if now - self._last_heartbeat >= self._HEARTBEAT_INTERVAL:
            self.write("heartbeat", {})
            self._last_heartbeat = now


# ── State helpers ─────────────────────────────────────────────────────────


def _chunk_lookup(state: RebalanceState) -> dict[str, ChunkRecord]:
    return {
        **{ch.chunk_id: ch for ch in state.computed.sell_chunks},
        **{ch.chunk_id: ch for ch in state.computed.buy_chunks},
    }


def _sell_chunk_ids_by_account(state: RebalanceState) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for ch in state.computed.sell_chunks:
        result.setdefault(ch.account, []).append(ch.chunk_id)
    return result


def _all_sells_terminal(
    account: str,
    order_map: dict[str, OrderRow],
    sell_ids: list[str],
) -> bool:
    terminal = {OrderStatus.Filled, OrderStatus.Cancelled}
    return all(
        order_map.get(cid, None) is not None and order_map[cid].status in terminal
        for cid in sell_ids
    )


def _actual_proceeds(
    account: str,
    order_map: dict[str, OrderRow],
    sell_ids: list[str],
) -> float:
    total = 0.0
    for cid in sell_ids:
        row = order_map.get(cid)
        if row and row.status == OrderStatus.Filled:
            total += row.filled_qty * row.limit_price
    return total


def _market_minutes(now: datetime | None = None) -> int | None:
    """Minutes since market open (9:30 ET), or None outside 9:30–16:00.

    Mirrors ``cli/strategy.py``: a naive *local* wall-clock time is interpreted
    as ET — there is no zoneinfo anywhere in the codebase.  Used only for the
    EOD-sweep clock half of the recompute trigger; journaling stays on UTC.
    """
    now = now or datetime.now()
    m = (now.hour - 9) * 60 + (now.minute - 30)
    if m < 0 or m > 390:
        return None
    return m


# ── Fill detection ────────────────────────────────────────────────────────


def detect_and_log_fills(
    new_map: dict[str, "OrderRow"],
    last_filled: dict[str, float],
    journal: "Journal | None",
) -> None:
    """Detect fill deltas between polls and write `fill` journal events.

    Compares each order's current ``filled_qty`` against the value stored in
    ``last_filled`` (mutated in-place after each call).  Any positive delta
    triggers one ``fill`` event via ``journal.write``.

    First-seen behaviour: an order first seen with ``filled_qty > 0`` emits a
    fill event for that full amount.  Fills that occurred before the monitor
    started are captured rather than silently dropped, yielding a complete
    audit trail from first observation forward.

    Safe to call with ``journal=None`` (no events written but ``last_filled``
    is still updated).  This makes unit testing without a real Journal file
    possible.

    Args:
        new_map:     {order_id: OrderRow} snapshot from the current poll.
        last_filled: Mutable dict tracking last-seen filled_qty per order_id.
                     Pass an empty dict on first call; it is updated in place.
        journal:     Journal instance to receive ``fill`` events, or None.
    """
    for order_id, row in new_map.items():
        prev = last_filled.get(order_id, 0.0)
        delta = row.filled_qty - prev
        if delta > 0:
            _log.info(
                "fill: %s %s %s delta=%.0f filled=%.0f/%.0f",
                order_id,
                row.symbol,
                row.side,
                delta,
                row.filled_qty,
                row.qty,
            )
            if journal is not None:
                journal.write(
                    "fill",
                    {
                        "order_id": order_id,
                        "symbol": row.symbol,
                        "side": row.side,
                        "delta": delta,
                        "filled_qty": row.filled_qty,
                        "limit_price": row.limit_price,
                        "status": row.status.value,
                    },
                )
        last_filled[order_id] = row.filled_qty


# ── Monitor App ───────────────────────────────────────────────────────────


class MonitorApp(App):
    """Live fill-progress monitor with stall detection."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh Now"),
        Binding("q", "quit_app", "Quit"),
        Binding("c", "confirm_requote", "Confirm Re-quote"),
        Binding("i", "ignore_stall", "Ignore Stall"),
    ]

    CSS = """
    MonitorApp {
        background: $surface;
    }
    #status-panel {
        width: 100%;
        height: 1fr;
        padding: 1 2;
        overflow-y: auto;
    }
    #stall-panel {
        width: 100%;
        height: auto;
        min-height: 4;
        padding: 1 2;
        border-top: solid $warning;
        background: $panel;
    }
    #footer-bar {
        height: 1;
        padding: 0 2;
        background: $panel-darken-1;
    }
    MonitorApp Footer {
        dock: bottom;
    }
    """

    def __init__(
        self,
        plan: PlanOutput,
        orders_adapter,
        quote_adapter=None,
        poll_seconds: int = 45,
        journal: Journal | None = None,
        plans_dir: Path | None = None,
        scan_mode: bool = False,
    ) -> None:
        super().__init__()
        self._plan = plan
        self._state = plan.state
        self._orders_adapter = orders_adapter
        self._scan_mode = scan_mode
        self._quote_adapter = quote_adapter
        self._poll_seconds = poll_seconds
        self._journal = journal
        self._plans_dir = plans_dir or Path("plans")

        self._chunk_map = _chunk_lookup(self._state)
        self._sell_ids_by_account = _sell_chunk_ids_by_account(self._state)
        self._order_map: dict[str, OrderRow] = {}
        self._stalls: list[StallEvent] = []
        self._suggestions: list[RequoteSuggestion] = []
        self._snoozed: set[str] = set()
        self._recomputed_accounts: set[str] = set()
        self._next_check: datetime | None = None
        self._ticker_for_chunk: dict[str, str] = {
            ch.chunk_id: ch.ticker
            for ch in list(self._state.computed.sell_chunks)
            + list(self._state.computed.buy_chunks)
        }
        self._side_for_chunk: dict[str, str] = {
            **{ch.chunk_id: "sell" for ch in self._state.computed.sell_chunks},
            **{ch.chunk_id: "buy" for ch in self._state.computed.buy_chunks},
        }
        # Per-order last-seen filled_qty, used to detect fill deltas between polls.
        # An order first seen with filled_qty > 0 is treated as an immediate fill
        # for that amount — this yields a complete trail from first observation
        # forward rather than silently discarding pre-monitor fills.
        self._last_filled: dict[str, float] = {}
        self._last_poll_error: str = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="status-panel")
        yield Static("", id="stall-panel")
        yield Static("", id="footer-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._do_poll()
        self.set_interval(self._poll_seconds, self._do_poll)

    # ── Poll ─────────────────────────────────────────────────────────────

    def _do_poll(self) -> None:
        _log.debug("poll start")
        try:
            rows = self._orders_adapter.get_orders()
            self._last_poll_error = ""
            _log.info("poll ok: %d order(s) received", len(rows))
        except Exception as exc:
            err = str(exc)
            self._log_event("poll_error", {"error": err})
            self._last_poll_error = err
            _log.error("poll error: %s", err)
            rows = []

        now = datetime.now(tz=timezone.utc)
        self._next_check = datetime.fromtimestamp(
            now.timestamp() + self._poll_seconds, tz=timezone.utc
        )

        # Build order map keyed by order_id (= chunk_id in our workflow)
        new_map: dict[str, OrderRow] = {}
        for row in rows:
            new_map[row.order_id] = row

        # Compare only the stable, order-meaningful fields — NOT placed_at /
        # last_update_at, which the OCR parser sets to datetime.now() on every
        # parse and would make every poll appear "changed" even when nothing moved.
        def _sig(m: dict) -> dict:
            return {oid: (r.status, r.filled_qty, r.qty) for oid, r in m.items()}

        changed = _sig(new_map) != _sig(self._order_map)
        if changed:
            self._log_event("poll", {"order_count": len(rows), "changed": True})
        else:
            if self._journal:
                self._journal.poll_heartbeat()

        # Detect fill deltas and write `fill` journal events BEFORE updating
        # self._order_map so that self._last_filled reflects the prior state.
        self._detect_and_log_fills(new_map)

        self._order_map = new_map

        # Detect stalls (ignore snoozed chunks)
        threshold = self._state.inputs.config.stall_threshold_seconds
        all_stalls = detect_stalls(list(new_map.values()), threshold, now)
        self._stalls = [s for s in all_stalls if s.chunk_id not in self._snoozed]

        # Get re-quote suggestions
        self._suggestions = []
        for stall in self._stalls:
            side = self._side_for_chunk.get(stall.chunk_id, "sell")
            ticker = self._ticker_for_chunk.get(stall.chunk_id, "")
            quote = self._get_quote(ticker)
            if quote:
                sugg = recommend_requote(stall, side, quote)
                self._suggestions.append(sugg)
                self._log_event(
                    "stall_detected",
                    {
                        "chunk_id": stall.chunk_id,
                        "seconds_stalled": stall.seconds_stalled,
                        "suggested_limit": sugg.new_limit,
                    },
                )

        # Check for recompute triggers (F-1): fire once per account when its
        # sells are all terminal (proceeds fully known) OR the EOD-sweep clock
        # has passed (fallback — recompute on whatever proceeds are known).
        mkt_min = _market_minutes()
        for account, sell_ids in self._sell_ids_by_account.items():
            fire, reason = self._should_recompute(account, sell_ids, new_map, mkt_min)
            if fire:
                proceeds = _actual_proceeds(account, new_map, sell_ids)
                self._recompute_account(account, proceeds, reason)
                self._recomputed_accounts.add(account)

        self._refresh_display(now)

    def _should_recompute(
        self,
        account: str,
        sell_ids: list[str],
        order_map: dict[str, OrderRow],
        mkt_minutes: int | None,
    ) -> tuple[bool, str]:
        """Decide whether to recompute buys for ``account`` this poll.

        Returns ``(fire, reason)``.  Fires when the account's sells are all
        terminal (proceeds fully known) OR the EOD-sweep clock has passed
        (fallback).  Already-recomputed accounts and accounts with no sells
        never fire.  ``unfilled_frac`` is pinned to 0.0 so only the clock half
        of ``should_sweep`` can fire — the recompute trigger is *terminal OR
        clock*, never fill-fraction.
        """
        if account in self._recomputed_accounts or not sell_ids:
            return False, ""
        if _all_sells_terminal(account, order_map, sell_ids):
            return True, "all_sells_terminal"
        cfg = self._state.inputs.config
        if should_sweep(
            mkt_minutes,
            0.0,
            sweep_time_minutes=cfg.sweep_time_minutes,
            sweep_unfilled_frac=cfg.sweep_unfilled_frac,
        ):
            return True, "eod_sweep_clock"
        return False, ""

    def _recompute_account(self, account: str, proceeds: float, trigger: str) -> None:
        """Re-run the drift allocator on realized proceeds and journal the result.

        Per F-1 the revised allocations replace this account's in-memory
        ``buy_allocations``; chunks are intentionally NOT regenerated (no
        re-driving of order flow).  The hard rule holds: nothing is placed,
        modified, or cancelled — this only records a revised plan for the human.
        """
        before = {
            ba.strategy: ba.share_target
            for ba in self._state.computed.buy_allocations
            if ba.account == account
        }
        revised: list[BuyAllocationRecord] = recompute_buys(
            self._state, account, proceeds
        )
        # Replace this account's allocations in place; leave other accounts intact.
        self._state.computed.buy_allocations = [
            ba for ba in self._state.computed.buy_allocations if ba.account != account
        ] + revised
        self._log_event(
            "recompute_buys",
            {
                "account": account,
                "trigger": trigger,
                "proceeds": round(proceeds, 2),
                "before": before,
                "after": {ba.strategy: ba.share_target for ba in revised},
                "allocations": [
                    {
                        "strategy": ba.strategy,
                        "ticker": ba.ticker,
                        "share_target": ba.share_target,
                        "dollar_target": round(ba.dollar_target, 2),
                        "limit_price": ba.limit_price,
                    }
                    for ba in revised
                ],
            },
        )

    def _detect_and_log_fills(self, new_map: dict[str, "OrderRow"]) -> None:
        """Delegate to the module-level helper, updating self._last_filled in place."""
        detect_and_log_fills(new_map, self._last_filled, self._journal)

    def _get_quote(self, ticker: str) -> QuoteSnapshot | None:
        if not self._quote_adapter or not ticker:
            return None
        try:
            return self._quote_adapter.get_quote(ticker)
        except Exception:
            return None

    # ── Display ───────────────────────────────────────────────────────────

    def _refresh_display(self, now: datetime) -> None:
        self.query_one("#status-panel", Static).update(self._render_status())
        self.query_one("#stall-panel", Static).update(self._render_stalls())
        next_str = self._next_check.strftime("%H:%M:%S") if self._next_check else "—"
        self.query_one("#footer-bar", Static).update(
            f"Last poll: {now.strftime('%H:%M:%S')}    "
            f"Next check: {next_str}    "
            f"[R] Refresh  [Q] Quit"
        )

    def _render_scan(self) -> str:
        """Scan-mode display: raw OCR order feed, no plan-matching required."""
        lines: list[str] = []
        ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
        lines.append(f"[bold]SCAN MODE — live orders from ATP  {ts}[/bold]")
        lines.append("─" * 72)
        if self._last_poll_error:
            lines.append(f"  [red]Poll error:[/red] {self._last_poll_error}")
        if not self._order_map:
            lines.append(
                "  [dim]No orders detected — ensure the Orders panel is visible.[/dim]"
            )
        else:
            open_first = sorted(
                self._order_map.values(),
                key=lambda r: (r.status != OrderStatus.Open, r.symbol),
            )
            for row in open_first:
                st = row.status.value
                pct = (row.filled_qty / row.qty * 100) if row.qty > 0 else 0.0
                color = (
                    "[green]"
                    if row.status == OrderStatus.Filled
                    else "[yellow]"
                    if row.status == OrderStatus.Open
                    else "[dim]"
                )
                close = color.replace("[", "[/").replace("bold", "") or "[/dim]"
                lines.append(
                    f"  {color}{row.symbol:<6} {row.side:<4}  "
                    f"{row.filled_qty:>6.0f}/{row.qty:<6.0f}  ({pct:3.0f}%)  "
                    f"{st:<16}  lim ${row.limit_price:.4f}  {row.order_id}{close}"
                )
        lines.append("─" * 72)
        return "\n".join(lines)

    def _render_status(self) -> str:
        if self._scan_mode:
            return self._render_scan()
        lines: list[str] = []
        ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
        lines.append(f"[bold]EXECUTION STATUS — {ts}[/bold]")
        lines.append("─" * 60)

        accounts = {ch.account for ch in self._state.computed.sell_chunks}
        accounts |= {ch.account for ch in self._state.computed.buy_chunks}

        for account in sorted(accounts):
            lines.append(f"\n[bold]{account}[/bold]")
            sell_ids = self._sell_ids_by_account.get(account, [])
            if sell_ids:
                lines.append("  [underline]SELLS[/underline]")
                for cid in sell_ids:
                    lines.append(self._render_chunk_row(cid, "  "))

            # Buy section
            buy_chunks = [
                ch for ch in self._state.computed.buy_chunks if ch.account == account
            ]
            if buy_chunks:
                all_sells_done = (not sell_ids) or _all_sells_terminal(
                    account, self._order_map, sell_ids
                )
                proceeds_str = ""
                if account in self._recomputed_accounts:
                    proceeds = _actual_proceeds(account, self._order_map, sell_ids)
                    acct_in = next(
                        (a for a in self._state.inputs.accounts if a.name == account),
                        None,
                    )
                    cash = acct_in.cash_spaxx if acct_in else 0.0
                    pending = acct_in.pending_activity if acct_in else 0.0
                    effective_cash = cash + pending
                    if pending:
                        proceeds_str = (
                            f" (Budget: ${proceeds + effective_cash:,.2f} = "
                            f"${proceeds:,.2f} proceeds + ${cash:,.2f} cash "
                            f"{'+' if pending >= 0 else '−'} ${abs(pending):,.2f} pending)"
                        )
                    else:
                        proceeds_str = f" (Budget: ${proceeds + cash:,.2f} = ${proceeds:,.2f} proceeds + ${cash:,.2f} cash)"
                lines.append(f"  [underline]BUYS[/underline]{proceeds_str}")
                for ch in buy_chunks:
                    if all_sells_done:
                        lines.append(self._render_chunk_row(ch.chunk_id, "  "))
                    else:
                        lines.append(
                            f"    ├─ {ch.ticker:<6} WAITING — sells not complete"
                        )

        lines.append("\n" + "─" * 60)
        return "\n".join(lines)

    def _render_chunk_row(self, chunk_id: str, indent: str) -> str:
        ch = self._chunk_map.get(chunk_id)
        if ch is None:
            return f"{indent}  [dim]{chunk_id} — not in plan[/dim]"
        row = self._order_map.get(chunk_id)
        stalled = any(s.chunk_id == chunk_id for s in self._stalls)
        color_start = "[yellow]" if stalled else ""
        color_end = "[/yellow]" if stalled else ""

        if row is None:
            return f"{indent}  ├─ {color_start}{ch.ticker:<6} {ch.shares:,.0f} sh  LIMIT ${ch.limit_price:.4f}  [dim]NOT YET IN ATP[/dim]{color_end}"

        filled = row.filled_qty
        total = row.qty
        pct = filled / total * 100 if total > 0 else 0
        status_str = row.status.value
        avg_str = f"avg ${row.limit_price:.4f}" if filled > 0 else ""
        proceeds = filled * row.limit_price if row.side == "SELL" else 0.0
        proceeds_str = f"  proceeds ${proceeds:,.2f}" if proceeds > 0 else ""

        stall_tag = " [yellow]⚠ STALLED[/yellow]" if stalled else ""
        return (
            f"{indent}  ├─ {color_start}{ch.ticker:<6} "
            f"{filled:,.0f}/{total:,.0f} filled ({pct:.0f}%)  "
            f"{status_str}  {avg_str}{proceeds_str}{color_end}{stall_tag}"
        )

    def _render_stalls(self) -> str:
        if not self._stalls:
            return ""
        lines: list[str] = []
        for stall, sugg in zip(self._stalls, self._suggestions):
            mins = int(stall.seconds_stalled // 60)
            secs = int(stall.seconds_stalled % 60)
            ticker = self._ticker_for_chunk.get(stall.chunk_id, "?")
            lines.append(
                f"[yellow]⚠  STALL: {ticker} clip {stall.chunk_id} has been PartiallyFilled "
                f"for {mins}m {secs}s[/yellow]"
            )
            lines.append(
                f"   Original limit ${stall.original_limit:.4f}  "
                f"Suggested re-quote: [bold]${sugg.new_limit:.4f}[/bold]"
            )
            lines.append(
                "   [bold][C][/bold] Mark cancelled & re-quoted    [bold][I][/bold] Ignore"
            )
        return "\n".join(lines)

    # ── Actions ───────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._do_poll()

    def action_quit_app(self) -> None:
        self.exit(0)

    def action_confirm_requote(self) -> None:
        if not self._suggestions:
            return
        sugg = self._suggestions[0]
        stall = self._stalls[0]
        now = datetime.now(tz=timezone.utc)
        self._log_event(
            "requote_confirmed",
            {
                "chunk_id": stall.chunk_id,
                "original_limit": stall.original_limit,
                "new_limit": sugg.new_limit,
                "remaining_qty": sugg.remaining_qty,
            },
        )
        # Mark original as cancelled in order_map (simulation; real ATP cancel is manual)
        if stall.chunk_id in self._order_map:
            self._order_map[stall.chunk_id].status = OrderStatus.Cancelled
        # Remove from active stalls
        self._stalls = [s for s in self._stalls if s.chunk_id != stall.chunk_id]
        self._suggestions = [
            s for s in self._suggestions if s.chunk_id != stall.chunk_id
        ]
        self._refresh_display(now)

    def action_ignore_stall(self) -> None:
        if not self._stalls:
            return
        stall = self._stalls[0]
        self._snoozed.add(stall.chunk_id)
        self._stalls = self._stalls[1:]
        self._suggestions = self._suggestions[1:] if self._suggestions else []
        self._log_event("stall_ignored", {"chunk_id": stall.chunk_id})
        self._refresh_display(datetime.now(tz=timezone.utc))

    # ── Logging ───────────────────────────────────────────────────────────

    def _log_event(self, event_type: str, payload: dict) -> None:
        if self._journal:
            self._journal.write(event_type, payload)


# ── CLI entry point ───────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor ATP fill progress for an approved trade plan"
    )
    parser.add_argument(
        "--plan",
        default=None,
        help="Path to approved plan JSON (required unless --scan is set)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=45,
        help="Polling interval in seconds (default: 45)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use mock ATP adapter (no real ATP required)",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help=(
            "Scan mode: poll live ATP orders without a plan file. "
            "Displays a raw order feed and writes fill events to the journal."
        ),
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help=(
            "Test / diagnostic mode: save a timestamped OCR screenshot on every "
            "poll to logs/ocr_captures/ and write DEBUG-level entries to "
            "logs/monitor.log.  Use this to verify OCR coverage without touching "
            "any real order flow."
        ),
    )
    parser.add_argument(
        "--quote-source",
        choices=["yahoo", "atp", "mock"],
        default="yahoo",
        help=(
            "Where the stall advisor reads live prices for re-quote suggestions "
            "(default: yahoo). Ignored when --mock is set (mock reused for quotes)."
        ),
    )
    args = parser.parse_args()

    # ── Logging + OCR debug setup ─────────────────────────────────────────
    # Anchor to the package root so logs always land in fidelity_rebalancer/logs/
    # regardless of which directory the user invokes the monitor from.
    log_dir = _ROOT / "logs"
    observability.setup_logging(log_dir, verbose=args.test, filename="monitor.log")
    _log.info(
        "monitor starting  scan=%s  test=%s  poll=%ds",
        args.scan,
        args.test,
        args.poll_seconds,
    )

    if args.test:
        from adapters.atp_ocr import enable_debug as _enable_ocr_debug

        ocr_capture_dir = log_dir / "ocr_captures"
        _enable_ocr_debug(save_dir=ocr_capture_dir)
        _log.info("OCR debug images will be saved to %s", ocr_capture_dir)

    if not args.scan and not args.plan:
        parser.error("--plan is required unless --scan is set")

    if args.scan:
        # Scan mode: no plan file required; build an empty in-memory plan.
        from datetime import timezone as _tz

        from state.schema import Computed, EngineConfig, Inputs, RebalanceState

        _empty_state = RebalanceState(
            generated_at=datetime.now(tz=_tz.utc),
            generator="engine",
            inputs=Inputs(accounts=[], signals=[]),
            computed=Computed(
                cash_ok={},
                one_share_total={},
                sells=[],
                buy_allocations=[],
                sell_chunks=[],
                buy_chunks=[],
            ),
        )
        plan = PlanOutput(
            generated_at=_empty_state.generated_at,
            state=_empty_state,
            decisions=[],
        )
        plan_path = None
    else:
        plan_path = resolve_path(args.plan)
        plan = PlanOutput.model_validate_json(plan_path.read_text(encoding="utf-8"))

    poll_seconds = args.poll_seconds
    config_seconds = plan.state.inputs.config.polling_seconds
    if config_seconds and not args.poll_seconds:
        poll_seconds = config_seconds

    # Orders adapter: UIA → OCR → MockATP fallback chain.
    if args.mock:
        from adapters.mock_atp import MockATP

        adapter = MockATP()
    else:
        try:
            from adapters.atp_orders import ATPOrdersAdapter

            adapter = ATPOrdersAdapter()
        except Exception:
            try:
                from adapters.atp_ocr import OCROrdersAdapter

                adapter = OCROrdersAdapter()
            except Exception:
                from adapters.mock_atp import MockATP

                adapter = MockATP()

    # Quote adapter (live prices the stall advisor uses to propose new limits).
    # Without this, _get_quote() always returns None and recommend_requote()
    # never fires — stalls get flagged but no re-quote price is suggested.
    quote_adapter = None
    if args.mock:
        # MockATP serves both orders and quotes; reuse the one instance.
        quote_adapter = adapter
    elif args.quote_source == "mock":
        from adapters.mock_atp import MockATP

        quote_adapter = MockATP()
    elif args.quote_source == "atp":
        try:
            from adapters.atp_quote import ATPQuoteAdapter

            quote_adapter = ATPQuoteAdapter()
        except Exception:
            quote_adapter = None
    else:  # "yahoo" — reliable live source, always returns a quote
        try:
            from adapters.yfinance_fallback import YFinanceQuoteAdapter

            quote_adapter = YFinanceQuoteAdapter()
        except Exception:
            quote_adapter = None

    journal_path = log_dir / "journal.jsonl"  # log_dir is already _ROOT / "logs"
    journal = Journal(journal_path)
    journal.write(
        "monitor_start",
        {"plan": str(plan_path) if plan_path else "scan", "poll_seconds": poll_seconds},
    )

    plans_dir = plan_path.parent if plan_path else _ROOT / "plans"
    app = MonitorApp(
        plan=plan,
        orders_adapter=adapter,
        quote_adapter=quote_adapter,
        poll_seconds=poll_seconds,
        journal=journal,
        plans_dir=plans_dir,
        scan_mode=args.scan,
    )
    app.run()


if __name__ == "__main__":
    main()
