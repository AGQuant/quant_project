"""
V8 Intraday Backtest Endpoints (v2.0)

POST /api/v8/backtest/run?basket=buy_reversal&score_offset=-2
     Run full optimizer sweep (no DB write — too many combos).

POST /api/v8/backtest/simulate?basket=buy_reversal&score_offset=-2&write_db=true
     Simulate with current BASELINE (or custom config) and write every
     entry + exit to v8_backtest_log.

GET  /api/v8/backtest/log?run_id=...&limit=200
     Read the trade log from v8_backtest_log.

GET  /api/v8/backtest/last
     Return results of the most recent /run call (in-memory).
"""
from fastapi import APIRouter, HTTPException
from datetime import datetime, timedelta, timezone
from typing import Optional
import os

router = APIRouter(prefix="/api/v8/backtest", tags=["v8_backtest"])

IST = timezone(timedelta(hours=5, minutes=30))
_last_result: dict = {}


@router.post("/run")
def run_backtest(basket: str = "buy_reversal", score_offset: int = -2):
    """
    Full optimizer: sweeps every filter parameter, returns ranked results.
    Does NOT write to DB (too many simulation runs).
    score_offset: -1 tight | -2 loose (default) | -3 very loose
    """
    try:
        from v8_intra_backtest import run_optimizer
        result = run_optimizer(basket, score_offset, write_db=False)
        result['run_at'] = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
        _last_result.clear()
        _last_result.update(result)
        return result
    except Exception as e:
        raise HTTPException(500, f"Backtest optimizer failed: {e}")


@router.post("/simulate")
def simulate(
    basket: str       = "buy_reversal",
    score_offset: int = -2,
    write_db: bool    = True,
    config: Optional[dict] = None,
):
    """
    Simulate with a given config (or BASELINE if none provided).
    Writes every entry + exit to v8_backtest_log when write_db=True.
    Returns: stats + full trade list.
    """
    try:
        from v8_intra_backtest import run_simulation, BASELINE
        cfg = None
        if config:
            cfg = {k: tuple(v) for k, v in config.items()}
        result = run_simulation(basket, score_offset, cfg, write_db)
        # Serialise timestamps in trade list
        trades = result.get('trades', [])
        for t in trades:
            for k, v in t.items():
                if hasattr(v, 'isoformat'):
                    t[k] = v.strftime('%Y-%m-%d %H:%M')
        return {**result, 'trades': trades}
    except Exception as e:
        raise HTTPException(500, f"Simulate failed: {e}")


@router.get("/log")
def backtest_log(
    run_id: Optional[str] = None,
    basket: Optional[str] = None,
    limit: int = 200,
):
    """
    Read trade log from v8_backtest_log.
    Filter by run_id and/or basket. Latest trades first.
    """
    try:
        import psycopg, os
        wheres = []
        params = []
        if run_id:
            wheres.append("run_id = %s");  params.append(run_id)
        if basket:
            wheres.append("basket = %s");  params.append(basket)
        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(min(limit, 1000))

        with psycopg.connect(os.getenv("DATABASE_URL")) as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT id, run_id, basket, symbol,
                           entry_date::text, entry_ts::text,
                           entry_price, pp, r1, s1, rr, filter_score,
                           exit_ts::text, exit_price, result, pnl_pct,
                           created_at::text
                    FROM v8_backtest_log
                    {where_sql}
                    ORDER BY id DESC
                    LIMIT %s
                """, params)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        wins   = sum(1 for r in rows if r['result'] == 'WIN')
        losses = sum(1 for r in rows if r['result'] == 'LOSS')
        closed = wins + losses
        return {
            'count':    len(rows),
            'wins':     wins,
            'losses':   losses,
            'win_rate': round(wins/closed*100, 1) if closed else None,
            'trades':   rows,
        }
    except Exception as e:
        raise HTTPException(500, f"Log read failed: {e}")


@router.get("/last")
def last_result():
    """Return results of the most recent /run optimizer call."""
    if not _last_result:
        raise HTTPException(
            404, "No optimizer run yet — POST /api/v8/backtest/run first.")
    return _last_result
