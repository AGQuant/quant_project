"""
Fyers Backfill + Gap Healer - Scorr V8
========================================
Fills intraday_prices history from the Fyers HISTORY REST API.

Two jobs:
  1. backfill_7day()  - one-time on worker boot: pulls 7 days of
                        1-min futures + 15-min equity.
  2. heal_gap(symbol, timeframe) - on WebSocket reconnect: pulls only
                        the slice from the newest stored candle -> now,
                        so a dropped socket never leaves a hole.

History API limits (verified): up to 100 days/request for intraday
resolutions, so a 7-day pull is a single call per symbol.

Shared by fyers_feed.py (imported). Can also be run standalone:
  python fyers_backfill.py            -> full 7-day backfill
  python fyers_backfill.py --heal     -> heal gaps for all symbols
"""

import argparse, os, time, logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytz, psycopg2, requests

FYERS_CLIENT_ID = os.environ.get('FYERS_CLIENT_ID', '1A4STS8ZGD-100')
DATABASE_URL    = os.environ.get('DATABASE_URL')
HISTORY_URL     = 'https://api-t1.fyers.in/data/history'
IST             = pytz.timezone('Asia/Kolkata')

RETENTION_DAYS = 7
WORKERS        = 10

SKIP_SYMBOLS = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'}
SPECIAL_SYMBOLS = {'M&M': 'NSE:M%26M-EQ'}

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fyers_backfill')


def get_db(): return psycopg2.connect(DATABASE_URL)
def hdr(token): return {'Authorization': f'{FYERS_CLIENT_ID}:{token}'}
def fyers_eq_symbol(sym): return SPECIAL_SYMBOLS.get(sym, f'NSE:{sym}-EQ')


def get_universe(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe")
        futures = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT symbol FROM cmp_prices")
        all_stocks = {r[0] for r in cur.fetchall()}
    equity = all_stocks - futures
    return sorted(futures - SKIP_SYMBOLS), sorted(equity - SKIP_SYMBOLS)


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


def fetch_history(token, sym, resolution, timeframe, date_from, date_to):
    """One history call. date_from/date_to are date objects."""
    r = requests.get(HISTORY_URL, params={
        'symbol':      fyers_eq_symbol(sym),
        'resolution':  resolution,
        'date_format': '1',
        'range_from':  date_from.strftime('%Y-%m-%d'),
        'range_to':    date_to.strftime('%Y-%m-%d'),
        'cont_flag':   '1',
    }, headers=hdr(token), timeout=8)
    d = r.json()
    if 'candles' not in d:
        return []
    rows = []
    for c in d['candles']:
        ts = datetime.fromtimestamp(c[0], tz=IST).replace(tzinfo=None)
        rows.append((sym, ts, c[1], c[2], c[3], c[4], int(c[5]), timeframe, 'fyers'))
    return rows


def _batch(token, symbols, resolution, timeframe, date_from, date_to, conn):
    total, errors, done = 0, 0, 0
    buf = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        fmap = {ex.submit(fetch_history, token, s, resolution, timeframe, date_from, date_to): s
                for s in symbols}
        for fut in as_completed(fmap, timeout=600):
            sym = fmap[fut]
            try:
                rows = fut.result(timeout=10)
                buf.extend(rows); total += len(rows); done += 1
                if done % 50 == 0:
                    log.info(f"  {done}/{len(symbols)}")
                    upsert_candles(conn, buf); buf = []
            except Exception as e:
                errors += 1
                log.warning(f"  {sym}: {e}")
    upsert_candles(conn, buf)
    log.info(f"  {timeframe} backfill done: {total} candles, {errors} errors")
    return total


def backfill_7day(token, conn=None):
    """One-time full 7-day backfill. 1m futures + 15m equity."""
    own = conn is None
    if own: conn = get_db()
    futures, equity = get_universe(conn)
    now = datetime.now(IST)
    date_from = (now - timedelta(days=RETENTION_DAYS)).date()
    date_to   = now.date()
    log.info(f"Backfill {date_from} -> {date_to}: {len(futures)} futures (1m), {len(equity)} equity (15m)")
    _batch(token, futures, '1',  '1m',  date_from, date_to, conn)
    _batch(token, equity,  '15', '15m', date_from, date_to, conn)
    if own: conn.close()
    log.info("7-day backfill complete.")


def newest_ts(conn, symbol, timeframe):
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(ts) FROM intraday_prices WHERE symbol=%s AND timeframe=%s",
                    (symbol, timeframe))
        r = cur.fetchone()
    return r[0] if r and r[0] else None


def heal_gap(token, conn, symbols, resolution, timeframe):
    """
    On reconnect: for each symbol, find newest stored candle and pull
    only from that day -> now. Closes any hole from a socket drop.
    """
    now = datetime.now(IST)
    healed = 0
    for sym in symbols:
        last = newest_ts(conn, sym, timeframe)
        date_from = last.date() if last else (now - timedelta(days=RETENTION_DAYS)).date()
        try:
            rows = fetch_history(token, sym, resolution, timeframe, date_from, now.date())
            if rows:
                upsert_candles(conn, rows); healed += len(rows)
        except Exception as e:
            log.warning(f"  heal {sym}: {e}")
        time.sleep(0.05)
    log.info(f"Gap heal {timeframe}: {healed} candles patched across {len(symbols)} symbols")
    return healed


if __name__ == '__main__':
    import fyers_feed  # reuse its token resolver
    parser = argparse.ArgumentParser()
    parser.add_argument('--heal', action='store_true')
    parser.add_argument('--auth-code', type=str, default=None)
    args = parser.parse_args()

    conn = get_db()
    token = fyers_feed.get_valid_token(conn, args.auth_code)
    if args.heal:
        fut, eq = get_universe(conn)
        heal_gap(token, conn, fut, '1', '1m')
        heal_gap(token, conn, eq, '15', '15m')
    else:
        backfill_7day(token, conn)
    conn.close()
