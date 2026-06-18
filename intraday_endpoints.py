"""
Intraday paper engine endpoints (id=374; updated 18-Jun-2026).

Single source of truth: tc_intraday_* tables (written by scheduler every 5-min).

  GET  /api/intraday/dashboard   — instant-read dashboard payload for /intraday page
  POST /api/intraday/tick        — manual engine tick (enter + exit), also refreshes cache
  GET  /api/intraday/open        — open positions (optional ?side=)
  GET  /api/intraday/trades      — today's closed trades (optional ?side=)
"""

from fastapi import APIRouter
import tc_intraday as tci
import intraday_engine as ie

router = APIRouter()


@router.get("/api/intraday/dashboard")
def intraday_dashboard():
    """INSTANT-READ dashboard payload for the /intraday page.
    Reads tc_intraday_positions + tc_intraday_trades (the live scheduler tables).
    Returns {ts, cache_ts, cache_rows, sides:{LONG/SHORT:{funnel,stats,open,trades}}}."""
    return tci.intraday_dashboard()


@router.post("/api/intraday/tick")
def intraday_tick():
    """Manual engine tick: refreshes tc_cache, scans + enters new positions,
    runs exit checks + 15:15 square-off.
    Returns {new_entries, closed} for the page button feedback."""
    rc = tci.refresh_tc_cache()
    en = tci.run_intraday_paper_entry()
    ex = tci.run_intraday_paper_exit()
    return {
        "ok": True,
        "cache_written": rc.get("written"),
        "new_entries": en.get("positions", []),
        "closed": ex.get("closed"),
        "square_off": ex.get("square_off"),
        "ts": en.get("ts") or ex.get("ts"),
    }


@router.get("/api/intraday/open")
def intraday_open(side: str = None):
    return {"open": ie.get_open(side.upper() if side else None)}


@router.get("/api/intraday/trades")
def intraday_trades(side: str = None, limit: int = 50):
    return {"trades": ie.get_trades(side.upper() if side else None, limit)}
