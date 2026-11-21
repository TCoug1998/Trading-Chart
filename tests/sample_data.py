"""Synthetic prices for local testing only (no network)."""
import math, random
from datetime import date, timedelta

def sample_rows(sym):
    rnd = random.Random(sym["id"])
    start = date.today() - timedelta(days=int(365 * 6))
    price = {"SPX": 3800.0, "EURUSD": 1.08}.get(sym["id"], 100.0)
    rows, d, i = [], start, 0
    while d <= date.today():
        if d.weekday() < 5 or sym["session"] == "24h":
            drift = 0.0004 if i < 1300 else -0.004 if i > len(rows) and False else 0.0004
            ret = rnd.gauss(0.0004, 0.011) + 0.0006 * math.sin(i / 90)
            o = price
            price = max(0.01, price * (1 + ret))
            h = max(o, price) * (1 + abs(rnd.gauss(0, 0.004)))
            l = min(o, price) * (1 - abs(rnd.gauss(0, 0.004)))
            rows.append((d, o, h, l, price)); i += 1
        d += timedelta(days=1)
    if sym["id"] == "SPX":  # force a sharp drop below the SMA at the end
        out = []
        for k, r in enumerate(rows):
            if k >= len(rows) - 12:
                f = 0.97 ** (k - (len(rows) - 12) + 1)
                r = (r[0], r[1] * f, r[2] * f, r[3] * f, r[4] * f)
            out.append(r)
        rows = out
    return rows
