"""index_heal.py — cc#1200 scope 1: heal the NIFTY50 / BANKNIFTY CASH legs.

THE DEFECT THIS EXISTS FOR, in one line of main.py:

    syms = [s for s in syms if s not in ("NIFTY","BANKNIFTY","NIFTY50","FINNIFTY", ...)]

_heal_morning_gaps drops every index symbol before it starts. So the 15:40 heal repairs 200-odd
equities and has never once repaired an index. On 21-Aug-2026 that is exactly what happened: the
job healed 206 equities and 0 index cash legs, and NIFTY50/BANKNIFTY ended the day with no
source='fyers_eq' bars at all while both futures legs had 77. The gap then surfaced three
different ways on the dashboard — a +0.00% day change, a spine spot labelled LIVE on a Thursday
bar, and "1 day no data" on the PCR trend — none of which said "as of Thursday".

WHY THIS IS A SEPARATE MODULE AND NOT THREE LINES INSIDE _heal_morning_gaps.

1. main.py is WIRING ONLY (rule 4). _heal_morning_gaps is already logic living there; adding an
   index branch would deepen a violation rather than pay it down. New behaviour gets its own file
   (rule 5).
2. The equity heal CANNOT be reused for indices even if the exclusion were lifted, and the reason
   is a single clause in _yahoo_1m_today:

       if op is None or hi is None or lo is None or cl is None or not vol: continue

   `not vol` drops every bar with zero or null volume. Yahoo reports no volume for ^NSEI and
   ^NSEBANK on most 1-minute bars, so lifting the exclusion alone would have produced a heal that
   ran, reported success, and inserted nothing. A job that looks healthy while writing zero rows
   is the exact failure cc#1194 spent a day on; it is not worth re-creating here.

SO THIS USES THE PATH THAT DEMONSTRABLY WORKS FOR INDICES: yahoo_index_backfill._fetch_one, the
same fetch that put 376 real 1-minute bars per index into the table for 21-Aug. Nothing about
index fetching is re-implemented here.

THE RESAMPLER IS IMPORTED, NEVER COPIED. _resample_1m_to_5m is the one 1m->5m aggregation in this
codebase and it stays that way — a second copy would drift the first time a bucket rule changed.
It is imported lazily inside the function for the same reason scheduler.py defers its own
`import main`: at module load the two would form a cycle.

SOURCE TAG. Bars are written source='yahoo', timeframe='5m' — the honest name for where they came
from, and the same tag the 5-minute index rows already in the table carry, so this adds no third
parallel source. That tag is SAFE for the tape by construction: index_tape.py filters with
price_sources.not_fut(), an EXCLUSION of the two futures sources rather than an allow-list, and
its own header says a new cash source should join the tape automatically. Verified against
FUT_SOURCES = ("fyers_fut", "fyers_fut_rest") — 'yahoo' is not in it.

ON CONFLICT DO NOTHING throughout: a real bar is never overwritten by a healed one.
"""

import logging
import os
from datetime import date as _date, datetime, timedelta

import psycopg

log = logging.getLogger("scorr.index_heal")

# The two cash indices the dashboard actually renders. Deliberately NOT every index in
# INDEX_YSYM — FINNIFTY and the rest have no cash tile, and healing something nothing reads is
# how a table grows without anyone being able to say why.
INDEX_SYMBOLS = ("NIFTY50", "BANKNIFTY")

# The session, in IST. Bars outside it are dropped rather than trusted: Yahoo returns pre-open and
# post-close prints for indices and they are not part of the 09:15-15:30 series.
SESSION_OPEN = (9, 15)
SESSION_CLOSE = (15, 30)

_INSERT_SQL = """
    INSERT INTO intraday_prices
        (symbol, ts, open, high, low, close, volume, timeframe, source)
    VALUES (%s, %s, %s, %s, %s, %s, %s, '5m', 'yahoo')
    ON CONFLICT (symbol, ts, timeframe, source) DO NOTHING
"""

# What counts as "already covered". The cash leg is ANY non-futures source, because a day healed
# by this job (yahoo) is just as complete as one the live feed wrote (fyers_eq) — asking only for
# fyers_eq would re-heal every previously healed day for ever.
_COVER_SQL = """
    SELECT COUNT(*) FROM intraday_prices
     WHERE symbol = %s AND ts::date = %s AND timeframe = '5m'
       AND COALESCE(source, '') <> ALL(%s)
"""

# A full session is 09:15..15:25 inclusive on 5-minute buckets = 75 slots, and the live feed
# writes 72-76 depending on the auction tail. The bar is set at 60 rather than 75 so a session
# that genuinely closed early is not re-fetched on every run; below 60 the day is a real gap.
MIN_COMPLETE_BARS = 60


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL", ""))


def heal_index_cash(day=None, conn=None, force=False):
    """Fill missing 5m CASH bars for the index symbols on `day` (default: today, IST-naive).

    Returns {"symbols": {...}, "healed_index": n, "bars": n, "skipped": [...], "errors": [...]}
    so the caller can log healed_index SEPARATELY from the equity count — the card asks for that
    explicitly, and a single combined number is what let 0 healed indices hide behind 206 healed
    equities on 21-Aug.

    `force` re-fetches even when the day looks complete. Off by default: the point of the coverage
    check is that a clean session makes zero Yahoo calls.
    """
    from price_sources import not_fut          # cc#1053: cash leg = anything not futures

    day = day or _date.today()
    own = conn is None
    c = _conn() if own else conn
    out = {"day": str(day), "symbols": {}, "healed_index": 0, "bars": 0,
           "skipped": [], "errors": []}
    try:
        for sym in INDEX_SYMBOLS:
            try:
                with c.cursor() as cur:
                    cur.execute(_COVER_SQL, (sym, day, not_fut()))
                    have = cur.fetchone()[0] or 0
                if have >= MIN_COMPLETE_BARS and not force:
                    out["skipped"].append("%s (%d bars)" % (sym, have))
                    out["symbols"][sym] = {"had": have, "written": 0}
                    continue

                rows = _fetch_5m(sym, day)
                if not rows:
                    # An empty fetch is REPORTED, never silently counted as a heal. This is the
                    # state the volume filter in the equity path would have produced on every
                    # single run, and it must be visible rather than look like a clean day.
                    out["errors"].append("%s: yahoo returned no usable bars for %s" % (sym, day))
                    out["symbols"][sym] = {"had": have, "written": 0}
                    continue

                with c.cursor() as cur:
                    cur.executemany(_INSERT_SQL, [(sym,) + r for r in rows])
                    written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                c.commit()
                out["symbols"][sym] = {"had": have, "written": written, "fetched": len(rows)}
                out["bars"] += written
                if written:
                    out["healed_index"] += 1
            except Exception as e:                     # one bad index must not stop the other
                try:
                    c.rollback()
                except Exception:
                    pass
                out["errors"].append("%s: %s" % (sym, str(e)[:140]))
                log.warning("index_heal %s: %s", sym, e)
    finally:
        if own:
            c.close()
    log.info("index_heal %s: healed_index=%d bars=%d skipped=%s errors=%s",
             day, out["healed_index"], out["bars"], out["skipped"], out["errors"][:2])
    return out


def _fetch_5m(sym, day):
    """Yahoo 1m for `sym`, restricted to `day`'s session, aggregated to 5m buckets.

    Returns [(ts, o, h, l, c, v), ...] ready for the insert, or [] if nothing usable came back.
    """
    import yahoo_index_backfill as yib
    from main import _resample_1m_to_5m         # deferred: module-level would cycle via main

    # days=7 rather than 1: Yahoo's intraday window is relative to now, and asking for a single
    # day around a weekend or a holiday returns an empty frame. Over-fetching is free here — the
    # session filter below discards everything that is not the day asked for.
    raw = yib._fetch_one(sym, days=7) or []
    lo = datetime(day.year, day.month, day.day, *SESSION_OPEN)
    hi = datetime(day.year, day.month, day.day, *SESSION_CLOSE)

    windowed = []
    for cd in raw:
        ts = cd.get("ts") if isinstance(cd, dict) else cd[0]
        if ts is None or not (lo <= ts <= hi):
            continue
        if isinstance(cd, dict):
            o, h, l, cl = cd.get("open"), cd.get("high"), cd.get("low"), cd.get("close")
            v = cd.get("volume") or 0
        else:
            o, h, l, cl, v = cd[1], cd[2], cd[3], cd[4], (cd[5] if len(cd) > 5 else 0)
        # VOLUME IS NOT REQUIRED, and that is the whole reason this function exists rather than
        # a call to _yahoo_1m_today. An index has no traded volume of its own; demanding one
        # discards the entire series. Price completeness is still required — a bar missing any
        # of OHLC is dropped.
        if None in (o, h, l, cl):
            continue
        windowed.append((ts, float(o), float(h), float(l), float(cl), int(v or 0)))

    if not windowed:
        return []
    return _resample_1m_to_5m(windowed)
