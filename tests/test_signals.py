"""Check the signal engine against a literal, bar-by-bar port of the Pine script."""
import math, random, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from update import sma, signals

def pine_reference(close):
    n = len(close); na = None
    dma = [na if i < 199 else sum(close[i-199:i+1]) / 200 for i in range(n)]
    buy_uniq = sell_uniq = 0; out = []
    for i in range(n):
        def ge(a, b): return a is not None and b is not None and a >= b
        def lt(a, b): return a is not None and b is not None and a < b
        c1 = close[i-1] if i else None; d1 = dma[i-1] if i else None
        BUY = ge(close[i], dma[i]) and ge(c1, d1) and ge(close[i], c1)
        SELL = lt(close[i], dma[i]) and lt(c1, d1) and lt(close[i], c1)
        if BUY: buy_uniq += 1; sell_uniq = 0
        if SELL: sell_uniq += 1; buy_uniq = 0
        out.append(1 if (BUY and buy_uniq == 1) else -1 if (SELL and sell_uniq == 1) else 0)
    return dma, out

for seed in range(30):
    r = random.Random(seed); p = 100.0; close = []
    for i in range(3000):
        p *= 1 + r.gauss(0.0003, 0.012) + 0.002 * math.sin(i / 60); close.append(p)
    dma_ref, ev_ref = pine_reference(close)
    dma = sma(close); ev = signals(close, dma)
    assert all((a is None and b is None) or abs(a - b) < 1e-9 * a for a, b in zip(dma, dma_ref)), seed
    assert ev == ev_ref, seed
print("signal engine matches Pine reference on 30 x 3000 bars;",
      "events in last run:", sum(1 for e in ev if e), )
