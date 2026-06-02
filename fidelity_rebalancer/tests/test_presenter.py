"""
Tests for tui.presenter and tui.app using Textual's async pilot.

Acceptance criteria:
- App opens, displays first strategy, accepts keypresses.
- A → advances to next strategy.
- Q → exits cleanly, saves partial plan.
- Full approval → saves plan JSON + TXT.
- Skipped strategies absent from TXT.
- Modify flow → plan retains original + override.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from state.schema import RebalanceState

FIXTURE = _ROOT / "tests" / "fixtures" / "feb27_with_strategies.json"


def _load_state() -> RebalanceState:
    return RebalanceState.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_app(tmp_path: Path, state: RebalanceState | None = None):
    from tui.app import RebalanceApp

    s = state or _load_state()
    return RebalanceApp(s, plans_dir=tmp_path / "plans")


# ── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_app_renders_first_strategy(tmp_path: Path):
    """App opens and the first strategy header is visible."""
    app = _make_app(tmp_path)
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        all_text = " ".join(str(w.content) for w in app.screen.query("Static"))
        assert "EEM" in all_text
        assert "Test Retirement" in all_text


@pytest.mark.anyio
async def test_approve_advances_to_next_strategy(tmp_path: Path):
    """Pressing A advances from the sell strategy to the first buy strategy."""
    app = _make_app(tmp_path)
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        before = " ".join(str(w.content) for w in app.screen.query("Static"))
        assert "EEM" in before

        await pilot.press("a")
        await pilot.pause()
        after = " ".join(str(w.content) for w in app.screen.query("Static"))
        assert "EWY" in after


@pytest.mark.anyio
async def test_quit_saves_partial_plan(tmp_path: Path):
    """Q exits and writes a plan JSON even if not all strategies reviewed."""
    app = _make_app(tmp_path)
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
    # Plan file should exist
    plans_dir = tmp_path / "plans"
    jsons = list(plans_dir.glob("plan_*.json"))
    assert len(jsons) >= 1


@pytest.mark.anyio
async def test_full_approval_writes_json_and_txt(tmp_path: Path):
    """Approving all strategies writes plan JSON + TXT."""
    app = _make_app(tmp_path)
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        # 3 strategies total (1 sell + 2 buys)
        for _ in range(3):
            await pilot.press("a")
            await pilot.pause()

    plans_dir = tmp_path / "plans"
    jsons = list(plans_dir.glob("plan_*.json"))
    txts = list(plans_dir.glob("plan_*.txt"))
    assert len(jsons) >= 1
    assert len(txts) >= 1

    # Validate JSON parses
    from state.schema import PlanOutput

    plan = PlanOutput.model_validate_json(jsons[0].read_text(encoding="utf-8"))
    assert len(plan.decisions) == 3
    assert all(d.approval_status == "approved" for d in plan.decisions)

    # TXT should contain all approved chunks
    txt = txts[0].read_text(encoding="utf-8")
    assert "EEM" in txt
    assert "EWY" in txt
    assert "SMH" in txt


@pytest.mark.anyio
async def test_skip_excludes_from_txt(tmp_path: Path):
    """Skipped strategy appears in JSON as 'skipped' but not in TXT."""
    app = _make_app(tmp_path)
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        # Skip first (SELL EEM), approve the rest
        await pilot.press("s")
        await pilot.pause()
        # SkipReasonScreen appears — confirm with Enter (empty reason)
        await pilot.press("enter")
        await pilot.pause()
        for _ in range(2):
            await pilot.press("a")
            await pilot.pause()

    plans_dir = tmp_path / "plans"
    jsons = list(plans_dir.glob("plan_*.json"))
    txts = list(plans_dir.glob("plan_*.txt"))
    assert len(jsons) >= 1
    assert len(txts) >= 1

    from state.schema import PlanOutput

    plan = PlanOutput.model_validate_json(jsons[0].read_text(encoding="utf-8"))
    sell_decision = next(d for d in plan.decisions if d.side == "sell")
    assert sell_decision.approval_status == "skipped"

    txt = txts[0].read_text(encoding="utf-8")
    # EEM sell should not be in TXT (skipped)
    assert "EEM" not in txt
    # EWY and SMH should be there (approved)
    assert "EWY" in txt
    assert "SMH" in txt


@pytest.mark.anyio
async def test_modify_opens_and_cancel_returns_to_presenter(tmp_path: Path):
    """M opens the modify modal; cancelling returns without changing the strategy."""
    app = _make_app(tmp_path)
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        original_price = app._strategies[0][2].limit_price  # type: ignore[attr-defined]

        await pilot.press("m")
        await pilot.pause()
        # Cancel the modify modal
        await pilot.press("escape")
        await pilot.pause()

        # Approve all 3 strategies
        for _ in range(3):
            await pilot.press("a")
            await pilot.pause()

    plans_dir = tmp_path / "plans"
    jsons = list(plans_dir.glob("plan_*.json"))
    from state.schema import PlanOutput

    plan = PlanOutput.model_validate_json(jsons[0].read_text(encoding="utf-8"))
    sell_dec = next(d for d in plan.decisions if d.side == "sell")
    # Cancel did not change the price
    assert sell_dec.approved_limit_price == pytest.approx(original_price)


@pytest.mark.anyio
async def test_plan_json_round_trips(tmp_path: Path):
    """PlanOutput serializes and deserializes without loss."""
    app = _make_app(tmp_path)
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()

    plans_dir = tmp_path / "plans"
    jsons = list(plans_dir.glob("plan_*.json"))
    from state.schema import PlanOutput

    raw = jsons[0].read_text(encoding="utf-8")
    plan1 = PlanOutput.model_validate_json(raw)
    raw2 = plan1.model_dump_json(indent=2)
    plan2 = PlanOutput.model_validate_json(raw2)
    assert plan1.model_dump() == plan2.model_dump()
