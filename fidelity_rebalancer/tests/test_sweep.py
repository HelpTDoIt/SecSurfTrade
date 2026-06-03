"""
Tests for engine.sweep.should_sweep — the EOD-sweep predicate shared by the
live-recompute trigger (F-1) and the absorption scheduler (S-1).

The predicate is ``clock OR fill-fraction``.  F-1 wires it clock-only by
passing ``unfilled_frac=0.0``; S-1 will pass a live fraction.
"""
from __future__ import annotations

from engine.sweep import should_sweep


# ── Clock half ─────────────────────────────────────────────────────────────


def test_clock_fires_at_cutoff():
    assert should_sweep(330, 0.0, sweep_time_minutes=330, sweep_unfilled_frac=0.5)


def test_clock_fires_past_cutoff():
    assert should_sweep(390, 0.0, sweep_time_minutes=330, sweep_unfilled_frac=0.5)


def test_clock_silent_before_cutoff():
    assert not should_sweep(329, 0.0, sweep_time_minutes=330, sweep_unfilled_frac=0.5)


def test_none_minutes_never_fires_clock():
    # Outside the session (None) the clock half is disabled; frac half also off.
    assert not should_sweep(None, 0.0, sweep_time_minutes=330, sweep_unfilled_frac=0.5)


# ── Fill-fraction half ─────────────────────────────────────────────────────


def test_frac_fires_at_threshold():
    # Before the clock, but half the book is unfilled → sweep.
    assert should_sweep(100, 0.5, sweep_time_minutes=330, sweep_unfilled_frac=0.5)


def test_frac_silent_below_threshold():
    assert not should_sweep(100, 0.49, sweep_time_minutes=330, sweep_unfilled_frac=0.5)


# ── F-1 wiring: clock-only (unfilled_frac pinned to 0.0) ───────────────────


def test_f1_clock_only_silent_before_cutoff():
    # F-1 passes 0.0; with a 0.5 threshold the frac half can never fire, so a
    # mid-session poll with everything unfilled still does NOT sweep on frac.
    assert not should_sweep(120, 0.0, sweep_time_minutes=330, sweep_unfilled_frac=0.5)


def test_f1_clock_only_fires_on_clock():
    assert should_sweep(330, 0.0, sweep_time_minutes=330, sweep_unfilled_frac=0.5)
