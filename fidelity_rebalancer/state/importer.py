"""
Load/save canonical state JSON; normalize field names from React calc export.
"""
from __future__ import annotations

import json
from pathlib import Path

from .schema import RebalanceState


def load_state(path: str | Path) -> RebalanceState:
    """Load a state JSON from disk and validate against the canonical schema."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return RebalanceState.model_validate(data)


def save_state(state: RebalanceState, path: str | Path) -> None:
    """Serialize a RebalanceState to JSON and write to disk."""
    Path(path).write_text(
        state.model_dump_json(indent=2),
        encoding="utf-8",
    )
