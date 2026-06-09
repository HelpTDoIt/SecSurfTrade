"""
Compute engine state from Fidelity CSVs + signals JSON.

Usage (from SecSurfTrade/ or fidelity_rebalancer/):
    python -m fidelity_rebalancer.cli.compute --signals signals.json --export engine_state.json
    python -m cli.compute --signals signals.json --export engine_state.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Resolve project root so imports work when invoked as -m cli.compute
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from cli import resolve_output_path, resolve_path
from adapters.csv_reader import read_fidelity_csv
from engine import observability
from engine.calculator import calc_trades, parse_csv
from engine.chunker import (
    build_buy_chunks,
    build_buy_chunks_legacy,
    build_sell_chunks,
    build_sell_chunks_legacy,
)
from state.importer import save_state
from state.schema import (
    AccountInput,
    BuyAllocationRecord,
    ChunkRecord,
    Computed,
    EngineConfig,
    Inputs,
    PositionInput,
    RebalanceState,
    SellRecord,
    SignalInput,
)


_log = logging.getLogger(__name__)


def _load_accounts_config() -> dict[str, dict]:
    config_path = Path(__file__).parent.parent / "accounts.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"accounts.json not found at {config_path}.\n"
            "Copy accounts.example.json to accounts.json and fill in your account details."
        )
    return json.loads(config_path.read_text(encoding="utf-8"))


ACCOUNTS_CONFIG: dict[str, dict] = _load_accounts_config()


def _build_state(
    accounts_raw: dict[str, dict],
    signals: dict[str, dict],
    closes: dict[str, float],
    chunker: str = "legacy_dollar",
) -> RebalanceState:
    account_inputs: list[AccountInput] = []
    signal_inputs: list[SignalInput] = []

    # Process accounts in ACCOUNTS_CONFIG order for stable chunk IDs
    for acct_name in ACCOUNTS_CONFIG:
        if acct_name not in accounts_raw:
            continue
        cfg = ACCOUNTS_CONFIG[acct_name]
        positions_dict: dict = accounts_raw[acct_name]["positions"]
        spaxx_val = 0.0
        for sym, p in positions_dict.items():
            s_up = sym.upper()
            if s_up.startswith("SPAXX") or s_up.startswith("FDRXX") or s_up.startswith("FCASH") or s_up.startswith("CORE") or "CASH" in s_up:
                spaxx_val += float(p.get("value", 0.0))
        pending_val = float(accounts_raw[acct_name].get("pending_activity", 0.0))

        account_inputs.append(
            AccountInput(
                name=acct_name,
                type=cfg["type"],
                margin=bool(cfg.get("margin", False)),
                cash_reserve=float(cfg["cashReserve"]),
                positions=[
                    PositionInput(
                        symbol=sym,
                        quantity=p["quantity"],
                        price=p["price"],
                        value=p["value"],
                    )
                    for sym, p in positions_dict.items()
                ],
                cash_spaxx=spaxx_val,
                pending_activity=pending_val,
                strategy_allocations=cfg["strategies"],
            )
        )
        for strat in cfg["strategies"]:
            sig = signals.get(strat, {})
            signal_inputs.append(
                SignalInput(
                    account=acct_name,
                    strategy=strat,
                    current_ticker=sig.get("current", ""),
                    new_ticker=sig.get("new", ""),
                )
            )

    inputs_obj = Inputs(
        accounts=account_inputs,
        signals=signal_inputs,
        prev_closes=closes,
        config=EngineConfig(),
    )

    sell_records: list[SellRecord] = []
    buy_records: list[BuyAllocationRecord] = []
    sell_chunks: list[ChunkRecord] = []
    buy_chunks: list[ChunkRecord] = []
    cash_ok: dict[str, bool] = {}
    one_share_total: dict[str, float] = {}
    sell_ctr = 0
    buy_ctr = 0

    for acct_name in ACCOUNTS_CONFIG:
        if acct_name not in accounts_raw:
            continue
        cfg = ACCOUNTS_CONFIG[acct_name]
        positions_dict = accounts_raw[acct_name]["positions"]
        pending_val = float(accounts_raw[acct_name].get("pending_activity", 0.0))
        result = calc_trades(cfg, positions_dict, signals, closes, pending_val)

        cash_ok[acct_name] = result["cash_ok"]
        one_share_total[acct_name] = result["one_share_total"]

        for sell in result["sells"]:
            sell_records.append(
                SellRecord(
                    account=acct_name,
                    strategy=sell["strategy"],
                    ticker=sell["ticker"],
                    shares=sell["quantity"],
                    limit_price=sell["limit_price"],
                    est_proceeds=sell["est_proceeds"],
                )
            )
            sell_chunk_dicts = (
                build_sell_chunks_legacy(sell["quantity"], sell["limit_price"])
                if chunker == "legacy_dollar"
                else build_sell_chunks(sell["quantity"], sell["limit_price"], [], 0.0)
            )
            for ch in sell_chunk_dicts:
                sell_ctr += 1
                sell_chunks.append(
                    ChunkRecord(
                        chunk_id=f"s{sell_ctr}",
                        account=acct_name,
                        strategy=sell["strategy"],
                        ticker=sell["ticker"],
                        idx=ch["idx"],
                        shares=ch["shares"],
                        limit_price=ch["limit_price"],
                        cost=ch["cost"],
                        vol5min=1_000_000.0,
                    )
                )

        for buy in result["buys"]:
            buy_records.append(
                BuyAllocationRecord(
                    account=acct_name,
                    strategy=buy["strategy"],
                    ticker=buy["ticker"],
                    dollar_target=buy["dollar_target"],
                    limit_price=buy["limit_price"],
                    share_target=buy["shares"],
                    est_cost=buy["est_cost"],
                    is_rebalance=buy["is_rebalance"],
                    target_value=buy["target_value"],
                )
            )
            buy_chunk_dicts = (
                build_buy_chunks_legacy(buy["dollar_target"], buy["limit_price"])
                if chunker == "legacy_dollar"
                else build_buy_chunks(buy["dollar_target"], buy["limit_price"], [], 0.0)
            )
            for ch in buy_chunk_dicts:
                buy_ctr += 1
                buy_chunks.append(
                    ChunkRecord(
                        chunk_id=f"b{buy_ctr}",
                        account=acct_name,
                        strategy=buy["strategy"],
                        ticker=buy["ticker"],
                        idx=ch["idx"],
                        shares=ch["shares"],
                        limit_price=ch["limit_price"],
                        cost=ch["cost"],
                        vol5min=1_000_000.0,
                    )
                )

    computed = Computed(
        cash_ok=cash_ok,
        one_share_total=one_share_total,
        sells=sell_records,
        buy_allocations=buy_records,
        sell_chunks=sell_chunks,
        buy_chunks=buy_chunks,
    )

    return RebalanceState(
        generated_at=datetime.now(tz=timezone.utc),
        generator="engine",
        inputs=inputs_obj,
        computed=computed,
    )


def _find_downloads_csvs() -> Path | None:
    """
    Return the most likely directory of Fidelity CSVs in the user's Downloads folder.
    Looks for .csv files whose Account Name header matches a known account.
    Returns a temp-like Path object pointing at Downloads if any are found, else None.
    """
    downloads = Path.home() / "Downloads"
    if not downloads.is_dir():
        return None
    candidates = list(downloads.glob("*.csv"))
    if not candidates:
        return None
    _keys_lower = {k.lower() for k in ACCOUNTS_CONFIG}
    found = []
    for p in candidates:
        try:
            text = p.read_text(encoding="utf-8-sig", errors="ignore")
            # parse_csv resolves the Account Name column by header (skipping the
            # leading metadata rows Fidelity prepends), so detection works
            # regardless of column order.
            rows = parse_csv(text)
            if any(
                r.get("Account Name", "").strip().lower() in _keys_lower for r in rows
            ):
                found.append(p)
        except Exception:
            continue
    return downloads if found else None



def _check_mom_sanity_diff(current_state, current_path):
    """D-5: Compare current strategy allocations against the previous month's export."""
    import json
    import logging
    from pathlib import Path
    logger = logging.getLogger(__name__)
    
    plans_dir = Path(current_path).parent
    if not plans_dir.exists():
        return
        
    # Find all plan_*.json or state_*.json excluding the current one
    candidates = []
    for p in plans_dir.glob("*.json"):
        if p.name != Path(current_path).name and (p.name.startswith("plan_") or p.name.startswith("state_")):
            candidates.append(p)
            
    if not candidates:
        return
        
    # Sort by mtime
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    last_plan = candidates[0]
    
    try:
        with open(last_plan, "r", encoding="utf-8") as f:
            last_data = json.load(f)
            
        last_sells = last_data.get("computed", {}).get("sells", [])
        last_buys = last_data.get("computed", {}).get("buy_allocations", [])
        
        last_totals = {}
        for s in last_sells:
            last_totals[s.get("strategy")] = last_totals.get(s.get("strategy"), 0) + s.get("est_proceeds", 0)
        for b in last_buys:
            last_totals[b.get("strategy")] = last_totals.get(b.get("strategy"), 0) + b.get("target_value", 0)
            
        last_total_pool = sum(last_totals.values())
        if last_total_pool <= 0:
            return
            
        curr_sells = current_state.computed.sells
        curr_buys = current_state.computed.buy_allocations
        
        curr_totals = {}
        for s in curr_sells:
            curr_totals[s.strategy] = curr_totals.get(s.strategy, 0) + s.est_proceeds
        for b in curr_buys:
            curr_totals[b.strategy] = curr_totals.get(b.strategy, 0) + getattr(b, "target_value", 0)
            
        curr_total_pool = sum(curr_totals.values())
        if curr_total_pool <= 0:
            return
            
        import sys
        for strat in set(last_totals.keys()) | set(curr_totals.keys()):
            last_pct = (last_totals.get(strat, 0) / last_total_pool) * 100
            curr_pct = (curr_totals.get(strat, 0) / curr_total_pool) * 100
            
            if abs(curr_pct - last_pct) > 5.0:
                print(f"[WARN] MoM Sanity Diff: Strategy '{strat}' allocation changed by >5% "
                      f"({last_pct:.1f}% -> {curr_pct:.1f}%). Verify if this shift is intentional.", file=sys.stderr)
                      
    except Exception as e:
        import sys
        print(f"[WARN] MoM sanity diff could not be computed: {e}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute rebalance state from Fidelity CSVs + signals JSON"
    )
    parser.add_argument(
        "--inputs",
        default=None,
        help=(
            "Directory containing Fidelity CSV files. "
            "Omit to auto-detect from ~/Downloads."
        ),
    )
    parser.add_argument(
        "--signals", required=True, help="Path to signals JSON (signals + closes)"
    )
    parser.add_argument(
        "--export", required=True, help="Output path for the engine state JSON"
    )
    parser.add_argument(
        "--chunker",
        choices=("legacy_dollar", "book"),
        default="legacy_dollar",
        help="Chunker mode: legacy $100K dollar chunker (default) or book-relative",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Echo engine DEBUG detail (dollar figures, per-strategy) to the "
        "console and logs/strategy.log. Without it only INFO/WARNING are logged.",
    )
    args = parser.parse_args()

    # Engine observability: surface the calculator's INFO/WARNING in
    # logs/strategy.log (DEBUG + console with -v).  No decision log here — the
    # per-ticker strategy_decision records come from cli.strategy, not compute.
    observability.setup_logging(
        _ROOT / "logs", verbose=args.verbose, filename="strategy.log"
    )
    _log.info("compute run: chunker=%s verbose=%s", args.chunker, args.verbose)

    # Resolve CSV directory
    if args.inputs:
        inputs_dir = resolve_path(args.inputs)
    else:
        inputs_dir = _find_downloads_csvs()
        if inputs_dir is None:
            print(
                "Error: --inputs not provided and no Fidelity CSVs found in ~/Downloads.\n"
                "       Download position CSVs from Fidelity.com and re-run, or pass --inputs <dir>.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Auto-detected CSVs in: {inputs_dir}", file=sys.stderr)

    signals_path = resolve_path(args.signals)
    export_path = resolve_output_path(args.export)

    signals_data = json.loads(signals_path.read_text(encoding="utf-8"))
    signals: dict[str, dict] = signals_data["signals"]
    closes: dict[str, float] = signals_data.get("closes", {})

    _config_keys_lower = {k.lower(): k for k in ACCOUNTS_CONFIG}
    accounts_raw: dict[str, dict] = {}
    # When the same account appears in more than one CSV (e.g. a re-download
    # saved as "...Jun-01-2026 (1).csv"), keep the file with the NEWEST mtime.
    # A plain dict-overwrite keyed by sorted() filename would let a stale copy
    # win purely on alphabetical order, silently feeding old positions in.
    _chosen_mtime: dict[str, float] = {}
    _chosen_file: dict[str, str] = {}
    for csv_path in sorted(inputs_dir.glob("*.csv")):
        portfolio = read_fidelity_csv(csv_path)
        csv_name = portfolio.account_name
        canonical = _config_keys_lower.get(csv_name.lower())
        if not canonical:
            print(
                f"Warning: '{csv_name}' not in ACCOUNTS_CONFIG — skipped",
                file=sys.stderr,
            )
            continue
        mtime = csv_path.stat().st_mtime
        if canonical in _chosen_mtime and mtime <= _chosen_mtime[canonical]:
            print(
                f"Skipping older CSV for {canonical}: {csv_path.name} "
                f"(keeping newer {_chosen_file[canonical]})",
                file=sys.stderr,
            )
            continue
        if canonical in _chosen_mtime:
            print(
                f"Using newer CSV for {canonical}: {csv_path.name} "
                f"(replaces {_chosen_file[canonical]})",
                file=sys.stderr,
            )
        accounts_raw[canonical] = {
            "positions": {
                sym: p.model_dump() for sym, p in portfolio.positions.items()
            },
            "pending_activity": portfolio.pending_activity,
        }
        _chosen_mtime[canonical] = mtime
        _chosen_file[canonical] = csv_path.name

    if not accounts_raw:
        print("Error: no recognized accounts found in CSV directory", file=sys.stderr)
        sys.exit(1)

    state = _build_state(accounts_raw, signals, closes, chunker=args.chunker)
    save_state(state, export_path)
    print(f"Wrote engine state -> {export_path}")
    _check_mom_sanity_diff(state, export_path)


if __name__ == "__main__":
    main()
