"""
Performance Endpoints — Scorr (cc_task #9 subtask 5)
====================================================
Powers /performance (scorr_performance.html), 3 tabs:
  /api/performance/qb       — Quant Baskets: per-basket P&L + open positions
  /api/performance/alpha    — Alpha vs Nifty500 (per-basket return - benchmark)
  /api/performance/options  — Options (V5): per-underlying P&L + trade list

Pure SQL reads. Benchmark = nifty500_benchmark (weighted, base_date 2026-06-01).
"""

import os
from decimal import Decimal
import psycopg
from fastapi import APIRouter

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")
BENCH_BASE_DATE = "2026-06-01"


def _conn():
    return psycopg.connect(DATABASE_URL)


def _num(v):
    if isinstance(v, Decimal):
        return float(v)
    return v


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [{k: _num(v) for k, v in zip(cols, r)} for r in cur.fetchall()]


@router.get("/api/performance/qb")
def performance_qb():
    """Quant Baskets — per-basket P&L summary + all open positions."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT basket_name AS basket,
                   ROUND(SUM(pnl)::numeric, 0) AS pnl,
                   ROUND((SUM(pnl)/NULLIF(SUM(entry_price*qty),0)*100)::numeric, 2) AS return_pct,
                   COUNT(*) AS positions,
                   ROUND(SUM(current_value)::numeric, 0) AS market_value
            FROM quant_paper_positions WHERE status='open'
            GROUP BY basket_name ORDER BY pnl DESC
        """)
        baskets = _rows(cur)
        cur.execute("""
            SELECT basket_name AS basket, symbol,
                   ROUND(entry_price::numeric, 2) AS entry_price, qty,
                   ROUND(current_price::numeric, 2) AS current_price,
                   ROUND(pnl::numeric, 0) AS pnl, ROUND(pnl_pct::numeric, 2) AS pnl_pct,
                   gvm_at_entry AS gvm
            FROM quant_paper_positions WHERE status='open'
            ORDER BY pnl_pct DESC NULLS LAST
        """)
        positions = _rows(cur)
        cur.execute("SELECT ROUND(SUM(pnl)::numeric,0) AS pnl, COUNT(*) AS n FROM quant_paper_positions WHERE status='open'")
        tot = cur.fetchone()
    return {"tab": "quant_baskets",
            "total_pnl": _num(tot[0]) if tot and tot[0] is not None else 0.0,
            "total_positions": (tot[1] if tot else 0),
            "baskets": baskets, "positions": positions}


@router.get("/api/performance/alpha")
def performance_alpha():
    """Alpha vs Nifty500 — per-basket portfolio return minus weighted benchmark."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT ROUND((SUM(b.weight*(COALESCE(c.cmp,r.close)-b.entry_price)/b.entry_price)*100)::numeric, 3)
            FROM nifty500_benchmark b
            LEFT JOIN cmp_prices c ON c.symbol=b.symbol
            LEFT JOIN LATERAL (
                SELECT close FROM raw_prices WHERE symbol=b.symbol ORDER BY price_date DESC LIMIT 1
            ) r ON true
            WHERE b.base_date=%s
        """, (BENCH_BASE_DATE,))
        br = cur.fetchone()
        benchmark = _num(br[0]) if br and br[0] is not None else None
        cur.execute("""
            SELECT basket_name AS basket,
                   ROUND(SUM(pnl)::numeric, 0) AS pnl,
                   ROUND((SUM(pnl)/NULLIF(SUM(entry_price*qty),0)*100)::numeric, 2) AS return_pct,
                   COUNT(*) AS positions
            FROM quant_paper_positions WHERE status='open'
            GROUP BY basket_name ORDER BY return_pct DESC NULLS LAST
        """)
        baskets = _rows(cur)
        cur.execute("""
            SELECT ROUND((SUM(pnl)/NULLIF(SUM(entry_price*qty),0)*100)::numeric, 2) AS return_pct,
                   ROUND(SUM(pnl)::numeric, 0) AS pnl
            FROM quant_paper_positions WHERE status='open'
        """)
        t = cur.fetchone()
    for b in baskets:
        b["alpha"] = (round(b["return_pct"] - benchmark, 3)
                      if (b.get("return_pct") is not None and benchmark is not None) else None)
    total_ret = _num(t[0]) if t and t[0] is not None else None
    total = {"return_pct": total_ret, "pnl": _num(t[1]) if t and t[1] is not None else None,
             "alpha": (round(total_ret - benchmark, 3) if (total_ret is not None and benchmark is not None) else None)}
    return {"tab": "alpha", "benchmark": "Nifty500", "base_date": BENCH_BASE_DATE,
            "benchmark_return": benchmark, "baskets": baskets, "total": total}


@router.get("/api/performance/options")
def performance_options():
    """Options (V5) — per-underlying P&L summary + trade list."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT ticker, ROUND(SUM(pnl)::numeric, 0) AS pnl, COUNT(*) AS trades,
                   COUNT(*) FILTER (WHERE pnl > 0) AS wins,
                   COUNT(*) FILTER (WHERE pnl < 0) AS losses
            FROM options_trades GROUP BY ticker ORDER BY pnl DESC
        """)
        by_ticker = _rows(cur)
        cur.execute("""
            SELECT trade_date, ticker, option_type, ROUND(strike_price::numeric,0) AS strike,
                   action, lots, ROUND(entry_price::numeric,2) AS entry_price,
                   ROUND(exit_price::numeric,2) AS exit_price, ROUND(pnl::numeric,0) AS pnl,
                   status, strategy, remarks
            FROM options_trades ORDER BY trade_date DESC, id DESC
        """)
        trades = _rows(cur)
        for t in trades:
            t["trade_date"] = str(t["trade_date"])
        cur.execute("SELECT ROUND(SUM(pnl)::numeric,0) AS pnl, COUNT(*) AS n FROM options_trades")
        tot = cur.fetchone()
    return {"tab": "options", "strategy": "V5",
            "total_pnl": _num(tot[0]) if tot and tot[0] is not None else 0.0,
            "total_trades": (tot[1] if tot else 0),
            "by_ticker": by_ticker, "trades": trades}
