"""tc_universe_ticks.py -- cc#1862 step 3 FULL-UNIVERSE INTRADAY TC PERSISTENCE (founder-released
09-Sep-2026 ~04:30 IST, cc_task_logs id 5654: "SONNET 5 BUILDS BOTH P0s... the gates are the
safety net, not the model.").

WHAT THIS BUILDS AND WHY
    Founder ruling (cc#1862): "Store TC for ALL futures symbols, not just book symbols. This is
    the whole point -- without it there is no pre-entry TC history and no entry or exit rule can
    ever be backtested." bg_tc_scanner (cc#1746) already scores the FULL universe on all four
    score100 buckets every 5 minutes -- but only to decide today's entries; for every symbol not
    entered that tick (the overwhelming majority) the freshly-computed score100 is held in a local
    dict and discarded when the function returns (confirmed by reading tc_scanner_endpoints.
    run_scan(), cc_task_logs on this card). This module is the missing WRITE, using the SAME
    shared scorer (tc_v4_dual.score_card over tc_v4_scan._load_bulk) run_scan() and the app's own
    Trade Check tab both already use -- not a second compute path.

IMPORTANT CORRECTION POSTED TO THE FABLE ROOM (09-Sep-2026): v8_tc_score_ticks (v8_pivot_star.
run_tc_score_tick, dispatched by scheduler._bg_tc_score_tick) is NOT on a "different
implementation" as this card's own spec text states -- cc#1548 already repointed it to
tc_resolver.get_primary_styles() (the V2 four-bucket canon) months before this card was written.
That table is correctly on canon rules today; it just covers OPEN BOOK POSITIONS only (best-of-
side, one row per tick), which is a different product (feeds the AMBER TC_STRONG marker) from
what this module stores (every symbol, all four buckets, for backtesting pre-entry TC history).
The two are NOT duplicate implementations needing unification -- they are two correctly-canon
products at two different scopes. This module does not touch v8_tc_score_ticks or
_bg_tc_score_tick in any way.

GRAIN AND RETENTION -- founder-decided 08-Sep-2026: ALL FOUR BUCKETS (not best-bucket-only -- the
    HINDZINC 08-Sep worked example showed a full setup inversion, SELL-MOM VALID->REJECT while
    BUY-REV rose to VALID, that only shows up if every bucket is stored). 90-day ROLLING window
    (not a year) -- "90 calendar days is roughly 62 trading days. Enough to TEST a rule and not
    enough to TRUST one." Any backtest built on this window must say so.

CADENCE -- 5-minute market-hours tick, same beat as bg_tc_scanner, LAST TICK OF THE DAY AT 15:20
    IST. Stop there -- do not write ticks after 15:20 (founder-mandated).

PURGE SHIPS WITH THE WRITER -- founder-mandated: "The 90-day purge is NOT a later nice-to-have.
    Ship the writer and the purge TOGETHER." Implemented as a plain DELETE on the 15:20 (last)
    tick of each day, inside this same job -- never VACUUM FULL or any other locking operation
    inline (MAINTENANCE_LOCK_RULE cc#351). Reports rows deleted via ops_log every time it runs.

WHAT THIS DOES NOT DO
    Does not touch v8_tc_score_ticks / bg_tc_score_tick (see correction above). Does not touch
    bg_tc_scanner / tc_scanner_endpoints.run_scan() or the book's entry/exit logic -- this reads
    the SAME futures_universe + _load_bulk data bg_tc_scanner reads, as an independent scoring
    pass, so a slow write here can never affect the book's own scan/entry timing. Does not retire
    or deactivate any other TC job -- that is separate scope (cc#1862 steps 5-9), not started
    this pass; flagged as follow-up work in the Fable Room given the real remaining scope and
    that this card is explicitly "not clock-bound."

DO_NOT_TOUCH honoured: the canon rule set, weights and thresholds (session_log 39699/39713/39714/
    39720) -- this module calls score_card() unmodified, it does not change what TC computes.
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Json

import os

log = logging.getLogger("scorr.tc_universe_ticks")
DATABASE_URL = os.getenv("DATABASE_URL", "")
IST = ZoneInfo("Asia/Kolkata")
RETENTION_DAYS = 90   # founder-decided 08-Sep-2026: 90-day rolling, not a year
LAST_TICK_HOUR, LAST_TICK_MINUTE = 15, 20   # founder-mandated: stop writing ticks after 15:20 IST

_running = False


def _conn():
    return psycopg.connect(DATABASE_URL)


def _ist_now():
    return datetime.now(IST)


def _ensure_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS tc_universe_ticks (
        symbol TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL, bucket TEXT NOT NULL,
        side TEXT, score100 NUMERIC, verdict10 TEXT, weighted BOOLEAN, cmp NUMERIC,
        PRIMARY KEY (symbol, ts, bucket))""")
    cur.execute("""CREATE INDEX IF NOT EXISTS tc_universe_ticks_ts_idx
                   ON tc_universe_ticks (ts, symbol)""")


def _oplog(cur, title, details):
    try:
        cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                       VALUES (CURRENT_DATE, NOW(), 'tc_universe_ticks', %s, %s::jsonb)""",
                    (title, Json(details)))
    except Exception:
        pass


def run_tick() -> dict:
    """One 5-min tick: score the full active futures universe on all four score100 buckets (the
    SAME tc_v4_dual.score_card over tc_v4_scan._load_bulk bg_tc_scanner already runs) and bulk-
    insert every bucket's card for every symbol. On the 15:20 last tick, also purge rows older
    than the 90-day rolling window -- same job, same run, per the founder's "ship together" rule."""
    global _running
    if _running:
        return {"ok": False, "error": "already running"}
    _running = True
    try:
        now = _ist_now()
        if (now.hour, now.minute) > (LAST_TICK_HOUR, LAST_TICK_MINUTE):
            return {"ok": True, "skipped": "after 15:20 IST last tick"}

        from tc_v4_scan import _load_bulk
        from tc_v4_dual import score_card, STYLES, SIDES

        conn = _conn()
        try:
            cur = conn.cursor()
            _ensure_table(cur)
            D, _ctx = _load_bulk(cur)
            conn.commit()

            rows = []
            ts = now.astimezone(timezone.utc)
            failed = 0
            for sym, d in D.items():
                if not d.get("daily") or d.get("cmp") is None:
                    continue
                cmp_v = d.get("cmp")
                for side in SIDES:
                    for style in STYLES:
                        try:
                            c = score_card(d, style, side)
                        except Exception:
                            failed += 1
                            continue
                        if not c or c.get("score100") is None:
                            continue
                        rows.append((sym, ts, c.get("label"), side, c.get("score100"),
                                     c.get("verdict10"), bool(c.get("score10_weighted")), cmp_v))

            if rows:
                with conn.cursor() as cur2:
                    cur2.execute("""CREATE TEMP TABLE tc_universe_ticks_staging (
                        symbol TEXT, ts TIMESTAMPTZ, bucket TEXT, side TEXT,
                        score100 NUMERIC, verdict10 TEXT, weighted BOOLEAN, cmp NUMERIC
                    ) ON COMMIT DROP""")
                    with cur2.copy("COPY tc_universe_ticks_staging (symbol, ts, bucket, side, "
                                   "score100, verdict10, weighted, cmp) FROM STDIN") as cp:
                        for r in rows:
                            cp.write_row(r)
                    cur2.execute("""INSERT INTO tc_universe_ticks
                        (symbol, ts, bucket, side, score100, verdict10, weighted, cmp)
                        SELECT symbol, ts, bucket, side, score100, verdict10, weighted, cmp
                        FROM tc_universe_ticks_staging
                        ON CONFLICT (symbol, ts, bucket) DO NOTHING""")
                conn.commit()

            purged = None
            if (now.hour, now.minute) == (LAST_TICK_HOUR, LAST_TICK_MINUTE):
                with conn.cursor() as cur3:
                    cur3.execute("DELETE FROM tc_universe_ticks WHERE ts < NOW() - INTERVAL '%s days'"
                                 % RETENTION_DAYS)
                    purged = cur3.rowcount
                conn.commit()

            with conn.cursor() as cur4:
                _oplog(cur4, "tc_universe_tick",
                       {"symbols": len(D), "rows_written": len(rows), "failed": failed, "purged": purged})
                conn.commit()
            log.info(f"tc_universe_ticks: {len(rows)} rows, {len(D)} symbols, {failed} failed, purged={purged}")
            return {"ok": True, "rows_written": len(rows), "symbols": len(D), "failed": failed, "purged": purged}
        finally:
            conn.close()
    except Exception as e:
        log.error(f"tc_universe_ticks tick failed: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        _running = False
