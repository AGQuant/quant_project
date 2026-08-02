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
    """{price, label, date, is_live}. cc#811: this now DELEGATES to cmp_resolver.resolve_cmp.

    Two resolvers with different rules had grown side by side — cc#343's here (feed symbol ->
    cmp_prices, everyone else -> last completed close) and cc#717's cmp_resolver (intraday bar ->
    cache -> yahoo -> EOD). Same question, two answers, so the GVM card and a Trade Check card could
    legitimately print different prices for the same stock at the same moment. cc#811 names the
    cc#717 resolver as THE display price source, so this becomes a thin adapter over it rather than
    a second opinion. The cc#343 contract (the dict shape and the "Prev Close <date>" label for
    anything not live) is preserved for existing callers.

    The behavioural change callers will see is the one cc#811 asked for: a non-F&O symbol with an
    intraday feed now shows its live/last tick instead of an EOD close, and after 15:30 nothing
    falls back to raw_prices while that table still reads yesterday.
    """
    symbol = (symbol or "").upper().strip()
    try:
        import cmp_resolver
        r = cmp_resolver.resolve_cmp(cur, symbol)
        if r.get("cmp") is not None:
            if r.get("live"):
                return {"price": float(r["cmp"]), "label": "CMP", "date": None, "is_live": True}
            ts = r.get("ts")
            return {"price": float(r["cmp"]),
                    "label": "Last Tick" if r.get("source") == "last_tick" else "Prev Close",
                    "date": str(ts)[:10] if ts else None, "is_live": False}
    except Exception:
        pass   # resolver unavailable/failed -> the original cc#343 path below, never a broken card
    px, d = latest_completed_close(cur, symbol)
    return {"price": px, "label": "Prev Close", "date": d, "is_live": False}
