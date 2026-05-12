import json
with open("today_premarket.json") as f:
    state = json.load(f)
print("=== T-3.2: Chunk Ordering (per strategy/account) ===")
for side in ["sell_chunks", "buy_chunks"]:
    by_strat = {}
    for c in state["computed"][side]:
        key = c["ticker"] + "|" + c["account"]
        by_strat.setdefault(key, []).append(c)
    for key, cks in sorted(by_strat.items()):
        sizes = [c["shares"] for c in sorted(cks, key=lambda x: x["idx"])]
        ok = all(sizes[i] >= sizes[i+1] for i in range(len(sizes)-1))
        result = "PASS" if ok else "FAIL"
        print("  " + side + " " + key + "  sizes=" + str(sizes) + "  largest_first=" + result)