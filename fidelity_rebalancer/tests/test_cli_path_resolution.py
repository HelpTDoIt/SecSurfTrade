"""
Validates B-14 — ``cli.resolve_path`` / ``cli.resolve_output_path`` return a
resolved, absolute ``pathlib.Path`` (they previously returned ``str``).

This is the contract the call sites now rely on: the old ``Path(resolve_path(...))``
wrappers were dropped across cli/ and tui/, so a regression back to ``str`` must
fail loudly here rather than silently breaking ``Path`` operations downstream.
"""
from __future__ import annotations

from pathlib import Path

from cli import _PKG_ROOT, resolve_output_path, resolve_path


def test_resolve_path_returns_absolute_path(tmp_path):
    f = tmp_path / "data.json"
    f.write_text("{}", encoding="utf-8")
    out = resolve_path(str(f))
    assert isinstance(out, Path)
    assert out.is_absolute()
    assert out == f.resolve()


def test_resolve_path_missing_still_returns_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = resolve_path("nope/missing.json")
    assert isinstance(out, Path)
    assert out.is_absolute()


def test_resolve_path_falls_back_to_package_root(tmp_path, monkeypatch):
    # From a cwd with no ``cli/`` dir, a package-relative path resolves under
    # fidelity_rebalancer/ (the _PKG_ROOT fallback), still as an absolute Path.
    monkeypatch.chdir(tmp_path)
    out = resolve_path("cli/__init__.py")
    assert isinstance(out, Path)
    assert out.is_absolute()
    assert out == (_PKG_ROOT / "cli" / "__init__.py").resolve()


def test_resolve_output_path_returns_absolute_path(tmp_path):
    out = resolve_output_path(str(tmp_path / "out.json"))
    assert isinstance(out, Path)
    assert out.is_absolute()
    assert out == (tmp_path / "out.json").resolve()


def test_resolve_output_path_missing_parent_still_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = resolve_output_path("no_such_dir/out.json")
    assert isinstance(out, Path)
    assert out.is_absolute()
