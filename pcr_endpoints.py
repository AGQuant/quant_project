"""
pcr_endpoints.py — 5-min intraday PCR routes (Scorr platform).
Mounted in main.py via: app.include_router(pcr_router)
Read = no auth (display). Compute = admin-gated.
"""
import os
from typing import Optional
from fastapi import APIRouter, Header, HTTPException

router = APIRouter(prefix="/api/pcr", tags=["pcr"])
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _check_admin(token: Optional[str]):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


@router.get("/intraday")
def pcr_intraday_trend(underlying: str = "NIFTY", days: int = 2):
    """5-min PCR trend (ATM±5 + total) for an underlying over the last N days."""
    import pcr_intraday
    return pcr_intraday.get_pcr_intraday(underlying=underlying, days=days)


@router.post("/intraday/compute")
def pcr_intraday_compute(ts: Optional[str] = None, x_admin_token: Optional[str] = Header(None)):
    """Compute 5-min PCR. No ts = self-heal all missing bars; ts = recompute one bar."""
    _check_admin(x_admin_token)
    import pcr_intraday
    return pcr_intraday.compute_pcr_intraday(ts=ts)
