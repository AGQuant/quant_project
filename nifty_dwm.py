"""
Live-aware Nifty Day/Week/Month % return — shared by Trade Check R1 Market gates
(cc_task #143, 01-Jul-2026).

BUG: raw_prices only carries yesterday's EOD close during market hours (EOD engine
runs ~21:00 IST), so a naive "latest raw_prices row" read reports YESTERDAY's
return labelled as today's. Confirmed live: raw_prices Jun30=23865.75, Jun29=
23946.25 -> Day=-0.34%, while live NIFTY was actually +0.455% (intraday_prices
14:45 bar = 24022.40).

FIX: during market hours (Mon-Fri 09:15-15:30 IST), compute Day/Week/Month off the
latest live intraday_prices tick vs historical anchor closes. Outside market hours
(or if no live tick is available), fall back to the original all-EOD raw_prices
formula unchanged.
"""

from datetime import datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

_MKT_OPEN = time(9, 15)
_MKT_CLOSE = time(15, 30)


def _ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _is_market_hours(now: datetime) -> bool:
    """Weekday + 09:15-15:30 IST, per cc#143 spec (no holiday calendar check)."""
    if now.weekday() >= 5:
        return False
    return _MKT_OPEN <= now.time() <= _MKT_CLOSE


def _eod_fallback(cur, symbol: str):
    """Original all-EOD raw_prices formula -- unchanged, used outside market hours."""
    cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s
                   ORDER BY price_date DESC LIMIT 23""", (symbol,))
    nf = [float(r[0]) for r in cur.fetchall() if r[0] is not None][::-1]
    nf_day = (nf[-1] / nf[-2] - 1) * 100 if len(nf) >= 2 and nf[-2] else None
    nf_wk = (nf[-1] / nf[-6] - 1) * 100 if len(nf) >= 6 and nf[-6] else None
    nf_mo = (nf[-1] / nf[-23] - 1) * 100 if len(nf) >= 23 and nf[-23] else None
    return nf_day, nf_wk, nf_mo, "eod"


def live_nifty_dwm(cur, symbol: str = "NIFTY50"):
    """Returns (nf_day, nf_week, nf_month, source) as percent returns (float|None).
    source = "live_intraday" during market hours with a usable live tick, else "eod".
    """
    now = _ist_now()
    if _is_market_hours(now):
        cur.execute("""
            SELECT close FROM intraday_prices
            WHERE symbol=%s AND timeframe='5m' AND source='fyers_eq'
            ORDER BY ts DESC LIMIT 1
        """, (symbol,))
        row = cur.fetchone()
        latest = float(row[0]) if row and row[0] is not None else None

        if latest is not None:
            today_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
            cur.execute("""
                SELECT close FROM intraday_prices
                WHERE symbol=%s AND timeframe='5m' AND source='fyers_eq'
                  AND ts < %s AND ts::time BETWEEN '09:15' AND '15:30'
                ORDER BY ts DESC LIMIT 1
            """, (symbol, today_open))
            pr = cur.fetchone()
            prev_close = float(pr[0]) if pr and pr[0] is not None else None

            cur.execute("""
                SELECT close FROM raw_prices
                WHERE symbol=%s AND price_date < CURRENT_DATE
                ORDER BY price_date DESC LIMIT 22
            """, (symbol,))
            hist = [float(r[0]) for r in cur.fetchall() if r[0] is not None][::-1]
            # hist[-1] = t-1 (yesterday) ... hist[-k] = t-k trading days before today.
            wk_anchor = hist[-5] if len(hist) >= 5 else None
            mo_anchor = hist[-22] if len(hist) >= 22 else None

            nf_day = (latest / prev_close - 1) * 100 if prev_close else None
            nf_wk = (latest / wk_anchor - 1) * 100 if wk_anchor else None
            nf_mo = (latest / mo_anchor - 1) * 100 if mo_anchor else None
            if nf_day is not None or nf_wk is not None or nf_mo is not None:
                return nf_day, nf_wk, nf_mo, "live_intraday"

    return _eod_fallback(cur, symbol)
