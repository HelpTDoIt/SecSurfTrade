"""Tests for cli.eod_report -- pure-function coverage only (no I/O in assertions).

Run from fidelity_rebalancer/:
    $env:PYTHONPATH="."; python -m pytest tests/test_eod_report.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from cli.eod_report import (
    JournalSummary,
    _fmt_duration,
    format_report,
    load_journal,
    summarize,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, lines: list) -> None:
    """Write a list of objects (or raw strings) to a JSONL file."""
    with path.open("w", encoding="utf-8") as f:
        for item in lines:
            if isinstance(item, str):
                f.write(item + "\n")
            else:
                f.write(json.dumps(item) + "\n")


def _entry(ts: str, event_type: str, payload: dict | None = None) -> dict:
    return {"ts": ts, "event_type": event_type, "payload": payload or {}}


# ---------------------------------------------------------------------------
# load_journal
# ---------------------------------------------------------------------------


class TestLoadJournal:
    def test_empty_file_returns_no_entries(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("", encoding="utf-8")
        entries, n_bad = load_journal([str(f)])
        assert entries == []
        assert n_bad == 0

    def test_malformed_line_skipped_and_counted(self, tmp_path):
        f = tmp_path / "mixed.jsonl"
        _write_jsonl(
            f,
            [
                _entry("2026-05-01T10:00:00+00:00", "poll"),
                "THIS IS NOT JSON",
                _entry("2026-05-01T10:01:00+00:00", "heartbeat"),
            ],
        )
        entries, n_bad = load_journal([str(f)])
        assert n_bad == 1
        assert len(entries) == 2

    def test_out_of_order_lines_sorted_ascending(self, tmp_path):
        f = tmp_path / "unordered.jsonl"
        _write_jsonl(
            f,
            [
                _entry("2026-05-01T10:05:00+00:00", "poll"),
                _entry("2026-05-01T09:00:00+00:00", "monitor_start"),
                _entry("2026-05-01T10:03:00+00:00", "heartbeat"),
            ],
        )
        entries, _ = load_journal([str(f)])
        tss = [e["ts"] for e in entries]
        assert tss == sorted(tss)

    def test_multi_file_merge_sorted(self, tmp_path):
        f1 = tmp_path / "a.jsonl"
        f2 = tmp_path / "b.jsonl"
        _write_jsonl(
            f1,
            [
                _entry("2026-05-01T09:00:00+00:00", "monitor_start"),
                _entry("2026-05-01T09:02:00+00:00", "poll"),
            ],
        )
        _write_jsonl(
            f2,
            [
                _entry("2026-05-01T09:01:00+00:00", "heartbeat"),
                _entry("2026-05-01T09:03:00+00:00", "stall_detected"),
            ],
        )
        entries, n_bad = load_journal([str(f1), str(f2)])
        assert n_bad == 0
        assert len(entries) == 4
        tss = [e["ts"] for e in entries]
        assert tss == sorted(tss)

    def test_missing_required_key_is_malformed(self, tmp_path):
        f = tmp_path / "bad.jsonl"
        f.write_text(
            json.dumps({"ts": "2026-05-01T10:00:00+00:00"}) + "\n",
            encoding="utf-8",
        )
        entries, n_bad = load_journal([str(f)])
        assert n_bad == 1
        assert entries == []

    def test_blank_lines_ignored(self, tmp_path):
        f = tmp_path / "blanks.jsonl"
        content = (
            json.dumps(_entry("2026-05-01T10:00:00+00:00", "poll")) + "\n"
            "\n"
            "\n" + json.dumps(_entry("2026-05-01T10:01:00+00:00", "heartbeat")) + "\n"
        )
        f.write_text(content, encoding="utf-8")
        entries, n_bad = load_journal([str(f)])
        assert len(entries) == 2
        assert n_bad == 0


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


class TestSummarize:
    def test_empty_entries_returns_empty_summary(self):
        s = summarize([])
        assert s.first_ts is None
        assert s.last_ts is None
        assert s.duration_seconds is None
        assert s.event_counts == {}
        assert s.notable_events == []

    def test_single_entry_zero_duration(self):
        entries = [_entry("2026-05-01T10:00:00+00:00", "monitor_start")]
        s = summarize(entries)
        assert s.first_ts == "2026-05-01T10:00:00+00:00"
        assert s.last_ts == "2026-05-01T10:00:00+00:00"
        assert s.duration_seconds == 0.0

    def test_event_counts_correct(self):
        entries = [
            _entry("2026-05-01T10:00:00+00:00", "poll"),
            _entry("2026-05-01T10:01:00+00:00", "poll"),
            _entry("2026-05-01T10:02:00+00:00", "heartbeat"),
            _entry("2026-05-01T10:03:00+00:00", "stall_detected"),
        ]
        s = summarize(entries)
        assert s.event_counts["poll"] == 2
        assert s.event_counts["heartbeat"] == 1
        assert s.event_counts["stall_detected"] == 1

    def test_duration_computed_correctly(self):
        entries = [
            _entry("2026-05-01T10:00:00+00:00", "monitor_start"),
            _entry("2026-05-01T10:30:00+00:00", "recompute_trigger"),
        ]
        s = summarize(entries)
        assert s.duration_seconds == 1800.0  # 30 minutes

    def test_notable_events_collected(self):
        entries = [
            _entry("2026-05-01T10:00:00+00:00", "poll"),
            _entry("2026-05-01T10:01:00+00:00", "stall_detected", {"chunk_id": "s1"}),
            _entry("2026-05-01T10:02:00+00:00", "heartbeat"),
            _entry(
                "2026-05-01T10:03:00+00:00", "requote_confirmed", {"chunk_id": "s1"}
            ),
        ]
        s = summarize(entries)
        notable_types = [e["event_type"] for e in s.notable_events]
        assert "stall_detected" in notable_types
        assert "requote_confirmed" in notable_types
        assert "poll" not in notable_types
        assert "heartbeat" not in notable_types

    def test_poll_errors_captured(self):
        entries = [
            _entry("2026-05-01T10:00:00+00:00", "poll_error", {"error": "timeout"}),
            _entry("2026-05-01T10:01:00+00:00", "poll"),
        ]
        s = summarize(entries)
        assert len(s.poll_errors) == 1
        assert s.poll_errors[0]["payload"]["error"] == "timeout"


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


class TestFormatReport:
    def test_empty_summary_does_not_crash(self):
        s = summarize([])
        report = format_report(s, 0)
        assert "EOD TRADE JOURNAL REPORT" in report
        assert "no entries" in report

    def test_malformed_count_shown_when_nonzero(self):
        s = summarize([])
        report = format_report(s, 3)
        assert "3" in report
        assert "Malformed" in report

    def test_malformed_count_absent_when_zero(self):
        entries = [_entry("2026-05-01T10:00:00+00:00", "poll")]
        s = summarize(entries)
        report = format_report(s, 0)
        assert "Malformed" not in report

    def test_event_tally_present(self):
        entries = [
            _entry("2026-05-01T10:00:00+00:00", "poll"),
            _entry("2026-05-01T10:01:00+00:00", "poll"),
            _entry("2026-05-01T10:02:00+00:00", "heartbeat"),
        ]
        s = summarize(entries)
        report = format_report(s, 0)
        assert "poll" in report
        assert "heartbeat" in report

    def test_poll_error_warnings_section_present(self):
        entries = [
            _entry(
                "2026-05-01T10:00:00+00:00", "poll_error", {"error": "conn_refused"}
            ),
        ]
        s = summarize(entries)
        report = format_report(s, 0)
        assert "WARNINGS" in report
        assert "conn_refused" in report

    def test_no_warnings_section_when_no_poll_errors(self):
        entries = [_entry("2026-05-01T10:00:00+00:00", "poll")]
        s = summarize(entries)
        report = format_report(s, 0)
        assert "WARNINGS" not in report

    def test_file_labels_shown_in_header(self):
        s = summarize([])
        report = format_report(s, 0, file_labels=["logs/journal_abc.jsonl"])
        assert "journal_abc.jsonl" in report

    def test_duration_shown_correctly(self):
        entries = [
            _entry("2026-05-01T09:30:00+00:00", "monitor_start"),
            _entry("2026-05-01T10:30:00+00:00", "recompute_trigger"),
        ]
        s = summarize(entries)
        report = format_report(s, 0)
        assert "01:00:00" in report  # 1 hour exactly


# ---------------------------------------------------------------------------
# Payload-drift tolerance
# ---------------------------------------------------------------------------


class TestPayloadDrift:
    def test_recompute_trigger_with_actual_proceeds(self):
        """Drift: field is 'actual_proceeds' not 'proceeds'."""
        entries = [
            _entry(
                "2026-05-01T10:00:00+00:00",
                "recompute_trigger",
                {"account": "Roth IRA", "actual_proceeds": 103255.45},
            )
        ]
        s = summarize(entries)
        report = format_report(s, 0)
        # Should surface the value, not KeyError
        assert "recompute_trigger" in report
        assert "103,255.45" in report or "103255" in report

    def test_requote_confirmed_with_original_and_new_chunk_aliases(self):
        """Drift: uses original_chunk/new_chunk instead of chunk_id."""
        entries = [
            _entry(
                "2026-05-01T10:00:00+00:00",
                "requote_confirmed",
                {
                    "original_chunk": "s2",
                    "new_chunk": "s2b",
                    "new_limit": 62.38,
                    "remaining_qty": 25,
                },
            )
        ]
        s = summarize(entries)
        report = format_report(s, 0)
        assert "requote_confirmed" in report
        assert "s2" in report
        assert "s2b" in report

    def test_stall_detected_with_chunk_id_key(self):
        """Original schema: chunk_id key."""
        entries = [
            _entry(
                "2026-05-01T10:00:00+00:00",
                "stall_detected",
                {"chunk_id": "s1", "seconds_stalled": 300.0, "remaining_qty": 10},
            )
        ]
        s = summarize(entries)
        report = format_report(s, 0)
        assert "s1" in report
        assert "300" in report

    def test_mixed_malformed_and_valid_with_drift(self, tmp_path):
        """Integration: file has a bad line, valid entries with alias drift all render."""
        f = tmp_path / "drift.jsonl"
        _write_jsonl(
            f,
            [
                _entry("2026-05-01T09:00:00+00:00", "monitor_start"),
                "NOT_JSON",
                _entry(
                    "2026-05-01T09:05:00+00:00",
                    "recompute_trigger",
                    {"account": "IRA", "actual_proceeds": 50000.0},
                ),
                _entry(
                    "2026-05-01T09:10:00+00:00",
                    "requote_confirmed",
                    {"original_chunk": "s1", "new_chunk": "s1b", "new_limit": 55.0},
                ),
            ],
        )
        entries, n_bad = load_journal([str(f)])
        assert n_bad == 1
        assert len(entries) == 3

        s = summarize(entries)
        report = format_report(s, n_bad)
        assert "Malformed" in report
        assert "recompute_trigger" in report
        assert "s1b" in report


# ---------------------------------------------------------------------------
# _fmt_duration helper
# ---------------------------------------------------------------------------


class TestFmtDuration:
    def test_none_returns_na(self):
        assert _fmt_duration(None) == "N/A"

    def test_zero(self):
        assert _fmt_duration(0) == "00:00:00"

    def test_one_hour(self):
        assert _fmt_duration(3600) == "01:00:00"

    def test_mixed(self):
        assert _fmt_duration(3661) == "01:01:01"


# ---------------------------------------------------------------------------
# Real fixture smoke test
# ---------------------------------------------------------------------------


class TestRealFixture:
    """Run against the checked-in demo journal. Verifies the real payload shapes."""

    _FIXTURE = _ROOT / "logs" / "journal_e2e_demo.jsonl"

    def test_fixture_loads_without_errors(self):
        if not self._FIXTURE.exists():
            import pytest

            pytest.skip("fixture not found")
        entries, n_bad = load_journal([str(self._FIXTURE)])
        assert n_bad == 0
        assert len(entries) == 6

    def test_fixture_summary_sanity(self):
        if not self._FIXTURE.exists():
            import pytest

            pytest.skip("fixture not found")
        entries, n_bad = load_journal([str(self._FIXTURE)])
        s = summarize(entries)
        assert s.first_ts is not None
        assert s.last_ts is not None
        assert s.event_counts.get("poll", 0) == 2
        assert s.event_counts.get("stall_detected", 0) == 1
        assert s.event_counts.get("requote_suggested", 0) == 1
        assert s.event_counts.get("requote_confirmed", 0) == 1
        assert s.event_counts.get("recompute_trigger", 0) == 1

    def test_fixture_report_renders(self):
        if not self._FIXTURE.exists():
            import pytest

            pytest.skip("fixture not found")
        entries, n_bad = load_journal([str(self._FIXTURE)])
        s = summarize(entries)
        report = format_report(s, n_bad)
        # Spot-check key content
        assert "recompute_trigger" in report
        assert "requote_confirmed" in report
        # actual_proceeds alias in fixture
        assert "103,255.45" in report or "103255" in report
        # original_chunk/new_chunk aliases in fixture
        assert "s2" in report
