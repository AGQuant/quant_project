"""
research_endpoints.py — V10 research router (ISOLATED)

Mounted in main.py via a single app.include_router(...) line.
Holds ad-hoc research triggers that must NOT touch the live trading path.

Routes:
  POST /api/research/backfill_nifty?total_days=365
       -> runs the standalone Fyers 1yr NIFTY 5m backfill into nifty_5m_research.
  GET  /api/research/nifty5m_status
       -> quick row count + min/max ts of the research table.
"""
import os
from typing import Optional

import psycopg2
from fastapi import APIRouter, Header, HTTPException

router = APIRouter(prefix="/api/research", tags=["research"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _check_admin(token: Optional[str]):
    if not ADMIN_TOKEN:
        return True
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


@router.post("/backfill_nifty")
def backfill_nifty(total_days: int = 365, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    from research.fyers_nifty_1y_backfill import run_backfill
    return run_backfill(total_days=total_days)


@router.get("/nifty5m_status")
def nifty5m_status():
    try:
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM nifty_5m_research")
            cnt, mn, mx = cur.fetchone()
        conn.close()
        return {"table": "nifty_5m_research", "rows": cnt,
                "min_ts": str(mn), "max_ts": str(mx)}
    except Exception as e:
        return {"error": str(e)}
