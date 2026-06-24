"""
Scanner Endpoints — Scorr (18-Jun-2026)
Three smart screeners, pure SQL reads from existing live data.
No new compute. Instant sub-second response.

  /api/scanners/positional — V8 qualified signals today (v8_qualified)
  /api/scanners/investment — GVM>=7 quality stocks (gvm_scores)
  /api/scanners/tc_lite    — TC Lite intraday signal screener (cc_task #77)

  /api/scanners/intraday   — moved to intraday_scanner_endpoints.py (4-gate V1, 18-Jun-2026)
"""

import os
from typing import Optional
import psycopg
from fastapi import APIRouter

from tc_lite_scanner import router as tc_lite_router

router = APIRouter()
router.include_router(tc_lite_router)   # cc_task #77 — TC Lite screener (/api/scanners/tc_lite)
DATABASE_URL = os.getenv("DATABASE_URL", "")

def _conn():
    return psycopg.connect(DATABASE_URL)


@router.get("/api/scanners/positional")
def scanner_positional(
    basket: Optional[str] = None,
    min_gvm: float = 6.5,
    limit: int = 60,
):
    """V8 qualified signals today — positional swing setups (1-5 days)."""
    with _conn() as conn, conn.cursor() as cur:
        basket_filter = "AND q.basket = %s" if basket else ""
        params = [min_gvm]
        if basket:
            params.append(basket)
        params.append(limit)

        cur.execute(f"""
            SELECT
                q.symbol, q.basket,
                ROUND(q.gvm_score::numeric, 1)   AS gvm,
                ROUND(q.rsi_month::numeric, 1)   AS rsi_m,
                ROUND(q.rsi_weekly::numeric, 1)  AS rsi_w,
                ROUND(q.week_return::numeric, 2) AS week_pct,
                ROUND(q.mom_2d::numeric, 2)      AS mom_2d_pct,
                ROUND(q.dma_50::numeric, 1)      AS dma_50_pct,
                ROUND(q.sector_week::numeric, 2) AS sector_week_pct,
                ROUND(q.sector_day::numeric, 2)  AS sector_day_pct,
                c.cmp,
                p.pp, p.r1, p.s1
            FROM v8_qualified q
            LEFT JOIN cmp_prices c ON c.symbol = q.symbol
            LEFT JOIN v8_paper_pivots p ON p.symbol = q.symbol
                AND p.pivot_date = (SELECT MAX(pivot_date) FROM v8_paper_pivots)
            WHERE q.signal_date = CURRENT_DATE
              AND (q.gvm_score IS NULL OR q.gvm_score >= %s)
              {basket_filter}
            ORDER BY q.basket, q.gvm_score DESC NULLS LAST
            LIMIT %s
        """, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    by_basket: dict = {}
    for r in rows:
        b = r["basket"]
        by_basket.setdefault(b, []).append(r)

    return {"scanner": "positional", "count": len(rows), "by_basket": by_basket, "rows": rows}


@router.get("/api/scanners/investment")
def scanner_investment(
    min_gvm: float = 7.0,
    verdict: Optional[str] = None,
    limit: int = 50,
):
    """Investment quality screen — GVM>=7 stocks for buy-and-hold consideration."""
    with _conn() as conn, conn.cursor() as cur:
        verdict_filter = "AND g.verdict = %s" if verdict else \
                         "AND g.verdict IN ('Excellent', 'Good')"
        params = [min_gvm]
        if verdict:
            params.append(verdict)
        params.append(limit)

        cur.execute(f"""
            SELECT
                g.symbol,
                ROUND(g.gvm_score::numeric, 2)  AS gvm,
                ROUND(g.g_score::numeric, 2)    AS g,
                ROUND(g.v_score::numeric, 2)    AS v,
                ROUND(g.m_score::numeric, 2)    AS m,
                g.verdict, g.segment,
                ROUND((g.market_cap/100000)::numeric, 0) AS mcap_lcr,
                c.cmp,
                ROUND(vm.week_return::numeric, 2)  AS week_pct,
                ROUND(vm.month_return::numeric, 2) AS month_pct
            FROM gvm_scores g
            LEFT JOIN cmp_prices c ON c.symbol = g.symbol
            LEFT JOIN LATERAL (
                SELECT week_return, month_return FROM v8_metrics
                WHERE symbol = g.symbol ORDER BY score_date DESC LIMIT 1
            ) vm ON true
            WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
              AND g.gvm_score >= %s
              {verdict_filter}
            ORDER BY g.gvm_score DESC
            LIMIT %s
        """, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    return {"scanner": "investment", "count": len(rows),
            "filters": {"min_gvm": min_gvm, "verdict": verdict or "Excellent + Good"}, "rows": rows}
