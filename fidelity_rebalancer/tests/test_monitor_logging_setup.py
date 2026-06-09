"""
Validates B-11 — ``tui.monitor`` no longer carries its own copy of the logging
setup; it delegates to ``engine.observability.setup_logging`` (the shared seam),
writing to ``monitor.log``.

  1. Source: the duplicate ``_setup_logging`` is gone from monitor.py, and the
     module imports + calls ``observability.setup_logging(..., filename="monitor.log")``.
  2. Behaviour: ``observability.setup_logging(tmp, filename="monitor.log")`` — the
     exact call monitor makes — creates ``monitor.log`` and returns its path.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from engine import observability

_MONITOR_SRC = Path(__file__).parent.parent / "tui" / "monitor.py"


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """setup_logging mutates the root logger; snapshot and restore around tests."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


def test_monitor_drops_private_setup_logging():
    """The duplicated _setup_logging helper was removed from monitor.py."""
    src = _MONITOR_SRC.read_text(encoding="utf-8")
    assert "def _setup_logging" not in src


def test_monitor_delegates_to_observability():
    """monitor.py imports observability and calls its setup_logging for monitor.log."""
    src = _MONITOR_SRC.read_text(encoding="utf-8")
    assert "from engine import observability" in src
    assert "observability.setup_logging(" in src
    assert 'filename="monitor.log"' in src


def test_setup_logging_creates_monitor_log(tmp_path):
    """The shared seam monitor relies on writes monitor.log and returns its path."""
    log_path = observability.setup_logging(tmp_path, verbose=False, filename="monitor.log")
    assert log_path == (tmp_path / "monitor.log")
    assert log_path.exists()
    assert log_path.name == "monitor.log"
