# Test Plan: Fidelity Trader+ Integration & Data Capture Optimization

**Date prepared:** 2026-05-07
**Execute:** Now (FT+ is open) — no trading signals required
**Goal:** Validate OCR accuracy, optimize FT+ layout, calibrate L2 reading

---

## Current FT+ Layout (from screenshot)

| Panel | Location | Status |
|-------|----------|--------|
| Order Entry | Top-left | Working (EEM buy visible) |
| Watchlist | Top-center | 6 tickers visible: FRDM, EEM, EIS, IYZ, XLE, DFEN |
| Orders | Bottom-left | 2 open EVGF sells visible |
| L2 Windows (×5) | Right side | XLE, IYZ, EIS, EEM, DFEN — first 3 show zeroes, DFEN has live depth |
| Quote (mini) | Below order entry | EEM quote visible |

**Observations from screenshot:**
- L2 panels for XLE, IYZ, EIS show `B 0.00 × 0 / A 0.00 × 0` — likely because market is closed or symbols need to be re-entered
- DFEN L2 has full depth (20+ levels visible with color coding) — confirms OCR can work when data is populated
- Watchlist has all recommended columns: Symbol, Last, Bid, Ask, % Chg, Close, Open, VWAP, Volume, 10D Avg Vol, Bid Size, Ask Size, Ext Hrs Last, Ext Hrs % Chg, 90D Avg Vol, Div Ex-Date, Div Local

---

## Recommended Watchlist Tickers

Populate the watchlist with tickers that span all asset classes and cover the typical SectorSurfer universe. This ensures OCR calibration works across different price ranges and spread widths.

### Tier 1: Always in watchlist (current holdings + active signals)
Add whatever tickers are in your current `signals.json`. Based on the Feb-27 fixture and your screenshot:

| Ticker | Class | Why |
|--------|-------|-----|
| FRDM | International | Active in World Try strategy |
| EEM | Large-cap | Active — visible in your screenshot |
| EIS | International | Active in World Try |
| IYZ | Sector | Active — low volume, good thin-ticker test |
| XLE | Sector | Active in SPDR Respectable |
| DFEN | Leveraged | Active in Leverage strategy — thin, wide spread |

### Tier 2: Calibration tickers (add these for OCR + spread testing)

| Ticker | Class | Typical Spread | ADV | Why Include |
|--------|-------|---------------|-----|-------------|
| SPY | Large-cap | ~1 bps | 80M+ | Benchmark — tightest spread, highest volume. OCR baseline. |
| QQQ | Large-cap | ~1 bps | 50M+ | Second benchmark. Validates tight_spread rules. |
| TQQQ | Leveraged | ~5 bps | 30M+ | Leveraged but liquid. Tests SpreadContext calibration. |
| PILL | Leveraged | ~25 bps | 50K | Very thin leveraged ETF. Key thin-ticker test case. |
| SMH | Sector | ~3 bps | 15M | Mid-liquidity sector. Common in SPDR Respectable. |
| EPOL | International | ~15 bps | 200K | Thin international. Tests wide_spread + thin detection. |
| IEF | Fixed income | ~2 bps | 10M | Bond ETF. Different price range (~$90). |
| BULZ | Leveraged | ~20 bps | 300K | Thin leveraged. Good L2 OCR test target. |

### Recommended watchlist order (top to bottom)
```
SPY, QQQ, FRDM, EEM, EIS, IYZ, XLE, DFEN, TQQQ, PILL, SMH, EPOL, IEF, BULZ
```
**14 tickers** — fits in one screen without scrolling (critical for OCR capture).

---

## Recommended L2 Window Tickers

You have space for 5 L2 panels (visible in screenshot). Prioritize the **thinnest tickers** where L2 depth matters most for chunk sizing:

| Slot | Ticker | Reason |
|------|--------|--------|
| 1 | **DFEN** | Leveraged, ~100K ADV, wide spread. Already populated and working. |
| 2 | **PILL** | Thinnest in the universe (~50K ADV). L2 depth critical for sizing. |
| 3 | **EPOL** | Thin international (~200K ADV). |
| 4 | **BULZ** | Thin leveraged (~300K ADV). |
| 5 | **IYZ** | Sector, ~2M ADV but can have thin books. Backup slot. |

> **During live trading:** Replace slots 3–5 with whatever tickers the thin-ticker detection flags from your actual signals. Run `python -m cli.strategy --state today.json --export /dev/null --source yfinance` pre-market and check the "Thin-ticker detection" output.

---

## Test Procedures

### A-1: Watchlist OCR Accuracy

**Goal:** Validate the watchlist OCR reads all 14+ columns correctly.

**Steps:**
1. Populate watchlist with the 14 recommended tickers
2. Ensure all column headers are visible (scroll right if needed — all headers must be on screen)
3. Run:
```bash
python scripts/atp_smoke.py --watchlist
```
4. Compare OCR output against FT+ screen values for each ticker:

| Field | How to verify |
|-------|--------------|
| Symbol | Must match exactly |
| Last | Within $0.01 of screen |
| Bid / Ask | Within $0.01 |
| Bid Size / Ask Size | Exact match |
| Volume | Within 1% (volume changes rapidly) |
| Close (prev_close) | Exact match |
| 10D Avg Vol / 90D Avg Vol | Within 5% |
| VWAP | Within $0.01 (only during market hours) |
| % Chg | Within 0.01% |
| Open | Within $0.01 |
| Ext Hrs Last | Within $0.01 (only pre/post market) |
| Div Ex-Date | Exact match or empty |

**PASS criteria:**
- [ ] All 14 tickers parsed (no missing rows)
- [ ] Symbol column 100% accurate
- [ ] Price fields (Last, Bid, Ask, Close, VWAP, Open) within $0.01 for all tickers
- [ ] Volume fields within 5%
- [ ] New columns (% Chg, Open, Ext Hrs Last, Ext Hrs % Chg) populated during appropriate hours

**Common failures:**
- OCR misreads `1` as `l` or `I` in price fields → check `_atp_parse.py` normalization
- Column headers merge (e.g., "10D Avg" + "Vol" → "10D AvgVol") → `_calibrate_cols` handles this but verify
- Ticker rows cut off at bottom of watchlist → ensure all rows visible without scrolling

### A-2: L2 OCR Accuracy

**Goal:** Validate L2 depth reading for a populated L2 panel.

**Steps:**
1. Ensure DFEN L2 panel has live data (should already be working per screenshot)
2. Run:
```bash
python scripts/atp_smoke.py DFEN
```
3. Compare Level II output against the DFEN L2 panel on screen

**PASS criteria:**
- [ ] At least 5 bid levels and 5 ask levels parsed
- [ ] Best bid/ask prices match FT+ display within $0.01
- [ ] Size values match (ARCX 200, XNMS 100, etc.)
- [ ] MPID (exchange codes) readable: ARCX, XNMS, EDGX, BATS, IEXG, etc.

**Then test each L2 slot:**
```bash
python scripts/atp_smoke.py PILL
python scripts/atp_smoke.py EPOL
python scripts/atp_smoke.py BULZ
python scripts/atp_smoke.py IYZ
```

**Known limitations:**
- L2 panels showing `0.00 × 0` (visible for XLE/IYZ/EIS in screenshot) → either market closed or symbol needs refresh
- During market hours, all L2 panels should populate. If not, click in the L2 search box and re-enter the symbol.

### A-3: Orders OCR Accuracy

**Goal:** Validate order row parsing against the Orders panel.

**Steps:**
1. Your screenshot shows 2 EVGF sell orders. Run:
```bash
python scripts/atp_smoke.py EVGF
```
2. Check the Orders section output against the Orders panel:

| Field | Expected (from screenshot) |
|-------|---------------------------|
| Symbol | EVGF |
| Action | Sell |
| Amount | 11, 50 |
| Status | Open |
| Limit | $69.00, $69.50 |
| Account | Individual - TOD *9440 |
| TIF | GTC |

**PASS criteria:**
- [ ] Both orders parsed
- [ ] Symbol, Action, Amount match exactly
- [ ] Limit price parsed correctly from "Limit at $69.00" order type text
- [ ] Account string captured
- [ ] New fields (Last, Bid, Ask, Mid, TIF) populated

### A-4: Full Pipeline with ATP Source

**Goal:** Validate the complete `--source atp` flow.

**Steps:**
```bash
# Generate a test state (or use an existing one)
python -m cli.strategy --state today.json --export today_atp.json --source atp
```

**PASS criteria:**
- [ ] Watchlist OCR succeeds for all tickers in state
- [ ] Strategy generation completes without error
- [ ] Output strategies have non-zero prev_close, adv10 values from ATP data

### A-5: L2 Integration End-to-End

**Goal:** Validate `--l2-symbols` with live ATP data.

**Steps:**
```bash
# Fetch L2 for DFEN (the only L2 panel with data in your screenshot)
python -m cli.strategy --state today.json --export today_l2.json --source atp --l2-symbols DFEN
```

**PASS criteria:**
- [ ] Output shows `DFEN  L2 OK (N bids, M asks)` with N,M > 0
- [ ] DFEN strategy uses book_relative chunking (not POV rechunking)
- [ ] Chunk sizes reflect actual book depth

### A-6: Debug Image Capture (if OCR fails)

**Goal:** Capture debug images for OCR troubleshooting.

**Steps:**
```python
# Enable debug mode before running any adapter
from adapters.atp_ocr import enable_debug
enable_debug()

from adapters.atp_watchlist import OCRWatchlistAdapter
rows = OCRWatchlistAdapter().get_watchlist()
# Saves: debug_full_window.png, debug_ocr_*.png
# Prints: every OCR detection with x,y coordinates
```

**Check:**
- [ ] `debug_full_window.png` captures the full FT+ window (not black/blank)
- [ ] OCR detections align with visible text in the screenshot
- [ ] Column x-positions in debug output match the actual column layout

---

## FT+ Layout Optimization

### Screen Real Estate (from screenshot)

Your current layout has:
- **Left ~35%:** Order entry (top) + Orders panel (bottom)
- **Center ~30%:** Watchlist
- **Right ~35%:** 5 L2 panels stacked

**Recommendation:** This layout is good for trading. No changes needed. The key constraint is that the **watchlist must not require scrolling** — all tickers and column headers must be visible in one screen for OCR.

### If you need terminal/Chrome space

The screenshot shows terminal and Chrome React calculator mentioned as potential additions. Options:

1. **Second monitor:** Keep FT+ full-screen on primary, terminal + Chrome on secondary. OCR captures the FT+ window regardless of focus (PrintWindow with PW_RENDERFULLCONTENT=2).

2. **Same monitor, FT+ behind terminal:** OCR still works — `PrintWindow` captures the window buffer even when occluded. This is already implemented and tested.

3. **Minimize L2 panels:** If you don't need all 5 L2 slots, collapse 1–2 to give the watchlist more vertical space for additional tickers.

---

## Troubleshooting Quick Reference

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| "Watchlist column header row not found" | Headers scrolled off screen | Scroll watchlist to show headers |
| "No data rows parsed" | Tickers below fold | Reduce ticker count or increase panel height |
| L2 shows 0.00 × 0 | Market closed or symbol stale | Re-enter symbol in L2 search during market hours |
| OCR misreads prices | Low contrast or small font | Increase FT+ font size in settings |
| "PrintWindow capture 0×0px" | FT+ minimized | Restore FT+ window (can be behind other windows, but not minimized) |
| debug_full_window.png is black | GPU rendering issue | Try `enable_debug()` and check if window handle is correct |
