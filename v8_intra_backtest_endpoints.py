"""
V8 Intraday Backtest Endpoints
POST /api/v8/backtest/run?basket=buy_reversal&score_offset=-2
GET  /api/v8/backtest/last
POST /api/v8/backtest/simulate   — custom filter config
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
    Run intraday backtest optimizer for a basket.
    Sweeps each filter parameter one-at-a-time across preset grid.
    score_offset: -1 tight | -2 loose (default) | -3 very loose
    Uses available intraday_prices (rolling 6-7 trading days of 5-min bars).
    Returns: per-param rankings + recommended config + baseline vs recommended stats.
    """
    try:
        from v8_intra_backtest import run_optimizer
        result = run_optimizer(basket, score_offset)
        result['run_at'] = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
        # Slim per_param to top-5 per parameter
        for param in result.get('per_param', {}):
            result['per_param'][param] = result['per_param'][param][:5]
        _last_result.clear()
        _last_result.update(result)
        return result
    except Exception as e:
        raise HTTPException(500, f"Backtest failed: {e}")


@router.get("/last")
def last_result():
    """Return results of the most recent backtest run."""
    if not _last_result:
        raise HTTPException(
            404, "No backtest run yet — POST /api/v8/backtest/run first.")
    return _last_result


@router.post("/simulate")
def simulate_with_config(
    basket: str = "buy_reversal",
    score_offset: int = -2,
    config: Optional[dict] = None,
):
    """
    Simulate trades with a fully custom filter config.
    config: dict mapping metric → [min, max] (None = no bound).
    Returns: stats + first 50 trades.
    """
    try:
        from v8_intra_backtest import (
            load_all, simulate_basket, stats, BASELINE
        )
        import psycopg, os

        cfg = {}
        if config:
            for k, v in config.items():
                mn, mx = (v[0], v[1]) if isinstance(v, list) else (v, v)
                cfg[k] = (mn, mx)
        else:
            cfg = dict(BASELINE)

        with psycopg.connect(os.getenv("DATABASE_URL")) as conn:
            bars, met, pivots = load_all(conn)

        trades    = simulate_basket(bars, met, pivots, cfg, score_offset)
        trade_stats = stats(trades, "custom")

        return {
            'config':        {k: list(v) for k, v in cfg.items()},
            'score_offset':  score_offset,
            'stats':         trade_stats,
            'total_trades':  len(trades),
            'trades':        [
                {k: str(v) if hasattr(v, 'date') else v
                 for k, v in t.items()}
                for t in trades[:50]
            ],
        }
    except Exception as e:
        raise HTTPException(500, f"Simulate failed: {e}")
