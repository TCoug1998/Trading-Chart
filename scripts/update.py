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


REPORT: list[str] = []
LAST = {"push": "", "email": ""}       # last log line per channel, for the health report
HISTORY: list[dict] = []               # alerts sent during this run
HEALTH: dict = {}                      # latest status per channel
NOW_UTC = datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(msg, flush=True)
    if msg.startswith("Push"):
        LAST["push"] = msg
    elif msg.startswith("E-mail"):
        LAST["email"] = msg
    if msg.startswith(("Push", "E-mail", "Test alert", "Alerts", "Pre-alert")):
        REPORT.append(msg)


def write_summary(extra: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["### Alertes", ""] + [f"- {m}" for m in REPORT] + ["", extra]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


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
    pwd = os.environ.get("SMTP_PASSWORD", "").strip()
    to = os.environ.get("ALERT_EMAIL_TO", "").strip()
    missing = [n for n, v in (("SMTP_HOST", host), ("SMTP_USER", user), ("SMTP_PASSWORD", pwd), ("ALERT_EMAIL_TO", to)) if not v]
    if missing:
        log(f"E-mail: not configured, missing secret(s) {', '.join(missing)}")
        return False
    if "gmail" in host.lower():
        pwd = pwd.replace(" ", "")  # Gmail shows app passwords in groups of four
    try:
        port = int(re.sub(r"\D", "", os.environ.get("SMTP_PORT", "")) or 465)
    except ValueError:
        port = 465
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    ctx = ssl.create_default_context()
    # Try the configured port first, then the other standard encrypted mode.
    attempts = [port] + [p for p in (465, 587) if p != port]
    for p in attempts:
        mode = "SSL" if p == 465 else "STARTTLS"
        stage = "connect"
        try:
            if mode == "SSL":
                s = smtplib.SMTP_SSL(host, p, context=ctx, timeout=30)
            else:
                s = smtplib.SMTP(host, p, timeout=30)
            with s:
                if mode == "STARTTLS":
                    stage = "starttls"
                    s.ehlo()
                    s.starttls(context=ctx)  # never send credentials in plaintext
                    s.ehlo()
                stage = "login"
                s.login(user, pwd)
                stage = "send"
                s.send_message(msg)
            log(f"E-mail: sent (port {p}, {mode})")
            return True
        except smtplib.SMTPAuthenticationError:
            log(f"E-mail: login refused on port {p} ({mode}): check SMTP_USER and the app password in SMTP_PASSWORD")
            return False  # another port will not fix wrong credentials
        except Exception as exc:
            log(f"E-mail: failed on port {p} ({mode}) at step '{stage}' ({type(exc).__name__})")
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
        miss = [n for n, v in (("VAPID_PRIVATE_KEY", key), ("WEBPUSH_SUBSCRIPTION", raw)) if not v]
        log(f"Push: not configured, missing secret(s) {', '.join(miss)}")
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


def _status(ok: bool, channel: str) -> str:
    if ok:
        return "ok"
    return "off" if "not configured" in LAST[channel] else "fail"


def notify(title: str, body: str, url: str, sym: str = "", kind: str = "") -> bool:
    """Send by push then e-mail. A failing channel is reported through the other one."""
    LAST["push"] = LAST["email"] = ""
    pushed, expired = send_push(title, body, url)
    push_st = _status(pushed, "push")
    extra = ""
    if expired:
        extra = ("\n\nLes notifications push de ton téléphone ont expiré. Ouvre l'app, "
                 "onglet Alertes, réactive-les et remplace le secret WEBPUSH_SUBSCRIPTION.")
    elif push_st == "fail":
        extra = "\n\nAttention : la notification push n'a pas pu être envoyée. Détail dans l'onglet Alertes de l'app."
    mailed = send_email(title, body + f"\n\n{url}" + extra)
    mail_st = _status(mailed, "email")
    if mail_st == "fail" and pushed:
        send_push("Alertes : e-mail en échec", "L'e-mail d'alerte n'a pas pu être envoyé. Vérifie l'onglet Alertes de l'app.", url)
    stamp = NOW_UTC.isoformat(timespec="seconds")
    for ch, st in (("push", push_st), ("email", mail_st)):
        if st != "off":
            HEALTH[ch] = {"status": st, "t": stamp, "detail": LAST[ch][:160] if st == "fail" else ""}
        else:
            HEALTH[ch] = {"status": "off", "t": stamp, "detail": ""}
    HISTORY.append({"t": stamp, "sym": sym, "kind": kind, "title": title, "push": push_st, "email": mail_st})
    return pushed or mailed


def describe(s: dict) -> str:
    dp = s["dp"]
    dist = s["distPct"]
    return (f"Dernière clôture ({fmt_date(s['date'])}) {fmt_num(s['last'], dp)}, "
            f"200 SMA {fmt_num(s['sma'], dp)}, écart {dist:+.2f} %.")


def alert_ids(cfg: dict) -> list[str]:
    """Symbols that send 200 SMA alerts: "alert_symbols" list (older configs: "alert_symbol")."""
    ids = cfg.get("alert_symbols")
    if not isinstance(ids, list):
        ids = [cfg.get("alert_symbol", "SPX")]
    known = {x["id"] for sec in cfg["sections"] for x in sec["symbols"]}
    out = []
    for i in ids:
        if i in known and i not in out:
            out.append(i)
        elif i not in known:
            log(f"Alerts: unknown symbol '{i}' in alert_symbols, ignored")
    return out


def process_alerts(summary: dict, data: dict, cfg: dict, state: dict, now_utc: datetime, url: str) -> None:
    ids = alert_ids(cfg)
    if not ids:
        log("Alerts: no symbol selected in alert_symbols")
        return
    if os.environ.get("TEST_ALERT", "").lower() == "true":
        lines = []
        for sid in ids:
            s = summary.get(sid)
            if s and s.get("sma") is not None:
                trend = "au-dessus" if s["above"] else "en dessous"
                lines.append(f"{sid} est {trend} de sa 200 SMA. {describe(s)}")
            else:
                lines.append(f"{sid} : données indisponibles.")
        ok = notify("Test d'alerte", "Les alertes fonctionnent pour : " + ", ".join(ids) + ".\n" + "\n".join(lines), url,
                    ",".join(ids), "TEST")
        log(f"Test alert: {'delivered' if ok else 'not delivered'}")
    for sid in ids:
        process_symbol(sid, summary, data, cfg, state, now_utc, url)


def process_symbol(sid: str, summary: dict, data: dict, cfg: dict, state: dict, now_utc: datetime, url: str) -> None:
    s = summary.get(sid)
    d = data.get(sid)
    if not s or not d or s.get("stale") or s.get("sma") is None:
        log(f"Alerts: {sid} data unavailable this run, no alert evaluated")
        st = state.setdefault(sid, {})
        today = NOW_UTC.date().isoformat()
        if st.get("data_alert") != today:      # at most one warning per day
            notify(f"{sid} : données indisponibles",
                   f"Les cours de {sid} n'ont pas pu être mis à jour. Les alertes 200 SMA de {sid} sont suspendues "
                   "jusqu'au retour des données.", url, sid, "DATA")
            st["data_alert"] = today
        return
    sym_cfg = next(x for sec in cfg["sections"] for x in sec["symbols"] if x["id"] == sid)
    rows = d["rows"]
    st = state.setdefault(sid, {})

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
                notify(f"{sid} : signal {kind}", body, url, sid, "BUY" if last_evt[1] > 0 else "SELL")
            else:
                log(f"Alerts: {sid} older signal recorded without alert")
            st["last_event"] = key

    # Proximity: confirmed close within X % of the 200 SMA (one alert per approach)
    pct_zone = float(cfg.get("proximity_pct", 2))
    last_c = confirmed[-1] if confirmed else None
    if pct_zone > 0 and last_c and last_c[5]:
        dist = (last_c[4] / last_c[5] - 1) * 100
        if abs(dist) <= pct_zone:
            if not st.get("near"):
                signal_today = last_evt is not None and last_evt[0] == last_c[0]
                if not signal_today:   # a crossing signal already covers this close
                    side, nxt = ("au-dessus", "VENTE si la baisse continue") if dist >= 0 else ("en dessous", "ACHAT si la hausse continue")
                    body = (f"{sid} clôture à {abs(dist):.2f} % {side} de sa 200 SMA le {fmt_date(last_c[0])} "
                            f"({fmt_num(last_c[4], s['dp'])} contre {fmt_num(last_c[5], s['dp'])}). "
                            f"Un signal {nxt} devient possible dans les prochaines séances.")
                    notify(f"{sid} : proche de la 200 SMA", body, url, sid, "NEAR")
                st["near"] = True
        elif abs(dist) > pct_zone * 1.25:     # leave the zone clearly before re-arming
            st["near"] = False

    # Pre-alert during the last part of the session
    if d["provisional"] and rows[-1][6]:
        _, minutes = session_status(date.fromisoformat(rows[-1][0]), sym_cfg["session"], now_utc)
        if minutes is not None and 0 < minutes <= cfg.get("pre_alert_minutes", 75) and st.get("pre_alert") != rows[-1][0]:
            kind = "ACHAT" if rows[-1][6] > 0 else "VENTE"
            body = (f"Signal {kind} probable à la clôture de ce soir (dans {int(minutes)} min) si le cours tient. "
                    f"{describe(s)} Non confirmé tant que la séance n'est pas close.")
            notify(f"{sid} : pré-alerte {kind}", body, url, sid, "PRE")
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
        "alertSymbol": (alert_ids(cfg) or ["SPX"])[0],
        "alertSymbols": alert_ids(cfg),
        "repo": os.environ.get("GITHUB_REPOSITORY", ""),
        "proximityPct": cfg.get("proximity_pct", 2),
        "health": {**state.get("_health", {}), **HEALTH},
        "history": list(reversed((state.get("_history", []) + HISTORY)[-20:])),
        "missing": missing,
        "sections": [{"title": s["title"], "ids": [x["id"] for x in s["symbols"] if x["id"] in summary]} for s in cfg["sections"]],
        "symbols": summary,
    }
    (SITE / "data" / "summary.json").write_text(json.dumps(meta, separators=(",", ":")), encoding="utf-8")
    state["_history"] = (state.get("_history", []) + HISTORY)[-50:]
    state.setdefault("_health", {}).update(HEALTH)
    state["_heartbeat"] = now_utc.strftime("%Y-%m")  # monthly commit keeps the schedule active
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log(f"Done: {len(summary)} symbols, {len(missing)} unavailable")
    if not REPORT:
        REPORT.append("No alert to send this run")
    write_summary(f"Symboles : {len(summary)} à jour" + (f", indisponibles : {', '.join(missing)}" if missing else ""))
    return 0 if summary else 1


if __name__ == "__main__":
    sys.exit(main())
