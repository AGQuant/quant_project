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

Reliable path: ONE symbol per request (Yahoo batch mode is flaky).

Usage (module):
    import yahoo_ondemand
    candles = yahoo_ondemand.fetch_intraday('TATAPOWER', days=15, interval='5m')
    # -> [{'ts','open','high','low','close','volume'}, ...]  oldest -> newest

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
