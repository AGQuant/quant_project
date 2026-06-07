"""
v10_endpoints.py — V10 ST+EMA intraday strategy routes (Scorr platform).
Mounted in main.py via: app.include_router(v10_router)
Isolated from V8 / live feed writes. Paper + advisory only.
"""
import os
from typing import Optional
from fastapi import APIRouter, Header, HTTPException

router = APIRouter(prefix="/api/v10", tags=["v10"])
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _check_admin(token: Optional[str]):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


@router.get("/signal")
def v10_signal():
    """Current NIFTY signal from the latest CLOSED 10m bar."""
    import v10_st_ema
    return v10_st_ema.current_signal()


@router.post("/append")
def v10_append(x_admin_token: Optional[str] = Header(None)):
    """Build + append latest closed 5m bars (NIFTY + BANKNIFTY) from live 1m feed."""
    _check_admin(x_admin_token)
    import v10_st_ema
    return v10_st_ema.build_and_append_5m()


@router.post("/tick")
def v10_tick(x_admin_token: Optional[str] = Header(None)):
    """Full 5-min cycle: append bars, run paper engine, Telegram alert on new entries.
    Scheduler hits this every 5 min during market hours."""
    _check_admin(x_admin_token)
    import v10_st_ema
    return v10_st_ema.tick()


# ---- Dashboard reads (no auth — display only) ----
@router.get("/positions")
def v10_positions():
    """Open paper positions (both indices)."""
    import v10_st_ema
    return {"open_positions": v10_st_ema.get_open_positions()}


@router.get("/trades")
def v10_trades(limit: int = 200):
    """Closed paper trade log with P&L."""
    import v10_st_ema
    return {"closed_trades": v10_st_ema.get_closed_trades(limit)}


@router.get("/summary")
def v10_summary():
    """Running settings + aggregate paper P&L summary."""
    import v10_st_ema
    return v10_st_ema.get_summary()
