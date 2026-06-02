from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

import pytest

from adapters import WatchlistRow
from cli import strategy as strat


def _row(sym: str) -> WatchlistRow:
    return WatchlistRow(
        symbol=sym,
        last=100.0,
        bid=99.99,
        ask=100.01,
        bid_size=100,
        ask_size=100,
        volume=1_000_000,
        prev_close=100.0,
        avg_vol_10d=1_000_000,
        avg_vol_90d=1_000_000,
        div_ex_date="",
        div_local=0.0,
        vwap=100.0,
        ts=datetime.now(tz=timezone.utc),
    )


def _install_fake_atp(monkeypatch, present: list[str]) -> None:
    """Replace adapters.atp_watchlist with a fake returning only `present`."""
    fake_mod = types.ModuleType("adapters.atp_watchlist")

    class _FakeAdapter:
        def get_watchlist(self) -> dict[str, WatchlistRow]:
            return {s: _row(s) for s in present}

    fake_mod.ATPWatchlistAdapter = _FakeAdapter
    monkeypatch.setitem(sys.modules, "adapters.atp_watchlist", fake_mod)


def test_contract_constants_stable():
    # The orchestrator keys off these exact values — guard against drift.
    assert strat.OCR_SHORTFALL_EXIT == 3
    assert strat.OCR_SHORTFALL_MARKER == "OCR_SHORTFALL"


def test_strict_raises_on_missing_ticker(monkeypatch):
    _install_fake_atp(monkeypatch, present=["AAA"])
    with pytest.raises(strat.OCRShortfall) as exc:
        strat._fetch_watchlist(["AAA", "BBB"], "atp", strict=True)
    assert "BBB" in str(exc.value)


def test_strict_ok_when_all_present(monkeypatch):
    _install_fake_atp(monkeypatch, present=["AAA", "BBB"])
    rows = strat._fetch_watchlist(["AAA", "BBB"], "atp", strict=True)
    assert set(rows) == {"AAA", "BBB"}


def test_non_strict_falls_back_to_yfinance(monkeypatch):
    _install_fake_atp(monkeypatch, present=["AAA"])

    captured = {}

    class _FakeYF:
        def get_watchlist(self, syms):
            captured["syms"] = list(syms)
            return {s: _row(s) for s in syms}

    monkeypatch.setattr(strat, "YFinanceWatchlistAdapter", _FakeYF)

    rows = strat._fetch_watchlist(["AAA", "BBB"], "atp", strict=False)
    # BBB filled from the yfinance fallback, no exception raised.
    assert set(rows) == {"AAA", "BBB"}
    assert captured["syms"] == ["BBB"]


def test_strict_does_not_affect_yfinance_source(monkeypatch):
    # strict only governs the atp path; yfinance source ignores it.
    class _FakeYF:
        def get_watchlist(self, syms):
            return {s: _row(s) for s in syms}

    monkeypatch.setattr(strat, "YFinanceWatchlistAdapter", _FakeYF)
    rows = strat._fetch_watchlist(["AAA"], "yfinance", strict=True)
    assert set(rows) == {"AAA"}
