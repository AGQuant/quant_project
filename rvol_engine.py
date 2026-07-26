"""cc#674: Time-of-day-adjusted Relative Volume (RVOL) engine.

RVOL = today's cumulative volume up to the current 5-min slot / the 21-session AVERAGE cumulative
volume up to the SAME slot. A 1.4x RVOL at 10:30 means the symbol has already traded 1.4x the volume
it typically has by 10:30 — a pace read, not a full-day compare.

Two parts:
  * build_profiles()  — nightly (~02:10 IST, after universe_technicals): per fyers_eq symbol, average
    the cumulative-volume curve across the last 21 sessions (fyers_eq+fyers_hist union, deduped on ts),
    one row per 5-min slot 09:15–15:25 into rvol_profiles (replace-per-symbol).
  * live_rvol(cur, symbol) — on-demand (deriv-metrics): today's cum vol at the latest completed slot /
    the profile average at that slot. Off-hours returns the last session's FINAL value with its date.

Read-only for callers other than the nightly builder; no live-engine coupling.
"""
import os
import logging
from datetime import datetime, timedelta

import psycopg

log = logging.getLogger("rvol_engine")
DATABASE_URL = os.getenv("DATABASE_URL", "")

SESSION_START = "09:15"
SESSION_END = "15:25"
EARLY_SLOTS = ("09:15:00", "09:20:00")   # first two slots are noisiest → UI badges EARLY
MIN_SESSIONS = 10                        # need >=10 sessions in a slot for a trustworthy RVOL


def _conn():
    return psycopg.connect(DATABASE_URL)


def _ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _is_market_hours(now=None):
    now = now or _ist_now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= hm <= (15 * 60 + 30)


def _ensure_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS rvol_profiles (
        symbol TEXT NOT NULL, slot_time TIME NOT NULL,
        avg_cum_vol NUMERIC, sessions_used INT,
        computed_at TIMESTAMPTZ DEFAULT NOW(), PRIMARY KEY (symbol, slot_time))""")


def build_profiles(conn=None):
    """Nightly: rebuild the per-slot average cumulative-volume profile for every equity symbol that has
    recent fyers_eq intraday bars. ~212 symbols × ~75 slots ≈ 16k rows. Replace-per-symbol (idempotent)."""
    own = conn is None
    conn = conn or _conn()
    try:
        cur = conn.cursor()
        _ensure_table(cur)
        cur.execute("""SELECT DISTINCT symbol FROM intraday_prices
                       WHERE source='fyers_eq' AND ts::date >= CURRENT_DATE - 45 ORDER BY symbol""")
        syms = [r[0] for r in cur.fetchall()]
        built, rows_total = 0, 0
        for sym in syms:
            cur.execute("""SELECT DISTINCT ts::date d FROM intraday_prices
                           WHERE symbol=%s AND source IN ('fyers_eq','fyers_hist')
                           ORDER BY d DESC LIMIT 21""", (sym,))
            dates = [r[0] for r in cur.fetchall()]
            if len(dates) < 3:
                continue
            cur.execute("""
                WITH bars AS (
                  SELECT ts::date d, ts::time slot,
                         SUM(volume) OVER (PARTITION BY ts::date ORDER BY ts) cum_vol
                  FROM (SELECT DISTINCT ON (ts) ts, volume, source FROM intraday_prices
                        WHERE symbol=%s AND source IN ('fyers_eq','fyers_hist') AND ts::date = ANY(%s)
                        ORDER BY ts, CASE source WHEN 'fyers_eq' THEN 0 ELSE 1 END) q )
                SELECT slot, ROUND(AVG(cum_vol))::numeric, COUNT(DISTINCT d)
                FROM bars WHERE slot BETWEEN %s AND %s GROUP BY slot ORDER BY slot""",
                (sym, dates, SESSION_START, SESSION_END))
            prof = cur.fetchall()
            if not prof:
                continue
            cur.execute("DELETE FROM rvol_profiles WHERE symbol=%s", (sym,))
            for slot, avg_cv, su in prof:
                cur.execute("""INSERT INTO rvol_profiles (symbol, slot_time, avg_cum_vol, sessions_used, computed_at)
                               VALUES (%s,%s,%s,%s,NOW())""", (sym, slot, avg_cv, su))
            built += 1
            rows_total += len(prof)
        conn.commit()
        log.info(f"rvol_profiles: rebuilt {built} symbols, {rows_total} slot-rows")
        return {"symbols": built, "rows": rows_total}
    finally:
        if own:
            conn.close()


def live_rvol(cur, symbol):
    """On-demand RVOL for the deriv-metrics energy block. Returns a compact dict or None.
    NULL (value=None) when the profile has <10 sessions at the current slot, or no profile/intraday yet."""
    sym = (symbol or "").upper()
    cur.execute("""SELECT MAX(ts::date) FROM intraday_prices
                   WHERE symbol=%s AND source IN ('fyers_eq','fyers_hist')""", (sym,))
    r = cur.fetchone()
    asof = r[0] if r else None
    if not asof:
        return None
    # today's (last session's) cumulative volume up to the latest completed slot
    cur.execute("""SELECT MAX(ts::time), SUM(volume) FROM (
                     SELECT DISTINCT ON (ts) ts, volume FROM intraday_prices
                     WHERE symbol=%s AND source IN ('fyers_eq','fyers_hist') AND ts::date=%s
                     ORDER BY ts, CASE source WHEN 'fyers_eq' THEN 0 ELSE 1 END) q""", (sym, asof))
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    slot, cum_vol = row[0], float(row[1] or 0)
    slot_s = str(slot)
    cur.execute("SELECT avg_cum_vol, sessions_used FROM rvol_profiles WHERE symbol=%s AND slot_time=%s",
                (sym, slot))
    p = cur.fetchone()
    now = _ist_now()
    closed = (not _is_market_hours(now)) or (asof != now.date())
    base = {"slot": slot_s[:5], "asof": str(asof), "closed": closed,
            "early": slot_s in EARLY_SLOTS, "sessions_used": (p[1] if p else 0)}
    if not p or p[0] is None or (p[1] or 0) < MIN_SESSIONS:
        base["rvol"] = None
        base["insufficient"] = True
        return base
    avg_cv = float(p[0])
    base["rvol"] = round(cum_vol / avg_cv, 2) if avg_cv > 0 else None
    return base
