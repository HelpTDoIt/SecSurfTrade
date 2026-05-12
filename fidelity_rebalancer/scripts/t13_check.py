import json
with open("today_premarket.json") as f:
    state = json.load(f)
print("=== T-1.3: Spread Context ===")
for s in state["computed"]["sell_strategies"] + state["computed"]["buy_strategies"]:
    tkr = s["ticker"]
    rule = s["rule"]
    reasoning = s.get("reasoning", [])
    spread_bullets = [r for r in reasoning if "spread" in r.lower() or "tight" in r.lower() or "wide" in r.lower()]
    side = s.get("side","?")
    bullets = str(spread_bullets[:2])
    print(tkr + " " + side + "  rule=" + rule + "  spread_bullets=" + bullets)