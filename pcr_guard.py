"""
pcr_guard.py — cc#1061 PCR DAILY SANITY GUARD
=============================================
One definition of "this PCR is not a real reading", shared by every writer of pcr_daily.

WHY THIS EXISTS. cc#745 already built a detector for a destroyed option leg and it WORKED — it
marked both bad rows quality='suspect' and wrote an explanatory note. What it did not do was
stop the impossible number being stored in `pcr`. Every chart reads `pcr`, not `quality`, so a
put leg that had collapsed to 5,265 against 53,179,620 calls rendered as PCR 0.000: a deep V in
the NIFTY trend line that looked like a real market event and was not. Detecting a fault and
then publishing it anyway is worse than not detecting it, because the flag reassures the person
reading the chart while the chart lies to them.

THE RULE: LOUD ABSENCE OVER QUIET GARBAGE. When one leg is missing or has collapsed relative to
the other, pcr is NULL and the surface draws a GAP. The raw put_oi/call_oi are still stored, so
the corruption stays visible to forensics — it just stops being charted as a market reading.

Restated under this card (16-Aug-2026), both already quality='suspect' and both still charted:
    2026-08-12 NIFTY      put 5,265      call 53,179,620   pcr 0.000 -> NULL
    2026-07-27 BANKNIFTY  put 38,400     call 18,688,020   pcr 0.002 -> NULL
The other 149 flagged rows in the table already held pcr NULL and were left untouched — they are
stock underlyings whose options are not stored (index-only scope), and NULL is already honest.

Neither true value is recoverable: option_chain's own put leg is destroyed for those bars.
"""

import logging

log = logging.getLogger("scorr.pcr_guard")

# A leg smaller than this fraction of the other is a broken capture, not a market. NIFTY and
# BANKNIFTY PCR live in roughly 0.5-2.0; the observed corruptions were 0.0001 and 0.002, i.e.
# two orders of magnitude below anything real, so this threshold is nowhere near a genuine
# reading. Deliberately ONE constant — the SQL below is built from it and cannot drift.
MIN_LEG_RATIO = 0.01


def guard_sql(put_expr: str, call_expr: str) -> str:
    """The pcr expression, guarded. Substitutes into any writer that already computes the two
    OI sums, so the three pcr_daily writers cannot disagree about what counts as impossible.

    Returns NULL when either leg is missing or has collapsed against the other; otherwise the
    same ROUND(put/call, 3) those writers always produced. Both legs zero also yields NULL,
    via the NULLIF on the denominator.
    """
    return (
        "CASE WHEN LEAST({p},{c})::numeric / NULLIF(GREATEST({p},{c}),0) < {r}"
        " THEN NULL ELSE ROUND({p}::numeric / NULLIF({c},0), 3) END"
    ).format(p=put_expr, c=call_expr, r=MIN_LEG_RATIO)


def warn_nulled(cur, price_date=None, source="pcr_daily"):
    """Log every row the guard just nulled, naming date and underlying. A guard that silently
    swallows a day is how a dead feed goes unnoticed for a week — the whole point is that the
    absence is LOUD. Returns the list of (price_date, underlying, put_oi, call_oi) it warned on.

    price_date=None checks the whole table (used after a range backfill); pass a date to scope
    it to one day (used by the nightly writer).
    """
    try:
        sql = ("SELECT price_date, underlying, put_oi, call_oi FROM pcr_daily "
               "WHERE pcr IS NULL AND COALESCE(put_oi,0) + COALESCE(call_oi,0) > 0")
        params = ()
        if price_date is not None:
            sql += " AND price_date = %s"
            params = (price_date,)
        sql += " ORDER BY price_date, underlying"
        cur.execute(sql, params)
        rows = cur.fetchall()
        for d, ul, p, c in rows:
            log.warning(
                "cc#1061 PCR GUARD: %s %s stored as NULL — one leg collapsed "
                "(put_oi=%s call_oi=%s, ratio below %s). Charted as a GAP, never as a value.",
                d, ul, p, c, MIN_LEG_RATIO)
        return rows
    except Exception as e:
        # A logging helper must never be the thing that breaks a write path.
        log.warning("cc#1061 warn_nulled(%s) failed: %s", source, e)
        return []
