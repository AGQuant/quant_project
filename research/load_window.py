"""
load_window.py — load ONE date-window of 5m candles into nifty_5m_research.

Robust, single-shot, prints clearly. No multiline shell needed.

Usage:
  python research/load_window.py NSE:NIFTY50-INDEX 2026-02-26 2026-06-06

Reads DATABASE_URL + Fyers token from env / fyers_tokens table.
"""
import os
import sys
import requests
import psycopg2
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "1A4STS8ZGD-100")


def main():
    if len(sys.argv) != 4:
        print("usage: python research/load_window.py <SYMBOL> <FROM yyyy-mm-dd> <TO yyyy-mm-dd>")
        sys.exit(1)
    symbol, rfrom, rto = sys.argv[1], sys.argv[2], sys.argv[3]

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("CREATE TABLE IF NOT EXISTS nifty_5m_research ("
                "ts TIMESTAMPTZ PRIMARY KEY, open DOUBLE PRECISION, high DOUBLE PRECISION,"
                "low DOUBLE PRECISION, close DOUBLE PRECISION, volume BIGINT)")
    conn.commit()

    cur.execute("SELECT access_token FROM fyers_tokens WHERE id=1")
    tok = cur.fetchone()[0]

    print(f"fetching {symbol} {rfrom} -> {rto} ...", flush=True)
    r = requests.get("https://api-t1.fyers.in/data/history",
                     params={"symbol": symbol, "resolution": "5", "date_format": "1",
                             "range_from": rfrom, "range_to": rto, "cont_flag": "1"},
                     headers={"Authorization": f"{CLIENT_ID}:{tok}"}, timeout=60)
    r.raise_for_status()
    cs = r.json().get("candles", [])
    print(f"got {len(cs)} candles", flush=True)

    rows = [(datetime.fromtimestamp(x[0], IST), x[1], x[2], x[3], x[4], int(x[5] or 0)) for x in cs]
    cur.executemany("INSERT INTO nifty_5m_research (ts,open,high,low,close,volume) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (ts) DO NOTHING", rows)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM nifty_5m_research")
    print(f"committed. DB rows now: {cur.fetchone()[0]}", flush=True)
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
