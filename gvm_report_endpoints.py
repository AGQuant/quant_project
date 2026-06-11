"""
gvm_report_endpoints.py — GVM company analytics report API.

Routes:
    GET /api/gvm/search?q=...        -> autocomplete (symbol/name)
    GET /api/gvm/company/{symbol}    -> full peer-benchmarked report
    GET /api/gvm/report/health       -> sanity check

The company report merges:
    - peer benchmark + pillars (gvm_company_report.build_company_report)
    - narrative content overview / key_takeaway / result_analysis (input_raw)
"""

import os
import logging
import psycopg
from fastapi import APIRouter, HTTPException
from typing import Optional

import gvm_company_report as gcr

log = logging.getLogger("scorr.gvm_report_api")

router = APIRouter(prefix="/api/gvm", tags=["gvm-report"])

DATABASE_URL = os.getenv("DATABASE_URL")


def _conn():
    return psycopg.connect(DATABASE_URL)


def _narrative(cur, symbol: str) -> dict:
    cur.execute("""
        SELECT overview, key_takeaway, result_analysis, company_name,
               mcap_rank, instrument_type, cap_category
        FROM input_raw WHERE nse_code = %s LIMIT 1
    """, (symbol,))
    r = cur.fetchone()
    if not r:
        return {}
    return {
        "overview": r[0], "key_takeaway": r[1], "result_analysis": r[2],
        "company_name_long": r[3], "mcap_rank": r[4],
        "instrument_type": r[5], "cap_category": r[6],
    }


@router.get("/search")
def gvm_search(q: str, limit: int = 12):
    limit = min(max(limit, 1), 25)
    try:
        with _conn() as conn:
            results = gcr.search_companies(conn, q, limit)
        # normalise numerics for JSON
        for x in results:
            for k in ("gvm_score", "market_cap"):
                if x.get(k) is not None:
                    x[k] = float(x[k])
        return {"query": q, "count": len(results), "results": results}
    except Exception as e:
        log.error(f"gvm_search failed: {e}")
        raise HTTPException(500, str(e))


@router.get("/company/{symbol}")
def gvm_company(symbol: str):
    try:
        with _conn() as conn:
            report = gcr.build_company_report(conn, symbol)
            if "error" in report:
                raise HTTPException(404, report["error"])
            with conn.cursor() as cur:
                narr = _narrative(cur, report["symbol"])
        report.update(narr)
        return report
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"gvm_company {symbol} failed: {e}")
        raise HTTPException(500, str(e))


@router.get("/report/health")
def gvm_report_health():
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM gvm_scores WHERE score_date=(SELECT MAX(score_date) FROM gvm_scores)")
            n = cur.fetchone()[0]
            cur.execute("SELECT COUNT(opm_rating) FROM gvm_scores WHERE score_date=(SELECT MAX(score_date) FROM gvm_scores)")
            persisted = cur.fetchone()[0]
        return {"status": "ok", "universe": n, "detail_persisted": persisted,
                "params": len(gcr.PARAMS)}
    except Exception as e:
        raise HTTPException(500, str(e))
