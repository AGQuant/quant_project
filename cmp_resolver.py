"""cmp_resolver.py — cc#717 / TC_INTRADAY_TRUTH_V2 part_1 (spec id=9973, locked 28-Jul-2026).

ONE shared live-price resolver for the Trade Check intraday engine. Every rule that needs the day's
move must read the SAME live number through this helper — never the ~09:15-frozen v8_metrics.day_1d
(root cause id=9973: v8_metrics.day_1d showed PETRONET -0.58% while raw_prices showed +1.95% live).

    resolve_cmp(cur, symbol) -> {"cmp", "prev_close", "day_pct", "source", "ts"}

Priority (first hit wins):
  1. "fyers"  — last 5-min bar (intraday_prices, source fyers_eq/fyers_fut, timeframe 5m) for symbols
                in the ACTIVE futures universe. Freshest live feed.
  2. "cache"  — cmp_prices row whose updated_at is < 15 min old (the 15-min session cache that makes
                subsequent cards in the same segment instant).
  3. "yahoo"  — on-demand via yahoo_ondemand.fetch_intraday (ONE-AT-A-TIME per the ground rule), then
                UPSERT into cmp_prices so the next lookup is a cache hit.
  4. "STALE"  — raw_prices last close, flagged so callers can badge it (nothing live available).

prev_close = last raw_prices close STRICTLY before today, so day_pct is a true intraday move.
This module is deliberately STANDALONE and side-effect-light (only the cmp_prices cache upsert) so it
can be imported by both the single-symbol loader (tc_v4_dual) and the batch scanner (tc_v4_scan)
without pulling in either — preserving the SHARED-MODULE CONTRACT (scanner score == single score).
"""
import threading
import logging

log = logging.getLogger("cmp_resolver")

CACHE_MAX_AGE_MIN = 15
_yahoo_lock = threading.Lock()   # one-at-a-time Yahoo politeness (ground rule)


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _prev_close(cur, symbol):
    cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND price_date < CURRENT_DATE
                   ORDER BY price_date DESC LIMIT 1""", (symbol,))
    r = cur.fetchone()
    return _f(r[0]) if r else None


def _is_future(cur, symbol):
    cur.execute("SELECT 1 FROM futures_universe WHERE UPPER(symbol)=UPPER(%s) AND is_active=TRUE", (symbol,))
    return cur.fetchone() is not None


def _pack(cmp_v, prev, source, ts):
    cmp_v, prev = _f(cmp_v), _f(prev)
    day_pct = (round((cmp_v - prev) / prev * 100.0, 2)
               if (cmp_v is not None and prev not in (None, 0)) else None)
    return {"cmp": cmp_v, "prev_close": prev, "day_pct": day_pct, "source": source, "ts": ts}


def _cache_upsert(cur, symbol, cmp_v, source):
    """UPSERT without assuming a unique constraint on cmp_prices.symbol (UPDATE, else INSERT)."""
    try:
        cur.execute("""UPDATE cmp_prices SET cmp=%s, updated_at=(NOW() AT TIME ZONE 'Asia/Kolkata'),
                       source=%s WHERE symbol=%s""", (cmp_v, source, symbol))
        if cur.rowcount == 0:
            cur.execute("""INSERT INTO cmp_prices (symbol, cmp, updated_at, source)
                           VALUES (%s, %s, (NOW() AT TIME ZONE 'Asia/Kolkata'), %s)""",
                        (symbol, cmp_v, source))
    except Exception as e:
        log.warning(f"cmp_prices upsert {symbol}: {e}")


def _yahoo_cmp(symbol):
    """Last close from a one-at-a-time Yahoo intraday pull. Returns float or None (never raises)."""
    try:
        import yahoo_ondemand
        with _yahoo_lock:
            bars = yahoo_ondemand.fetch_intraday(symbol, days=2, interval="5m")
        if bars:
            return _f(bars[-1].get("close"))
    except Exception as e:
        log.warning(f"resolve_cmp yahoo {symbol}: {e}")
    return None


def resolve_cmp(cur, symbol):
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return _pack(None, None, "STALE", None)
    prev = _prev_close(cur, symbol)

    # (1) FYERS last 5-min bar — futures universe only
    if _is_future(cur, symbol):
        cur.execute("""SELECT close, ts FROM intraday_prices
                       WHERE symbol=%s AND source IN ('fyers_eq','fyers_fut') AND timeframe='5m'
                       ORDER BY ts DESC LIMIT 1""", (symbol,))
        r = cur.fetchone()
        if r and r[0] is not None:
            return _pack(r[0], prev, "fyers", r[1])

    # (2) cmp_prices cache if < CACHE_MAX_AGE_MIN old
    cur.execute("SELECT cmp, updated_at, source FROM cmp_prices WHERE symbol=%s ORDER BY updated_at DESC LIMIT 1", (symbol,))
    r = cur.fetchone()
    if r and r[0] is not None and r[1] is not None:
        cur.execute("SELECT ((NOW() AT TIME ZONE 'Asia/Kolkata') - %s) < make_interval(mins => %s)",
                    (r[1], CACHE_MAX_AGE_MIN))
        if cur.fetchone()[0]:
            return _pack(r[0], prev, "cache", r[1])

    # (3) Yahoo on-demand -> write to cmp_prices -> use
    cmp_v = _yahoo_cmp(symbol)
    if cmp_v is not None:
        _cache_upsert(cur, symbol, cmp_v, "yahoo")
        return _pack(cmp_v, prev, "yahoo", None)

    # (4) STALE — raw_prices last close
    cur.execute("SELECT close, price_date FROM raw_prices WHERE symbol=%s ORDER BY price_date DESC LIMIT 1", (symbol,))
    r = cur.fetchone()
    if r and r[0] is not None:
        return _pack(r[0], prev, "STALE", r[1])
    return _pack(None, prev, "STALE", None)
