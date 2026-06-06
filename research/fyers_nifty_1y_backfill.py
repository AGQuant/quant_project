"""
fyers_nifty_1y_backfill.py  —  STANDALONE 1-year NIFTY 5m backfill (V10 prep)

ISOLATED. Not in the live feed, no scheduler, no _BG_TASKS.
Reuses the SAME daily token stored in fyers_tokens (id=1) by fyers_feed.py.

- Source     : Fyers v3 history API (/data/history)
- Symbol     : NSE:NIFTY50-INDEX  (index spot)
- Resolution : 5  (5-minute)
- Window     : Fyers caps intraday history at 100 days/request, so we loop
               ~100-day windows backward to cover ~1 year (4 calls).
- Target     : nifty_5m_research  (own table; touches nothing live)
- Idempotent : upsert on ts. Re-runnable safely.

Callable two ways:
  1. CLI on Railway/desktop:  python research/fyers_nifty_1y_backfill.py
  2. run_backfill() imported by the /api/research/backfill_nifty endpoint.
"""
import os
import time
import logging
from datetime import datetime, timedelta, timezone, date

import requests
import psycopg2

log = logging.getLogger("nifty_backfill")

DB_URL          = os.environ["DATABASE_URL"]
FYERS_CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "1A4STS8ZGD-100")
HISTORY_URL     = "https://api-t1.fyers.in/data/history"
SYMBOL          = "NSE:NIFTY50-INDEX"
RESOLUTION      = "5"
IST             = timezone(timedelta(hours=5, minutes=30))

WINDOW_DAYS   = 100     # Fyers intraday history cap per request
TOTAL_DAYS    = 365     # ~1 year
SLEEP_BETWEEN = 1.0     # politeness between history calls


def _get_token(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT access_token, access_created FROM fyers_tokens WHERE id=1")
        row = cur.fetchone()
    if not row or not row[0]:
        raise RuntimeError("No Fyers token in fyers_tokens — start fyers_feed.py first.")
    return row[0]


def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS nifty_5m_research (
                ts     TIMESTAMPTZ PRIMARY KEY,
                open   DOUBLE PRECISION,
                high   DOUBLE PRECISION,
                low    DOUBLE PRECISION,
                close  DOUBLE PRECISION,
                volume BIGINT
            )
        """)
    conn.commit()


def _fetch_window(token, range_from: str, range_to: str):
    """One history call. Returns list of [ts, o, h, l, c, v] candles."""
    params = {
        "symbol": SYMBOL,
        "resolution": RESOLUTION,
        "date_format": "1",          # range_from/to are YYYY-MM-DD
        "range_from": range_from,
        "range_to": range_to,
        "cont_flag": "1",
    }
    headers = {"Authorization": f"{FYERS_CLIENT_ID}:{token}"}
    r = requests.get(HISTORY_URL, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    d = r.json()
    if d.get("s") != "ok":
        raise RuntimeError(f"Fyers history error [{range_from}->{range_to}]: {d}")
    return d.get("candles", [])


def _store(conn, candles):
    rows = []
    for c in candles:
        # candle = [epoch_seconds, open, high, low, close, volume]
        dt = datetime.fromtimestamp(c[0], IST)
        rows.append((dt, c[1], c[2], c[3], c[4], int(c[5] or 0)))
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO nifty_5m_research (ts, open, high, low, close, volume)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (ts) DO UPDATE SET
                open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                close=EXCLUDED.close, volume=EXCLUDED.volume
        """, rows)
    conn.commit()
    return len(rows)


def run_backfill(total_days: int = TOTAL_DAYS):
    conn = psycopg2.connect(DB_URL)
    try:
        token = _get_token(conn)
        _ensure_table(conn)

        end = date.today()
        start_target = end - timedelta(days=total_days)
        windows = []
        cursor = end
        while cursor > start_target:
            w_from = max(cursor - timedelta(days=WINDOW_DAYS), start_target)
            windows.append((w_from, cursor))
            cursor = w_from - timedelta(days=1)

        total_stored = 0
        results = []
        for w_from, w_to in windows:
            try:
                candles = _fetch_window(token, w_from.isoformat(), w_to.isoformat())
                n = _store(conn, candles)
                total_stored += n
                results.append({"from": w_from.isoformat(), "to": w_to.isoformat(),
                                "candles": len(candles), "stored": n})
                log.info(f"window {w_from}->{w_to}: {n} bars")
            except Exception as e:
                results.append({"from": w_from.isoformat(), "to": w_to.isoformat(),
                                "error": str(e)})
                log.warning(f"window {w_from}->{w_to} failed: {e}")
            time.sleep(SLEEP_BETWEEN)

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM nifty_5m_research")
            cnt, mn, mx = cur.fetchone()

        return {
            "status": "ok",
            "symbol": SYMBOL,
            "resolution": "5m",
            "windows": results,
            "rows_touched": total_stored,
            "table_total": cnt,
            "table_min": str(mn),
            "table_max": str(mx),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json
    print(json.dumps(run_backfill(), indent=2, default=str))
