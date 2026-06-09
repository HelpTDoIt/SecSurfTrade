"""
Tests for cli.compute._find_downloads_csvs auto-detection.

Verifies the Account Name column is resolved by parsing the header row
(not a hard-coded index), so detection works regardless of where Fidelity
places the "Account Name" column.
"""

from __future__ import annotations

from pathlib import Path

import cli.compute as compute
from cli.compute import _find_downloads_csvs

# Fidelity-style CSV with a leading metadata row (starts with '"') and the
# "Account Name" column deliberately NOT at index 1 — here it is the 4th
# column (index 3). This proves the header lookup, not a fixed index.
_HEADER = (
    "Account Number,Symbol,Description,Account Name,Quantity,Last Price,"
    "Current Value,Type"
)
_CSV_MATCH = (
    '"Brokerage Positions as of 06/08/2026"\n'
    f"{_HEADER}\n"
    "Z11111111,EIS,iShares MSCI Israel ETF,My Taxable,200.000,$28.50,$5700.00,Cash\n"
    "Z11111111,SMH,Semiconductor ETF,My Taxable,30.000,$200.00,$6000.00,Cash\n"
)
_CSV_NO_MATCH = (
    '"Brokerage Positions as of 06/08/2026"\n'
    f"{_HEADER}\n"
    "Z99999999,EIS,iShares MSCI Israel ETF,Some Other Account,200.000,$28.50,$5700.00,Cash\n"
)


def _write_downloads_csv(tmp_path: Path, body: str) -> Path:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "Portfolio_Positions.csv").write_text(body, encoding="utf-8")
    return downloads


def test_finds_matching_account_via_header_lookup(monkeypatch, tmp_path):
    downloads = _write_downloads_csv(tmp_path, _CSV_MATCH)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(compute, "ACCOUNTS_CONFIG", {"My Taxable": {}})

    result = _find_downloads_csvs()

    assert result == downloads


def test_returns_none_when_no_account_matches(monkeypatch, tmp_path):
    _write_downloads_csv(tmp_path, _CSV_NO_MATCH)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(compute, "ACCOUNTS_CONFIG", {"My Taxable": {}})

    result = _find_downloads_csvs()

    assert result is None
