"""
fut_fill_price.py — cc#1019 FUT_BOOK_CUTOVER_V1 (founder-locked, session_log 21766).

ONE PLACE THAT ANSWERS "WHAT PRICE DO WE RECORD FOR THIS FILL".

The V8 paper book is a FUTURES book from the 14-Aug-2026 close. The engine still THINKS in cash
prices — qualification, target, stop and every exit trigger read the equity CMP and are untouched
by this file — but the price we WRITE down for a fill is the futures price, because that is the
instrument the book is meant to represent. Decision basis and record basis are two different
things, and this module is the seam between them.

THE LOOKUP
    1. the latest native `fyers_fut` 5-min bar at or before the fill moment, within
       STALENESS_MIN minutes;
    2. failing that, the same read against `fyers_fut_rest` (the REST fallback bars written by
       fut_rest_fallback.py when the websocket leg goes dark);
    3. failing both, the EQUITY price the caller already has — recorded, but NEVER silently: it
       goes out as a WARNING naming the symbol, the moment and how stale the newest fut bar was.

"At or before", not "at or after". The recorded fill is the last futures price that actually
existed when the engine acted. A bar stamped after the fill is a price the fill never saw — the
class of error v8_futures_book.py's ENTRY_LAG_TOLERANCE_MIN exists to reject.

WHY EXITS ASK ABOUT THE ENTRY FIRST
    P&L is a subtraction, so both legs must be the same instrument. Two positions (NTPC 29-Jul,
    INDUSINDBK 05-Aug) were entered before the fut feed existed — no fut bar at their entry, and
    inventing one is forbidden (session_log 21766 item 3). Their entry_price is a cash price, so
    their exit must be recorded as a cash price too, or the trade would book a basis difference as
    profit. `entry_is_fut_priced()` is that test, and it is asked of the DATA (is there a fut bar
    at that entry?) rather than of a date constant, so it stays right when the feed's history
    changes underneath it.

TRAPS
  * intraday_prices.ts, v8_paper_positions.entry_ts and v8_paper_trades.exit_ts are ALL naive IST.
    No timezone conversion happens here (the cc#844 phantom-330-minute class).
  * Read-only. This module never writes, never commits and never rolls back — it is called from
    inside the caller's open transaction, immediately before that caller's own INSERT.
  * Every failure returns the equity price rather than raising. A pricing lookup must never be the
    reason a fill goes unrecorded.
"""

import logging
from datetime import timedelta

log = logging.getLogger("scorr.fut_fill_price")

FUT_SOURCE = "fyers_fut"            # native websocket bars
FUT_REST_SOURCE = "fyers_fut_rest"  # REST fallback bars (fut_rest_fallback.py)

# How old the newest fut bar may be and still be "the price at this fill". Bars are 5-minute
# buckets stamped at bucket START, so a live fill lands 0-5 minutes after its own bar; 10 leaves
# room for one skipped bucket and nothing more. Beyond that we are quoting a price the market has
# already moved away from, and the honest answer is the loud equity fallback.
STALENESS_MIN = 10


def _bar_at(cur, symbol, ts, source):
    """Latest `source` close at or before ts. Returns (close, bar_ts) or (None, None).

    The staleness bound is applied in SQL so the index does the work and a symbol with no recent
    bar costs one lookup, not a scan back through the whole series.
    """
    cur.execute("""
        SELECT close, ts FROM intraday_prices
        WHERE symbol = %s AND source = %s AND ts <= %s AND ts >= %s
        ORDER BY ts DESC LIMIT 1
    """, (symbol, source, ts, ts - timedelta(minutes=STALENESS_MIN)))
    row = cur.fetchone()
    if not row or row[0] is None:
        return None, None
    return float(row[0]), row[1]


def _newest_bar_age_min(cur, symbol, ts):
    """How stale the newest fut bar is, in minutes — for the warning line only. None if the symbol
    has no fut bars at all, which is a different (and worth saying) situation from a late one."""
    try:
        cur.execute("""
            SELECT ts FROM intraday_prices
            WHERE symbol = %s AND source IN (%s, %s) AND ts <= %s
            ORDER BY ts DESC LIMIT 1
        """, (symbol, FUT_SOURCE, FUT_REST_SOURCE, ts))
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        return round((ts - row[0]).total_seconds() / 60.0, 1)
    except Exception:
        return None


def fill_price(conn, symbol, ts, eq_price, what="fill"):
    """The price to RECORD for a fill of `symbol` at `ts`.

    Returns (price, basis) where basis is 'fut', 'fut_rest' or 'eq'. `eq_price` is what the caller
    would have written before this card — it is both the fallback and the proof that this function
    can never lose a fill.
    """
    if eq_price is None or ts is None or not symbol:
        return eq_price, "eq"
    try:
        with conn.cursor() as cur:
            px, bar_ts = _bar_at(cur, symbol, ts, FUT_SOURCE)
            if px is not None:
                return round(px, 2), "fut"
            px, bar_ts = _bar_at(cur, symbol, ts, FUT_REST_SOURCE)
            if px is not None:
                log.info("cc#1019 %s %s: fut REST fallback bar %s used for the %s price",
                         symbol, ts, bar_ts, what)
                return round(px, 2), "fut_rest"
            age = _newest_bar_age_min(cur, symbol, ts)
    except Exception as e:
        log.warning("cc#1019 %s: fut price lookup failed (%s) — recording the EQUITY %s price "
                    "%s. THIS ROW IS EQ-BASIS.", symbol, e, what, eq_price)
        return eq_price, "eq"
    log.warning("cc#1019 %s at %s: NO futures bar within %d min (%s) — recording the EQUITY %s "
                "price %s. THIS ROW IS EQ-BASIS, not futures-priced.",
                symbol, ts, STALENESS_MIN,
                "no fut bar for this symbol at all" if age is None else f"newest is {age} min old",
                what, eq_price)
    return eq_price, "eq"


def entry_is_fut_priced(conn, symbol, entry_ts):
    """Was this position's entry recorded on the futures basis?

    Asked of the data — does a fut bar exist at that entry, inside the same staleness window the
    fill used — so the two legs of a trade can never end up on different instruments. A position
    opened before the fut feed existed (NTPC, INDUSINDBK) answers False and keeps a cash exit.
    """
    if not symbol or entry_ts is None:
        return False
    try:
        with conn.cursor() as cur:
            for src in (FUT_SOURCE, FUT_REST_SOURCE):
                px, _ = _bar_at(cur, symbol, entry_ts, src)
                if px is not None:
                    return True
    except Exception as e:
        log.warning("cc#1019 %s: entry-basis check failed (%s) — treating the entry as EQ, which "
                    "keeps both legs of the trade on one basis.", symbol, e)
    return False


def exit_price(conn, symbol, entry_ts, exit_ts, eq_exit):
    """The price to RECORD for an exit. Same basis as the entry, always.

    Returns (price, basis). The exit TRIGGER is not this function's business — target/SL were
    already evaluated on the cash price by the caller and are untouched; this only decides what
    number is written into v8_paper_trades.exit_price.
    """
    if not entry_is_fut_priced(conn, symbol, entry_ts):
        return eq_exit, "eq"
    return fill_price(conn, symbol, exit_ts, eq_exit, what="exit")
