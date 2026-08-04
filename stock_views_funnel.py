"""
Stock Views Funnel (FUNNEL 2) — Scorr (cc#787)
==============================================
Founder decision 02-Aug-2026: broker/analyst recommendation content is NOT junk. It is the
P1 prize for Stock Views (STOCK_VIEWS_FRAMEWORK_V2 id=13456). Two funnels now run over the
same raw_news table:

  FUNNEL 1  news-polish   — editorial flow. UNCHANGED. Never sees reco content
                            (guard: news_endpoints._reco_exclude_clause).
  FUNNEL 2  Stock Views   — THIS module. Reco content + catalysts on universe stocks.

What lives here
  stock_views_feed_data(hours)      — the funnel-2 read (shared core, no auth, writes nothing)
  GET /api/news/stock_views/feed    — HTTP route over that core (auth-gated)
  fetch_universe_reco_news(slot)    — per-stock Google News over the ACTIVE FUTURES UNIVERSE,
                                      reusing the position_news politeness stack

SCOPE (funnel2_scope_UPDATE_02AUG — founder: volume must not explode):
  - source_type IN ('domestic','company') ONLY. No global rows (Reuters/Bloomberg).
  - IPO-type content excluded (ipo | listing | gmp | grey market).
  - Response capped at 200 rows newest-first, with total_count so the real volume is visible
    without pulling everything.

raw_news.is_reco is created by a Railway-console migration (MAINTENANCE_LOCK_RULE hard-blocks
ALTER TABLE on the run_sql path). Every read here degrades cleanly while the column is absent.
"""

import os
import time
import random
import logging

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import news_fetcher as nf
from scorr_auth import _is_authed

log = logging.getLogger("scorr.stock_views")
router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")

FEED_ROW_CAP   = 200      # founder cap — Claude web sees volume via total_count, not via rows
FEED_MAX_HOURS = 168
# Google query biased to recommendation content (the funnel-2 prize).
RECO_QUERY_TMPL = "{name} target price OR rating"
UNIVERSE_SLOTS  = 2       # 2 fetch slots/day, off market hours — each takes half the universe


def _conn():
    return psycopg.connect(DATABASE_URL)


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── FUNNEL 2 read ───────────────────────────────────────────────────────────────
def stock_views_feed_data(hours: int = 48):
    """SHARED CORE (no auth, writes nothing) for the Stock Views feed. Both the HTTP route
    below and the Scorr MCP tool `stock_views_feed` call THIS one helper — the funnel-2
    definition exists exactly once (same pattern as stock_views_shortlist_data, cc#737).

    Selection = reco content OR a catalyst on a stock in the ACTIVE futures universe, over the
    last N hours, restricted to Indian sources and with IPO noise stripped."""
    hours = max(1, min(int(hours or 48), FEED_MAX_HOURS))
    with _conn() as conn, conn.cursor() as cur:
        has_reco = nf.has_is_reco_column(conn)
        # Pre-migration the is_reco column does not exist, so the reco leg of the OR cannot be
        # evaluated. Degrade to the universe-catalyst leg only rather than erroring — the feed
        # returns fewer rows and says so via reco_column_present, instead of 500-ing.
        reco_expr = "COALESCE(r.is_reco, false)" if has_reco else "false"
        base = f"""
            FROM raw_news r
            WHERE r.fetched_at > NOW() - make_interval(hours => %s)
              AND r.source_type IN ('domestic','company')
              AND r.canonical_id IS NULL
              AND COALESCE(r.headline,'') !~* '\\y(ipo|listing|gmp|grey market)\\y'
              AND (
                    {reco_expr}
                 OR (r.symbol IS NOT NULL AND UPPER(r.symbol) IN (
                        SELECT UPPER(symbol) FROM futures_universe
                        WHERE is_active AND symbol IS NOT NULL))
              )
        """
        cur.execute("SELECT COUNT(*) " + base, (hours,))
        total = cur.fetchone()[0]
        cur.execute(f"""
            SELECT r.id AS raw_id, r.symbol, r.headline, r.description,
                   r.source_name, r.source_type, r.url, r.published_at, r.fetched_at,
                   r.relevance_score, {reco_expr} AS is_reco
            {base}
            ORDER BY COALESCE(r.published_at, r.fetched_at) DESC
            LIMIT %s
        """, (hours, FEED_ROW_CAP))
        rows = _rows(cur)
    return {"hours": hours, "total_count": total, "returned": len(rows),
            "capped_at": FEED_ROW_CAP, "truncated": total > len(rows),
            "reco_count": sum(1 for a in rows if a.get("is_reco")),
            "reco_column_present": has_reco, "articles": rows}


@router.get("/api/news/stock_views/feed")
def stock_views_feed(request: Request, hours: int = 48):
    """cc#787 FUNNEL 2: what Claude web scans on 'stock views' for P1/P2 candidates."""
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        return stock_views_feed_data(hours)
    except Exception as e:
        log.error(f"stock_views_feed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── FUNNEL 2 coverage fetch ─────────────────────────────────────────────────────
def _active_universe(conn):
    """Active futures universe (~209 symbols), largest-first is irrelevant here so keep it
    stable-sorted: the slot split must be deterministic across runs."""
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT UPPER(symbol) FROM futures_universe
                       WHERE is_active AND symbol IS NOT NULL ORDER BY 1""")
        return [r[0] for r in cur.fetchall()]


def fetch_universe_reco_news(slot: int = 0, conn=None, max_symbols=None):
    """cc#787 COVERAGE: per-stock Google News across the ACTIVE FUTURES UNIVERSE, biased to
    recommendation content, landing in raw_news as source_type='company' (is_reco set by the
    shared classifier inside _insert_rows).

    Politeness is the position_news stack, reused verbatim: sequential, browser UA, 2-4s
    jitter, Retry-After honoured, abort on MAX_CONSECUTIVE_429. Two slots/day, each taking
    half the universe (slot 0 / slot 1 alternate), so one run never walks all ~209 symbols.

    429 budget: on abort the remaining symbols are simply left for the next slot — the halves
    alternate, so nothing is starved permanently. max_symbols halves the slice again for a
    manual throttled run.
    """
    own = conn is None
    if own:
        conn = _conn()
    try:
        import news_tagger
        # cc#847: Position News is retired, so the three identity helpers this job actually used
        # (_name_map / _split_title / _target_is_primary) now live in news_identity.py. Same
        # functions, moved verbatim — this job's behaviour is unchanged.
        import news_identity as pn

        universe = _active_universe(conn)
        if not universe:
            stats = {"symbols_count": 0, "note": "empty futures universe"}
            nf._write_ops_log(conn, "news_fetch", "fetch_universe_reco_news", stats)
            return {"ok": True, **stats}

        slot = int(slot) % UNIVERSE_SLOTS
        symbols = [s for i, s in enumerate(universe) if i % UNIVERSE_SLOTS == slot]
        if max_symbols:
            symbols = symbols[:int(max_symbols)]

        names = pn._name_map(conn, symbols)
        try:
            index = dict(news_tagger.build_index(conn))
        except Exception as e:
            log.warning(f"build_index: {e}")
            index = {}

        agg = {"parsed": 0, "stale_rejected": 0, "quality_rejected": 0, "content_dup_skipped": 0,
               "dup_skipped": 0, "inserted": 0, "reco_flagged": 0, "reco_dropped_no_column": 0}
        http_429 = http_other = empty = alias_filtered = 0
        consec_429 = 0
        aborted = False
        scanned = 0

        for sym in symbols:
            query = RECO_QUERY_TMPL.format(name=names.get(sym) or sym)
            entries, status, bozo, retry_after = nf._fetch_feed(
                nf._google_news_url(query), agent=nf.BROWSER_UA)
            scanned += 1
            if status == 429:
                http_429 += 1
                consec_429 += 1
                if retry_after:
                    try:
                        time.sleep(min(float(retry_after), nf.RETRY_AFTER_CAP))
                    except (TypeError, ValueError):
                        pass
                if consec_429 >= nf.MAX_CONSECUTIVE_429:
                    aborted = True
                    log.error(f"fetch_universe_reco_news: IP flagged — aborting after "
                              f"{consec_429} consecutive 429s (slot {slot}, {scanned}/{len(symbols)} scanned)")
                    break
            else:
                consec_429 = 0
                if status is not None and status >= 400:
                    http_other += 1
                elif not entries:
                    empty += 1
                else:
                    rows = []
                    for e in entries:
                        link = (e.get("link") or "").strip()
                        headline, source = pn._split_title(e.get("title") or "")
                        if not link or not headline:
                            continue
                        desc = nf._truncate(e.get("summary") or e.get("description") or "")
                        # Same identity gate position_news uses: keep only entries PRIMARILY about
                        # this exact stock. An empty index stores nothing — an unverifiable
                        # attribution is the dangerous case, not the missing row.
                        if not (index and pn._target_is_primary(
                                index, sym, headline + " . " + desc)):
                            alias_filtered += 1
                            continue
                        rows.append(("company", sym, headline[:2000], desc, link,
                                     nf._md5(link), (source or "Google News")[:50],
                                     nf._published_at(e)))
                    if rows:
                        st = nf._insert_rows(conn, rows)
                        for k in agg:
                            agg[k] += st.get(k, 0)
            time.sleep(random.uniform(nf.COMPANY_SLEEP_MIN, nf.COMPANY_SLEEP_MAX))

        stats = {"slot": slot, "universe_size": len(universe), "slot_size": len(symbols),
                 "scanned": scanned, "alias_filtered": alias_filtered, "http_429": http_429,
                 "http_other": http_other, "empty": empty, "aborted": aborted, **agg}
        nf._write_ops_log(conn, "news_fetch", "fetch_universe_reco_news", stats)
        log.info(f"fetch_universe_reco_news: {stats}")
        return {"ok": True, **stats}
    except Exception as e:
        log.error(f"fetch_universe_reco_news: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass
