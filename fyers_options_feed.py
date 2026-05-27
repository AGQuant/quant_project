"""
Fyers Options Feed — Scorr V8
================================
Fetches 1-min OHLCV + OI for NIFTY and BANKNIFTY options
Monthly expiry, ATM +/- 10 strikes.

NIFTY strike interval:     50 pts  -> ATM +/- 500
BANKNIFTY strike interval: 100 pts -> ATM +/- 1000

Retention: 7 days rolling.

Usage:
  py -3.11 fyers_options_feed.py --auth-code <code>
"""

import argparse, hashlib, os, time, logging, calendar
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytz, psycopg2, requests

FYERS_CLIENT_ID = '1A4STS8ZGD-100'
FYERS_SECRET    = 'YXTIR2MN9V'
HISTORY_URL     = 'https://api-t1.fyers.in/data/history'
QUOTES_URL      = 'https://api-t1.fyers.in/data/quotes'
DATABASE_URL    = os.environ.get('DATABASE_URL')
IST             = pytz.timezone('Asia/Kolkata')
FETCH_INTERVAL  = 1
RETENTION_DAYS  = 7
WORKERS         = 10
ATM_RANGE       = 10
MONTHS = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC']

INDICES = {
    'NIFTY':     {'fyers_sym': 'NSE:NIFTY50-INDEX',  'interval': 50,  'prefix': 'NIFTY'},
    'BANKNIFTY': {'fyers_sym': 'NSE:NIFTYBANK-INDEX', 'interval': 100, 'prefix': 'BANKNIFTY'},
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('options_feed')


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

def setup_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS options_prices (
                id SERIAL, symbol TEXT NOT NULL, underlying TEXT NOT NULL,
                expiry TEXT NOT NULL, strike NUMERIC NOT NULL, option_type TEXT NOT NULL,
                ts TIMESTAMP NOT NULL, open NUMERIC, high NUMERIC, low NUMERIC,
                close NUMERIC, volume BIGINT, oi BIGINT,
                timeframe TEXT DEFAULT '1m', source TEXT DEFAULT 'fyers'
            )""")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_opts_sym_ts ON options_prices(symbol, ts, timeframe)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_opts_ul_ts ON options_prices(underlying, ts DESC)")
    conn.commit()
    log.info("options_prices table ready")

def upsert(conn, rows):
    if not rows: return
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO options_prices
                (symbol,underlying,expiry,strike,option_type,ts,open,high,low,close,volume,oi,timeframe,source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol,ts,timeframe) DO UPDATE SET
                open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                close=EXCLUDED.close,volume=EXCLUDED.volume,oi=EXCLUDED.oi
        """, rows)
    conn.commit()

def delete_old(conn):
    cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(days=RETENTION_DAYS)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM options_prices WHERE ts < %s", (cutoff,))
        n = cur.rowcount
    conn.commit()
    log.info(f"Deleted {n} old option records (>{RETENTION_DAYS} days)")

def get_ltp(token, fyers_sym):
    r = requests.get(QUOTES_URL, params={'symbols': fyers_sym}, headers=hdr(token), timeout=5)
    d = r.json()
    if d.get('s') == 'ok' and d.get('d'):
        return float(d['d'][0]['v']['lp'])
    raise Exception(f"LTP failed: {d}")

def get_monthly_expiry():
    now = datetime.now(IST)
    year2 = str(now.year)[2:]
    def last_thursday(year, month):
        last_day = calendar.monthrange(year, month)[1]
        d = datetime(year, month, last_day)
        while d.weekday() != 3:
            d -= timedelta(days=1)
        return d
    exp = last_thursday(now.year, now.month)
    if now.date() > exp.date():
        if now.month == 12:
            return f"{str(now.year+1)[2:]}JAN"
        return f"{year2}{MONTHS[now.month]}"
    return f"{year2}{MONTHS[now.month-1]}"

def build_symbols(ltp, interval, prefix):
    expiry = get_monthly_expiry()
    atm = round(ltp / interval) * interval
    syms = []
    for i in range(-ATM_RANGE, ATM_RANGE + 1):
        strike = atm + i * interval
        for ot in ['CE','PE']:
            syms.append((f"NSE:{prefix}{expiry}{int(strike)}{ot}", strike, ot, expiry))
    return syms

def fetch_one_option(token, sym, strike, ot, expiry, underlying):
    now = datetime.now(IST)
    r = requests.get(HISTORY_URL, params={
        'symbol': sym, 'resolution': '1', 'date_format': '1',
        'range_from': (now - timedelta(days=RETENTION_DAYS)).strftime('%Y-%m-%d'),
        'range_to':   now.strftime('%Y-%m-%d'),
        'cont_flag': '1', 'oi_flag': '1',
    }, headers=hdr(token), timeout=5)
    d = r.json()
    if 'candles' not in d: return []
    rows = []
    for c in d['candles']:
        ts = datetime.fromtimestamp(c[0], tz=IST).replace(tzinfo=None)
        oi = int(c[6]) if len(c) > 6 else 0
        rows.append((sym, underlying, expiry, strike, ot, ts,
                     c[1], c[2], c[3], c[4], int(c[5]), oi, '1m', 'fyers'))
    return rows

def fetch_index_options(token, name, cfg, conn):
    ltp    = get_ltp(token, cfg['fyers_sym'])
    expiry = get_monthly_expiry()
    syms   = build_symbols(ltp, cfg['interval'], cfg['prefix'])
    atm    = round(ltp / cfg['interval']) * cfg['interval']
    log.info(f"  {name} LTP={ltp:.0f} ATM={atm} expiry={expiry} contracts={len(syms)}")
    all_rows = []
    errors   = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(fetch_one_option, token, sym, strike, ot, expiry, name): sym
                   for sym, strike, ot, expiry in syms}
        for future in as_completed(futures, timeout=300):
            try: all_rows.extend(future.result(timeout=6))
            except Exception: errors += 1
    if all_rows:
        upsert(conn, all_rows)
    log.info(f"  {name}: {len(all_rows)} candles stored ({errors} errors)")

def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5: return False
    mins = now.hour * 60 + now.minute
    return (9*60+15) <= mins <= (15*60+30)

def run(auth_code):
    token = get_fyers_token(auth_code)
    conn  = get_db()
    setup_table(conn)
    last_fetch = last_cleanup = None
    log.info(f"Options feed running. Active expiry: {get_monthly_expiry()}")

    while True:
        now = datetime.now(IST)
        if now.hour == 16 and now.minute == 0 and last_cleanup != now.date():
            delete_old(conn); last_cleanup = now.date()
        if not is_market_open():
            time.sleep(60); continue
        if last_fetch is None or (now - last_fetch).seconds >= FETCH_INTERVAL * 60:
            log.info("Fetching NIFTY + BANKNIFTY options...")
            for name, cfg in INDICES.items():
                try: fetch_index_options(token, name, cfg, conn)
                except Exception as e: log.error(f"{name}: {e}")
            last_fetch = now
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
