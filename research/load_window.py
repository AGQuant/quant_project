"""
load_window.py — load 5m candles into a research table.

Two modes:
  ONE WINDOW:
    python research/load_window.py NSE:NIFTY50-INDEX 2026-02-26 2026-06-06
  FULL YEAR (auto-loops 100d windows):
    python research/load_window.py NSE:NIFTY50-INDEX year
    python research/load_window.py NSE:NIFTYBANK-INDEX year

Target table is chosen by symbol:
  NSE:NIFTY50-INDEX    -> nifty_5m_research
  NSE:NIFTYBANK-INDEX  -> banknifty_5m_research
"""
import os
import sys
import time
import requests
import psycopg2
from datetime import datetime, timezone, timedelta, date

IST = timezone(timedelta(hours=5, minutes=30))
CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "1A4STS8ZGD-100")
BATCH = 500
WINDOW_DAYS = 100

TABLE_FOR = {
    "NSE:NIFTY50-INDEX": "nifty_5m_research",
    "NSE:NIFTYBANK-INDEX": "banknifty_5m_research",
}


def _conn():
    c = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=15)
    c.autocommit = True
    return c


def _token(cur):
    cur.execute("SELECT access_token FROM fyers_tokens WHERE id=1")
    return cur.fetchone()[0]


def _ensure(cur, table):
    cur.execute(f"CREATE TABLE IF NOT EXISTS {table} ("
                "ts TIMESTAMPTZ PRIMARY KEY, open DOUBLE PRECISION, high DOUBLE PRECISION,"
                "low DOUBLE PRECISION, close DOUBLE PRECISION, volume BIGINT)")


def load_one(cur, table, symbol, tok, rfrom, rto):
    print(f"fetching {symbol} {rfrom} -> {rto} ...", flush=True)
    r = requests.get("https://api-t1.fyers.in/data/history",
                     params={"symbol": symbol, "resolution": "5", "date_format": "1",
                             "range_from": rfrom, "range_to": rto, "cont_flag": "1"},
                     headers={"Authorization": f"{CLIENT_ID}:{tok}"}, timeout=60)
    r.raise_for_status()
    cs = r.json().get("candles", [])
    rows = [(datetime.fromtimestamp(x[0], IST), x[1], x[2], x[3], x[4], int(x[5] or 0)) for x in cs]
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        cur.executemany(f"INSERT INTO {table} (ts,open,high,low,close,volume) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (ts) DO NOTHING", chunk)
    print(f"  window done: {len(rows)} candles", flush=True)
    return len(rows)


def main():
    if len(sys.argv) < 3:
        print("usage: python research/load_window.py <SYMBOL> year | <FROM> <TO>")
        sys.exit(1)
    symbol = sys.argv[1]
    table = TABLE_FOR.get(symbol)
    if not table:
        print(f"unknown symbol {symbol}; known: {list(TABLE_FOR)}")
        sys.exit(1)

    conn = _conn(); cur = conn.cursor()
    _ensure(cur, table)
    tok = _token(cur)

    if sys.argv[2] == "year":
        end = date.today()
        cu = end
        windows = []
        while cu > end - timedelta(days=365):
            f = max(cu - timedelta(days=WINDOW_DAYS), end - timedelta(days=365))
            windows.append((f, cu)); cu = f - timedelta(days=1)
        total = 0
        for f, t in windows:
            total += load_one(cur, table, symbol, tok, f.isoformat(), t.isoformat())
            time.sleep(1)
        print(f"all windows fetched: {total} candles touched", flush=True)
    else:
        rfrom, rto = sys.argv[2], sys.argv[3]
        load_one(cur, table, symbol, tok, rfrom, rto)

    cur.execute(f"SELECT COUNT(*), MIN(ts), MAX(ts) FROM {table}")
    cnt, mn, mx = cur.fetchone()
    print(f"DONE {table}: rows={cnt}  {mn} -> {mx}", flush=True)
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
