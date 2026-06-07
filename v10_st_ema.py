"""
V10 ST+EMA — NIFTY/BANKNIFTY directional intraday paper engine (Scorr module)
=============================================================================
ISOLATED from V8 / paper engine. Reads live 1m WS feed + option_chain; does NOT
modify them. Paper + advisory only.

PIPELINE (every 5 min, market hours, via scheduler tick):
  1m WS feed -> resample CLOSED 1m -> append closed 5m bar:
      NIFTY50   -> nifty_5m_test_data
      BANKNIFTY -> banknifty_5m_test_data
  -> signal on last CLOSED 10m bar (+30m gate)
  -> TWO-LEG paper engine, both legs open/close together:
       FUT leg : P&L = points * lot           (NIFTY lot 65, BANKNIFTY lot 30)
       OPT leg : write ATM monthly option from option_chain
                 BUY signal  -> write PUT (PE) ; SELL signal -> write CALL (CE)
                 P&L = (entry_ltp - exit_ltp) * lot   (writing: premium decay = gain)
  -> close on SL/target (close-based on underlying) OR opposite-flip
  -> Telegram alert on new entry

PARAMS: ST 150/3 (10m) + EMA 3/10 gate (30m), SL 100 / Target 200 (close-based).
  NIFTY backtest (FUT): +5936 pts (~Rs4.45L/lot/yr), 49.3% win, PF 1.88, 150 trades.
  BANKNIFTY params NOT yet optimised (running NIFTY params on paper until Thu).

Option data: option_chain table (NIFTY+BANKNIFTY index options only, ~3-4d rolling,
  monthly expiry). ATM = strike nearest to underlying at signal time.
"""
import os
from datetime import datetime, timedelta, timezone, date

import numpy as np
import pandas as pd
import psycopg2

IST = timezone(timedelta(hours=5, minutes=30))

TF_MAIN   = "10min"
TF_GATE   = "30min"
ST_PERIOD = 150
ST_MULT   = 3.0
EMA_FAST  = 3
EMA_SLOW  = 10
SL_PTS    = 100
TGT_PTS   = 200

TABLE       = "nifty_5m_test_data"
FEED_SYMBOL = "NIFTY50"

# feed_symbol -> (5m table, lot, option_chain underlying tag)
INDEX_CFG = {
    "NIFTY50":   {"table": "nifty_5m_test_data",     "lot": 65, "oc": "NIFTY"},
    "BANKNIFTY": {"table": "banknifty_5m_test_data", "lot": 30, "oc": "BANKNIFTY"},
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


# ---------- signal core ----------
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


# ---------- option chain lookup ----------
def _atm_option(cur, oc_underlying, opt_type, underlying_px):
    """Nearest-strike (ATM) monthly option's latest ltp for the given side.
    Returns (strike, expiry, ltp) or (None,None,None) if no data."""
    cur.execute(
        "SELECT strike, expiry, ltp FROM option_chain "
        "WHERE underlying=%s AND option_type=%s "
        "AND expiry = (SELECT MIN(expiry) FROM option_chain WHERE underlying=%s AND expiry >= CURRENT_DATE) "
        "AND ts = (SELECT MAX(ts) FROM option_chain WHERE underlying=%s) "
        "ORDER BY ABS(strike - %s) ASC LIMIT 1",
        (oc_underlying, opt_type, oc_underlying, oc_underlying, underlying_px))
    r = cur.fetchone()
    if not r:
        return (None, None, None)
    return (float(r[0]), r[1], float(r[2]) if r[2] is not None else None)


def _opt_ltp(cur, oc_underlying, opt_type, strike, expiry):
    cur.execute(
        "SELECT ltp FROM option_chain WHERE underlying=%s AND option_type=%s AND strike=%s AND expiry=%s "
        "ORDER BY ts DESC LIMIT 1", (oc_underlying, opt_type, strike, expiry))
    r = cur.fetchone()
    return float(r[0]) if r and r[0] is not None else None


# ---------- paper engine (two-leg) ----------
def _close_leg(cur, pid, reason, exit_px_or_ltp):
    """Move an open position row into v10_trades with computed P&L, then delete it."""
    cur.execute("SELECT symbol, side, entry_price, lot_size, leg, opt_strike, opt_type FROM v10_positions WHERE id=%s", (pid,))
    sym, side, entry, lot, leg, ostrike, otype = cur.fetchone()
    if leg == "FUT":
        pts = (exit_px_or_ltp - entry) if side == "BUY" else (entry - exit_px_or_ltp)
        pnl = pts * lot
    else:  # OPT — writing: gain when premium falls
        pts = entry - exit_px_or_ltp        # premium collected - premium paid to close
        pnl = pts * lot
    cur.execute(
        "INSERT INTO v10_trades (symbol,side,entry_price,entry_ts,exit_price,exit_ts,exit_reason,"
        "points,lot_size,pnl,leg,opt_strike,opt_type) "
        "SELECT symbol,side,entry_price,entry_ts,%s,NOW(),%s,%s,%s,%s,leg,opt_strike,opt_type "
        "FROM v10_positions WHERE id=%s",
        (round(exit_px_or_ltp, 2), reason, round(pts, 2), lot, round(pnl, 2), pid))
    cur.execute("DELETE FROM v10_positions WHERE id=%s", (pid,))
    return {"leg": leg, "reason": reason, "points": round(pts, 2), "pnl": round(pnl, 2)}


def _paper_step(cur, feed_symbol, table, lot, oc):
    s = _signal_for(table)
    if s.get("status") != "ok":
        return {"feed": feed_symbol, "status": s.get("status", "err")}
    px = s["price"]; sig = s["signal"]; events = []

    cur.execute("SELECT id, side, leg, entry_price, stop, target, opt_strike, opt_type, opt_expiry "
                "FROM v10_positions WHERE symbol=%s AND status='OPEN'", (feed_symbol,))
    legs = cur.fetchall()

    # decide exit: SL/target on underlying close, or opposite-flip
    def _exit_reason(side):
        if side == "BUY":
            if px <= (px*0+ (entry_fut - SL_PTS)) : pass
        return None

    # Determine if we should close (based on FUT leg semantics; both legs close together)
    fut = next((x for x in legs if x[2] == "FUT"), None)
    hit = None
    if fut:
        _, fside, _, fentry, fstop, ftarget, _, _, _ = fut
        if fside == "BUY":
            if px <= fstop: hit = "SL"
            elif px >= ftarget: hit = "TARGET"
        else:
            if px >= fstop: hit = "SL"
            elif px <= ftarget: hit = "TARGET"
        if not hit and sig in ("BUY", "SELL") and sig != fside:
            hit = "FLIP"

    if legs and hit:
        for row in legs:
            pid, side, leg, entry, stop, target, ostrike, otype, oexp = row
            if leg == "FUT":
                ev = _close_leg(cur, pid, hit, px)
            else:
                ltp = _opt_ltp(cur, oc, otype, ostrike, oexp)
                ev = _close_leg(cur, pid, hit, ltp if ltp is not None else entry)
            events.append({"action": "CLOSE", **ev})
        legs = []

    # open both legs if flat and signal present
    if not legs and sig in ("BUY", "SELL"):
        fstop = px - SL_PTS if sig == "BUY" else px + SL_PTS
        ftarget = px + TGT_PTS if sig == "BUY" else px - TGT_PTS
        # FUT leg
        cur.execute("INSERT INTO v10_positions (symbol,side,entry_price,entry_ts,stop,target,lot_size,status,leg) "
                    "VALUES (%s,%s,%s,NOW(),%s,%s,%s,'OPEN','FUT') ON CONFLICT (symbol,leg,status) DO NOTHING",
                    (feed_symbol, sig, px, round(fstop, 1), round(ftarget, 1), lot))
        events.append({"action": "OPEN", "leg": "FUT", "side": sig, "entry": round(px, 1),
                       "stop": round(fstop, 1), "target": round(ftarget, 1)})
        # OPT leg: BUY->write PE, SELL->write CE
        otype = "PE" if sig == "BUY" else "CE"
        strike, expiry, prem = _atm_option(cur, oc, otype, px)
        if strike is not None and prem is not None:
            cur.execute("INSERT INTO v10_positions (symbol,side,entry_price,entry_ts,stop,target,lot_size,status,leg,opt_strike,opt_type,opt_expiry) "
                        "VALUES (%s,%s,%s,NOW(),NULL,NULL,%s,'OPEN','OPT',%s,%s,%s) ON CONFLICT (symbol,leg,status) DO NOTHING",
                        (feed_symbol, sig, prem, lot, strike, otype, expiry))
            events.append({"action": "OPEN", "leg": "OPT", "write": otype, "strike": strike,
                           "expiry": str(expiry), "premium": prem})
        else:
            events.append({"action": "OPT_SKIP", "reason": "no option_chain data for ATM"})

    return {"feed": feed_symbol, "price": round(px, 1), "signal": sig, "events": events}


def paper_run():
    conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
    out = []
    try:
        for feed_symbol, cfg in INDEX_CFG.items():
            try:
                out.append(_paper_step(cur, feed_symbol, cfg["table"], cfg["lot"], cfg["oc"]))
            except Exception as e:
                out.append({"feed": feed_symbol, "status": "error", "error": str(e)})
        conn.commit()
    finally:
        cur.close(); conn.close()
    return out


# ---------- dashboard reads ----------
def get_open_positions():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    df = pd.read_sql("SELECT symbol, leg, side, entry_price, entry_ts, stop, target, lot_size, "
                     "opt_strike, opt_type, opt_expiry FROM v10_positions WHERE status='OPEN' "
                     "ORDER BY symbol, leg", conn)
    conn.close()
    return df.to_dict("records")


def get_closed_trades(limit=200):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    df = pd.read_sql("SELECT symbol, leg, side, entry_price, entry_ts, exit_price, exit_ts, "
                     "exit_reason, points, lot_size, pnl, opt_strike, opt_type FROM v10_trades "
                     "ORDER BY exit_ts DESC LIMIT %s", conn, params=(limit,))
    conn.close()
    return df.to_dict("records")


def get_summary():
    conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
    cur.execute("SELECT leg, COUNT(*), COUNT(*) FILTER (WHERE pnl>0), "
                "COALESCE(ROUND(SUM(pnl)::numeric,2),0) FROM v10_trades GROUP BY leg")
    by_leg = {r[0]: {"trades": r[1], "wins": r[2], "pnl": float(r[3])} for r in cur.fetchall()}
    cur.close(); conn.close()
    return {
        "spec": f"ST{ST_PERIOD}/{ST_MULT} 10m + EMA{EMA_FAST}/{EMA_SLOW} 30m gate, SL{SL_PTS}/T{TGT_PTS}",
        "lots": {"NIFTY50": INDEX_CFG["NIFTY50"]["lot"], "BANKNIFTY": INDEX_CFG["BANKNIFTY"]["lot"]},
        "live_paper": by_leg,
        "backtest_nifty_fut": {"points": 5936, "annual_rs_per_lot": 445000, "win_rate": 49.3,
                               "profit_factor": 1.88, "trades": 150, "max_dd_pts": -1138,
                               "note": "1yr NIFTY, after Rs1000/trade (harshest cost)"},
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
    appended = build_and_append_5m()
    paper = paper_run()
    alerts = []
    for p in paper:
        opens = [e for e in p.get("events", []) if e.get("action") == "OPEN" and e.get("leg") == "FUT"]
        for ev in opens:
            optev = next((e for e in p["events"] if e.get("action") == "OPEN" and e.get("leg") == "OPT"), None)
            optline = (f"\nWrite {optev['write']} {optev['strike']} @ {optev['premium']}" if optev else "")
            msg = (f"V10 {ev['side']} {p['feed']} @ {ev['entry']}\n"
                   f"Stop {ev['stop']} | Target {ev['target']}{optline}")
            alerts.append(telegram_alert(msg))
    return {"append": appended, "paper": paper, "alerts": alerts}


if __name__ == "__main__":
    import json
    print(json.dumps(current_signal(), indent=2, default=str))
