"""
v10_endpoints.py — V10 ST+EMA intraday strategy routes (Scorr platform).
Mounted in main.py via: app.include_router(v10_router)
Isolated from V8 / live feed writes. Signals are advisory only.
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
    """Current signal from the latest CLOSED 10m bar (reads nifty_5m_test_data)."""
    import v10_st_ema
    return v10_st_ema.current_signal()


@router.post("/append")
def v10_append(x_admin_token: Optional[str] = Header(None)):
    """Build + append the latest closed 5m bar from the live 1m feed."""
    _check_admin(x_admin_token)
    import v10_st_ema
    return v10_st_ema.build_and_append_5m()


@router.post("/tick")
def v10_tick(x_admin_token: Optional[str] = Header(None)):
    """Full 5-min cycle: append 5m bar, compute signal, Telegram alert if BUY/SELL.
    This is the route the scheduler hits every 5 minutes during market hours."""
    _check_admin(x_admin_token)
    import v10_st_ema
    return v10_st_ema.tick()
