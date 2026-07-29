"""
tc_sim_endpoints.py — cc#748: TC outcome sim API (backs the MCP Scorr:tc_sim_summary tool).

GET  /api/tc-sim/summary   open positions + closed stats (WR/avg pnl/avg hours/exit split) by style+dir
POST /api/tc-sim/tick      manual one-shot tick (off-hours testing / backfill); the scheduler fires it
                           automatically on each hourly entry mark on trading days.
Dashboard surface deferred (cc#748: MCP readout first). Context isolation id=244: no V8 objects.
"""

from fastapi import APIRouter
import tc_sim

router = APIRouter()


@router.get("/api/tc-sim/summary")
def tc_sim_summary():
    return tc_sim.tc_sim_summary()


@router.post("/api/tc-sim/tick")
def tc_sim_tick():
    return tc_sim.run_tc_sim_tick()
