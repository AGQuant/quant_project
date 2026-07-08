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
"""

import os
import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scorr_auth import _is_authed

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")

# cc_task #125: Knowledge Hub (Learn tab) lives on the /news Intelligence surface.
# Its routes stay in their own module (knowledge_endpoints.py) and are mounted by
# nesting into news_router, which main.py already includes — main.py stays untouched.
from knowledge_endpoints import router as knowledge_router
router.include_router(knowledge_router)


def _conn():
    return psycopg.connect(DATABASE_URL)


# cc#322: canonical polished-news view — the SINGLE source every polished endpoint reads from,
# so the raw_news/polished_news join + timestamp logic exists exactly once. display_time is the
# canonical article timestamp (polish time first, raw only as a last resort) used for BOTH sort
# and display; raw_published_at is exposed separately for the rare original-date reference.
# Ensured idempotently at import so the view exists wherever this code runs (Railway=truth,
# GitHub=code) — CREATE OR REPLACE is a no-op when it already matches.
_V_POLISHED_ARTICLES_DDL = """
CREATE OR REPLACE VIEW v_polished_articles AS
SELECT p.id AS polished_id, p.raw_news_id AS raw_news_id, r.id AS raw_id,
       COALESCE(p.headline_clean, r.headline) AS headline,
       p.summary, p.full_summary, p.category, p.sentiment, p.impact, p.mentioned_symbols,
       COALESCE(p.source, r.source_name) AS source_name,
       r.source_type, r.symbol, r.url,
       COALESCE(p.published_time, p.polished_at, r.published_at) AS display_time,
       r.published_at AS raw_published_at, p.polished_at
FROM polished_news p JOIN raw_news r ON r.id = p.raw_news_id
"""


def _ensure_polished_view():
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(_V_POLISHED_ARTICLES_DDL)
            conn.commit()
    except Exception:
        pass   # view is created out-of-band too; a transient DB hiccup here must not block import


_ensure_polished_view()


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
    # cc#322: read the canonical v_polished_articles view; display_time (polish time) is the
    # sort + displayed timestamp, exposed under the legacy `published_at` field name.
    cur.execute("""
        SELECT polished_id, raw_id, headline,
               summary, category, sentiment, impact, mentioned_symbols,
               symbol, source_type, source_name, url, display_time AS published_at
        FROM v_polished_articles
        WHERE source_type = %s
        ORDER BY display_time DESC NULLS LAST, polished_id DESC
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
    """cc#209/#210: DB-ONLY two-tier polished news for a symbol. Source is strictly
    polished_news matched by the symbol tag (mentioned_symbols @> ARRAY[symbol]) or a
    legacy per-company row (r.symbol) — NO raw/unpolished fallback, NO web/external source.

    cc#210: each item is tiered at query time (no schema change):
      • PRIMARY   — the company name / NSE code appears in the headline (word-boundary).
                    Listed first, newest first, up to 6.
      • MENTIONED — tagged in the body but NOT the headline (sector/peer editorials).
                    Listed after all primary, newest first, up to 4, badged in the UI.
    Each row carries full_summary (the complete polished article, markdown for AI
    Editorials) for the inline expand. Empty → clean empty state, zero web calls."""
    sym = symbol.upper()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT polished_id, raw_id, headline,
                   full_summary, summary, category, sentiment, impact,
                   mentioned_symbols, symbol, source_name,
                   display_time AS published_at
            FROM v_polished_articles
            WHERE symbol = %s OR mentioned_symbols @> ARRAY[%s]::text[]
            ORDER BY display_time DESC NULLS LAST, polished_id DESC
            LIMIT 30
        """, (sym, sym))
        rows = _rows(cur)
        try:
            import news_tagger
            pats = news_tagger.headline_identity(conn, sym)
        except Exception:
            pats = []
    import news_tagger as _nt
    primary, mentioned = [], []
    for a in rows:
        a["tier"] = "primary" if _nt.is_primary(pats, a.get("headline")) else "mentioned"
        (primary if a["tier"] == "primary" else mentioned).append(a)
    articles = primary[:6] + mentioned[:4]           # already newest-first from SQL
    return {"symbol": sym, "polished": True,
            "count": len(articles), "primary_count": len(primary[:6]),
            "mentioned_count": len(mentioned[:4]), "articles": articles}


# cc#242 (POSITION_NEWS_PIPELINE_V1, id=1660): stock-tagged (source_type='company') rows are
# polish candidates ONLY when the symbol is an OPEN position (V8 paper OR SmartGain holdings) —
# non-position stock news stays raw forever, never polished. Market rows (domestic/global) are
# always candidates. Polish generation itself stays Claude-web manual; this only scopes the
# candidate query/endpoint.
_POSITION_POLISH_CLAUSE = (
    " AND (r.source_type <> 'company' OR r.symbol IN ("
    "   SELECT symbol FROM v8_paper_positions WHERE status='OPEN' AND symbol IS NOT NULL"
    "   UNION SELECT symbol FROM smartgain_holdings WHERE symbol IS NOT NULL))"
)


@router.get("/api/news/unpolished")
def news_unpolished(sample: int = 20):
    """Count + sample of raw_news rows with no matching polished_news that are eligible for
    polish. Used by the manual Claude.ai polish session to know what is pending. Quality-filtered
    (task #54) + cc#242 position gate: market news always, stock-tagged only for open positions."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM raw_news r
            WHERE NOT EXISTS (SELECT 1 FROM polished_news p WHERE p.raw_news_id = r.id)
        """ + _QUALITY_CLAUSE + _POSITION_POLISH_CLAUSE)
        pending = cur.fetchone()[0]
        cur.execute("""
            SELECT r.id AS raw_id, r.source_type, r.symbol, r.headline, r.description,
                   r.source_name, r.url, r.published_at
            FROM raw_news r
            WHERE NOT EXISTS (SELECT 1 FROM polished_news p WHERE p.raw_news_id = r.id)
        """ + _QUALITY_CLAUSE + _POSITION_POLISH_CLAUSE + """
            ORDER BY r.published_at DESC NULLS LAST, r.fetched_at DESC
            LIMIT %s
        """, (sample,))
        rows = _rows(cur)
    return {"unpolished_count": pending, "sample_size": len(rows), "sample": rows}


@router.get("/api/news/live")
def news_live(hours: int = 72, per_cat: int = 60):
    """Raw unpolished headlines from the last N hours, grouped by source_type.
    Powers the CIO Dashboard Top News tab — raw only, no polished join, fast.
    Per-category cap (ROW_NUMBER) so domestic/global aren't drowned by company.
    cc#186: per_cat max raised 200->300 — domestic runs ~259 items/48h and the
    old 60-default (and 200 cap) truncated coverage to ~14h."""
    hours = max(1, min(hours, 168))
    per_cat = max(1, min(per_cat, 300))
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
    _wc = ("(LENGTH(TRIM(COALESCE(full_summary,summary,''))) "
           "- LENGTH(REPLACE(TRIM(COALESCE(full_summary,summary,'')),' ','')) + 1)")
    # cc#322: read v_polished_articles; window + sort on display_time (polish time) so a stock
    # story with an OLD raw date but polished recently isn't excluded/mis-sorted by raw time.
    sql = f"""
        SELECT polished_id, raw_id, headline,
               summary, full_summary, category, sentiment, impact, mentioned_symbols,
               symbol, source_type, source_name, url, display_time AS published_at,
               CEIL({_wc}::numeric / 200) AS read_min
        FROM v_polished_articles
        WHERE display_time >= NOW() - (%s || ' days')::interval
    """
    params = [days]
    if cat == "ipo":
        sql += f" AND UPPER(COALESCE(category,'')) IN {IPO_SET}"
    elif cat == "global":
        sql += f" AND UPPER(COALESCE(category,'')) IN {GLOBAL_SET}"
    elif cat in ("company_updates", "company"):
        cat = "company_updates"
        sql += f" AND UPPER(COALESCE(category,'')) IN {COMPANY_SET}"
    else:  # ai_editorial — India editorial: all categories NOT in global/ipo/company
        cat = "ai_editorial"            # cc_task #70: no read-time gate (supersedes #69 >=2min)
        sql += (f" AND UPPER(COALESCE(category,'')) NOT IN {GLOBAL_SET}"
                f" AND UPPER(COALESCE(category,'')) NOT IN {IPO_SET}"
                f" AND UPPER(COALESCE(category,'')) NOT IN {COMPANY_SET}")
    sql += " ORDER BY display_time DESC NULLS LAST, polished_id DESC LIMIT %s"
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
# normalized to exactly these values; the API filters on an EXACT match (no ILIKE,
# no partial) so a tab never catches the wrong stories.
# cc_task #91: 'Company Updates' was renamed to 'Domestic' in polished_news.category.
# Map both the new 'domestic' tab id and the legacy 'company_updates' id to "Domestic".
_CANON_CAT = {
    "ai_editorial":    "AI Editorial",
    "company_updates": "Domestic",
    "domestic":        "Domestic",
    "global":          "Global",
    "ipo":             "IPO",
}


@router.get("/api/news/polished")
def news_polished(request: Request, category: str = "all", limit: int = 20, offset: int = 0):
    """Polished news for the /news redesign (cc_task #79, spec 636).
    category = all | ai_editorial | company_updates | global | ipo (strict exact match).
    Sorted polished_at DESC (newest first, all categories interleaved on 'all').
    limit (default 20, max 100) + offset paginate. Returns category_counts for tab badges.
    cc#160: endpoint-level auth (this route was reachable with no cookie, bypassing
    the /news page's login gate) — same _is_authed() check as the page, 401 not redirect."""
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized", "login_url": "/login"}, status_code=401)
    cat = (category or "all").lower()
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    where, params = "", []
    if cat in _CANON_CAT:
        where = "WHERE category = %s"
        params.append(_CANON_CAT[cat])
    else:
        cat = "all"   # unknown / 'all' -> no category filter
    # cc#322: read v_polished_articles. Was aliasing r.published_at AS published_time — actively
    # discarding the REAL polish time and showing the raw source date under a polish-time name.
    # Now published_time = display_time (the canonical polish timestamp), sorted by it too.
    sql = f"""
        SELECT polished_id AS id, raw_news_id,
               headline AS headline_clean,
               summary, full_summary, category, sentiment, impact,
               mentioned_symbols, source_name AS source,
               display_time AS published_time, polished_at
        FROM v_polished_articles
        {where}
        ORDER BY display_time DESC NULLS LAST, polished_id DESC
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


# cc#217: /api/admin/refresh_news retired — its company-news fetch is superseded by
# position_news.py (cc#207); market/global RSS runs scheduled at 06:00 IST (scheduler).
