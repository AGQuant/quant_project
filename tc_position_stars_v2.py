"""tc_position_stars_v2.py — cc#1172 push 7: the open-position star, moved onto the four-bucket engine.

WHY THIS FILE EXISTS
    The live star batch is scheduler._bg_tc_position_stars. It calls tc_resolver.get_primary_tc(),
    which resolves to tc_v4_endpoints.trade_check_v4 — the OLD tier1+tier2 scorer. Its score is
    tier1.score + tier2.score against a max of tier1.max + tier2.max, which is 12+8 = 20 for a LONG
    and 11+8 = 19 for a SHORT. Fixed denominators, two tiers, no styles.

    The Check page has run the four-bucket engine (tc_v4_dual) for months. So the star on a position
    and the Check page on the same symbol at the same tick have been answering with different
    rulebooks — the same propagation drift Fable proved in the screener on 20-Aug, in a second place.

    This module scores through tc_v4_dual, the SAME engine the Check page and tc_screener_v2 use.

DIRECTION IS THE POSITION'S OWN SIDE, and that is not a detail
    cc#728 and cc#738 locked it: a LONG position is checked LONG, a SHORT is checked SHORT. The star
    answers "is this position still supported?", and a strong SELL card is not reassurance for a
    LONG book — it is the opposite. So the winner is the best card WITHIN the position's own side,
    picked by tc_v4_dual.best_card(cards, side), the same locked cc#1033 ratio the Check page uses.

    The across-all-four winner is STORED anyway, in best_any_*. It is not acted on. It costs one
    column and it means a later ruling can read it out of history instead of waiting for a rebuild.

SELECTION IS ON THE RATIO, NOT ON score10 — deliberately, and it matches the engine
    While the SELL buckets sit on inactive registry rows their score10 falls back to an unweighted
    ratio, so choosing on score10 today would compare a WEIGHTED BUY number against an UNWEIGHTED
    SELL one. That is the like-for-like failure cc#1033 exists to prevent. The selector flips in one
    place — tc_v4_dual.best_card — once SELL is calibrated, and every consumer including this one
    inherits the flip. That is the point of it being shared.

WHAT IT WRITES, AND WHAT IT LEAVES ALONE
    A NEW table, tc_position_stars_v2. The old tc_position_stars is never ALTERed and never deleted
    (MAINTENANCE_LOCK_RULE, cc#351, and the card's own do_not_touch). Both batches run on the same
    :30 mark for now, so the two can be compared row-for-row on real data before any read flips.

    computed_at is a naive IST timestamp, matching the v1 table exactly, so a row in one table lines
    up with the row in the other. Mixing naive IST and naive UTC across two tables that are meant to
    be compared is how cc#844 happened.

ENGINE_LIVENESS_RULE (CLAUDE.md rule 9): registered is not live. This module is wired into the
    existing 09:30-15:30 :30-mark dispatch; the card stays open until the first batch has written
    rows and the counts are stated.
"""

import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

import psycopg
from fastapi import APIRouter, Header, HTTPException

import tc_v4_dual

router = APIRouter()
log = logging.getLogger("tc_position_stars_v2")

_DB = os.getenv("DATABASE_URL", "")
IST = ZoneInfo("Asia/Kolkata")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# v8_paper_positions stores LONG/SHORT; the engine speaks BUY/SELL. One map, in one place, so no
# call site has to remember which vocabulary it is holding.
_SIDE_TO_DIR = {"LONG": "BUY", "SHORT": "SELL"}

DDL = """
CREATE TABLE IF NOT EXISTS tc_position_stars_v2 (
    symbol            TEXT      NOT NULL,
    side              TEXT      NOT NULL,   -- LONG | SHORT, exactly as v8_paper_positions stores it
    dir_side          TEXT      NOT NULL,   -- BUY | SELL, the engine direction that side maps to
    bucket            TEXT,                 -- winning card WITHIN the position's own side
    score10           NUMERIC,
    verdict10         TEXT,                 -- STRONG | VALID | WATCH | REJECT
    weighted          BOOLEAN,              -- FALSE => unweighted ratio, NOT the calibrated score
    raw_score         NUMERIC,              -- so a row reconciles with the detail card
    raw_max           NUMERIC,
    best_any_bucket   TEXT,                 -- best of all four cards: stored, NOT acted on
    best_any_score10  NUMERIC,
    best_any_weighted BOOLEAN,
    cmp               NUMERIC,
    computed_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (symbol, side, computed_at)
);
CREATE INDEX IF NOT EXISTS tc_position_stars_v2_at_idx ON tc_position_stars_v2 (computed_at DESC);
"""


def _ist_now_naive():
    return datetime.now(IST).replace(tzinfo=None)


def _f(v):
    return None if v is None else float(v)


def ensure_table(cur):
    for stmt in [s for s in DDL.split(";") if s.strip()]:
        cur.execute(stmt)


def run_position_stars_v2(batch_ts=None):
    """Score every OPEN paper position on the four-bucket engine and write one row per position."""
    batch_ts = batch_ts or _ist_now_naive()
    conn = psycopg.connect(_DB)
    try:
        with conn.cursor() as cur:
            ensure_table(cur)
            cur.execute("SELECT DISTINCT symbol, side FROM v8_paper_positions "
                        "WHERE status='OPEN' AND symbol IS NOT NULL")
            pairs = cur.fetchall()
        conn.commit()

        rows = []
        failed = []
        for sym, side in pairs:
            dir_side = _SIDE_TO_DIR.get((side or "").strip().upper())
            if dir_side is None:
                # An unknown side is not something to guess at. Named, skipped, counted.
                failed.append((sym, f"unmapped side {side!r}"))
                continue
            try:
                from tc_resolver import get_primary_styles   # cc#1549: resolver, not a hardcoded tc_v4_dual call
                res = get_primary_styles()(sym, "ALL")
            except Exception as e:                      # one bad symbol must not lose the batch
                failed.append((sym, f"{type(e).__name__}: {str(e)[:80]}"))
                continue
            if not isinstance(res, dict) or res.get("error"):
                failed.append((sym, str(res.get("error"))[:80] if isinstance(res, dict) else "no dict"))
                continue
            cards = res.get("cards") or []
            own = tc_v4_dual.best_card(cards, dir_side)
            if own is None:
                failed.append((sym, f"no {dir_side} card scored"))
                continue
            any_best = tc_v4_dual.best_card(cards)
            rows.append((sym, side, dir_side,
                         own.get("label"), own.get("score10"), own.get("verdict10"),
                         bool(own.get("score10_weighted")), own.get("score"), own.get("max"),
                         (any_best or {}).get("label"), (any_best or {}).get("score10"),
                         bool((any_best or {}).get("score10_weighted")),
                         _f(res.get("cmp")), batch_ts))

        if rows:
            with conn.cursor() as cur:
                cur.executemany("""INSERT INTO tc_position_stars_v2
                    (symbol, side, dir_side, bucket, score10, verdict10, weighted,
                     raw_score, raw_max, best_any_bucket, best_any_score10, best_any_weighted,
                     cmp, computed_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, side, computed_at) DO UPDATE SET
                        bucket=EXCLUDED.bucket, score10=EXCLUDED.score10,
                        verdict10=EXCLUDED.verdict10, weighted=EXCLUDED.weighted,
                        raw_score=EXCLUDED.raw_score, raw_max=EXCLUDED.raw_max,
                        best_any_bucket=EXCLUDED.best_any_bucket,
                        best_any_score10=EXCLUDED.best_any_score10,
                        best_any_weighted=EXCLUDED.best_any_weighted, cmp=EXCLUDED.cmp""", rows)
            conn.commit()

        uncal = sorted({r[3] for r in rows if r[6] is False})
        out = {"ok": True, "computed_at": batch_ts.strftime("%Y-%m-%d %H:%M:%S"),
               "positions": len(pairs), "scored": len(rows), "failed": len(failed),
               "uncalibrated_buckets": uncal}
        if uncal:
            # Loud, not quiet. A star drawn from an unweighted ratio is not the calibrated number,
            # and the reader of this result should not have to go find that out from the rows.
            log.warning("tc_position_stars_v2 %s: buckets scored UNWEIGHTED (registry inactive): %s",
                        batch_ts, uncal)
        if failed:
            out["failed_sample"] = failed[:10]
            log.warning("tc_position_stars_v2 %s: %d failed, sample %s", batch_ts, len(failed), failed[:5])
        log.info("tc_position_stars_v2: %d/%d scored @ %s", len(rows), len(pairs), batch_ts)
        return out
    except Exception as e:
        log.exception("tc_position_stars_v2 batch failed")
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        conn.close()


@router.post("/api/admin/run-position-stars-v2")
def admin_run_position_stars_v2(x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return run_position_stars_v2()


@router.get("/api/trade-check/position-stars-v2")
def position_stars_v2():
    """Latest four-bucket star per open position (symbol|side).

    Same shape as the v1 endpoint plus score10/band/bucket, so a surface can move across without
    reshaping its reader. Never errors on a missing table: an absent first batch degrades to
    no-star, and says WHICH of the two it is rather than serving an empty list that reads like
    "nothing scored" (ENGINE_LIVENESS_RULE).
    """
    try:
        with psycopg.connect(_DB) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('tc_position_stars_v2')")
            if cur.fetchone()[0] is None:
                return {"status": "no_table", "rows": [], "stars": {},
                        "note": "first batch has not run — absent, not empty"}
            cur.execute("""
                SELECT DISTINCT ON (symbol, side)
                       symbol, side, dir_side, bucket, score10, verdict10, weighted,
                       raw_score, raw_max, best_any_bucket, best_any_score10, cmp, computed_at
                FROM tc_position_stars_v2
                ORDER BY symbol, side, computed_at DESC""")
            db_rows = cur.fetchall()
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}",
                "rows": [], "stars": {}}
    if not db_rows:
        return {"status": "no_run", "rows": [], "stars": {},
                "note": "table exists but holds no rows — absent, not empty"}
    out, stars = [], {}
    uncal = 0
    for (sym, side, dir_side, bucket, s10, v10, wt, raw, rawmax,
         any_b, any_s10, cmp_v, cat) in db_rows:
        item = {"symbol": sym, "side": side, "dir_side": dir_side, "bucket": bucket,
                "score10": _f(s10), "verdict10": v10, "weighted": bool(wt),
                "score": _f(raw), "max": _f(rawmax),
                "best_any_bucket": any_b, "best_any_score10": _f(any_s10),
                "cmp": _f(cmp_v),
                "computed_at": cat.strftime("%Y-%m-%d %H:%M:%S") if cat else None}
        if not wt:
            uncal += 1
        out.append(item)
        stars[f"{sym}|{side}"] = item
    return {"status": "ok", "count": len(out), "uncalibrated_rows": uncal,
            "engine": "tc_v4_dual four-bucket (cc#1172)", "rows": out, "stars": stars}
