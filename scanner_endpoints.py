"""
Scanner Endpoints — Scorr (18-Jun-2026)
Three smart screeners, pure SQL reads from existing live data.
No new compute. Instant sub-second response.

  /api/scanners/intraday   — live strength today (v8_metrics, 5-min live)
  /api/scanners/positional — V8 qualified signals today (v8_qualified)
  /api/scanners/investment — GVM>=7 quality stocks (gvm_scores)
"""

import os
from typing import Optional
import psycopg
from fastapi import APIRouter

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")

def _conn():
    return psycopg.connect(DATABASE_URL)


@router.get("/api/scanners/intraday")
def scanner_intraday(
    min_day1d: float = 0.3,
    min_vol_ratio: float = 1.0,
    limit: int = 30,
):
    """Live intraday strength — stocks up today with volume confirmation."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT
                m.symbol,
                ROUND(m.day_1d::numeric, 2)        AS day_1d_pct,
                ROUND(m.mom_2d::numeric, 2)         AS mom_2d_pct,
                ROUND(m.vol_ratio::numeric, 2)      AS vol_ratio,
                ROUND(m.sector_day::numeric, 2)     AS sector_day_pct,
                ROUND(m.sector_week::numeric, 2)    AS sector_week_pct,
                ROUND(m.dma_50::numeric, 1)         AS dma_50_pct,
                ROUND(m.rsi_weekly::numeric, 1)     AS rsi_weekly,
                ROUND(g.gvm_score::numeric, 1)      AS gvm,
                g.segment,
                c.cmp
            FROM v8_metrics m
            LEFT JOIN gvm_scores g ON g.symbol = m.symbol
                AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
            LEFT JOIN cmp_prices c ON c.symbol = m.symbol
            WHERE m.score_date = CURRENT_DATE
              AND m.day_1d >= %s
              AND (m.vol_ratio IS NULL OR m.vol_ratio >= %s)
              AND m.dma_50 > 0
            ORDER BY m.day_1d DESC
            LIMIT %s
        """, (min_day1d, min_vol_ratio, limit))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    return {"scanner": "intraday", "count": len(rows),
            "filters": {"min_day1d": min_day1d, "min_vol_ratio": min_vol_ratio}, "rows": rows}


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
                         "AND g.verdict IN ('Strong Buy', 'Buy')"
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
            "filters": {"min_gvm": min_gvm, "verdict": verdict or "Strong Buy + Buy"}, "rows": rows}
