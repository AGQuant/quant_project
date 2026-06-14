"""
pcr_backfill.py — One-time historical OI + PCR backfill for INDEX options (Scorr).
====================================================================================
Why this exists
---------------
The live WS strips OI (Fyers SDK pops the 'OI' field), so option_chain.oi was NULL
for Jun-2026 until the live DEPTH-poll fix landed. Intraday bars for the gap days
already exist in option_chain (LTP/vol present, oi NULL). This module re-fetches the
SAME bars via the History API with oi_flag=1 (7th candle column = OI) and UPSERTs the
OI back onto those existing rows, then recomputes both PCR tables.

Scope (locked): NIFTY + BANKNIFTY only, current monthly expiry, ATM +/- 10 strikes.
Stock options are intentionally excluded (not stored live, not backfilled).

FAIL-LOUD guard: on the FIRST option fetched, if candles carry no 7th (OI) column,
the run ABORTS with a clear message — we never silently write NULL/zero OI.

Run ON RAILWAY only (the Fyers token is IP-bound to the worker). Trigger:
    POST /api/pcr/backfill?start=YYYY-MM-DD&end=YYYY-MM-DD   (admin-gated)
or MCP tool: pcr_backfill(start, end).

After OI is upserted, this calls:
    1. pcr_intraday.compute_pcr_intraday()  -> self-heals 5-min pcr_intraday
    2. _recompute_pcr_daily_for_range()     -> fills pcr_daily for each gap day

Symbol format (monthly, matches live feed): NSE:NIFTY26JUN23200CE
"""

import os
import calendar
from datetime import datetime, date, timedelta

import psycopg2
import requests

FYERS_CLIENT_ID = os.environ.get('FYERS_CLIENT_ID', '1A4STS8ZGD-100')
DATABASE_URL    = os.environ.get('DATABASE_URL')
HISTORY_URL     = 'https://api-t1.fyers.in/data/history'
QUOTES_URL      = 'https://api-t1.fyers.in/data/quotes'

MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
          'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']

# Index option config — INDEX ONLY (locked scope)
INDEX_CFG = {
    'NIFTY':     {'fyers_index': 'NSE:NIFTY50-INDEX',  'step': 50,  'n': 10},
    'BANKNIFTY': {'fyers_index': 'NSE:NIFTYBANK-INDEX', 'step': 100, 'n': 10},
}


def _conn():
    return psycopg2.connect(DATABASE_URL)


def _load_token(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT access_token FROM fyers_tokens WHERE id=1")
        r = cur.fetchone()
    if not r or not r[0]:
        raise RuntimeError("No Fyers access_token in fyers_tokens (id=1)")
    return r[0]


def _hdr(token):
    return {'Authorization': f'{FYERS_CLIENT_ID}:{token}'}


def _last_tuesday(y, m):
    """Last Tuesday of month — NSE monthly expiry since Sep 2025."""
    last_day = calendar.monthrange(y, m)[1]
    d = date(y, m, last_day)
    while d.weekday() != 1:   # 1 = Tuesday
        d -= timedelta(days=1)
    return d


def _current_expiry(ref: date) -> date:
    exp = _last_tuesday(ref.year, ref.month)
    if ref > exp:
        if ref.month == 12:
            exp = _last_tuesday(ref.year + 1, 1)
        else:
            exp = _last_tuesday(ref.year, ref.month + 1)
    return exp


def _expiry_code(exp: date) -> str:
    """Monthly expiry code e.g. 26JUN."""
    return f"{exp.strftime('%y')}{exp.strftime('%b').upper()}"


def _opt_symbol(underlying: str, strike: int, otype: str, exp: date) -> str:
    return f"NSE:{underlying}{_expiry_code(exp)}{int(strike)}{otype}"


def _get_ltp(token, fyers_sym):
    r = requests.get(QUOTES_URL, params={'symbols': fyers_sym},
                     headers=_hdr(token), timeout=8)
    d = r.json()
    if d.get('s') == 'ok' and d.get('d'):
        return float(d['d'][0]['v']['lp'])
    raise RuntimeError(f"LTP fetch failed for {fyers_sym}: {d}")


def _build_strikes(ltp, step, n):
    atm = round(ltp / step) * step
    return [int(atm + i * step) for i in range(-n, n + 1) if (atm + i * step) > 0]


def _fetch_option_history(token, sym, start, end):
    """Returns list of candles [ts,o,h,l,c,v,oi]. oi_flag=1 => 7th col is OI."""
    r = requests.get(HISTORY_URL, params={
        'symbol': sym, 'resolution': '5', 'date_format': '1',
        'range_from': start, 'range_to': end,
        'cont_flag': '1', 'oi_flag': '1',
    }, headers=_hdr(token), timeout=15)
    d = r.json()
    if d.get('s') != 'ok':
        return None, d
    return d.get('candles', []), d


def _upsert_oi(conn, sym, underlying, strike, otype, exp, candles):
    """UPSERT each 5-min bar's OI onto option_chain (matches live schema)."""
    from pytz import timezone
    from datetime import time as _dt_time
    ist = timezone('Asia/Kolkata')
    MKT_OPEN, MKT_CLOSE = _dt_time(9, 15), _dt_time(15, 30)
    rows = 0
    with conn.cursor() as cur:
        for c in candles:
            if len(c) < 7:
                continue
            ts = datetime.fromtimestamp(c[0], tz=ist).replace(tzinfo=None)
            # Skip History-API daily-rollup / after-hours bars (e.g. 23:30 with NULL OI).
            # The live feed only writes during market hours; backfill must match.
            if not (MKT_OPEN <= ts.time() <= MKT_CLOSE):
                continue
            oi = int(c[6]) if c[6] is not None else None
            cur.execute("""
                INSERT INTO option_chain
                    (symbol, underlying, strike, option_type, expiry, ltp, oi, volume, bid, ask, ts)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,%s)
                ON CONFLICT (symbol, ts) DO UPDATE SET
                    oi = EXCLUDED.oi,
                    ltp = COALESCE(option_chain.ltp, EXCLUDED.ltp),
                    volume = COALESCE(option_chain.volume, EXCLUDED.volume)
            """, (sym, underlying, strike, otype, exp,
                  c[4], oi, int(c[5]) if c[5] is not None else None, ts))
            rows += 1
    conn.commit()
    return rows


def _recompute_pcr_daily_for_range(conn, start: date, end: date):
    """Fill pcr_daily for each date in [start,end] from the now-OI-populated option_chain."""
    filled = []
    d = start
    while d <= end:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pcr_daily (price_date, underlying, put_oi, call_oi, pcr)
                SELECT DATE(ts), underlying,
                    SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END),
                    SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END),
                    ROUND(SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END)::numeric /
                          NULLIF(SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END),0), 3)
                FROM option_chain
                WHERE DATE(ts) = %s
                  AND underlying IN ('NIFTY','BANKNIFTY')
                  AND ts = (SELECT MAX(oc2.ts) FROM option_chain oc2
                            WHERE DATE(oc2.ts) = %s AND oc2.underlying = option_chain.underlying)
                GROUP BY DATE(ts), underlying
                ON CONFLICT (price_date, underlying) DO UPDATE SET
                    put_oi=EXCLUDED.put_oi, call_oi=EXCLUDED.call_oi,
                    pcr=EXCLUDED.pcr, computed_at=NOW()
            """, (d, d))
            n = cur.rowcount
        conn.commit()
        if n:
            filled.append(str(d))
        d += timedelta(days=1)
    return filled


def run_backfill(start: str, end: str, conn=None):
    """
    Main entry. start/end = 'YYYY-MM-DD' (inclusive).
    Backfills OI for NIFTY+BANKNIFTY ATM+/-10 monthly options, then recomputes
    pcr_intraday (self-heal) + pcr_daily for the range.
    """
    own = conn is None
    if own:
        conn = _conn()
    try:
        token = _load_token(conn)
        sd = datetime.strptime(start, '%Y-%m-%d').date()
        ed = datetime.strptime(end, '%Y-%m-%d').date()
        exp = _current_expiry(ed)   # monthly series active across the gap window

        summary = {'expiry': _expiry_code(exp), 'underlyings': {}, 'oi_guard': 'ok'}
        guard_checked = False

        for underlying, cfg in INDEX_CFG.items():
            ltp = _get_ltp(token, cfg['fyers_index'])
            strikes = _build_strikes(ltp, cfg['step'], cfg['n'])
            total_rows = 0
            contracts = 0
            for strike in strikes:
                for otype in ('CE', 'PE'):
                    sym = _opt_symbol(underlying, strike, otype, exp)
                    candles, raw = _fetch_option_history(token, sym, start, end)
                    if candles is None:
                        continue
                    # FAIL-LOUD: first successful fetch must carry the OI column
                    if not guard_checked and candles:
                        if len(candles[0]) < 7:
                            return {
                                "status": "abort",
                                "reason": "History API returned NO OI column (len<7) — "
                                          "oi_flag unsupported for options on this plan.",
                                "sample_candle": candles[0],
                                "symbol": sym,
                            }
                        guard_checked = True
                    if candles:
                        total_rows += _upsert_oi(conn, sym, underlying, strike,
                                                 otype, exp, candles)
                        contracts += 1
            summary['underlyings'][underlying] = {
                'ltp': ltp, 'strikes': len(strikes),
                'contracts_with_data': contracts, 'bars_upserted': total_rows,
            }

        # 1) self-heal intraday PCR for the now-populated bars
        import pcr_intraday
        intraday = pcr_intraday.compute_pcr_intraday(conn=conn)
        summary['pcr_intraday'] = intraday

        # 2) recompute pcr_daily per gap day
        summary['pcr_daily_filled'] = _recompute_pcr_daily_for_range(conn, sd, ed)
        summary['status'] = 'ok'
        return summary
    finally:
        if own:
            conn.close()


if __name__ == '__main__':
    import sys
    s = sys.argv[1] if len(sys.argv) > 1 else '2026-06-08'
    e = sys.argv[2] if len(sys.argv) > 2 else '2026-06-12'
    print(run_backfill(s, e))
