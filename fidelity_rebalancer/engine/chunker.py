"""
Order chunkers + ex-div + tick-size helpers.

Two chunkers are provided:

  • build_sell_chunks / build_buy_chunks  — **book-relative** (default).
    max_chunk_shares = min(
        max_pct_of_top3_depth × Σ(top-3 levels at side),
        max_pct_of_5min_volume × trailing_5min_volume,
    ),  rounded down to nearest 100.

  • build_sell_chunks_legacy / build_buy_chunks_legacy — port of the React
    calculator's $100K dollar-based chunker.  Kept for parity testing
    via `--chunker=legacy_dollar`.

Pure functions — no I/O.
"""
from __future__ import annotations

import json
import math
import random
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

CHUNK_DEFAULT = 100_000   # legacy $100K chunker default

# ── POV chunker constants ─────────────────────────────────────────────────
# Square-root law (Almgren-style):  I = Y · σ · √(Q/V)
# Y is the impact coefficient, σ is a daily realized-vol proxy in bps.
# These values are typical for US large/mid-cap equities; tune per asset
# class if needed.
_IMPACT_COEFF      = 0.5      # Y, dimensionless
_DAILY_SIGMA_BPS   = 100.0    # σ proxy (1% daily vol)
_DEFAULT_JITTER    = 0.15     # ±15% jitter to defeat HFT pattern matching
_MAX_CHUNKS        = 20       # safety cap (matches Fidelity's daily order ceiling)

# ── Intraday volume profile (U-shape) ────────────────────────────────────
# Empirical US equity ETF volume distribution.  Multiplier applied to the
# flat vol_5min estimate (ADV/78) to match actual liquidity at each period.
_VOL_PROFILE: list[tuple[tuple[int, int], tuple[int, int], float]] = [
    # (start_hour, start_min), (end_hour, end_min), multiplier
    ((9, 30),  (10,  0), 1.8),   # Opening — high volume, wide spreads
    ((10, 0),  (11, 30), 1.1),   # Mid-morning — best execution window
    ((11, 30), (13, 30), 0.6),   # Lunch — volume drops 40-60%
    ((13, 30), (15, 30), 1.2),   # Afternoon — recovery
    ((15, 30), (16,  0), 1.5),   # Closing — spike
]


def vol_profile_multiplier(hour: int, minute: int) -> float:
    """
    Intraday volume multiplier relative to the flat ADV/78 estimate.
    Returns 1.0 for times outside market hours (pre-market, after-hours).
    """
    t = (hour, minute)
    for start, end, mult in _VOL_PROFILE:
        if start <= t < end:
            return mult
    return 1.0


# ── Tick size ─────────────────────────────────────────────────────────────

def tick(price: float) -> float:
    """Minimum price increment.  Sub-dollar instruments use $0.0001 ticks."""
    return 0.0001 if price < 1.0 else 0.01


def round_to_tick(price: float, ref_price: Optional[float] = None) -> float:
    """Round `price` to the tick size implied by `ref_price` (or `price` if omitted)."""
    t = tick(ref_price if ref_price is not None else price)
    return round(price / t) * t


# ── Round helpers ─────────────────────────────────────────────────────────

def _round_to_100(n: float) -> int:
    """Port of JS roundTo100: Math.round(n / 100) * 100, half-up."""
    return math.floor(n / 100 + 0.5) * 100


def _floor_to_100(n: float) -> int:
    return int(math.floor(n / 100) * 100)


# ── Book-relative chunker ─────────────────────────────────────────────────

def _max_chunk_shares(
    levels: Iterable,
    vol5min: float,
    max_pct_of_top3_depth: float,
    max_pct_of_5min_volume: float,
) -> int:
    """
    Compute the per-chunk share cap from the depth-of-book and trailing volume.
    Returns at least 100 so the loop always makes progress.
    """
    levels = list(levels)
    top3 = sum(getattr(lv, "size", 0) for lv in levels[:3])
    by_depth = _floor_to_100(max_pct_of_top3_depth * top3)
    by_vol   = _floor_to_100(max_pct_of_5min_volume * (vol5min or 0))
    candidates = [c for c in (by_depth, by_vol) if c > 0]
    if not candidates:
        return 100
    return max(100, min(candidates))


def build_sell_chunks(
    rem_shares: float,
    limit_price: float,
    bids: Iterable,
    vol5min: float,
    *,
    max_pct_of_top3_depth: float = 0.25,
    max_pct_of_5min_volume: float = 0.15,
) -> list[dict]:
    """
    Book-relative sell chunker.
    `bids` is the bid side of an L2 snapshot (best-first).
    """
    if rem_shares <= 0.0:
        return []
    cap = _max_chunk_shares(bids, vol5min, max_pct_of_top3_depth, max_pct_of_5min_volume)

    chunks: list[dict] = []
    left = float(rem_shares)
    i = 0
    while left > 0.001 and i < 50:
        if left <= cap:
            shs: float = left
        else:
            shs = float(cap)
        chunks.append({
            "idx": i,
            "shares": shs,
            "limit_price": limit_price,
            "cost": shs * limit_price,
        })
        left = max(0.0, left - shs)
        i += 1
    return chunks


def build_buy_chunks(
    rem_dollars: float,
    limit_price: float,
    asks: Iterable,
    vol5min: float,
    *,
    max_pct_of_top3_depth: float = 0.25,
    max_pct_of_5min_volume: float = 0.15,
) -> list[dict]:
    """
    Book-relative buy chunker.  Total cost across chunks never exceeds rem_dollars.
    `asks` is the ask side of an L2 snapshot (best-first).
    """
    if limit_price <= 0 or rem_dollars < 0.01:
        return []
    cap = _max_chunk_shares(asks, vol5min, max_pct_of_top3_depth, max_pct_of_5min_volume)

    chunks: list[dict] = []
    budget_left = float(rem_dollars)
    i = 0
    while budget_left > 0.01 and i < 50:
        max_shs_by_budget = int(math.floor(budget_left / limit_price))
        if max_shs_by_budget <= 0:
            break
        if max_shs_by_budget > cap:
            shs = cap
        else:
            shs = max_shs_by_budget
        cost = shs * limit_price
        if cost > budget_left + 1e-6:
            break
        chunks.append({
            "idx": i,
            "shares": float(shs),
            "limit_price": limit_price,
            "cost": cost,
        })
        budget_left = max(0.0, budget_left - cost)
        i += 1
    return chunks


# ── Legacy $100K dollar chunker (parity with React calc) ──────────────────

def build_sell_chunks_legacy(
    rem_shares: float,
    def_lim: float,
    chunk_limit: float = CHUNK_DEFAULT,
) -> list[dict]:
    """Port of JS buildSellChunks — kept for `--chunker=legacy_dollar` parity."""
    chunks: list[dict] = []
    left = rem_shares
    i = 0
    while left > 0.001:
        lim = def_lim
        dollar_val = left * lim
        if dollar_val > chunk_limit and lim > 0:
            raw = math.floor(chunk_limit / lim)
            shs = _round_to_100(raw)
            if shs <= 0:
                shs = min(left, 100)
            shs = min(shs, left)
        else:
            shs = left
        chunks.append({"idx": i, "shares": shs, "limit_price": lim, "cost": shs * lim})
        left = max(0.0, left - shs)
        i += 1
        if i > 50:
            break
    return chunks


def build_buy_chunks_legacy(
    rem_dollars: float,
    def_lim: float,
    chunk_limit: float = CHUNK_DEFAULT,
) -> list[dict]:
    """Port of JS buildBuyChunks — kept for `--chunker=legacy_dollar` parity."""
    if def_lim <= 0 or rem_dollars < 0.01:
        return []
    chunks: list[dict] = []
    budget_left = rem_dollars
    i = 0
    while budget_left > 0.01:
        lim = def_lim
        if lim <= 0:
            break
        max_shs = math.floor(budget_left / lim)
        if max_shs <= 0:
            break
        if max_shs * lim > chunk_limit:
            raw = math.floor(chunk_limit / lim)
            shs = _round_to_100(raw)
            if shs <= 0:
                shs = 100
            shs = min(shs, max_shs)
        else:
            shs = max_shs
        cost = shs * lim
        chunks.append({"idx": i, "shares": shs, "limit_price": lim, "cost": cost})
        budget_left = max(0.0, budget_left - cost)
        i += 1
        if i > 50:
            break
    return chunks


# ── POV-aware chunker ─────────────────────────────────────────────────────
#
# Institutional-style execution:
#
#   POV (% of ADV)          Behavior
#   ──────────────────      ────────────────────────────────────────────────
#   < 1%  + tight spread    Single order — invisible, no need to slice.
#   1 – 5%                  Standard slicing, ceil(pov_pct) chunks.
#   5 – 10%                 Aggressive slicing, ceil(1.5×pov_pct) chunks.
#   > 10%                   Market-moving — warn, ceil(2×pov_pct) chunks.
#
# Sells get +1 chunk vs buys at the same size: empirical work (Almgren et al.)
# shows sell-side impact is more persistent (asymmetric impact).
#
# Chunks are jittered ±15% to break the uniform-slice pattern that HFT
# venues fingerprint.  Jitter is deterministic per (shares, price) so
# repeated runs produce identical chunks.
#
# Square-root law gives the expected impact in bps:  Y · σ · √(Q/V).

def estimate_impact_bps(
    total_shares: float,
    adv: Optional[float],
    sigma_bps: float = _DAILY_SIGMA_BPS,
) -> float:
    """Square-root law: I_bps = Y · σ · √(Q/V).  Returns 0 if ADV unknown."""
    if not adv or adv <= 0 or total_shares <= 0:
        return 0.0
    pov_frac = total_shares / adv
    return _IMPACT_COEFF * sigma_bps * math.sqrt(pov_frac)


def _pov_tier(pov_pct: float, spread_bps: float) -> tuple[int, str]:
    """Returns (tier, label) per the table above."""
    if pov_pct < 1.0 and spread_bps < 10.0:
        return (1, "invisible")
    if pov_pct < 5.0:
        return (2, "standard")
    if pov_pct < 10.0:
        return (3, "aggressive")
    return (4, "market_moving")


def _chunk_count(tier: int, pov_pct: float, side: str) -> int:
    """Number of chunks for a given tier, with sell-side asymmetry."""
    if tier == 1:
        return 1
    if tier == 2:
        n = math.ceil(pov_pct)
    elif tier == 3:
        n = math.ceil(1.5 * pov_pct)
    else:
        n = math.ceil(2.0 * pov_pct)
    if side == "sell":
        n += 1
    return max(2, min(int(n), _MAX_CHUNKS))


def _split_with_jitter(
    total_shares: float,
    limit_price: float,
    n: int,
    rng: random.Random,
    jitter_pct: float,
) -> list[dict]:
    """Split `total_shares` into n chunks with ±jitter, rounded to 100-share lots."""
    total_int = int(math.floor(total_shares))
    if total_int <= 0 or n <= 0:
        return []
    if n == 1:
        return [{
            "idx": 0,
            "shares": float(total_int),
            "limit_price": limit_price,
            "cost": total_int * limit_price,
        }]

    base = total_shares / n
    raw = [base * (1.0 + rng.uniform(-jitter_pct, jitter_pct)) for _ in range(n)]
    scale = total_shares / sum(raw)
    raw = [s * scale for s in raw]

    rounded = [_round_to_100(s) for s in raw[:-1]]
    last = total_int - sum(rounded)
    if last <= 0:
        # Jitter pushed too much into the head; fall back to equal split
        eq = _round_to_100(total_shares / n)
        rounded = [eq] * (n - 1)
        last = total_int - sum(rounded)
    rounded.append(max(0, last))

    chunks: list[dict] = []
    for i, shs in enumerate(rounded):
        if shs <= 0:
            continue
        chunks.append({
            "idx": i,
            "shares": float(shs),
            "limit_price": limit_price,
            "cost": shs * limit_price,
        })
    return chunks


def build_chunks_pov(
    total_shares: float,
    limit_price: float,
    *,
    adv: Optional[float],
    spread_bps: float,
    side: str,
    rng: Optional[random.Random] = None,
    jitter_pct: float = _DEFAULT_JITTER,
    sigma_bps: float = _DAILY_SIGMA_BPS,
) -> tuple[list[dict], dict]:
    """
    POV-aware chunker.  Returns (chunks, info_dict).

    info_dict keys:
      pov_pct        — order shares as % of ADV (None if ADV unknown)
      tier           — 1..4 (or 0 if ADV unknown)
      tier_label     — "invisible" | "standard" | "aggressive" |
                       "market_moving" | "unknown_adv"
      est_impact_bps — square-root-law impact estimate
      n_chunks       — number of chunks emitted
    """
    info: dict = {
        "pov_pct":        None,
        "tier":           0,
        "tier_label":     "unknown_adv",
        "est_impact_bps": 0.0,
        "n_chunks":       0,
    }
    if total_shares <= 0 or limit_price <= 0:
        return [], info

    seed = int(total_shares * 1000) ^ int(limit_price * 10000)
    rng = rng or random.Random(seed)

    if not adv or adv <= 0:
        # No ADV: split by dollar value (≤$50K → single, else 2 chunks)
        n = 1 if total_shares * limit_price < 50_000 else 2
        chunks = _split_with_jitter(total_shares, limit_price, n, rng, jitter_pct)
        info["n_chunks"] = len(chunks)
        return chunks, info

    pov_pct = (total_shares / adv) * 100.0
    tier, label = _pov_tier(pov_pct, spread_bps)
    n = _chunk_count(tier, pov_pct, side)
    impact_bps = estimate_impact_bps(total_shares, adv, sigma_bps=sigma_bps)

    chunks = _split_with_jitter(total_shares, limit_price, n, rng, jitter_pct)
    info.update({
        "pov_pct":        pov_pct,
        "tier":           tier,
        "tier_label":     label,
        "est_impact_bps": impact_bps,
        "n_chunks":       len(chunks),
    })
    return chunks, info


# ── Gap capture multi-phase chunker ──────────────────────────────────────
#
# Three pricing phases for sells at market open when a gap-up is detected:
#   Phase 1 (gap capture):    30% of shares at aggressive price (prev_close × 0.99)
#   Phase 2 (standard):       50% of shares at the normal strategy price
#   Phase 3 (completion sweep): 20% of shares at bid (ensure same-day fill)

_GAP_PHASE_SPLIT = (0.30, 0.50, 0.20)


def build_gap_capture_chunks(
    total_shares: float,
    gap_price: float,
    standard_price: float,
    sweep_price: float,
) -> list[dict]:
    """
    Three-phase sell chunks with different limit prices per phase.
    Returns chunk dicts with 'phase' field ('gap_capture', 'standard', 'sweep').
    """
    if total_shares <= 0:
        return []

    total_int = int(math.floor(total_shares))
    phase_shares = [
        _round_to_100(total_int * pct) for pct in _GAP_PHASE_SPLIT[:-1]
    ]
    phase_shares.append(max(0, total_int - sum(phase_shares)))

    prices = [gap_price, standard_price, sweep_price]
    labels = ["gap_capture", "standard", "sweep"]
    chunks: list[dict] = []
    idx = 0
    for shs, price, label in zip(phase_shares, prices, labels):
        if shs <= 0:
            continue
        chunks.append({
            "idx": idx,
            "shares": float(shs),
            "limit_price": price,
            "cost": shs * price,
            "phase": label,
        })
        idx += 1
    return chunks


# ── Ex-dividend adjustment ────────────────────────────────────────────────

_EXDIV_FIXTURE_PATH = (
    Path(__file__).parent.parent / "tests" / "fixtures" / "exdiv_calendar.json"
)


@lru_cache(maxsize=1)
def _load_exdiv_fixture() -> dict[str, dict[str, float]]:
    """Static lookup table used by tests (and as a runtime cache override)."""
    if not _EXDIV_FIXTURE_PATH.exists():
        return {}
    try:
        return json.loads(_EXDIV_FIXTURE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _yfinance_exdiv(symbol: str, day: date) -> Optional[float]:
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        divs = yf.Ticker(symbol).dividends
        if divs is None or len(divs) == 0:
            return None
        for ts, amt in divs.items():
            ex_date = ts.date() if hasattr(ts, "date") else ts
            if ex_date == day:
                return float(amt)
    except Exception:
        return None
    return None


def adjust_prev_close_for_exdiv(
    symbol: str,
    prev_close: float,
    today: date,
    *,
    calendar: Optional[dict] = None,
) -> float:
    """
    On the 1st of any month, if `symbol` has an ex-dividend event today,
    return `prev_close - dividend_amount`.  Otherwise return `prev_close`.

    `calendar` overrides the fixture lookup; useful for tests.  yfinance is
    only consulted when neither the override nor the fixture has an answer.
    """
    if today.day != 1:
        return prev_close

    sym = symbol.upper()
    iso = today.isoformat()

    table = calendar if calendar is not None else _load_exdiv_fixture()
    div = (table.get(sym) or {}).get(iso)
    if div is None:
        div = _yfinance_exdiv(symbol, today)
    if div is None or div <= 0:
        return prev_close
    return prev_close - float(div)
