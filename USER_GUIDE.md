# Fidelity Rebalancer — User Guide

> **What this does:** Pulls SectorSurfer signals, reads your Fidelity positions, computes the exact sell/buy orders needed to rebalance across 3 accounts, and walks you through entering each order in ATP. You enter every order manually; the app tells you exactly what to type and in what sequence.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [One-Time Setup](#2-one-time-setup)
3. [Daily Workflow — At a Glance](#3-daily-workflow--at-a-glance)
4. [Step-by-Step Reference](#4-step-by-step-reference)
5. [React Calculator Reference](#5-react-calculator-reference)
6. [Python CLI Reference](#6-python-cli-reference)
7. [Known Limitations](#7-known-limitations)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Prerequisites

### Software

| Requirement            | Notes                                                |
| ---------------------- | ---------------------------------------------------- |
| Python 3.12+           | `python --version` to verify                         |
| A modern browser       | Chrome preferred (for React calculator)              |
| Fidelity Trader+ (FT+) | Open and logged in; Watchlist + L2 panels visible    |
| Internet access        | For SectorSurfer scraper + Yahoo Finance price fetch |

### Python packages

```powershell
cd C:\Users\Jason\Documents\Code\SecSurfTrade

# Core engine
pip install -e "fidelity_rebalancer/[dev]"

# SectorSurfer browser scraper
pip install -e "fidelity_rebalancer/[browser]"
playwright install chromium

# FT+ OCR data capture (one-time)
pip install rapidocr-onnxruntime pillow pywin32
```

`run.ps1` installs all of the above automatically if missing — you do not need to run these manually.

### Accounts configured

Account names, types, strategy allocations, and cash reserves live in **`fidelity_rebalancer/accounts.json`** (gitignored — never committed). On a fresh clone, copy the example and fill in your own account details:

```powershell
Copy-Item fidelity_rebalancer\accounts.example.json fidelity_rebalancer\accounts.json
# Then edit accounts.json with your real account names and allocations
```

Each account entry requires a `csvSlot` field (`"roth"`, `"rollover"`, or `"tod"`) that tells the React calculator which of the three CSV upload areas maps to that account:

```json
{
  "My Retirement Account": {
    "csvSlot": "roth",
    "type": "retirement",
    "strategies": { "Strategy Alpha": 0.25, "Strategy Beta": 0.75 },
    "cashReserve": 0
  }
}
```

Account names must match the **Account Name** field in your Fidelity CSV headers (matching is case-insensitive). The React calculator loads this file at startup — no source changes needed when account details change.

### Strategy name mapping configured

SectorSurfer portal strategy names (e.g. "YE25: Strategy Alpha + frdm - js") differ from the short names used in the engine and `accounts.json`. The mapping lives in **`strategy_map.json`** (gitignored — never committed):

```powershell
Copy-Item strategy_map.example.json strategy_map.json
# Then edit strategy_map.json with your real SectorSurfer portal strategy names
```

Keys beginning with `_` are treated as comments. The mapping tolerates SectorSurfer year-prefix changes (YE25 → YE26) and minor whitespace differences automatically — you only need to edit `strategy_map.json` if SectorSurfer changes the base strategy name itself.

If `strategy_map.json` is missing, the scraper prints a warning and strategy matching is disabled (signals are still collected but may not match engine strategy names).

---

## 2. One-Time Setup

### Launch the app

From the project root in PowerShell:

```powershell
cd C:\Users\Jason\Documents\Code\SecSurfTrade
.\run.ps1
```

`run.ps1` automatically:

1. Verifies Python 3.12+ is in PATH
2. Installs the `fidelity-rebalancer` package if missing
3. Installs the `playwright` package if missing
4. Downloads the Playwright Chromium browser if missing (one-time, ~150 MB)
5. Verifies `server.py` is present and valid
6. Starts the Yahoo Finance proxy server on port 7824
7. Opens the React calculator (`http://localhost:7823/rebalance_calculator.html`) in Chrome

If any step cannot be completed automatically, it prints a plain-English error and exits cleanly.

### First SectorSurfer login

The scraper uses its own Playwright-managed Chromium browser with a persistent profile stored in `.browser_profile/`. Credentials are stored in **Windows Credential Manager** (encrypted with your Windows login via DPAPI) — never in a plaintext file.

**First-time setup:** `run.ps1` checks for stored credentials on startup. If none are found, it prints instructions to run the scraper once — log in manually in the browser window that opens, then enter credentials at the terminal prompt. The scraper's Python-side prompt uses `getpass` to collect the password securely and stores it directly to Windows Credential Manager without ever materialising it in the shell environment.

```powershell
python scripts/sectorsurfer_signals.py --out signals.json
```

After you log in:

- The browser session (cookies) is saved to `.browser_profile/` — you stay logged in across runs until SectorSurfer's session expires.
- If your session is expired, the scraper auto-fills and submits the login form using credentials from Windows Credential Manager.

The Chromium browser window is always visible — you can watch what the scraper is doing.

### Reset credentials

To clear stored credentials (e.g. password changed):

```powershell
python -c "import keyring; keyring.delete_password('sectorsurfer', 'username'); keyring.delete_password('sectorsurfer', 'password')"
```

Then run the scraper — it will open a browser window and wait for you to log in manually, then offer to save the new credentials at the terminal prompt.

### Verify the Python engine (optional)

```powershell
cd fidelity_rebalancer
$env:PYTHONPATH = "."
python -m pytest tests/ -q
# Expected: 289 passed
```

---

## 3. Daily Workflow — At a Glance

```
DAY BEFORE TRADING:
  1. Check SectorSurfer email for signal changes
  2. Run scraper → signals.json (includes prev-day closes)

TRADING MORNING (pre-market):
  3. Run morning-prep.ps1 — validates config, runs scraper, opens calculator
     (Or manually: download 3 Fidelity CSVs, run cli.compute → state.json)
  4. Run scripts/validate_config.py to catch config errors before trading
  5. Run cli.preflight --state state.json — interactive readiness gate:
     • Confirms FT+ is running with every needed ticker in the Watchlist
       and an L2 window open for each thin ticker (7-window cap)
     • Sizes the orders from live FT+ data (pauses for explicit 'yes' before
       any yfinance fallback that sizes without live L2 depth)
     • Runs the pre-trade sanity gate (RED blocks, YELLOW pauses, GREEN proceeds)
  6. Open React calculator → Import State → review Trades tab
     (Alternatively: load CSVs in Setup tab, enter signals, Calculate)

MARKET OPEN:
  7. Use Entry tab in React calculator to enter orders in FT+
     • Sell Round 1 → enter all first-chunk sells across all accounts
     • Check for fills, then Sell Round 2 (if any)
     • Buy Round 1 → enter all first-chunk buys (IRAs: wait for sell proceeds)
     • Repeat for subsequent buy rounds

DURING TRADING:
  8. Log fills in the Trades tab as orders execute
     (Optional: cli.progress --state state.json flags buys behind schedule)

END OF DAY:
  9. Review Allocation tab — confirm drift is within tolerance
     Export State for records if needed
 10. Run cli.eod_report — formats the session's journal log into a
     post-session summary (event tally, notable-events timeline, poll errors)
```

---

## 4. Step-by-Step Reference

### Step 1 — Get SectorSurfer signals

SectorSurfer sends an email 3+ hours before market open when a strategy changes. Run the scraper that morning (or the evening before):

```powershell
cd C:\Users\Jason\Documents\Code\SecSurfTrade
python scripts/sectorsurfer_signals.py --out signals.json
```

A Chromium browser window opens, navigates to sumgrowth.com, and scrapes your strategies. The window closes automatically when done.

Output: `signals.json` — contains signals (current/new ticker per strategy) **and** previous-day closing prices for all tickers via yfinance.

**Login behavior:**

| Scenario                            | What happens                                                                                                                                            |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Session still valid                 | Browser opens, navigates directly to strategies page, scrapes, closes                                                                                   |
| Session expired, credentials stored | Browser opens, auto-fills login form, submits, scrapes, closes                                                                                          |
| Session expired, no credentials     | Browser opens, shows login form — **log in manually in the browser window**, then enter credentials in the terminal when prompted to save for next time |
| Auto-login fails (password changed) | Stored credentials deleted, falls back to manual login                                                                                                  |

**Options:**

| Flag        | Default        | Description                                                      |
| ----------- | -------------- | ---------------------------------------------------------------- |
| `--out`     | `signals.json` | Output file path                                                 |
| `--dry-run` | off            | Print signals to stdout without writing file                     |
| `--debug`   | off            | Save full page screenshot + HTML to `debug/` for troubleshooting |

**What it scrapes:** The [My Strategies page](https://www.sumgrowth.com/MyPages/Strategies2.aspx) at sumgrowth.com. For each strategy, it reads the SELL ticker (what you hold now) and the BUY ticker (what to move to). A HOLD shows the same ticker for both. Only the **Active Strategies** table is read — the Sandbox section below is ignored.

**Strategy mapping** (SectorSurfer portal name → engine name):

The mapping lives in **`strategy_map.json`** (gitignored). See [One-Time Setup](#accounts-configured) for how to configure it. The scraper tolerates year prefix changes (YE25 → YE26) and minor whitespace differences automatically — edit `strategy_map.json` only if SectorSurfer changes the base strategy name itself.

> **Done trades:** If a trade is marked "Done" on the portal (you already executed it this month), the scraper correctly reports the strategy as HOLDing the **new** ticker, not the old one.

### Step 2 — Download Fidelity CSVs

In Fidelity.com (not ATP):

1. Accounts → select account → Positions → **Download** (top-right link)
2. Repeat for all three accounts
3. Save all CSVs to your **Downloads** folder — the engine auto-detects them there

### Step 3 — Compute the trade plan (CLI path)

```powershell
cd C:\Users\Jason\Documents\Code\SecSurfTrade\fidelity_rebalancer
$env:PYTHONPATH = "."

python -m cli.compute --signals ../signals.json --export ../state.json
```

`--inputs` is **optional** — if omitted, the engine scans `~/Downloads` for Fidelity CSVs automatically. Pass it explicitly if your CSVs are elsewhere:

```powershell
python -m cli.compute --inputs ./csvs --signals ../signals.json --export ../state.json
```

**Options:**

| Flag        | Default                        | Description                                                                      |
| ----------- | ------------------------------ | -------------------------------------------------------------------------------- |
| `--inputs`  | auto-detect from `~/Downloads` | Directory containing Fidelity CSV files                                          |
| `--signals` | required                       | Path to `signals.json` from Step 1                                               |
| `--export`  | required                       | Output path for engine state JSON                                                |
| `--chunker` | `legacy_dollar`                | `legacy_dollar` = $100K chunks (matches React calc); `book` = live book-relative |

Output: `state.json` — full engine state with accounts, signals, sells, buys, and chunks.

### Step 4 — Load into the React calculator

**Option A (recommended): Import State**

In the React calculator (http://localhost:7823/rebalance_calculator.html):

1. Click **⬆ Import State** (bottom of Setup tab)
2. Select `state.json`
3. Calculator populates all positions, signals, closes, and computes trades automatically — jumps straight to the Trades tab

**Option B: Manual entry in Setup tab**

If you prefer to stay in the browser:

1. For each account, click **📂 Load file** and select the Fidelity CSV from Downloads — or paste CSV text directly into the textarea
2. Enter signal tickers in the Strategy Signals table
3. Click **⬇ Fetch from Yahoo Finance** to auto-populate previous closes — or enter them manually
4. Click **Calculate Trades →**

### Step 5 — Review the Trades tab

The Trades tab shows the full execution plan:

- **Trade Summary** — how many sells/buys per account, active signals
- **Per-account sections** — Phase 1: SELLS, then Phase 2: BUYS
- **Chunk tables** — each order broken into ≤$100K chunks; limit price is editable
- **Fill tracking** — log fills as they happen; remaining shares/dollars update live

No action needed here before opening — this is the review step. Verify the numbers look correct before moving to the Entry tab.

> **Tip:** Limit prices in the chunk tables are editable. If the market has moved since the close price, adjust before entering in ATP. Changes sync automatically to the Entry tab.

### Step 6 — Enter orders in ATP (Entry tab)

Click the **📥 Entry** tab. This shows one round of orders at a time.

**Round structure:**

- **Sell Round N** — all N-th sell chunks across all accounts (e.g., Round 1 = all first-chunk sells)
- **Buy Round N** — all N-th buy chunks across all accounts

**For sells:**

- Shares are **fixed** — read directly from the chunk; enter this number in ATP
- Adjust limit price if needed; est. proceeds update immediately
- IRAs: Sell simultaneously in both accounts; TOD: sells and buys can go simultaneously

**For buys:**

- Dollar target is **fixed**; you set the limit price
- Shares **auto-calculate** = `floor(dollar_target ÷ price)` — this is what you enter in ATP
- **IRA cash advisory** banner: shows available cash (deplCash + actual sell proceeds) vs. estimated buy cost. Red = sell proceeds not yet settled; advisory only — you decide when to proceed.

**Navigation:**

- **← Prev / Next Round →** — move between rounds
- Progress dots at top — click any dot to jump directly to that round
- **← Trades** — return to the full Trades tab to log fills
- On the last round, button becomes **View Allocation →**

**ATP order entry for each row:**

1. Switch ATP to the correct account (dropdown in ATP)
2. Enter ticker in the order ticket
3. Set Buy/Sell, Shares (from Entry tab), Limit Price (from Entry tab), Day order
4. Preview → Place Order

### Step 7 — Log fills in the Trades tab

As orders execute, return to the Trades tab to record fills:

- In each strategy's **Fills** section, click **+ Fill**
- Enter the fill price and quantity
- Remaining shares/balance updates; PARTIAL → FILLED badge tracks progress
- IRA buy budget recalculates live once any sell fill is logged

### Step 8 — Review the Allocation tab

After trading, click **⚖️ Allocation** to see the full drift picture:

- **Pre-Trade %** — where you were before trading
- **Rec %** — where the calculator targeted
- **Actual %** — where you ended up (based on logged fills)
- **Drift** — distance from target allocation

Use **⬇ Export State** (Trades tab) to save a snapshot of the session for your records. File is named `calc_export_YYYYMMDD_HHMM.json`.

---

## 5. React Calculator Reference

Open at: **http://localhost:7823/rebalance_calculator.html**

### Setup tab

| Section                  | What it does                                                                                                                                                          |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Import Position Data  | Load Fidelity CSVs via **📂 Load file** button or paste. Shows position count when parsed.                                                                            |
| 2. Strategy Signals      | Enter current + new ticker per strategy. NEW field turns red when it differs from CURRENT (= active trade).                                                           |
| 3. Previous Close Prices | Auto-populated from `signals.json` import, or click **⬇ Fetch from Yahoo Finance** to pull live via the local proxy server (no CORS issues). Edit any field manually. |
| Calculate Trades →       | Runs the trade calculation. Resets all fills and chunk overrides.                                                                                                     |
| ⬆ Import State           | Load a `state.json` or `calc_export.json` — populates everything and jumps to Trades.                                                                                 |

**Auto-import via URL:** Append `?import=<relative-path>` to auto-load a state file on page open:

```
http://localhost:7823/rebalance_calculator.html?import=state.json
```

Only same-origin relative paths are accepted — absolute URLs and protocol-relative paths are blocked. The banner at the top shows the import result; dismiss with ✕.

### Trades tab

| Element           | What it does                                                                                     |
| ----------------- | ------------------------------------------------------------------------------------------------ |
| ⬆ Import Fills    | Load a CSV of fill data (columns: Account, Side, Strategy, FillPrice, FillShares)                |
| ⬇ Export Orders   | Download a CSV of all pending chunk orders                                                       |
| ⬇ Export State    | Download a full JSON snapshot (compatible with `cli.compare`)                                    |
| PHASE 1: SELLS    | Per-strategy sell orders with chunk breakdown; editable limit prices                             |
| PHASE 2: BUYS     | Per-strategy buy orders; live recalc when real fill data is entered                              |
| + Fill            | Add a fill row (price × shares) to any strategy's fill tracker                                   |
| LIVE RECALC badge | Appears on buys when actual sell fills have been logged; buy allocation adjusts to real proceeds |

### Entry tab

Round-based order entry helper. See [Step 6](#step-6--enter-orders-in-atp-entry-tab) for full details.

### Allocation tab

Post-trade drift analysis. Shows target %, pre-trade %, recommended %, and actual % for each strategy in each account. Actual column populates once fills are logged in the Trades tab.

### SOP tab

Quick reference card for the trading day sequence.

---

## 6. Python CLI Reference

All commands run from `fidelity_rebalancer/` with `$env:PYTHONPATH = "."`.

### `cli.compute`

Reads Fidelity CSVs + signals JSON → writes engine state JSON.

```powershell
python -m cli.compute --signals ../signals.json --export ../state.json
```

Auto-detects Fidelity CSVs from `~/Downloads` if `--inputs` is omitted.

### `cli.strategy`

Reads Fidelity Trader+ live data via OCR, runs the strategy engine, and writes a state JSON ready for the React calculator. This is the preferred pre-market command when FT+ is open.

```powershell
python -m cli.strategy --state today.json --export today.json --source atp
```

Terminal output includes a realized-volatility block (`sigma=N bps`, daily units: 100 bps = 1% daily vol) and a thin-ticker block if any order exceeds 3% of ADV.

**Options:**

| Flag                     | Default    | Description                                                                                                                                                        |
| ------------------------ | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--state`                | required   | Input state JSON (from `cli.compute`)                                                                                                                              |
| `--export`               | required   | Output path for updated state JSON                                                                                                                                 |
| `--source`               | `yfinance` | `atp` = live FT+ OCR prices; `yfinance` = Yahoo Finance fallback                                                                                                   |
| `--l2-symbols [SYM ...]` | none       | Space-separated tickers for L2 depth / book-relative chunking (e.g. `DFEN PILL`). Pass with no args (`--l2-symbols`) to auto-detect thin tickers (> 3% ADV).       |
| `--verbose` / `-v`       | off        | Print per-trade detail lines: ticker, rule, exact limit price, chunk count. Suppressed by default so that `> run.log` captures only non-sensitive progress output. |

**FT+ layout requirements for `--source atp`:**

- Watchlist must show all signal tickers without scrolling (14 tickers fit comfortably)
- Required columns: Symbol, Last, Bid, Ask, Close, VWAP, Volume, 10D Avg Vol, 90D Avg Vol, Div Ex-Date
- For `--l2-symbols`: each named ticker needs an L2 panel open in FT+

**Smoke test (verify OCR before trading):**

```powershell
# With L2 panel open for TICKER:
python scripts/atp_smoke.py TICKER

# Without L2 panel (watchlist + orders only):
python scripts/atp_smoke.py --skip-l2 TICKER
```

### `cli.preflight`

Interactive morning readiness gate + order-sizing walkthrough. Runs **after** `cli.compute` has produced `state.json` and **before** you enter orders. This is the recommended pre-market command once FT+ is open.

```powershell
python -m cli.preflight --state state.json
```

Three steps, all interactive:

1. **Readiness gate** (loops until GREEN) — confirms Fidelity Trader+ is running, reads the Watchlist via OCR, builds an L2-window plan for thin tickers against the window cap, and verifies every needed ticker is in the Watchlist and every thin ticker has an L2 window. If anything is missing it prints exactly what to add in FT+ and waits for you to press Enter to re-check.
2. **Order sizing** — runs `cli.strategy --source atp --strict-atp` with auto L2 detection. On an OCR shortfall it **pauses** and makes you choose `[R]etry / [Y]es-fall-back-to-yfinance / [A]bort`. The yfinance fallback sizes _without_ live L2 depth and only happens after you type `yes` to confirm — there is **no silent auto-fallback**.
3. **Pre-trade sanity gate** — RED findings block (exit 2), YELLOW findings pause for confirmation, GREEN proceeds. Then it spoonfeeds the exact next steps through to manual order entry.

**Options:**

| Flag                   | Default  | Description                                                              |
| ---------------------- | -------- | ------------------------------------------------------------------------ |
| `--state`              | required | State JSON from `cli.compute` (sized in place)                           |
| `--cap`                | `7`      | Max L2 windows available in FT+                                          |
| `--adv-pct`            | `10.0`   | Flag a chunk as oversized when it exceeds this %% of 10-day ADV          |
| `--confirmed-proceeds` | none     | Passed through to `cli.strategy` (actual sell proceeds per account JSON) |

### `cli.compare`

Compares engine state (from `cli.compute`) against React calculator export (from ⬇ Export State). Used for parity validation.

```powershell
python -m cli.compare --engine state.json --calc calc_export_YYYYMMDD_HHMM.json
```

Green `✓` = match. Red `✗` = discrepancy with exact diff. Zero red lines = engine and React calc agree on all trades and chunk sizes.

### `cli.progress`

Portfolio-level buy progress tracker. Compares buy fill completion (filled shares / target shares) against elapsed trading time and flags buys that are behind schedule. Run it during trading to see whether your buys are keeping pace with the day.

```powershell
python -m cli.progress --state state.json
```

Prints the trading-day percentage elapsed, portfolio buy completion, and a per-buy table marking any buy that is behind schedule (less than half the time-elapsed pace). Read-only — never places orders.

### `cli.eod_report`

End-of-day trade-journal report. Reads the append-only JSONL audit log(s) written by the live monitor (`logs/journal*.jsonl`) and prints a human-readable post-session summary. This tool **never** places trades — it only reads logs.

```powershell
python -m cli.eod_report
python -m cli.eod_report --journal logs/journal_e2e_demo.jsonl
```

The report includes the session span (start/end/duration in local time), an event tally, a chronological **notable-events timeline** (everything except routine `heartbeat`/`poll` noise — including any unknown or newly-introduced event type, so nothing silently vanishes), and a poll-error warnings section. Malformed lines are skipped and counted; unreadable files are reported distinctly.

**Options:**

| Flag        | Default               | Description                                                                                                     |
| ----------- | --------------------- | --------------------------------------------------------------------------------------------------------------- |
| `--journal` | `logs/journal*.jsonl` | Glob or explicit path to journal JSONL file(s). Resolved relative to `fidelity_rebalancer/` if not found as-is. |

### `scripts/sectorsurfer_signals.py`

Scrapes the SectorSurfer portal and fetches previous-day closes via yfinance. See [Step 1](#step-1--get-sectorsurfer-signals).

```powershell
python scripts/sectorsurfer_signals.py --out signals.json
```

| Flag         | Description                                      |
| ------------ | ------------------------------------------------ |
| `--out FILE` | Output path (default: `signals.json`)            |
| `--dry-run`  | Print signals to stdout without writing the file |
| `--debug`    | Save page screenshot + HTML to `debug/`          |

### `scripts/morning-prep.ps1`

Pre-market automation wrapper. Runs the scraper, validates config, and opens the calculator — single command for the full morning setup.

```powershell
.\scripts\morning-prep.ps1
```

Exits with an error if `accounts.json` or `strategy_map.json` is missing or invalid, before running the scraper.

### `scripts/validate_config.py`

Checks `accounts.json` and (optionally) `signals.json` for common configuration errors before running the engine.

```powershell
python scripts/validate_config.py
python scripts/validate_config.py --accounts fidelity_rebalancer/accounts.json --signals signals.json
```

Catches: missing required fields, allocation weights that don't sum to 1.0, account names with no `csvSlot`, strategy names in signals that don't match `accounts.json`.

### `scripts/exdiv_dryrun.py`

Dry-run for the ex-dividend close-price adjustment. Run before a trading window where any held ticker has an ex-dividend date on or after the window date to confirm the engine adjusts the previous close correctly.

```powershell
python scripts/exdiv_dryrun.py
```

### `scripts/stall_rehearsal.py`

Exercises the stall-detection engine without a live ATP connection. Verifies `detect_stalls()` and `recommend_requote()` against a mock partially-filled order. Run before a trading window to confirm stall detection is working.

```powershell
python scripts/stall_rehearsal.py
```

---

## 7. Known Limitations

### No ATP write automation

The app is read-only. It tells you exactly what to enter in ATP; you type it. Automated order placement is deferred to Phase B pending validation of the recommendation engine over multiple trading windows.

### IRA buy timing is advisory

IRAs have no margin — buys must wait for sell proceeds to settle. The Entry tab shows an advisory cash banner but does not enforce a gate. You decide when to proceed.

### Yahoo Finance fetch requires the local server

The **⬇ Fetch from Yahoo Finance** button routes through the local proxy server (`server.py`) started by `run.ps1`. If the fetch fails, verify `run.ps1` is still running, or use Import State — `signals.json` already contains closes from the scraper run.

### L2/book chunker (thin tickers only)

`--l2-symbols` on `cli.strategy` fetches live L2 depth from open FT+ L2 panels via OCR and switches affected tickers to book-relative chunking. Validated in live testing (A-5). Only needed for tickers where spread and book depth meaningfully affect execution (DFEN, PILL, EPOL, BULZ). Standard tickers (SPY, QQQ, EEM) use `legacy_dollar` chunking automatically.

### Chunk IDs don't appear in ATP

To match a chunk row to an ATP order: use account + ticker + share quantity + limit price. Chunk IDs are internal.

### Single-instance ATP, 3 accounts

ATP supports one active account at a time via the account dropdown. The Entry tab groups all accounts in each round — switch accounts between rows as needed.

### Scraper browser is always visible

The Playwright Chromium window opens on every scraper run and closes when done. This is intentional — you can see exactly what the scraper is doing and intervene if needed.

---

## 8. Troubleshooting

| Symptom                                                           | Likely cause                                                   | Fix                                                                                                                                                                                                                                                                     |
| ----------------------------------------------------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `accounts.json not found`                                         | First-time setup                                               | Copy `accounts.example.json` to `accounts.json` and fill in your account names and allocations                                                                                                                                                                          |
| `no recognized accounts found in CSV directory`                   | Account name mismatch                                          | Check CSV header; matching is case-insensitive. New/unrecognized account names print a warning — add them to `accounts.json`                                                                                                                                            |
| `--inputs not provided and no Fidelity CSVs found in ~/Downloads` | CSVs not downloaded yet, or not in Downloads                   | Download position CSVs from Fidelity.com or pass `--inputs <dir>` explicitly                                                                                                                                                                                            |
| Scraper: `Unrecognized portal strategies`                         | SectorSurfer renamed a strategy (and it has a SELL/BUY ticker) | Update `strategy_map.json` — add the new portal name as a key mapping to your engine strategy name                                                                                                                                                                      |
| Scraper: auto-login fails every run                               | Password changed, or stored credentials are wrong              | Delete keyring entries (`python -c "import keyring; keyring.delete_password('sectorsurfer','username'); keyring.delete_password('sectorsurfer','password')"`), run scraper, log in manually, save new credentials when prompted                                         |
| Scraper: session expired on every run                             | `.browser_profile/` deleted or corrupted                       | Delete `.browser_profile/` entirely and run the scraper — log in manually and save credentials again                                                                                                                                                                    |
| Scraper: `[ACTION REQUIRED]` prompt appears                       | Browser session expired and no stored credentials              | Log in at the Chromium browser window; optionally save credentials at the terminal prompt                                                                                                                                                                               |
| Scraper: SELL: wait times out, returns 0 signals                  | Login panel still showing (not actually logged in)             | Check if the Chromium window shows the login form; log in manually. If it happens consistently, clear keyring entries and re-save: `python -c "import keyring; keyring.delete_password('sectorsurfer','username'); keyring.delete_password('sectorsurfer','password')"` |
| Scraper: SELL: wait times out, returns `... loading ...`          | AJAX data didn't load in 20s                                   | Run with `--debug` to capture a screenshot; may be a slow connection or site outage                                                                                                                                                                                     |
| Yahoo Finance fetch returns nothing                               | Yahoo unreachable or market closed                             | Use Import State (closes are in `signals.json`) or enter prices manually                                                                                                                                                                                                |
| Yahoo Finance fetch: "Fetch failed: Server error"                 | `run.ps1` server not running                                   | Restart `.\run.ps1`; the proxy server must be running for the fetch to work                                                                                                                                                                                             |
| React calc shows wrong closes after CSV load                      | No closes in CSV; closes come from signals                     | Run scraper first, then Import State; or use Fetch from Yahoo Finance                                                                                                                                                                                                   |
| Entry tab shows "Run calculation from Setup first"                | No calculation run yet                                         | Go to Setup tab, load data, click Calculate Trades → (or Import State)                                                                                                                                                                                                  |
| IRA cash advisory shows red on Buy Round 1                        | Sell proceeds not yet settled                                  | Advisory only — wait for ATP to show fills, log them in Trades tab, then proceed                                                                                                                                                                                        |
| `cli.compare` shows mismatches                                    | Engine and React calc use different prices                     | Confirm `prev_closes` in both exports match; rerun compute with the same signals.json used in React                                                                                                                                                                     |
| `run.ps1` exits with "Python not found"                           | Python not in PATH                                             | Install Python 3.12+ from python.org; ensure "Add to PATH" was checked                                                                                                                                                                                                  |
| `run.ps1` exits with "pip install failed"                         | No internet or corporate proxy                                 | Run `pip install -e fidelity_rebalancer` manually from a network that allows PyPI                                                                                                                                                                                       |
| `run.ps1` exits with "Chromium install failed"                    | Disk space or proxy issue                                      | Run `python -m playwright install chromium` manually                                                                                                                                                                                                                    |
| `ModuleNotFoundError` on any Python module                        | Package not installed                                          | Re-run `.\run.ps1` — it installs missing packages automatically                                                                                                                                                                                                         |
| Tests fail / unexpected failures                                  | Dependencies out of sync                                       | `pip install -e "fidelity_rebalancer/[dev]"` and re-run; expected: 289 passed                                                                                                                                                                                           |
