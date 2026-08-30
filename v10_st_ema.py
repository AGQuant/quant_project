"""
V10 ST+EMA — NIFTY/BANKNIFTY directional intraday paper engine (Scorr module)
=============================================================================
ISOLATED from V8 / paper engine. Reads live 5m feed from intraday_prices; does NOT
modify them. Paper + advisory only.

PIPELINE (every 5 min, market hours, via scheduler tick):
  5m bars from intraday_prices (source=fyers_eq, timeframe=5m) -> append to:
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

PARAMS: ST 150/3 (10m) + EMA 3/10 gate (30m).
  NIFTY:     SL 100 / Target 200 (close-based).
  BANKNIFTY: SL 150 / Target 300 (wider — BNF more volatile; per-index since
             BNF optimisation backtest still pending). See INDEX_CFG.
  NIFTY backtest (FUT): +5936 pts (~Rs4.45L/lot/yr), 49.3% win, PF 1.88, 150 trades.

Option data: option_chain table (NIFTY+BANKNIFTY index options only, ~3-4d rolling,
  monthly expiry). ATM = strike nearest to underlying at signal time.

09-Jun-2026: switched from 1m aggregation to direct 5m read (intraday_prices,
  source=fyers_eq, timeframe=5m). Scorr is a 5-min system per spec id=167.
22-Jun-2026 (cc_task #65): migrated psycopg2 -> psycopg (v3) to match the rest
  of the codebase; tick() now accepts the scheduler's connection; added
  _backfill_5m(), gap_exit(), get_performance(); per-index SL/TGT; signal now
  returns st_band + flip_ts.
"""
import os
from decimal import Decimal
from datetime import datetime, timedelta, timezone, date

import numpy as np
import pandas as pd
import psycopg

IST = timezone(timedelta(hours=5, minutes=30))

# cc#1478: these module-level constants are the NIFTY DEFAULTS, kept so any caller that still
# reads them gets NIFTY values — the per-index truth lives in INDEX_CFG below.
TF_MAIN   = "10min"
TF_GATE   = "30min"
ST_PERIOD = 150
ST_MULT   = 3.0
EMA_FAST  = 3
EMA_SLOW  = 10
SL_PTS    = 100   # NIFTY default (kept for backtest spec string / fallback)
TGT_PTS   = 200

TABLE       = "nifty_5m_test_data"
FEED_SYMBOL = "NIFTY50"

# feed_symbol -> full per-index engine config (cc#1478 built the plumbing; cc#1482 amends the
# BANKNIFTY values to session_log 34206, V10_BANKNIFTY_SPEC_V3_10M_250_LOCKED, superseding
# 34192's 20m/EMA5-13/SL150-T150 before it ever traded a Monday tick). The signal path
# (timeframes, SuperTrend, EMA gate) is per index, not module-global. NIFTY is UNCHANGED per
# 34179 (ST150/3.0 10m, EMA3/10 30m gate, SL100/T200 — byte-identical signals, verified
# old-vs-new on the same data). BANKNIFTY: ST150/4.0 on 10m, EMA3/10 with the 30m gate,
# SL250/T250 — founder-locked knowing the Jun-Aug replay segment was negative.
INDEX_CFG = {
    "NIFTY50":   {"table": "nifty_5m_test_data",     "lot": 65, "oc": "NIFTY",
                  "tf_main": "10min", "tf_gate": "30min",
                  "st_period": 150, "st_mult": 3.0, "ema_fast": 3, "ema_slow": 10,
                  "sl_pts": 100, "tgt_pts": 200},
    "BANKNIFTY": {"table": "banknifty_5m_test_data", "lot": 30, "oc": "BANKNIFTY",
                  "tf_main": "10min", "tf_gate": "30min",
                  "st_period": 150, "st_mult": 4.0, "ema_fast": 3, "ema_slow": 10,
                  "sl_pts": 250, "tgt_pts": 250},
}


def _spec_str(cfg):
    """cc#1478: ONE spec-string builder for current_signal and get_summary — per index, never a
    module-global echo. NIFTY renders exactly the pre-refactor string."""
    tfm = cfg["tf_main"].replace("min", "m")
    tfg = cfg["tf_gate"].replace("min", "m")
    return (f"ST{cfg['st_period']}/{cfg['st_mult']} {tfm} + EMA{cfg['ema_fast']}/{cfg['ema_slow']} "
            f"{tfg} gate, SL{cfg['sl_pts']}/T{cfg['tgt_pts']}")


# ---------- db helpers (psycopg v3) ----------
def _db():
    return psycopg.connect(os.environ["DATABASE_URL"])


def _read_df(conn, sql, params=None):
    """Read a query into a DataFrame using a psycopg v3 connection (no SQLAlchemy).
    Coerces Decimal columns to float so downstream numpy/JSON behave as they did
    under pd.read_sql; leaves datetime/text columns untouched."""
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d[0] for d in cur.description]
        data = cur.fetchall()
    df = pd.DataFrame(data, columns=cols)
    for col in cols:
        sample = next((v for v in df[col] if v is not None), None)
        if isinstance(sample, Decimal):
            df[col] = df[col].astype(float)
    return df


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
    """Returns (direction array, supertrend line array). The line is the active
    band: final lower band when up-trend, final upper band when down-trend."""
    n = len(c); a = _atr(h, l, c, period); hl2 = (h+l)/2
    up = hl2 + mult*a; lo = hl2 - mult*a
    fu = up.copy(); fl = lo.copy(); d = np.ones(n, int)
    for i in range(1, n):
        fu[i] = up[i] if (up[i] < fu[i-1] or c[i-1] > fu[i-1]) else fu[i-1]
        fl[i] = lo[i] if (lo[i] > fl[i-1] or c[i-1] < fl[i-1]) else fl[i-1]
        d[i] = 1 if c[i] > fu[i-1] else (-1 if c[i] < fl[i-1] else d[i-1])
    line = np.where(d == 1, fl, fu)
    return d, line


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


# ---------- 5m appender (reads intraday_prices directly, no aggregation) ----------
def _append_one(cur, feed_symbol, table):
    df = _read_df(
        cur.connection,
        "SELECT ts, open, high, low, close FROM intraday_prices "
        "WHERE symbol=%s AND source='fyers_eq' AND timeframe='5m' "
        "AND ts >= NOW() - INTERVAL '2 days' ORDER BY ts",
        (feed_symbol,))
    if df.empty:
        return {"feed": feed_symbol, "status": "no_5m_data", "appended": 0}
    df["ts"] = pd.to_datetime(df["ts"])
    # strip tz if present
    if getattr(df["ts"].dt, "tz", None) is not None:
        df["ts"] = df["ts"].dt.tz_localize(None)
    # only use bars that are fully closed (bar_ts + 5min <= now)
    now = pd.Timestamp(datetime.now(IST).replace(tzinfo=None))
    df = df[df["ts"] + pd.Timedelta(minutes=5) <= now]
    rows = [(r.ts.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close), 0)
            for r in df.itertuples()]
    if rows:
        cur.executemany(
            f"INSERT INTO {table} (ts,open,high,low,close,volume) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (ts) DO NOTHING", rows)
    cur.execute(f"SELECT COUNT(*), MAX(ts) FROM {table}")
    cnt, mx = cur.fetchone()
    return {"feed": feed_symbol, "table": table, "status": "ok",
            "candidates": len(rows), "table_rows": cnt, "latest": str(mx)}


def build_and_append_5m(conn=None):
    c = conn or _db(); owned = conn is None
    results = []
    try:
        with c.cursor() as cur:
            for feed_symbol, cfg in INDEX_CFG.items():
                try:
                    results.append(_append_one(cur, feed_symbol, cfg["table"]))
                except Exception as e:
                    results.append({"feed": feed_symbol, "status": "error", "error": str(e)})
        c.commit()
    finally:
        if owned:
            c.close()
    return {"status": "ok", "feeds": results}


def _backfill_5m(days=5, conn=None):
    """Repair the 5m tables from intraday_prices over the last N days. Idempotent
    (ON CONFLICT DO NOTHING). Use after a tick outage to fill the gap, e.g. POST
    /api/v10/backfill. Unlike _append_one this ignores the 'fully closed' filter
    for past sessions and loads everything available."""
    c = conn or _db(); owned = conn is None
    out = []
    try:
        with c.cursor() as cur:
            for feed_symbol, cfg in INDEX_CFG.items():
                cur.execute(
                    "SELECT ts, open, high, low, close FROM intraday_prices "
                    "WHERE symbol=%s AND source='fyers_eq' AND timeframe='5m' "
                    "AND ts >= NOW() - (%s || ' days')::interval ORDER BY ts",
                    (feed_symbol, days))
                bars = cur.fetchall()
                rows = [(b[0], float(b[1]), float(b[2]), float(b[3]), float(b[4]), 0)
                        for b in bars]
                if rows:
                    cur.executemany(
                        f"INSERT INTO {cfg['table']} (ts,open,high,low,close,volume) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (ts) DO NOTHING", rows)
                cur.execute(f"SELECT COUNT(*), MAX(ts) FROM {cfg['table']}")
                cnt, mx = cur.fetchone()
                out.append({"feed": feed_symbol, "loaded": len(rows),
                            "table_rows": cnt, "latest": str(mx)})
        c.commit()
    finally:
        if owned:
            c.close()
    return {"status": "ok", "days": days, "feeds": out}


# ---------- signal core ----------
def _load_5m(table):
    conn = _db()
    try:
        df = _read_df(conn, f"SELECT ts, open, high, low, close FROM {table} ORDER BY ts")
    finally:
        conn.close()
    df["ts"] = pd.to_datetime(df["ts"])
    if getattr(df["ts"].dt, "tz", None) is not None:
        df["ts"] = df["ts"].dt.tz_localize(None)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    return df


def _zone_series(df5, cfg):
    g = _resample(df5, cfg["tf_gate"])
    ef, es = _ema(g["close"].values, cfg["ema_fast"]), _ema(g["close"].values, cfg["ema_slow"])
    return pd.Series(np.where(ef > es, 1, -1), index=g["ts"])


def _signal_for(cfg):
    """cc#1478: takes the per-index cfg, not a bare table — timeframes, ST and the EMA gate all
    come from it. `g10` keeps its name though BANKNIFTY resamples to 20m: renaming every local
    would have widened this diff for zero behaviour."""
    df5 = _load_5m(cfg["table"])
    g10 = _resample(df5, cfg["tf_main"])
    if len(g10) < cfg["st_period"] + 5:
        return {"status": "insufficient_data", "bars": len(g10)}
    o, h, l, c = (g10[x].values for x in ["open", "high", "low", "close"])
    st, st_line = _supertrend(o, h, l, c, cfg["st_period"], cfg["st_mult"])
    zone = _zone_series(df5, cfg).reindex(g10["ts"], method="ffill").values
    last = len(g10) - 1
    flipped = st[last] != st[last-1]
    direction = int(st[last])
    signal = "FLAT"
    if flipped and direction == zone[last]:
        signal = "BUY" if direction == 1 else "SELL"
    # last flip timestamp (bar where direction last changed)
    diffs = np.where(np.diff(st) != 0)[0]
    flip_idx = int(diffs[-1] + 1) if len(diffs) else 0
    return {"status": "ok", "as_of": str(g10["ts"].iloc[last]), "price": float(c[last]),
            "st_dir": direction, "flip": bool(flipped), "zone": int(zone[last]),
            "signal": signal, "st_band": float(st_line[last]),
            "flip_ts": str(g10["ts"].iloc[flip_idx])}


def current_signal(feed_symbol="NIFTY50"):
    cfg = INDEX_CFG.get(feed_symbol, INDEX_CFG[FEED_SYMBOL])
    sl = cfg.get("sl_pts", SL_PTS); tgt = cfg.get("tgt_pts", TGT_PTS)
    s = _signal_for(cfg)
    if s.get("status") != "ok":
        return {**s, "symbol": feed_symbol}
    px = s["price"]; signal = s["signal"]
    return {
        "status": "ok", "symbol": feed_symbol, "as_of": s["as_of"], "price": round(px, 1),
        "st_dir": "up" if s["st_dir"] == 1 else "down", "st_flip": s["flip"],
        "st_band": round(s["st_band"], 1), "flip_ts": s["flip_ts"],
        "gate_zone": "buy" if s["zone"] == 1 else "sell", "signal": signal,
        "stop": round(px - sl, 1) if signal == "BUY" else (round(px + sl, 1) if signal == "SELL" else None),
        "target": round(px + tgt, 1) if signal == "BUY" else (round(px - tgt, 1) if signal == "SELL" else None),
        "spec": _spec_str(cfg),   # cc#1478: per index — NIFTY's string is byte-identical to before
    }


# ---------- option chain lookup ----------
def _atm_option(cur, oc_underlying, opt_type, underlying_px):
    """Nearest-strike (ATM) nearest-expiry option's latest ltp for the given side.
    Returns (strike, expiry, ltp) or (None,None,None) if no data.

    cc#746 step 3: expiry is resolved PER underlying — MIN(expiry) WHERE underlying=%s AND
    expiry >= CURRENT_DATE — so NIFTY (weekly) and BANKNIFTY (monthly-only) each pick their OWN
    nearest live contract; there is NO shared-cycle assumption (confirmed correct). Known gap
    (FOUNDER DECISION PENDING, no roll implemented yet): on EXPIRY DAY, MIN(expiry) equals today,
    so a fresh entry writes the same-day expiring contract instead of rolling to the next series.
    The roll THRESHOLD (e.g. stop selecting the expiring series after HH:MM on expiry day) is the
    open decision — wire it here once the founder sets it."""
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


def _paper_step(cur, feed_symbol, cfg):
    """cc#1478: takes the per-index cfg whole — the signal path below needs the timeframes and
    ST/EMA params, not just the table. Open rows keep THEIR OWN stored stop/target (read from the
    row further down, never from cfg) — the live BANKNIFTY pair entered 28-Aug keeps its
    SL150/T300 exactly as entered; only entries AFTER this cutover take the new SL150/T150."""
    table, lot, oc = cfg["table"], cfg["lot"], cfg["oc"]
    sl_pts, tgt_pts = cfg["sl_pts"], cfg["tgt_pts"]
    s = _signal_for(cfg)
    if s.get("status") != "ok":
        return {"feed": feed_symbol, "status": s.get("status", "err")}
    px = s["price"]; sig = s["signal"]; events = []

    cur.execute("SELECT id, side, leg, entry_price, stop, target, opt_strike, opt_type, opt_expiry "
                "FROM v10_positions WHERE symbol=%s AND status='OPEN'", (feed_symbol,))
    legs = cur.fetchall()

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
        fstop = px - sl_pts if sig == "BUY" else px + sl_pts
        ftarget = px + tgt_pts if sig == "BUY" else px - tgt_pts
        # FUT leg
        cur.execute("INSERT INTO v10_positions (symbol,side,entry_price,entry_ts,stop,target,lot_size,status,leg) "
                    "VALUES (%s,%s,%s,NOW(),%s,%s,%s,'OPEN','FUT') ON CONFLICT (symbol,leg,status) DO NOTHING",
                    (feed_symbol, sig, px, round(fstop, 1), round(ftarget, 1), lot))
        events.append({"action": "OPEN", "leg": "FUT", "side": sig, "entry": round(px, 1),
                       "stop": round(fstop, 1), "target": round(ftarget, 1)})
        # OPT leg: BUY->write PE, SELL->write CE. The option leg is always WRITTEN (sold), so its
        # side label is SELL regardless of the signal direction — cc#746 fix: previously stored `sig`,
        # which mislabelled BUY-signal PE legs as side=BUY. P&L is unaffected (_close_leg's OPT branch
        # is pure premium-collected math and never reads side).
        otype = "PE" if sig == "BUY" else "CE"
        strike, expiry, prem = _atm_option(cur, oc, otype, px)
        if strike is not None and prem is not None:
            cur.execute("INSERT INTO v10_positions (symbol,side,entry_price,entry_ts,stop,target,lot_size,status,leg,opt_strike,opt_type,opt_expiry) "
                        "VALUES (%s,'SELL',%s,NOW(),NULL,NULL,%s,'OPEN','OPT',%s,%s,%s) ON CONFLICT (symbol,leg,status) DO NOTHING",
                        (feed_symbol, prem, lot, strike, otype, expiry))
            events.append({"action": "OPEN", "leg": "OPT", "side": "SELL", "write": otype, "strike": strike,
                           "expiry": str(expiry), "premium": prem})
        else:
            events.append({"action": "OPT_SKIP", "reason": "no option_chain data for ATM"})

    return {"feed": feed_symbol, "price": round(px, 1), "signal": sig, "events": events}


def paper_run(conn=None):
    c = conn or _db(); owned = conn is None
    out = []
    try:
        with c.cursor() as cur:
            for feed_symbol, cfg in INDEX_CFG.items():
                try:
                    out.append(_paper_step(cur, feed_symbol, cfg))
                except Exception as e:
                    out.append({"feed": feed_symbol, "status": "error", "error": str(e)})
        c.commit()
    finally:
        if owned:
            c.close()
    return out


def gap_exit(conn=None):
    """Force-close any OPEN position stranded by a tick outage — i.e. whose entry
    pre-dates the latest available 5m bar's session. Exits at the first bar OPEN
    strictly after the entry date (reason GAP_EXIT). Both legs close together.
    Safe to call repeatedly: no-op when nothing is stranded."""
    c = conn or _db(); owned = conn is None
    closed = []
    try:
        with c.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM v10_positions WHERE status='OPEN'")
            syms = [r[0] for r in cur.fetchall()]
            for sym in syms:
                cfg = INDEX_CFG.get(sym)
                if not cfg:
                    continue
                table, oc = cfg["table"], cfg["oc"]
                cur.execute("SELECT MIN(entry_ts::date) FROM v10_positions WHERE symbol=%s AND status='OPEN'", (sym,))
                entry_date = cur.fetchone()[0]
                cur.execute(f"SELECT MAX(ts::date) FROM {table}")
                last_date = cur.fetchone()[0]
                if entry_date is None or last_date is None or entry_date >= last_date:
                    continue  # not stranded
                cur.execute(f"SELECT open FROM {table} WHERE ts::date > %s ORDER BY ts LIMIT 1", (entry_date,))
                row = cur.fetchone()
                if not row:
                    continue
                gap_open = float(row[0])
                cur.execute("SELECT id, leg, opt_strike, opt_type, opt_expiry, entry_price "
                            "FROM v10_positions WHERE symbol=%s AND status='OPEN'", (sym,))
                for pid, leg, ostrike, otype, oexp, entry in cur.fetchall():
                    if leg == "FUT":
                        ev = _close_leg(cur, pid, "GAP_EXIT", gap_open)
                    else:
                        ltp = _opt_ltp(cur, oc, otype, ostrike, oexp)
                        ev = _close_leg(cur, pid, "GAP_EXIT", ltp if ltp is not None else float(entry))
                    closed.append({"symbol": sym, **ev})
        c.commit()
    finally:
        if owned:
            c.close()
    return {"status": "ok", "closed": closed}


# ---------- dashboard reads ----------
def _fx(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def get_open_positions():
    # cc#434 fix_1: RAW cursor (was _read_df -> pandas -> to_dict gave numpy/Timestamp types that
    # broke JSON serialization -> 500 -> the page showed "0 open positions"). fix_2: attach a
    # last-tick CMP (cmp_prices holds the last known LTP => Friday close off-market) + unrealised P&L
    # (FUT: (cmp-entry)*lot*dir; OPT is the ATM WRITE leg -> (entry-premium)*lot, premium from the
    # latest option_chain snapshot, as-of stamped).
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, leg, side, entry_price, entry_ts, stop, target, lot_size, "
                        "opt_strike, opt_type, opt_expiry FROM v10_positions WHERE status='OPEN' "
                        "ORDER BY symbol, leg")
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.execute("SELECT symbol, cmp FROM cmp_prices WHERE symbol IN ('NIFTY50','BANKNIFTY')")
            cmp_map = {r[0]: _fx(r[1]) for r in cur.fetchall()}
            out = []
            for r in rows:
                sym = r["symbol"]; leg = (r["leg"] or "").upper()
                entry = _fx(r["entry_price"]); lot = int(r["lot_size"] or 0)
                dirmul = 1 if (r["side"] or "").upper() == "BUY" else -1
                cmp_v = pnl = asof = None
                if leg == "FUT":
                    cmp_v = cmp_map.get(sym)
                    if cmp_v is not None and entry is not None:
                        pnl = round((cmp_v - entry) * lot * dirmul, 2)
                elif leg == "OPT" and r.get("opt_strike") and r.get("opt_type"):
                    under = "NIFTY" if sym in ("NIFTY50", "NIFTY") else sym
                    cur.execute("SELECT ltp, ts FROM option_chain WHERE underlying=%s "
                                "AND strike=%s::numeric AND option_type=%s ORDER BY ts DESC LIMIT 1",
                                (under, str(r["opt_strike"]), r["opt_type"]))
                    orow = cur.fetchone()
                    if orow and orow[0] is not None:
                        cmp_v = _fx(orow[0])
                        asof = orow[1].isoformat() if orow[1] else None
                        if entry is not None:      # ATM write -> profit as premium decays
                            pnl = round((entry - cmp_v) * lot, 2)
                out.append({
                    "symbol": sym, "leg": leg, "side": (r["side"] or "").upper(),
                    "entry_price": entry,
                    "entry_ts": r["entry_ts"].isoformat() if r["entry_ts"] else None,
                    "stop": _fx(r["stop"]), "target": _fx(r["target"]), "lot_size": lot,
                    "opt_strike": r["opt_strike"], "opt_type": r["opt_type"],
                    "opt_expiry": str(r["opt_expiry"]) if r.get("opt_expiry") else None,
                    "cmp": cmp_v, "unrealised_pnl": pnl, "premium_asof": asof,
                })
    finally:
        conn.close()
    return out


def get_closed_trades(limit=200):
    # cc#434 fix_1: RAW cursor (was _read_df -> pandas serialization 500 -> empty closed-trades list).
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, leg, side, entry_price, entry_ts, exit_price, exit_ts, "
                        "exit_reason, points, lot_size, pnl, opt_strike, opt_type FROM v10_trades "
                        "ORDER BY exit_ts DESC LIMIT %s", (limit,))
            cols = [d[0] for d in cur.description]
            out = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                out.append({
                    "symbol": d["symbol"], "leg": d["leg"], "side": d["side"],
                    "entry_price": _fx(d["entry_price"]),
                    "entry_ts": d["entry_ts"].isoformat() if d["entry_ts"] else None,
                    "exit_price": _fx(d["exit_price"]),
                    "exit_ts": d["exit_ts"].isoformat() if d["exit_ts"] else None,
                    "exit_reason": d["exit_reason"], "points": _fx(d["points"]),
                    "lot_size": int(d["lot_size"]) if d["lot_size"] is not None else None,
                    "pnl": _fx(d["pnl"]), "opt_strike": d["opt_strike"], "opt_type": d["opt_type"],
                })
    finally:
        conn.close()
    return out


def get_summary():
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT leg, COUNT(*), COUNT(*) FILTER (WHERE pnl>0), "
                        "COALESCE(ROUND(SUM(pnl)::numeric,2),0) FROM v10_trades GROUP BY leg")
            by_leg = {r[0]: {"trades": r[1], "wins": r[2], "pnl": float(r[3])} for r in cur.fetchall()}
    finally:
        conn.close()
    # cc#1478: the spec line is PER INDEX now (a one-string NIFTY line under-described the book
    # the moment BANKNIFTY ran different parameters), and the stale hand-carried backtest block
    # (+5936 / 49.3 / 1.88, plus a derived rupee figure) is REPLACED by the honest no-lookahead
    # replay numbers — NIFTY per session_log 34179 (bt4), BANKNIFTY per 34192 (the cfg this card
    # cut over to). Never restate these from memory: they are the logged replay outputs.
    return {
        "spec": {sym: _spec_str(cfg) for sym, cfg in INDEX_CFG.items()},
        "lots": {"NIFTY50": INDEX_CFG["NIFTY50"]["lot"], "BANKNIFTY": INDEX_CFG["BANKNIFTY"]["lot"]},
        "live_paper": by_leg,
        "backtest_nifty_fut": {"points": 4030, "win_rate": 42.6, "profit_factor": 1.48,
                               "trades": 148, "max_dd_pts": -778,
                               "note": "no-lookahead replay 28-Aug-25..28-Aug-26 (34179)"},
        "backtest_bnf_fut": {"points": 8856, "win_rate": 55.0, "profit_factor": 1.46,
                             "trades": 149, "max_dd_pts": -1248,
                             "note": "no-lookahead replay 28-Aug-25..28-Aug-26 (session_log 34206)"},
    }



def _cut_stats(r):
    """cc#1247 — one segment's stats, struck with the SAME formulas as the overall card.

    NULL, NOT ZERO, WHERE A NUMBER WOULD BE A LIE. A segment with no trades has no win rate — 0.0%
    would read as "it lost every trade" rather than "it has none" — and a segment that has never
    taken a loss has no profit factor, because the denominator is zero and not small. Both come back
    None and the surface renders an em-dash. This is deliberately stricter than the overall block
    above, which still returns 0.0 for an empty book; that number is on the do-not-touch list for
    this card, so it is left alone and only the new per-segment fields follow the honest rule.
    """
    key, trades, wins, pnl, gp, gl = r
    return {
        "trades": trades,
        "wins": wins,
        "pnl": float(pnl),
        "win_rate": round(100.0 * wins / trades, 1) if trades else None,
        "profit_factor": round(float(gp) / abs(float(gl)), 2) if float(gl) != 0 else None,
    }


def get_performance():
    """Full live-paper stats from v10_trades for the dashboard performance panel."""
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE pnl>0), COALESCE(SUM(pnl),0), "
                "COALESCE(AVG(points) FILTER (WHERE pnl>0),0), "
                "COALESCE(AVG(points) FILTER (WHERE pnl<=0),0), "
                "COALESCE(SUM(pnl) FILTER (WHERE pnl>0),0), "
                "COALESCE(SUM(pnl) FILTER (WHERE pnl<0),0) FROM v10_trades")
            tot, wins, pnl, avg_win, avg_loss, gross_profit, gross_loss = cur.fetchone()
            # cc#1247: the two cuts now carry gross profit and gross loss as well, so win rate and
            # profit factor can be struck PER SEGMENT from the SAME formulas the total uses. This is
            # read-path aggregation only — two extra aggregate columns on queries that already
            # grouped these rows. No engine logic, no new table, no changed definition.
            _CUT = ("SELECT {k}, COUNT(*), COUNT(*) FILTER (WHERE pnl>0), COALESCE(SUM(pnl),0), "
                    "COALESCE(SUM(pnl) FILTER (WHERE pnl>0),0), "
                    "COALESCE(SUM(pnl) FILTER (WHERE pnl<0),0) FROM v10_trades GROUP BY {k}")
            cur.execute(_CUT.format(k="symbol"))
            by_symbol = {r[0]: _cut_stats(r) for r in cur.fetchall()}
            cur.execute(_CUT.format(k="leg"))
            by_leg = {r[0]: _cut_stats(r) for r in cur.fetchall()}
            cur.execute("SELECT exit_ts::date d, COALESCE(SUM(pnl),0) FROM v10_trades "
                        "WHERE exit_ts >= NOW() - INTERVAL '7 days' GROUP BY d ORDER BY d")
            last_7 = [{"date": str(r[0]), "pnl": float(r[1])} for r in cur.fetchall()]
            cur.execute("SELECT points FROM v10_trades WHERE points IS NOT NULL ORDER BY exit_ts")
            pts = [float(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()
    # max drawdown over the cumulative points equity curve
    eq = peak = max_dd = 0.0
    for p in pts:
        eq += p
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
    tot = tot or 0
    gp, gl = float(gross_profit), float(gross_loss)
    pf = round(gp / abs(gl), 2) if gl != 0 else None
    return {
        "total_trades": tot,
        "win_rate": round(100 * wins / tot, 1) if tot else 0.0,
        "total_pnl": round(float(pnl), 2),
        "avg_win_pts": round(float(avg_win), 2),
        "avg_loss_pts": round(float(avg_loss), 2),
        "profit_factor": pf,
        "max_drawdown_pts": round(max_dd, 2),
        "last_7_days": last_7,
        "by_symbol": by_symbol,
        "by_leg": by_leg,
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


def tick(conn=None):
    """Scheduler entry point. Accepts the scheduler's psycopg v3 connection (or
    opens its own if called standalone). Appends 5m bars, runs the paper engine,
    and fires Telegram alerts on new FUT entries."""
    appended = build_and_append_5m(conn)
    paper = paper_run(conn)
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
