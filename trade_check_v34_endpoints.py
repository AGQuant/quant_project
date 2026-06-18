"""
Trade Check v3.4 endpoints — FastAPI router.
Side-by-side with v3.3 (conversational spec id=143). Does NOT replace it.

Routes:
  POST /api/trade-check/v34        — score a symbol (caller passes chart gates)
  POST /api/trade-check/v34/promote — manual promote a check to personal_journal
  GET  /api/trade-check/v34/health — sanity
  GET  /api/trade-check/screen-nifty50 — screener, both sides (id=371). n<=210 for All-208 tab.
  POST /api/trade-check/tc-cache/refresh — recompute tc_cache snapshot (id=373). Manual, standalone.
  GET  /api/trade-check/intraday-scan — two-stage intraday scan: cached TC>=10 + live filters (id=373).
  GET  /api/trade-check/intraday-paper/status — intraday paper engine: open positions + trades (18-Jun-2026).
  POST /api/trade-check/intraday-paper/run — manual intraday paper tick (scan+enter+exit).
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

import trade_check_v34 as tc
import native_trade_check as ntc
import tc_intraday as tci

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


@router.post("/api/trade-check/tc-cache/refresh")
def tc_cache_refresh(n: int = 210):
    """Recompute tc_cache snapshot (TC score per symbol x side) for top-N mcap.
    Standalone/manual in phase 1; scheduler wiring deferred to phase 1.5.
    Spec id=373. Heavy: N*2 computes."""
    n = max(10, min(n, 210))
    return tci.refresh_tc_cache(n=n)


@router.get("/api/trade-check/intraday-scan")
def intraday_scan(side: str = "LONG"):
    """Two-stage intraday scan (id=373). Stage1: cached TC>=10 universe.
    Stage2 (live): 1H positive + vol pace>=1.5x + gain 1-2% band + DMA20 + week-low-S1 + hold-open.
    LONG/SHORT separate. Reads tc_cache (refresh it first for fresh scores)."""
    side = "SHORT" if side.upper() == "SHORT" else "LONG"
    return tci.intraday_scan(side=side)


@router.get("/api/trade-check/intraday-paper/status")
def intraday_paper_status():
    """Intraday paper engine status (18-Jun-2026): open positions + today's
    closed trades + summary. Auto-runs every 5-min via scheduler.
    Context-isolated (tc_intraday_* tables) — never mixes with v8_paper."""
    return tci.intraday_paper_status()


@router.post("/api/trade-check/intraday-paper/run")
def intraday_paper_run():
    """Manual trigger: scan + auto-enter + exit check. Same as the 5-min
    scheduler tick. For testing / on-demand. Heavy (refreshes tc_cache)."""
    rc = tci.refresh_tc_cache()
    en = tci.run_intraday_paper_entry()
    ex = tci.run_intraday_paper_exit()
    return {"ok": True, "cache_written": rc.get("written"),
            "entered": en.get("entered"), "closed": ex.get("closed"),
            "square_off": ex.get("square_off"), "ts": en.get("ts")}
