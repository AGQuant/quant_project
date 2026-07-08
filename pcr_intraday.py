"""
pcr_intraday.py — 5-min intraday PCR rollup (Scorr platform).
================================================================
Forward-capture PERMANENT 5-min PCR for NIFTY + BANKNIFTY.

Reads the live option_chain table (the same source the EOD PCR uses) +
intraday_prices for the spot anchor. Computes TWO PCRs per 5-min bar:

  pcr_atm5  : ATM +/- 5 strikes  (focused signal)
  pcr_total : full stored band   (standard market PCR)

Writes to pcr_intraday (permanent — NO rolling purge).

Self-healing: compute_pcr_intraday() with no args does every option_chain ts
not yet in pcr_intraday (covers any scheduler miss within the 7-day option
window). Pass a ts to (re)compute a single bar.

Strike intervals: NIFTY 50, BANKNIFTY 100. ATM = nearest interval to spot close.

Mounted in main.py via app.include_router(pcr_router) from pcr_endpoints.py.
Scheduler calls compute_pcr_intraday() every 5-min in _live_loop().

NOTE (07-Jun-2026): live option feed writes to option_chain (confirmed source
of scheduler _compute_and_store_pcr). The repo fyers_options_feed.py writes
options_prices, which does NOT exist in prod — drift to resolve on Monday
fyers restart. This module reads option_chain to match production.
"""

import os
from datetime import datetime, timedelta

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL")

# Strike interval per underlying
INTERVALS = {"NIFTY": 50, "BANKNIFTY": 100}
# Spot symbol in intraday_prices per underlying
SPOT_SYMBOL = {"NIFTY": "NIFTY50", "BANKNIFTY": "BANKNIFTY"}
ATM_BAND = 5  # ATM +/- 5 strikes


def get_conn():
    return psycopg.connect(DATABASE_URL)


def setup_table(conn):
    """Create the permanent pcr_intraday table (idempotent)."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pcr_intraday (
                ts            TIMESTAMP   NOT NULL,
                underlying    TEXT        NOT NULL,
                spot          NUMERIC,
                atm_strike    NUMERIC,
                pcr_atm5      NUMERIC,
                put_oi_atm5   BIGINT,
                call_oi_atm5  BIGINT,
                pcr_total     NUMERIC,
                put_oi_total  BIGINT,
                call_oi_total BIGINT,
                computed_at   TIMESTAMP   DEFAULT NOW(),
                PRIMARY KEY (ts, underlying)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pcr_intraday_ul_ts ON pcr_intraday(underlying, ts DESC)")
    conn.commit()
    return {"status": "ok", "table": "pcr_intraday"}


def _nearest_spot(conn, underlying, ts):
    """Spot close at-or-before ts from intraday_prices (within 10 min)."""
    sym = SPOT_SYMBOL.get(underlying)
    if not sym:
        return None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT close FROM intraday_prices
            WHERE symbol = %s AND ts <= %s AND ts >= %s
            ORDER BY ts DESC LIMIT 1
        """, (sym, ts, ts - timedelta(minutes=10)))
        r = cur.fetchone()
    return float(r[0]) if r and r[0] is not None else None


def _compute_one(conn, underlying, ts):
    """Compute + upsert one (ts, underlying) PCR bar. Returns row dict or None."""
    interval = INTERVALS.get(underlying)
    if not interval:
        return None

    spot = _nearest_spot(conn, underlying, ts)
    atm_strike = round(spot / interval) * interval if spot is not None else None
    lo = hi = None
    if atm_strike is not None:
        lo = atm_strike - ATM_BAND * interval
        hi = atm_strike + ATM_BAND * interval

    with conn.cursor() as cur:
        # total band (all stored strikes for this ts/underlying)
        cur.execute("""
            SELECT
                SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END),
                SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END)
            FROM option_chain
            WHERE underlying = %s AND ts = %s
        """, (underlying, ts))
        row = cur.fetchone()
        put_total = int(row[0]) if row and row[0] is not None else 0
        call_total = int(row[1]) if row and row[1] is not None else 0

        # ATM +/- 5 band (only if we have a spot anchor)
        put_atm = call_atm = None
        if lo is not None:
            cur.execute("""
                SELECT
                    SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END),
                    SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END)
                FROM option_chain
                WHERE underlying = %s AND ts = %s AND strike BETWEEN %s AND %s
            """, (underlying, ts, lo, hi))
            r2 = cur.fetchone()
            put_atm = int(r2[0]) if r2 and r2[0] is not None else 0
            call_atm = int(r2[1]) if r2 and r2[1] is not None else 0

        pcr_total = round(put_total / call_total, 4) if call_total else None
        pcr_atm5 = (round(put_atm / call_atm, 4) if (call_atm and put_atm is not None) else None)

        # cc#292: bad/partial options-chain fetch guard. put_atm5==0 AND call_atm5==0 is the
        # reliable tell (observed on EVERY bad tick 02-Jul): the ATM±5 strikes didn't load, and the
        # total-band figures are unreliable too — call_oi_total froze at 80,015 while put stayed
        # ~normal, producing an impossible pcr_total=25.95 (normal NIFTY PCR is 0.5-2.0). Secondary
        # heuristic: a one-sided >75% total-OI collapse vs the last good tick (the other side steady)
        # is the same corruption without the ATM=0 signature. On a bad tick, null pcr_total (and
        # pcr_atm5) — extending the pipeline's existing pcr_atm5 null-on-bad-tick behavior — so the
        # impossible spike never enters the series/chart. Raw OI is still stored for forensics.
        bad_tick = (put_atm == 0 and call_atm == 0)
        if not bad_tick and pcr_total is not None:
            cur.execute("""
                SELECT put_oi_total, call_oi_total FROM pcr_intraday
                WHERE underlying=%s AND ts < %s AND pcr_total IS NOT NULL
                  AND put_oi_total IS NOT NULL AND call_oi_total IS NOT NULL
                ORDER BY ts DESC LIMIT 1
            """, (underlying, ts))
            pr = cur.fetchone()
            if pr and pr[0] and pr[1]:
                p_drop = put_total  < pr[0] * 0.25
                c_drop = call_total < pr[1] * 0.25
                if p_drop != c_drop:   # exactly one side collapsed — one-sided corruption
                    bad_tick = True
        # cc#292: hard sanity bound — a total-band PCR outside [0.1, 5] is implausible for
        # NIFTY/BANKNIFTY (normal 0.5-2.0) and only arises from a corrupt/partial OI fetch (a
        # one-sided collapse, or a put/call side reading 0). Catch-all guaranteeing no impossible
        # value ever reaches the series/chart, even when the ATM=0 tell doesn't fire (e.g. no spot
        # anchor at 09:05 leaves ATM null while call_oi_total collapsed → pcr_total=26).
        if pcr_total is not None and (pcr_total > 5 or pcr_total < 0.1):
            bad_tick = True
        if bad_tick:
            pcr_total = None   # null pcr_total (extends the existing pcr_atm5 null-on-bad-tick pattern)

        cur.execute("""
            INSERT INTO pcr_intraday
                (ts, underlying, spot, atm_strike,
                 pcr_atm5, put_oi_atm5, call_oi_atm5,
                 pcr_total, put_oi_total, call_oi_total, computed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (ts, underlying) DO UPDATE SET
                spot=EXCLUDED.spot, atm_strike=EXCLUDED.atm_strike,
                pcr_atm5=EXCLUDED.pcr_atm5, put_oi_atm5=EXCLUDED.put_oi_atm5,
                call_oi_atm5=EXCLUDED.call_oi_atm5, pcr_total=EXCLUDED.pcr_total,
                put_oi_total=EXCLUDED.put_oi_total, call_oi_total=EXCLUDED.call_oi_total,
                computed_at=NOW()
        """, (ts, underlying, spot, atm_strike,
              pcr_atm5, put_atm, call_atm,
              pcr_total, put_total, call_total))
    conn.commit()
    return {
        "ts": str(ts), "underlying": underlying, "spot": spot,
        "atm_strike": atm_strike, "pcr_atm5": pcr_atm5, "pcr_total": pcr_total,
    }


def compute_pcr_intraday(ts=None, conn=None):
    """
    Main entry.
      ts=None  -> self-heal: compute every option_chain ts not yet in pcr_intraday.
      ts given -> (re)compute that single bar (str 'YYYY-MM-DD HH:MM:SS' or datetime).
    """
    own = conn is None
    if own:
        conn = get_conn()
    try:
        setup_table(conn)

        if ts is not None:
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            out = []
            for ul in INTERVALS:
                r = _compute_one(conn, ul, ts)
                if r:
                    out.append(r)
            return {"status": "ok", "mode": "single", "bars": out}

        # self-heal: option_chain ts/underlying pairs missing from pcr_intraday
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT oc.ts, oc.underlying
                FROM option_chain oc
                LEFT JOIN pcr_intraday p
                    ON p.ts = oc.ts AND p.underlying = oc.underlying
                WHERE p.ts IS NULL
                ORDER BY oc.ts, oc.underlying
            """)
            todo = cur.fetchall()

        done = 0
        for (t, ul) in todo:
            if ul in INTERVALS:
                if _compute_one(conn, ul, t):
                    done += 1
        return {"status": "ok", "mode": "heal", "computed": done, "pending": len(todo)}
    finally:
        if own:
            conn.close()


def get_pcr_intraday(underlying="NIFTY", days=2, conn=None):
    """Read 5-min PCR trend for an underlying over the last N days (most recent first)."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        cutoff = datetime.utcnow() + timedelta(hours=5, minutes=30) - timedelta(days=days)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ts, spot, atm_strike, pcr_atm5, put_oi_atm5, call_oi_atm5,
                       pcr_total, put_oi_total, call_oi_total
                FROM pcr_intraday
                WHERE underlying = %s AND ts >= %s
                ORDER BY ts DESC
            """, (underlying.upper(), cutoff))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            r["ts"] = str(r["ts"])
            for k in ("spot", "atm_strike", "pcr_atm5", "pcr_total"):
                if r[k] is not None:
                    r[k] = float(r[k])
        return {"underlying": underlying.upper(), "days": days, "count": len(rows), "rows": rows}
    finally:
        if own:
            conn.close()


def get_pcr_intraday_hourly(underlying="NIFTY", conn=None):
    """cc#290: total PCR (pcr_total) at the xx:15 hourly mark (09:15-15:15) across the most
    recent 5 TRADING days (rolling window, auto-advances daily). Mirrors the
    /api/v8/indiavix_intraday 5-day/hourly rollup exactly, applied to pcr_intraday. Missing
    marks are skipped, never interpolated. Single series (total PCR, not the ATM±5 breakdown)."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        ul = underlying.upper()
        with conn.cursor() as cur:
            cur.execute("""
                WITH days AS (
                    SELECT DISTINCT ts::date AS d
                    FROM pcr_intraday
                    WHERE underlying = %s
                    ORDER BY d DESC LIMIT 5
                )
                SELECT ts, pcr_total
                FROM pcr_intraday
                WHERE underlying = %s
                  AND ts::date IN (SELECT d FROM days)
                  AND EXTRACT(MINUTE FROM ts) = 15
                  AND EXTRACT(HOUR FROM ts) BETWEEN 9 AND 15
                ORDER BY ts ASC
            """, (ul, ul))
            points = []
            for ts, pcr in cur.fetchall():
                if pcr is None:
                    continue
                points.append({"ts": str(ts), "pcr": float(pcr)})
        return {"underlying": ul, "points": points, "count": len(points)}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    print(compute_pcr_intraday())
