"""
Native Trade Check endpoint — POST /api/check

Zero-token, pure Railway DB. Powers the dedicated CHECK page (/check).
Independent of /api/scorr/chat (no Claude, no ADMIN_TOKEN, no MCP) so it
never hangs on the 20-40s tool path. v3.3 ONLY.

Body: { "symbol": "RELIANCE", "side": "LONG"|"SHORT",
        "gate1": true|false|null, "gate2": true|false|null }
gate1 = 5-min strength through day; gate2 = 1-Day reversal/breakout.
Returns the structured dict from compute_trade_check().
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from native_trade_check import compute_trade_check

router = APIRouter()


class CheckRequest(BaseModel):
    symbol: str
    side: Optional[str] = "LONG"
    gate1: Optional[bool] = None
    gate2: Optional[bool] = None


@router.post("/api/check")
def api_check(req: CheckRequest):
    side = (req.side or "LONG").upper()
    if side not in ("LONG", "SHORT"):
        side = "LONG"
    return compute_trade_check(req.symbol, side, req.gate1, req.gate2)


@router.get("/api/check/health")
def api_check_health():
    return {"status": "ok", "engine": "native_v3.3", "cost": "$0",
            "needs_admin_token": False, "needs_claude": False}
