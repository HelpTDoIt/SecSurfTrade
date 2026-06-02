"""
Pre-trade sanity gate.

Pure logic: given a fully-sized RebalanceState, return a SanityReport of
findings. RED findings block (verdict RED, ok False); YELLOW findings warn
but proceed. Imports only stdlib + state.schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from state.schema import RebalanceState

Severity = Literal["RED", "YELLOW"]

_CHUNK_SUM_TOL = 1e-6


@dataclass(frozen=True)
class SanityFinding:
    severity: str  # "RED" or "YELLOW"
    code: str  # stable short code, e.g. "NON_POSITIVE_SHARES"
    message: str  # human-readable, includes the offending entity
    ref: str = ""  # optional: ticker or chunk_id or account for grouping


@dataclass(frozen=True)
class SanityReport:
    verdict: str  # "GREEN" | "YELLOW" | "RED"
    findings: list[SanityFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict != "RED"


# Plain-English "what it means / what to do" gloss per finding code, so the
# CLI output is self-explanatory without the HTML calculator open for context.
# RED = blocking (do not enter the orders); YELLOW = warning (proceed with care).
FINDING_HELP: dict[str, str] = {
    # ── RED (blocking) ──────────────────────────────────────────────────────
    "NON_POSITIVE_SHARES": (
        "A share count is zero or negative — the order is malformed. "
        "Re-run order sizing; do not enter this."
    ),
    "CHUNK_SUM_MISMATCH": (
        "The sized chunks for this ticker do not add up to the target share "
        "count — entering them would trade the wrong total. Re-run order sizing; "
        "do not enter these chunks."
    ),
    "DANGLING_CHUNK_ID": (
        "A strategy points at a chunk that does not exist — the sized state is "
        "inconsistent. Re-run order sizing."
    ),
    "NON_POSITIVE_LIMIT": (
        "A limit price is zero or negative — unusable for an order. "
        "Re-run order sizing."
    ),
    "LIMIT_FAR_FROM_PREVCLOSE": (
        "A limit price is far from the prior close — likely a stale quote or a "
        "typo. Verify the live price before trusting this limit."
    ),
    # ── YELLOW (warnings) ───────────────────────────────────────────────────
    "CASH_NOT_OK": (
        "Planned buys exceed the cash currently settled in this account. The "
        "per-account message states whether this is an expected IRA timing gap "
        "(buys funded by today's unsettled sells) or a genuine cash shortfall. "
        "Margin accounts do not raise this finding — same-window buy+sell is "
        "funded by buying power, not settled proceeds."
    ),
    "ORPHAN_CHUNK": (
        "A sized chunk is not referenced by any strategy — it may never be "
        "entered. Confirm it is intended."
    ),
    "MISSING_PREVCLOSE": (
        "No prior close was available, so the limit-vs-prev-close check could "
        "not fully run. Eyeball the limit price manually."
    ),
    "COST_ARITHMETIC_DRIFT": (
        "A chunk's stored cost does not equal shares x limit — usually rounding. "
        "Harmless unless the gap is large; verify if so."
    ),
    "THIN_NO_L2": (
        "A thin ticker was sized without live L2 depth — real fills may be worse "
        "than modeled. Trade smaller / more carefully."
    ),
    "OVERSIZED_VS_ADV": (
        "A chunk is a large fraction of the ticker's 10-day average volume — it "
        "may move the market. Consider splitting it across sessions."
    ),
}


def explain(code: str) -> str:
    """Return the plain-English gloss for a finding code (fallback if unknown)."""
    return FINDING_HELP.get(
        code, "See the documentation for this check; resolve before trading."
    )


def _key(account: str, strategy: str, ticker: str) -> tuple[str, str, str]:
    return (account, strategy, ticker)


def check_sanity(
    state: RebalanceState,
    *,
    limit_dev_pct: float = 25.0,
    cost_tol: float = 0.01,
) -> SanityReport:
    findings: list[SanityFinding] = []
    computed = state.computed
    prev_closes = state.inputs.prev_closes

    sells = computed.sells
    buys = computed.buy_allocations
    sell_chunks = computed.sell_chunks
    buy_chunks = computed.buy_chunks

    # ── RULE 1: NON_POSITIVE_SHARES ────────────────────────────────────────
    for s in sells:
        if s.shares <= 0:
            findings.append(
                SanityFinding(
                    "RED",
                    "NON_POSITIVE_SHARES",
                    f"Sell {s.ticker} in {s.account}/{s.strategy} has non-positive "
                    f"shares ({s.shares}).",
                    ref=s.ticker,
                )
            )
    for b in buys:
        if b.share_target <= 0:
            findings.append(
                SanityFinding(
                    "RED",
                    "NON_POSITIVE_SHARES",
                    f"Buy {b.ticker} in {b.account}/{b.strategy} has non-positive "
                    f"share_target ({b.share_target}).",
                    ref=b.ticker,
                )
            )
    for c in (*sell_chunks, *buy_chunks):
        if c.shares <= 0:
            findings.append(
                SanityFinding(
                    "RED",
                    "NON_POSITIVE_SHARES",
                    f"Chunk {c.chunk_id} ({c.ticker}) has non-positive shares "
                    f"({c.shares}).",
                    ref=c.chunk_id,
                )
            )

    # ── RULE 2: CHUNK_SUM_MISMATCH ─────────────────────────────────────────
    sell_chunk_sum: dict[tuple[str, str, str], float] = {}
    for c in sell_chunks:
        k = _key(c.account, c.strategy, c.ticker)
        sell_chunk_sum[k] = sell_chunk_sum.get(k, 0.0) + c.shares
    buy_chunk_sum: dict[tuple[str, str, str], float] = {}
    for c in buy_chunks:
        k = _key(c.account, c.strategy, c.ticker)
        buy_chunk_sum[k] = buy_chunk_sum.get(k, 0.0) + c.shares

    for s in sells:
        k = _key(s.account, s.strategy, s.ticker)
        total = sell_chunk_sum.get(k, 0.0)
        if abs(total - s.shares) > _CHUNK_SUM_TOL:
            findings.append(
                SanityFinding(
                    "RED",
                    "CHUNK_SUM_MISMATCH",
                    f"Sell {s.ticker} in {s.account}/{s.strategy}: chunk shares "
                    f"sum to {total} but record requires {s.shares}.",
                    ref=s.ticker,
                )
            )
    for b in buys:
        k = _key(b.account, b.strategy, b.ticker)
        total = buy_chunk_sum.get(k, 0.0)
        if abs(total - b.share_target) > _CHUNK_SUM_TOL:
            findings.append(
                SanityFinding(
                    "RED",
                    "CHUNK_SUM_MISMATCH",
                    f"Buy {b.ticker} in {b.account}/{b.strategy}: chunk shares "
                    f"sum to {total} but record requires {b.share_target}.",
                    ref=b.ticker,
                )
            )

    # ── RULE 3 + 9: DANGLING_CHUNK_ID (RED) / ORPHAN_CHUNK (YELLOW) ─────────
    sell_chunk_ids = {c.chunk_id for c in sell_chunks}
    buy_chunk_ids = {c.chunk_id for c in buy_chunks}
    referenced_sell: set[str] = set()
    referenced_buy: set[str] = set()

    for strat in computed.sell_strategies:
        for cid in strat.chunk_ids:
            referenced_sell.add(cid)
            if cid not in sell_chunk_ids:
                findings.append(
                    SanityFinding(
                        "RED",
                        "DANGLING_CHUNK_ID",
                        f"Sell strategy {strat.ticker} in "
                        f"{strat.account}/{strat.strategy} references unknown "
                        f"chunk_id {cid}.",
                        ref=cid,
                    )
                )
    for strat in computed.buy_strategies:
        for cid in strat.chunk_ids:
            referenced_buy.add(cid)
            if cid not in buy_chunk_ids:
                findings.append(
                    SanityFinding(
                        "RED",
                        "DANGLING_CHUNK_ID",
                        f"Buy strategy {strat.ticker} in "
                        f"{strat.account}/{strat.strategy} references unknown "
                        f"chunk_id {cid}.",
                        ref=cid,
                    )
                )

    for c in sell_chunks:
        if c.chunk_id not in referenced_sell:
            findings.append(
                SanityFinding(
                    "YELLOW",
                    "ORPHAN_CHUNK",
                    f"Sell chunk {c.chunk_id} ({c.ticker}) is not referenced by "
                    f"any strategy.",
                    ref=c.chunk_id,
                )
            )
    for c in buy_chunks:
        if c.chunk_id not in referenced_buy:
            findings.append(
                SanityFinding(
                    "YELLOW",
                    "ORPHAN_CHUNK",
                    f"Buy chunk {c.chunk_id} ({c.ticker}) is not referenced by "
                    f"any strategy.",
                    ref=c.chunk_id,
                )
            )

    # ── RULE 4: CASH_NOT_OK ────────────────────────────────────────────────
    # Branch by account type. Margin accounts fund same-window buy+sell from
    # buying power (not settled proceeds), so the cash gate does not apply —
    # suppress the finding entirely for them. Retirement accounts get the
    # expected-IRA-timing wording; cash accounts get the genuine-shortfall one.
    acct_by_name = {a.name: a for a in state.inputs.accounts}
    for account, ok in computed.cash_ok.items():
        if ok:
            continue
        acct = acct_by_name.get(account)
        if acct is not None and acct.margin:
            continue
        if acct is None or acct.type == "retirement":
            detail = (
                "Expected for an IRA/retirement rebalance: the buys are funded "
                "by today's sells, which have not settled yet. Confirm the buys "
                "are covered by today's sell proceeds (or supply "
                "--confirmed-proceeds); do not enter buys that exceed your "
                "actual available cash."
            )
        else:
            detail = (
                "This is a cash (non-margin) account: the buys exceed the cash "
                "currently settled. Wait for proceeds to settle, reduce the "
                "buys, or mark the account margin-enabled if it is."
            )
        findings.append(
            SanityFinding(
                "YELLOW",
                "CASH_NOT_OK",
                f"Buys in account {account} do not fit within settled cash "
                f"(cash_ok is False). {detail}",
                ref=account,
            )
        )

    # ── RULE 5: NON_POSITIVE_LIMIT ─────────────────────────────────────────
    for s in sells:
        if s.limit_price <= 0:
            findings.append(
                SanityFinding(
                    "RED",
                    "NON_POSITIVE_LIMIT",
                    f"Sell {s.ticker} in {s.account}/{s.strategy} has "
                    f"non-positive limit_price ({s.limit_price}).",
                    ref=s.ticker,
                )
            )
    for b in buys:
        if b.limit_price <= 0:
            findings.append(
                SanityFinding(
                    "RED",
                    "NON_POSITIVE_LIMIT",
                    f"Buy {b.ticker} in {b.account}/{b.strategy} has "
                    f"non-positive limit_price ({b.limit_price}).",
                    ref=b.ticker,
                )
            )
    for c in (*sell_chunks, *buy_chunks):
        if c.limit_price <= 0:
            findings.append(
                SanityFinding(
                    "RED",
                    "NON_POSITIVE_LIMIT",
                    f"Chunk {c.chunk_id} ({c.ticker}) has non-positive "
                    f"limit_price ({c.limit_price}).",
                    ref=c.chunk_id,
                )
            )

    # ── RULE 6: LIMIT_FAR_FROM_PREVCLOSE / RULE 8: MISSING_PREVCLOSE ────────
    # One finding per (entity) per rule; iterate sells then buys.
    for s in sells:
        prev = prev_closes.get(s.ticker)
        if prev is None or prev <= 0:
            findings.append(
                SanityFinding(
                    "YELLOW",
                    "MISSING_PREVCLOSE",
                    f"Sell {s.ticker} in {s.account}/{s.strategy} has no usable "
                    f"prev_close; limit cannot be fully validated.",
                    ref=s.ticker,
                )
            )
        elif abs(s.limit_price / prev - 1.0) * 100.0 > limit_dev_pct:
            findings.append(
                SanityFinding(
                    "RED",
                    "LIMIT_FAR_FROM_PREVCLOSE",
                    f"Sell {s.ticker} limit {s.limit_price} deviates "
                    f"{abs(s.limit_price / prev - 1.0) * 100.0:.2f}% from "
                    f"prev_close {prev} (threshold {limit_dev_pct}%).",
                    ref=s.ticker,
                )
            )
    for b in buys:
        prev = prev_closes.get(b.ticker)
        if prev is None or prev <= 0:
            findings.append(
                SanityFinding(
                    "YELLOW",
                    "MISSING_PREVCLOSE",
                    f"Buy {b.ticker} in {b.account}/{b.strategy} has no usable "
                    f"prev_close; limit cannot be fully validated.",
                    ref=b.ticker,
                )
            )
        elif abs(b.limit_price / prev - 1.0) * 100.0 > limit_dev_pct:
            findings.append(
                SanityFinding(
                    "RED",
                    "LIMIT_FAR_FROM_PREVCLOSE",
                    f"Buy {b.ticker} limit {b.limit_price} deviates "
                    f"{abs(b.limit_price / prev - 1.0) * 100.0:.2f}% from "
                    f"prev_close {prev} (threshold {limit_dev_pct}%).",
                    ref=b.ticker,
                )
            )

    # ── RULE 7: COST_ARITHMETIC_DRIFT ──────────────────────────────────────
    for c in (*sell_chunks, *buy_chunks):
        expected = c.shares * c.limit_price
        if abs(c.cost - expected) > cost_tol * max(1.0, expected):
            findings.append(
                SanityFinding(
                    "YELLOW",
                    "COST_ARITHMETIC_DRIFT",
                    f"Chunk {c.chunk_id} ({c.ticker}) cost {c.cost} != "
                    f"shares*limit ({expected}).",
                    ref=c.chunk_id,
                )
            )

    if any(f.severity == "RED" for f in findings):
        verdict = "RED"
    elif any(f.severity == "YELLOW" for f in findings):
        verdict = "YELLOW"
    else:
        verdict = "GREEN"

    return SanityReport(verdict=verdict, findings=findings)
