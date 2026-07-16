"""
fyers_range_backfill_endpoints.py — cc#153, extended cc#159
================================================================
On-demand admin trigger to REST-backfill Fyers futures 5-min bars.
Reuses fyers_backfill.backfill_range() unchanged — this file is wiring only,
no new backfill logic. Exists because backfill_7day()/heal_gap() are normally
only invoked from inside the standalone fyers_feed.py worker process (on boot,
gated by an already-fresh-data skip check, or daily at 18:00 IST) — there was
no admin-triggerable path to re-run a full-window REST backfill on demand.

cc#159: accepts start/end/symbols. cc#488 fix (task_3): the full-universe run
takes ~15-20 min but the MCP connector's HTTP client times out well before
that and retries — each retry landed on the `_running` guard and returned
"already_running" with 0 bars actually confirmed written, making the whole
call look like a no-op. Switched to fire-and-forget: the POST spawns a
background thread and returns "started" immediately; progress/result is
polled via GET /api/admin/backfill_futures_fyers/status. Blocked automatically
during market hours (09:15-15:30 IST) by fyers_backfill's own
_assert_not_market_hours() guard.
"""
from datetime import datetime, date
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import logging
import os
import threading
import time

log = logging.getLogger("scorr.fyers_range_backfill")
router = APIRouter(prefix="/api/admin", tags=["fyers_backfill"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
DEFAULT_START = "2026-06-26"   # cc#159: start of the Jun26 contamination-fix window

_running = False
_status = {"state": "idle", "started_at": None, "finished_at": None, "result": None, "error": None}


class BackfillRangeRequest(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None
    symbols: Optional[List[str]] = None
    contract: Optional[str] = None   # cc#184: explicit 'YYMMM' futures contract (e.g. '26JUL')


def _check_admin(token):
    if not ADMIN_TOKEN:
        return True
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


def _run_backfill_sync(start: Optional[str], end: Optional[str],
                       symbols: Optional[List[str]], contract: Optional[str]):
    import fyers_backfill
    import fyers_feed
    conn = fyers_feed.get_db()
    try:
        token = fyers_feed.get_valid_token(conn)
        date_from = datetime.strptime(start or DEFAULT_START, "%Y-%m-%d").date()
        date_to = datetime.strptime(end, "%Y-%m-%d").date() if end else date.today()
        # cc#184: this endpoint is the TRUE FUTURES backfill — resolve explicit
        # monthly contracts (fyers_fut, DO NOTHING) instead of the old spot -EQ
        # collision. contract defaults to the current active month if omitted.
        return fyers_backfill.backfill_range(token, conn, date_from, date_to, symbols,
                                             futures=True, contract=contract)
    finally:
        conn.close()


def _run_backfill_bg(start, end, symbols, contract):
    global _running
    try:
        result = _run_backfill_sync(start, end, symbols, contract)
        log.info(f"fyers_range_backfill: {result}")
        _status.update(state="complete", finished_at=time.time(), result=result, error=None)
    except Exception as e:
        log.error(f"fyers_range_backfill failed: {e}")
        _status.update(state="error", finished_at=time.time(), result=None, error=str(e))
    finally:
        _running = False


@router.post("/backfill_futures_fyers")
async def backfill_futures_fyers_now(body: BackfillRangeRequest = BackfillRangeRequest(),
                                      x_admin_token: Optional[str] = Header(None)):
    """cc#153/cc#159, cc#488: on-demand Fyers REST 5-min futures backfill. start
    defaults to 2026-06-26, end defaults to today, symbols defaults to all active
    futures (~212). Fire-and-forget — spawns a background thread and returns
    "started" immediately (the full universe takes ~15-20 min at 5s API pacing,
    longer than the MCP connector's request timeout). Poll GET
    .../backfill_futures_fyers/status for the result. Blocked automatically if
    triggered during market hours (09:15-15:30 IST) — the guard runs inside the
    background thread, so a market-hours call returns "started" and then lands
    in status=error with the guard's message."""
    _check_admin(x_admin_token)
    global _running
    if _running:
        return {"status": "already_running", **_status}
    _running = True
    _status.update(state="running", started_at=time.time(), finished_at=None, result=None, error=None)
    threading.Thread(target=_run_backfill_bg, args=(body.start, body.end, body.symbols, body.contract),
                      daemon=True).start()
    return {"status": "started", **_status}


@router.get("/backfill_futures_fyers/status")
async def backfill_futures_fyers_status(x_admin_token: Optional[str] = Header(None)):
    """cc#488: poll the outcome of the fire-and-forget backfill trigger above."""
    _check_admin(x_admin_token)
    return {"running": _running, **_status}
