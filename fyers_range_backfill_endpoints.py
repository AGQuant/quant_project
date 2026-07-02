"""
fyers_range_backfill_endpoints.py — cc#153, extended cc#159
================================================================
On-demand admin trigger to REST-backfill Fyers futures 5-min bars.
Reuses fyers_backfill.backfill_range() unchanged — this file is wiring only,
no new backfill logic. Exists because backfill_7day()/heal_gap() are normally
only invoked from inside the standalone fyers_feed.py worker process (on boot,
gated by an already-fresh-data skip check, or daily at 18:00 IST) — there was
no admin-triggerable path to re-run a full-window REST backfill on demand.

cc#159: accepts start/end/symbols and runs SYNCHRONOUSLY (awaits completion)
so the caller — now also registered as the backfill_futures_fyers MCP tool in
mcp_dispatch.py — gets back a real summary (symbols processed, bars written,
gaps remaining) instead of a fire-and-forget "started" status. Blocked
automatically during market hours (09:15-15:30 IST) by fyers_backfill's own
_assert_not_market_hours() guard.
"""
from datetime import datetime, date
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import asyncio
import logging
import os

log = logging.getLogger("scorr.fyers_range_backfill")
router = APIRouter(prefix="/api/admin", tags=["fyers_backfill"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
DEFAULT_START = "2026-06-26"   # cc#159: start of the Jun26 contamination-fix window

_running = False


class BackfillRangeRequest(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None
    symbols: Optional[List[str]] = None


def _check_admin(token):
    if not ADMIN_TOKEN:
        return True
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


def _run_backfill_sync(start: Optional[str], end: Optional[str], symbols: Optional[List[str]]):
    import fyers_backfill
    import fyers_feed
    conn = fyers_feed.get_db()
    try:
        token = fyers_feed.get_valid_token(conn)
        date_from = datetime.strptime(start or DEFAULT_START, "%Y-%m-%d").date()
        date_to = datetime.strptime(end, "%Y-%m-%d").date() if end else date.today()
        return fyers_backfill.backfill_range(token, conn, date_from, date_to, symbols)
    finally:
        conn.close()


@router.post("/backfill_futures_fyers")
async def backfill_futures_fyers_now(body: BackfillRangeRequest = BackfillRangeRequest(),
                                      x_admin_token: Optional[str] = Header(None)):
    """cc#153/cc#159: on-demand Fyers REST 5-min futures backfill. start defaults
    to 2026-06-26, end defaults to today, symbols defaults to all active futures
    (~212). Runs synchronously (~15-20 min for the full universe, 5s pacing
    between Fyers History API calls) and returns a summary. Blocked automatically
    if triggered during market hours (09:15-15:30 IST)."""
    _check_admin(x_admin_token)
    global _running
    if _running:
        return {"status": "already_running"}
    _running = True
    try:
        result = await asyncio.to_thread(_run_backfill_sync, body.start, body.end, body.symbols)
        log.info(f"fyers_range_backfill: {result}")
        return {"status": "complete", **result}
    except Exception as e:
        log.error(f"fyers_range_backfill failed: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        _running = False
