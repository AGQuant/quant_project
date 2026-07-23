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
    """cc#611: canonical Position News tab — DEDICATED per-open-position Google News from the
    position_news table (revived; the whole-universe polished_news funnel starved open symbols to
    ~1 item). Match on pn.symbol (exact, ingest-set). Each item = clean headline + a polished
    `summary` (NULL until the CC polish batch runs -> the card shows the raw headline with an
    `polished:false` flag, never blank) + meta(source, time, symbol). 7-day DISPLAY cap; grouped by
    symbol, newest first. `open_positions` = the LIVE open book (V8 OPEN ∪ SmartGain), reported
    SEPARATELY from `total` items and `symbols` (= symbols that currently carry news). Founder-only."""
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized", "login_url": "/login"}, status_code=401)
    groups = {}
    open_positions = 0
    try:
        with _conn() as conn, conn.cursor() as cur:
            # cc#611: idempotent schema guard so a fresh deploy that hasn't run a fetch yet still has
            # the summary/polished_at columns this query reads (matches the _ensure_cols pattern).
            try:
                import position_news
                position_news.ensure_schema(conn)
            except Exception:
                conn.rollback()
            # PART B: live open-position count (V8 OPEN ∪ SmartGain) — independent of whether a symbol
            # has any news yet. This is the header number; the old code reported len(groups).
            cur.execute("""SELECT COUNT(*) FROM (
                SELECT symbol FROM v8_paper_positions WHERE status='OPEN' AND symbol IS NOT NULL
                UNION SELECT symbol FROM smartgain_holdings WHERE symbol IS NOT NULL) p""")
            open_positions = cur.fetchone()[0]
            cur.execute("""
                WITH pos AS (
                    SELECT symbol FROM v8_paper_positions WHERE status='OPEN' AND symbol IS NOT NULL
                    UNION SELECT symbol FROM smartgain_holdings WHERE symbol IS NOT NULL
                )
                -- cc#619: PREFER the polished_news summary for this exact article when one exists —
                -- join position_news.url_hash -> raw_news.url_hash (globally unique) -> polished_news
                -- (Intel-tab quality). Fall back to the CC one-liner (pn.summary), else the raw
                -- headline. Read-only: no writes to polished_news, no new polish pipeline.
                SELECT pn.id, pn.symbol, pn.origin, pn.headline,
                       COALESCE(pol.full_summary, pn.summary) AS summary,
                       pn.source_name, pn.url,
                       COALESCE(pn.published_at, pn.fetched_at) AS published_at,
                       (COALESCE(pol.full_summary, pn.summary) IS NOT NULL) AS polished,
                       (pol.full_summary IS NOT NULL) AS intel
                FROM position_news pn
                JOIN pos ON pos.symbol = pn.symbol
                LEFT JOIN raw_news rn ON rn.url_hash = pn.url_hash
                LEFT JOIN polished_news pol ON pol.raw_news_id = rn.id
                WHERE COALESCE(pn.published_at, pn.fetched_at) >= NOW() - INTERVAL '7 days'
                ORDER BY pn.symbol ASC,
                         COALESCE(pn.published_at, pn.fetched_at) DESC NULLS LAST, pn.id DESC
            """)
            for pid, sym, origin, headline, summary, source, url, pub, polished, intel in cur.fetchall():
                g = groups.get(sym)
                if g is None:
                    g = {"symbol": sym, "origin": origin, "count": 0, "items": []}
                    groups[sym] = g
                g["count"] += 1
                g["items"].append({
                    "headline": headline, "summary": summary, "body": summary,   # body kept for the renderer
                    "source": source, "url": url, "symbol": sym,
                    "polished": bool(polished), "intel": bool(intel),   # intel = Intel-tab polished match
                    "published_at": pub.isoformat() if pub else None,
                })
    except Exception as e:
        return JSONResponse({"error": str(e), "groups": [], "symbols": 0, "total": 0,
                             "open_positions": 0}, status_code=200)
    # newest-active symbols first: biggest groups on top, then alphabetical
    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["symbol"]))
    total = sum(g["count"] for g in ordered)
    return {"open_positions": open_positions, "symbols": len(ordered), "total": total, "groups": ordered}


@router.on_event("startup")
async def _startup_backfill():
    """cc#611 PART A: on boot, run an immediate position-news backfill if the feed is stale (>18h) —
    so a deploy after a stall self-heals the open-position tab without waiting for the next slot."""
    import threading

    def _go():
        try:
            import position_news
            position_news.backfill_if_stale(hours=18)
        except Exception as e:
            import logging
            logging.getLogger("position_news").warning(f"cc#611 startup backfill skipped: {e}")

    threading.Thread(target=_go, name="cc611-posnews-backfill", daemon=True).start()
