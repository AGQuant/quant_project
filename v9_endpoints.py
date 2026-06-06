"""
V9 Pair Strategy — FastAPI Endpoints
======================================
Wired into main.py via app.include_router(v9_router).

Endpoints:
  POST /api/v9/discover        — run pair discovery (v9_pair_discovery.py)
  POST /api/v9/backtest        — run full backtest (v9_pair_backtest.py)
  GET  /api/v9/pairs           — list valid pairs from pair_universe
  GET  /api/v9/results         — backtest results summary per combo
  GET  /api/v9/results/{combo} — detailed results for one combo
  GET  /api/v9/trades/{combo}  — all trades for one combo
  GET  /api/v9/best_combo      — best combo by total PnL
"""

from fastapi import APIRouter, HTTPException, Header
from typing import Optional
import psycopg
import os

router = APIRouter(prefix="/api/v9", tags=["v9_pair"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _check_admin(token):
    if not ADMIN_TOKEN: return True
    if token != ADMIN_TOKEN: raise HTTPException(403, "Invalid admin token")
    return True


def api_query(sql, params=None, single=False):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params or ())
            cols = [d[0] for d in cur.description] if cur.description else []
            if single:
                r = cur.fetchone(); return dict(zip(cols, r)) if r else None
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        return {"error": str(e)}


@router.post("/discover")
def v9_discover(x_admin_token: Optional[str] = Header(None)):
    """Run V9 pair discovery — finds valid pairs from 209 futures universe."""
    _check_admin(x_admin_token)
    try:
        import v9_pair_discovery
        return v9_pair_discovery.run_discovery()
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/backtest")
def v9_backtest(x_admin_token: Optional[str] = Header(None)):
    """Run V9 backtest — all 10 combos on all valid pairs."""
    _check_admin(x_admin_token)
    try:
        import v9_pair_backtest
        return v9_pair_backtest.run_backtest()
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/pairs")
def v9_pairs(segment: Optional[str] = None):
    """List all valid pairs from pair_universe."""
    if segment:
        return api_query("""
            SELECT symbol_a, symbol_b, segment, correlation, coint_pvalue,
                   hedge_ratio, discovery_date
            FROM pair_universe
            WHERE is_active = TRUE AND segment = %s
            ORDER BY correlation DESC
        """, (segment,))
    return api_query("""
        SELECT symbol_a, symbol_b, segment, correlation, coint_pvalue,
               hedge_ratio, discovery_date
        FROM pair_universe
        WHERE is_active = TRUE
        ORDER BY segment, correlation DESC
    """)


@router.get("/results")
def v9_results():
    """Backtest results summary — aggregated per combo."""
    return api_query("""
        SELECT
            combo_id,
            z_entry, z_exit, z_stop, zscore_window, hedge_recompute,
            COUNT(DISTINCT symbol_a || '/' || symbol_b) AS pairs_tested,
            SUM(total_trades)                           AS total_trades,
            ROUND(AVG(win_rate)::numeric, 2)            AS avg_win_rate,
            ROUND(SUM(total_pnl)::numeric, 2)           AS total_pnl,
            ROUND(AVG(avg_return_pct)::numeric, 4)      AS avg_return_pct,
            ROUND(AVG(sharpe_ratio)::numeric, 4)        AS avg_sharpe,
            ROUND(AVG(max_drawdown)::numeric, 4)        AS avg_max_drawdown,
            ROUND(AVG(profit_factor)::numeric, 4)       AS avg_profit_factor,
            ROUND(AVG(stop_rate)::numeric, 2)           AS avg_stop_rate,
            ROUND(AVG(time_stop_rate)::numeric, 2)      AS avg_time_stop_rate,
            ROUND(AVG(avg_holding_days)::numeric, 1)    AS avg_holding_days
        FROM pair_backtest_results
        GROUP BY combo_id, z_entry, z_exit, z_stop, zscore_window, hedge_recompute
        ORDER BY total_pnl DESC
    """)


@router.get("/results/{combo_id}")
def v9_results_combo(combo_id: int):
    """Detailed results for one combo — one row per pair."""
    return api_query("""
        SELECT symbol_a, symbol_b, segment,
               total_trades, win_rate, stop_rate, time_stop_rate,
               total_pnl, avg_return_pct, avg_holding_days,
               sharpe_ratio, max_drawdown, profit_factor
        FROM pair_backtest_results
        WHERE combo_id = %s
        ORDER BY total_pnl DESC
    """, (combo_id,))


@router.get("/trades/{combo_id}")
def v9_trades(combo_id: int, symbol_a: Optional[str] = None,
              symbol_b: Optional[str] = None, limit: int = 200):
    """All trades for a combo. Optionally filter by pair."""
    if symbol_a and symbol_b:
        return api_query("""
            SELECT symbol_a, symbol_b, direction, entry_date, exit_date,
                   entry_z, exit_z, entry_price_a, entry_price_b,
                   exit_price_a, exit_price_b, lot_size_a, lot_size_b,
                   pnl_a, pnl_b, total_pnl, return_pct, holding_days, exit_reason
            FROM pair_backtest_trades
            WHERE combo_id=%s AND symbol_a=%s AND symbol_b=%s
            ORDER BY entry_date ASC
        """, (combo_id, symbol_a.upper(), symbol_b.upper()))
    return api_query("""
        SELECT symbol_a, symbol_b, direction, entry_date, exit_date,
               entry_z, exit_z, total_pnl, return_pct, holding_days, exit_reason
        FROM pair_backtest_trades
        WHERE combo_id = %s
        ORDER BY entry_date ASC LIMIT %s
    """, (combo_id, limit))


@router.get("/best_combo")
def v9_best_combo():
    """Best combo by total PnL across all pairs."""
    return api_query("""
        SELECT
            combo_id, z_entry, z_exit, z_stop, zscore_window, hedge_recompute,
            COUNT(DISTINCT symbol_a || '/' || symbol_b) AS pairs,
            SUM(total_trades)                           AS total_trades,
            ROUND(AVG(win_rate)::numeric, 2)            AS win_rate,
            ROUND(SUM(total_pnl)::numeric, 2)           AS total_pnl,
            ROUND(AVG(sharpe_ratio)::numeric, 4)        AS sharpe,
            ROUND(AVG(max_drawdown)::numeric, 4)        AS max_drawdown,
            ROUND(AVG(profit_factor)::numeric, 4)       AS profit_factor
        FROM pair_backtest_results
        GROUP BY combo_id, z_entry, z_exit, z_stop, zscore_window, hedge_recompute
        ORDER BY total_pnl DESC LIMIT 1
    """, single=True)
