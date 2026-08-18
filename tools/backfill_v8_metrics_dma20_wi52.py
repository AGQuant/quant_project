"""
tools/backfill_v8_metrics_dma20_wi52.py — cc#1097, the record of a backfill that has already run
=================================================================================================
WHAT THIS IS. The exact statements used to fill v8_metrics.dma_20 and v8_metrics.week_index_52 on
18-Aug-2026, committed so the operation is reproducible and auditable rather than living only in a
chat log. It is written to be safe to re-run: every UPDATE carries an IS NULL predicate, so a
second run fills nothing that a first run already filled.

WHY IT EXISTED. Fable could not run the founder-requested 12-month sell_reversal replay. Both
columns were 100% NULL before May-2026 — 46,463 and 46,291 rows — and both are HARD GATES in
SELL_REVERSAL_SPEC_V6_R1FALL (session_log 5626, filters 3 and 5). Every candidate before May-2026
failed on missing data, so the candidate set silently collapsed to the last three months and any
result labelled "1 year" would have been a 3-month test wearing a 1-year label. That is
session_log 23043 again, where a v8_metrics backfill covered 93 of 209 symbols and every backtest
after it was quietly a 45%-universe test until the founder caught it on a trade-count contradiction.

THE THREE RULES THIS FILE ENFORCES, all from the card:
  1. FILL NULLS ONLY. Every UPDATE carries an explicit `IS NULL` predicate on the column it writes.
     A row that already holds a value is never rewritten — that is the difference between a
     backfill and a silent restatement of live history.
  2. INSUFFICIENT HISTORY WRITES NOTHING. dma_20 requires exactly 20 trading days in the window and
     week_index_52 exactly 252, asserted as `n = 20` / `n = 252` rather than `>=`. A symbol without
     them keeps its NULL. A 40-day 52-week index is a fabricated number and worse than the gap.
  3. THE WINDOW IS THE SYMBOL'S OWN TRADING DAYS, from raw_prices — never calendar days. `ROWS
     BETWEEN n PRECEDING AND CURRENT ROW` over `PARTITION BY symbol ORDER BY price_date` is exactly
     that, and it handles holidays and suspensions without a calendar table.

THE FORMULAS ARE THE LIVE WRITER'S, VERIFIED BEFORE ANY WRITE — not re-derived. Both windows are
INCLUSIVE of the current day, and that was settled by measurement rather than by reading:

    TIINDIA 2026-08-13   stored dma_20 -0.93   inclusive -0.93   exclusive -1.20
    TIINDIA 2026-08-14   stored dma_20 -2.32   inclusive -2.32   exclusive -2.59
    TIINDIA 2026-08-13   stored wi52  49.01    inclusive 49.01
    TIINDIA 2026-08-14   stored wi52  45.31    inclusive 45.31

The inclusive window reproduces the stored values exactly and the exclusive one does not, on two
independent dates. Fable's card carried the same two definitions from his own recomputation, so
both readings agree and no third definition was shipped.

RESULT OF THE 18-AUG RUN:
    dma_20         46,463 NULL  ->    347 NULL     46,116 filled
    week_index_52  46,291 NULL  ->    328 NULL     45,963 filled
    every remaining NULL is a symbol-date with no raw_prices row (347 of 347 for dma_20; 73 for
    week_index_52) or short history (60 and 315 respectively) — none carries a partial window.
    sell_reversal daily-gate candidates, 2025-08-15..2026-08-14:
        before   297 symbol-days, first candidate 2026-05-26
        after  1,984 symbol-days, first candidate 2025-08-26, across 181 symbols

NOT A SCHEDULED JOB. This runs by hand, once, and is not registered anywhere — ENGINE_LIVENESS_RULE
concerns things that must keep breathing, and a one-off historical repair is not one of them.
"""

import os
import sys

# Batching by score_date quarter keeps each statement short. The window scan over raw_prices is the
# expensive half, so the batches are deliberately coarse — one pass per quarter, not per day.
_BATCHES = ["2025-06-01/2025-09-01", "2025-09-01/2026-01-01", "2026-01-01/2026-04-01",
            "2026-04-01/2100-01-01"]

# raw_prices starts in 2015 and holds 1.87M rows. The window only needs enough lead-in to satisfy
# the longest lookback before the earliest v8_metrics row (2025-06-02): 252 trading days is roughly
# 13 months, so 2023-06-01 is a wide margin and still avoids scanning a decade.
_LEAD_IN = "2023-06-01"

DMA20_SQL = """
WITH p AS (
    SELECT symbol, price_date, close,
           AVG(close) OVER w AS avg20,
           COUNT(*)   OVER w AS n20
    FROM raw_prices
    WHERE close > 0 AND price_date >= DATE %(lead_in)s
      AND symbol IN (SELECT DISTINCT symbol FROM v8_metrics)
    WINDOW w AS (PARTITION BY symbol ORDER BY price_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
)
UPDATE v8_metrics m
   SET dma_20 = (p.close / p.avg20 - 1) * 100
  FROM p
 WHERE m.symbol = p.symbol
   AND m.score_date = p.price_date
   AND m.dma_20 IS NULL          -- rule 1: fill nulls only
   AND p.n20 = 20                -- rule 2: exactly 20 trading days, never a partial window
   AND p.avg20 > 0
   AND m.score_date >= DATE %(lo)s AND m.score_date < DATE %(hi)s
"""

WI52_SQL = """
WITH p AS (
    SELECT symbol, price_date, close,
           MIN(low)  OVER w AS lo252,
           MAX(high) OVER w AS hi252,
           COUNT(*)  OVER w AS n252
    FROM raw_prices
    WHERE close > 0 AND price_date >= DATE %(lead_in)s
      AND symbol IN (SELECT DISTINCT symbol FROM v8_metrics)
    WINDOW w AS (PARTITION BY symbol ORDER BY price_date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW)
)
UPDATE v8_metrics m
   SET week_index_52 = (p.close - p.lo252) / (p.hi252 - p.lo252) * 100
  FROM p
 WHERE m.symbol = p.symbol
   AND m.score_date = p.price_date
   AND m.week_index_52 IS NULL   -- rule 1
   AND p.n252 = 252              -- rule 2
   AND p.hi252 > p.lo252
   AND m.score_date >= DATE %(lo)s AND m.score_date < DATE %(hi)s
"""

COUNTS_SQL = """
SELECT COUNT(*)                                        AS total_rows,
       COUNT(*) FILTER (WHERE dma_20 IS NULL)          AS dma_20_null,
       COUNT(*) FILTER (WHERE week_index_52 IS NULL)   AS week_index_52_null
  FROM v8_metrics
"""


def run(conn, dry_run: bool = False):
    """Fill both columns, batch by batch, and return the per-column row counts written."""
    filled = {"dma_20": 0, "week_index_52": 0}
    with conn.cursor() as cur:
        cur.execute(COUNTS_SQL)
        before = cur.fetchone()
        print("before: total=%s dma_20_null=%s week_index_52_null=%s" % before)
        if dry_run:
            return {"before": before, "filled": filled, "dry_run": True}
        for col, sql in (("dma_20", DMA20_SQL), ("week_index_52", WI52_SQL)):
            for b in _BATCHES:
                lo, hi = b.split("/")
                cur.execute(sql, {"lead_in": _LEAD_IN, "lo": lo, "hi": hi})
                filled[col] += cur.rowcount
                print("  %-14s %s -> %s  %6d rows" % (col, lo, hi, cur.rowcount))
            conn.commit()
        cur.execute(COUNTS_SQL)
        after = cur.fetchone()
    print("after:  total=%s dma_20_null=%s week_index_52_null=%s" % after)
    return {"before": before, "after": after, "filled": filled}


def _conn():
    import psycopg2
    return psycopg2.connect(os.environ["DATABASE_URL"])


if __name__ == "__main__":
    run(_conn(), dry_run="--dry-run" in sys.argv)
