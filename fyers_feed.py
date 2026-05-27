"""
Fyers Intraday Feed — Scorr V8
================================
Primary: Fyers API
  - Futures (290):     1-min OHLCV
  - Non-futures (~1467): 5-min OHLCV
Fallback: Yahoo Finance (every 15 min, all 1757 stocks, 5-min)

Runs continuously during market hours (9:15–15:30 IST).
Stores to Railway intraday_prices table.
Auto-deletes records older than 15 days daily at 16:00.

Usage:
  py -3.11 fyers_feed.py --auth-code <code>
  py -3.11 fyers_feed.py  (will prompt for auth-code)
"""

import argparse
import hashlib
import os
import sys
import time
import logging
from datetime import datetime, timedelta
import pytz
import psycopg2
import requests

# ── CONFIG ────────────────────────────────────────────────────────────
FYERS_CLIENT_ID = '1A4STS8ZGD-100'
FYERS_SECRET    = 'YXTIR2MN9V'
FYERS_REDIRECT  = 'http://127.0.0.1'

DATABASE_URL = os.environ.get('DATABASE_URL')  # set in env or Railway

IST = pytz.timezone('Asia/Kolkata')
MARKET_OPEN  = (9, 15)
MARKET_CLOSE = (15, 30)

FUTURES_INTERVAL    = 1    # minutes
EQUITY_INTERVAL     = 5    # minutes
YAHOO_FALLBACK_MINS = 15   # minutes
RETENTION_DAYS      = 15

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fyers_feed')


# ── AUTH ──────────────────────────────────────────────────────────────

def get_fyers_token(auth_code: str) -> str:
    h = hashlib.sha256(f'{FYERS_CLIENT_ID}:{FYERS_SECRET}'.encode()).hexdigest()
    r = requests.post(
        'https://api-t1.fyers.in/api/v3/validate-authcode',
        json={'grant_type': 'authorization_code', 'appIdHash': h, 'code': auth_code},
        timeout=10
    )
    d = r.json()
    if d.get('code') != 200:
        raise Exception(f"Fyers auth failed: {d}")
    log.info("✅ Fyers token obtained")
    return d['access_token']


def fyers_headers(token: str) -> dict:
    return {'Authorization': f'{FYERS_CLIENT_ID}:{token}'}


# ── DB ────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DATABASE_URL)


def upsert_candles(conn, rows: list):
    """rows = list of (symbol, ts, open, high, low, close, volume, timeframe, source)"""
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
    log.info(f"  Upserted {len(rows)} candles")


def delete_old_records(conn):
    cutoff = datetime.now(IST) - timedelta(days=RETENTION_DAYS)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM intraday_prices WHERE ts < %s", (cutoff,))
        deleted = cur.rowcount
    conn.commit()
    log.info(f"🗑  Deleted {deleted} records older than {RETENTION_DAYS} days")


# ── UNIVERSE ──────────────────────────────────────────────────────────

def get_universe(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe")
        futures = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT symbol FROM cmp_prices")
        all_stocks = {r[0] for r in cur.fetchall()}
    equity = all_stocks - futures
    log.info(f"Universe: {len(futures)} futures, {len(equity)} equity")
    return futures, equity


# ── FYERS FETCH ───────────────────────────────────────────────────────

def fyers_history(token: str, symbol: str, resolution: str, date_format: int = 1) -> list:
    """Fetch intraday candles from Fyers history API."""
    now = datetime.now(IST)
    range_from = int((now - timedelta(days=15)).timestamp())
    range_to   = int(now.timestamp())

    r = requests.get(
        'https://api-t1.fyers.in/api/v3/history',
        params={
            'symbol':      f'NSE:{symbol}-EQ',
            'resolution':  resolution,
            'date_format': date_format,
            'range_from':  range_from,
            'range_to':    range_to,
            'cont_flag':   1,
        },
        headers=fyers_headers(token),
        timeout=10
    )
    d = r.json()
    if d.get('s') != 'ok':
        return []
    candles = d.get('candles', [])
    return candles  # [timestamp, open, high, low, close, volume]


def fetch_fyers_batch(token: str, symbols: list, resolution: str, timeframe: str, conn) -> int:
    rows = []
    for sym in symbols:
        try:
            candles = fyers_history(token, sym, resolution)
            for c in candles:
                ts = datetime.fromtimestamp(c[0], tz=IST).replace(tzinfo=None)
                rows.append((sym, ts, c[1], c[2], c[3], c[4], c[5], timeframe, 'fyers'))
            time.sleep(0.1)  # rate limit
        except Exception as e:
            log.warning(f"Fyers error {sym}: {e}")

    if rows:
        upsert_candles(conn, rows)
    return len(rows)


# ── YAHOO FALLBACK ────────────────────────────────────────────────────

def fetch_yahoo_fallback(symbols: list, conn) -> int:
    """Fetch 5-min data from Yahoo for all symbols."""
    import yfinance as yf
    rows = []
    log.info(f"📡 Yahoo fallback: fetching {len(symbols)} symbols")

    for sym in symbols:
        try:
            ticker = yf.Ticker(f"{sym}.NS")
            df = ticker.history(period='15d', interval='5m')
            if df.empty:
                continue
            for ts, row in df.iterrows():
                ts_naive = ts.to_pydatetime().replace(tzinfo=None)
                rows.append((sym, ts_naive, row['Open'], row['High'], row['Low'],
                             row['Close'], int(row['Volume']), '5m', 'yahoo'))
            time.sleep(0.05)
        except Exception as e:
            log.warning(f"Yahoo error {sym}: {e}")

    if rows:
        upsert_candles(conn, rows)
    return len(rows)


# ── CMP UPDATE ────────────────────────────────────────────────────────

def update_cmp_fyers(token: str, symbols: list, conn):
    """Update cmp_prices from Fyers quotes (batch of 50)."""
    batch_size = 50
    rows = []
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i+batch_size]
        sym_str = ','.join([f'NSE:{s}-EQ' for s in batch])
        try:
            r = requests.get(
                'https://api-t1.fyers.in/api/v3/quotes',
                params={'symbols': sym_str},
                headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'},
                timeout=10
            )
            d = r.json()
            if d.get('s') == 'ok':
                for item in d.get('d', []):
                    v = item.get('v', {})
                    sym = item['n'].replace('NSE:', '').replace('-EQ', '')
                    lp  = v.get('lp', 0)
                    if lp:
                        rows.append((sym, lp))
            time.sleep(0.1)
        except Exception as e:
            log.warning(f"CMP batch error: {e}")

    if rows:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO cmp_prices (symbol, cmp, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (symbol) DO UPDATE SET cmp=EXCLUDED.cmp, updated_at=NOW()
            """, rows)
        conn.commit()
        log.info(f"  CMP updated: {len(rows)} stocks")


# ── SCHEDULER ─────────────────────────────────────────────────────────

def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    mins = h * 60 + m
    return (MARKET_OPEN[0] * 60 + MARKET_OPEN[1]) <= mins <= (MARKET_CLOSE[0] * 60 + MARKET_CLOSE[1])


def run(auth_code: str):
    token = get_fyers_token(auth_code)
    conn  = get_db()

    futures, equity = get_universe(conn)
    futures_list = sorted(futures)
    equity_list  = sorted(equity)

    last_futures_fetch  = None
    last_equity_fetch   = None
    last_yahoo_fallback = None
    last_cleanup        = None
    fyers_ok            = True

    log.info("🚀 Fyers feed started")

    while True:
        now = datetime.now(IST)

        # Daily cleanup at 16:00
        if now.hour == 16 and now.minute == 0:
            if last_cleanup != now.date():
                delete_old_records(conn)
                last_cleanup = now.date()

        if not is_market_open():
            time.sleep(30)
            continue

        # ── Futures: every 1 min ──────────────────────────────────────
        if last_futures_fetch is None or (now - last_futures_fetch).seconds >= FUTURES_INTERVAL * 60:
            if fyers_ok:
                try:
                    log.info(f"📈 Futures 1-min fetch ({len(futures_list)} symbols)")
                    n = fetch_fyers_batch(token, futures_list, '1', '1m', conn)
                    last_futures_fetch = now
                    fyers_ok = True
                except Exception as e:
                    log.error(f"Fyers futures failed: {e}")
                    fyers_ok = False

        # ── Equity: every 5 min ───────────────────────────────────────
        if last_equity_fetch is None or (now - last_equity_fetch).seconds >= EQUITY_INTERVAL * 60:
            if fyers_ok:
                try:
                    log.info(f"📊 Equity 5-min fetch ({len(equity_list)} symbols)")
                    n = fetch_fyers_batch(token, equity_list, '5', '5m', conn)
                    last_equity_fetch = now
                    fyers_ok = True
                except Exception as e:
                    log.error(f"Fyers equity failed: {e}")
                    fyers_ok = False

        # ── Yahoo fallback: every 15 min if Fyers down ────────────────
        if not fyers_ok:
            if last_yahoo_fallback is None or (now - last_yahoo_fallback).seconds >= YAHOO_FALLBACK_MINS * 60:
                log.warning("⚠️ Fyers down — Yahoo fallback")
                all_symbols = futures_list + equity_list
                fetch_yahoo_fallback(all_symbols, conn)
                last_yahoo_fallback = now
                fyers_ok = True  # retry Fyers next cycle

        time.sleep(30)  # check every 30 seconds


# ── MAIN ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Add unique constraint if not exists
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'uq_intraday_symbol_ts_tf'
                    ) THEN
                        ALTER TABLE intraday_prices
                        ADD CONSTRAINT uq_intraday_symbol_ts_tf UNIQUE (symbol, ts, timeframe);
                    END IF;
                END $$;
            """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Schema setup warning: {e}")

    parser = argparse.ArgumentParser()
    parser.add_argument('--auth-code', type=str, help='Fyers auth code from redirect URL')
    args = parser.parse_args()

    auth_code = args.auth_code
    if not auth_code:
        print("\nOpen this URL in browser, login, paste the auth_code from redirect:")
        print(f"https://api-t1.fyers.in/api/v3/generate-authcode?client_id={FYERS_CLIENT_ID}&redirect_uri=http%3A%2F%2F127.0.0.1&response_type=code&state=None\n")
        auth_code = input("Auth code: ").strip()

    run(auth_code)
