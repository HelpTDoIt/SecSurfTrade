"""
Internal parsing helpers for ATP display strings.
No pywinauto dependency — pure string → numeric conversions.
"""
from __future__ import annotations

import re


_SUFFIXES = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def parse_price(text: str) -> float:
    """
    Parse an ATP price string such as '1,234.56' or '$62.71' → float.
    Returns 0.0 on failure.
    """
    cleaned = re.sub(r"[,$+%\s]", "", (text or "").strip())
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_size(text: str) -> int:
    """
    Parse an ATP size string: '1,200', '1.2M', '2K' → int.
    Returns 0 on failure.
    """
    cleaned = (text or "").strip().replace(",", "")
    if not cleaned:
        return 0
    # Check for suffix
    upper = cleaned.upper()
    for suffix, mult in _SUFFIXES.items():
        if upper.endswith(suffix):
            try:
                return int(float(upper[: -len(suffix)]) * mult)
            except ValueError:
                return 0
    try:
        return int(float(cleaned))
    except ValueError:
        return 0


def parse_volume(text: str) -> int:
    """Alias for parse_size — same format, just clearer call-site intent."""
    return parse_size(text)
