"""
News Endpoints — Scorr (task #38)
=================================
Read APIs over raw_news / polished_news + an admin refresh trigger.
Backend only; no HTML page routes (frontend surfaces are a separate task).

  GET  /api/news/market           — latest 20 polished domestic + 10 global
  GET  /api/news/company/{symbol} — latest 10 polished for a symbol (raw fallback)
  GET  /api/news/unpolished       — count + sample of raw_news awaiting polish
  GET  /api/news/live             — raw headlines grouped by source_type (CIO tab, task #40)
  GET  /api/news/top              — polished news by category for /news page (task #47)
  GET  /api/news/polished         — polished news, strict canonical-category filter (cc_task #79)
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


def _polished_by_type(cur, source_type: str, limit: int):
    cur.execute("""
        SELECT p.id AS polished_id, r.id AS raw_id,
               COALESCE(p.headline_clean, r.headline) AS headline,
               p.summary, p.category, p.sentiment, p.impact, p.mentioned_symbols,
               r.symbol, r.source_type, r.source_name, r.url, r.published_at
        FROM polished_news p
        JOIN raw_news r ON r.id = p.raw_news_id
        WHERE r.source_type = %s
        ORDER BY r.published_at DESC NULLS LAST, r.fetched_at DESC
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
                   r.symbol, r.source_name, r.url, r.published_at
            FROM polished_news p
            JOIN raw_news r ON r.id = p.raw_news_id
            WHERE r.symbol = %s
            ORDER BY r.published_at DESC NULLS LAST, r.fetched_at DESC
            LIMIT 10
        """, (sym,))
        polished = _rows(cur)
        if polished:
            return {"symbol": sym, "polished": True, "count": len(polished), "articles": polished}
        # fallback: raw articles not yet polished
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
    """Count + sample of raw_news rows with no matching polished_news.
    Used by the manual Claude.ai polish session to know what is pending.
    Quality-filtered (task #54) so the count reflects real articles, not junk."""
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
    """Raw unpolished headlines from the last N hours, grouped by source_type.
    Powers the CIO Dashboard Top News tab — raw only, no polished join, fast.
    Per-category cap (ROW_NUMBER) so domestic/global aren't drowned by company."""
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
def news_top(days: int = 3, category: str = "ai_editorial", limit: int = 50):
    """Polished news for /news page V2 tabs (cc_task #69, #70). Filters by polished
    category buckets (UPPER() so legacy lowercase global/ipo/company/domestic map too):
      ai_editorial = India editorial — any category NOT in global/ipo/company buckets
                     (cc_task #70: no read-time gate; legacy 'domestic' included)
      company_updates = COMPANY_UPDATES (+ legacy 'company')
      global          = GLOBAL / GLOBAL_MACRO / GLOBAL_TECH (+ legacy 'global')
      ipo             = IPO / STARTUP (+ legacy 'ipo')
    read_min = ceil(words(full_summary or summary)/200)."""
    days = max(1, min(days, 30))
    limit = max(1, min(limit, 200))
    cat = (category or "ai_editorial").lower()
    GLOBAL_SET  = "('GLOBAL','GLOBAL_MACRO','GLOBAL_TECH')"
    IPO_SET     = "('IPO','STARTUP')"
    COMPANY_SET = "('COMPANY_UPDATES','COMPANY')"
    # word count of full_summary (fallback summary) = spaces + 1 -> read-time badge
    _wc = ("(LENGTH(TRIM(COALESCE(p.full_summary,p.summary,''))) "
           "- LENGTH(REPLACE(TRIM(COALESCE(p.full_summary,p.summary,'')),' ','')) + 1)")
    sql = f"""
        SELECT p.id AS polished_id, r.id AS raw_id,
               COALESCE(p.headline_clean, r.headline) AS headline,
               p.summary, p.full_summary, p.category, p.sentiment, p.impact, p.mentioned_symbols,
               r.symbol, r.source_type, r.source_name, r.url, r.published_at,
               CEIL({_wc}::numeric / 200) AS read_min
        FROM polished_news p
        JOIN raw_news r ON r.id = p.raw_news_id
        WHERE r.published_at >= NOW() - (%s || ' days')::interval
    """
    params = [days]
    if cat == "ipo":
        sql += f" AND UPPER(COALESCE(p.category,'')) IN {IPO_SET}"
    elif cat == "global":
        sql += f" AND UPPER(COALESCE(p.category,'')) IN {GLOBAL_SET}"
    elif cat in ("company_updates", "company"):
        cat = "company_updates"
        sql += f" AND UPPER(COALESCE(p.category,'')) IN {COMPANY_SET}"
    else:  # ai_editorial — India editorial: all categories NOT in global/ipo/company
        cat = "ai_editorial"            # cc_task #70: no read-time gate (supersedes #69 >=2min)
        sql += (f" AND UPPER(COALESCE(p.category,'')) NOT IN {GLOBAL_SET}"
                f" AND UPPER(COALESCE(p.category,'')) NOT IN {IPO_SET}"
                f" AND UPPER(COALESCE(p.category,'')) NOT IN {COMPANY_SET}")
    sql += " ORDER BY r.published_at DESC NULLS LAST LIMIT %s"
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


# Canonical category map (cc_task #79, spec id=636). polished_news.category is now
# normalized to exactly these 4 values; the API filters on an EXACT match (no ILIKE,
# no partial) so a tab never catches the wrong stories.
_CANON_CAT = {
    "ai_editorial":    "AI Editorial",
    "company_updates": "Company Updates",
    "global":          "Global",
    "ipo":             "IPO",
}


@router.get("/api/news/polished")
def news_polished(category: str = "all", limit: int = 20, offset: int = 0):
    """Polished news for the /news redesign (cc_task #79, spec 636).
    category = all | ai_editorial | company_updates | global | ipo (strict exact match).
    Sorted polished_at DESC (newest first, all categories interleaved on 'all').
    limit (default 20, max 100) + offset paginate. Returns category_counts for tab badges."""
    cat = (category or "all").lower()
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    where, params = "", []
    if cat in _CANON_CAT:
        where = "WHERE p.category = %s"
        params.append(_CANON_CAT[cat])
    else:
        cat = "all"   # unknown / 'all' -> no category filter
    sql = f"""
        SELECT p.id, p.raw_news_id,
               COALESCE(p.headline_clean, r.headline) AS headline_clean,
               p.summary, p.full_summary, p.category, p.sentiment, p.impact,
               p.mentioned_symbols, r.source_name AS source,
               r.published_at AS published_time, p.polished_at
        FROM polished_news p
        JOIN raw_news r ON r.id = p.raw_news_id
        {where}
        ORDER BY p.polished_at DESC NULLS LAST
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        articles = _rows(cur)
        cur.execute("SELECT category, COUNT(*) FROM polished_news GROUP BY category")
        counts = {row[0]: row[1] for row in cur.fetchall()}
    return {"category": cat, "limit": limit, "offset": offset,
            "count": len(articles), "category_counts": counts, "articles": articles}


def _refresh_job():
    """Runs both fetchers on its own connection (background thread)."""
    import news_fetcher
    with _conn() as conn:
        news_fetcher.fetch_market_news(conn)
        news_fetcher.fetch_company_news(conn)


@router.post("/api/admin/refresh_news")
def refresh_news(background_tasks: BackgroundTasks):
    """Trigger market + company news fetch in the background."""
    background_tasks.add_task(_refresh_job)
    return {"status": "started", "jobs": ["fetch_market_news", "fetch_company_news"]}
