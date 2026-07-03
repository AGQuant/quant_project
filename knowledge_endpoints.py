"""
Knowledge Hub Endpoints — Scorr (cc_task #125)
==============================================
Read APIs over knowledge_hub_articles — 50 evergreen educational articles fed
in batches of 5 via Claude.ai. Surfaced as the "Learn" tab on the /news page.

  GET /api/knowledge/articles          — list (metadata only, no body), newest first
  GET /api/knowledge/articles/{slug}   — single article with full content_md

reading_time_min mirrors cc_task #58: chars / 1500 (min 1) — computed at read
time when the stored column is NULL, so the badge is always populated.
"""

import os
import psycopg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from scorr_auth import _is_authed

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(DATABASE_URL)


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# read-time fallback: stored value, else ceil(chars/1500) min 1 (cc_task #58 formula)
_READ_MIN = ("COALESCE(reading_time_min, "
             "GREATEST(1, CEIL(LENGTH(COALESCE(content_md,''))::numeric / 1500)))::int")


@router.get("/api/knowledge/articles")
def knowledge_articles(request: Request, category: str = "all", limit: int = 100, offset: int = 0):
    """List Knowledge Hub articles (metadata only — no content_md for a light list).
    category = all | exact category string (e.g. 'Taxation'). Newest published first.
    Returns category_counts for tab/pill badges.
    cc#160: endpoint-level auth (this route was reachable with no cookie, bypassing
    the /news page's login gate) — same _is_authed() check as the page, 401 not redirect."""
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized", "login_url": "/login"}, status_code=401)
    cat = (category or "all").strip()
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    where, params = "", []
    if cat and cat.lower() != "all":
        where = "WHERE category = %s"
        params.append(cat)
    sql = f"""
        SELECT id, slug, title, summary, category, batch_number, published_at,
               {_READ_MIN} AS reading_time_min
        FROM knowledge_hub_articles
        {where}
        ORDER BY published_at DESC NULLS LAST, id DESC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        articles = _rows(cur)
        cur.execute("""SELECT category, COUNT(*) FROM knowledge_hub_articles
                       GROUP BY category""")
        counts = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute("SELECT COUNT(*) FROM knowledge_hub_articles")
        total = cur.fetchone()[0]
    return {"category": cat, "limit": limit, "offset": offset,
            "count": len(articles), "total": total,
            "category_counts": counts, "articles": articles}


@router.get("/api/knowledge/articles/{slug}")
def knowledge_article(slug: str, request: Request):
    """Single Knowledge Hub article with full content_md (for the expand view).
    cc#160: endpoint-level auth — same _is_authed() check as the page, 401 not redirect."""
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized", "login_url": "/login"}, status_code=401)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT id, slug, title, summary, category, content_md, batch_number,
                   published_at, {_READ_MIN} AS reading_time_min
            FROM knowledge_hub_articles
            WHERE slug = %s
            LIMIT 1
        """, (slug,))
        rows = _rows(cur)
    if not rows:
        raise HTTPException(404, f"No Knowledge Hub article with slug '{slug}'")
    return rows[0]
