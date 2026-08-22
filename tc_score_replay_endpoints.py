"""tc_score_replay_endpoints.py — cc#1211: the TC score entry replay API.

GET  /api/tc/replay/summary?threshold=&hold=   read the stored sweep (spec item 5)
GET  /api/tc/replay/table                      the markdown results table, built from the tables
GET  /api/tc/replay/selfcheck?symbol=          as-of loader vs the live scorer, the card's verify 2
POST /api/admin/run-tc-replay                  score the five sessions, then sweep

WHY THERE IS A POST HERE AT ALL. The replay is roughly 100k card evaluations across five
sessions and it has to run where the database is. It is NOT a scheduled job and must not become
one - the card says replay only, no live job - so it is a manual one-shot, which is the same
shape tc_screener_v2 already uses for /api/admin/run-tc-screener-v2. Pressing it twice is safe:
scoring upserts on (ts, symbol, bucket) and the sweep clears each cell before refilling it.

It is deliberately slow and deliberately not backgrounded. A caller should see it finish, or see
it time out and know the run did not complete, rather than get an instant 200 and a half-filled
table that looks like a result.
"""

from fastapi import APIRouter, Query

import tc_score_replay as R

router = APIRouter()


@router.get("/api/tc/replay/summary")
def replay_summary(threshold: int = Query(None), hold: int = Query(None)):
    """The stored sweep. With no arguments, every cell; with both, one cell plus its trades."""
    with R._conn() as conn, conn.cursor() as cur:
        cells = R._cells(cur)
        if threshold is None or hold is None:
            return {"sessions": R.SESSIONS, "cells": cells,
                    "merit_gate": {"avg": R.MERIT_AVG, "acc": R.MERIT_ACC},
                    "asof_limits": R.ASOF_LIMITS}
        cur.execute("""SELECT symbol, bucket, side, entry_ts, entry_px, entry_src,
                              exit_ts, exit_px, exit_reason, pnl_pct
                       FROM tc_score_replay_trades
                       WHERE threshold=%s AND hold_days=%s
                       ORDER BY entry_ts, symbol""", (threshold, hold))
        cols = ["symbol", "bucket", "side", "entry_ts", "entry_px", "entry_src",
                "exit_ts", "exit_px", "exit_reason", "pnl_pct"]
        trades = [dict(zip(cols, r)) for r in cur.fetchall()]
    cell = next((c for c in cells if c["threshold"] == threshold and c["hold"] == hold), None)
    return {"sessions": R.SESSIONS, "threshold": threshold, "hold": hold,
            "cell": cell, "trades": trades, "asof_limits": R.ASOF_LIMITS}


@router.get("/api/tc/replay/table")
def replay_table():
    return {"markdown": R.results_table(),
            "best": R.best_cells(2),
            "breakdowns": [R.bucket_breakdown(c["threshold"], c["hold"]) for c in R.best_cells(2)]}


@router.get("/api/tc/replay/selfcheck")
def replay_selfcheck(symbol: str = Query("RELIANCE"), side: str = Query("BUY"),
                     style: str = Query("MOMENTUM")):
    """Verify item 2. Run this BEFORE trusting any table: it is the only test that can catch the
    as-of loader having drifted from the scorer's own loader."""
    return R.selfcheck(symbol, side, style)


@router.post("/api/admin/run-tc-replay")
def run_tc_replay(phase: str = Query("all", description="score | sweep | all")):
    out = {"sessions": R.SESSIONS, "phase": phase}
    if phase in ("score", "all"):
        out["ticks_scored"] = R.score_all()
    if phase in ("sweep", "all"):
        out["trades"] = R.sweep()
    return out
