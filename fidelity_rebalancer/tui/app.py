"""
Textual app entry point for the strategy approval flow.

Usage (from SecSurfTrade/ or fidelity_rebalancer/):
    python -m fidelity_rebalancer.tui.app --plan <state_json>
    python -m tui.app --plan <state_json> --resume <partial_plan_json>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from textual.app import App, ComposeResult
from textual.widgets import Static

from cli import resolve_output_path, resolve_path
from state.schema import (
    BuyAllocationRecord,
    BuyStrategy,
    ChunkRecord,
    PlanOutput,
    RebalanceState,
    SellRecord,
    SellStrategy,
    StrategyDecision,
)
from tui.presenter import PresenterScreen


# ── Helpers ───────────────────────────────────────────────────────────────


def _build_chunk_lookup(state: RebalanceState) -> dict[str, ChunkRecord]:
    lookup: dict[str, ChunkRecord] = {}
    for ch in state.computed.sell_chunks:
        lookup[ch.chunk_id] = ch
    for ch in state.computed.buy_chunks:
        lookup[ch.chunk_id] = ch
    return lookup


def _build_record_lookup(
    state: RebalanceState,
) -> tuple[dict[str, SellRecord], dict[str, BuyAllocationRecord]]:
    sells = {(r.account, r.strategy, r.ticker): r for r in state.computed.sells}
    buys = {
        (r.account, r.strategy, r.ticker): r for r in state.computed.buy_allocations
    }
    return sells, buys


def _collect_strategies(
    state: RebalanceState,
    sells_lookup: dict,
    buys_lookup: dict,
    chunk_lookup: dict,
) -> list[tuple]:
    """
    Returns list of (side, idx, strategy, record, chunks).
    Sells first, then buys.
    """
    result = []
    for i, strat in enumerate(state.computed.sell_strategies):
        key = (strat.account, strat.strategy, strat.ticker)
        record = sells_lookup.get(key)
        if record is None:
            continue
        chunks = [chunk_lookup[cid] for cid in strat.chunk_ids if cid in chunk_lookup]
        result.append(("sell", i, strat, record, chunks))
    for i, strat in enumerate(state.computed.buy_strategies):
        key = (strat.account, strat.strategy, strat.ticker)
        record = buys_lookup.get(key)
        if record is None:
            continue
        chunks = [chunk_lookup[cid] for cid in strat.chunk_ids if cid in chunk_lookup]
        result.append(("buy", i, strat, record, chunks))
    return result


def _make_txt_line(
    decision: StrategyDecision,
    side: str,
    strategy: SellStrategy | BuyStrategy,
    chunks: list[ChunkRecord],
    approved_limit: float,
    order_type: str,
) -> str:
    lines = []
    price_str = f"MARKET" if order_type == "MARKET" else f"LIMIT ${approved_limit:.4f}"
    for ch in chunks:
        side_label = "SELL" if side == "sell" else "BUY "
        line = (
            f"[ ] {strategy.account} — {side_label} {strategy.ticker}"
            f" {ch.shares:,.0f} shs {price_str} DAY"
            f"    ({strategy.strategy}, chunk {ch.chunk_id})"
        )
        lines.append(line)
    # Surface the engine's pricing rationale (G-1/B-2): the reasoning bullets
    # are computed per strategy but were previously DEBUG-only / TUI-only. Carry
    # them into the printed checklist so the human can audit *why* each limit
    # was chosen before entering the order in ATP.
    if lines and getattr(strategy, "reasoning", None):
        lines.append(f"      why ({strategy.rule}):")
        for bullet in strategy.reasoning:
            lines.append(f"        - {bullet}")
    return "\n".join(lines)


def _save_plan(
    state: RebalanceState,
    decisions: list[StrategyDecision],
    strategies: list[tuple],
    plans_dir: Path,
) -> tuple[Path, Path, Path]:
    plans_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M")
    json_path = plans_dir / f"plan_{ts}.json"
    txt_path = plans_dir / f"plan_{ts}.txt"
    fills_path = plans_dir / f"fills_{ts}.csv"

    plan = PlanOutput(
        generated_at=datetime.now(tz=timezone.utc),
        state=state,
        decisions=decisions,
    )
    json_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    # TXT checklist + fills CSV template (one row per chunk, approved strategies only)
    txt_lines: list[str] = []
    fills_rows: list[str] = ["Account,Side,Strategy,FillPrice,FillShares"]
    chunk_lookup = _build_chunk_lookup(state)
    for decision in decisions:
        if decision.approval_status == "skipped":
            continue
        side = decision.side
        idx = decision.idx
        if side == "sell":
            strat = state.computed.sell_strategies[idx]
        else:
            strat = state.computed.buy_strategies[idx]
        chunks = [chunk_lookup[cid] for cid in strat.chunk_ids if cid in chunk_lookup]
        line = _make_txt_line(
            decision,
            side,
            strat,
            chunks,
            decision.approved_limit_price,
            decision.approved_order_type,
        )
        if line:
            txt_lines.append(line)

        side_label = "SELL" if side == "sell" else "BUY"
        fill_price = decision.approved_limit_price
        for ch in chunks:
            fills_rows.append(
                f'"{strat.account}","{side_label}","{strat.strategy}",'
                f"{fill_price:.4f},{ch.shares:.0f}"
            )

    txt_path.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")
    fills_path.write_text("\n".join(fills_rows) + "\n", encoding="utf-8")
    return json_path, txt_path, fills_path


# ── App ───────────────────────────────────────────────────────────────────


class RebalanceApp(App):
    """Approval wizard — presents each strategy for human sign-off."""

    CSS = """
    RebalanceApp {
        background: $surface;
    }
    #no-strategies {
        content-align: center middle;
        height: 100%;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        state: RebalanceState,
        plans_dir: Path,
        start_idx: int = 0,
        initial_decisions: list[StrategyDecision] | None = None,
    ) -> None:
        super().__init__()
        self._state = state
        self._plans_dir = plans_dir

        sells_lookup, buys_lookup = _build_record_lookup(state)
        chunk_lookup = _build_chunk_lookup(state)
        self._strategies = _collect_strategies(
            state, sells_lookup, buys_lookup, chunk_lookup
        )
        self._decisions: list[StrategyDecision] = initial_decisions or []
        self._current_idx = start_idx

    # ── Lifecycle ────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        if not self._strategies:
            return  # compose shows the no-strategies notice
        if self._current_idx >= len(self._strategies):
            self._save_and_exit()
        else:
            self._push_current()

    def compose(self) -> ComposeResult:
        if not self._strategies:
            yield Static(
                "No strategies found in state JSON.\nRun cli.compute first.",
                id="no-strategies",
            )

    # ── Navigation ───────────────────────────────────────────────────────

    def _push_current(self) -> None:
        side, idx, strat, record, chunks = self._strategies[self._current_idx]
        position = (self._current_idx + 1, len(self._strategies))
        screen = PresenterScreen(side, idx, strat, record, chunks, position)
        self.push_screen(screen)

    def advance(self, decision: StrategyDecision) -> None:
        """Called by PresenterScreen when the user approves/skips."""
        self._decisions.append(decision)
        self._current_idx += 1
        self.pop_screen()
        if self._current_idx >= len(self._strategies):
            self._save_and_exit()
        else:
            self._push_current()

    def quit_and_save(self) -> None:
        """Called by PresenterScreen on Q — save partial plan and exit."""
        self._do_save()
        self.exit(0)

    def _save_and_exit(self) -> None:
        json_path, txt_path, fills_path = self._do_save()
        self.notify(
            f"Plan saved → {json_path.name}  +  {txt_path.name}  +  {fills_path.name}",
            timeout=5,
        )
        self.exit(0)

    def _do_save(self) -> tuple[Path, Path, Path]:
        return _save_plan(
            self._state,
            self._decisions,
            self._strategies,
            self._plans_dir,
        )


# ── CLI entry point ───────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Present rebalance strategies for human approval"
    )
    parser.add_argument("--plan", required=True, help="Path to engine state JSON")
    parser.add_argument("--resume", help="Path to partial plan JSON to resume from")
    parser.add_argument(
        "--plans-dir",
        default="plans",
        help="Directory to write approved plan files (default: ./plans)",
    )
    args = parser.parse_args()

    state_path = Path(resolve_path(args.plan))
    state = RebalanceState.model_validate_json(state_path.read_text(encoding="utf-8"))

    start_idx = 0
    initial_decisions: list[StrategyDecision] = []
    if args.resume:
        resume_path = Path(resolve_path(args.resume))
        plan = PlanOutput.model_validate_json(resume_path.read_text(encoding="utf-8"))
        initial_decisions = plan.decisions
        start_idx = len(initial_decisions)

    plans_dir = Path(resolve_output_path(args.plans_dir))
    app = RebalanceApp(state, plans_dir, start_idx, initial_decisions)
    app.run()


if __name__ == "__main__":
    main()
