"""
News Endpoints — Scorr (task #38)
=================================
Read APIs over raw_news / polished_news + an admin refresh trigger.
Backend only; no HTML page routes (frontend surfaces are a separate task).

  GET  /api/news/market           — latest 20 polished domestic + 10 global
  GET  /api/news/company/{symbol} — latest 10 polished for a symbol (raw fallback)
  GET  /api/news/unpolished       — count + sample of raw_news awaiting polish
  GET  /api/news/live             — raw headlines grouped by source_type (CIO tab, task #40)
  GET  /api/news/top              — polished news by category for /news page (task #47, #66)
  POST /api/admin/refresh_news    — background fetch (market + company), returns started
"""

import os
import psycopg
from fastapi import APIRouter, BackgroundTasks

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(DATABASE_URL)


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# Quality gate (task #54): exclude junk from the unpolished backlog count/sample so
# the manual polish session sees only real articles (mirrors news_fetcher ingest filter).
_QUALITY_CLAUSE = (
    " AND LENGTH(COALESCE(r.description,'')) >= 80"
    " AND r.description NOT LIKE '% &nbsp;%'"
    " AND r.description NOT LIKE '%NSE/BSE%'"
    " AND r.description NOT LIKE '%Share Price%'"
    " AND r.description NOT LIKE '%Option Chain%'"
    " AND r.headline NOT LIKE '%AD HOC%'"
    " AND r.headline NOT LIKE '%Share Price%'"
    " AND r.headline NOT LIKE '%Option Chain%'"
)

# Live feed quality filter — strips low-value headlines from V8 live news tab.
_LIVE_QUALITY_FILTER = """
    AND LENGTH(COALESCE(headline, '')) > 80
    AND headline NOT ILIKE '%share price%'
    AND headline NOT ILIKE '%option chain%'
    AND headline NOT ILIKE '% GMP%'
    AND headline NOT ILIKE '%upper circuit%'
    AND headline NOT ILIKE '%lower circuit%'
    AND headline NOT ILIKE '%stocks to buy%'
    AND headline NOT ILIKE '%stocks to sell%'
    AND headline NOT ILIKE '%stocks to watch%'
    AND headline NOT ILIKE '%buy or sell%'
    AND headline NOT ILIKE '%breakout stocks%'
    AND headline NOT ILIKE '%multibagger%'
    AND headline NOT ILIKE '%penny stock%'
    AND headline NOT ILIKE '%IPO allotment%'
    AND headline NOT ILIKE '%IPO day 1%'
    AND headline NOT ILIKE '%IPO day 2%'
    AND headline NOT ILIKE '%IPO day 3%'
    AND headline NOT ILIKE '%subscribed%'
    AND headline NOT ILIKE '%grey market%'
    AND headline NOT ILIKE '%Nifty 50 prediction%'
    AND headline NOT ILIKE '%Sensex prediction%'
    AND headline NOT ILIKE '%10 things%'
    AND headline NOT ILIKE '%things to know%'
    AND headline NOT ILIKE '%The Asia Trade%'
    AND headline NOT ILIKE '%Insight with%'
    AND LENGTH(COALESCE(description, '')) >= 100
"""


def _polished_by_type(cur, source_type: str, limit: int):
    cur.execute("""
        SELECT p.id AS polished_id, r.id AS raw_id,
               COALESCE(p.headline_clean, r.headline) AS headline,
               p.summary, p.category, p.sentiment, p.impact, p.mentioned_symbols,
               r.symbol, r.source_type, r.source_name, r.url, r.published_at,
               p.polished_at
        FROM polished_news p
        JOIN raw_news r ON r.id = p.raw_news_id
        WHERE r.source_type = %s
        ORDER BY p.polished_at DESC NULLS LAST
        LIMIT %s
    """, (source_type, limit))
    return _rows(cur)


@router.get("/api/news/market")
def news_market():
    """Latest polished market news — 20 domestic + 10 global."""
    with _conn() as conn, conn.cursor() as cur:
        domestic = _polished_by_type(cur, "domestic", 20)
        global_  = _polished_by_type(cur, "global", 10)
    return {"domestic": domestic, "global": global_,
            "count": len(domestic) + len(global_)}


@router.get("/api/news/company/{symbol}")
def news_company(symbol: str):
    """Latest 10 polished articles for a symbol; falls back to raw if none polished yet."""
    sym = symbol.upper()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT p.id AS polished_id, r.id AS raw_id,
                   COALESCE(p.headline_clean, r.headline) AS headline,
                   p.summary, p.category, p.sentiment, p.impact, p.mentioned_symbols,
                   r.symbol, r.source_name, r.url, r.published_at, p.polished_at
            FROM polished_news p
            JOIN raw_news r ON r.id = p.raw_news_id
            WHERE r.symbol = %s
            ORDER BY p.polished_at DESC NULLS LAST
            LIMIT 10
        """, (sym,))
        polished = _rows(cur)
        if polished:
            return {"symbol": sym, "polished": True, "count": len(polished), "articles": polished}
        cur.execute("""
            SELECT id AS raw_id, headline, description, symbol, source_name, url, published_at
            FROM raw_news
            WHERE symbol = %s
            ORDER BY published_at DESC NULLS LAST, fetched_at DESC
            LIMIT 10
        """, (sym,))
        raw = _rows(cur)
    return {"symbol": sym, "polished": False, "count": len(raw), "articles": raw}


@router.get("/api/news/unpolished")
def news_unpolished(sample: int = 20):
    """Count + sample of raw_news rows with no matching polished_news."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM raw_news r
            WHERE NOT EXISTS (SELECT 1 FROM polished_news p WHERE p.raw_news_id = r.id)
        """ + _QUALITY_CLAUSE)
        pending = cur.fetchone()[0]
        cur.execute("""
            SELECT r.id AS raw_id, r.source_type, r.symbol, r.headline, r.description,
                   r.source_name, r.url, r.published_at
            FROM raw_news r
            WHERE NOT EXISTS (SELECT 1 FROM polished_news p WHERE p.raw_news_id = r.id)
        """ + _QUALITY_CLAUSE + """
            ORDER BY r.published_at DESC NULLS LAST, r.fetched_at DESC
            LIMIT %s
        """, (sample,))
        rows = _rows(cur)
    return {"unpolished_count": pending, "sample_size": len(rows), "sample": rows}


@router.get("/api/news/live")
def news_live(hours: int = 72, per_cat: int = 60):
    """Filtered headlines from the last N hours, grouped by source_type.
    Powers the V8 live news tab and CIO Top News. Quality-filtered to remove
    daily tip articles, GMP noise, circuit movers, and TV show blurbs."""
    hours = max(1, min(hours, 168))
    per_cat = max(1, min(per_cat, 200))
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id, headline, description, source_name, source_type, symbol, published_at
            FROM (
                SELECT id, headline, description, source_name, source_type, symbol, published_at,
                       ROW_NUMBER() OVER (PARTITION BY source_type ORDER BY published_at DESC NULLS LAST) AS rn
                FROM raw_news
                WHERE published_at >= NOW() - (%s || ' hours')::interval
            """ + _LIVE_QUALITY_FILTER + """
            ) t
            WHERE rn <= %s
            ORDER BY source_type, published_at DESC NULLS LAST
        """, (hours, per_cat))
        rows = _rows(cur)
    groups = {"domestic": [], "global": [], "company": []}
    for r in rows:
        groups.setdefault(r["source_type"], []).append(r)
    return {"hours": hours, "count": len(rows),
            "domestic": groups.get("domestic", []),
            "global": groups.get("global", []),
            "company": groups.get("company", [])}


@router.get("/api/news/top")
def news_top(days: int = 3, category: str = "india", limit: int = 50):
    """Polished news for the /news page, sorted by polished_at DESC so freshly
    polished articles always appear at the top regardless of original publish date.

    Categories (task #66 — Domestic + Company merged into India):
      india    = domestic + company, excluding ipo (DEFAULT)
      global   = source_type global
      ipo      = polished category = 'ipo'
      domestic = alias for backward compatibility
      company  = alias for backward compatibility
    """
    days = max(1, min(days, 30))
    limit = max(1, min(limit, 200))
    cat = (category or "india").lower()
    sql = """
        SELECT p.id AS polished_id, r.id AS raw_id,
               COALESCE(p.headline_clean, r.headline) AS headline,
               p.summary, p.full_summary, p.category, p.sentiment, p.impact,
               p.mentioned_symbols, p.polished_at,
               r.symbol, r.source_type, r.source_name, r.url, r.published_at
        FROM polished_news p
        JOIN raw_news r ON r.id = p.raw_news_id
        WHERE p.polished_at >= NOW() - (%s || ' days')::interval
    """
    params = [days]
    if cat == "ipo":
        sql += " AND p.category = 'ipo'"
    elif cat == "global":
        sql += " AND r.source_type = 'global'"
    elif cat == "company":
        sql += " AND r.source_type = 'company'"
    elif cat == "domestic":
        sql += " AND r.source_type = 'domestic' AND (p.category IS NULL OR p.category <> 'ipo')"
    else:  # india — domestic + company merged (task #66)
        cat = "india"
        sql += " AND r.source_type IN ('domestic','company') AND (p.category IS NULL OR p.category <> 'ipo')"
    # Sort by polished_at DESC — freshly polished articles always surface first
    # regardless of original published_at date. Fixes the issue where articles
    # polished today (Jun 23) with pub_date Jun 22 were buried under yesterday's feed.
    sql += " ORDER BY p.polished_at DESC NULLS LAST LIMIT %s"
    params.append(limit)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        articles = _rows(cur)
        cur.execute("SELECT MAX(polished_at) FROM polished_news")
        last = cur.fetchone()[0]
        cur.execute("""SELECT COUNT(*) FROM polished_news
                       WHERE polished_at::date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date""")
        today_count = cur.fetchone()[0]
    return {"category": cat, "days": days, "count": len(articles),
            "last_polished": last.isoformat() if last else None,
            "today_count": today_count, "articles": articles}


def _refresh_job():
    import news_fetcher
    with _conn() as conn:
        news_fetcher.fetch_market_news(conn)
        news_fetcher.fetch_company_news(conn)


@router.post("/api/admin/refresh_news")
def refresh_news(background_tasks: BackgroundTasks):
    """Trigger market + company news fetch in the background."""
    background_tasks.add_task(_refresh_job)
    return {"status": "started", "jobs": ["fetch_market_news", "fetch_company_news"]}
