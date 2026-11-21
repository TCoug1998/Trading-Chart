"""Daily data update, 200-day SMA signals and alerts.

Replicates the TradingView script "200-Day Simple Moving Average (Signals)":
  BUY  = close >= sma and close[1] >= sma[1] and close >= close[1]
  SELL = close <  sma and close[1] <  sma[1] and close <  close[1]
  A signal fires on the first BUY after a SELL (or the first ever), and vice versa.

Secrets are read from environment variables only and are never printed.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import shutil
import smtplib
import ssl
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "symbols.json"
STATE = ROOT / "state" / "state.json"
WEB = ROOT / "web"
SITE = ROOT / "_site"

SMA_LEN = 200
SESSIONS = {
    # timezone, official close, time after which the daily bar is treated as final
    "us": ("America/New_York", dtime(16, 0), dtime(16, 10)),
    "eu": ("Europe/Paris", dtime(17, 35), dtime(17, 50)),
    "24h": ("UTC", dtime(0, 0), dtime(0, 0)),
    # MSCI publishes its daily close after the US close; final value from about 18:00 New York
    "msci": ("America/New_York", dtime(16, 0), dtime(18, 0)),
}


def log(msg: str) -> None:
    print(msg, flush=True)


def safe_name(symbol_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", symbol_id)


def site_url() -> str:
    url = os.environ.get("SITE_URL", "").strip()
    if url:
        return url if url.endswith("/") else url + "/"
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        if name.lower() == f"{owner.lower()}.github.io":
            return f"https://{owner.lower()}.github.io/"
        return f"https://{owner.lower()}.github.io/{name}/"
    return ""


# ---------------------------------------------------------------------------
# Signal engine (pure functions, unit-tested in tests/test_signals.py)
# ---------------------------------------------------------------------------

def sma(values: list[float], length: int = SMA_LEN) -> list[float | None]:
    """Simple moving average, None until `length` bars exist (same as ta.sma)."""
    out: list[float | None] = [None] * len(values)
    window = 0.0
    for i in range(length - 1, len(values)):
        if (i - length + 1) % 500 == 0:  # exact resum regularly, no floating drift
            window = sum(values[i - length + 1 : i + 1])
        else:
            window += values[i] - values[i - length]
        out[i] = window / length
    return out


def signals(closes: list[float], dma: list[float | None]) -> list[int]:
    """Return +1 on an Uptrend() bar, -1 on a Downtrend() bar, 0 otherwise."""
    events = [0] * len(closes)
    buy_uniq = 0
    sell_uniq = 0
    for i in range(1, len(closes)):
        c, d = closes[i], dma[i]
        pc, pd = closes[i - 1], dma[i - 1]
        if d is None or pd is None:
            continue
        buy = c >= d and pc >= pd and c >= pc
        sell = c < d and pc < pd and c < pc
        if buy:
            buy_uniq += 1
            sell_uniq = 0
        if sell:
            sell_uniq += 1
            buy_uniq = 0
        if buy and buy_uniq == 1:
            events[i] = 1
        elif sell and sell_uniq == 1:
            events[i] = -1
    return events


def session_status(last_bar: date, session: str, now_utc: datetime) -> tuple[bool, float | None]:
    """Return (provisional, minutes_to_close) for the last daily bar."""
    tz_name, close_t, final_t = SESSIONS[session]
    local = now_utc.astimezone(ZoneInfo(tz_name))
    if session == "24h":
        return last_bar >= local.date(), None
    if last_bar < local.date():
        return False, None
    provisional = local.time() < final_t
    close_dt = datetime.combine(local.date(), close_t, ZoneInfo(tz_name))
    minutes = (close_dt - local).total_seconds() / 60
    return provisional, minutes


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_yahoo(ticker: str, years: int):
    import yfinance as yf

    last_err = None
    for attempt in range(3):
        try:
            df = yf.Ticker(ticker).history(period="max", interval="1d", auto_adjust=False, actions=False)
            df = df.dropna(subset=["Close"])
            if len(df) == 0:
                raise ValueError("empty history")
            rows = []
            for idx, r in df.iterrows():
                o, h, l, c = (float(r[k]) for k in ("Open", "High", "Low", "Close"))
                if not all(math.isfinite(x) for x in (o, h, l, c)) or c <= 0:
                    continue
                if o <= 0:  # some indices report 0 open on old bars
                    o = h = l = c
                rows.append((idx.date(), o, h, l, c))
            # Deduplicate dates (keep last) and sort.
            dedup = {}
            for row in rows:
                dedup[row[0]] = row
            return sorted(dedup.values(), key=lambda x: x[0])
        except Exception as exc:  # network or parsing error, retry
            last_err = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{ticker}: {type(last_err).__name__}")


def fetch_previous(url: str, fname: str):
    """Fallback: reuse the file currently published on the site."""
    if not url:
        return None
    import requests

    try:
        resp = requests.get(f"{url}data/sym/{fname}.json", timeout=20)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def build_symbol(sym: dict, rows, years: int, now_utc: datetime) -> dict:
    dp = sym.get("dp", 2)
    closes = [r[4] for r in rows]
    dma = sma(closes)
    ev = signals(closes, dma)
    provisional, _ = session_status(rows[-1][0], sym["session"], now_utc)
    cutoff = rows[-1][0] - timedelta(days=365 * years + 5)
    out_rows = []
    for i, r in enumerate(rows):
        if r[0] < cutoff:
            continue
        out_rows.append([
            r[0].isoformat(),
            round(r[1], dp), round(r[2], dp), round(r[3], dp), round(r[4], dp),
            None if dma[i] is None else round(dma[i], dp + 2),
            ev[i],
        ])
    return {
        "id": sym["id"],
        "name": sym["name"],
        "dp": dp,
        "provisional": provisional,
        "rows": out_rows,
    }


def summarize(data: dict, section: str, stale: bool) -> dict:
    rows = data["rows"]
    last, prev = rows[-1], rows[-2] if len(rows) > 1 else rows[-1]
    c, p, s = last[4], prev[4], last[5]
    ps = prev[5]
    evt = None
    for r in reversed(rows):
        if r[6]:
            evt = {"date": r[0], "type": "BUY" if r[6] > 0 else "SELL"}
            break
    if evt and evt["date"] == last[0]:
        evt["provisional"] = data["provisional"]
    return {
        "id": data["id"], "name": data["name"], "section": section, "dp": data["dp"],
        "date": last[0], "last": c, "prev": p,
        "chg": c - p, "chgPct": (c / p - 1) * 100 if p else 0,
        "sma": s, "smaUp": (s is not None and ps is not None and s >= ps),
        "distPct": (c / s - 1) * 100 if s else None,
        "above": (s is not None and c >= s),
        "lastEvent": evt, "provisional": data["provisional"], "stale": stale,
        "file": safe_name(data["id"]),
    }


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def fmt_num(x: float, dp: int = 2) -> str:
    return f"{x:,.{dp}f}".replace(",", " ")


def fmt_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def send_email(subject: str, body: str) -> bool:
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    pwd = os.environ.get("SMTP_PASSWORD", "")
    to = os.environ.get("ALERT_EMAIL_TO", "").strip()
    if not (host and user and pwd and to):
        log("E-mail: not configured, skipped")
        return False
    port = int(os.environ.get("SMTP_PORT", "465") or 465)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(user, pwd)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ctx)  # refuse plaintext
                s.login(user, pwd)
                s.send_message(msg)
        log("E-mail: sent")
        return True
    except Exception as exc:
        log(f"E-mail: failed ({type(exc).__name__})")
        return False


def vapid_public_key() -> str | None:
    key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not key:
        return None
    try:
        from py_vapid import Vapid
        from cryptography.hazmat.primitives import serialization

        v = Vapid.from_string(key)
        raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    except Exception as exc:
        log(f"Push: invalid VAPID_PRIVATE_KEY ({type(exc).__name__})")
        return None


def send_push(title: str, body: str, url: str) -> tuple[bool, bool]:
    """Return (sent_to_at_least_one, a_subscription_expired)."""
    key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    raw = os.environ.get("WEBPUSH_SUBSCRIPTION", "").strip()
    if not (key and raw):
        log("Push: not configured, skipped")
        return False, False
    try:
        subs = json.loads(raw)
    except json.JSONDecodeError:
        log("Push: WEBPUSH_SUBSCRIPTION is not valid JSON")
        return False, False
    if isinstance(subs, dict):
        subs = [subs]
    from pywebpush import WebPushException, webpush

    sent, expired = False, False
    # VAPID contact: the site origin (no personal e-mail sent to push services).
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    claims_sub = f"https://{parts.netloc}" if parts.scheme == "https" and parts.netloc else "https://github.com"
    for i, sub in enumerate(subs, 1):
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=key,
                vapid_claims={"sub": claims_sub},
                ttl=86400,
            )
            sent = True
            log(f"Push: sent to device {i}")
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                expired = True
                log(f"Push: device {i} subscription expired")
            else:
                log(f"Push: device {i} failed (HTTP {status})")
        except Exception as exc:
            log(f"Push: device {i} failed ({type(exc).__name__})")
    return sent, expired


def notify(title: str, body: str, url: str) -> bool:
    pushed, expired = send_push(title, body, url)
    extra = ""
    if expired:
        extra = ("\n\nLes notifications push de ton téléphone ont expiré. Ouvre l'app, "
                 "onglet Alertes, réactive-les et remplace le secret WEBPUSH_SUBSCRIPTION.")
    mailed = send_email(title, body + f"\n\n{url}" + extra)
    return pushed or mailed


def describe(s: dict) -> str:
    dp = s["dp"]
    dist = s["distPct"]
    return (f"Dernière clôture ({fmt_date(s['date'])}) {fmt_num(s['last'], dp)}, "
            f"200 SMA {fmt_num(s['sma'], dp)}, écart {dist:+.2f} %.")


def process_alerts(summary: dict, data: dict, cfg: dict, state: dict, now_utc: datetime, url: str) -> None:
    sid = cfg["alert_symbol"]
    s = summary.get(sid)
    d = data.get(sid)
    if not s or not d or s.get("stale"):
        log(f"Alerts: {sid} data unavailable this run, no alert evaluated")
        return
    sym_cfg = next(x for sec in cfg["sections"] for x in sec["symbols"] if x["id"] == sid)
    rows = d["rows"]
    st = state.setdefault(sid, {})

    # Test mode
    if os.environ.get("TEST_ALERT", "").lower() == "true":
        trend = "au-dessus" if s["above"] else "en dessous"
        ok = notify(f"{sid} : test d'alerte",
                    f"Les alertes fonctionnent. {sid} est {trend} de sa 200 SMA. {describe(s)}", url)
        log(f"Test alert: {'delivered' if ok else 'not delivered'}")

    confirmed = rows[:-1] if d["provisional"] else rows
    last_evt = next(((r[0], r[6]) for r in reversed(confirmed) if r[6]), None)
    if last_evt:
        key = f"{last_evt[0]}:{'BUY' if last_evt[1] > 0 else 'SELL'}"
        recent_dates = [r[0] for r in confirmed[-10:]]  # catch up after missed runs
        if st.get("last_event") != key:
            first_run = "last_event" not in st
            if (not first_run and last_evt[0] in recent_dates) or (first_run and last_evt[0] == confirmed[-1][0]):
                kind = "ACHAT" if last_evt[1] > 0 else "VENTE"
                sentence = ("repasse au-dessus de sa 200 SMA" if last_evt[1] > 0 else "casse sa 200 SMA")
                body = (f"Signal {kind} confirmé à la clôture du {fmt_date(last_evt[0])} : {sid} {sentence}. "
                        f"{describe(s)}")
                notify(f"{sid} : signal {kind}", body, url)
            else:
                log("Alerts: older signal recorded without alert")
            st["last_event"] = key

    # Pre-alert during the last part of the session
    if d["provisional"] and rows[-1][6]:
        _, minutes = session_status(date.fromisoformat(rows[-1][0]), sym_cfg["session"], now_utc)
        if minutes is not None and 0 < minutes <= cfg.get("pre_alert_minutes", 75) and st.get("pre_alert") != rows[-1][0]:
            kind = "ACHAT" if rows[-1][6] > 0 else "VENTE"
            body = (f"Signal {kind} probable à la clôture de ce soir (dans {int(minutes)} min) si le cours tient. "
                    f"{describe(s)} Non confirmé tant que la séance n'est pas close.")
            notify(f"{sid} : pré-alerte {kind}", body, url)
            st["pre_alert"] = rows[-1][0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    now_utc = datetime.now(timezone.utc)
    url = site_url()
    years = int(cfg.get("history_years", 20))
    offline = os.environ.get("OFFLINE_SAMPLE")  # local testing only

    if SITE.exists():
        shutil.rmtree(SITE)
    shutil.copytree(WEB, SITE)
    (SITE / "data" / "sym").mkdir(parents=True, exist_ok=True)

    summary, data, missing = {}, {}, []
    for sec in cfg["sections"]:
        for sym in sec["symbols"]:
            fname = safe_name(sym["id"])
            stale = False
            try:
                if offline:
                    from sample_data import sample_rows  # type: ignore
                    rows = sample_rows(sym)
                else:
                    rows = fetch_yahoo(sym["yahoo"], years)
                    time.sleep(0.5)
                if len(rows) < 2:
                    raise RuntimeError("not enough data")
                built = build_symbol(sym, rows, years, now_utc)
                log(f"{sym['id']}: {len(rows)} bars, last {rows[-1][0].isoformat()}")
            except Exception as exc:
                built = fetch_previous(url, fname)
                if built:
                    stale = True
                    log(f"{sym['id']}: fetch failed ({exc}), previous data kept")
                else:
                    missing.append(sym["id"])
                    log(f"{sym['id']}: unavailable ({exc})")
                    continue
            (SITE / "data" / "sym" / f"{fname}.json").write_text(json.dumps(built, separators=(",", ":")), encoding="utf-8")
            data[sym["id"]] = built
            summary[sym["id"]] = summarize(built, sec["title"], stale)

    process_alerts(summary, data, cfg, state, now_utc, url)

    meta = {
        "generated": now_utc.isoformat(timespec="seconds"),
        "pushKey": vapid_public_key(),
        "alertSymbol": cfg["alert_symbol"],
        "missing": missing,
        "sections": [{"title": s["title"], "ids": [x["id"] for x in s["symbols"] if x["id"] in summary]} for s in cfg["sections"]],
        "symbols": summary,
    }
    (SITE / "data" / "summary.json").write_text(json.dumps(meta, separators=(",", ":")), encoding="utf-8")
    state["_heartbeat"] = now_utc.strftime("%Y-%m")  # monthly commit keeps the schedule active
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log(f"Done: {len(summary)} symbols, {len(missing)} unavailable")
    return 0 if summary else 1


if __name__ == "__main__":
    sys.exit(main())
