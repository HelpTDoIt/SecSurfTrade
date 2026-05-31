# SecSurfTrade — Fidelity Rebalancer

A personal portfolio rebalancing tool for Fidelity brokerage accounts using [SectorSurfer](https://www.sumgrowth.com) signals.

Pulls SectorSurfer signals, reads Fidelity position CSVs, computes the exact sell/buy orders needed to rebalance across multiple accounts, and walks you through entering each order in Fidelity Active Trader Pro (ATP). **The app never places orders** — you enter every order manually; the app tells you exactly what to type and in what sequence.

---

## What it does

- **Scrapes SectorSurfer** (sumgrowth.com) to get current SELL/BUY signals per strategy
- **Reads Fidelity CSVs** from your Downloads folder to build the current portfolio
- **Computes trade plans** — sells, buys, and ≤$100K execution chunks — matching your strategy allocations
- **React calculator** — browser-based UI for reviewing trades, logging fills, and tracking allocation drift
- **OCR adapters** — reads live bid/ask/L2 from Fidelity Trader+ via screen capture for limit price generation
- **Parity check** — validates engine output against the React calculator's computed trades

---

## Quick start

```powershell
# Clone and launch
git clone https://github.com/HelpTDoIt/SecSurfTrade.git
cd SecSurfTrade

# First-time: copy the account template and fill in your account details
Copy-Item fidelity_rebalancer\accounts.example.json fidelity_rebalancer\accounts.json
# Edit accounts.json with your real account names, types, strategy allocations

# Launch (installs all dependencies automatically)
.\run.ps1
```

`run.ps1` verifies Python 3.12+, installs packages, downloads the Playwright Chromium browser if needed, and opens the React calculator at `http://localhost:7823/rebalance_calculator.html`.

See [USER_GUIDE.md](USER_GUIDE.md) for the full daily workflow.

---

## Requirements

- Windows 10/11
- Python 3.12+
- Fidelity Active Trader Pro (open and logged in, for OCR reads)
- A SectorSurfer / SumGrowth account

---

## Project structure

```
SecSurfTrade/
├── run.ps1                         # one-click launcher and dependency checker
├── server.py                       # local Yahoo Finance proxy (CORS bridge for React calc)
├── rebalance_calculator.html       # React calculator (CDN React, no build step)
├── strategy_map.example.json       # template — copy to strategy_map.json and fill in yours
├── scripts/                        # operational scripts — run these on trading days
│   ├── sectorsurfer_signals.py     #   SectorSurfer scraper → signals.json
│   ├── morning-prep.ps1            #   pre-market automation wrapper (1-command setup)
│   ├── validate_config.py          #   pre-trade config sanity check
│   ├── exdiv_dryrun.py             #   ex-dividend adjustment dry-run
│   └── stall_rehearsal.py          #   stall-detection rehearsal
├── docs/                           # reference documentation
│   ├── test_plan_atp_integration.md
│   ├── test_plan_trading_window.md
│   └── dev/                        #   per-phase implementation specs (development history)
├── README.md
├── USER_GUIDE.md                   # daily workflow, setup, CLI reference
├── 00_ARCHITECTURE.md              # system architecture and design decisions
└── fidelity_rebalancer/            # Python package
    ├── accounts.example.json       #   template — copy to accounts.json and fill in yours
    ├── pyproject.toml
    ├── engine/                     #   pure calculation logic (no I/O)
    ├── adapters/                   #   ATP OCR readers, CSV importer, mock ATP
    ├── state/                      #   Pydantic schema, state import/export, parity diff
    ├── tui/                        #   Textual terminal UI (order approval, live monitor)
    ├── cli/                        #   CLI entry points (compute, strategy, compare)
    ├── scripts/                    #   dev/diagnostic utilities (not part of daily workflow)
    └── tests/                      #   182 unit tests + fixtures
```

---

## Security

This repository is safe to make public. Sensitive data is kept out of the repo by design:

| Data                                     | Where it lives                                                                      |
| ---------------------------------------- | ----------------------------------------------------------------------------------- |
| Account names and allocations            | `accounts.json` — gitignored, never committed                                       |
| SectorSurfer credentials                 | Windows Credential Manager (via OS keyring) — never on disk in plaintext            |
| Fidelity position CSVs                   | `~/Downloads/` — never committed (`*.csv` gitignored)                               |
| Browser session cookies                  | `.browser_profile/` — gitignored                                                    |
| Per-trade detail (tickers, limit prices) | Routed to `logging.DEBUG`, suppressed by default; use `--verbose` on `cli.strategy` |

---

## Running the tests

```powershell
cd fidelity_rebalancer
$env:PYTHONPATH = "."
python -m pytest tests/ -q
# Expected: 182 passed
```
