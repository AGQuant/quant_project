"""
Fyers Intraday Feed - Scorr V8
================================
Primary live feed. Runs as a standalone Railway WORKER (not in FastAPI).

  - Futures (210):    1-min  OHLCV -> intraday_prices
  - Equity (~1507):   15-min OHLCV -> intraday_prices
  - Indices (NIFTY, BANKNIFTY, INDIAVIX): LTP every 1-min -> cmp_prices

Retention: 7 days rolling. Yahoo DROPPED for intraday (chart-API only used elsewhere for EOD).

TOKEN MODEL (Fyers v3):
  - access_token  : valid 1 day  -> auto-refreshed daily before market open
  - refresh_token : valid 15 days -> used + PIN to mint new access_token
  - After 15 days the refresh_token expires with NO renewal endpoint ->
    one manual auth-code bootstrap required (Fyers limitation, not ours).

  Tokens persisted in Railway table `fyers_tokens` (single row, id=1).

USAGE:
  Normal (worker / daily):   python fyers_feed.py
     -> reads refresh_token from DB, auto-mints access_token, runs.
  Bootstrap (first time / after 15d):  python fyers_feed.py --auth-code <code>
     -> exchanges auth-code for access+refresh, stores both, runs.
"""

import argparse, hashlib, os, time, logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytz, psycopg2, requests

# --- CONFIG (env-var first, hardcoded fallback so Railway worker works) ---
FYERS_CLIENT_ID = os.environ.get('FYERS_CLIENT_ID', '1A4STS8ZGD-100')
FYERS_SECRET    = os.environ.get('FYERS_SECRET',    'YXTIR2MN9V')
FYERS_PIN       = os.environ.get('FYERS_PIN',       '2580')
DATABASE_URL    = os.environ.get('DATABASE_URL')

AUTHCODE_URL    = 'https://api-t1.fyers.in/api/v3/validate-authcode'
REFRESH_URL     = 'https://api-t1.fyers.in/api/v3/validate-refresh-token'
HISTORY_URL     = 'https://api-t1.fyers.in/data/history'
QUOTES_URL      = 'https://api-t1.fyers.in/data/quotes'
IST             = pytz.timezone('Asia/Kolkata')

FUTURES_INTERVAL    = 1     # minutes
EQUITY_INTERVAL     = 15    # minutes  (was 5 -> per spec, non-futures = 15-min)
INDEX_INTERVAL      = 1
RETENTION_DAYS      = 7
REFRESH_VALID_DAYS  = 15
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


# ---------------------------------------------------------------- DB

def get_db(): return psycopg2.connect(DATABASE_URL)

def app_id_hash():
    return hashlib.sha256(f'{FYERS_CLIENT_ID}:{FYERS_SECRET}'.encode()).hexdigest()


# ---------------------------------------------------------------- TOKENS

def load_tokens(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT access_token, refresh_token, access_created, refresh_created "
                    "FROM fyers_tokens WHERE id=1")
        return cur.fetchone()

def save_tokens(conn, access=None, refresh=None, new_refresh=False):
    now = datetime.now(IST).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM fyers_tokens WHERE id=1")
        exists = cur.fetchone()
        if exists:
            if new_refresh:
                cur.execute("""UPDATE fyers_tokens SET access_token=%s, refresh_token=%s,
                               access_created=%s, refresh_created=%s, updated_at=NOW() WHERE id=1""",
                            (access, refresh, now, now))
            else:
                cur.execute("""UPDATE fyers_tokens SET access_token=%s, access_created=%s,
                               updated_at=NOW() WHERE id=1""", (access, now))
        else:
            cur.execute("""INSERT INTO fyers_tokens (id, access_token, refresh_token,
                           access_created, refresh_created, updated_at)
                           VALUES (1,%s,%s,%s,%s,NOW())""", (access, refresh, now, now))
    conn.commit()

def bootstrap_from_authcode(conn, auth_code):
    """First-time / post-15-day: exchange auth-code for access + refresh tokens."""
    r = requests.post(AUTHCODE_URL, json={
        'grant_type': 'authorization_code',
        'appIdHash':  app_id_hash(),
        'code':       auth_code,
    }, timeout=10)
    d = r.json()
    if d.get('code') != 200:
        raise Exception(f"Auth-code exchange failed: {d}")
    save_tokens(conn, access=d['access_token'], refresh=d.get('refresh_token'), new_refresh=True)
    log.info("Bootstrap OK - access + refresh tokens stored")
    return d['access_token']

def refresh_access_token(conn, refresh_token):
    """Daily: mint a fresh access_token using refresh_token + PIN."""
    r = requests.post(REFRESH_URL, json={
        'grant_type':   'refresh_token',
        'appIdHash':    app_id_hash(),
        'refresh_token': refresh_token,
        'pin':          FYERS_PIN,
    }, timeout=10)
    d = r.json()
    if d.get('s') == 'error' or 'access_token' not in d:
        raise Exception(f"Refresh failed: {d}")
    save_tokens(conn, access=d['access_token'], new_refresh=False)
    log.info("Access token refreshed via refresh_token")
    return d['access_token']

def get_valid_token(conn, auth_code=None):
    """
    Resolve a usable access_token:
      1. --auth-code provided -> bootstrap (stores access + refresh)
      2. refresh_token in DB and < 15 days old -> refresh access_token
      3. otherwise -> manual bootstrap required (raise with instructions)
    """
    if auth_code:
        return bootstrap_from_authcode(conn, auth_code)

    row = load_tokens(conn)
    if row and row[1]:  # refresh_token present
        refresh_token, refresh_created = row[1], row[3]
        age_days = (datetime.now(IST).replace(tzinfo=None) - refresh_created).days if refresh_created else 999
        if age_days < REFRESH_VALID_DAYS:
            return refresh_access_token(conn, refresh_token)
        log.warning(f"refresh_token is {age_days}d old (>={REFRESH_VALID_DAYS}) - manual bootstrap needed")

    raise SystemExit(
        "\nNO VALID REFRESH TOKEN. One-time manual bootstrap required:\n"
        f"  1. Open: https://api-t1.fyers.in/api/v3/generate-authcode?client_id={FYERS_CLIENT_ID}"
        "&redirect_uri=http%3A%2F%2F127.0.0.1&response_type=code&state=None\n"
        "  2. Login, copy the auth_code from the redirect URL\n"
        "  3. Run once: python fyers_feed.py --auth-code <code>\n"
        "  (After this, the worker auto-refreshes for ~15 days.)\n"
    )


# ---------------------------------------------------------------- FEED

def hdr(token): return {'Authorization': f'{FYERS_CLIENT_ID}:{token}'}

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
            for name, fsym in INDEX_LTP_SYMBOLS.items():
                if fsym == item['n']:
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
    all_rows, errors, done, total = [], 0, 0, len(symbols)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        fmap = {ex.submit(fetch_one, token, s, resolution, timeframe): s for s in symbols}
        for fut in as_completed(fmap, timeout=300):
            sym = fmap[fut]
            try:
                rows = fut.result(timeout=6)
                all_rows.extend(rows)
                done += 1
                if done % 50 == 0:
                    log.info(f"  {done}/{total} done")
                    if all_rows:
                        upsert_candles(conn, all_rows); all_rows = []
            except Exception as e:
                errors += 1
                log.warning(f"  {sym}: {e}")
    if all_rows: upsert_candles(conn, all_rows)
    log.info(f"  {timeframe} complete ({total} symbols, {errors} errors)")

def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5: return False
    mins = now.hour * 60 + now.minute
    return (9*60+15) <= mins <= (15*60+30)

def run(auth_code=None):
    conn  = get_db()
    token = get_valid_token(conn, auth_code)
    futures, equity = get_universe(conn)
    last_futures = last_equity = last_index = last_cleanup = last_token_day = None
    log.info("Feed running (worker mode). 1m futures / 15m equity / 7d retention.")

    while True:
        now = datetime.now(IST)

        # Daily token refresh ~08:45 IST, before market open
        if now.hour == 8 and now.minute >= 45 and last_token_day != now.date():
            try:
                row = load_tokens(conn)
                if row and row[1]:
                    token = refresh_access_token(conn, row[1])
                last_token_day = now.date()
            except Exception as e:
                log.error(f"Daily token refresh failed: {e}")

        # Daily cleanup at 16:00
        if now.hour == 16 and now.minute == 0 and last_cleanup != now.date():
            delete_old_records(conn); last_cleanup = now.date()

        if not is_market_open():
            time.sleep(60); continue

        if last_index is None or (now - last_index).seconds >= INDEX_INTERVAL * 60:
            update_index_ltp(conn, token); last_index = now

        if last_futures is None or (now - last_futures).seconds >= FUTURES_INTERVAL * 60:
            try:
                log.info(f"Futures 1-min ({len(futures)} symbols)")
                fetch_batch(token, futures, '1', '1m', conn); last_futures = now
            except Exception as e:
                log.error(f"Futures failed: {e}")

        if last_equity is None or (now - last_equity).seconds >= EQUITY_INTERVAL * 60:
            try:
                log.info(f"Equity 15-min ({len(equity)} symbols)")
                fetch_batch(token, equity, '15', '15m', conn); last_equity = now
            except Exception as e:
                log.error(f"Equity failed: {e}")

        time.sleep(30)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--auth-code', type=str, default=None)
    args = parser.parse_args()
    run(auth_code=args.auth_code)
