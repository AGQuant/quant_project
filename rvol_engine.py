"""cc#674 + cc#680: Time-of-day-adjusted Relative Volume (RVOL) engine.

RVOL = today's cumulative volume up to the current 5-min slot / the 21-session AVERAGE cumulative
volume up to the SAME slot. A 1.4x RVOL at 10:30 means the symbol has already traded 1.4x the volume
it typically has by 10:30 — a pace read, not a full-day compare.

cc#680 CRITICAL FIX — intraday volume semantics. intraday_prices 5-min `volume` is stored CUMULATIVE
on most sessions (each bar = cumulative shares since open; the day's final bar ≈ the raw_prices day
volume) but PER-BAR on some. Naively SUM()-ing cumulative bars inflated the profile ~13-38x, so RVOL
read ~0.1x when the truth was ~1.0x. This is the same trap the v8_signal_writer VolX baseline already
handles (cc#150/cc#170 monotonic→cumulative detection). We now detect per session and anchor to the
raw_prices day volume (ground truth):
  * build_profiles() — per session, is_cumulative if MAX(bar) >= 0.3 * raw_prices day-volume (a single
    5-min bar is never 30% of a day unless it's a cumulative counter). cum(t) = bar-value if cumulative
    else running-sum; each session's curve is normalised to its raw day volume, then the whole per-slot
    average is scaled so the final slot = the symbol's 21d avg raw volume. Invariant: final slot must
    be within 0.5x–2x of the 21d raw avg, else the symbol is dropped + ops_log rvol_profile_mismatch.
  * live_rvol(cur, symbol) — today's cumulative volume is cumulative-aware (majority-non-decreasing →
    cumulative → latest bar; else SUM), divided by the profile at the current slot.
"""
import os
import logging
from datetime import datetime, timedelta

import psycopg

log = logging.getLogger("rvol_engine")
DATABASE_URL = os.getenv("DATABASE_URL", "")

SESSION_START = "09:15"
SESSION_END = "15:25"
EARLY_SLOTS = ("09:15:00", "09:20:00")
MIN_SESSIONS = 10

# cc#680: rebuild + anchor SQL (also used verbatim by the one-shot re-seed). Per-session cumulative
# detection via the raw_prices day-volume anchor; normalise each session to its raw day volume; scale
# the per-slot average so the 15:25 slot == the symbol's 21d avg raw volume.
_BUILD_SQL = """
WITH dedup AS (
  SELECT DISTINCT ON (symbol, ts) symbol, ts, volume FROM intraday_prices
  WHERE source IN ('fyers_eq','fyers_hist') AND ts::date >= CURRENT_DATE - 45
  ORDER BY symbol, ts, CASE source WHEN 'fyers_eq' THEN 0 ELSE 1 END),
bars AS (SELECT symbol, ts::date d, ts::time slot, volume,
                SUM(volume) OVER (PARTITION BY symbol, ts::date ORDER BY ts) run_sum FROM dedup),
sess AS (SELECT b.symbol, b.d, MAX(b.volume) max_bar, MAX(b.run_sum) sum_bar, rp.volume raw_vol
         FROM bars b JOIN raw_prices rp ON rp.symbol=b.symbol AND rp.price_date=b.d
         GROUP BY b.symbol, b.d, rp.volume),
sess2 AS (SELECT symbol, d, raw_vol, (max_bar >= 0.3*raw_vol) is_cum,
                 CASE WHEN max_bar >= 0.3*raw_vol THEN max_bar ELSE sum_bar END day_max
          FROM sess WHERE raw_vol > 0),
cs AS (SELECT b.symbol, b.d, b.slot,
              (CASE WHEN s.is_cum THEN b.volume ELSE b.run_sum END)::numeric/NULLIF(s.day_max,0)*s.raw_vol cum_norm,
              DENSE_RANK() OVER (PARTITION BY b.symbol ORDER BY b.d DESC) rnk
       FROM bars b JOIN sess2 s ON s.symbol=b.symbol AND s.d=b.d),
prof AS (SELECT symbol, slot, AVG(cum_norm) p, COUNT(DISTINCT d) n
         FROM cs WHERE rnk<=21 AND slot BETWEEN %(ss)s AND %(se)s GROUP BY symbol, slot),
raw21 AS (SELECT symbol, AVG(raw_vol) r FROM (SELECT DISTINCT symbol, d, raw_vol,
             DENSE_RANK() OVER (PARTITION BY symbol ORDER BY d DESC) rk FROM sess2) z WHERE rk<=21 GROUP BY symbol),
p1525 AS (SELECT symbol, p FROM prof WHERE slot=%(se_t)s)
INSERT INTO rvol_profiles (symbol, slot_time, avg_cum_vol, sessions_used, computed_at)
SELECT prof.symbol, prof.slot,
       GREATEST(ROUND(prof.p * COALESCE(raw21.r/NULLIF(p1525.p,0),1)),0),
       prof.n, NOW()
FROM prof JOIN raw21 ON raw21.symbol=prof.symbol LEFT JOIN p1525 ON p1525.symbol=prof.symbol
"""


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


def _ops_log(cur, category, title, details):
    try:
        import json
        cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                    "VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)", (category, title, json.dumps(details)))
    except Exception:
        pass


def build_profiles(conn=None):
    """Nightly rebuild: cumulative-aware, raw-anchored per-slot average cumulative-volume profiles for
    every equity symbol with recent fyers_eq intraday. Full replace. Invariant: drop any symbol whose
    final-slot avg strays outside 0.5x–2x of its 21d raw avg (ops_log rvol_profile_mismatch)."""
    own = conn is None
    conn = conn or _conn()
    try:
        cur = conn.cursor()
        _ensure_table(cur)
        cur.execute("DELETE FROM rvol_profiles")
        cur.execute(_BUILD_SQL, {"ss": SESSION_START, "se": SESSION_END, "se_t": SESSION_END + ":00"})
        # invariant: final-slot avg must be within 0.5x–2x of the symbol's 21d avg raw volume.
        cur.execute("""
            WITH fin AS (SELECT DISTINCT ON (symbol) symbol, avg_cum_vol FROM rvol_profiles
                         ORDER BY symbol, slot_time DESC),
            raw21 AS (SELECT symbol, AVG(volume) r FROM (
                        SELECT symbol, volume, row_number() OVER (PARTITION BY symbol ORDER BY price_date DESC) rk
                        FROM raw_prices) z WHERE rk<=21 GROUP BY symbol)
            SELECT f.symbol, ROUND(f.avg_cum_vol), ROUND(r.r) FROM fin f JOIN raw21 r ON r.symbol=f.symbol
            WHERE r.r > 0 AND (f.avg_cum_vol < 0.5*r.r OR f.avg_cum_vol > 2.0*r.r)""")
        bad = cur.fetchall()
        for sym, fin_v, raw_v in bad:
            cur.execute("DELETE FROM rvol_profiles WHERE symbol=%s", (sym,))
            _ops_log(cur, "rvol", "rvol_profile_mismatch",
                     {"symbol": sym, "final_slot_avg": float(fin_v or 0), "raw21_avg": float(raw_v or 0)})
        cur.execute("SELECT COUNT(DISTINCT symbol), COUNT(*) FROM rvol_profiles")
        nsym, nrows = cur.fetchone()
        conn.commit()
        log.info(f"rvol_profiles: {nsym} symbols, {nrows} rows, {len(bad)} dropped (invariant)")
        return {"symbols": nsym, "rows": nrows, "dropped": len(bad)}
    finally:
        if own:
            conn.close()


def live_rvol(cur, symbol):
    """On-demand RVOL. Today's cumulative volume is CUMULATIVE-AWARE (cc#680): if today's bars are a
    majority non-decreasing series they are a cumulative counter → today_cum = the latest bar; else
    per-bar → today_cum = SUM. Divided by the profile at the latest completed slot."""
    sym = (symbol or "").upper()
    cur.execute("""SELECT MAX(ts::date) FROM intraday_prices
                   WHERE symbol=%s AND source IN ('fyers_eq','fyers_hist')""", (sym,))
    r = cur.fetchone()
    asof = r[0] if r else None
    if not asof:
        return None
    cur.execute("""SELECT ts::time, volume FROM (
                     SELECT DISTINCT ON (ts) ts, volume FROM intraday_prices
                     WHERE symbol=%s AND source IN ('fyers_eq','fyers_hist') AND ts::date=%s
                     ORDER BY ts, CASE source WHEN 'fyers_eq' THEN 0 ELSE 1 END) q ORDER BY ts""", (sym, asof))
    bars = cur.fetchall()
    if not bars:
        return None
    slot = bars[-1][0]
    vols = [float(v or 0) for _, v in bars]
    # cumulative if the series is majority non-decreasing (tolerates a few feed dips).
    if len(vols) >= 3:
        nondec = sum(1 for a, b in zip(vols, vols[1:]) if b >= a)
        is_cum = (nondec / max(len(vols) - 1, 1)) >= 0.6
    else:
        is_cum = False
    cum_vol = max(vols) if is_cum else sum(vols)
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
