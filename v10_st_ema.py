"""
V10 ST+EMA — NIFTY/BANKNIFTY directional intraday paper engine (Scorr module)
=============================================================================
ISOLATED from V8 / paper engine. Reads the live 1m WS feed (intraday_prices)
but does NOT modify it. Advisory + paper only (no real execution).

PIPELINE (every 5 min during market hours, via scheduler tick):
  1m WS feed -> resample CLOSED 1m -> append closed 5m bar into:
      NIFTY50   -> nifty_5m_test_data
      BANKNIFTY -> banknifty_5m_test_data
  -> compute signal on last CLOSED 10m bar (+30m gate)
  -> paper engine: open on signal, close on SL/target/opposite-flip
  -> P&L = points * lot_size   (NIFTY lot 65, BANKNIFTY lot 30)
  -> Telegram alert on a new BUY/SELL

LOCKED PARAMS (both indices use same structure pending BNF re-backtest Thu):
  ST 150/3 on 10m + EMA 3/10 gate on 30m, SL 100 / Target 200 (close-based).
  NIFTY backtest: +5936 pts (~Rs4.45L/lot/yr), 49.3% win, PF 1.88, 150 trades.
  BANKNIFTY params NOT yet optimised — running same as NIFTY for now (paper).

Tables:
  v10_positions : one OPEN row per symbol (UNIQUE symbol,status)
  v10_trades    : closed trade log with points + pnl
"""
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import psycopg2

IST = timezone(timedelta(hours=5, minutes=30))

# ---- Shared strategy params ----
TF_MAIN   = "10min"
TF_GATE   = "30min"
ST_PERIOD = 150
ST_MULT   = 3.0
EMA_FAST  = 3
EMA_SLOW  = 10
SL_PTS    = 100
TGT_PTS   = 200

# NIFTY-only constants kept for backward compat (current_signal)
TABLE       = "nifty_5m_test_data"
FEED_SYMBOL = "NIFTY50"

# ---- Per-index config: feed_symbol -> (table, lot_size) ----
INDEX_CFG = {
    "NIFTY50":   {"table": "nifty_5m_test_data",     "lot": 65},
    "BANKNIFTY": {"table": "banknifty_5m_test_data", "lot": 30},
}


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


# ---------- 1m -> 5m appender ----------
def _append_one(cur, feed_symbol, table):
    df = pd.read_sql(
        "SELECT ts, open, high, low, close FROM intraday_prices "
        "WHERE symbol=%s AND source='fyers' AND timeframe='1m' "
        "AND ts >= NOW() - INTERVAL '2 days' ORDER BY ts",
        cur.connection, params=(feed_symbol,))
    if df.empty:
        return {"feed": feed_symbol, "status": "no_1m_data", "appended": 0}
    df["ts"] = pd.to_datetime(df["ts"])
    g5 = _resample(df, "5min")
    now = pd.Timestamp(datetime.now(IST).replace(tzinfo=None))
    g5 = g5[g5["ts"] + pd.Timedelta(minutes=5) <= now]
    rows = [(r.ts.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close), 0)
            for r in g5.itertuples()]
    if rows:
        cur.executemany(
            f"INSERT INTO {table} (ts,open,high,low,close,volume) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (ts) DO NOTHING", rows)
    cur.execute(f"SELECT COUNT(*), MAX(ts) FROM {table}")
    cnt, mx = cur.fetchone()
    return {"feed": feed_symbol, "table": table, "status": "ok",
            "candidates": len(rows), "table_rows": cnt, "latest": str(mx)}


def build_and_append_5m():
    conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
    results = []
    try:
        for feed_symbol, cfg in INDEX_CFG.items():
            try:
                results.append(_append_one(cur, feed_symbol, cfg["table"]))
            except Exception as e:
                results.append({"feed": feed_symbol, "status": "error", "error": str(e)})
        conn.commit()
    finally:
        cur.close(); conn.close()
    return {"status": "ok", "feeds": results}


# ---------- data + signal core (table-agnostic) ----------
def _load_5m(table):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    df = pd.read_sql(f"SELECT ts, open, high, low, close FROM {table} ORDER BY ts", conn)
    conn.close()
    df["ts"] = pd.to_datetime(df["ts"])
    if getattr(df["ts"].dt, "tz", None) is not None:
        df["ts"] = df["ts"].dt.tz_localize(None)
    return df


def _zone_series(df5):
    g = _resample(df5, TF_GATE)
    ef, es = _ema(g["close"].values, EMA_FAST), _ema(g["close"].values, EMA_SLOW)
    return pd.Series(np.where(ef > es, 1, -1), index=g["ts"])


def _signal_for(table):
    """Return dict with last-closed-10m signal state for a given table."""
    df5 = _load_5m(table)
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
    return {"status": "ok", "as_of": str(g10["ts"].iloc[last]), "price": float(c[last]),
            "st_dir": direction, "flip": bool(flipped), "zone": int(zone[last]), "signal": signal}


def current_signal():
    """NIFTY signal (backward-compatible shape)."""
    s = _signal_for(TABLE)
    if s.get("status") != "ok":
        return s
    px = s["price"]; signal = s["signal"]
    return {
        "status": "ok", "as_of": s["as_of"], "price": round(px, 1),
        "st_dir": "up" if s["st_dir"] == 1 else "down", "st_flip": s["flip"],
        "gate_zone": "buy" if s["zone"] == 1 else "sell", "signal": signal,
        "stop": round(px - SL_PTS, 1) if signal == "BUY" else (round(px + SL_PTS, 1) if signal == "SELL" else None),
        "target": round(px + TGT_PTS, 1) if signal == "BUY" else (round(px - TGT_PTS, 1) if signal == "SELL" else None),
        "spec": f"ST{ST_PERIOD}/{ST_MULT} 10m + EMA{EMA_FAST}/{EMA_SLOW} 30m gate, SL{SL_PTS}/T{TGT_PTS}",
    }


# ---------- paper engine ----------
def _paper_step(cur, feed_symbol, table, lot):
    """Open/close paper position for one index based on current signal + price.
    Close-based SL/target on the latest 10m close; opposite-flip also closes."""
    s = _signal_for(table)
    if s.get("status") != "ok":
        return {"feed": feed_symbol, "status": s.get("status", "err")}
    px = s["price"]; sig = s["signal"]; events = []

    # fetch open position (if any)
    cur.execute("SELECT id, side, entry_price, stop, target FROM v10_positions "
                "WHERE symbol=%s AND status='OPEN'", (feed_symbol,))
    pos = cur.fetchone()

    def _close(pid, side, entry, reason, exit_px):
        pts = (exit_px - entry) if side == "BUY" else (entry - exit_px)
        pnl = pts * lot
        cur.execute("INSERT INTO v10_trades (symbol,side,entry_price,entry_ts,exit_price,exit_ts,"
                    "exit_reason,points,lot_size,pnl) SELECT symbol,side,entry_price,entry_ts,%s,NOW(),"
                    "%s,%s,%s,%s FROM v10_positions WHERE id=%s",
                    (exit_px, reason, round(pts, 2), lot, round(pnl, 2), pid))
        cur.execute("DELETE FROM v10_positions WHERE id=%s", (pid,))
        events.append({"action": "CLOSE", "reason": reason, "points": round(pts, 2), "pnl": round(pnl, 2)})

    if pos:
        pid, side, entry, stop, target = pos
        # check close-based SL/target on latest close
        hit = None
        if side == "BUY":
            if px <= stop: hit = "SL"
            elif px >= target: hit = "TARGET"
        else:  # SELL
            if px >= stop: hit = "SL"
            elif px <= target: hit = "TARGET"
        # opposite signal flip also closes
        if not hit and sig in ("BUY", "SELL") and sig != side:
            hit = "FLIP"
        if hit:
            _close(pid, side, entry, hit, px)
            pos = None  # now flat; may re-enter below on flip

    # open new position if flat and signal present
    if not pos and sig in ("BUY", "SELL"):
        stop = px - SL_PTS if sig == "BUY" else px + SL_PTS
        target = px + TGT_PTS if sig == "BUY" else px - TGT_PTS
        cur.execute("INSERT INTO v10_positions (symbol,side,entry_price,entry_ts,stop,target,lot_size,status) "
                    "VALUES (%s,%s,%s,NOW(),%s,%s,%s,'OPEN') ON CONFLICT (symbol,status) DO NOTHING",
                    (feed_symbol, sig, px, round(stop, 1), round(target, 1), lot))
        events.append({"action": "OPEN", "side": sig, "entry": round(px, 1),
                       "stop": round(stop, 1), "target": round(target, 1)})

    return {"feed": feed_symbol, "price": round(px, 1), "signal": sig, "events": events}


def paper_run():
    """Run paper engine for all indices. Returns events for alerting."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
    out = []
    try:
        for feed_symbol, cfg in INDEX_CFG.items():
            try:
                out.append(_paper_step(cur, feed_symbol, cfg["table"], cfg["lot"]))
            except Exception as e:
                out.append({"feed": feed_symbol, "status": "error", "error": str(e)})
        conn.commit()
    finally:
        cur.close(); conn.close()
    return out


# ---------- read helpers for dashboard ----------
def get_open_positions():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    df = pd.read_sql("SELECT symbol, side, entry_price, entry_ts, stop, target, lot_size "
                     "FROM v10_positions WHERE status='OPEN' ORDER BY entry_ts DESC", conn)
    conn.close()
    return df.to_dict("records")


def get_closed_trades(limit=200):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    df = pd.read_sql("SELECT symbol, side, entry_price, entry_ts, exit_price, exit_ts, "
                     "exit_reason, points, lot_size, pnl FROM v10_trades "
                     "ORDER BY exit_ts DESC LIMIT %s", conn, params=(limit,))
    conn.close()
    return df.to_dict("records")


def get_summary():
    conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
    cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE pnl>0), "
                "COALESCE(ROUND(SUM(pnl)::numeric,2),0), COALESCE(ROUND(SUM(points)::numeric,2),0) FROM v10_trades")
    n, wins, pnl, pts = cur.fetchone()
    cur.close(); conn.close()
    return {"closed_trades": n, "wins": wins, "win_rate": round(wins/n*100, 1) if n else 0,
            "total_points": float(pts or 0), "total_pnl": float(pnl or 0),
            "spec": f"ST{ST_PERIOD}/{ST_MULT} 10m + EMA{EMA_FAST}/{EMA_SLOW} 30m gate, SL{SL_PTS}/T{TGT_PTS}",
            "lots": {"NIFTY50": INDEX_CFG["NIFTY50"]["lot"], "BANKNIFTY": INDEX_CFG["BANKNIFTY"]["lot"]}}


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
    """Full 5-min cycle: append 5m bars (both indices), run paper engine, alert new entries."""
    appended = build_and_append_5m()
    paper = paper_run()
    alerts = []
    for p in paper:
        for ev in p.get("events", []):
            if ev.get("action") == "OPEN":
                msg = (f"V10 {ev['side']} {p['feed']} @ {ev['entry']}\n"
                       f"Stop {ev['stop']} | Target {ev['target']}\n"
                       f"ST{ST_PERIOD}/{ST_MULT} 10m + EMA{EMA_FAST}/{EMA_SLOW} 30m gate")
                alerts.append(telegram_alert(msg))
    return {"append": appended, "paper": paper, "alerts": alerts}


if __name__ == "__main__":
    import json
    print(json.dumps(current_signal(), indent=2, default=str))
