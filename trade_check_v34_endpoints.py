"""
Trade Check v3.4 endpoints — FastAPI router.
Side-by-side with v3.3 (conversational spec id=143). Does NOT replace it.

Routes:
  POST /api/trade-check/v34        — score a symbol (caller passes chart gates)
  POST /api/trade-check/v34/promote — manual promote a check to personal_journal
  GET  /api/trade-check/v34/health — sanity
  GET  /api/trade-check/screen-nifty50 — screener, both sides (id=371). n<=210 for All-208 tab.
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

import trade_check_v34 as tc
import native_trade_check as ntc

router = APIRouter()


class CheckRequest(BaseModel):
    symbol: str
    side: str = "LONG"           # LONG | SHORT
    gate_5min: bool = False      # caller IS the gate (human-in-AI-loop)
    gate_1day: bool = False
    render: bool = True          # also return text table


class PromoteRequest(BaseModel):
    symbol: str
    side: str = "LONG"
    gate_5min: bool = False
    gate_1day: bool = False
    qty: int
    entry_price: float
    notes: Optional[str] = ""


@router.post("/api/trade-check/v34")
def check(req: CheckRequest):
    result = tc.trade_check(req.symbol, req.side, req.gate_5min, req.gate_1day)
    if req.render and "error" not in result:
        result["table"] = tc.render_table(result)
    return result


@router.post("/api/trade-check/v34/promote")
def promote(req: PromoteRequest):
    """Manual-only promote. Scores then writes to personal_journal (never v8)."""
    result = tc.trade_check(req.symbol, req.side, req.gate_5min, req.gate_1day)
    if "error" in result:
        return result
    promo = tc.promote_to_personal_journal(
        result, req.qty, req.entry_price, req.notes or "")
    return {"check": result, "promote": promo}


@router.get("/api/trade-check/v34/health")
def health():
    return {
        "version": tc.VERSION,
        "parent_spec": tc.SPEC_PARENT,
        "max_weighted": tc.MAX_WEIGHTED,
        "thresholds": {"STRONG": tc.STRONG_MIN, "VALID": tc.VALID_MIN},
        "separation": "independent of V8 paper engine (id=210)",
        "status": "ok",
    }


@router.get("/api/trade-check/screen-nifty50")
def screen_nifty50(n: int = 50, top: int = 10):
    """Screener — runs native v3.4 trade check on the top-N by market cap,
    BOTH sides, returns top-`top` ranked each side.
    n=50 -> Nifty 50 (mcap proxy) tab. n=210 -> All futures (~208) tab.
    Same engine as the single check, run per symbol. WATCH kept (live v3.4).
    Spec: session_log id=371. On-demand only (heavy: N*2 DB passes)."""
    n = max(10, min(n, 210))
    top = max(1, min(top, 20))
    return ntc.screen_top50(n=n, top=top)
