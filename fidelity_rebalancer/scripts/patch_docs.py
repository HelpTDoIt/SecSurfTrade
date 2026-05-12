import re

# ── test_plan_trading_window.md ─────────────────────────────────────────────
tp = r"C:\Users\Jason\Documents\Code\SecSurfTrade\fidelity_rebalancer\docs\test_plan_trading_window.md"
with open(tp, encoding="utf-8") as f:
    c = f.read()

orig = len(c)

# 1. Add last-validated line
c = c.replace(
    "**Date prepared:** 2026-05-07\n**Execute during:**",
    "**Date prepared:** 2026-05-07\n"
    "**Last pre-window validation:** 2026-05-12 — S-2, T-1.1, T-1.2 (pre-market), T-1.3, T-2.1, T-3.2 all pass\n"
    "**Execute during:**",
)

# 2. Fix sigma notation in T-1.1 example block
c = c.replace(
    "  SPY     σ=148 bps\n  DFEN    σ=512 bps (leveraged → higher)\n  EIS     σ=210 bps",
    "  SPY     sigma=148 bps\n  DFEN    sigma=512 bps (leveraged -> higher)\n  EIS     sigma=210 bps",
)

# 3. T-1.1 PASS: add daily-bps note and fix sigma notation
c = c.replace(
    "**PASS:**\n"
    "- [ ] At least 80% of tickers show non-default sigma (not \"100 bps (default)\")\n"
    "- [ ] Leveraged ETFs (DFEN, BULZ, TQQQ, etc.) show σ > 300 bps\n"
    "- [ ] Large-cap ETFs (SPY, QQQ, EEM) show σ between 80–250 bps\n"
    "\n"
    "### T-1.2:",
    "**PASS:** (sigma values are **daily** bps: 100 bps = 1% daily vol)\n"
    "- [ ] At least 80% of tickers show non-default sigma (not \"100 bps (default)\")\n"
    "- [ ] Leveraged ETFs (DFEN, BULZ, TQQQ, etc.) show sigma > 300 bps\n"
    "- [ ] Large-cap ETFs (SPY, QQQ, EEM) show sigma between 80-250 bps\n"
    "\n"
    "### T-1.2:",
)

# 4. S-2 PASS: fix thin-ticker bullet
c = c.replace(
    "- [ ] Thin-ticker detection section printed (may or may not flag tickers)\n",
    "- [ ] Thin-ticker detection block printed **only if** any ticker exceeds 3% ADV; no output if all are liquid (both outcomes are valid)\n",
)

# 5. E-2: "Screenshot of ATP" -> "Screenshot of FT+"
c = c.replace(
    "- [ ] Screenshot of ATP at each test point\n",
    "- [ ] Screenshot of FT+ at each test point\n",
)

# 6. T-1.2: fix multiplier symbol
c = c.replace(
    "# Pre-market (before 9:30): expect 1.0×\n",
    "# Pre-market (before 9:30): expect 1.0x\n",
)
c = c.replace(
    "# Opening (9:35): expect 1.8×\n",
    "# Opening (9:35): expect 1.8x\n",
)
c = c.replace(
    "# Lunch (12:00): expect 0.6×\n",
    "# Lunch (12:00): expect 0.6x\n",
)
c = c.replace(
    "# Close (15:45): expect 1.5×\n",
    "# Close (15:45): expect 1.5x\n",
)
c = c.replace(
    "- [ ] Pre-market shows `Volume profile multiplier: 1.0×`\n",
    "- [ ] Pre-market shows `Volume profile multiplier: 1.0x (outside market hours)`\n",
)
c = c.replace(
    "- [ ] At 9:35 shows `1.8×`\n",
    "- [ ] At 9:35 shows `1.8x`\n",
)
c = c.replace(
    "- [ ] At 12:00 shows `0.6×`\n",
    "- [ ] At 12:00 shows `0.6x`\n",
)

if len(c) == orig:
    print("WARNING: test plan — no changes made (check search strings)")
else:
    with open(tp, "w", encoding="utf-8", newline="\n") as f:
        f.write(c)
    print(f"test_plan patched: {orig} -> {len(c)} chars")

# ── USER_GUIDE.md ────────────────────────────────────────────────────────────
ug = r"C:\Users\Jason\Documents\Code\SecSurfTrade\fidelity_rebalancer\USER_GUIDE.md"
with open(ug, encoding="utf-8") as f:
    c = f.read()

orig = len(c)

# 1. Test count
c = c.replace("# Expected: 144 passed", "# Expected: 182 passed")

# 2. --l2-symbols: fix comma-separated -> space-separated, add auto-detect note
c = c.replace(
    "| `--l2-symbols` | none | Comma-separated tickers to fetch L2 depth for book-relative chunking (e.g. `DFEN,PILL`) |",
    "| `--l2-symbols [SYM ...]` | none | Space-separated tickers for L2 depth / book-relative chunking (e.g. `DFEN PILL`). Pass with no args (`--l2-symbols`) to auto-detect thin tickers (> 3% ADV). |",
)

# 3. Add sigma note to cli.strategy description
c = c.replace(
    "**Options:**\n"
    "\n"
    "| Flag | Default | Description |\n"
    "|---|---|---|\n"
    "| `--state`",
    "Terminal output includes a realized-volatility block (`sigma=N bps`, daily units: 100 bps = 1% daily vol) "
    "and a thin-ticker block if any order exceeds 3% of ADV.\n"
    "\n"
    "**Options:**\n"
    "\n"
    "| Flag | Default | Description |\n"
    "|---|---|---|\n"
    "| `--state`",
)

if len(c) == orig:
    print("WARNING: user guide — no changes made (check search strings)")
else:
    with open(ug, "w", encoding="utf-8", newline="\n") as f:
        f.write(c)
    print(f"user_guide patched: {orig} -> {len(c)} chars")