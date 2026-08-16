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

import pcr_guard   # cc#1061: one definition of an impossible PCR

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


def _upsert_oi(conn, sym, underlying, strike, otype, exp, candles, force_oi=False):
    """UPSERT each 5-min bar's OI onto option_chain (matches live schema).

    cc#1057 — THE OI COLUMN NO LONGER CLOBBERS A LIVE-CAPTURED BAR BY DEFAULT.

    This function used to end in a bare `oi = EXCLUDED.oi` while `ltp` and `volume` were both
    protected with COALESCE(existing, incoming). Two columns were guarded and the third, the
    only one this job exists to fill, was not. The result: a backfill over a window the live
    feed had ALREADY captured replaced a real per-tick OI path with the History API's much
    coarser one, silently and in place.

    MEASURED 16-Aug-2026, and this is what settled it against the competing "the OI feed just
    froze" explanation: 681 of 710 pcr_intraday rows across 10-14 Aug carry put/call OI totals
    that no longer match option_chain at the SAME timestamp. If the live feed had merely
    stalled, the two would agree — both would hold the same frozen number, because
    pcr_intraday is computed FROM option_chain at capture time. They disagree on 96% of rows,
    so option_chain was rewritten after those rows were derived from it. Downstream effect:
    NIFTY 13-Aug now holds 3 distinct OI snapshots across 77 bars, 12-Aug holds 4 across 62.
    A 5-minute series that is really a daily one, which reads on a chart as a calm market.

    Consequence worth stating plainly: pcr_intraday is now the ONLY surviving record of the
    real intraday OI path for that window. Recomputing it from option_chain would not repair
    it — it would finish destroying it.

    DEFAULT IS PRESERVE, `force_oi=True` IS THE DELIBERATE OVERWRITE. The repair capability is
    kept, because a genuinely corrupt live OI (cc#591's zeroed put leg, cc#745's partial
    capture) is exactly what this job should be able to fix. It just stops being the accident
    that happens when someone backfills a window that was never actually missing.
    """
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
                    oi = """ + ("EXCLUDED.oi" if force_oi
                                 else "COALESCE(option_chain.oi, EXCLUDED.oi)") + """,
                    ltp = COALESCE(option_chain.ltp, EXCLUDED.ltp),
                    volume = COALESCE(option_chain.volume, EXCLUDED.volume)
            """, (sym, underlying, strike, otype, exp,
                  c[4], oi, int(c[5]) if c[5] is not None else None, ts))
            rows += 1
    conn.commit()
    return rows


# cc#745: PCR data-integrity sanity gate. The 27-Jul BANKNIFTY row captured put_oi=38,400 against a
# ~12M trailing median (partial-strike / expiry-roll capture), giving a false PCR 0.002 that reached the
# digest. A reading is SUSPECT when a leg's OI deviates >10x from its trailing-5-session median (the
# precise detector — it catches the collapse without false-flagging genuinely low-PCR sessions like the
# early-June 0.28 band, which have consistent OI), OR PCR is grossly implausible (<0.05 / >6.0 backstop
# for the <3-history case). Flagged rows are MARKED, never deleted — the digest + mood-gate exclude them.
_PCR_QUALITY_NOTE = ("cc#745 sanity gate: put/call OI deviates >10x from the trailing-5-session median "
                     "(or PCR outside [0.05,6.0]) — capture suspect, excluded from trend/mood reads")

_PCR_SUSPECT_COND = """
  p.pcr IS NOT NULL AND (
    p.pcr < 0.05 OR p.pcr > 6.0
    OR (
      (SELECT COUNT(*) FROM pcr_daily h WHERE h.underlying=p.underlying AND h.price_date < p.price_date) >= 3
      AND (
        p.put_oi  < 0.1 * COALESCE((SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY q.put_oi)
          FROM (SELECT put_oi FROM pcr_daily y WHERE y.underlying=p.underlying AND y.price_date < p.price_date ORDER BY y.price_date DESC LIMIT 5) q), p.put_oi)
        OR p.put_oi  > 10 * COALESCE((SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY q.put_oi)
          FROM (SELECT put_oi FROM pcr_daily y WHERE y.underlying=p.underlying AND y.price_date < p.price_date ORDER BY y.price_date DESC LIMIT 5) q), p.put_oi)
        OR p.call_oi < 0.1 * COALESCE((SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY q.call_oi)
          FROM (SELECT call_oi FROM pcr_daily y WHERE y.underlying=p.underlying AND y.price_date < p.price_date ORDER BY y.price_date DESC LIMIT 5) q), p.call_oi)
        OR p.call_oi > 10 * COALESCE((SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY q.call_oi)
          FROM (SELECT call_oi FROM pcr_daily y WHERE y.underlying=p.underlying AND y.price_date < p.price_date ORDER BY y.price_date DESC LIMIT 5) q), p.call_oi)
      )
    )
  )
"""


def ensure_pcr_quality_cols(conn):
    """cc#745: app-side idempotent add of the quality marker columns (never via the run_sql MCP path,
    which hard-blocks ALTER)."""
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE pcr_daily ADD COLUMN IF NOT EXISTS quality TEXT DEFAULT 'ok'")
        cur.execute("ALTER TABLE pcr_daily ADD COLUMN IF NOT EXISTS quality_note TEXT")
    conn.commit()


def mark_pcr_quality(conn):
    """cc#745: (re)mark EVERY pcr_daily row 'ok'/'suspect' from the sanity gate. Idempotent + cheap
    (small table) so writers call it after each insert and it also serves as the one-time history
    backfill. Only sets the marker + note — never touches put_oi/call_oi/pcr (historical integrity)."""
    ensure_pcr_quality_cols(conn)
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE pcr_daily p SET
              quality      = CASE WHEN {_PCR_SUSPECT_COND} THEN 'suspect' ELSE 'ok' END,
              quality_note = CASE WHEN {_PCR_SUSPECT_COND} THEN %s ELSE NULL END
        """, (_PCR_QUALITY_NOTE,))
        n = cur.rowcount
        cur.execute("SELECT COUNT(*) FROM pcr_daily WHERE quality='suspect'")
        suspect = cur.fetchone()[0]
    conn.commit()
    return {"rows_marked": n, "suspect_total": suspect}


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
                    -- cc#1061: NULL, never an impossible number. See pcr_guard.py.
                    CASE WHEN LEAST(SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END),SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END))::numeric / NULLIF(GREATEST(SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END),SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END)),0) < 0.01 THEN NULL ELSE ROUND(SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END)::numeric / NULLIF(SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END),0), 3) END
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
    mark_pcr_quality(conn)   # cc#745: flag any implausible captures in the just-written range
    with conn.cursor() as _c:                       # cc#1061: name every day the guard nulled
        pcr_guard.warn_nulled(_c, source="_recompute_pcr_daily_for_range")
    return filled


def run_backfill(start: str, end: str, conn=None, force_oi: bool = False):
    """
    Main entry. start/end = 'YYYY-MM-DD' (inclusive).
    Backfills OI for NIFTY+BANKNIFTY ATM+/-10 monthly options, then recomputes
    pcr_intraday (self-heal) + pcr_daily for the range.

    force_oi (cc#1057): default False PRESERVES any OI already on the bar and fills only the
    gaps, so backfilling a window the live feed already covered can no longer overwrite the
    real per-tick series with the History API's coarser one. Pass True only to deliberately
    repair a known-bad live capture.
    """
    own = conn is None
    if own:
        conn = _conn()
    try:
        token = _load_token(conn)
        sd = datetime.strptime(start, '%Y-%m-%d').date()
        ed = datetime.strptime(end, '%Y-%m-%d').date()
        exp = _current_expiry(ed)   # monthly series active across the gap window

        summary = {'expiry': _expiry_code(exp), 'underlyings': {}, 'oi_guard': 'ok',
                   # cc#1057: say which way the OI column was written, in the response.
                   # A backfill that quietly replaced real data is how this was missed.
                   'oi_mode': 'overwrite (force_oi)' if force_oi else 'preserve existing (cc#1057 default)'}
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
                                                 otype, exp, candles, force_oi=force_oi)
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
