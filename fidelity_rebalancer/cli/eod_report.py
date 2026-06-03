"""
End-of-day trade-journal report.

Reads the append-only JSONL audit log(s) written by tui/monitor.py and prints
a human-readable post-session summary.  This tool NEVER places trades.

Usage (from SecSurfTrade/ or fidelity_rebalancer/):
    python -m fidelity_rebalancer.cli.eod_report --journal logs/journal*.jsonl
    python -m cli.eod_report --journal logs/journal_e2e_demo.jsonl
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from cli import resolve_path

# High-frequency routine events EXCLUDED from the notable-events timeline.
# Everything else -- including unknown / future event types -- is treated as
# notable, so a newly-introduced event type can never silently vanish from the
# timeline (the journal schema has drifted before; e.g. requote_suggested was
# not in the monitor source yet showed up in real logs).
_ROUTINE = {"heartbeat", "poll"}


# ---------------------------------------------------------------------------
# Pure data functions
# ---------------------------------------------------------------------------


def load_journal(paths: list[str]) -> tuple[list[dict], int, list[str]]:
    """
    Read one or more JSONL files and return (entries, n_malformed, unreadable).

    entries  -- list of dicts, each {"ts": str, "event_type": str, "payload": dict},
                sorted ascending by ts string (ISO-8601 lexicographic sort is safe
                as long as every record uses the same timezone offset, which the
                monitor guarantees by writing UTC).
    n_malformed -- count of LINES that could not be parsed as JSON or were
                   missing required keys.
    unreadable  -- paths of FILES that could not be opened at all (distinct from
                   a bad line inside an otherwise-readable file).
    """
    entries: list[dict] = []
    n_malformed = 0
    unreadable: list[str] = []

    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            unreadable.append(path)
            continue

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("not a dict")
                # Require at least ts and event_type
                if "ts" not in obj or "event_type" not in obj:
                    raise ValueError("missing required keys")
                entries.append(obj)
            except (json.JSONDecodeError, ValueError):
                n_malformed += 1

    entries.sort(key=lambda e: e.get("ts", ""))
    return entries, n_malformed, unreadable


@dataclass
class JournalSummary:
    first_ts: str | None = None
    last_ts: str | None = None
    duration_seconds: float | None = None
    event_counts: dict[str, int] = field(default_factory=dict)
    notable_events: list[dict] = field(
        default_factory=list
    )  # raw entries, everything except _ROUTINE noise
    poll_errors: list[dict] = field(default_factory=list)


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; return None on failure."""
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _to_local_display(ts: str) -> str:
    """Format an ISO-8601 timestamp in the machine's LOCAL time, with a tz label.

    The journal stores UTC; a human reading the EOD report wants local wall-clock
    (a 9:30 ET open should read 09:30, not 13:30). Falls back to the raw trimmed
    string if the timestamp cannot be parsed.
    """
    dt = _parse_iso(ts)
    if dt is None:
        return (ts or "")[:19].replace("T", " ")
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def summarize(entries: list[dict]) -> JournalSummary:
    """
    Derive a JournalSummary from a sorted list of journal entries.
    Pure function -- no I/O.
    """
    s = JournalSummary()

    if not entries:
        return s

    s.first_ts = entries[0].get("ts")
    s.last_ts = entries[-1].get("ts")

    t0 = _parse_iso(s.first_ts)
    t1 = _parse_iso(s.last_ts)
    if t0 is not None and t1 is not None:
        s.duration_seconds = (t1 - t0).total_seconds()

    for entry in entries:
        etype = entry.get("event_type", "unknown")
        s.event_counts[etype] = s.event_counts.get(etype, 0) + 1
        if etype not in _ROUTINE:
            s.notable_events.append(entry)
            if etype == "poll_error":
                s.poll_errors.append(entry)

    return s


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "N/A"
    total = int(abs(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    sec = total % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _chunk_id_from_payload(payload: dict) -> str:
    """Extract a chunk identifier using all known aliases."""
    for key in ("chunk_id", "original_chunk", "new_chunk"):
        val = payload.get(key)
        if val is not None:
            return str(val)
    return ""


def _proceeds_from_payload(payload: dict) -> str:
    """Extract proceeds using all known aliases."""
    for key in ("proceeds", "actual_proceeds"):
        val = payload.get(key)
        if val is not None:
            return f"${val:,.2f}"
    return ""


def _notable_line(entry: dict) -> str:
    """Format one notable event as a single ASCII line."""
    ts = _to_local_display(entry.get("ts", ""))
    etype = entry.get("event_type", "unknown")
    payload = entry.get("payload", {})

    parts: list[str] = [f"{ts}  {etype:<22}"]

    if etype == "stall_detected":
        cid = _chunk_id_from_payload(payload)
        secs = payload.get("seconds_stalled", "")
        if cid:
            parts.append(f"chunk={cid}")
        if secs != "":
            parts.append(f"stalled={secs}s")
        rem = payload.get("remaining_qty")
        if rem is not None:
            parts.append(f"remaining={rem}")

    elif etype == "stall_ignored":
        cid = _chunk_id_from_payload(payload)
        if cid:
            parts.append(f"chunk={cid}")

    elif etype == "requote_suggested":
        cid = _chunk_id_from_payload(payload)
        orig = payload.get("original_limit")
        new = payload.get("new_limit")
        if cid:
            parts.append(f"chunk={cid}")
        if orig is not None:
            parts.append(f"orig_limit=${orig}")
        if new is not None:
            parts.append(f"new_limit=${new}")

    elif etype == "requote_confirmed":
        # chunk aliases: chunk_id OR original_chunk/new_chunk
        orig_c = payload.get("chunk_id") or payload.get("original_chunk", "")
        new_c = payload.get("new_chunk", "")
        new_limit = payload.get("new_limit")
        rem = payload.get("remaining_qty")
        if orig_c:
            parts.append(f"orig={orig_c}")
        if new_c:
            parts.append(f"new={new_c}")
        if new_limit is not None:
            parts.append(f"limit=${new_limit}")
        if rem is not None:
            parts.append(f"remaining={rem}")

    elif etype == "recompute_trigger":
        account = payload.get("account", "")
        proc = _proceeds_from_payload(payload)
        if account:
            parts.append(f"account={account}")
        if proc:
            parts.append(f"proceeds={proc}")

    elif etype == "recompute_buys":
        account = payload.get("account", "")
        proc = _proceeds_from_payload(payload)
        trig = payload.get("trigger", "")
        if account:
            parts.append(f"account={account}")
        if trig:
            parts.append(f"trigger={trig}")
        if proc:
            parts.append(f"proceeds={proc}")
        after = payload.get("after")
        if isinstance(after, dict) and after:
            tgt = " ".join(f"{k}={v}" for k, v in after.items())
            parts.append(f"targets[{tgt}]")

    elif etype == "poll_error":
        err = payload.get("error", "")
        if err:
            parts.append(f"error={err!r}")

    else:
        # Unknown / un-templated event type (e.g. monitor_start, or a brand-new
        # event the monitor starts logging): show the raw payload compactly so it
        # is never silently dropped from the timeline.
        compact = json.dumps(payload, separators=(",", ":"))
        if compact == "{}":
            compact = "(no payload)"
        elif len(compact) > 80:
            compact = compact[:77] + "..."
        parts.append(compact)

    # rstrip() trims the column padding when an event has no detail fields, so
    # bare lines carry no invisible trailing whitespace.
    return "  ".join(parts).rstrip()


def format_report(
    summary: JournalSummary,
    n_malformed: int,
    file_labels: list[str] | None = None,
    unreadable: list[str] | None = None,
) -> str:
    """
    Render summary as a printable ASCII text block.
    Pure function -- no I/O.
    """
    lines: list[str] = []
    sep = "-" * 64

    # ----- Header -----
    lines.append(sep)
    lines.append("  EOD TRADE JOURNAL REPORT")
    lines.append(sep)

    if file_labels:
        for label in file_labels:
            lines.append(f"  File : {label}")
    else:
        lines.append("  File : (none)")

    total_entries = sum(summary.event_counts.values())
    lines.append(f"  Lines read   : {total_entries + n_malformed}")
    lines.append(f"  Valid entries: {total_entries}")
    if n_malformed:
        lines.append(f"  Malformed lines (skipped): {n_malformed}")
    if unreadable:
        for path in unreadable:
            lines.append(f"  Unreadable file (skipped): {path}")

    # ----- Session span -----
    lines.append("")
    lines.append("SESSION SPAN")
    lines.append(sep)
    if summary.first_ts is None:
        lines.append("  (no entries)")
    else:
        lines.append(f"  Start   : {_to_local_display(summary.first_ts)}")
        lines.append(f"  End     : {_to_local_display(summary.last_ts)}")
        lines.append(f"  Duration: {_fmt_duration(summary.duration_seconds)}")

    # ----- Event tally -----
    lines.append("")
    lines.append("EVENT TALLY")
    lines.append(sep)
    if not summary.event_counts:
        lines.append("  (no events)")
    else:
        sorted_counts = sorted(
            summary.event_counts.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )
        for etype, count in sorted_counts:
            lines.append(f"  {etype:<26}  {count:>4}")

    # ----- Notable events timeline -----
    lines.append("")
    lines.append("NOTABLE EVENTS (chronological)")
    lines.append(sep)
    if not summary.notable_events:
        lines.append("  (none)")
    else:
        for entry in summary.notable_events:
            lines.append(_notable_line(entry))

    # ----- Warnings: poll errors -----
    if summary.poll_errors:
        lines.append("")
        lines.append("WARNINGS -- POLL ERRORS")
        lines.append(sep)
        for entry in summary.poll_errors:
            payload = entry.get("payload", {})
            err = payload.get("error", "(no error detail)")
            ts = _to_local_display(entry.get("ts", ""))
            lines.append(f"  {ts}  {err}")

    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="EOD trade-journal report -- read JSONL audit log(s) and summarize"
    )
    parser.add_argument(
        "--journal",
        default="logs/journal*.jsonl",
        help=(
            "Glob or explicit path to journal JSONL file(s). "
            'Default: "logs/journal*.jsonl". '
            "Resolved relative to fidelity_rebalancer/ if not found as-is."
        ),
    )
    args = parser.parse_args()

    # Resolve: try as-is first, then relative to package root
    raw_pattern = args.journal
    resolved = resolve_path(raw_pattern)

    # Glob expansion -- try resolved pattern first, then original
    files = glob.glob(resolved)
    if not files and resolved != raw_pattern:
        files = glob.glob(raw_pattern)

    if not files:
        print(f"No journal files found matching: {raw_pattern}")
        return

    files = sorted(files)
    entries, n_malformed, unreadable = load_journal(files)
    s = summarize(entries)
    report = format_report(s, n_malformed, file_labels=files, unreadable=unreadable)
    print(report)


if __name__ == "__main__":
    main()
