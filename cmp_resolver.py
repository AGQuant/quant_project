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


def _prev_close(cur, symbol, before=None):
    """Last completed daily close STRICTLY before `before` (default: today).

    cc#811: `before` exists because the day move must be measured against the session the PRICE
    belongs to. After 15:30 the live number is the last intraday tick of TODAY, so prev_close is
    yesterday — but for a symbol whose feed died days ago the last tick belongs to an OLDER session,
    and pairing it with yesterday's close invents a move that never happened. Anchoring on the bar's
    own date makes the pair self-consistent whatever the bar's age.
    """
    if before is None:
        cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND price_date < CURRENT_DATE
                       AND close IS NOT NULL ORDER BY price_date DESC LIMIT 1""", (symbol,))
    else:
        cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND price_date < %s
                       AND close IS NOT NULL ORDER BY price_date DESC LIMIT 1""", (symbol, before))
    r = cur.fetchone()
    return _f(r[0]) if r else None


# cc#811: SPOT sources only. 'fyers_fut' is deliberately ABSENT and must never be added back.
# Measured 02-Aug-2026 on the 31-Jul session: all 207 F&O symbols carry an eq bar AND a fut bar at
# the IDENTICAL ts, so the old `source IN ('fyers_eq','fyers_fut') ORDER BY ts DESC LIMIT 1` was a
# coin-flip between spot and the futures premium for every one of them — mean absolute divergence
# 0.388%, max 1.84%. That is the same class of bug cc#367 fixed inside the feed worker for
# cmp_prices; it was still live here, in the resolver cc#811 makes THE display price source.
SPOT_SOURCES = ('fyers_eq', 'fyers_ext', 'fyers_hist')


def _pack(cmp_v, prev, source, ts, live=None):
    cmp_v, prev = _f(cmp_v), _f(prev)
    day_pct = (round((cmp_v - prev) / prev * 100.0, 2)
               if (cmp_v is not None and prev not in (None, 0)) else None)
    return {"cmp": cmp_v, "prev_close": prev, "day_pct": day_pct, "source": source, "ts": ts,
            # cc#811: `live` drives the UI's live-dot. True only when the number came from a tick in
            # the CURRENT session — during market hours that is a live price, and between 15:30 and
            # the nightly job it is today's closing tick, which is still today's truth. A last tick
            # from an older session, a cache hit, or an EOD close are all False: honest, not live.
            "live": bool(live)}


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

    # (1) cc#811: last 5-min SPOT bar, for ANY symbol that has one.
    #
    # Two founder corrections live in this single tier. First, the old code gated it on
    # _is_future(), so only the ~209 F&O names ever reached the live feed and every other symbol
    # fell through to the EOD tail — cc#809 puts the full ~1,800-name universe on the same feed, so
    # the gate is now simply wrong. Second, and more important: this tier does NOT filter to today.
    # After 15:30 it keeps returning the session's FINAL tick, which is exactly the founder's
    # correction — raw_prices must not be used after close, because the Yahoo job only refreshes it
    # near midnight, so from 15:30 until then raw_prices still reads YESTERDAY. The last tick is the
    # honest number in that window, and it is what the V8 dashboard already shows.
    cur.execute("""SELECT close, ts FROM intraday_prices
                   WHERE symbol=%s AND source IN %s AND timeframe='5m' AND close IS NOT NULL
                   ORDER BY ts DESC LIMIT 1""", (symbol, SPOT_SOURCES))
    r = cur.fetchone()
    if r and r[0] is not None:
        bar_date = r[1].date() if hasattr(r[1], "date") else None
        cur.execute("SELECT (NOW() AT TIME ZONE 'Asia/Kolkata')::date")
        today = cur.fetchone()[0]
        is_today = (bar_date == today)
        return _pack(r[0], _prev_close(cur, symbol, before=bar_date),
                     "fyers" if is_today else "last_tick", r[1], live=is_today)

    prev = _prev_close(cur, symbol)

    # (2) cmp_prices cache if < CACHE_MAX_AGE_MIN old
    cur.execute("SELECT cmp, updated_at, source FROM cmp_prices WHERE symbol=%s ORDER BY updated_at DESC LIMIT 1", (symbol,))
    r = cur.fetchone()
    if r and r[0] is not None and r[1] is not None:
        cur.execute("SELECT ((NOW() AT TIME ZONE 'Asia/Kolkata') - %s) < make_interval(mins => %s)",
                    (r[1], CACHE_MAX_AGE_MIN))
        if cur.fetchone()[0]:
            return _pack(r[0], prev, "cache", r[1], live=True)

    # (3) Yahoo on-demand -> write to cmp_prices -> use
    cmp_v = _yahoo_cmp(symbol)
    if cmp_v is not None:
        _cache_upsert(cur, symbol, cmp_v, "yahoo")
        return _pack(cmp_v, prev, "yahoo", None, live=True)

    # (4) STALE — raw_prices last close. cc#811: reached ONLY by symbols with no intraday feed at
    # all, which is the founder's stated boundary for when EOD may be displayed.
    cur.execute("SELECT close, price_date FROM raw_prices WHERE symbol=%s AND close IS NOT NULL "
                "ORDER BY price_date DESC LIMIT 1", (symbol,))
    r = cur.fetchone()
    if r and r[0] is not None:
        return _pack(r[0], _prev_close(cur, symbol, before=r[1]), "STALE", r[1])
    return _pack(None, prev, "STALE", None)


def resolve_cmp_many(cur, symbols):
    """cc#811: BATCH resolver -> {symbol: <same dict as resolve_cmp>}.

    List surfaces are the whole point of this task — the GVM segment list, the V13 screener, sector
    member tables — and they render hundreds to ~1,800 rows. Calling resolve_cmp per row would mean
    4+ round-trips per symbol; at 1,800 rows that is a page that never loads. This does the same
    resolution in a fixed FOUR queries regardless of universe size.

    Deliberately omits the Yahoo tier: it is a one-at-a-time network fetch (the politeness ground
    rule) and has no place in a page render. A symbol that would only have resolved via Yahoo lands
    on its EOD close here and is correctly marked not-live, rather than stalling the request."""
    syms = sorted({(s or "").strip().upper() for s in (symbols or []) if s and str(s).strip()})
    out = {}
    if not syms:
        return out

    cur.execute("SELECT (NOW() AT TIME ZONE 'Asia/Kolkata')::date")
    today = cur.fetchone()[0]

    # (1) last spot 5-min bar per symbol
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, close, ts FROM intraday_prices
                   WHERE symbol = ANY(%s) AND source IN %s AND timeframe='5m' AND close IS NOT NULL
                   ORDER BY symbol, ts DESC""", (syms, SPOT_SOURCES))
    bars = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    # (2) fresh cmp_prices cache
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, cmp, updated_at FROM cmp_prices
                   WHERE symbol = ANY(%s) AND cmp IS NOT NULL AND updated_at IS NOT NULL
                     AND ((NOW() AT TIME ZONE 'Asia/Kolkata') - updated_at) < make_interval(mins => %s)
                   ORDER BY symbol, updated_at DESC""", (syms, CACHE_MAX_AGE_MIN))
    cache = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    # (3) latest EOD close per symbol (the no-feed fallback)
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, close, price_date FROM raw_prices
                   WHERE symbol = ANY(%s) AND close IS NOT NULL
                   ORDER BY symbol, price_date DESC""", (syms,))
    eod = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    # (4) the two candidate prev-closes: before today, and before the latest stored date. Anchoring
    #     per-symbol (see _prev_close) without going back to the DB once per row.
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, close FROM raw_prices
                   WHERE symbol = ANY(%s) AND close IS NOT NULL AND price_date < CURRENT_DATE
                   ORDER BY symbol, price_date DESC""", (syms,))
    prev_before_today = {row[0]: row[1] for row in cur.fetchall()}
    cur.execute("""SELECT DISTINCT ON (r.symbol) r.symbol, r.close FROM raw_prices r
                   JOIN (SELECT symbol, MAX(price_date) AS mx FROM raw_prices
                         WHERE symbol = ANY(%s) AND close IS NOT NULL GROUP BY symbol) m
                     ON m.symbol = r.symbol AND r.price_date < m.mx
                   WHERE r.close IS NOT NULL
                   ORDER BY r.symbol, r.price_date DESC""", (syms,))
    prev_before_eod = {row[0]: row[1] for row in cur.fetchall()}

    for s in syms:
        if s in bars:
            close, ts = bars[s]
            bar_date = ts.date() if hasattr(ts, "date") else None
            is_today = (bar_date == today)
            # A same-session bar pairs with "last close before today"; an older bar has already had
            # its own session closed into raw_prices, so it pairs with the close before THAT date.
            prev = prev_before_today.get(s) if is_today else prev_before_eod.get(s)
            out[s] = _pack(close, prev, "fyers" if is_today else "last_tick", ts, live=is_today)
        elif s in cache:
            cmp_v, upd = cache[s]
            out[s] = _pack(cmp_v, prev_before_today.get(s), "cache", upd, live=True)
        elif s in eod:
            close, pdate = eod[s]
            out[s] = _pack(close, prev_before_eod.get(s), "STALE", pdate)
        else:
            out[s] = _pack(None, None, "STALE", None)
    return out
