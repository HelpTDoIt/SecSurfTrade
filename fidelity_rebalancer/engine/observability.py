"""
Neutral observability for the strategy/compute engine.

Two independent mechanisms, both quiet by default so importing the engine or
calling its pure functions in unit tests has *no* side effects:

1. Leveled stdlib logging  ->  ``logs/strategy.log`` (+ console when verbose).
   ``setup_logging(log_dir, *, verbose, filename)`` configures the root logger;
   engine modules just use ``logging.getLogger(__name__)``.

2. Structured decision log ->  ``logs/decisions.jsonl`` (append-only JSONL).
   ``enable_decision_log(path)`` / ``record(event_type, payload)`` /
   ``disable_decision_log()``.  ``record`` is inert until enabled, so the pure
   generators stay side-effect-free in tests but emit a full per-ticker
   decision trail when the CLI turns the log on.

Why a new module and not ``tui.monitor.Journal``?  Dependency direction is
``tui -> engine -> state/adapters``; the engine must not import ``tui``.  This
is the engine's neutral equivalent of the live monitor's Journal, deliberately
dependency-free.

Sensitive-data policy (mirrors the ``--verbose`` rationale in cli/strategy.py):

  * INFO  — operational only: phase markers, counts, summaries.  No tickers,
            account names, share counts, or dollar amounts.
  * WARNING/ERROR — may name a ticker for an actionable data-quality alert
            (e.g. "ADV unavailable for EEM"), but never share counts, dollar
            amounts, or account names.
  * DEBUG — full per-decision detail (ticker, limit, shares, features,
            account).  Only reaches the file/console under ``verbose``.
  * decisions.jsonl — full structured detail incl. sizing; lives in the
            gitignored ``logs/`` dir, the same trust level as the live
            monitor's ``journal.jsonl``.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

_PathLike = Union[str, Path]


# ── 1. Leveled stdlib logging ──────────────────────────────────────────────


def setup_logging(
    log_dir: _PathLike,
    *,
    verbose: bool = False,
    filename: str = "strategy.log",
) -> Path:
    """Configure root logging: always a file handler, console only when verbose.

    Generalised from ``tui.monitor._setup_logging``.  The file handler always
    accepts DEBUG; the *root level* gates what reaches it — INFO by default,
    DEBUG when ``verbose`` (which also echoes to stderr).  This is what keeps
    the per-ticker DEBUG detail out of the default log: it is filtered at the
    root before ever touching the file.

    Idempotent: existing handlers are cleared first, so repeated calls (across
    CLI steps or test cases) don't accumulate duplicate output.

    Returns the path to the log file.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_path = log_dir / filename
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)  # file accepts everything; root level gates it

    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(fh)

    if verbose:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.setLevel(logging.DEBUG)
        root.addHandler(ch)

    return log_path


# ── 2. Structured decision log (JSONL) ─────────────────────────────────────


class _DecisionLog:
    """Append-only JSONL sink, inert until enabled.  Thread-safe."""

    def __init__(self) -> None:
        self._path: Optional[Path] = None
        self._lock = threading.Lock()

    def enable(self, path: _PathLike) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch(mode=0o600, exist_ok=True)
        with self._lock:
            self._path = p
        return p

    def disable(self) -> None:
        with self._lock:
            self._path = None

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def record(self, event_type: str, payload: dict) -> None:
        with self._lock:
            path = self._path
            if path is None:
                return  # inert until enabled — no-op for pure callers/tests
            entry = {
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "event_type": event_type,
                "payload": payload,
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")


_decision_log = _DecisionLog()


def enable_decision_log(path: _PathLike) -> Path:
    """Start writing structured decision records to ``path`` (JSONL, append).

    Returns the resolved path.  Safe to call more than once (last path wins).
    """
    return _decision_log.enable(path)


def disable_decision_log() -> None:
    """Stop recording.  Subsequent ``record`` calls become no-ops."""
    _decision_log.disable()


def decision_log_enabled() -> bool:
    """True when a destination is set (``record`` will write)."""
    return _decision_log.enabled


def record(event_type: str, payload: dict) -> None:
    """Append one structured record iff the decision log is enabled, else no-op.

    Safe to call from pure engine functions: with the log disabled — the
    default, and the state in unit tests — this does nothing, so those
    functions stay side-effect-free.
    """
    _decision_log.record(event_type, payload)
