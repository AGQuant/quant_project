"""
yahoo_daily_update.py
=====================
Daily delta update — fetches last 5 days of OHLC for all symbols in raw_prices.
Runs at 3:45 PM IST Mon-Fri via Railway scheduler (after market close).

Self-healing: 5-day window catches any gaps from holidays or missed runs.
Safe to re-run: UPSERT on (symbol, price_date) — no duplicates ever.

Usage:
    python yahoo_daily_update.py          # runs update
    python yahoo_daily_update.py --test   # test DB connection only
"""

import sys
import time
import datetime
import yfinance as yf
import psycopg
from psycopg.rows import dict_row

# ============================================================
# CONFIG — uses Railway internal URL when deployed on Railway
# For local test: swap to public proxy URL
# ============================================================
import os
DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:nvKhlXtKniyClYvpVVjQeYeAypwsbCeE@kodama.proxy.rlwy.net:49570/railway?sslmode=require"
)

LOOKBACK_DAYS = 7    # Fetch last 7 days — catches weekends + 1 holiday buffer
RETRY_DELAY   = 5    # Seconds before retry
BATCH_SIZE    = 50   # Symbols per batch
SLEEP_BETWEEN = 1    # Seconds between batches

# Index mapping
INDICES = {
    "^NSEI":    "NIFTY50",
    "^NSEBANK": "BANKNIFTY",
}

UPSERT_SQL = """
INSERT INTO raw_prices
    (symbol, price_date, open, high, low, close, adjusted_close, volume)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, price_date) DO UPDATE SET
    open           = EXCLUDED.open,
    high           = EXCLUDED.high,
    low            = EXCLUDED.low,
    close          = EXCLUDED.close,
    adjusted_close = EXCLUDED.adjusted_close,
    volume         = EXCLUDED.volume;
"""


def to_yahoo_ticker(db_symbol):
    reverse_index = {v: k for k, v in INDICES.items()}
    if db_symbol in reverse_index:
        return reverse_index[db_symbol]
    return db_symbol + ".NS"


def fetch_recent(db_symbol, retry=True):
    """Fetch last LOOKBACK_DAYS of OHLC for one symbol."""
    yahoo_ticker = to_yahoo_ticker(db_symbol)
    end   = datetime.date.today() + datetime.timedelta(days=1)  # include today
    start = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)

    try:
        ticker = yf.Ticker(yahoo_ticker)
        df = ticker.history(
            start=str(start),
            end=str(end),
            interval="1d",
            auto_adjust=False
        )

        if df is None or df.empty:
            raise ValueError("Empty dataframe")

        rows = []
        for date, row in df.iterrows():
            rows.append((
                db_symbol,
                date.date(),
                round(float(row["Open"]),     2) if row["Open"]      == row["Open"]      else None,
                round(float(row["High"]),     2) if row["High"]      == row["High"]      else None,
                round(float(row["Low"]),      2) if row["Low"]       == row["Low"]       else None,
                round(float(row["Close"]),    2) if row["Close"]     == row["Close"]     else None,
                round(float(row["Adj Close"]),2) if row["Adj Close"] == row["Adj Close"] else None,
                int(row["Volume"])               if row["Volume"]    == row["Volume"]    else 0,
            ))
        return rows

    except Exception as e:
        if retry:
            time.sleep(RETRY_DELAY)
            return fetch_recent(db_symbol, retry=False)
        else:
            return None


def main():
    # --test mode: just verify DB connection
    if "--test" in sys.argv:
        print("Testing DB connection...")
        conn = psycopg.connect(DB_URL)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM raw_prices;")
            count = cur.fetchone()[0]
        conn.close()
        print(f"OK — raw_prices has {count:,} rows")
        return

    start_time = time.time()
    run_date = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')
    print(f"[DAILY UPDATE] {run_date}")

    conn = psycopg.connect(DB_URL)

    # Pull distinct symbols from raw_prices (already loaded = our universe)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT DISTINCT symbol FROM raw_prices ORDER BY symbol;")
        all_symbols = [row["symbol"] for row in cur.fetchall()]

    total = len(all_symbols)
    print(f"[INFO] Updating {total} symbols — last {LOOKBACK_DAYS} days")

    failed = []
    updated_rows = 0

    for i in range(0, total, BATCH_SIZE):
        batch = all_symbols[i: i + BATCH_SIZE]
        for db_symbol in batch:
            rows = fetch_recent(db_symbol)
            if rows is None:
                failed.append(db_symbol)
                continue
            with conn.cursor() as cur:
                cur.executemany(UPSERT_SQL, rows)
            conn.commit()
            updated_rows += len(rows)
        time.sleep(SLEEP_BETWEEN)

    elapsed = (time.time() - start_time) / 60
    print(f"[DONE] {updated_rows:,} rows upserted | {len(failed)} failed | {elapsed:.1f} min")

    if failed:
        print(f"[FAILED] {', '.join(failed[:10])}{'...' if len(failed) > 10 else ''}")

    conn.close()


if __name__ == "__main__":
    main()