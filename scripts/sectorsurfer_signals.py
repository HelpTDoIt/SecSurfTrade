#!/usr/bin/env python3
"""
SectorSurfer / SumGrowth signal scraper.

Reads the My Strategies page at sumgrowth.com and writes signals.json
for use with cli.compute.

Usage:
    python scripts/sectorsurfer_signals.py [--out signals.json] [--debug] [--dry-run]

First run: a browser window opens. Log in manually in the browser — the script
waits. After login, it offers to save your credentials in the terminal so future
runs auto-login.  Credentials are stored in the OS keyring (Windows Credential
Manager on Windows).
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

# ── configuration ──────────────────────────────────────────────────────────────

PORTAL_URL = "https://www.sumgrowth.com/MyPages/Strategies2.aspx"

# Persistent browser profile — stores cookies so login survives between runs.
PROFILE_DIR = Path(__file__).resolve().parent.parent / ".browser_profile"


def _load_strategy_map() -> dict[str, str]:
    """Load portal→engine strategy name mappings from strategy_map.json (gitignored).

    Keys beginning with '_' are treated as comments and skipped.
    Matching uses _resolve_strategy() which normalizes year prefixes (YExx:) and
    whitespace, so the map survives the annual year rollover without edits.

    To set up: copy strategy_map.example.json to strategy_map.json and fill in
    your actual SectorSurfer portal strategy names.
    """
    map_file = Path(__file__).resolve().parent.parent / "strategy_map.json"
    if not map_file.exists():
        sys.stderr.write(
            f"WARNING: strategy_map.json not found — strategy matching disabled.\n"
            f"  Copy strategy_map.example.json to strategy_map.json and add your names.\n"
        )
        sys.stderr.flush()
        return {}
    try:
        data = json.loads(map_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            sys.stderr.write(
                f"ERROR: strategy_map.json must be a JSON object, got {type(data).__name__}\n"
            )
            sys.stderr.flush()
            return {}
        return {k: v for k, v in data.items() if not str(k).startswith("_")}
    except Exception as exc:
        sys.stderr.write(f"ERROR reading strategy_map.json: {exc}\n")
        sys.stderr.flush()
        return {}


STRATEGY_MAP: dict[str, str] = _load_strategy_map()

# Portal rows that are sub-strategies or benchmarks — never trade directly.
_IGNORE_EXACT: frozenset[str] = frozenset({"BMS-RR", "SSTOP"})
_IGNORE_PREFIX: tuple[str, ...] = ("P:",)


def _ignore(name: str) -> bool:
    return name in _IGNORE_EXACT or any(name.startswith(p) for p in _IGNORE_PREFIX)


import re as _re


def _normalize(name: str) -> str:
    """Strip leading year prefix (YExx:) and collapse whitespace, lowercase."""
    name = _re.sub(r"^YE\d{2,4}:\s*", "", name.strip())
    return " ".join(name.split()).lower()


def _resolve_strategy(portal_name: str) -> str | None:
    """Map a portal strategy name to an engine name, tolerating minor variations."""
    # 1. Exact match
    if portal_name in STRATEGY_MAP:
        return STRATEGY_MAP[portal_name]
    # 2. Normalized match (covers year rollover and extra whitespace)
    target = _normalize(portal_name)
    for key, value in STRATEGY_MAP.items():
        if _normalize(key) == target:
            return value
    return None


def _strip_dash(ticker: str) -> str:
    """Remove trailing dash (SectorSurfer extended-hours notation)."""
    return ticker.strip().rstrip("-")


# ── logging ────────────────────────────────────────────────────────────────────


def _log(msg: str) -> None:
    """Write a timestamped diagnostic line to stderr, flushed immediately."""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


# ── in-browser JavaScript extraction ──────────────────────────────────────────

# Walks the live DOM starting from the FIRST TABLE on the page (the Active
# Strategies table).  Scoping to the first table prevents duplicate strategy
# names in the Sandbox section below from being matched — sandbox entries
# would otherwise overwrite the correct signals.
#
# Merges text nodes with <input> values so strategy names (inputs) and
# SELL/BUY signals (text nodes) appear in the correct sequence for parsing.
_EXTRACT_JS = r"""
(() => {
    const strip = s => (s || '').trim().replace(/-+$/, '');
    const INPUT_SENTINEL = '\x00';   // marks tokens that came from <input>

    // ── scope to first table (Active Strategies section) ──────────────────
    // The Sandbox section below the main table contains duplicate strategy
    // names. Scoping to the first table prevents those from being matched.
    const strategyRoot = document.querySelector('table') || document.body;

    // ── build ordered token list ───────────────────────────────────────────
    const tokens = [];
    (function walk(node) {
        if (node.nodeType === Node.TEXT_NODE) {
            const t = node.textContent.trim();
            if (t) tokens.push(t);
        } else if (node.nodeType === Node.ELEMENT_NODE) {
            if ((node.tagName === 'INPUT') &&
                (node.type === 'text' || node.type === '')) {
                const v = node.value.trim();
                if (v) tokens.push(INPUT_SENTINEL + v);
            }
            for (const child of node.childNodes) walk(child);
        }
    })(strategyRoot);

    // ── parse token stream ─────────────────────────────────────────────────
    const strategies = [];
    let cur = null;

    for (let i = 0; i < tokens.length; i++) {
        const tok = tokens[i];

        // Belt-and-suspenders: stop if we hit a sandbox/test section header,
        // even though we already scoped to the first table above.
        if (/^sandbox/i.test(tok)) break;

        // Input token → potential strategy name.
        // Heuristic: longer than 3 chars, not a bare ticker, not a number,
        // and NOT an internal identifier like "BMS-BSD" (ALL_CAPS-ALL_CAPS).
        // BMS-* sub-strategy IDs appear in the DOM and would otherwise
        // interrupt SELL/BUY token parsing for the preceding strategy.
        if (tok.startsWith(INPUT_SENTINEL)) {
            const name = tok.slice(1);
            if (name.length > 3
                && !/^[A-Z]{1,6}-?$/.test(name)       // not a bare ticker
                && !/^\d/.test(name)) {                // not a number
                cur = { name, sell: null, buy: null, has_trade: false, is_done: false };
                strategies.push(cur);
            }
            continue;
        }

        if (!cur) continue;

        // Helper: skip past INPUT sentinel tokens to find the next plain text.
        // BMS-* and other sentinel tokens can appear between "SELL:"/"BUY:" and
        // the actual ticker, causing the ticker to be missed without this.
        const nextText = (fromIdx) => {
            let j = fromIdx;
            while (j < tokens.length && tokens[j].startsWith(INPUT_SENTINEL)) j++;
            return { val: (tokens[j] || '').trim(), idx: j };
        };

        // ── SELL signal (two forms: "SELL:" + next token, or "SELL: TICK" combined)
        if (tok === 'SELL:') {
            const { val, idx } = nextText(i + 1);
            if (/^[A-Z]/.test(val)) { cur.sell = strip(val); i = idx; }
            continue;
        }
        const sellM = tok.match(/^SELL:\s+([A-Z][A-Z0-9]*-?)$/);
        if (sellM) { cur.sell = strip(sellM[1]); continue; }

        // ── BUY signal
        if (tok === 'BUY:') {
            const { val, idx } = nextText(i + 1);
            if (/^[A-Z]/.test(val) || val === 'Rebal' || val === '-New-') {
                cur.buy = (val === 'Rebal' || val === '-New-')
                    ? (cur.sell || '')
                    : strip(val);
                i = idx;
            }
            continue;
        }
        const buyM = tok.match(/^BUY:\s+([A-Z][A-Z0-9]*-?|Rebal|-New-)$/);
        if (buyM) {
            const val = buyM[1];
            cur.buy = (val === 'Rebal' || val === '-New-')
                ? (cur.sell || '')
                : strip(val);
            continue;
        }

        // ── trade status
        // "Acknowledge Trade" = pending trade (not yet executed by user)
        // "Done:date"         = trade already executed; strategy now holds BUY ticker
        if (tok === 'Acknowledge Trade') { cur.has_trade = true; }
        if (/^Done:\d/.test(tok))        { cur.is_done  = true; }
    }

    return strategies;
})()
"""


# ── credential storage ────────────────────────────────────────────────────────

_KEYRING_SERVICE = "sectorsurfer"
CREDS_FILE = PROFILE_DIR / "creds.json"  # kept for one-time migration only


def _load_creds_legacy() -> dict | None:
    """Read credentials from the old plaintext creds.json (migration path only)."""
    if not CREDS_FILE.exists():
        return None
    try:
        data = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
        if data.get("username") and data.get("password"):
            _log("Found legacy creds.json.")
            return data
    except Exception as exc:
        _log(f"Failed to read legacy creds.json: {exc}")
    return None


def _load_creds() -> dict | None:
    """
    Return stored {username, password} from the OS keyring, or None.

    On Windows the keyring backend is Windows Credential Manager (DPAPI-
    encrypted).  On macOS it is Keychain; on Linux, Secret Service.

    If nothing is found in the keyring but a legacy creds.json exists, the
    credentials are migrated to the keyring and the plaintext file is deleted.
    """
    try:
        import keyring
    except ImportError:
        _log("keyring not installed — cannot load credentials.")
        return None

    try:
        username = keyring.get_password(_KEYRING_SERVICE, "username")
        password = keyring.get_password(_KEYRING_SERVICE, "password")
    except Exception as exc:
        _log(f"keyring read failed: {exc}")
        return None

    if username and password:
        _log("Loaded stored credentials from keyring.")
        return {"username": username, "password": password}

    # Nothing in keyring — check for legacy plaintext file and migrate.
    legacy = _load_creds_legacy()
    if legacy:
        _log("Migrating credentials from creds.json → keyring...")
        try:
            keyring.set_password(_KEYRING_SERVICE, "username", legacy["username"])
            keyring.set_password(_KEYRING_SERVICE, "password", legacy["password"])
            CREDS_FILE.unlink(missing_ok=True)
            _log("Migration complete — creds.json deleted.")
        except Exception as exc:
            _log(f"Migration to keyring failed ({exc}) — creds.json kept.")
        return legacy

    _log("No stored credentials found in keyring.")
    return None


def _save_creds(username: str, password: str) -> None:
    """Persist credentials to the OS keyring (Windows Credential Manager on Windows)."""
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, "username", username)
        keyring.set_password(_KEYRING_SERVICE, "password", password)
        _log("Credentials saved to keyring.")
        if CREDS_FILE.exists():
            CREDS_FILE.unlink(missing_ok=True)
            _log("Removed legacy creds.json.")
    except ImportError:
        _log("keyring not installed — cannot save credentials.")
    except Exception as exc:
        _log(f"keyring write failed: {exc}")


def _do_login(page, creds: dict) -> bool:
    """
    Fill and submit the SectorSurfer login form using stored credentials.
    Returns True if the login panel disappears (success).

    Scopes field search to the pnlLogin panel so we never accidentally
    match hidden registration fields like txtCreateUserName.
    Tries clicking the visible submit button; falls back to pressing Enter.
    """
    _log("Auto-login: filling form...")
    try:
        # Scope everything to the login panel — prevents matching
        # hidden inputs like txtCreateUserName (registration form).
        panel = page.locator("[id*='pnlLogin']").first
        _log("  Waiting for login panel to be visible (up to 5s)...")
        panel.wait_for(state="visible", timeout=5_000)

        # Username: first visible text input inside the panel
        user_field = panel.locator("input[type='text'], input[type='']").first
        user_field.wait_for(state="visible", timeout=5_000)
        user_field.fill(creds["username"])
        _log(f"  Username filled (element id={user_field.get_attribute('id')!r}).")

        # Password
        pass_field = panel.locator("input[type='password']").first
        pass_field.fill(creds["password"])
        _log("  Password filled.")

        # Submit: click the visible submit button inside the panel.
        # ASP.NET Login control renders an input[type=submit] inside the panel.
        # Fall back to pressing Enter if no button is found.
        submitted = False
        for sel in (
            "input[type='submit']",
            "button[type='submit']",
            "input[type='button']",
        ):
            try:
                btn = panel.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=1_000):
                    btn.click()
                    _log(f"  Submit button clicked ({sel}).")
                    submitted = True
                    break
            except Exception:
                continue
        if not submitted:
            _log("  No visible submit button found — pressing Enter on password field.")
            pass_field.press("Enter")

        # Confirm login: wait for the panel to disappear from DOM or become hidden.
        # Using wait_for_function covers both hide-in-place and full page navigation.
        _log("  Waiting up to 15s for login to complete...")
        page.wait_for_function(
            """() => {
                const p = document.querySelector('[id*="pnlLogin"]');
                if (!p) return true;
                const s = window.getComputedStyle(p);
                // offsetParent===null on position:fixed elements even when visible —
                // use display/visibility and bounding rect instead.
                if (s.display === 'none' || s.visibility === 'hidden') return true;
                const r = p.getBoundingClientRect();
                return r.width === 0 && r.height === 0;
            }""",
            timeout=15_000,
        )
        _log("  Login panel gone — auto-login succeeded.")
        return True
    except Exception as exc:
        _log(f"  Auto-login failed: {type(exc).__name__}: {exc}")
        return False


def _offer_save_creds() -> None:
    """
    After a successful manual browser login, offer to save credentials in
    the terminal so future runs auto-login.

    The browser window is open and the user just logged in there.  We prompt
    here in the terminal (read from stdin) — the two inputs are decoupled.
    Skipped silently if the terminal is not interactive (redirected stdin).
    """
    _log("Offering to save credentials for future auto-login...")
    try:
        import getpass

        sys.stderr.write(
            "\n"
            "  Login detected!\n"
            "  Save credentials for auto-login next time?\n"
            "  Username (press Enter to skip): "
        )
        sys.stderr.flush()
        username = input().strip()
        if not username:
            _log("  User pressed Enter — credential save skipped.")
            return
        _log("  Username entered. Prompting for password...")
        password = getpass.getpass("  Password: ")
        if password:
            _save_creds(username, password)
        else:
            _log("  Empty password entered — credentials NOT saved.")
    except (EOFError, OSError):
        # Non-interactive terminal (scheduled task, redirected stdin, etc.)
        _log("  Non-interactive terminal — credential save skipped.")
    except Exception as exc:
        _log(f"  Credential save error: {exc}")


# ── browser scrape ─────────────────────────────────────────────────────────────


def _scrape(*, debug: bool) -> list[dict]:
    _log("Importing playwright...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _log(
            "ERROR: playwright not installed. Run: pip install playwright && playwright install chromium"
        )
        sys.exit(1)

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"Profile directory: {PROFILE_DIR}")

    with sync_playwright() as pw:
        # Playwright bundled Chromium with a persistent profile.
        # Sessions (cookies) are saved in .browser_profile/ — login is only
        # needed once, or when SectorSurfer's session expires.
        # Credentials are stored in the OS keyring for auto-login.
        _log(f"Launching Chromium with persistent profile: {PROFILE_DIR}")
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        _log("Browser launched.")

        # ── open scraper tab ──────────────────────────────────────────────────
        _log("Opening new tab...")
        page = ctx.new_page()
        _log(f"Navigating to {PORTAL_URL} ...")

        try:
            page.goto(PORTAL_URL, wait_until="load", timeout=30_000)
            _log(f"Page load complete. URL: {page.url}")

            # Detect login by page CONTENT (not URL — SectorSurfer shows the
            # login form at the strategies2.aspx URL itself, no redirect).
            def _login_visible() -> bool:
                try:
                    v = page.locator(
                        "[id*='pnlLogin'],[name*='pnlLogin']"
                    ).first.is_visible(timeout=2_000)
                    _log(f"  pnlLogin visible check: {v}")
                    return v
                except Exception as exc:
                    fallback = "strategies2" not in page.url.lower()
                    _log(
                        f"  pnlLogin check raised {type(exc).__name__} — URL fallback: "
                        f"'strategies2' in URL={not fallback}, so login_visible={fallback}"
                    )
                    return fallback

            login_visible = _login_visible()
            _log(f"Login required: {login_visible}")

            if login_visible:
                creds = _load_creds()
                logged_in = False

                if creds is not None:
                    # ── path A: auto-login with stored credentials ────────────
                    _log("Path A: attempting auto-login with stored credentials...")
                    logged_in = _do_login(page, creds)
                    if logged_in:
                        _log("Auto-login succeeded. Refreshing stored credentials.")
                        _save_creds(creds["username"], creds["password"])
                    else:
                        _log(
                            "Auto-login FAILED (wrong credentials?). "
                            "Clearing stored credentials and switching to manual login."
                        )
                        if CREDS_FILE.exists():
                            CREDS_FILE.unlink()
                            _log(f"Deleted {CREDS_FILE}")

                if not logged_in:
                    # ── path B: manual login ──────────────────────────────────
                    # The browser is already open and showing the login page.
                    # The user logs in directly in the browser window.
                    # We wait here for the login panel to disappear.
                    # After successful login, we offer to save credentials in
                    # the terminal — this is SEPARATE from the browser action.
                    _log("Path B: waiting for manual login in browser window...")
                    print(
                        "\n"
                        "  [ACTION REQUIRED] Please log in at the SectorSurfer browser window.\n"
                        "  Waiting up to 2 minutes...",
                        file=sys.stderr,
                        flush=True,
                    )
                    try:
                        page.wait_for_function(
                            """() => {
                                const p = document.querySelector('[id*="pnlLogin"]');
                                if (!p) return true;
                                const s = window.getComputedStyle(p);
                                // offsetParent===null on position:fixed elements even when visible —
                                // use display/visibility and bounding rect instead.
                                if (s.display === 'none' || s.visibility === 'hidden') return true;
                                const r = p.getBoundingClientRect();
                                return r.width === 0 && r.height === 0;
                            }""",
                            timeout=120_000,
                        )
                        _log("Login panel disappeared — manual login detected.")
                        logged_in = True
                        # Offer to save credentials AFTER browser login is done.
                        # input() reads from the terminal (stdin), not the browser.
                        _offer_save_creds()
                    except BaseException as exc:
                        # BaseException catches Ctrl+C (KeyboardInterrupt) so cleanup
                        # can run immediately rather than hanging in the wait loop.
                        _log(f"Manual login wait ended: {type(exc).__name__}: {exc}")
                        print(
                            "  Warning: login panel did not disappear. Proceeding anyway.",
                            file=sys.stderr,
                            flush=True,
                        )

                if logged_in:
                    _log("Re-navigating to strategies page (authenticated)...")
                    page.goto(PORTAL_URL, wait_until="load", timeout=30_000)
                    _log(f"Re-navigation complete. URL: {page.url}")
                else:
                    _log(
                        "Warning: not confirmed logged in. Attempting extraction anyway."
                    )
            else:
                _log("Already authenticated — proceeding directly to extraction.")

            # Wait for strategy AJAX data to finish rendering.
            # The page initially shows "... loading ..." and populates
            # SELL:/BUY: rows asynchronously — without this wait, the extractor
            # runs on the loading skeleton and returns nothing useful.
            _log("Waiting for SELL:/BUY: tokens to appear in page (AJAX render)...")
            try:
                page.wait_for_function(
                    "document.body.innerText.includes('SELL:')",
                    timeout=20_000,
                )
                _log("SELL: tokens found in page text. Proceeding with extraction.")
            except Exception as exc:
                _log(
                    f"Wait for SELL: timed out ({exc}). Extracting whatever is available."
                )

            if debug:
                dbg = Path(__file__).resolve().parent.parent / "debug"
                dbg.mkdir(exist_ok=True)
                page.screenshot(path=str(dbg / "ss_strategies.png"), full_page=True)
                (dbg / "ss_strategies.html").write_text(
                    page.content(), encoding="utf-8"
                )
                _log(f"Debug files written to {dbg}/")

            _log("Running DOM extraction JavaScript (scoped to first table)...")
            raw: list[dict] = page.evaluate(_EXTRACT_JS)
            _log(f"Extraction complete: {len(raw)} rows returned.")
            for r in raw:
                _log(f"  raw: {r}")

        finally:
            # Close the whole context — kills all pages in it.
            # ctx.close() from the main thread is safe because Path B catches
            # BaseException (including Ctrl+C), so the greenlet is never left
            # mid-switch when we reach here.
            _log("Cleanup: closing browser...")
            try:
                ctx.close()
                _log("Cleanup: browser closed.")
            except BaseException as exc:
                _log(f"Cleanup: browser close error — {type(exc).__name__}: {exc}")

    return raw


# ── signal assembly ────────────────────────────────────────────────────────────


def _fetch_closes(tickers: list[str]) -> dict[str, float]:
    """Previous-day closing prices via yfinance."""
    closes: dict[str, float] = {}
    if not tickers:
        return closes
    _log(f"Fetching closes for: {', '.join(tickers)}")
    try:
        import yfinance as yf
    except ImportError:
        _log("Warning: yfinance not installed — closes omitted.")
        return closes

    for ticker in tickers:
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if not hist.empty:
                price = round(float(hist["Close"].iloc[-1]), 4)
                closes[ticker] = price
                _log(f"  {ticker}: {price}")
            else:
                _log(
                    f"  {ticker}: no history (market closed, bad ticker, or de-listed?)"
                )
        except Exception as exc:
            _log(f"  {ticker}: fetch error — {exc}")

    return closes


def build_signals(raw: list[dict], *, verbose: bool = True) -> dict:
    signals: dict[str, dict] = {}
    unrecognized: list[str] = []

    _log(f"Building signals from {len(raw)} raw portal rows...")

    for row in raw:
        portal_name: str = (row.get("name") or "").strip()
        sell: str = _strip_dash(row.get("sell") or "")
        buy: str = _strip_dash(row.get("buy") or "")
        has_trade: bool = bool(row.get("has_trade"))
        is_done: bool = bool(row.get("is_done"))

        if _ignore(portal_name):
            _log(f"  IGNORE  {portal_name!r}")
            continue

        calc_name = _resolve_strategy(portal_name)
        if calc_name is None:
            # Only warn about rows that have at least one ticker — those could
            # be real strategies whose portal name has changed.  Rows with no
            # sell and no buy are header / billing inputs that leaked through
            # the DOM walker (e.g. account name, "Monthly Billing: $71.85").
            # Log them for debugging but don't surface them as user warnings.
            if sell or buy:
                unrecognized.append(portal_name)
                _log(
                    f"  UNRECOGNIZED  {portal_name!r}  (has tickers but not in STRATEGY_MAP)"
                )
            else:
                _log(f"  SKIP (no tickers, not a strategy row)  {portal_name!r}")
            continue

        # current = what the strategy holds RIGHT NOW
        # new     = what it should move to (same as current for a HOLD)
        #
        # When is_done=True, the trade was already executed by the user.
        # The strategy now holds the BUY ticker, not the SELL ticker.
        if is_done and buy:
            current = buy
            new = buy
            _log(
                f"  MATCH   {portal_name!r} → {calc_name!r} | "
                f"sell={sell!r} buy={buy!r} is_done=True → HOLD {buy!r}"
            )
        else:
            current = sell
            new = buy if buy else current
            _log(
                f"  MATCH   {portal_name!r} → {calc_name!r} | "
                f"sell={sell!r} buy={buy!r} has_trade={has_trade} → "
                f"current={current!r} new={new!r}"
            )

        signals[calc_name] = {"current": current, "new": new}

        if verbose:
            if has_trade and not is_done:
                print(f"  {calc_name:<25} TRADE  {current} → {new}", file=sys.stderr)
            else:
                print(f"  {calc_name:<25} HOLD   {current}", file=sys.stderr)

    if unrecognized:
        print(
            "\nUnrecognized portal strategies (not in STRATEGY_MAP):\n"
            + "\n".join(f"  {n!r}" for n in unrecognized),
            file=sys.stderr,
        )

    all_tickers = sorted(
        {t for sig in signals.values() for t in (sig["current"], sig["new"]) if t}
    )
    closes = _fetch_closes(all_tickers)

    return {"signals": signals, "closes": closes}


# ── CLI ────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape SectorSurfer signals → signals.json"
    )
    parser.add_argument(
        "--out",
        default="signals.json",
        help="Output path (default: signals.json)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save page screenshot + HTML to debug/ for selector troubleshooting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print signals to stdout without writing the file",
    )
    args = parser.parse_args()

    _log("=" * 50)
    _log("SectorSurfer signal scraper")
    _log("=" * 50)

    raw = _scrape(debug=args.debug)

    _log(f"Scrape complete: {len(raw)} raw rows.")
    _log("Building signals...")
    print(f"\nParsed {len(raw)} portal rows:", file=sys.stderr)

    output = build_signals(raw)

    json_out = json.dumps(output, indent=2)

    if args.dry_run:
        print(json_out)
        return

    out_path = Path(args.out)
    out_path.write_text(json_out, encoding="utf-8")
    _log(f"Output written: {len(output['signals'])} signals → {out_path}")

    trades = [k for k, v in output["signals"].items() if v["current"] != v["new"]]
    print(f"\n✓  {len(output['signals'])} signals → {out_path}", file=sys.stderr)
    if trades:
        print(f"Active trades this month: {', '.join(trades)}", file=sys.stderr)
        _log(f"Active trades: {', '.join(trades)}")
    else:
        print("All strategies: HOLD this month.", file=sys.stderr)
        _log("All strategies: HOLD.")


if __name__ == "__main__":
    main()
