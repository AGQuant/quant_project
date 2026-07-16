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

# cc#228: fyers_eq (live WS) is the SOLE canonical equity source. The legacy REST equity
# backfill/heal path below wrote source='fyers' — a 100% duplicate of fyers_eq, and the V8
# engine reads ONLY fyers_eq (cc#140), so those rows were unread dead weight (legacy also
# wrote on the closed Sat 04-Jul). This EQUITY path is now DORMANT: skipped unless explicitly
# forced (force=True, or flip LEGACY_EQUITY_BACKFILL=True) — kept present as an emergency
# MANUAL fallback only. The FUTURES path (source='fyers_fut') is UNAFFECTED.
LEGACY_EQUITY_BACKFILL = False

# cc_task #87 (canonical rule, locked): backfill is POST-MARKET / on-demand ONLY.
# Never write history bars to intraday_prices during the live session.
MARKET_OPEN     = dt_time(9, 15)
MARKET_CLOSE    = dt_time(15, 30)

SKIP_SYMBOLS    = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'}
# cc#184: the FUTURES path keeps NIFTY + BANKNIFTY (index futures are real,
# native WS subscribes them under those exact symbols) but drops NIFTY50
# (that is the SPOT index — futures live under 'NIFTY'). FINNIFTY/MIDCPNIFTY/
# SENSEX/BANKEX have no stock-futures rows to backfill here.
FUT_SKIP        = {'NIFTY50', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'}
SPECIAL_SYMBOLS = {'M&M': 'NSE:M&M-EQ'}

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fyers_backfill')


def get_db(): return psycopg2.connect(DATABASE_URL)
def hdr(token): return {'Authorization': f'{FYERS_CLIENT_ID}:{token}'}
def fyers_eq_symbol(sym): return SPECIAL_SYMBOLS.get(sym, f'NSE:{sym}-EQ')


def fyers_fut_symbol(sym, contract):
    """cc#184: Fyers monthly-futures ticker, e.g.
        fyers_fut_symbol('SBIN', '26JUL')      -> 'NSE:SBIN26JULFUT'
        fyers_fut_symbol('NIFTY', '26JUL')     -> 'NSE:NIFTY26JULFUT'
        fyers_fut_symbol('BANKNIFTY', '26JUL') -> 'NSE:BANKNIFTY26JULFUT'
    `contract` is the 'YYMMM' expiry code. Format mirrors
    fyers_feed.futures_fyers_symbol EXACTLY so REST-backfilled bars land on the
    same instrument the live WS writes (one symbol format, one source of truth)."""
    return f'NSE:{sym}{contract}FUT'


def default_contract():
    """Current active monthly contract code 'YYMMM' from the live expiry rule
    (fyers_feed.current_expiry — last Tuesday of month, rolls to next month after
    expiry). Lazy import avoids the fyers_feed<->fyers_backfill circular import at
    module load; by call time both modules are fully initialised."""
    import fyers_feed
    exp = fyers_feed.current_expiry()
    return f"{exp.strftime('%y')}{exp.strftime('%b').upper()}"


def _assert_not_market_hours(fn):
    """cc_task #87: hard-block any backfill write during 09:15-15:30 IST. Stale
    history bars written mid-session caused a wrong-price V8 paper entry. Backfill
    is post-market / on-demand only — raise so no code path can violate this."""
    now = datetime.now(IST)
    if now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE:
        raise RuntimeError(
            f"{fn} blocked during market hours (09:15-15:30 IST) — "
            "backfill is post-market/on-demand only")


def get_universe(conn, futures=False):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe WHERE is_active = TRUE")
        syms = {r[0] for r in cur.fetchall()}
    # cc#184: futures path keeps NIFTY/BANKNIFTY, drops NIFTY50; equity path
    # (boot/heal spot healer) keeps the original SKIP_SYMBOLS behaviour.
    return sorted(syms - (FUT_SKIP if futures else SKIP_SYMBOLS))


def upsert_candles(conn, rows, on_conflict='update'):
    if not rows: return
    # cc#184: futures backfill uses DO NOTHING so it never overwrites native
    # WebSocket fut bars (e.g. 03-Jul). Equity healer keeps DO UPDATE.
    action = ("DO UPDATE SET open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,"
              "close=EXCLUDED.close,volume=EXCLUDED.volume"
              if on_conflict == 'update' else "DO NOTHING")
    with conn.cursor() as cur:
        cur.executemany(f"""
            INSERT INTO intraday_prices (symbol,ts,open,high,low,close,volume,timeframe,source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol,ts,timeframe,source) {action}
        """, rows)
    conn.commit()


def fetch_history(token, sym, resolution, timeframe, date_from, date_to,
                  fyers_symbol=None, source='fyers', cont_flag='1'):
    params = {
        'symbol':      fyers_symbol or fyers_eq_symbol(sym),
        'resolution':  resolution,
        'date_format': '1',
        'range_from':  date_from.strftime('%Y-%m-%d'),
        'range_to':    date_to.strftime('%Y-%m-%d'),
        'cont_flag':   cont_flag,
    }
    last_status, last_body = None, None
    for attempt in range(HISTORY_RETRIES + 1):
        try:
            r = requests.get(HISTORY_URL, params=params, headers=hdr(token), timeout=10)
            last_status = r.status_code
            d = r.json()
        except Exception as e:
            d = {}
            last_body = f"request_exception: {e}"
        candles = d.get('candles') if isinstance(d, dict) else None
        if candles:
            rows = []
            for c in candles:
                ts = datetime.fromtimestamp(c[0], tz=IST).replace(tzinfo=None)
                rows.append((sym, ts, c[1], c[2], c[3], c[4], int(c[5]), timeframe, source))
            return rows
        if last_body is None:
            last_body = str(d)[:300]
        if attempt < HISTORY_RETRIES:
            time.sleep(1 + attempt)
    # cc#489 step_5: was a silent empty return — diagnosable now (was previously
    # guessed at rather than confirmed; 15-Jul backfill wrote 0 bars for all ~210
    # symbols with no visibility into why).
    log.warning(f"fetch_history EMPTY: {sym} {params['symbol']} {params['range_from']}->"
                f"{params['range_to']} status={last_status} body={last_body}")
    return []


def backfill_range(token, conn=None, date_from=None, date_to=None, symbols=None,
                   futures=False, contract=None, force=False):
    """cc#159: sequential REST backfill for an explicit date range and/or symbol
    subset. Same pacing/rate-limit behavior (5s sleep between symbols).

    cc#184: `futures=True` makes this a TRUE futures backfill — it resolves each
    symbol to its explicit monthly contract (NSE:{sym}{contract}FUT, cont_flag=0
    so there is NO continuous-series splice across the expiry rollover), writes
    source='fyers_fut', and upserts DO NOTHING (never clobbers native WS bars).
    `contract` defaults to the current active monthly ('YYMMM'); pass it
    explicitly (e.g. '26JUL') to mark a whole window against one held contract
    regardless of run date. The default (futures=False) path is the legacy
    equity/spot healer (source='fyers', -EQ symbol) used by boot/heal callers."""
    _assert_not_market_hours('backfill_range')
    # cc#228: legacy equity backfill (source='fyers') is dormant — fyers_eq WS is the sole
    # equity source. Futures is exempt (never a duplicate). Manual fallback: force=True.
    if not futures and not (force or LEGACY_EQUITY_BACKFILL):
        log.warning("backfill_range: legacy EQUITY backfill (source='fyers') is DORMANT (cc#228) "
                    "— fyers_eq WS is the sole equity source; skipped (force=True for manual use).")
        return {"skipped": "legacy_equity_backfill_dormant", "source": "fyers",
                "symbols_processed": 0, "bars_written": 0}
    own = conn is None
    if own: conn = get_db()

    now       = datetime.now(IST)
    date_from = date_from or (now - timedelta(days=RETENTION_DAYS)).date()
    date_to   = date_to or now.date()
    universe  = get_universe(conn, futures=futures)
    syms      = sorted(set(symbols) & set(universe)) if symbols else universe

    if futures:
        contract = contract or default_contract()
        source, cont_flag, on_conflict = 'fyers_fut', '0', 'nothing'
        log.info(f"FUTURES backfill {date_from} -> {date_to}: {len(syms)} symbols, "
                 f"explicit {contract} contract, 5m, DO NOTHING, 5s sleep")
    else:
        source, cont_flag, on_conflict = 'fyers', '1', 'update'
        log.info(f"Backfill {date_from} -> {date_to}: {len(syms)} symbols, 5m, sequential 5s sleep")

    total, empty = 0, 0
    for i, sym in enumerate(syms, 1):
        fsym = fyers_fut_symbol(sym, contract) if futures else None
        rows = fetch_history(token, sym, '5', '5m', date_from, date_to,
                             fyers_symbol=fsym, source=source, cont_flag=cont_flag)
        if rows:
            upsert_candles(conn, rows, on_conflict=on_conflict)
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
        "gaps_remaining": empty, "source": source,
        "contract": contract if futures else None,
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


def heal_gap(token, conn, symbols, force=False):
    """On reconnect: pull only from newest stored candle -> now per symbol.
    cc#228: this heals EQUITY into source='fyers' (via fetch_history default, -EQ symbol),
    which the V8 engine never reads (fyers_eq only) — so it is dormant now; fyers_eq WS is the
    sole equity source. Kept present as a manual fallback (force=True)."""
    _assert_not_market_hours('heal_gap')
    if not (force or LEGACY_EQUITY_BACKFILL):
        log.warning("heal_gap: legacy EQUITY gap-heal (source='fyers') is DORMANT (cc#228) — "
                    "fyers_eq WS is the sole equity source; skipped (force=True for manual use).")
        return 0
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
