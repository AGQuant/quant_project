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


# ── cc#629: V9 SECTOR PAIRS "BRAHMASTRA" paper book (v9_paper_engine) ────────────────────────────
# Backtest context strip (session_log 8169) — clearly labelled BACKTEST alongside live paper numbers.
V9_BACKTEST = {"avg_monthly_pct": 2.70, "sharpe": 1.63, "months": 57, "pos_months_pct": 69,
               "annualized_pct": 32, "label": "BACKTEST 57mo (before costs) · paper is the judge"}


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


@router.get("/paper/open")
def v9_paper_open():
    """cc#629: OPEN Brahmastra pairs with live MTM per leg + spread P&L (long: cur-entry; short:
    entry-cur; ×lot). Current price = latest raw_prices close. Held to the next monthly rebalance."""
    rows = api_query("""SELECT id, rebalance_date, segment, long_symbol, short_symbol, long_lot,
                               short_lot, long_entry, short_entry, gvm_long, gvm_short, gvm_gap,
                               dm_mag_long, dm_mag_short, entry_date
                        FROM v9_paper_positions WHERE status='OPEN'
                        ORDER BY rebalance_date DESC, segment""")
    if isinstance(rows, dict):
        return rows
    syms = sorted({r["long_symbol"] for r in rows} | {r["short_symbol"] for r in rows})
    cur_px = {}
    if syms:
        px = api_query("""SELECT DISTINCT ON (symbol) symbol, close FROM raw_prices
                          WHERE symbol = ANY(%s) AND close IS NOT NULL
                          ORDER BY symbol, price_date DESC""", (syms,))
        if isinstance(px, list):
            cur_px = {r["symbol"]: _f(r["close"]) for r in px}
    out = []
    for r in rows:
        le, se = _f(r["long_entry"]), _f(r["short_entry"])
        lc, sc = cur_px.get(r["long_symbol"]), cur_px.get(r["short_symbol"])
        llot, slot = int(r["long_lot"] or 1), int(r["short_lot"] or 1)
        long_pnl = (lc - le) * llot if (lc is not None and le is not None) else None
        short_pnl = (se - sc) * slot if (sc is not None and se is not None) else None
        spread = (long_pnl or 0) + (short_pnl or 0) if (long_pnl is not None and short_pnl is not None) else None
        notional = (le * llot + se * slot) if (le and se) else None
        out.append({**r,
                    "long_cmp": lc, "short_cmp": sc,
                    "long_pnl": round(long_pnl, 2) if long_pnl is not None else None,
                    "short_pnl": round(short_pnl, 2) if short_pnl is not None else None,
                    "spread_pnl": round(spread, 2) if spread is not None else None,
                    "ret_pct": round(spread / notional * 100, 2) if (spread is not None and notional) else None})
    return {"open_pairs": out, "count": len(out), "backtest": V9_BACKTEST}


@router.get("/paper/closed")
def v9_paper_closed(limit: int = 120):
    """cc#629: closed Brahmastra pairs — monthly history, newest exit first."""
    return api_query("""SELECT rebalance_date, exit_date, segment, long_symbol, short_symbol,
                               long_lot, short_lot, long_entry, long_exit, short_entry, short_exit,
                               spread_pnl, ret_pct, hold_days
                        FROM v9_paper_trades ORDER BY exit_date DESC, id DESC LIMIT %s""",
                     (min(max(limit, 1), 500),))


@router.get("/paper/summary")
def v9_paper_summary():
    """cc#629: summary tiles — months live, avg monthly %, win-rate, cumulative % (closed trades),
    plus the labelled backtest context. Live = paper only; backtest shown separately."""
    trades = api_query("""SELECT exit_date, ret_pct FROM v9_paper_trades WHERE ret_pct IS NOT NULL""")
    if isinstance(trades, dict):
        return trades
    from collections import defaultdict
    by_month = defaultdict(list)
    for t in trades:
        ed = t["exit_date"]
        key = ed.strftime("%Y-%m") if hasattr(ed, "strftime") else str(ed)[:7]
        by_month[key].append(_f(t["ret_pct"]) or 0.0)
    month_rets = {m: round(sum(v) / len(v), 2) for m, v in by_month.items()} if by_month else {}
    n_months = len(month_rets)
    avg_monthly = round(sum(month_rets.values()) / n_months, 2) if n_months else None
    pos_months = sum(1 for v in month_rets.values() if v > 0)
    wins = sum(1 for t in trades if (_f(t["ret_pct"]) or 0) > 0)
    wr = round(wins / len(trades) * 100, 1) if trades else None
    cumulative = round(sum(_f(t["ret_pct"]) or 0 for t in trades), 2) if trades else 0.0
    open_ct = api_query("SELECT COUNT(*) AS n FROM v9_paper_positions WHERE status='OPEN'", single=True)
    return {"months_live": n_months, "avg_monthly_pct": avg_monthly,
            "pos_months": pos_months, "closed_trades": len(trades),
            "win_rate": wr, "cumulative_pct": cumulative,
            "open_pairs": (open_ct or {}).get("n", 0) if isinstance(open_ct, dict) else 0,
            "backtest": V9_BACKTEST}


@router.post("/paper/rebalance")
def v9_paper_rebalance(x_admin_token: Optional[str] = Header(None)):
    """cc#629: manual trigger of the monthly Brahmastra rebalance (idempotent per date). The scheduled
    run is the nightly first-trading-day job; this is the admin arm for the same engine."""
    _check_admin(x_admin_token)
    try:
        import v9_paper_engine
        return v9_paper_engine.run_monthly()
    except Exception as e:
        raise HTTPException(500, str(e))
