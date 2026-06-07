"""
v10_endpoints.py — V10 ST+EMA strategy routes (Scorr platform).
Mounted in main.py via: app.include_router(v10_router)
Isolated from V8 / live feed. Signals are advisory only.
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
    """Latest actionable signal from live Fyers 10m data."""
    import v10_st_ema
    return v10_st_ema.current_signal(live=True)


@router.post("/run_alert")
def v10_run_alert(x_admin_token: Optional[str] = Header(None)):
    """Compute signal and fire Telegram alert if BUY/SELL."""
    _check_admin(x_admin_token)
    import v10_st_ema
    return v10_st_ema.run_and_alert()


@router.get("/backtest")
def v10_backtest():
    """Signal computed on static history (validation, no live fetch)."""
    import v10_st_ema
    return v10_st_ema.current_signal(live=False)
