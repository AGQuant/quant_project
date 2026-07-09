"""
price_resolver.py — cc#343: SINGLE source of truth for a symbol's DISPLAY price on the
GVM page (card header, Fibcheck anchor, Pivot Range CMP dot, peers). Root cause it fixes:
RAMCOIND (a non-feed symbol) showed Fibcheck 362.65 (a mid-market PARTIAL raw_prices row)
while the card showed 336.1 (the stale nightly-cached close) — two "prices" for one stock.

Rule (founder 09-Jul):
  * FEED symbol (active futures_universe, the ~212 live-LTP names) -> live CMP (cmp_prices),
    labelled "CMP".
  * NON-FEED symbol -> the LATEST COMPLETED daily close from raw_prices, labelled
    "Prev Close <date>". NEVER an intraday/live price, and NEVER a same-day partial row:
    while the market is open, price_date = CURRENT_DATE is excluded.

Fibcheck anchors on the previous close for EVERY symbol (feed or not) — it is a structural,
end-of-day retracement view, not a live tape — so it calls latest_completed_close() directly.
"""
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN = dt_time(9, 15)
_MARKET_CLOSE = dt_time(15, 30)


def market_open_now() -> bool:
    """True during NSE cash-session wall-clock (Mon-Fri 09:15-15:30 IST). Holidays are not
    checked here — on a holiday there is no CURRENT_DATE partial row to exclude anyway, so
    excluding it is harmless."""
    n = datetime.now(IST)
    return n.weekday() < 5 and _MARKET_OPEN <= n.time() <= _MARKET_CLOSE


def is_feed_symbol(cur, symbol: str) -> bool:
    """A live-feed name = an ACTIVE futures_universe symbol (the ~212 that get a live LTP)."""
    cur.execute("SELECT 1 FROM futures_universe WHERE symbol=%s AND is_active=TRUE LIMIT 1", (symbol,))
    return cur.fetchone() is not None


def latest_completed_close(cur, symbol: str):
    """(price: float|None, date: str|None) of the latest COMPLETED daily close. While the market
    is open, today's row is a mid-session partial and is excluded. This is the Fibcheck anchor
    and the non-feed card price — one definition, so they can never disagree."""
    if market_open_now():
        cur.execute("""SELECT close, price_date FROM raw_prices
                       WHERE symbol=%s AND close IS NOT NULL AND price_date < CURRENT_DATE
                       ORDER BY price_date DESC LIMIT 1""", (symbol,))
    else:
        cur.execute("""SELECT close, price_date FROM raw_prices
                       WHERE symbol=%s AND close IS NOT NULL
                       ORDER BY price_date DESC LIMIT 1""", (symbol,))
    r = cur.fetchone()
    if not r or r[0] is None:
        return None, None
    return float(r[0]), str(r[1])


def resolve_price(cur, symbol: str) -> dict:
    """{price, label, date, is_live}. FEED -> live CMP; NON-FEED -> latest completed close.
    A feed symbol with no cmp_prices row falls back to the completed close (never crashes)."""
    symbol = (symbol or "").upper().strip()
    if is_feed_symbol(cur, symbol):
        cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s LIMIT 1", (symbol,))
        r = cur.fetchone()
        if r and r[0] is not None:
            return {"price": float(r[0]), "label": "CMP", "date": None, "is_live": True}
    px, d = latest_completed_close(cur, symbol)
    return {"price": px, "label": "Prev Close", "date": d, "is_live": False}
