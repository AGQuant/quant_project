"""tc_screener_v2.py — cc#1172 push 6: the screener, rebuilt on the four-bucket engine.

WHY THIS FILE EXISTS
    The live screener at trade_check_v34_endpoints.run_tc_screener_precompute() calls
    `tc.trade_check(sym, side)` — the OLD pre-four-bucket generic scorer. The four-bucket V4 dual
    engine exists only on the Check page, so the screener and the Check page have been scoring the
    same symbol by different rulebooks.

    That is not a theory. Fable found the drift in the data on 20-Aug: tc_screener_cache holds a
    SHORT scored 17.5 when the locked SELL card maxes at 14, which is arithmetically impossible on
    the four-bucket engine, and its failed_rules names match no locked card. A number that cannot
    exist is the clearest possible proof the two paths diverged.

    This module scores every symbol through the SAME shared engine the Check page uses —
    tc_v4_dual.trade_check_v4_dual — so a symbol reads identically wherever it appears. That is the
    whole point of the card: the rebuild IS the propagation fix.

WHAT IT STORES, AND WHAT IT DOES NOT TOUCH
    Writes tc_screener_v2, a NEW table. tc_screener_cache is left exactly as it is, as archive —
    never ALTERed, never deleted (MAINTENANCE_LOCK_RULE, cc#351, and the card's own do_not_touch).
    One row per (symbol, bucket) per run, so all four cards are queryable, plus an is_best flag on
    the card the engine chose.

    score10 rides with a `weighted` flag beside it. A bucket whose registry rows are inactive
    scores on the unweighted ratio, and a row that does not say so would be a calibrated-looking
    number that is not calibrated — the failure this whole card exists to end. The flag is stored,
    not inferred at read time.

NO BACKFILL. Corrected scoring starts at the first run; historical tc_screener_cache rows stay
    labelled as old-engine archive. Inventing a corrected past would be worse than the gap.

ENGINE_LIVENESS_RULE (CLAUDE.md rule 9): building and registering this is NOT the same as it being
    live. The card stays open until a first run has produced rows and the counts are stated.
"""

import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Header, HTTPException
from typing import Optional

from tc_resolver import get_primary_styles   # cc#1549: resolver, not a hardcoded tc_v4_dual import

router = APIRouter()
log = logging.getLogger("tc_screener_v2")

_DB = os.getenv("DATABASE_URL", "")
IST = ZoneInfo("Asia/Kolkata")
UNIVERSE = "futures"
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _ist():
    return datetime.now(IST)


def _f(v):
    return None if v is None else float(v)


DDL = """
CREATE TABLE IF NOT EXISTS tc_screener_v2 (
    id          SERIAL PRIMARY KEY,
    run_date    DATE        NOT NULL,
    universe    TEXT        NOT NULL,
    symbol      TEXT        NOT NULL,
    bucket      TEXT        NOT NULL,          -- BUY-MOM | BUY-REV | SELL-MOM | SELL-REV
    side        TEXT        NOT NULL,          -- BUY | SELL
    score10     NUMERIC,                       -- the normalized 0-10 score
    verdict10   TEXT,                          -- STRONG | VALID | WATCH | REJECT
    weighted    BOOLEAN,                       -- FALSE => unweighted ratio, NOT the calibrated score
    raw_score   NUMERIC,                       -- kept so a row reconciles with the detail card
    raw_max     NUMERIC,
    is_best     BOOLEAN     NOT NULL DEFAULT FALSE,
    cmp         NUMERIC,
    computed_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS tc_screener_v2_run_idx ON tc_screener_v2 (run_date, universe, side, score10 DESC);
CREATE INDEX IF NOT EXISTS tc_screener_v2_sym_idx ON tc_screener_v2 (run_date, symbol);
"""


def ensure_table(cur):
    for stmt in [s for s in DDL.split(";") if s.strip()]:
        cur.execute(stmt)


def run_tc_screener_v2(limit_symbols: Optional[int] = None):
    """Score the active futures universe through the four-bucket engine and replace today's rows.

    Heavy: one engine call per symbol (the engine scores all four cards in that call, so this is
    HALF the calls the old screener made — it ran the scorer once per side).
    """
    run_date = _ist().date()
    conn = psycopg.connect(_DB)
    try:
        with conn.cursor() as cur:
            ensure_table(cur)
            cur.execute("SELECT symbol FROM futures_universe WHERE is_active = TRUE ORDER BY symbol")
            symbols = [r[0] for r in cur.fetchall()]
        conn.commit()
        if limit_symbols:
            symbols = symbols[:limit_symbols]

        rows = []
        failed = []
        for sym in symbols:
            try:
                res = get_primary_styles()(sym, "ALL")
            except Exception as e:                      # one bad symbol must not lose the run
                failed.append((sym, f"{type(e).__name__}: {str(e)[:80]}"))
                continue
            if not isinstance(res, dict) or res.get("error"):
                failed.append((sym, str(res.get("error"))[:80] if isinstance(res, dict) else "no dict"))
                continue
            best_label = res.get("best_label")
            cmp_v = _f(res.get("cmp"))
            for c in (res.get("cards") or []):
                rows.append((run_date, UNIVERSE, sym, c.get("label"), c.get("side"),
                             c.get("score10"), c.get("verdict10"),
                             bool(c.get("score10_weighted")),
                             c.get("score"), c.get("max"),
                             c.get("label") == best_label, cmp_v))

        with conn.cursor() as cur:
            # Replace TODAY's rows only. History for other dates is untouched — this is a daily
            # snapshot table, not a ledger, and a re-run of the same day must be idempotent.
            cur.execute("DELETE FROM tc_screener_v2 WHERE run_date = %s AND universe = %s",
                        (run_date, UNIVERSE))
            cur.executemany("""INSERT INTO tc_screener_v2
                (run_date, universe, symbol, bucket, side, score10, verdict10, weighted,
                 raw_score, raw_max, is_best, cmp)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
        conn.commit()

        uncal = sorted({r[3] for r in rows if r[7] is False})
        out = {"ok": True, "run_date": str(run_date), "symbols": len(symbols),
               "rows": len(rows), "cards_per_symbol": (len(rows) / len(symbols)) if symbols else 0,
               "failed_symbols": len(failed),
               "uncalibrated_buckets": uncal}
        if uncal:
            # Loud, not quiet: a run where a whole bucket scored uncalibrated is a fact the reader
            # of this result needs, not a detail to discover later from the rows.
            log.warning("tc_screener_v2 run %s: buckets scored UNWEIGHTED (registry inactive): %s",
                        run_date, uncal)
        if failed:
            out["failed_sample"] = failed[:10]
            log.warning("tc_screener_v2 run %s: %d symbols failed, sample %s",
                        run_date, len(failed), failed[:5])
        return out
    except Exception as e:
        log.exception("tc_screener_v2 run failed")
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        conn.close()


@router.post("/api/admin/run-tc-screener-v2")
def admin_run_tc_screener_v2(limit: Optional[int] = None,
                             x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return run_tc_screener_v2(limit_symbols=limit)


@router.get("/api/trade-check/screen-v2")
def screen_v2(side: str = "BUY", limit: int = 25, run_date: Optional[str] = None):
    """Top symbols for a side on the latest run, ranked by the normalized score.

    Ranks on the BEST card per symbol, so a symbol appears once per side with the card that
    actually won — not four times, and not on a card the engine did not choose.
    """
    side = (side or "BUY").strip().upper()
    if side not in ("BUY", "SELL"):
        raise HTTPException(400, "side must be BUY or SELL")
    limit = max(1, min(int(limit or 25), 200))
    try:
        with psycopg.connect(_DB) as conn, conn.cursor() as cur:
            ensure_table(cur)
            if run_date:
                rd = run_date
            else:
                cur.execute("SELECT MAX(run_date) FROM tc_screener_v2 WHERE universe = %s", (UNIVERSE,))
                r = cur.fetchone()
                rd = r[0] if r and r[0] else None
            if rd is None:
                # No run has happened. Say that, rather than serving an empty list that reads like
                # "nothing qualified today" — those are different statements (ENGINE_LIVENESS_RULE).
                return {"status": "no_run", "side": side, "rows": [],
                        "note": "tc_screener_v2 has never been run — this is not an empty result, "
                                "it is an absent one."}
            cur.execute("""SELECT symbol, bucket, score10, verdict10, weighted, raw_score, raw_max, cmp
                           FROM tc_screener_v2
                           WHERE run_date = %s AND universe = %s AND side = %s AND is_best
                           ORDER BY score10 DESC NULLS LAST, symbol
                           LIMIT %s""", (rd, UNIVERSE, side, limit))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.execute("""SELECT COUNT(*) FILTER (WHERE NOT weighted), COUNT(*)
                           FROM tc_screener_v2 WHERE run_date = %s AND universe = %s AND side = %s AND is_best""",
                        (rd, UNIVERSE, side))
            unw, tot = cur.fetchone()
            # cc#1594 (RECO cc_task_logs 4510): the registry weight sum per bucket, read at request
            # time from tc_rule_weights — never a literal (TC_SCORE_100_V1, session_log 29138).
            cur.execute("SELECT bucket, SUM(weight) FROM tc_rule_weights WHERE active GROUP BY bucket")
            weight_sums = {b: float(w) for b, w in cur.fetchall()}
        # cc#1594: name the two scales so no reader divides raw_score by raw_max and calls it the
        # score. raw_score / raw_max are the UNWEIGHTED card credit and its raw max (sum of each
        # rule's own max, derived per card in tc_v4_dual — it varies by symbol when a rule is not
        # emitted). score10 is the registry-WEIGHTED canon, 10 * sum(w * pass) / sum(w); score100
        # is score10 x 10 and nothing else (29138). BHARATFORG BUY-REV 01-Sep: raw 11.5/20,
        # score10 6.43 -> score100 64.3. 11.5/20 = 57.5 and 11.5/21 = 54.8 are both wrong reads.
        for r in rows:
            s10 = r.get("score10")
            r["score100"] = round(float(s10) * 10.0, 1) if s10 is not None else None
            r["weight_sum"] = weight_sums.get(r.get("bucket"))
            for k in ("raw_score", "raw_max"):
                if r.get(k) is not None:
                    r[k] = float(r[k])
        return {"status": "ok", "run_date": str(rd), "side": side, "count": len(rows),
                # Carried on the payload so a surface can label the list rather than infer it.
                "uncalibrated_rows": int(unw or 0), "total_rows": int(tot or 0),
                "engine": "tc_v4_dual four-bucket (cc#1172)",
                "scale": {"score100": "score10 x 10 — registry-weighted (tc_rule_weights, read at compute time)",
                          "raw_score": "unweighted card credit", "raw_max": "sum of each emitted rule's own max (varies by symbol)",
                          "weight_sum": "SUM(weight) FROM tc_rule_weights WHERE active, per bucket, read now"},
                "weight_sums": weight_sums, "rows": rows}
    except Exception as e:
        raise HTTPException(500, f"screen_v2 failed: {e}")
