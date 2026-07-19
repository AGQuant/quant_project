"""
bt6_endpoints.py — V6 Backtest (BT) playground routes (Scorr platform).
Mounted in main.py via: app.include_router(bt6_router)

READ-MOSTLY WRAPPER (cc#544): thin API layer over the EXISTING bt6 engine owned by
Claude-web (ownership rule id=4708). This file NEVER creates/alters any bt* SQL
function or table — it only CALLS bt6_launch(...) and READs bt6_runs / bt6_trades.

Engine surface (read/call only, never alter):
  - bt6_launch(basket text, params jsonb) RETURNS integer (new run_id). Runs 30-120s
    and REJECTS concurrent runs by raising an error (surfaced here as HTTP 409).
  - bt6_runs   : run_id, basket, run_label, params, status, n_trades, wins, wr_pct,
                 avg_ret_pct, net_ret_pct, started_at, finished_at, error
  - bt6_trades : run_id, symbol, entry_ts, entry_px, exit_ts, exit_px, exit_reason, ret_pct
"""
import os
import json
from datetime import datetime, date
from decimal import Decimal

import psycopg
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/bt6", tags=["bt6"])


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _jsafe(v):
    """Coerce a DB value into something json-serializable (cc#544 spec: timestamps
    to str, numerics to float). jsonb columns already arrive as dict/list."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return str(v)
    return v


def _rows_to_dicts(cur):
    cols = [c.name for c in cur.description]
    return [{c: _jsafe(val) for c, val in zip(cols, row)} for row in cur.fetchall()]


@router.post("/run")
def bt6_run(body: dict):
    """Launch a backtest run via the existing engine. body: {basket: str, params: dict}.
    Long-running (30-120s) — the DB statement is left to run to completion (no short
    timeout imposed). The engine rejects concurrent runs by raising; that (and any other
    engine error) is surfaced cleanly as HTTP 409 {"error": "<message>"} for the UI."""
    basket = (body or {}).get("basket")
    params = (body or {}).get("params") or {}
    if not basket:
        return JSONResponse(status_code=400, content={"error": "basket is required"})
    try:
        # Fresh connection; no statement_timeout set so a full 30-120s run completes.
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT bt6_launch(%s, %s::jsonb)", (basket, json.dumps(params)))
            run_id = cur.fetchone()[0]
        return {"run_id": int(run_id)}
    except Exception as e:
        # Concurrent-run guard + any engine-raised error -> 409 with the raw message.
        return JSONResponse(status_code=409, content={"error": str(e)})


@router.get("/runs")
def bt6_runs():
    """Run history, newest first. Read-only."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM bt6_runs ORDER BY run_id DESC")
        return {"runs": _rows_to_dicts(cur)}


@router.get("/trades")
def bt6_trades(run_id: int):
    """Trade log for one run, ordered by entry time. Read-only."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM bt6_trades WHERE run_id=%s ORDER BY entry_ts", (run_id,))
        return {"run_id": run_id, "trades": _rows_to_dicts(cur)}


@router.delete("/run/{run_id}")
def bt6_delete(run_id: int):
    """Delete a run + its trades (trades first, in case there is no FK cascade)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM bt6_trades WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM bt6_runs WHERE run_id=%s", (run_id,))
    return {"deleted": run_id}
