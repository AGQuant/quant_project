"""
Fyers Intraday Feed — Scorr V8
================================
Primary: Fyers API
  - Futures (290):       1-min OHLCV → intraday_prices
  - Non-futures (~1467): 5-min OHLCV → intraday_prices
Fallback: Yahoo Finance (every 15 min, all stocks, 5-min)

Rolling 15-day retention. Auto-cleanup daily at 16:00 IST.

Usage:
  py -3.11 fyers_feed.py --auth-code <code>
"""

import argparse
import hashlib
import os
import time
import logging
from datetime import datetime, timedelta
import pytz
import psycopg2
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

FYERS_CLIENT_ID = '1A4STS8ZGD-100'
FYERS_SECRET    = 'YXTIR2MN9V'
HISTORY_URL     = 'https://api-t1.fyers.in/data/history'

DATABASE_URL = os.environ.get('DATABASE_URL')
IST = pytz.timezone('Asia/Kolkata')

FUTURES_INTERVAL    = 1
EQUITY_INTERVAL     = 5
YAHOO_FALLBACK_MINS = 15
RETENTION_DAYS      = 15

SYMBOL_MAP = {
    'NIFTY':      'NSE:NIFTY50-INDEX',
    'BANKNIFTY':  'NSE:NIFTYBANK-INDEX',
    'FINNIFTY':   'NSE:FINNIFTY-INDEX',
    'MIDCPNIFTY': 'NSE:MIDCPNIFTY-INDEX',
    'SENSEX':     'BSE:SENSEX-INDEX',
    'BANKEX':     'BSE:BANKEX-INDEX',
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fyers_feed')


def make_session():
    s = requests.Session()
    s.mount('https://', HTTPAdapter(max_retries=Retry(total=1, backoff_factor=0.1)))
    return s


SESSION = make_session()


def fyers_symbol(sym: str) -> str:
    return SYMBOL_MAP.get(sym, f'NSE:{sym}-EQ')


def get_fyers_token(auth_code: str) -> str:
    h = hashlib.sha256(f'{FYERS_CLIENT_ID}:{FYERS_SECRET}'.encode()).hexdigest()
    r = SESSION.post(
        'https://api-t1.fyers.in/api/v3/validate-authcode',
        json={'grant_type': 'authorization_code', 'appIdHash': h, 'code': auth_code},
        timeout=10
    )
    d = r.json()
    if d.get('code') != 200:
        raise Exception(f"Auth failed: {d}")
    log.info("✅ Fyers token obtained")
    return d['access_token']


def get_db():
    return psycopg2.connect(DATABASE_URL)


def upsert_candles(conn, rows: list):
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO intraday_prices (symbol, ts, open, high, low, close, volume, timeframe, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, ts, timeframe) DO UPDATE SET
                open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                close=EXCLUDED.close, volume=EXCLUDED.volume, source=EXCLUDED.source
        """, rows)
    conn.commit()


def delete_old_records(conn):
    cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(days=RETENTION_DAYS)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM intraday_prices WHERE ts < %s", (cutoff,))
        deleted = cur.rowcount
    conn.commit()
    log.info(f"🗑  Deleted {deleted} old records")


def get_universe(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe")
        futures = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT symbol FROM cmp_prices")
        all_stocks = {r[0] for r in cur.fetchall()}
    equity = all_stocks - futures
    log.info(f"Universe: {len(futures)} futures, {len(equity)} equity")
    return sorted(futures), sorted(equity)


def fetch_fyers_history(token: str, sym: str, resolution: str) -> list:
    now = datetime.now(IST)
    range_from = (now - timedelta(days=15)).strftime('%Y-%m-%d')
    range_to   = now.strftime('%Y-%m-%d')
    r = SESSION.get(HISTORY_URL,
        params={
            'symbol':      fyers_symbol(sym),
            'resolution':  resolution,
            'date_format': '1',
            'range_from':  range_from,
            'range_to':    range_to,
            'cont_flag':   '1',
        },
        headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'},
        timeout=(3, 5)  # connect=3s, read=5s
    )
    d = r.json()
    if 'candles' not in d:
        log.warning(f"  {sym}: {d.get('message', str(d)[:60])}")
        return []
    return d.get('candles', [])


def fetch_batch(token: str, symbols: list, resolution: str, timeframe: str, conn) -> int:
    rows = []
    errors = 0
    total = len(symbols)
    for i, sym in enumerate(symbols):
        if i % 50 == 0:
            log.info(f"  Progress: {i}/{total}")
        try:
            candles = fetch_fyers_history(token, sym, resolution)
            for c in candles:
                ts = datetime.fromtimestamp(c[0], tz=IST).replace(tzinfo=None)
                rows.append((sym, ts, c[1], c[2], c[3], c[4], int(c[5]), timeframe, 'fyers'))
            time.sleep(0.05)
        except Exception as e:
            errors += 1
            log.warning(f"  Error {sym}: {e}")

    if rows:
        upsert_candles(conn, rows)
    log.info(f"  ✅ {timeframe} upserted {len(rows)} candles ({errors} errors)")
    return len(rows)


def fetch_yahoo_fallback(symbols: list, conn) -> int:
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed")
        return 0

    rows = []
    log.info(f"📡 Yahoo fallback: {len(symbols)} symbols")
    for sym in symbols:
        try:
            df = yf.Ticker(f"{sym}.NS").history(period='15d', interval='5m')
            if df.empty:
                continue
            for ts, row in df.iterrows():
                ts_naive = ts.to_pydatetime().replace(tzinfo=None)
                rows.append((sym, ts_naive, float(row['Open']), float(row['High']),
                             float(row['Low']), float(row['Close']), int(row['Volume']), '5m', 'yahoo'))
            time.sleep(0.05)
        except Exception as e:
            log.warning(f"  Yahoo {sym}: {e}")

    if rows:
        upsert_candles(conn, rows)
        log.info(f"  ✅ Yahoo upserted {len(rows)} candles")
    return len(rows)


def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return (9*60+15) <= mins <= (15*60+30)


def run(auth_code: str):
    token = get_fyers_token(auth_code)
    conn  = get_db()
    futures, equity = get_universe(conn)

    last_futures = None
    last_equity  = None
    last_yahoo   = None
    last_cleanup = None
    fyers_ok     = True

    log.info("🚀 Feed running. Ctrl+C to stop.")

    while True:
        now = datetime.now(IST)

        if now.hour == 16 and now.minute == 0 and last_cleanup != now.date():
            delete_old_records(conn)
            last_cleanup = now.date()

        if not is_market_open():
            time.sleep(60)
            continue

        if fyers_ok and (last_futures is None or (now - last_futures).seconds >= FUTURES_INTERVAL * 60):
            try:
                log.info(f"📈 Futures 1-min ({len(futures)} symbols)")
                fetch_batch(token, futures, '1', '1m', conn)
                last_futures = now
            except Exception as e:
                log.error(f"Futures failed: {e}")
                fyers_ok = False

        if fyers_ok and (last_equity is None or (now - last_equity).seconds >= EQUITY_INTERVAL * 60):
            try:
                log.info(f"📊 Equity 5-min ({len(equity)} symbols)")
                fetch_batch(token, equity, '5', '5m', conn)
                last_equity = now
            except Exception as e:
                log.error(f"Equity failed: {e}")
                fyers_ok = False

        if not fyers_ok and (last_yahoo is None or (now - last_yahoo).seconds >= YAHOO_FALLBACK_MINS * 60):
            fetch_yahoo_fallback(futures + equity, conn)
            last_yahoo = now
            fyers_ok = True

        time.sleep(30)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--auth-code', type=str)
    args = parser.parse_args()

    auth_code = args.auth_code
    if not auth_code:
        print(f"\nOpen:\nhttps://api-t1.fyers.in/api/v3/generate-authcode?client_id={FYERS_CLIENT_ID}&redirect_uri=http%3A%2F%2F127.0.0.1&response_type=code&state=None\n")
        auth_code = input("Paste auth_code: ").strip()

    run(auth_code)
