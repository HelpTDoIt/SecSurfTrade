import math, yfinance as yf
for sym in ["EEM", "FRDM", "SPY"]:
    hist = yf.Ticker(sym).history(period="60d")
    if hist.empty:
        print(sym + ": no data")
        continue
    closes = hist["Close"]
    rets = [math.log(closes.iloc[i] / closes.iloc[i-1]) for i in range(1, len(closes))]
    rms = (sum(r**2 for r in rets) / len(rets))**0.5
    sigma_bps = int(rms * 10000)
    print(sym + ": " + str(len(rets)) + " days  sigma=" + str(sigma_bps) + " bps  last=" + str(round(closes.iloc[-1],2)))