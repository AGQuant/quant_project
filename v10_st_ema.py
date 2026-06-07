"""
V10 ST+EMA — NIFTY directional signal engine (Scorr platform module)
=====================================================================
ISOLATED. Does not touch fyers_feed, intraday_prices, V8, or paper engine.
Computes a long/short/flat signal for NIFTY on demand and (optionally) alerts.

LOCKED STRATEGY SPEC (backtested 1yr NIFTY, after Rs1000/trade futures cost):
  - Timeframe   : 10-minute candles (resampled from 5m)
  - Supertrend  : ATR period 150, multiplier 3.0   (trigger)
  - Gate        : EMA 3 vs EMA 10 on 30-minute candles (regime filter)
                  EMA3 > EMA10 -> BUY zone ; EMA3 < EMA10 -> SELL zone
  - Entry       : ST flip whose direction matches the gate zone
  - Exit        : SL 100 / Target 200 (close-based) OR opposite ST flip
  - Backtest    : +5936 pts (~Rs4.45L/lot/yr), 49.3% win, PF 1.88,
                  150 trades/yr, 10/13 months positive
  - Sizing note : intended for ~Rs5L capital per lot (max DD ~ -Rs85k)

Data:
  - LIVE signal      : pulls recent 5m from Fyers (yahoo_ondemand-style), resamples to 10m
  - BACKTEST/validate: reads nifty_5m_test_data (static 1yr history)

This module ONLY produces signals. Execution is manual/advisory — consistent
with Scorr's RA-compliant, no-fund-management product stance.
"""
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import psycopg2
import requests

IST = timezone(timedelta(hours=5, minutes=30))
CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "1A4STS8ZGD-100")

# ---- LOCKED PARAMETERS ----
TF_MAIN   = "10min"
TF_GATE   = "30min"
ST_PERIOD = 150
ST_MULT   = 3.0
EMA_FAST  = 3
EMA_SLOW  = 10
SL_PTS    = 100
TGT_PTS   = 200
SYMBOL    = "NSE:NIFTY50-INDEX"


# ---------- indicators ----------
def _atr(h, l, c, period):
    n = len(c); tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    tr[0] = h[0]-l[0]
    a = np.zeros(n); a[:period] = tr[:period].mean()
    for i in range(period, n):
        a[i] = (a[i-1]*(period-1)+tr[i])/period
    return a


def _supertrend(o, h, l, c, period, mult):
    n = len(c); a = _atr(h, l, c, period); hl2 = (h+l)/2
    up = hl2 + mult*a; lo = hl2 - mult*a
    fu = up.copy(); fl = lo.copy(); d = np.ones(n, int)
    for i in range(1, n):
        fu[i] = up[i] if (up[i] < fu[i-1] or c[i-1] > fu[i-1]) else fu[i-1]
        fl[i] = lo[i] if (lo[i] > fl[i-1] or c[i-1] < fl[i-1]) else fl[i-1]
        d[i] = 1 if c[i] > fu[i-1] else (-1 if c[i] < fl[i-1] else d[i-1])
    return d


def _ema(arr, span):
    out = np.empty(len(arr)); out[0] = arr[0]; k = 2/(span+1)
    for i in range(1, len(arr)):
        out[i] = arr[i]*k + out[i-1]*(1-k)
    return out


# ---------- data ----------
def _resample(df5, rule):
    return (df5.set_index("ts")
            .resample(rule)
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
            .dropna().reset_index())


def _fetch_live_5m(days=3):
    """Pull recent 5m from Fyers (token in fyers_tokens id=1). No DB writes."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
    cur.execute("SELECT access_token FROM fyers_tokens WHERE id=1")
    tok = cur.fetchone()[0]; cur.close(); conn.close()
    end = datetime.now(IST).date()
    start = end - timedelta(days=days)
    r = requests.get("https://api-t1.fyers.in/data/history",
                     params={"symbol": SYMBOL, "resolution": "5", "date_format": "1",
                             "range_from": start.isoformat(), "range_to": end.isoformat(),
                             "cont_flag": "1"},
                     headers={"Authorization": f"{CLIENT_ID}:{tok}"}, timeout=30)
    r.raise_for_status()
    cs = r.json().get("candles", [])
    df = pd.DataFrame([(datetime.fromtimestamp(x[0], IST).replace(tzinfo=None),
                        x[1], x[2], x[3], x[4]) for x in cs],
                      columns=["ts", "open", "high", "low", "close"])
    return df


def _load_hist_5m():
    """Static 1yr history for backtest/validation."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    df = pd.read_sql("SELECT ts, open, high, low, close FROM nifty_5m_test_data ORDER BY ts", conn)
    conn.close()
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
    return df


# ---------- signal ----------
def _zone_series(df5):
    g = _resample(df5, TF_GATE)
    ef, es = _ema(g["close"].values, EMA_FAST), _ema(g["close"].values, EMA_SLOW)
    z = pd.Series(np.where(ef > es, 1, -1), index=g["ts"])
    return z


def current_signal(live=True):
    """Return the latest actionable signal as of the most recent CLOSED 10m bar."""
    df5 = _fetch_live_5m() if live else _load_hist_5m()
    g10 = _resample(df5, TF_MAIN)
    if len(g10) < ST_PERIOD + 5:
        return {"status": "insufficient_data", "bars": len(g10)}
    o, h, l, c = (g10[x].values for x in ["open", "high", "low", "close"])
    st = _supertrend(o, h, l, c, ST_PERIOD, ST_MULT)
    zone = _zone_series(df5).reindex(g10["ts"], method="ffill").values

    last = len(g10) - 1
    flipped = st[last] != st[last-1]
    direction = int(st[last])
    in_zone = (direction == zone[last])
    signal = "FLAT"
    if flipped and in_zone:
        signal = "BUY" if direction == 1 else "SELL"

    px = float(c[last])
    return {
        "status": "ok",
        "as_of": str(g10["ts"].iloc[last]),
        "price": round(px, 1),
        "st_dir": "up" if direction == 1 else "down",
        "st_flip": bool(flipped),
        "gate_zone": "buy" if zone[last] == 1 else "sell",
        "signal": signal,
        "stop": round(px - SL_PTS, 1) if signal == "BUY" else (round(px + SL_PTS, 1) if signal == "SELL" else None),
        "target": round(px + TGT_PTS, 1) if signal == "BUY" else (round(px - TGT_PTS, 1) if signal == "SELL" else None),
        "spec": f"ST{ST_PERIOD}/{ST_MULT} 10m + EMA{EMA_FAST}/{EMA_SLOW} 30m gate, SL{SL_PTS}/T{TGT_PTS}",
    }


def telegram_alert(msg):
    tok = os.environ.get("V10_TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    chat = os.environ.get("V10_TELEGRAM_CHAT_ID") or os.environ.get("CHAT_ID")
    if not tok or not chat:
        return {"sent": False, "reason": "telegram env not set"}
    try:
        r = requests.get(f"https://api.telegram.org/bot{tok}/sendMessage",
                         params={"chat_id": chat, "text": msg}, timeout=10)
        return {"sent": r.ok}
    except Exception as e:
        return {"sent": False, "reason": str(e)}


def run_and_alert():
    sig = current_signal(live=True)
    if sig.get("signal") in ("BUY", "SELL"):
        msg = (f"V10 ST+EMA SIGNAL: {sig['signal']} NIFTY @ {sig['price']}\n"
               f"Stop {sig['stop']} | Target {sig['target']}\n"
               f"As of {sig['as_of']} | {sig['spec']}")
        sig["alert"] = telegram_alert(msg)
    return sig


if __name__ == "__main__":
    import json
    print(json.dumps(current_signal(live=False), indent=2, default=str))
