"""
Yahoo On-Demand Intraday Fetcher - Scorr V8
=============================================
Fetches intraday OHLCV for a SINGLE symbol from the Yahoo chart API,
ON DEMAND, WITHOUT storing anything in the database.

Why this exists
---------------
The Fyers feed and `intraday_prices` are FUTURES-ONLY by design (208 names,
1-min, 7 days). When we need an intraday / 5-min read on a NON-FUTURES stock
(research / ad-hoc), we pull just that one symbol live from Yahoo. Yahoo
serves 5-min candles for up to ~60 days, so a 15-day 5-min pattern is
available. Nothing is written to Postgres - the caller analyses the returned
list and discards it, so the futures-only DB stays clean.

source override
---------------
get_intraday_smart(..., source='yahoo') forces a LIVE Yahoo pull and SKIPS the
DB entirely, even for futures names. Use this to manually pull Yahoo data for a
futures stock (e.g. RELIANCE) when you suspect the Fyers feed is down - comparing
it against what Fyers should be showing tells you if Fyers has an issue. No silent
auto-fallback by design: the manual step is itself the Fyers health check.

Reliable path: ONE symbol per request (Yahoo batch mode is flaky).

Usage (module):
    import yahoo_ondemand
    candles = yahoo_ondemand.fetch_intraday('TATAPOWER', days=15, interval='5m')
    # -> [{'ts','open','high','low','close','volume'}, ...]  oldest -> newest

    # DB-first (futures) else live Yahoo (non-futures), no store:
    res = yahoo_ondemand.get_intraday_smart('TATAPOWER', days=15, interval='5m')

    # Force live Yahoo even for a futures stock (Fyers-down check):
    res = yahoo_ondemand.get_intraday_smart('RELIANCE', days=2, interval='5m', source='yahoo')

Usage (CLI):
    python yahoo_ondemand.py TATAPOWER --days 15 --interval 5m
"""

import argparse
import json
import time
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
import pytz

IST = pytz.timezone('Asia/Kolkata')
CHART_URL = 'https://query1.finance.yahoo.com/v8/finance/chart/{sym}'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')

# Yahoo intraday history caps (approx, in days): 1m ~ 7d, 5m/15m/30m ~ 60d.
MAX_DAYS = {'1m': 7, '2m': 60, '5m': 60, '15m': 60, '30m': 60, '60m': 730, '1d': 3650}

# Symbols whose Yahoo ticker differs from the raw NSE symbol (rare). The
# default rule simply appends '.NS' (e.g. SBIN -> SBIN.NS, M&M -> M&M.NS).
YSYM_OVERRIDE = {}


def yahoo_symbol(symbol, exchange='NS'):
    if symbol in YSYM_OVERRIDE:
        return YSYM_OVERRIDE[symbol]
    return '{}.{}'.format(symbol, exchange)


def fetch_intraday(symbol, days=15, interval='5m', exchange='NS'):
    """Fetch `days` of `interval` candles for ONE symbol from Yahoo. No DB writes.

    Returns a list of dicts sorted oldest -> newest:
        {'ts','open','high','low','close','volume'}
    Raises on a hard HTTP/parse failure so the caller can surface it.
    """
    interval = interval.lower()
    cap = MAX_DAYS.get(interval, 60)
    days = min(max(int(days), 1), cap)

    ysym = yahoo_symbol(symbol, exchange)

    now = int(time.time())
    period1 = now - days * 86400 - 86400   # pad one day, trimmed precisely below
    period2 = now + 86400

    url = CHART_URL.format(sym=quote(ysym, safe=''))
    params = {
        'period1': period1,
        'period2': period2,
        'interval': interval,
        'includePrePost': 'false',
        'events': 'div,splits',
    }
    r = requests.get(url, params=params, headers={'User-Agent': UA}, timeout=12)
    r.raise_for_status()
    d = r.json()

    chart = d.get('chart') or {}
    if chart.get('error'):
        raise ValueError('Yahoo error for {}: {}'.format(ysym, chart['error']))
    result = chart.get('result') or []
    if not result:
        return []
    res = result[0]
    ts = res.get('timestamp') or []
    qb = ((res.get('indicators') or {}).get('quote') or [{}])[0]
    o = qb.get('open') or []
    h = qb.get('high') or []
    lo_ = qb.get('low') or []
    c = qb.get('close') or []
    v = qb.get('volume') or []

    out = []
    n = len(ts)
    for i in range(n):
        op = o[i] if i < len(o) else None
        hi = h[i] if i < len(h) else None
        low = lo_[i] if i < len(lo_) else None
        cl = c[i] if i < len(c) else None
        if op is None or hi is None or low is None or cl is None:
            continue  # Yahoo pads gaps/holidays with nulls
        dt = datetime.fromtimestamp(ts[i], tz=IST).replace(tzinfo=None)
        out.append({
            'ts': dt.strftime('%Y-%m-%d %H:%M:%S'),
            'open': round(float(op), 2),
            'high': round(float(hi), 2),
            'low': round(float(low), 2),
            'close': round(float(cl), 2),
            'volume': int(v[i]) if (i < len(v) and v[i] is not None) else 0,
        })

    # Trim precisely to the requested window (we padded by a day above).
    cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(days=days)
    out = [row for row in out
           if datetime.strptime(row['ts'], '%Y-%m-%d %H:%M:%S') >= cutoff]
    return out


def _db_intraday(symbol, days):
    """Stored Fyers intraday (futures, 1-min) for a symbol, or [] if none stored."""
    try:
        import os, psycopg2
        dburl = os.environ.get('DATABASE_URL')
        if not dburl:
            return []
        db_days = min(max(int(days), 1), 7)
        cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(days=db_days)
        with psycopg2.connect(dburl) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, ts, open, high, low, close, volume "
                "FROM intraday_prices WHERE symbol=%s AND ts >= %s ORDER BY ts ASC",
                (symbol.upper(), cutoff))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def get_intraday_smart(symbol, days=15, interval='5m', exchange='NS', source='auto'):
    """Intraday for ANY stock: DB-first, then live Yahoo (no store).

    - source='auto' (default):
        Futures (rows in intraday_prices) -> stored Fyers 1-min candles.
        Non-futures (nothing stored)      -> live Yahoo `interval` candles, no store.
    - source='yahoo':
        Force a LIVE Yahoo pull, SKIP the DB entirely, even for futures names.
        Use to manually check a futures stock when Fyers is suspected down.

    Returns dict: {symbol, source, interval, count, candles, note?}.
    """
    sym = symbol.upper()
    src = (source or 'auto').lower()

    # Forced live Yahoo (Fyers-down manual check) — skip DB.
    if src == 'yahoo':
        try:
            candles = fetch_intraday(sym, days=days, interval=interval, exchange=exchange)
        except Exception as e:
            return {'symbol': sym, 'source': 'yahoo_live_forced', 'interval': interval,
                    'count': 0, 'candles': [], 'error': str(e)}
        for c in candles:
            c['symbol'] = sym
            c['source'] = 'yahoo'
        return {'symbol': sym, 'source': 'yahoo_live_forced', 'interval': interval,
                'count': len(candles), 'candles': candles,
                'note': 'FORCED live Yahoo (DB skipped). Compare vs expected Fyers to gauge feed health.'}

    rows = _db_intraday(sym, days)
    if rows:
        for r in rows:
            ts = r.get('ts')
            r['ts'] = ts.strftime('%Y-%m-%d %H:%M:%S') if hasattr(ts, 'strftime') else str(ts)
            r['source'] = 'fyers'
            for k in ('open', 'high', 'low', 'close'):
                if r.get(k) is not None:
                    r[k] = round(float(r[k]), 2)
            r['volume'] = int(r['volume']) if r.get('volume') is not None else 0
        return {'symbol': sym, 'source': 'fyers_db', 'interval': '1m',
                'count': len(rows), 'candles': rows}
    try:
        candles = fetch_intraday(sym, days=days, interval=interval, exchange=exchange)
    except Exception as e:
        return {'symbol': sym, 'source': 'yahoo_live', 'interval': interval,
                'count': 0, 'candles': [], 'error': str(e)}
    for c in candles:
        c['symbol'] = sym
        c['source'] = 'yahoo'
    return {'symbol': sym, 'source': 'yahoo_live', 'interval': interval,
            'count': len(candles), 'candles': candles,
            'note': 'non-futures: fetched live from Yahoo, not stored'}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Yahoo on-demand intraday fetcher (no DB writes).')
    ap.add_argument('symbol')
    ap.add_argument('--days', type=int, default=15)
    ap.add_argument('--interval', default='5m')
    ap.add_argument('--exchange', default='NS')
    a = ap.parse_args()
    rows = fetch_intraday(a.symbol, days=a.days, interval=a.interval, exchange=a.exchange)
    print(json.dumps({'symbol': a.symbol, 'interval': a.interval, 'days': a.days,
                      'count': len(rows), 'candles': rows}, indent=2))
