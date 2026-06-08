"""
Textual screens for the per-strategy approval flow.

PresenterScreen  — shows one strategy; handles A/M/S/Q.
ModifyScreen     — inline modal for editing limit price / order type.
"""
from __future__ import annotations

from typing import Literal, Optional

from rich.panel import Panel
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Label, Static
from textual.containers import Container, Horizontal, Vertical

from state.schema import (
    BuyAllocationRecord,
    BuyStrategy,
    ChunkRecord,
    SellRecord,
    SellStrategy,
    StrategyDecision,
)


_URGENCY_COLOR = {"normal": "green", "patient": "yellow", "aggressive": "red"}


class ModifyScreen(ModalScreen[Optional[tuple[float, str]]]):
    """
    Modal for modifying a strategy's limit price (and optionally order type).
    Dismisses with (new_price, order_type) or None if cancelled.
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ModifyScreen {
        align: center middle;
    }
    ModifyScreen > Container {
        width: 60;
        height: auto;
        border: thick $primary;
        padding: 1 2;
    }
    ModifyScreen Label {
        margin: 0 0 1 0;
    }
    ModifyScreen Input {
        margin: 0 0 1 0;
    }
    ModifyScreen #warn {
        color: yellow;
        height: auto;
    }
    ModifyScreen #market-warn {
        color: red;
        height: auto;
    }
    ModifyScreen Horizontal {
        align-horizontal: center;
        height: auto;
        margin-top: 1;
    }
    ModifyScreen Button {
        margin: 0 1;
    }
    """

    def __init__(
        self,
        current_price: float,
        last_price: float,
        current_order_type: str = "LIMIT",
    ) -> None:
        super().__init__()
        self._current_price = current_price
        self._last_price = last_price
        self._current_order_type = current_order_type

    def compose(self) -> ComposeResult:
        with Container():
            yield Label("[b]Modify Order[/b]")
            yield Label(f"Current limit price: ${self._current_price:.4f}")
            if self._last_price > 0:
                yield Label(f"Last trade price:    ${self._last_price:.4f}")
            yield Label("New limit price (or type MARKET for a market order):")
            yield Input(
                value=f"{self._current_price:.4f}",
                id="price-input",
                placeholder="e.g. 62.3900",
            )
            yield Static("", id="warn")
            yield Static("", id="market-warn")
            with Horizontal():
                yield Button("Confirm", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_input_changed(self, event: Input.Changed) -> None:
        val = event.value.strip().upper()
        warn = self.query_one("#warn", Static)
        mwarn = self.query_one("#market-warn", Static)
        if val == "MARKET":
            warn.update("")
            mwarn.update("[b red]MARKET order selected — fills at whatever price the market offers.[/b red]")
            return
        mwarn.update("")
        try:
            price = float(val)
            if self._last_price > 0:
                pct = abs(price - self._last_price) / self._last_price
                if pct >= 0.05:
                    warn.update(
                        f"[yellow]⚠ Price is {pct*100:.1f}% from last — confirm intentional.[/yellow]"
                    )
                    return
            warn.update("")
        except ValueError:
            warn.update("[red]Not a valid number.[/red]")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        val = self.query_one("#price-input", Input).value.strip().upper()
        if val == "MARKET":
            self.dismiss((0.0, "MARKET"))
            return
        try:
            price = float(val)
        except ValueError:
            self.query_one("#warn", Static).update("[red]Enter a valid number or MARKET.[/red]")
            return
        self.dismiss((price, "LIMIT"))


class SkipReasonScreen(ModalScreen[Optional[str]]):
    """Tiny modal asking for an optional skip reason."""

    DEFAULT_CSS = """
    SkipReasonScreen {
        align: center middle;
    }
    SkipReasonScreen > Container {
        width: 60;
        height: auto;
        border: thick $primary;
        padding: 1 2;
    }
    SkipReasonScreen Horizontal {
        align-horizontal: center;
        height: auto;
        margin-top: 1;
    }
    SkipReasonScreen Button {
        margin: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Container():
            yield Label("[b]Skip strategy[/b]")
            yield Label("Reason (optional):")
            yield Input(placeholder="e.g. already entered manually", id="reason-input")
            with Horizontal():
                yield Button("Skip", variant="warning", id="ok")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        reason = self.query_one("#reason-input", Input).value.strip() or None
        self.dismiss(reason or "")


class PresenterScreen(Screen):
    """Displays one strategy (sell or buy) and handles approval."""

    BINDINGS = [
        Binding("a", "approve", "Approve", priority=True),
        Binding("m", "modify", "Modify", priority=True),
        Binding("s", "skip", "Skip", priority=True),
        Binding("q", "quit_app", "Quit", priority=True),
    ]

    DEFAULT_CSS = """
    PresenterScreen {
        layout: vertical;
    }
    #header {
        height: 3;
        background: $panel;
        padding: 0 2;
        border-bottom: solid $primary;
        content-align: center middle;
    }
    #body {
        layout: horizontal;
        height: 1fr;
    }
    #left {
        width: 1fr;
        height: 100%;
        padding: 1;
        border-right: solid $panel;
    }
    #right {
        width: 1fr;
        height: 100%;
        padding: 1;
    }
    #reasoning {
        height: auto;
        padding: 1 2;
        border-top: solid $panel;
    }
    PresenterScreen Footer {
        dock: bottom;
    }
    """

    def __init__(
        self,
        side: Literal["sell", "buy"],
        idx: int,
        strategy: SellStrategy | BuyStrategy,
        record: SellRecord | BuyAllocationRecord,
        chunks: list[ChunkRecord],
        position: tuple[int, int],
    ) -> None:
        super().__init__()
        self._side = side
        self._idx = idx
        self._strategy = strategy
        self._record = record
        self._chunks = chunks
        self._position = position
        self._pending_price = strategy.limit_price
        self._pending_order_type = "LIMIT"

    # ── Rendering ────────────────────────────────────────────────────────

    def _header_text(self) -> str:
        cur, total = self._position
        side_label = "SELL" if self._side == "sell" else "BUY "
        urgency = self._strategy.urgency
        color = _URGENCY_COLOR.get(urgency, "white")
        return (
            f"[bold]{cur}/{total}[/bold]  "
            f"{self._strategy.account}  │  "
            f"[bold]{side_label}[/bold] [bold cyan]{self._strategy.ticker}[/bold cyan]  │  "
            f"{self._strategy.strategy}  │  "
            f"urgency:[{color}]{urgency}[/{color}]"
        )

    def _left_panel(self) -> str:
        strat = self._strategy
        lines: list[str] = []
        lines.append("[bold underline]Execution Plan[/bold underline]")
        lines.append(f"Order type : {self._pending_order_type}")
        
        orig = getattr(strat, "original_limit_price", None)
        if orig and abs(orig - self._pending_price) > 1e-5:
            diff = self._pending_price - orig
            diff_str = f"+${diff:.4f}" if diff > 0 else f"-${abs(diff):.4f}"
            lines.append(f"Limit price: [bold red]${self._pending_price:.4f}[/bold red] (override: {diff_str} from engine ${orig:.4f})")
        else:
            lines.append(f"Limit price: [bold]${self._pending_price:.4f}[/bold]")
        
        lines.append(f"Rule       : {strat.rule}")

        if self._side == "sell":
            rec: SellRecord = self._record  # type: ignore[assignment]
            lines.append(f"Shares     : {rec.shares:,.0f}")
            lines.append(f"Est proceeds: ${rec.est_proceeds:,.2f}")
        else:
            rec2: BuyAllocationRecord = self._record  # type: ignore[assignment]
            lines.append(f"Share target: {rec2.share_target:,}")
            lines.append(f"Budget      : ${rec2.dollar_target:,.2f}")
            lines.append(f"Est cost    : ${rec2.est_cost:,.2f}")

        return "\n".join(lines)

    def _right_panel(self) -> str:
        lines: list[str] = ["[bold underline]Chunks[/bold underline]"]
        price = self._pending_price  # reflects any modification
        
        chunks_by_phase = {}
        for ch in self._chunks:
            phase = getattr(ch, "phase", "main")
            chunks_by_phase.setdefault(phase, []).append(ch)
            
        for phase, phase_chunks in chunks_by_phase.items():
            if len(chunks_by_phase) > 1 or phase != "main":
                lines.append(f"\n[italic]{phase.upper()} TRANCHE[/italic]")
            for ch in phase_chunks:
                cost = ch.shares * price
                gate_str = f" (gate: {ch.earliest_entry})" if getattr(ch, "earliest_entry", None) else ""
                lines.append(
                    f"[dim]{ch.chunk_id}[/dim]{gate_str}\n"
                    f"  {ch.shares:,.0f} sh × ${price:.4f} = ${cost:,.2f}"
                )
        if not self._chunks:
            lines.append("[dim](no chunks)[/dim]")
        return "\n".join(lines)

    def _reasoning_panel(self) -> str:
        bullets = "\n".join(f"• {b}" for b in self._strategy.reasoning)
        return f"[bold underline]Reasoning[/bold underline]\n{bullets}"

    # ── Compose ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static(self._header_text(), id="header")
        with Horizontal(id="body"):
            yield Static(self._left_panel(), id="left")
            yield Static(self._right_panel(), id="right")
        yield Static(self._reasoning_panel(), id="reasoning")
        yield Footer()

    # ── Actions ──────────────────────────────────────────────────────────

    def action_approve(self) -> None:
        decision = StrategyDecision(
            side=self._side,
            idx=self._idx,
            approval_status="approved",
            approved_limit_price=self._pending_price,
            approved_order_type=self._pending_order_type,
        )
        self.app.advance(decision)  # type: ignore[attr-defined]

    def action_modify(self) -> None:
        last = 0.0  # no live quote in the presenter; sanity check still fires
        self.app.push_screen(
            ModifyScreen(self._pending_price, last, self._pending_order_type),
            self._on_modify_result,
        )

    def _on_modify_result(self, result: Optional[tuple[float, str]]) -> None:
        if result is None:
            return
        new_price, order_type = result
        original = self._strategy.limit_price
        
        if getattr(self._strategy, "original_limit_price", None) is None:
            self._strategy.original_limit_price = original
            
        self._pending_price = new_price if order_type == "LIMIT" else original
        self._pending_order_type = order_type
        self.query_one("#left", Static).update(self._left_panel())
        self.query_one("#right", Static).update(self._right_panel())

    def action_skip(self) -> None:
        self.app.push_screen(SkipReasonScreen(), self._on_skip_result)

    def _on_skip_result(self, result: Optional[str]) -> None:
        if result is None:
            return
        decision = StrategyDecision(
            side=self._side,
            idx=self._idx,
            approval_status="skipped",
            approved_limit_price=self._strategy.limit_price,
            skip_reason=result or None,
        )
        self.app.advance(decision)  # type: ignore[attr-defined]

    def action_quit_app(self) -> None:
        self.app.quit_and_save()  # type: ignore[attr-defined]
