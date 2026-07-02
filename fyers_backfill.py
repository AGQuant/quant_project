"""
Fyers Backfill + Gap Healer - Scorr V8
========================================
Fills intraday_prices history from the Fyers HISTORY REST API.

All 208 futures: 5-min bars, 7-day rolling.

Two jobs:
  1. backfill_7day()  - one-time on worker boot: pulls 7 days of 5-min bars
                        for all futures SEQUENTIALLY with 5s sleep to avoid
                        Fyers rate limits. ~208 symbols x 5s = ~17 min.
  2. heal_gap()       - on WebSocket reconnect: pulls only the slice from
                        newest stored candle -> now per symbol.

History API limits: up to 100 days/request for intraday resolutions.
Rate limiting: sequential + 5s sleep = reliable full coverage.

Shared by fyers_feed.py (imported). Can also be run standalone:
  python fyers_backfill.py            -> full 7-day futures backfill
  python fyers_backfill.py --heal     -> heal gaps for futures
"""

import argparse, os, time, logging
from datetime import datetime, timedelta, time as dt_time
import pytz, psycopg2, requests

FYERS_CLIENT_ID = os.environ.get('FYERS_CLIENT_ID', '1A4STS8ZGD-100')
DATABASE_URL    = os.environ.get('DATABASE_URL')
HISTORY_URL     = 'https://api-t1.fyers.in/data/history'
IST             = pytz.timezone('Asia/Kolkata')

RETENTION_DAYS  = 7
HISTORY_RETRIES = 2
SLEEP_BETWEEN   = 5  # seconds between symbol calls — avoids rate limit

# cc_task #87 (canonical rule, locked): backfill is POST-MARKET / on-demand ONLY.
# Never write history bars to intraday_prices during the live session.
MARKET_OPEN     = dt_time(9, 15)
MARKET_CLOSE    = dt_time(15, 30)

SKIP_SYMBOLS    = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'}
SPECIAL_SYMBOLS = {'M&M': 'NSE:M&M-EQ'}

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fyers_backfill')


def get_db(): return psycopg2.connect(DATABASE_URL)
def hdr(token): return {'Authorization': f'{FYERS_CLIENT_ID}:{token}'}
def fyers_eq_symbol(sym): return SPECIAL_SYMBOLS.get(sym, f'NSE:{sym}-EQ')


def _assert_not_market_hours(fn):
    """cc_task #87: hard-block any backfill write during 09:15-15:30 IST. Stale
    history bars written mid-session caused a wrong-price V8 paper entry. Backfill
    is post-market / on-demand only — raise so no code path can violate this."""
    now = datetime.now(IST)
    if now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE:
        raise RuntimeError(
            f"{fn} blocked during market hours (09:15-15:30 IST) — "
            "backfill is post-market/on-demand only")


def get_universe(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe WHERE is_active = TRUE")
        futures = {r[0] for r in cur.fetchall()}
    return sorted(futures - SKIP_SYMBOLS)


def upsert_candles(conn, rows):
    if not rows: return
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO intraday_prices (symbol,ts,open,high,low,close,volume,timeframe,source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol,ts,timeframe,source) DO UPDATE SET
                open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                close=EXCLUDED.close,volume=EXCLUDED.volume
        """, rows)
    conn.commit()


def fetch_history(token, sym, resolution, timeframe, date_from, date_to):
    params = {
        'symbol':      fyers_eq_symbol(sym),
        'resolution':  resolution,
        'date_format': '1',
        'range_from':  date_from.strftime('%Y-%m-%d'),
        'range_to':    date_to.strftime('%Y-%m-%d'),
        'cont_flag':   '1',
    }
    for attempt in range(HISTORY_RETRIES + 1):
        try:
            r = requests.get(HISTORY_URL, params=params, headers=hdr(token), timeout=10)
            d = r.json()
        except Exception:
            d = {}
        candles = d.get('candles') if isinstance(d, dict) else None
        if candles:
            rows = []
            for c in candles:
                ts = datetime.fromtimestamp(c[0], tz=IST).replace(tzinfo=None)
                rows.append((sym, ts, c[1], c[2], c[3], c[4], int(c[5]), timeframe, 'fyers'))
            return rows
        if attempt < HISTORY_RETRIES:
            time.sleep(1 + attempt)
    return []


def backfill_range(token, conn=None, date_from=None, date_to=None, symbols=None):
    """cc#159: sequential REST backfill for an explicit date range and/or symbol
    subset (generalizes backfill_7day for on-demand admin/MCP-triggered runs).
    Same pacing/rate-limit behavior (5s sleep between symbols), same fyers/5m
    upsert target. Returns a summary dict instead of None."""
    _assert_not_market_hours('backfill_range')
    own = conn is None
    if own: conn = get_db()

    now       = datetime.now(IST)
    date_from = date_from or (now - timedelta(days=RETENTION_DAYS)).date()
    date_to   = date_to or now.date()
    universe  = get_universe(conn)
    syms      = sorted(set(symbols) & set(universe)) if symbols else universe

    log.info(f"Backfill {date_from} -> {date_to}: {len(syms)} futures, 5m, sequential 5s sleep")

    total, empty = 0, 0
    for i, sym in enumerate(syms, 1):
        rows = fetch_history(token, sym, '5', '5m', date_from, date_to)
        if rows:
            upsert_candles(conn, rows)
            total += len(rows)
        else:
            empty += 1
        if i % 20 == 0:
            log.info(f"  {i}/{len(syms)} — {total} candles, {empty} empty")
        time.sleep(SLEEP_BETWEEN)

    log.info(f"Backfill complete: {total} candles, {empty} empty/skipped of {len(syms)}")
    if own: conn.close()
    return {
        "date_from": str(date_from), "date_to": str(date_to),
        "symbols_processed": len(syms), "bars_written": total,
        "gaps_remaining": empty,
    }


def backfill_7day(token, conn=None):
    """Sequential 7-day backfill for all futures. ~17 min. Called on worker boot.
    Thin wrapper over backfill_range (cc#159) — same behavior as before."""
    return backfill_range(token, conn)


def newest_ts(conn, symbol, timeframe):
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(ts) FROM intraday_prices WHERE symbol=%s AND timeframe=%s",
                    (symbol, timeframe))
        r = cur.fetchone()
    return r[0] if r and r[0] else None


def heal_gap(token, conn, symbols):
    """On reconnect: pull only from newest stored candle -> now per symbol."""
    _assert_not_market_hours('heal_gap')
    now    = datetime.now(IST)
    healed = 0
    for sym in symbols:
        last      = newest_ts(conn, sym, '5m')
        date_from = last.date() if last else (now - timedelta(days=RETENTION_DAYS)).date()
        rows      = fetch_history(token, sym, '5', '5m', date_from, now.date())
        if rows:
            upsert_candles(conn, rows)
            healed += len(rows)
        time.sleep(SLEEP_BETWEEN)
    log.info(f"Gap heal: {healed} candles patched across {len(symbols)} symbols")
    return healed


if __name__ == '__main__':
    import fyers_feed
    parser = argparse.ArgumentParser()
    parser.add_argument('--heal', action='store_true')
    parser.add_argument('--auth-code', type=str, default=None)
    args = parser.parse_args()

    conn  = get_db()
    token = fyers_feed.get_valid_token(conn, args.auth_code)
    symbols = get_universe(conn)
    if args.heal:
        heal_gap(token, conn, symbols)
    else:
        backfill_7day(token, conn)
    conn.close()
