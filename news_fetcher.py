"""
News Fetcher — Scorr (task #38)
================================
Layer-1 (raw) automated news ingestion. Backend only — no frontend surfaces.

  fetch_market_news(conn)   — domestic (ET/Moneycontrol/LiveMint) + global
                              (Reuters/Bloomberg) RSS → raw_news.
  fetch_company_news(conn)  — Google News RSS for top-500 stocks by mcap
                              (company_name from gvm_scores) → raw_news.
  cleanup_old_news(conn)    — 30-day rolling delete (CASCADEs polished_news).

Dedup: url_hash = MD5(url), UNIQUE → INSERT ... ON CONFLICT DO NOTHING.
description is hard-capped at 1000 chars (raw_news.description is VARCHAR(1000)).
feedparser is imported lazily inside the functions so a deploy race (code live
before requirements installs) can't break module import / router mounting.

Layer-2 (polished) is a MANUAL Claude.ai session step ("polish todays news"),
NOT built here.
"""

import os
import re
import time
import hashlib
import logging
import calendar
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg

log = logging.getLogger("scorr.news")
DATABASE_URL = os.getenv("DATABASE_URL", "")

DESC_MAX        = 1000     # raw_news.description is VARCHAR(1000)
RETENTION_DAYS  = 30
COMPANY_LIMIT   = 500      # top-N stocks by mcap
PER_COMPANY_MAX = 10       # cap entries stored per company (volume control)
FETCH_WORKERS   = 5        # spec: concurrency 5 for company Google-News fan-out
HTTP_TIMEOUT    = 12

# RSS feeds — source_type drives downstream filtering (domestic | global | company)
RSS_DOMESTIC = [
    ("Economic Times Markets",   "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Economic Times Industry",  "https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms"),
    ("Moneycontrol",             "https://www.moneycontrol.com/rss/MCtopnews.xml"),
    ("LiveMint",                 "https://www.livemint.com/rss/markets"),
    ("Business Standard Markets", "https://www.business-standard.com/rss/markets-106.rss"),
]
RSS_GLOBAL = [
    ("Reuters",   "https://feeds.reuters.com/reuters/businessNews"),
    ("Bloomberg", "https://feeds.bloomberg.com/markets/news.rss"),
]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE  = re.compile(r"\s+")


def _conn():
    return psycopg.connect(DATABASE_URL)


def _md5(s: str) -> str:
    return hashlib.md5((s or "").encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    """Strip HTML tags + collapse whitespace (RSS summaries carry markup)."""
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def _truncate(text: str, n: int = DESC_MAX) -> str:
    text = _clean(text)
    return text[:n] if text else text


def _published_at(entry):
    """RSS published_parsed (UTC struct_time) → aware datetime, or None."""
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    except Exception:
        return None


# ── Quality gate (task #54) — keep junk out of raw_news at ingest time:
#    share-price tracker pages, &nbsp; stubs, sub-80-char blurbs, headline-only
#    repeats, AD HOC notices, and non-Latin (CJK/Arabic/etc) spam. ──────────────
_JUNK_DESC = ("&nbsp;", "NSE/BSE", "Share Price", "Option Chain")
_JUNK_HEAD = ("AD HOC", "Share Price", "Option Chain")

# -- Bloomberg RSS relevance gate (task #101) -- Bloomberg's feed dumps TV-show
#    clips, sports, lifestyle and food-brand pieces into raw_news. For Bloomberg
#    sources only: reject known show/segment headlines, and require at least one
#    market/financial keyword in headline+description. --------------------------
_BLOOMBERG_NOISE_PATTERNS = (
    "closing bell", "bloomberg money", "bloomberg surveillance",
    "masters in business", "bloomberg quicktake", "odd lots",
    "bloomberg businessweek", "bloomberg law", "bloomberg markets",
    "bloomberg technology", "bloomberg open interest",
)
_BLOOMBERG_REQUIRED_KEYWORDS = (
    "market", "stock", "equity", "index", "indices", "rate", "rates", "yield",
    "fed", "federal reserve", "inflation", "gdp", "recession", "economy",
    "economic", "india", "nifty", "sensex", "rbi", "rupee", "inr", "fpi", "fii",
    "emerging market", "oil", "crude", "gold", "silver", "commodity", "bitcoin",
    "fund", "etf", "ipo", "merger", "acquisition", "earnings", "profit",
    "revenue", "bank", "hedge", "private equity", "venture", "valuation", "ai",
    "chip", "semiconductor", "tech", "technology", "tariff", "trade", "sanction",
    "supply chain", "wall street",
)


def _non_latin_dominant(text: str) -> bool:
    """True if >30% of the alphabetic chars are outside Latin (Unicode > U+024F)."""
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return False
    non_latin = sum(1 for c in letters if ord(c) > 0x024F)
    return non_latin / len(letters) > 0.30


def _is_quality_article(headline: str, description: str, source_name: str = None) -> bool:
    """Reject low-quality articles before they enter raw_news (task #54)."""
    h = headline or ""
    d = description or ""
    if len(d) < 80:
        return False
    if any(j in d for j in _JUNK_DESC):
        return False
    if any(j in h for j in _JUNK_HEAD):
        return False
    if h and d[:len(h)] == h:          # description is just the headline repeated
        return False
    if _non_latin_dominant(h) or _non_latin_dominant(d):
        return False
    # task #101 -- Bloomberg-only relevance gate
    if source_name and "bloomberg" in source_name.lower():
        h_low = h.lower()
        if any(p in h_low for p in _BLOOMBERG_NOISE_PATTERNS):
            return False
        combined = (h + d).lower()
        if not any(kw in combined for kw in _BLOOMBERG_REQUIRED_KEYWORDS):
            return False
    return True


def ensure_schema(conn):
    """Defensive CREATE IF NOT EXISTS — mirrors the migration so the module is
    self-sufficient even on a fresh DB."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS raw_news (
                id BIGSERIAL PRIMARY KEY,
                source_type VARCHAR(20) NOT NULL,
                symbol VARCHAR(20),
                headline TEXT NOT NULL,
                description VARCHAR(1000),
                url TEXT,
                url_hash VARCHAR(64) UNIQUE,
                source_name VARCHAR(50),
                published_at TIMESTAMPTZ,
                fetched_at TIMESTAMPTZ DEFAULT NOW()
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_raw_news_symbol_pub  ON raw_news(symbol, published_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_raw_news_srctype_pub ON raw_news(source_type, published_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_raw_news_fetched     ON raw_news(fetched_at DESC)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS polished_news (
                id BIGSERIAL PRIMARY KEY,
                raw_news_id BIGINT REFERENCES raw_news(id) ON DELETE CASCADE,
                headline_clean TEXT,
                summary TEXT,
                category VARCHAR(20),
                sentiment VARCHAR(10),
                impact VARCHAR(10),
                mentioned_symbols TEXT[],
                polished_at TIMESTAMPTZ DEFAULT NOW()
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_polished_sentiment ON polished_news(sentiment)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_polished_category  ON polished_news(category)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_polished_rawid     ON polished_news(raw_news_id)")
    conn.commit()


def _insert_rows(conn, rows) -> int:
    """rows = list of (source_type, symbol, headline, description, url, url_hash,
    source_name, published_at). Dedups on url_hash. Returns inserted count.
    Low-quality rows are skipped at ingest (task #54)."""
    if not rows:
        return 0
    inserted = 0
    skipped = 0
    with conn.cursor() as cur:
        for r in rows:
            if not _is_quality_article(r[2], r[3], r[6]):   # r[2]=headline, r[3]=desc, r[6]=source_name
                skipped += 1
                log.debug(f"[news_fetcher] skipped low-quality article: {(r[2] or '')[:60]}")
                continue
            cur.execute("""
                INSERT INTO raw_news
                    (source_type, symbol, headline, description, url, url_hash, source_name, published_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (url_hash) DO NOTHING
            """, r)
            inserted += cur.rowcount
    conn.commit()
    if skipped:
        log.info(f"_insert_rows: skipped {skipped} low-quality article(s)")
    return inserted


def _parse_feed(url: str):
    """Fetch + parse one RSS feed. Returns feedparser entries ([] on failure)."""
    import feedparser  # lazy: never break module import if dep not yet installed
    try:
        d = feedparser.parse(url)
        return d.entries or []
    except Exception as e:
        log.warning(f"feed parse failed {url}: {e}")
        return []


def _rows_from_entries(entries, source_type, source_name, symbol=None, cap=None):
    rows, seen = [], set()
    for e in (entries[:cap] if cap else entries):
        link = (e.get("link") or "").strip()
        title = _clean(e.get("title") or "")
        if not link or not title:
            continue
        h = _md5(link)
        if h in seen:
            continue
        seen.add(h)
        rows.append((source_type, symbol, title[:2000],
                     _truncate(e.get("summary") or e.get("description") or ""),
                     link, h, source_name[:50], _published_at(e)))
    return rows


def fetch_market_news(conn=None):
    """Domestic + global market RSS → raw_news (deduped)."""
    own = conn is None
    if own:
        conn = _conn()
    try:
        ensure_schema(conn)
        total, by_src = 0, {}
        for source_type, feeds in (("domestic", RSS_DOMESTIC), ("global", RSS_GLOBAL)):
            for name, url in feeds:
                rows = _rows_from_entries(_parse_feed(url), source_type, name)
                n = _insert_rows(conn, rows)
                total += n
                by_src[name] = n
        log.info(f"fetch_market_news: {total} new | {by_src}")
        return {"ok": True, "inserted": total, "by_source": by_src}
    except Exception as e:
        log.error(f"fetch_market_news: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        if own:
            conn.close()


def _top_companies(conn, limit: int):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, company_name FROM gvm_scores
            WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)
              AND company_name IS NOT NULL AND market_cap IS NOT NULL
            ORDER BY market_cap DESC NULLS LAST
            LIMIT %s
        """, (limit,))
        return cur.fetchall()


def _google_news_url(company_name: str) -> str:
    from urllib.parse import quote
    q = quote(f"{company_name} NSE stock")
    return f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"


def fetch_company_news(conn=None, symbols=None, limit: int = COMPANY_LIMIT):
    """Google News RSS per company (top-N by mcap, or an explicit symbol list).
    Network fan-out is concurrent (FETCH_WORKERS); all DB writes stay on this
    thread (psycopg connections are not thread-safe)."""
    own = conn is None
    if own:
        conn = _conn()
    try:
        ensure_schema(conn)
        if symbols:
            with conn.cursor() as cur:
                cur.execute("""SELECT symbol, company_name FROM gvm_scores
                               WHERE score_date=(SELECT MAX(score_date) FROM gvm_scores)
                                 AND symbol = ANY(%s)""", (list(symbols),))
                companies = cur.fetchall()
        else:
            companies = _top_companies(conn, limit)

        if not companies:
            return {"ok": True, "inserted": 0, "companies": 0, "note": "no companies"}

        def _work(item):
            sym, name = item
            entries = _parse_feed(_google_news_url(name or sym))
            return _rows_from_entries(entries, "company", "Google News",
                                      symbol=sym, cap=PER_COMPANY_MAX)

        total, done = 0, 0
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            futures = {ex.submit(_work, c): c for c in companies}
            for fut in as_completed(futures):
                try:
                    total += _insert_rows(conn, fut.result())
                except Exception as e:
                    log.warning(f"company news {futures[fut][0]}: {e}")
                done += 1
        log.info(f"fetch_company_news: {total} new across {done} companies")
        return {"ok": True, "inserted": total, "companies": done}
    except Exception as e:
        log.error(f"fetch_company_news: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        if own:
            conn.close()


def cleanup_old_news(conn=None):
    """30-day rolling delete on raw_news (CASCADE removes matching polished_news)."""
    own = conn is None
    if own:
        conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM raw_news WHERE fetched_at < NOW() - INTERVAL '%s days'"
                        % int(RETENTION_DAYS))
            deleted = cur.rowcount
        conn.commit()
        log.info(f"cleanup_old_news: deleted {deleted} rows (>{RETENTION_DAYS}d)")
        return {"ok": True, "deleted": deleted}
    except Exception as e:
        log.error(f"cleanup_old_news: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        if own:
            conn.close()
