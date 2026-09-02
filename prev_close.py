"""prev_close.py — cc#1565 · ONE previous-session close for every "Day change" on the platform.

WHY THIS FILE EXISTS
    /api/v8/live_metrics printed day_pct = cmp / today's OPEN - 1 and every surface labelled it
    "Day Change". On 02-Sep-2026 that read +0.22% for NIFTY50 while Fyers showed -0.72%: the index
    had gapped down at the open, so "since open" was green on a red day. The Market Gate
    (nifty_dwm) anchored on a different bar again — the previous session's last CONTINUOUS 5-min
    bar (15:10), 82 points below the official close that printed in the 15:30 closing auction.
    Two anchors, both wrong, one label. This module is the single anchor both now read.

ORDER OF TRUTH  (first hit wins; basis says which)
    raw_eod      raw_prices close for the last price_date before `before`. The official EOD row.
    auction_bar  no raw row yet (the nightly loader has not run) -> the previous session's 15:30
                 bar in intraday_prices. The closing-auction print IS the official close.
    last_bar     no 15:30 bar either -> the previous session's last spot bar. Honest, not official.
    (None)       nothing at all -> close None. Callers return NULL, never 0: absent beats fabricated.

    "Previous session" is data-derived: the latest intraday date before `before` that has spot
    bars for that symbol. No holiday calendar, no weekday arithmetic, so a holiday cannot skip it.

    Futures prints are excluded at every tier (price_sources.not_fut). At 15:30 a stock carries
    BOTH an auction bar and a fyers_fut bar at the same ts (RELIANCE 01-Sep: 1309.0 vs 1312.2) —
    "regardless of source" in the card means any SPOT source, never the derivatives leg (cc#811).

`before`
    The session the PRICE belongs to, as a date. Defaults to today in IST. live_metrics passes its
    own as_of date so that a weekend or evening read pairs Friday's last tick with THURSDAY's close
    rather than with Friday's own EOD row (cmp_resolver learnt the same lesson in cc#811).
    Today is resolved in SQL — no Python-side tz shift (cc#844).

Driver-agnostic: handed cursors from psycopg2 (nifty_dwm callers) and psycopg3 (v8_endpoints).
Lists bind with = ANY(%s) / <> ALL(%s); a tuple would become a composite under psycopg3 (cc#835).
READ PATH ONLY. Nothing here writes.
"""

from price_sources import not_fut

BASIS_RAW_EOD = "raw_eod"
BASIS_AUCTION = "auction_bar"
BASIS_LAST_BAR = "last_bar"


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def prev_session_close_many(cur, symbols, before=None):
    """{symbol: (close, basis, as_of)} for every symbol asked, in a FIXED two queries.

    as_of is the raw_prices price_date (raw_eod) or the bar ts (auction_bar / last_bar).
    A symbol with nothing on record maps to (None, None, None) — present, so callers need not
    special-case a missing key."""
    syms = sorted({(s or "").strip().upper() for s in (symbols or []) if s and str(s).strip()})
    out = {s: (None, None, None) for s in syms}
    if not syms:
        return out

    # (a) the official EOD row before the session
    cur.execute("""
        SELECT DISTINCT ON (symbol) symbol, close, price_date
        FROM raw_prices
        WHERE symbol = ANY(%s) AND close IS NOT NULL
          AND price_date < COALESCE(%s::date, (NOW() AT TIME ZONE 'Asia/Kolkata')::date)
        ORDER BY symbol, price_date DESC
    """, (syms, before))
    for sym, close, d in cur.fetchall():
        out[sym] = (_f(close), BASIS_RAW_EOD, d)

    missing = [s for s in syms if out[s][0] is None]
    if not missing:
        return out

    # (b)/(c) previous session's 15:30 spot bar, else its last spot bar. One query: the ORDER BY
    # puts the 15:30 bar first when it exists (auction source ahead of any continuous print at the
    # same ts), otherwise the latest bar of that date wins.
    cur.execute("""
        WITH ps AS (
            SELECT symbol, MAX(ts::date) AS d
            FROM intraday_prices
            WHERE symbol = ANY(%s) AND timeframe = '5m' AND close IS NOT NULL
              AND COALESCE(source,'') <> ALL(%s)
              AND ts::date < COALESCE(%s::date, (NOW() AT TIME ZONE 'Asia/Kolkata')::date)
            GROUP BY symbol)
        SELECT DISTINCT ON (ip.symbol) ip.symbol, ip.close, ip.ts, (ip.ts::time = '15:30') AS at_close
        FROM intraday_prices ip
        JOIN ps ON ps.symbol = ip.symbol AND ip.ts::date = ps.d
        WHERE ip.timeframe = '5m' AND ip.close IS NOT NULL
          AND COALESCE(ip.source,'') <> ALL(%s)
        ORDER BY ip.symbol,
                 (ip.ts::time = '15:30') DESC,
                 (COALESCE(ip.source,'') IN ('auction','fyers_eq_auction')) DESC,
                 ip.ts DESC
    """, (missing, not_fut(), before, not_fut()))
    for sym, close, ts, at_close in cur.fetchall():
        out[sym] = (_f(close), BASIS_AUCTION if at_close else BASIS_LAST_BAR, ts)
    return out


def prev_session_close(cur, symbol, before=None):
    """(close, basis, as_of) for one symbol. See prev_session_close_many."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return (None, None, None)
    return prev_session_close_many(cur, [sym], before=before)[sym]


def day_pct(cmp_v, prev):
    """round((cmp/prev - 1) * 100, 2), or None when either side is missing. NULL, never 0."""
    cmp_v, prev = _f(cmp_v), _f(prev)
    if cmp_v is None or not prev:
        return None
    return round((cmp_v / prev - 1) * 100.0, 2)
