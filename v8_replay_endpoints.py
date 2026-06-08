"""
v8_replay_endpoints.py — endpoints for the V8 paper 5-min stepped replay.
08-Jun-2026. Wires v8_paper_replay into the API. include_router() in main.py.

Endpoints:
  POST /api/v8/replay/run?start=YYYY-MM-DD[&end=YYYY-MM-DD][&wipe=true]
       Runs the stepped 5-min replay. wipe defaults true (clears paper book).
  GET  /api/v8/replay/summary
       Current paper book stats (open positions + realized trade stats by basket).
"""

from fastapi import APIRouter, HTTPException, Query
from datetime import date, datetime
import psycopg
import os

import v8_paper_replay as rp

router = APIRouter(prefix="/api/v8/replay", tags=["v8_replay"])


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


@router.post("/run")
def replay_run(
    start: str = Query(..., description="YYYY-MM-DD inclusive start"),
    end: str = Query(None, description="YYYY-MM-DD inclusive end (default today)"),
    wipe: bool = Query(True, description="Wipe paper book first (DESTRUCTIVE)"),
):
    try:
        sd = datetime.strptime(start, "%Y-%m-%d").date()
        ed = datetime.strptime(end, "%Y-%m-%d").date() if end else date.today()
    except ValueError:
        raise HTTPException(400, "dates must be YYYY-MM-DD")
    try:
        with _conn() as conn:
            return rp.run_replay(conn, start=sd, end=ed, wipe=wipe)
    except Exception as e:
        raise HTTPException(500, f"replay_run failed: {e}")


@router.get("/summary")
def replay_summary():
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT basket, side, COUNT(*) n,
                       COUNT(*) FILTER (WHERE pnl > 0) wins,
                       COALESCE(ROUND(SUM(pnl)::numeric,2),0) pnl,
                       COALESCE(ROUND(AVG(return_pct)::numeric,2),0) avg_ret
                FROM v8_paper_trades GROUP BY basket, side ORDER BY basket, side
            """)
            cols = [d[0] for d in cur.description]
            by_basket = [dict(zip(cols, r)) for r in cur.fetchall()]

            cur.execute("""
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE pnl > 0),
                       COALESCE(ROUND(SUM(pnl)::numeric,2),0),
                       COALESCE(ROUND(AVG(return_pct)::numeric,2),0)
                FROM v8_paper_trades
            """)
            n, wins, total_pnl, avg_ret = cur.fetchone()

            cur.execute("""
                SELECT symbol, side, basket, entry_price, entry_ts, target, stop_loss
                FROM v8_paper_positions WHERE status='OPEN' ORDER BY entry_ts
            """)
            pcols = [d[0] for d in cur.description]
            open_pos = [dict(zip(pcols, r)) for r in cur.fetchall()]

        return {
            "closed_trades": n, "wins": wins,
            "win_rate_pct": round(wins / n * 100, 1) if n else 0.0,
            "total_pnl": float(total_pnl), "avg_return_pct": float(avg_ret),
            "by_basket": by_basket,
            "open_positions": open_pos, "open_count": len(open_pos),
        }
    except Exception as e:
        raise HTTPException(500, f"replay_summary failed: {e}")
