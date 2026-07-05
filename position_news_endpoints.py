"""
position_news_endpoints.py — cc#207
Read API for the Position News quarantine tab. Grouped by symbol, newest first
within a group. This is a reading room, not a product surface — plain and fast.

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
    """Position News grouped by symbol (V8 open + SmartGain holdings). Founder-only."""
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized", "login_url": "/login"}, status_code=401)
    groups = {}
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, origin, headline, source_name, url, published_at
                FROM position_news
                ORDER BY symbol ASC, published_at DESC NULLS LAST, id DESC
            """)
            for sym, origin, headline, source, url, pub in cur.fetchall():
                g = groups.get(sym)
                if g is None:
                    g = {"symbol": sym, "origin": origin, "count": 0, "items": []}
                    groups[sym] = g
                g["count"] += 1
                g["items"].append({
                    "headline": headline, "source": source, "url": url,
                    "published_at": pub.isoformat() if pub else None,
                })
    except Exception as e:
        return JSONResponse({"error": str(e), "groups": [], "symbols": 0, "total": 0}, status_code=200)
    # newest-active symbols first: biggest groups on top, then alphabetical
    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["symbol"]))
    total = sum(g["count"] for g in ordered)
    return {"symbols": len(ordered), "total": total, "groups": ordered}
