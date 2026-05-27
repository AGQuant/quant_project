"""
Fyers Intraday Feed — Scorr V8
================================
Primary: Fyers API
  - Futures (208):      1-min OHLCV -> intraday_prices
  - Equity (~1507):     5-min OHLCV -> intraday_prices
  - Indices (NIFTY, BANKNIFTY, INDIAVIX): LTP every 1-min -> cmp_prices
Fallback: Yahoo Finance (every 15 min, 5-min) - CMP only, no intraday storage

Retention: 7 days rolling.

Usage:
  py -3.11 fyers_feed.py --auth-code <code>
"""

import argparse, hashlib, os, time, logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytz, psycopg2, requests

FYERS_CLIENT_ID = '1A4STS8ZGD-100'
FYERS_SECRET    = 'YXTIR2MN9V'
HISTORY_URL     = 'https://api-t1.fyers.in/data/history'
QUOTES_URL      = 'https://api-t1.fyers.in/data/quotes'
DATABASE_URL    = os.environ.get('DATABASE_URL')
IST             = pytz.timezone('Asia/Kolkata')

FUTURES_INTERVAL    = 1
EQUITY_INTERVAL     = 5
INDEX_INTERVAL      = 1
RETENTION_DAYS      = 7
WORKERS             = 10

SKIP_SYMBOLS = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'}

INDEX_LTP_SYMBOLS = {
    'NIFTY':     'NSE:NIFTY50-INDEX',
    'BANKNIFTY': 'NSE:NIFTYBANK-INDEX',
    'INDIAVIX':  'NSE:INDIAVIX-INDEX',
}

SPECIAL_SYMBOLS = {
    'M&M': 'NSE:M%26M-EQ',
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fyers_feed')


def get_fyers_token(auth_code):
    h = hashlib.sha256(f'{FYERS_CLIENT_ID}:{FYERS_SECRET}'.encode()).hexdigest()
    r = requests.post('https://api-t1.fyers.in/api/v3/validate-authcode',
        json={'grant_type':'authorization_code','appIdHash':h,'code':auth_code}, timeout=10)
    d = r.json()
    if d.get('code') != 200:
        raise Exception(f"Auth failed: {d}")
    log.info("Fyers token obtained")
    return d['access_token']

def hdr(token): return {'Authorization': f'{FYERS_CLIENT_ID}:{token}'}
def get_db(): return psycopg2.connect(DATABASE_URL)

def fyers_eq_symbol(sym):
    return SPECIAL_SYMBOLS.get(sym, f'NSE:{sym}-EQ')

def upsert_candles(conn, rows):
    if not rows: return
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO intraday_prices (symbol,ts,open,high,low,close,volume,timeframe,source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol,ts,timeframe) DO UPDATE SET
                open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                close=EXCLUDED.close,volume=EXCLUDED.volume,source=EXCLUDED.source
        """, rows)
    conn.commit()

def update_index_ltp(conn, token):
    sym_str = ','.join(INDEX_LTP_SYMBOLS.values())
    try:
        r = requests.get(QUOTES_URL, params={'symbols': sym_str}, headers=hdr(token), timeout=5)
        d = r.json()
        if d.get('s') != 'ok': return
        rows = []
        for item in d.get('d', []):
            lp = item['v'].get('lp', 0)
            if not lp: continue
            fyers_sym = item['n']
            for name, fsym in INDEX_LTP_SYMBOLS.items():
                if fsym == fyers_sym:
                    rows.append((name, lp))
        if rows:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO cmp_prices (symbol, cmp, updated_at) VALUES (%s, %s, NOW())
                    ON CONFLICT (symbol) DO UPDATE SET cmp=EXCLUDED.cmp, updated_at=NOW()
                """, rows)
            conn.commit()
            log.info(f"  Index LTP: {rows}")
    except Exception as e:
        log.warning(f"Index LTP failed: {e}")

def delete_old_records(conn):
    cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(days=RETENTION_DAYS)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM intraday_prices WHERE ts < %s", (cutoff,))
        n = cur.rowcount
    conn.commit()
    log.info(f"Deleted {n} old records (>{RETENTION_DAYS} days)")

def get_universe(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe")
        futures = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT symbol FROM cmp_prices")
        all_stocks = {r[0] for r in cur.fetchall()}
    equity = all_stocks - futures
    futures_clean = sorted(futures - SKIP_SYMBOLS)
    equity_clean  = sorted(equity - SKIP_SYMBOLS)
    log.info(f"Universe: {len(futures_clean)} futures, {len(equity_clean)} equity")
    return futures_clean, equity_clean

def fetch_one(token, sym, resolution, timeframe):
    now = datetime.now(IST)
    r = requests.get(HISTORY_URL, params={
        'symbol':      fyers_eq_symbol(sym),
        'resolution':  resolution,
        'date_format': '1',
        'range_from':  (now - timedelta(days=RETENTION_DAYS)).strftime('%Y-%m-%d'),
        'range_to':    now.strftime('%Y-%m-%d'),
        'cont_flag':   '1',
    }, headers=hdr(token), timeout=5)
    d = r.json()
    if 'candles' not in d: return []
    rows = []
    for c in d['candles']:
        ts = datetime.fromtimestamp(c[0], tz=IST).replace(tzinfo=None)
        rows.append((sym, ts, c[1], c[2], c[3], c[4], int(c[5]), timeframe, 'fyers'))
    return rows

def fetch_batch(token, symbols, resolution, timeframe, conn):
    all_rows = []
    errors   = 0
    done     = 0
    total    = len(symbols)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures_map = {ex.submit(fetch_one, token, sym, resolution, timeframe): sym for sym in symbols}
        for future in as_completed(futures_map, timeout=300):
            sym = futures_map[future]
            try:
                rows = future.result(timeout=6)
                all_rows.extend(rows)
                done += 1
                if done % 50 == 0:
                    log.info(f"  {done}/{total} done")
                    if all_rows:
                        upsert_candles(conn, all_rows)
                        all_rows = []
            except Exception as e:
                errors += 1
                log.warning(f"  {sym}: {e}")
    if all_rows:
        upsert_candles(conn, all_rows)
    log.info(f"  {timeframe} complete ({total} symbols, {errors} errors)")

def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5: return False
    mins = now.hour * 60 + now.minute
    return (9*60+15) <= mins <= (15*60+30)

def run(auth_code):
    token = get_fyers_token(auth_code)
    conn  = get_db()
    futures, equity = get_universe(conn)
    last_futures = last_equity = last_index = last_cleanup = None
    log.info("Feed running. Ctrl+C to stop.")

    while True:
        now = datetime.now(IST)
        if now.hour == 16 and now.minute == 0 and last_cleanup != now.date():
            delete_old_records(conn); last_cleanup = now.date()
        if not is_market_open():
            time.sleep(60); continue

        if last_index is None or (now - last_index).seconds >= INDEX_INTERVAL * 60:
            update_index_ltp(conn, token); last_index = now

        if last_futures is None or (now - last_futures).seconds >= FUTURES_INTERVAL * 60:
            try:
                log.info(f"Futures 1-min ({len(futures)} symbols)")
                fetch_batch(token, futures, '1', '1m', conn)
                last_futures = now
            except Exception as e:
                log.error(f"Futures failed: {e}")

        if last_equity is None or (now - last_equity).seconds >= EQUITY_INTERVAL * 60:
            try:
                log.info(f"Equity 5-min ({len(equity)} symbols)")
                fetch_batch(token, equity, '5', '5m', conn)
                last_equity = now
            except Exception as e:
                log.error(f"Equity failed: {e}")

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
