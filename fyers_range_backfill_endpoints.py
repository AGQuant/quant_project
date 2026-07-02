"""
fyers_range_backfill_endpoints.py — cc#153
============================================
On-demand admin trigger to REST-backfill Fyers futures 5-min bars.
Reuses fyers_backfill.backfill_7day() unchanged — this file is wiring only,
no new backfill logic. Exists because backfill_7day()/heal_gap() are normally
only invoked from inside the standalone fyers_feed.py worker process (on boot,
gated by an already-fresh-data skip check, or daily at 18:00 IST) — there was
no admin-triggerable path to re-run a full-window REST backfill on demand.

fyers_backfill.RETENTION_DAYS=7 already covers back to ~25-Jun as of
02-Jul-2026, so backfill_7day() as-is fills the Jun26-now window this task
needs without modification. Blocked automatically during market hours
(09:15-15:30 IST) by fyers_backfill's own _assert_not_market_hours() guard.
"""
from fastapi import APIRouter, Header, HTTPException
from typing import Optional
import asyncio
import logging
import os

log = logging.getLogger("scorr.fyers_range_backfill")
router = APIRouter(prefix="/api/admin", tags=["fyers_backfill"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

_running = False


def _check_admin(token):
    if not ADMIN_TOKEN:
        return True
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


async def _run_backfill():
    global _running
    import fyers_backfill
    import fyers_feed
    try:
        conn = fyers_feed.get_db()
        try:
            token = fyers_feed.get_valid_token(conn)
            await asyncio.to_thread(fyers_backfill.backfill_7day, token, conn)
            log.info("fyers_range_backfill: backfill_7day complete")
        finally:
            conn.close()
    except Exception as e:
        log.error(f"fyers_range_backfill failed: {e}")
    finally:
        _running = False


@router.post("/backfill_futures_fyers")
async def backfill_futures_fyers_now(x_admin_token: Optional[str] = Header(None)):
    """cc#153: on-demand Fyers REST 5-min futures backfill (RETENTION_DAYS=7d
    back). Runs ~15-20 min in the background (212 symbols, 5s pacing between
    Fyers History API calls). Returns immediately with status=started."""
    _check_admin(x_admin_token)
    global _running
    if _running:
        return {"status": "already_running"}
    _running = True
    asyncio.create_task(_run_backfill())
    return {"status": "started",
            "note": "Fyers REST 5-min futures backfill running in background "
                    "(~15-20 min, 212 symbols, 5s pacing). Blocked automatically "
                    "if triggered during market hours (09:15-15:30 IST)."}
