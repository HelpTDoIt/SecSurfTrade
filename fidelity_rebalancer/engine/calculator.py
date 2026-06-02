"""
Port of the React calculator's parseCSV, consolidate, calcTrades, allocBuys.
All functions are pure (no I/O, no side effects).
"""

from __future__ import annotations

import math


def _fv(s: str) -> float:
    """JS parseFloat equivalent: strip $, +, %, commas then parse; return 0 on failure.

    The $ is stripped ANYWHERE in the string, not just leading. Fidelity exports
    negative dollar amounts as '-$99105.05' (sign before $), and the prior
    leading-only strip silently turned those into 0.0 — the Pending activity row
    was being parsed as $0 across the entire codebase.
    """
    if not s:
        return 0.0
    cleaned = (
        s.strip().replace("$", "").replace(",", "").replace("+", "").replace("%", "")
    )
    try:
        return float(cleaned) or 0.0
    except ValueError:
        return 0.0


def parse_csv(text: str) -> list[dict[str, str]]:
    """
    Port of JS parseCSV.
    Filters lines that start with '"' (Fidelity metadata rows), splits the
    remainder on commas, strips $, +, % from values.
    """
    lines = [
        l
        for l in text.split("\n")
        for l in [l.rstrip("\r")]
        if l.strip() and not l.startswith('"')
    ]
    if len(lines) < 2:
        return []
    hdr = [h.strip() for h in lines[0].split(",")]
    rows = []
    for line in lines[1:]:
        parts = line.split(",")
        row: dict[str, str] = {}
        for i, h in enumerate(hdr):
            raw = (parts[i] if i < len(parts) else "").strip()
            if raw.startswith("$"):
                raw = raw[1:]
            raw = raw.replace("+", "").replace("%", "")
            row[h] = raw
        if row.get("Symbol"):
            rows.append(row)
    return rows


def consolidate(rows: list[dict[str, str]]) -> dict:
    """
    Port of JS consolidate.
    Groups rows by Symbol, summing quantity and value for duplicates (e.g.
    SMH in Cash + Margin lots). Price is taken from the first occurrence.

    The "Pending activity" pseudo-row (signed dollar amount, no Symbol that
    matches a ticker) is split out into its own field so it doesn't pollute
    positions but is still available to the cash gate.

    Returns {"account_name": str, "positions": {symbol: {...}}, "pending_activity": float}.
    """
    positions: dict[str, dict] = {}
    name = ""
    pending_activity = 0.0
    for r in rows:
        if not name and r.get("Account Name"):
            name = r["Account Name"]
        s = r.get("Symbol", "")
        if not s:
            continue
        # Fidelity exports a "Pending activity" row with a signed Current Value
        # and no real ticker — treat as cash adjustment, not a position.
        if s.strip().lower() == "pending activity":
            pending_activity += _fv(r.get("Current Value", ""))
            continue
        q = _fv(r.get("Quantity", ""))
        v = _fv(r.get("Current Value", ""))
        p = _fv(r.get("Last Price", ""))
        if s in positions:
            positions[s]["quantity"] += q
            positions[s]["value"] += v
        else:
            positions[s] = {"symbol": s, "quantity": q, "value": v, "price": p}
    return {
        "account_name": name,
        "positions": positions,
        "pending_activity": pending_activity,
    }


def calc_trades(
    cfg: dict,
    positions: dict[str, dict],
    signals: dict[str, dict[str, str]],
    closes: dict[str, float],
    pending_activity: float = 0.0,
) -> dict:
    """
    Port of JS calcTrades.

    cfg:       {"strategies": {name: alloc}, "cashReserve": float}
    positions: {symbol: {"symbol", "quantity", "value", "price"}}
    signals:   {strategy: {"current": ticker, "new": ticker}}
    closes:    {ticker: float}  (prev-close prices)
    pending_activity: signed dollar amount from the Fidelity 'Pending activity'
                     row. Negative = already-committed funds (unsettled buys,
                     pending withdrawals); positive = unsettled incoming.

    Returns dict with sells, buys, total_pool, depl_cash, est_sell,
    cash_ok, s_pos, one_share_total.
    """
    strategies: dict[str, float] = cfg["strategies"]
    cash_reserve: float = cfg.get("cashReserve", 0.0)
    s_names = list(strategies.keys())

    # Build per-strategy position snapshot
    s_pos: dict[str, dict] = {}
    total_strat_val = 0.0
    for s in s_names:
        sig = signals.get(s, {})
        t = sig.get("current", "")
        if t and t in positions:
            pos = positions[t]
            s_pos[s] = {
                "ticker": t,
                "value": pos["value"],
                "quantity": pos["quantity"],
                "price": pos["price"],
            }
            total_strat_val += pos["value"]
        else:
            s_pos[s] = {"ticker": t or "", "value": 0.0, "quantity": 0.0, "price": 0.0}

    spaxx = positions.get("SPAXX**", {}).get("value", 0.0)
    # Effective cash = settled SPAXX + signed pending. Pending negative
    # (unsettled buys / pending withdrawal) reduces available cash; pending
    # positive (unsettled incoming) adds to it. Caller can floor pending at
    # zero before passing in if they want conservative semantics.
    effective_cash = spaxx + pending_activity
    depl_cash = max(0.0, effective_cash - cash_reserve)
    total_pool = total_strat_val + depl_cash

    # Classify strategies
    trading: list[str] = []
    holding: list[str] = []
    for s in s_names:
        sig = signals.get(s, {})
        if sig.get("new") and sig["new"] != sig.get("current", ""):
            trading.append(s)
        else:
            holding.append(s)

    # oneShare = sum of one share price for each strategy's ETF
    # JS: closes[signals[s]?.new || signals[s]?.current] || sPos[s]?.price || 0
    one_share = 0.0
    for s in s_names:
        sig = signals.get(s, {})
        ticker = sig.get("new") or sig.get("current") or ""
        price = closes.get(ticker) or s_pos[s]["price"] or 0.0
        one_share += price

    cash_ok = depl_cash > one_share

    # Build sells for trading strategies
    sells: list[dict] = []
    sold_tickers: set[str] = set()
    for s in trading:
        p = s_pos[s]
        if p["quantity"] > 0 and p["ticker"] not in sold_tickers:
            sold_tickers.add(p["ticker"])
            lim = closes.get(p["ticker"]) or p["price"]
            sells.append(
                {
                    "strategy": s,
                    "ticker": p["ticker"],
                    "quantity": p["quantity"],
                    "limit_price": lim,
                    "est_proceeds": p["quantity"] * lim,
                }
            )

    est_sell = sum(x["est_proceeds"] for x in sells)
    buys = _alloc_buys(
        s_names,
        strategies,
        signals,
        closes,
        s_pos,
        trading,
        holding,
        cash_ok,
        est_sell,
        depl_cash,
        total_pool,
    )

    return {
        "sells": sells,
        "buys": buys,
        "total_pool": total_pool,
        "depl_cash": depl_cash,
        "est_sell": est_sell,
        "cash_ok": cash_ok,
        "s_pos": s_pos,
        "one_share_total": one_share,
    }


def _alloc_buys(
    s_names: list[str],
    strats: dict[str, float],
    signals: dict[str, dict[str, str]],
    closes: dict[str, float],
    s_pos: dict[str, dict],
    trading: list[str],
    holding: list[str],
    cash_ok: bool,
    sell_proceeds: float,
    depl_cash: float,
    total_pool: float,
) -> list[dict]:
    """Port of JS allocBuys."""
    avail = sell_proceeds + depl_cash
    out: list[dict] = []

    if len(trading) == 0 and not cash_ok:
        return out

    def mk(s: str, tk: str, d: float, p: float, rb: bool) -> None:
        if p > 0 and d > 0:
            out.append(
                {
                    "strategy": s,
                    "ticker": tk or "",
                    "dollar_target": d,
                    "limit_price": p,
                    "is_rebalance": rb,
                    "target_value": strats[s] * total_pool,
                }
            )

    if len(trading) == 0 and cash_ok:
        # Pure rebalance: no signal changes, just deploy cash
        for s in s_names:
            sig = signals.get(s, {})
            tk = sig.get("current", "")
            d = strats[s] * total_pool - (s_pos.get(s, {}).get("value", 0.0))
            p = closes.get(tk) or s_pos.get(s, {}).get("price", 0.0) or 0.0
            if d > 0:
                mk(s, tk, d, p, True)

    elif len(trading) >= 1 and not cash_ok:
        # Trade signals, insufficient cash to rebalance all
        if len(trading) == 1:
            s = trading[0]
            sig = signals[s]
            p = closes.get(sig["new"]) or 0.0
            mk(s, sig["new"], avail, p, False)
        else:
            t = sum(strats[s] for s in trading)
            for s in trading:
                sig = signals[s]
                p = closes.get(sig["new"]) or 0.0
                d = (strats[s] / t) * avail
                mk(s, sig["new"], d, p, True)

    elif cash_ok:
        # Trade signals AND enough cash to rebalance
        spent = 0.0
        for s in trading:
            tgt = strats[s] * total_pool
            tk = signals[s]["new"]
            p = closes.get(tk) or 0.0
            if p > 0 and tgt > 0:
                c = min(tgt, avail - spent)
                if c > 0:
                    mk(s, tk, c, p, True)
                    spent += c
        for s in holding:
            sig = signals.get(s, {})
            tk = sig.get("current", "")
            d = strats[s] * total_pool - (s_pos.get(s, {}).get("value", 0.0))
            p = closes.get(tk) or s_pos.get(s, {}).get("price", 0.0) or 0.0
            if d > 0 and p > 0:
                c = min(d, avail - spent)
                if c > 0:
                    mk(s, tk, c, p, True)
                    spent += c

    # Scale down if total buy targets exceed available funds
    tot = sum(a["dollar_target"] for a in out)
    if tot > avail and tot > 0:
        r = avail / tot
        for a in out:
            a["dollar_target"] *= r

    # Compute share counts
    for a in out:
        p = a["limit_price"]
        a["shares"] = math.floor(a["dollar_target"] / p) if p > 0 else 0
        a["est_cost"] = a["shares"] * p

    return [a for a in out if a["shares"] > 0]
