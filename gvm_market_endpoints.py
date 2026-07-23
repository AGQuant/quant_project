"""
GVM + Market read endpoints — self-contained router.

Extracted from main.py (refactor file 2/5, 04-Jun-2026). Read-only data endpoints.
Self-contained: own _conn, api_query, _ist_now. Imports nothing from main.py.

Endpoints:
  GET /api/gvm/{symbol}            — full GVM + input_raw overview/takeaway/result_analysis
  GET /api/gvm/top/{n}             — top N by GVM (optional verdict filter)
  GET /api/filter                  — stocks in a GVM range
  GET /api/sectors                 — sector ratings (no param) OR per-stock segment ladder (?segment=)
  GET /api/market/top_gainers      — top gainers by day%, joined with GVM
  GET /api/cmp/{symbol}            — latest CMP
  GET /api/intraday/{symbol}       — intraday OHLC from DB
  GET /api/intraday_ondemand/{symbol} — on-demand Yahoo intraday
  GET /api/global                  — latest global scorecard
  GET /api/global/history/{name}   — global index daily history
  GET /api/global/intraday/{name}  — global 5-min intraday

NOTE: /api/gvm/{symbol} and /api/gvm/top/{n} live here, but /api/gvm/recompute
and /api/gvm/history/{symbol} remain in gvm_nightly.py (gvm_nightly_router).
Path ordering: this router is included AFTER gvm_nightly_router in main.py so the
static /recompute + /history routes are matched before the /{symbol} catch-all.
"""

from fastapi import APIRouter, HTTPException
from datetime import date, datetime, timedelta
from typing import Optional
import psycopg
import os
import asyncio

import yahoo_ondemand

router = APIRouter(tags=["gvm_market"])


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _ist_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def api_query(sql, params=None, single=False):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params or ())
            cols = [d[0] for d in cur.description] if cur.description else []
            if single:
                r = cur.fetchone()
                return dict(zip(cols, r)) if r else None
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        return {"error": str(e)}


# ── GVM ─────────────────────────────────────────────────────────────────────

@router.get("/api/gvm/{symbol}")
def get_gvm(symbol: str):
    # full company map — cap_category + instrument_type + mcap_rank + overview + takeaway + result_analysis
    r = api_query("""
        SELECT g.symbol, g.company_name, g.segment, g.price,
               g.g_score, g.v_score, g.m_score, g.gvm_score,
               g.verdict, g.punchline, g.market_cap,
               i.overview, i.key_takeaway, i.result_analysis,
               i.instrument_type, i.cap_category, i.mcap_rank,
               i.last_overview_updated::text AS last_overview_updated,
               i.last_takeaway_updated::text AS last_takeaway_updated,
               i.last_result_analysis_updated::text AS last_result_analysis_updated
        FROM gvm_scores g
        LEFT JOIN input_raw i ON i.nse_code = g.symbol
        WHERE g.symbol = %s
    """, (symbol.upper(),), single=True)
    if not r:
        raise HTTPException(404, f"{symbol} not found")
    return r


@router.get("/api/gvm/snapshot/{symbol}")
def get_gvm_snapshot(symbol: str):
    """cc#608: compact GVM snapshot for the Open-Positions quick-action "G" popout — GVM + G/V/M
    pillars + verdict + punchline + 180d delta-GVM (gvm_history) + segment rank. A subset of the
    full company report (never the whole thing). Read-only."""
    sym = symbol.upper()
    base = api_query("""SELECT symbol, company_name, segment, gvm_score, g_score, v_score, m_score,
                               verdict, punchline, ROUND(price::numeric,2) AS price, market_cap
                        FROM gvm_scores WHERE symbol=%s""", (sym,), single=True)
    if not base:
        raise HTTPException(404, f"{symbol} not found")
    cur_gvm = base.get("gvm_score")
    # 180d delta-GVM: the score ~6 months ago (nearest row in a ±20d window around T-180) vs today.
    d = api_query("""SELECT gvm_score FROM gvm_history WHERE symbol=%s
                     AND score_date BETWEEN CURRENT_DATE-200 AND CURRENT_DATE-160
                     ORDER BY score_date DESC LIMIT 1""", (sym,), single=True)
    base["dgvm_180"] = (round(float(cur_gvm) - float(d["gvm_score"]), 2)
                        if (cur_gvm is not None and d and d.get("gvm_score") is not None) else None)
    # segment rank by GVM (1 = best); rank = peers-with-higher-GVM + 1, out of segment total.
    base["segment_rank"] = base["segment_total"] = None
    if base.get("segment") and cur_gvm is not None:
        rk = api_query("""SELECT (SELECT COUNT(*) FROM gvm_scores WHERE segment=%s AND gvm_score > %s)+1 AS rnk,
                                 (SELECT COUNT(*) FROM gvm_scores WHERE segment=%s AND gvm_score IS NOT NULL) AS total""",
                       (base["segment"], cur_gvm, base["segment"]), single=True)
        if rk:
            base["segment_rank"] = rk.get("rnk")
            base["segment_total"] = rk.get("total")
    return base


@router.get("/api/candles/{symbol}")
def get_candles(symbol: str, days: int = 90):
    """cc#608: daily OHLC candles from raw_prices for the quick-action "C" chart popout (equity
    symbols; the existing /api/v10/candles is index-only). Pairs with /api/intraday/{symbol} for
    the 5m-today view. Read-only."""
    days = min(max(days, 5), 365)
    return api_query("""SELECT price_date::text AS date,
                               ROUND(open::numeric,2)  AS open,  ROUND(high::numeric,2) AS high,
                               ROUND(low::numeric,2)   AS low,   ROUND(close::numeric,2) AS close,
                               volume
                        FROM raw_prices WHERE symbol=%s AND price_date >= CURRENT_DATE - %s
                        ORDER BY price_date ASC""", (symbol.upper(), days))


@router.get("/api/gvm/top/{n}")
def get_top(n: int, verdict: Optional[str] = None):
    n = min(max(n, 1), 100)
    if verdict:
        return api_query("SELECT symbol, company_name, segment, g_score, v_score, m_score, gvm_score, verdict, market_cap FROM gvm_scores WHERE verdict=%s ORDER BY gvm_score DESC LIMIT %s", (verdict, n))
    return api_query("SELECT symbol, company_name, segment, g_score, v_score, m_score, gvm_score, verdict, market_cap FROM gvm_scores ORDER BY gvm_score DESC LIMIT %s", (n,))


@router.get("/api/filter")
def get_filter(min_gvm: float = 0, max_gvm: float = 10):
    return api_query("SELECT symbol, company_name, segment, g_score, v_score, m_score, gvm_score, verdict, market_cap FROM gvm_scores WHERE gvm_score>=%s AND gvm_score<=%s ORDER BY gvm_score DESC", (min_gvm, max_gvm))


@router.get("/api/sectors")
def get_sectors(segment: Optional[str] = None):
    # No segment → sector-level mcap-weighted ratings (legacy behaviour, unchanged).
    if not segment:
        return api_query("SELECT segment, simple_avg_gvm AS avg_gvm, mcap_weighted_gvm, stocks_count AS stock_count, verdict, top_stock, top_stock_gvm FROM sector_ratings ORDER BY mcap_weighted_gvm DESC")
    # Segment provided → per-stock ladder for that segment, ordered by GVM.
    # RSIm sourced from momentum_scores (full-universe coverage). v8_metrics holds
    # only the F&O set, so non-futures segments (microfinance/MSME/etc.) returned
    # blank RSIm before this fix (15-Jun-2026). Validated live same day.
    return api_query("""
        SELECT g.symbol, g.company_name, g.segment,
               ROUND(g.gvm_score::numeric,2) AS gvm_score,
               ROUND(g.g_score::numeric,2)   AS g_score,
               ROUND(g.v_score::numeric,2)   AS v_score,
               ROUND(g.m_score::numeric,2)   AS m_score,
               g.verdict, g.market_cap,
               ROUND(g.price::numeric,2)     AS price,
               ROUND(s.pe::numeric,2)                    AS pe,
               ROUND(s."Price to book value"::numeric,2) AS pb,
               ROUND(s."Return on equity"::numeric,2)    AS roe,
               ROUND(s.opm::numeric,2)                   AS opm,
               ROUND(s.dividend_yield::numeric,2)        AS div_yield,
               ROUND(s.return_1y::numeric,2)             AS return_1y,
               ROUND(rsi.rsi_month::numeric,2)           AS rsi_month,
               rsi.rsi_month_rating
        FROM gvm_scores g
        LEFT JOIN screener_raw s ON s.nse_code = g.symbol
        LEFT JOIN LATERAL (
            SELECT rsi_month, rsi_month_rating FROM momentum_scores ms
            WHERE ms.symbol = g.symbol ORDER BY ms.score_date DESC LIMIT 1
        ) rsi ON true
        WHERE g.segment ILIKE %s
        ORDER BY g.gvm_score DESC NULLS LAST
    """, (f"%{segment}%",))


# ── Market ───────────────────────────────────────────────────────────────────

@router.get("/api/market/top_gainers")
def get_top_gainers(price_date: Optional[str] = None, n: int = 20, min_gvm: Optional[float] = None,
                    min_day_pct: Optional[float] = None, universe: str = "all", min_volume: Optional[int] = None):
    n = min(max(n, 1), 100)
    if not price_date:
        row = api_query("SELECT MAX(price_date)::text AS latest FROM raw_prices", single=True)
        price_date = row["latest"] if row else str(date.today())
    conds = ["r.price_date=%s", "r.open>0", "r.close>0"]
    vals = [price_date]
    if min_volume:
        conds.append("r.volume>=%s"); vals.append(min_volume)
    if universe == "gvm_only":
        conds.append("g.symbol IS NOT NULL")
    if min_gvm is not None:
        conds.append("g.gvm_score>=%s"); vals.append(min_gvm)
    having = f"HAVING ROUND(((r.close/NULLIF(r.open,0)-1)*100)::numeric,2)>={float(min_day_pct)}" if min_day_pct is not None else ""
    join_type = "INNER" if universe == "gvm_only" else "LEFT"
    sql = f"""
        SELECT r.symbol, COALESCE(g.company_name,r.symbol) AS company_name, COALESCE(g.segment,'Unknown') AS segment,
               ROUND(r.close::numeric,2) AS close, ROUND(r.open::numeric,2) AS open,
               ROUND(((r.close/NULLIF(r.open,0)-1)*100)::numeric,2) AS day_pct,
               r.volume, ROUND(g.gvm_score::numeric,2) AS gvm_score,
               ROUND(g.g_score::numeric,2) AS g_score, ROUND(g.v_score::numeric,2) AS v_score,
               ROUND(g.m_score::numeric,2) AS m_score, g.verdict, r.price_date::text AS price_date
        FROM raw_prices r {join_type} JOIN gvm_scores g ON r.symbol=g.symbol
        WHERE {" AND ".join(conds)}
        GROUP BY r.symbol,g.company_name,g.segment,r.close,r.open,r.volume,g.gvm_score,g.g_score,g.v_score,g.m_score,g.verdict,r.price_date
        {having} ORDER BY day_pct DESC LIMIT %s
    """
    vals.append(n)
    return api_query(sql, vals)


@router.get("/api/cmp/{symbol}")
def get_cmp(symbol: str):
    r = api_query("SELECT symbol, cmp, updated_at, source FROM cmp_prices WHERE symbol=%s", (symbol.upper(),), single=True)
    if not r:
        raise HTTPException(404, f"{symbol} CMP not found")
    return r


@router.get("/api/intraday/{symbol}")
def get_intraday(symbol: str, days: int = 1):
    days = min(max(days, 1), 7)
    cutoff = _ist_now() - timedelta(days=days)
    return api_query("SELECT symbol,ts,open,high,low,close,volume FROM intraday_prices WHERE symbol=%s AND ts>=%s ORDER BY ts ASC", (symbol.upper(), cutoff))


@router.get("/api/intraday_ondemand/{symbol}")
async def intraday_ondemand(symbol: str, days: int = 15, interval: str = "5m", source: str = "auto"):
    return await asyncio.to_thread(yahoo_ondemand.get_intraday_smart, symbol.upper(), days, interval, "NS", source)


# ── Global ───────────────────────────────────────────────────────────────────

@router.get("/api/global")
def get_global():
    return api_query("""
        SELECT g.symbol, g.name, g.category, g.price, g.prev_close, g.chg_pct,
               g.quote_date::text AS quote_date, g.source, g.updated_at::text AS updated_at
        FROM global_indices g
        JOIN (SELECT symbol, MAX(quote_date) AS md FROM global_indices GROUP BY symbol) m
          ON g.symbol=m.symbol AND g.quote_date=m.md
        ORDER BY CASE g.category WHEN 'index' THEN 1 WHEN 'volatility' THEN 2 WHEN 'commodity' THEN 3 WHEN 'currency' THEN 4 ELSE 5 END, g.name
    """)


@router.get("/api/global/history/{name}")
def get_global_history(name: str, days: int = 1825):
    cutoff = (_ist_now().date() - timedelta(days=days))
    return api_query("SELECT name,symbol,category,price,prev_close,chg_pct,quote_date::text FROM global_indices WHERE LOWER(name)=LOWER(%s) AND quote_date>=%s ORDER BY quote_date ASC", (name, cutoff))


@router.get("/api/global/intraday/{name}")
def get_global_intraday(name: str, days: int = 7):
    cutoff = _ist_now() - timedelta(days=min(max(days, 1), 7))
    return api_query("SELECT symbol,name,ts,open,high,low,close,volume FROM global_intraday WHERE UPPER(name)=UPPER(%s) AND ts>=%s ORDER BY ts ASC", (name, cutoff))
