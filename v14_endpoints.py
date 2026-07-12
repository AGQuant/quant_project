"""
v14_endpoints.py — V14 intraday engine API (cc#442, spec id=3060).
Read surfaces for the /v14 page + a manual admin tick. Engine logic lives in v14_engine.py.
"""
import os
import logging
from typing import Optional

import psycopg
from fastapi import APIRouter, Header, HTTPException

import v14_engine

log = logging.getLogger("scorr.v14.api")
router = APIRouter(prefix="/api/v14", tags=["v14"])
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _conn():
    return psycopg.connect(DATABASE_URL)


def _check_admin(token: Optional[str]):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(401, "admin token required")


@router.get("/positions")
def v14_positions():
    """Live open V14 paper positions (tag + side + entry/stop/target + gates snapshot)."""
    with _conn() as conn:
        return {"open_positions": v14_engine.get_open(conn)}


@router.get("/trades")
def v14_trades(limit: int = 200):
    """Closed V14 paper trades — full log with per-trade gates snapshot + P&L (pts/%/net)."""
    with _conn() as conn:
        return {"closed_trades": v14_engine.get_trades(conn, limit)}


@router.get("/summary")
def v14_summary(trade_date: Optional[str] = None):
    """Per-tag day summary + daily P&L view (open MTM + closed realized) + day-log history."""
    with _conn() as conn:
        tags = v14_engine.get_tag_summary(conn, trade_date)
        daily = v14_engine.get_daily_pnl(conn)
        day_log = v14_engine.get_day_log(conn)
    return {"trade_date": trade_date, "by_tag": tags, "daily_pnl": daily, "day_log": day_log,
            "spec": "V14 P1.1 · ORB / VWAP-RECLAIM / R1-REJ · paper · id=3062/3063/3064 (final)",
            "cost": {"flat_rs": v14_engine.COST_FLAT, "slippage_pct": v14_engine.COST_SLIPPAGE},
            "max_slots": v14_engine.MAX_SLOTS, "clock": v14_engine.CLOCK_WINDOWS,
            "execution": "signals on equity 5m; execution/MTM on futures 5m"}


@router.post("/tick")
def v14_tick(x_admin_token: Optional[str] = Header(None)):
    """Run ONE 5-min V14 cycle now (manage exits + evaluate + paper-open). Admin/manual + scheduler."""
    _check_admin(x_admin_token)
    with _conn() as conn:
        return v14_engine.run_v14_cycle(conn)
