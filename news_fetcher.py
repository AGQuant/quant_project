"""
News Fetcher — Scorr (task #38)
================================
Layer-1 (raw) automated news ingestion. Backend only — no frontend surfaces.

  fetch_market_news(conn)   — domestic (ET/Moneycontrol/LiveMint) + global
                              (Reuters/Bloomberg) RSS → raw_news.

Company-news Google fetch retired (cc#207 → cc#217): superseded by position_news.py
(open V8 + SmartGain symbols only). The Google-News politeness helpers below
(_google_news_url, _fetch_feed, BROWSER_UA, COMPANY_SLEEP*, RETRY_AFTER_CAP,
MAX_CONSECUTIVE_429) are retained — position_news.py imports them.

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
import random
import hashlib
import logging
import calendar
from datetime import datetime, timezone, timedelta

import psycopg

log = logging.getLogger("scorr.news")
DATABASE_URL = os.getenv("DATABASE_URL", "")

DESC_MAX        = 1000     # raw_news.description is VARCHAR(1000)
# cc#192: two-tier news retention — unpolished raw_news dies at 48h, polished
# (and its raw parent) lives 90 days (cc#208: 30 -> 90, all categories incl AI
# Editorial). Backlog above the alert threshold means ingest or the polish step
# broke upstream.
UNPOLISHED_MAX_HOURS       = 48
POLISHED_MAX_DAYS          = 90     # cc#208: founder ask — store polished news 90 days
UNPOLISHED_ALERT_THRESHOLD = 800
HTTP_TIMEOUT    = 12

# cc#186: Google News RSS 429-rate-limited the Railway datacenter IP. Mitigation:
# sequential (concurrency 1), 2-4s jitter, a browser User-Agent, honor Retry-After,
# abort early once flagged (10 consecutive 429s). cc#217: the company-news fetch is
# retired, but these politeness helpers live on for position_news.py.
BROWSER_UA          = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
COMPANY_SLEEP_MIN   = 2.0
COMPANY_SLEEP_MAX   = 4.0
MAX_CONSECUTIVE_429 = 10
RETRY_AFTER_CAP     = 30    # seconds — never sleep longer than this on Retry-After

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


# -- Domestic noise filters (task #102) -- from manual audit of 780-article
#    backlog. Source-agnostic headline gates (crypto, IPO grey-market premium,
#    quote-of-the-day, broker-recommendation listicles, non-market obituaries).
_DOM_CRYPTO_RE      = re.compile(r"bitcoin|crypto|ethereum|solana|xrp|nft|web3")
_DOM_GMP_RE         = re.compile(r"gmp|grey market premium|grey market")
# cc#217: 'multibagger stock' intentionally NOT blocked — founder ruling (05-Jul): multibagger
# idea-listicles are useful signal for a research platform, not junk. Other broker-tip listicle
# formats stay blocked.
_DOM_LISTICLE_RE    = re.compile(r"stocks to buy below|buy or sell:|f&o talk|concurrent gainers")
_DOM_OBIT_RE        = re.compile(r"dies at \d+|passes away|death of")
_DOM_OBIT_MARKET_RE = re.compile(r"market|stock|equity|share|nse|bse|sensex|nifty")


def _non_latin_dominant(text: str) -> bool:
    """True if >30% of the alphabetic chars are outside Latin (Unicode > U+024F)."""
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return False
    non_latin = sum(1 for c in letters if ord(c) > 0x024F)
    return non_latin / len(letters) > 0.30


def _is_quality_article(headline: str, description: str, source_name: str = None) -> bool:
    """Reject low-quality articles before they enter raw_news (task #54). cc#217: the
    company-source relaxation (cc#206) is gone with the retired company fetch — only
    domestic/global RSS reach this now."""
    h = headline or ""
    d = description or ""
    if len(d) < 80:
        return False
    if any(j in d for j in _JUNK_DESC):
        return False
    if any(j in h for j in _JUNK_HEAD):
        return False
    if h and d[:len(h)] == h:   # description is just the headline repeated
        return False
    if _non_latin_dominant(h) or _non_latin_dominant(d):
        return False
    # task #102 -- domestic source noise gates (headline-based, source-agnostic)
    h_low = h.lower()
    if _DOM_CRYPTO_RE.search(h_low):            # F1 crypto
        return False
    if _DOM_GMP_RE.search(h_low):               # F2 IPO grey-market premium
        return False
    if h_low.startswith("quote of the day"):    # F3 quote of the day
        return False
    if _DOM_LISTICLE_RE.search(h_low):          # F4 broker listicles / F&O tips
        return False
    if _DOM_OBIT_RE.search(h_low) and not _DOM_OBIT_MARKET_RE.search(d.lower()):  # F5 non-market obituary
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


def _insert_rows(conn, rows):
    """rows = list of (source_type, symbol, headline, description, url, url_hash,
    source_name, published_at). Dedups on url_hash. Low-quality rows are skipped at
    ingest (task #54).

    cc#206: returns a FUNNEL dict {parsed, quality_rejected, dup_skipped, inserted}
    so no stage is ever silent again. url_hash is globally UNIQUE, so dup_skipped
    counts BOTH intra-source repeats and cross-source-type collisions (a company URL
    already present as a domestic row) — the counter tells us which stage kills rows."""
    stats = {"parsed": len(rows), "quality_rejected": 0, "dup_skipped": 0, "inserted": 0}
    if not rows:
        return stats
    with conn.cursor() as cur:
        for r in rows:
            if not _is_quality_article(r[2], r[3], r[6]):   # r[2]=headline r[3]=desc r[6]=source_name
                stats["quality_rejected"] += 1
                log.debug(f"[news_fetcher] skipped low-quality article: {(r[2] or '')[:60]}")
                continue
            cur.execute("""
                INSERT INTO raw_news
                    (source_type, symbol, headline, description, url, url_hash, source_name, published_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (url_hash) DO NOTHING
            """, r)
            if cur.rowcount:
                stats["inserted"] += 1
            else:
                stats["dup_skipped"] += 1
    conn.commit()
    if stats["quality_rejected"]:
        log.info(f"_insert_rows: skipped {stats['quality_rejected']} low-quality article(s)")
    return stats


def _fetch_feed(url: str, agent: str = None):
    """cc#186: fetch + parse one RSS feed, FAIL-LOUD. Returns
    (entries, http_status, bozo, retry_after). feedparser swallows HTTP status
    into d.status; a 429 comes back as status=429 with empty entries, which the
    old code silently treated as 'ok, 0 new' — that hid 13 days of company-news
    outage. Callers inspect status to count 429s and honor Retry-After."""
    import feedparser  # lazy: never break module import if dep not yet installed
    try:
        d = feedparser.parse(url, agent=agent) if agent else feedparser.parse(url)
        status = getattr(d, "status", None)
        bozo = 1 if getattr(d, "bozo", 0) else 0
        headers = getattr(d, "headers", {}) or {}
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        return (d.entries or []), status, bozo, retry_after
    except Exception as e:
        log.warning(f"feed fetch failed {url}: {e}")
        return [], None, 1, None


def _parse_feed(url: str):
    """Entries-only wrapper (market-news path, unchanged behaviour)."""
    entries, _s, _b, _r = _fetch_feed(url)
    return entries


def _write_ops_log(conn, category: str, title: str, details: dict):
    """cc#186: visible per-run telemetry to ops_log (mirrors scheduler._log_alert
    shape). Used for both the every-run news_fetch record and zero-insert alerts."""
    try:
        from psycopg.types.json import Json
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), %s, %s, %s)""",
                        (category, title, Json(details)))
        conn.commit()
    except Exception as e:
        log.error(f"_write_ops_log failed ({category}/{title}): {e}")


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
        desc = _truncate(e.get("summary") or e.get("description") or "")
        rows.append((source_type, symbol, title[:2000],
                     desc, link, h, source_name[:50], _published_at(e)))
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
                n = _insert_rows(conn, rows)["inserted"]
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


def _google_news_url(company_name: str) -> str:
    from urllib.parse import quote
    q = quote(f"{company_name} NSE stock")
    return f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"


def _stock_universe(conn):
    """cc#243/#244: stock-news ingest universe = OPEN positions ONLY (V8 paper OPEN UNION
    SmartGain holdings), resolved fresh each fetch run — NOT the full 209 futures universe
    (cc#244 path-1 FINAL). Returns [(symbol, company_name)] — company_name from gvm_scores,
    else the symbol."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH pos AS (
                SELECT symbol FROM v8_paper_positions WHERE status='OPEN' AND symbol IS NOT NULL
                UNION SELECT symbol FROM smartgain_holdings WHERE symbol IS NOT NULL
            )
            SELECT p.symbol, COALESCE(g.company_name, p.symbol)
            FROM pos p
            LEFT JOIN gvm_scores g ON g.symbol = p.symbol
                AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
            ORDER BY p.symbol
        """)
        return [(r[0], r[1]) for r in cur.fetchall()]


def fetch_stock_news(conn=None, symbols=None):
    """cc#242 (POSITION_NEWS_PIPELINE_V1, session_log 1660) + cc#243 delta: per-stock Google News
    for OPEN positions only (V8 paper OPEN UNION SmartGain holdings, resolved fresh each run —
    cc#243 narrowed this from the full 209 futures universe) -> raw_news with source_type='company'
    + symbol tag. Single funnel with market news (supersedes the position_news quarantine table,
    id=402). An ALIAS-MATCH
    filter at ingest (news_tagger.headline_identity / is_primary) keeps junk out — only articles
    whose headline/description actually name the stock are stored. Polish selectivity happens at
    batch time (position symbols only), NOT ingest — the full universe stays visible raw in the
    V8 Live News tab. Politeness (sequential, browser UA, 2-4s jitter, Retry-After, 10x-429
    abort) is reused from the market path to stay under Google News' datacenter-IP limit."""
    own = conn is None
    if own:
        conn = _conn()
    try:
        ensure_schema(conn)
        import news_tagger
        universe = symbols if symbols else _stock_universe(conn)   # cc#243/#244: open positions only
        if not universe:
            return {"ok": True, "note": "no open positions", "inserted": 0}
        parsed = alias_filtered = inserted = dup_skipped = quality_rejected = 0
        http_429 = http_other = empty = 0
        consec_429 = 0
        aborted = False
        for sym, cname in universe:
            pats = news_tagger.headline_identity(conn, sym)   # per-stock alias/code identity
            entries, status, bozo, retry_after = _fetch_feed(_google_news_url(cname or sym), agent=BROWSER_UA)
            if status == 429:
                http_429 += 1; consec_429 += 1
                if retry_after:
                    try: time.sleep(min(float(retry_after), RETRY_AFTER_CAP))
                    except (TypeError, ValueError): pass
                if consec_429 >= MAX_CONSECUTIVE_429:
                    aborted = True
                    log.error(f"fetch_stock_news: IP flagged — aborting after {consec_429} consecutive 429s")
                    break
            else:
                consec_429 = 0
                if status is not None and status >= 400:
                    http_other += 1
                elif not entries:
                    empty += 1
                else:
                    kept = []
                    for e in entries:
                        parsed += 1
                        head = _clean(e.get("title") or "")
                        desc = _clean(e.get("summary") or e.get("description") or "")
                        # alias filter at INGEST: keep ONLY entries that actually name the stock
                        if pats and news_tagger.is_primary(pats, head + " . " + desc):
                            kept.append(e)
                        else:
                            alias_filtered += 1
                    st = _insert_rows(conn, _rows_from_entries(kept, "company", "Google News", symbol=sym))
                    inserted += st["inserted"]; dup_skipped += st["dup_skipped"]
                    quality_rejected += st["quality_rejected"]
            time.sleep(random.uniform(COMPANY_SLEEP_MIN, COMPANY_SLEEP_MAX))
        stats = {"symbols_count": len(universe), "parsed": parsed, "alias_filtered": alias_filtered,
                 "quality_rejected": quality_rejected, "dup_skipped": dup_skipped, "inserted": inserted,
                 "http_429": http_429, "http_other": http_other, "empty": empty, "aborted": aborted}
        _write_ops_log(conn, "news_fetch", "fetch_stock_news", stats)
        log.info(f"fetch_stock_news: {stats}")
        return {"ok": True, **stats}
    except Exception as e:
        log.error(f"fetch_stock_news: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        if own:
            conn.close()


def news_retention(conn=None):
    """cc#192: daily two-tier news retention (scheduled 01:50 IST). Makes the
    04-Jul one-time backlog cleanup permanent so unpolished news never piles up.

      (1) UNPOLISHED: delete raw_news with NO polished_news child once it is older
          than 48h (by COALESCE(published_at, fetched_at) — publish time when
          known, else ingest time).
      (2) POLISHED: delete polished_news older than 90 days (cc#208: 30 -> 90, all
          categories incl AI Editorial) AND its raw_news parent (delete the parent;
          the FK CASCADE removes the polished child).

    Writes an ops_log(category=news_retention) record every run with both counts,
    and alerts if the surviving unpolished backlog is implausibly large (>800 =>
    ingest or the polish step broke upstream)."""
    own = conn is None
    if own:
        conn = _conn()
    try:
        with conn.cursor() as cur:
            # (1) unpolished older than 48h, no polished child
            cur.execute(
                "DELETE FROM raw_news r "
                "WHERE COALESCE(r.published_at, r.fetched_at) < NOW() - INTERVAL '%s hours' "
                "  AND NOT EXISTS (SELECT 1 FROM polished_news p WHERE p.raw_news_id = r.id)"
                % int(UNPOLISHED_MAX_HOURS))
            deleted_unpolished = cur.rowcount

            # (2) polished older than 90 days -> count, then delete raw parent (CASCADE)
            cur.execute("SELECT COUNT(*) FROM polished_news WHERE polished_at < NOW() - INTERVAL '%s days'"
                        % int(POLISHED_MAX_DAYS))
            deleted_polished = cur.fetchone()[0] or 0
            cur.execute(
                "DELETE FROM raw_news r WHERE EXISTS ("
                "  SELECT 1 FROM polished_news p WHERE p.raw_news_id = r.id "
                "    AND p.polished_at < NOW() - INTERVAL '%s days')"
                % int(POLISHED_MAX_DAYS))

            # (3) surviving unpolished backlog — the health signal
            cur.execute("SELECT COUNT(*) FROM raw_news r "
                        "WHERE NOT EXISTS (SELECT 1 FROM polished_news p WHERE p.raw_news_id = r.id)")
            unpolished_remaining = cur.fetchone()[0] or 0
        conn.commit()

        stats = {"deleted_unpolished_48h": deleted_unpolished,
                 "deleted_polished_90d": deleted_polished,   # cc#208: 30 -> 90
                 "unpolished_remaining": unpolished_remaining}
        _write_ops_log(conn, "news_retention", "news_retention", stats)
        if unpolished_remaining > UNPOLISHED_ALERT_THRESHOLD:
            _write_ops_log(conn, "alert", "news_backlog_high",
                           {"message": f"unpolished raw_news backlog {unpolished_remaining} > "
                                       f"{UNPOLISHED_ALERT_THRESHOLD} after retention — upstream ingest/"
                                       f"polish likely broke",
                            "unpolished_remaining": unpolished_remaining})
        log.info(f"news_retention: -{deleted_unpolished} unpolished(48h), "
                 f"-{deleted_polished} polished(90d), {unpolished_remaining} unpolished remain")
        return {"ok": True, **stats}
    except Exception as e:
        log.error(f"news_retention: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        if own:
            conn.close()
