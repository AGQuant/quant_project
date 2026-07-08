"""
position_news_endpoints.py — canonical polished company-news lookup for OPEN positions
(id=1660 / cc#243). Grouped by symbol, newest first within a group; plain and fast.
Surfaced by the Position News tab (relocated to v8_dashboard.html in cc#294; the retired
cc#207 "quarantine tab" framing no longer applies). Query/logic unchanged.

  GET /api/news/position   -> { symbols, total, groups:[{symbol,origin,count,items:[...]}] }
"""
import os
import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scorr_auth import _is_authed

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(DATABASE_URL)


@router.get("/api/news/position")
def position_news_feed(request: Request):
    """cc#243 (delta on cc#242, POSITION_NEWS_PIPELINE_V1 id=1660): CANONICAL lookup — polished
    COMPANY news for OPEN positions, last 7 days. Match on rn.symbol (exact, ingest-set), NOT
    mentioned_symbols. Excludes AI Editorials and RSS market news (source_type='company' only).
    Card = headline_clean + full_summary body + meta(source, time, symbol). DB retention stays
    90d; this is a 7-day DISPLAY cap. Grouped by symbol, newest first. Founder-only."""
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized", "login_url": "/login"}, status_code=401)
    groups = {}
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH pos AS (
                    SELECT symbol FROM v8_paper_positions WHERE status='OPEN' AND symbol IS NOT NULL
                    UNION SELECT symbol FROM smartgain_holdings WHERE symbol IS NOT NULL
                )
                SELECT pn.id, pn.headline_clean, pn.full_summary,
                       pn.category, pn.sentiment, pn.impact, pn.source,
                       COALESCE(pn.published_time, pn.polished_at) AS published_at,
                       rn.symbol
                FROM polished_news pn
                JOIN raw_news rn ON rn.id = pn.raw_news_id
                JOIN pos ON pos.symbol = rn.symbol
                WHERE rn.source_type = 'company'
                  AND COALESCE(pn.published_time, pn.polished_at) >= NOW() - INTERVAL '7 days'
                ORDER BY rn.symbol ASC,
                         COALESCE(pn.published_time, pn.polished_at) DESC NULLS LAST, pn.id DESC
            """)
            for pid, headline, body, category, sentiment, impact, source, pub, sym in cur.fetchall():
                g = groups.get(sym)
                if g is None:
                    g = {"symbol": sym, "count": 0, "items": []}
                    groups[sym] = g
                g["count"] += 1
                g["items"].append({
                    "headline": headline, "body": body, "category": category,
                    "sentiment": sentiment, "impact": impact, "source": source,
                    "symbol": sym,
                    "published_at": pub.isoformat() if pub else None,
                })
    except Exception as e:
        return JSONResponse({"error": str(e), "groups": [], "symbols": 0, "total": 0}, status_code=200)
    # newest-active symbols first: biggest groups on top, then alphabetical
    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["symbol"]))
    total = sum(g["count"] for g in ordered)
    return {"symbols": len(ordered), "total": total, "groups": ordered}
