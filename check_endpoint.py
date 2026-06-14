"""
Native Trade Check endpoints — v3.3, zero-token, pure Railway DB.
No Claude, no ADMIN_TOKEN, no MCP. Engine: native_trade_check v3 (all-auto).

POST /api/check                      composite — full Tier1+Tier2 card
                                     side=INVEST -> fundamental buy-and-hold card
GET  /api/check/rule/{rule}          single parameter (R1..R12, F1..F7)
GET  /api/check/health
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from native_trade_check import compute_trade_check, compute_single_rule
from invest_check import compute_invest_check

router = APIRouter()


class CheckRequest(BaseModel):
    symbol: str
    side: Optional[str] = "LONG"
    gate1: Optional[bool] = None   # optional human override for R10
    gate2: Optional[bool] = None   # optional human override for R12


@router.post("/api/check")
def api_check(req: CheckRequest):
    side = (req.side or "LONG").upper()
    if side == "INVEST":
        return compute_invest_check(req.symbol)
    if side not in ("LONG", "SHORT"):
        side = "LONG"
    return compute_trade_check(req.symbol, side, req.gate1, req.gate2)


@router.get("/api/check/rule/{rule}")
def api_check_rule(rule: str, symbol: str, side: str = "LONG"):
    side = side.upper()
    if side not in ("LONG", "SHORT"):
        side = "LONG"
    return compute_single_rule(symbol, side, rule)


@router.get("/api/check/health")
def api_check_health():
    return {"status": "ok", "engine": "native_v3.3_all_auto_v3", "cost": "$0",
            "auto_params": 18, "gates": "optional overrides for R10/R12",
            "modes": ["LONG", "SHORT", "INVEST"],
            "needs_admin_token": False, "needs_claude": False}
