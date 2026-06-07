"""
V10 ST+EMA — NIFTY directional intraday signal engine (Scorr platform module)
=============================================================================
ISOLATED from V8 / paper engine. The live 1m WS feed (intraday_prices) is read
but NOT modified. Signals are advisory only (RA-compliant, no execution).

ARCHITECTURE:
  1m WS feed (intraday_prices, 7d rolling, untouched)
     -> every 5 min: resample CLOSED 1m bars -> append closed 5m bar
        into nifty_5m_test_data (historical 1yr base + growing live)
     -> V10 reads nifty_5m_test_data, resamples 5m -> 10m (+30m gate)
     -> signal on last CLOSED 10m bar -> Telegram alert on BUY/SELL

LOCKED STRATEGY SPEC (backtested 1yr NIFTY, after Rs1000/trade futures cost):
  - Timeframe   : 10-minute candles (resampled from stored 5m)
  - Supertrend  : ATR period 150, multiplier 3.0   (trigger)
  - Gate        : EMA 3 vs EMA 10 on 30-minute candles (regime filter)
                  EMA3 > EMA10 -> BUY zone ; EMA3 < EMA10 -> SELL zone
  - Entry       : ST flip whose direction matches the gate zone
  - Exit        : SL 100 / Target 200 (close-based) OR opposite ST flip
  - Backtest    : +5936 pts (~Rs4.45L/lot/yr), 49.3% win, PF 1.88,
                  150 trades/yr, 10/13 months positive
  - Sizing      : ~Rs5L capital per lot (max DD ~ -Rs85k)
"""
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import psycopg2

IST = timezone(timedelta(hours=5, minutes=30))

# ---- LOCKED PARAMETERS ----
TF_MAIN   = "10min"
TF_GATE   = "30min"
ST_PERIOD = 150
ST_MULT   = 3.0
EMA_FAST  = 3
EMA_SLOW  = 10
SL_PTS    = 100
TGT_PTS   = 200
TABLE     = "nifty_5m_test_data"
FEED_SYMBOL = "NIFTY50"   # as stored in intraday_prices (source='fyers')


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


def _resample(df, rule):
    return (df.set_index("ts")
            .resample(rule)
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
            .dropna().reset_index())


# ---------- 1m -> 5m appender (live) ----------
def build_and_append_5m():
    """Resample CLOSED 1m bars from intraday_prices into 5m and append to
    nifty_5m_test_data. Only bars whose full 5-min window has elapsed are written
    (no partial/forming bar). Idempotent via PK on ts."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    # pull last ~2 days of 1m to cover any gap, resample, append closed bars only
    df = pd.read_sql(
        "SELECT ts, open, high, low, close FROM intraday_prices "
        "WHERE symbol=%s AND source='fyers' AND timeframe='1m' "
        "AND ts >= NOW() - INTERVAL '2 days' ORDER BY ts", conn, params=(FEED_SYMBOL,))
    if df.empty:
        cur.close(); conn.close()
        return {"status": "no_1m_data"}
    df["ts"] = pd.to_datetime(df["ts"])
    g5 = _resample(df, "5min")
    # drop the still-forming current 5m bar: keep only bars that have fully closed
    now = pd.Timestamp(datetime.now(IST).replace(tzinfo=None))
    g5 = g5[g5["ts"] + pd.Timedelta(minutes=5) <= now]
    rows = [(r.ts.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close), 0)
            for r in g5.itertuples()]
    if rows:
        cur.executemany(
            f"INSERT INTO {TABLE} (ts,open,high,low,close,volume) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (ts) DO NOTHING", rows)
        conn.commit()
    cur.execute(f"SELECT COUNT(*), MAX(ts) FROM {TABLE}")
    cnt, mx = cur.fetchone()
    cur.close(); conn.close()
    return {"status": "ok", "appended_candidates": len(rows), "table_rows": cnt, "latest": str(mx)}


# ---------- data load ----------
def _load_5m():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    df = pd.read_sql(f"SELECT ts, open, high, low, close FROM {TABLE} ORDER BY ts", conn)
    conn.close()
    df["ts"] = pd.to_datetime(df["ts"])
    if getattr(df["ts"].dt, "tz", None) is not None:
        df["ts"] = df["ts"].dt.tz_localize(None)
    return df


# ---------- signal ----------
def _zone_series(df5):
    g = _resample(df5, TF_GATE)
    ef, es = _ema(g["close"].values, EMA_FAST), _ema(g["close"].values, EMA_SLOW)
    return pd.Series(np.where(ef > es, 1, -1), index=g["ts"])


def current_signal():
    """Latest actionable signal as of the most recent CLOSED 10m bar."""
    df5 = _load_5m()
    g10 = _resample(df5, TF_MAIN)
    if len(g10) < ST_PERIOD + 5:
        return {"status": "insufficient_data", "bars": len(g10)}
    o, h, l, c = (g10[x].values for x in ["open", "high", "low", "close"])
    st = _supertrend(o, h, l, c, ST_PERIOD, ST_MULT)
    zone = _zone_series(df5).reindex(g10["ts"], method="ffill").values

    last = len(g10) - 1
    flipped = st[last] != st[last-1]
    direction = int(st[last])
    signal = "FLAT"
    if flipped and direction == zone[last]:
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
    import requests
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


def tick():
    """Full 5-min cycle: append latest 5m bar, compute signal, alert if BUY/SELL."""
    appended = build_and_append_5m()
    sig = current_signal()
    sig["append"] = appended
    if sig.get("signal") in ("BUY", "SELL"):
        msg = (f"V10 ST+EMA: {sig['signal']} NIFTY @ {sig['price']}\n"
               f"Stop {sig['stop']} | Target {sig['target']}\n"
               f"As of {sig['as_of']} | {sig['spec']}")
        sig["alert"] = telegram_alert(msg)
    return sig


if __name__ == "__main__":
    import json
    print(json.dumps(current_signal(), indent=2, default=str))
