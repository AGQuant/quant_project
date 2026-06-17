"""
Intraday paper engine endpoints (id=374).

  POST /api/intraday/tick        — run one engine tick (manage open + enter). Manual phase 1.
  GET  /api/intraday/dashboard   — full page payload (funnel + open + trade log + stats, per side)
  GET  /api/intraday/open        — open positions (optional ?side=)
  GET  /api/intraday/trades      — today's closed trades (optional ?side=)
"""

from fastapi import APIRouter
import intraday_engine as ie

router = APIRouter()


@router.post("/api/intraday/tick")
def intraday_tick():
    """One engine tick: manage/exit open positions, then enter new qualifiers
    (both sides) if before 15:00 cutoff. Square-off all at/after 15:15.
    Standalone/manual in phase 1 — scheduler wiring deferred to phase 1.5."""
    return ie.run_tick()


@router.get("/api/intraday/dashboard")
def intraday_dashboard():
    return ie.get_dashboard()


@router.get("/api/intraday/open")
def intraday_open(side: str = None):
    return {"open": ie.get_open(side.upper() if side else None)}


@router.get("/api/intraday/trades")
def intraday_trades(side: str = None, limit: int = 50):
    return {"trades": ie.get_trades(side.upper() if side else None, limit)}
