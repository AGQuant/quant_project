"""
Native Trade Check endpoint — POST /api/check

Zero-token, pure Railway DB. Powers the dedicated CHECK page (/check).
Independent of /api/scorr/chat (no Claude, no ADMIN_TOKEN, no MCP).

Body: { "symbol": "RELIANCE", "side": "LONG"|"SHORT",
        "gate1": true|false|null, "gate2": true|false|null }

GET /api/check/suggest?q=BHARTI  — symbol autocomplete (top 8 matches)
GET /api/check/health             — health check
"""

from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional
import os
import psycopg
from native_trade_check import compute_trade_check

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")


class CheckRequest(BaseModel):
    symbol: str
    side: Optional[str] = "LONG"
    gate1: Optional[bool] = None
    gate2: Optional[bool] = None


@router.post("/api/check")
def api_check(req: CheckRequest):
    side = (req.side or "LONG").upper()
    if side not in ("LONG", "SHORT"):
        side = "LONG"
    return compute_trade_check(req.symbol, side, req.gate1, req.gate2)


@router.get("/api/check/suggest")
def api_check_suggest(q: str = Query(default="", min_length=1)):
    """Return top 8 symbol + company matches for autocomplete."""
    if not q or len(q.strip()) < 1:
        return []
    term = q.strip().upper()
    try:
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, company_name, segment,
                       ROUND(gvm_score::numeric, 2) AS gvm
                FROM gvm_scores
                WHERE UPPER(symbol) LIKE %s OR UPPER(company_name) LIKE %s
                ORDER BY
                    CASE WHEN UPPER(symbol) = %s THEN 0
                         WHEN UPPER(symbol) LIKE %s THEN 1
                         ELSE 2 END,
                    market_cap DESC NULLS LAST
                LIMIT 8
            """, (f"{term}%", f"%{term}%", term, f"{term}%"))
            rows = cur.fetchall()
            return [
                {"symbol": r[0], "company": r[1], "segment": r[2], "gvm": float(r[3]) if r[3] else None}
                for r in rows
            ]
    except Exception as e:
        return []


@router.get("/api/check/health")
def api_check_health():
    return {"status": "ok", "engine": "native_v3.3", "cost": "$0",
            "needs_admin_token": False, "needs_claude": False}
