"""
fetch_nifty_5m_research.py  —  STANDALONE research fetcher (V10 prep)

ISOLATED. Not wired to main.py, no scheduler, no _BG_TASKS.
Run on-demand only:  py -3.11 fetch_nifty_5m_research.py

- Source : Yahoo chart API (^NSEI), one-at-a-time (no yfinance batch)
- Target : nifty_5m_research  (own table, touches nothing else)
- 60-day 5-min OHLC, IST. Idempotent upsert on ts.
"""
import os, requests, psycopg2
from datetime import datetime, timezone, timedelta

DB_URL = os.environ["DATABASE_URL"]
IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "^NSEI"
URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/"
    f"{SYMBOL}?range=60d&interval=5m"
)
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch():
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(ts):
        o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None in (o, h, l, c):
            continue
        dt = datetime.fromtimestamp(t, IST)
        rows.append((dt, o, h, l, c, v or 0))
    return rows


def store(rows):
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
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
    cur.executemany("""
        INSERT INTO nifty_5m_research (ts, open, high, low, close, volume)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (ts) DO UPDATE SET
            open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
            close=EXCLUDED.close, volume=EXCLUDED.volume
    """, rows)
    conn.commit()
    cur.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM nifty_5m_research")
    print("stored:", cur.fetchone())
    cur.close(); conn.close()


if __name__ == "__main__":
    rows = fetch()
    print(f"fetched {len(rows)} bars")
    store(rows)
