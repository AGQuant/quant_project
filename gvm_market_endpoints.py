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

from fastapi import APIRouter, HTTPException, Query
from datetime import date, datetime, timedelta
from typing import Optional
import psycopg
import os
import asyncio
import json

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


def _ops_log(category, title, details):
    """cc#673: fire-and-forget ops_log write for chart-data invariants (silent on healthy symbols)."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                        "VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)", (category, title, json.dumps(details)))
            conn.commit()
    except Exception:
        pass


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


def _flt(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _traj_point(sym, segment, days):
    """cc#630: as-of (CURRENT_DATE-days) — segment MEDIAN gvm + this symbol's rank + member count,
    from gvm_history nearest-prior rows (one per symbol). Cheap indexed lookup."""
    if not segment:
        return {}
    return api_query("""
        WITH latest AS (
            SELECT DISTINCT ON (symbol) symbol, gvm_score FROM gvm_history
            WHERE segment=%s AND score_date <= CURRENT_DATE - %s ORDER BY symbol, score_date DESC)
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY gvm_score) AS med,
               COUNT(*) FILTER (WHERE gvm_score IS NOT NULL) AS n,
               (SELECT COUNT(*) FROM latest l2 WHERE l2.gvm_score >
                    (SELECT gvm_score FROM latest WHERE symbol=%s)) + 1 AS rnk
        FROM latest""", (segment, days, sym), single=True) or {}


def _trajectory(sym, segment, gvm_now, m_now):
    """cc#630: TRAJECTORY block — stock GVM/M deltas (1M vs <=d-21, 6M vs <=d-126), sector GVM deltas
    (median-based, same convention as the sector-delta row), and the rank path (now/1M/6M). 6M crosses
    the backfill era (live gvm_history began 2026-05-30) -> backfill_flag_6m from the d-126 row's
    method. gvm_history ONLY; nearest-prior; no new feeds."""
    d21 = api_query("SELECT gvm_score, m_score FROM gvm_history WHERE symbol=%s AND score_date<=CURRENT_DATE-21 "
                    "ORDER BY score_date DESC LIMIT 1", (sym,), single=True) or {}
    d126 = api_query("SELECT gvm_score, m_score, method FROM gvm_history WHERE symbol=%s AND score_date<=CURRENT_DATE-126 "
                     "ORDER BY score_date DESC LIMIT 1", (sym,), single=True) or {}

    def _dd(now, then):
        return round(now - then, 2) if (now is not None and then is not None) else None
    p0, p1, p6 = _traj_point(sym, segment, 0), _traj_point(sym, segment, 21), _traj_point(sym, segment, 126)
    med0, med1, med6 = _flt(p0.get("med")), _flt(p1.get("med")), _flt(p6.get("med"))
    return {
        # cc#631: NOW values for the 3x3 matrix (sector = same segment-median basis as the deltas)
        "gvm_now": round(gvm_now, 2) if gvm_now is not None else None,
        "m_now": round(m_now, 2) if m_now is not None else None,
        "sector_gvm_now": round(med0, 2) if med0 is not None else None,
        "gvm_d21": _dd(gvm_now, _flt(d21.get("gvm_score"))), "gvm_d126": _dd(gvm_now, _flt(d126.get("gvm_score"))),
        "m_d21": _dd(m_now, _flt(d21.get("m_score"))), "m_d126": _dd(m_now, _flt(d126.get("m_score"))),
        "sector_gvm_d21": _dd(med0, med1), "sector_gvm_d126": _dd(med0, med6),
        "rank_now": p0.get("rnk"), "rank_1m": p1.get("rnk"), "rank_6m": p6.get("rnk"),
        "segment_n": p0.get("n"),
        "backfill_flag_6m": bool(d126 and d126.get("method") is not None),
    }


_PERF_IDX = {"w1": 5, "m1": 21, "m3": 63, "y1": 252}   # trading-row offsets for 1W/1M/3M/1Y


def _perf_from_closes(closes):
    out = {}
    c0 = closes[0] if closes else None
    for k, i in _PERF_IDX.items():
        out[k] = (round((c0 - closes[i]) / closes[i] * 100.0, 2)
                  if (c0 is not None and len(closes) > i and closes[i]) else None)
    return out


def _perf_grid(sym, segment):
    """cc#630: 2x4 price-performance heat grid (Stock / Sector × 1W/1M/3M/1Y). Stock = raw_prices close
    latest vs the close 5/21/63/252 trading rows back. Sector = MEDIAN of segment members' same-window
    change (raw_prices), consistent with the trajectory block's median sector math. Server-side."""
    scl = api_query("SELECT close FROM raw_prices WHERE symbol=%s AND close IS NOT NULL "
                    "ORDER BY price_date DESC LIMIT 260", (sym,))
    stock_closes = [_flt(r["close"]) for r in scl] if isinstance(scl, list) else []
    stock_perf = _perf_from_closes(stock_closes)
    sector_perf = {k: None for k in _PERF_IDX}
    members = []
    if segment:
        m = api_query("SELECT symbol FROM gvm_scores WHERE segment=%s AND "
                      "score_date=(SELECT MAX(score_date) FROM gvm_scores)", (segment,))
        if isinstance(m, list):
            members = [r["symbol"] for r in m]
    if members:
        rows = api_query("SELECT symbol, close FROM raw_prices WHERE symbol=ANY(%s) AND close IS NOT NULL "
                         "AND price_date >= CURRENT_DATE-400 ORDER BY symbol, price_date DESC", (members,))
        bym = {}
        if isinstance(rows, list):
            for r in rows:
                bym.setdefault(r["symbol"], []).append(_flt(r["close"]))
        buckets = {k: [] for k in _PERF_IDX}
        for _s, cl in bym.items():
            p = _perf_from_closes(cl)
            for k in _PERF_IDX:
                if p[k] is not None:
                    buckets[k].append(p[k])
        for k in _PERF_IDX:
            vals = sorted(buckets[k])
            sector_perf[k] = round(vals[len(vals) // 2], 2) if vals else None
    return {"stock": stock_perf, "sector": sector_perf}


_TREND_RANGE_DAYS = {"3m": 95, "1y": 372, "3y": 1100, "5y": 1830}


@router.get("/api/gvm/trend/{symbol}")
def get_gvm_trend(symbol: str, rng: str = Query("5y", alias="range")):
    """cc#634: GVM + Price series for the click-to-expand chart (up to 5Y). gvm_history (composite +
    G/V/M) and raw_prices EOD close, over range 3m|1y|3y|5y, each downsampled to <=260 points for
    payload sanity (weekly-ish at 5Y). v_backfill_era flags that v_score was held constant pre-live
    (method IS NOT NULL) — same honesty disclosure as the trajectory 6M asterisk.
    cc#649: the query param is bound as `rng` (alias 'range') — the old `range: str` param shadowed the
    builtin range(), so the >260-point downsample (only hit by 3Y/5Y) raised TypeError and 500'd."""
    days = _TREND_RANGE_DAYS.get((rng or "5y").lower(), 1830)
    sym = symbol.upper()

    def _ds(rows):
        if not isinstance(rows, list) or len(rows) <= 260:
            return rows if isinstance(rows, list) else []
        n = len(rows); step = n / 260.0
        return [rows[min(int(i * step), n - 1)] for i in range(260)]

    gv = api_query("""SELECT score_date::text AS d, ROUND(gvm_score::numeric,2) AS gvm,
                             ROUND(g_score::numeric,2) AS g, ROUND(v_score::numeric,2) AS v,
                             ROUND(m_score::numeric,2) AS m, (method IS NOT NULL) AS backfill
                      FROM gvm_history WHERE symbol=%s AND score_date >= CURRENT_DATE - %s
                      ORDER BY score_date ASC""", (sym, days))
    px = api_query("""SELECT price_date::text AS d, ROUND(close::numeric,2) AS close FROM raw_prices
                      WHERE symbol=%s AND price_date >= CURRENT_DATE - %s AND close IS NOT NULL
                      ORDER BY price_date ASC""", (sym, days))
    v_backfill = bool(isinstance(gv, list) and any(r.get("backfill") for r in gv))
    return {"symbol": sym, "range": (rng or "5y").lower(),
            "gvm": _ds(gv), "price": _ds(px), "v_backfill_era": v_backfill}


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
    # cc#627: sector RATING box — the segment's mcap-weighted GVM + verdict (sector_ratings, existing
    # table; zero new data source) so the Analysis panel can show "segment · rating · rank".
    base["sector_rating"] = base["sector_verdict"] = None
    if base.get("segment"):
        sr = api_query("SELECT ROUND(mcap_weighted_gvm::numeric,2) AS r, verdict FROM sector_ratings WHERE segment=%s",
                       (base["segment"],), single=True)
        if sr:
            base["sector_rating"] = sr.get("r")
            base["sector_verdict"] = sr.get("verdict")
    # cc#630: TRAJECTORY block (gvm/m/sector deltas + rank path, gvm_history nearest-prior) + perf
    # heat grid (raw_prices, median sector). Best-effort — never fails the snapshot.
    try:
        base["trajectory"] = _trajectory(sym, base.get("segment"),
                                         _flt(base.get("gvm_score")), _flt(base.get("m_score")))
    except Exception:
        base["trajectory"] = None
    try:
        perf = _perf_grid(sym, base.get("segment"))
        base["stock_perf"], base["sector_perf"] = perf["stock"], perf["sector"]
    except Exception:
        base["stock_perf"] = base["sector_perf"] = None
    return base


@router.get("/api/candles/{symbol}")
def get_candles(symbol: str, days: int = 90):
    """cc#608/cc#669: daily OHLC candles from raw_prices for the price-chart popout (equity symbols;
    /api/v10/candles is index-only). Pairs with /api/intraday/{symbol} for the 5m view. Read-only.
    ``days`` <= 0 => ALL tab: the symbol's COMPLETE stored history (MIN(price_date) per symbol, never
    hardcoded). To keep the payload light (cc#649 pattern), history longer than ~1,500 daily bars is
    downsampled server-side to WEEKLY candles (first-open / max-high / min-low / last-close / sum-vol);
    shorter history returns daily. cc#806: bounded windows are capped at 1825 days (was 365), so the
    card's 1M/3M/6M/1Y/3Y/ALL pills all pass their full calendar window untruncated."""
    sym = symbol.upper()
    # cc#673: anchor the window to MAX(price_date) for THIS symbol (not CURRENT_DATE) so the tail is
    # ALWAYS the latest stored bar regardless of server clock / feed lag / holidays — fixes the
    # "chart ends weeks early" class of bug. Then assert the last returned bar == MAX(price_date).
    mx = api_query("SELECT MAX(price_date)::text AS d FROM raw_prices WHERE symbol=%s", (sym,), single=True)
    maxd = mx.get("d") if isinstance(mx, dict) else None
    if not maxd:
        return []
    if days <= 0:   # ALL — full stored history, weekly-downsampled above ~1,500 points
        cnt = api_query("SELECT COUNT(*) AS c FROM raw_prices WHERE symbol=%s", (sym,), single=True)
        if isinstance(cnt, dict) and (cnt.get("c") or 0) > 1500:
            # weekly buckets: last bucket is the week-start of maxd (not maxd itself) → skip the invariant.
            return api_query("""SELECT to_char(date_trunc('week', price_date),'YYYY-MM-DD') AS date,
                                       ROUND((array_agg(open  ORDER BY price_date ASC ))[1]::numeric,2) AS open,
                                       ROUND(MAX(high)::numeric,2) AS high,
                                       ROUND(MIN(low)::numeric,2)  AS low,
                                       ROUND((array_agg(close ORDER BY price_date DESC))[1]::numeric,2) AS close,
                                       SUM(volume) AS volume
                                FROM raw_prices WHERE symbol=%s
                                GROUP BY date_trunc('week', price_date) ORDER BY date ASC""", (sym,))
        rows = api_query("""SELECT price_date::text AS date,
                                   ROUND(open::numeric,2)  AS open,  ROUND(high::numeric,2) AS high,
                                   ROUND(low::numeric,2)   AS low,   ROUND(close::numeric,2) AS close,
                                   volume
                            FROM raw_prices WHERE symbol=%s ORDER BY price_date ASC""", (sym,))
    else:
        # cc#806: cap raised 365 -> 1825. It HAD to be: the shared chart card asked for ALL with
        # days=365*20 and this line silently clamped it to 365, so "ALL" rendered exactly one year —
        # identical to the 1Y pill. (The V8 local chart sent days=0 and took the branch above, so it
        # showed real full history; that is the same GVM-vs-V8 divergence cc#803 closed, one layer
        # down in the transport.) With the cap at 365 the new 3Y/ALL pills would all have collapsed
        # onto 1Y too. 1825 daily bars ≈ 1,250 rows — under the 1,500-row weekly-downsample threshold,
        # so these stay daily. days<=0 still means true full stored history.
        days = min(max(days, 5), 1825)
        rows = api_query("""SELECT price_date::text AS date,
                                   ROUND(open::numeric,2)  AS open,  ROUND(high::numeric,2) AS high,
                                   ROUND(low::numeric,2)   AS low,   ROUND(close::numeric,2) AS close,
                                   volume
                            FROM raw_prices WHERE symbol=%s AND price_date > %s::date - %s
                            ORDER BY price_date ASC""", (sym, maxd, days))
    # cc#673 invariant: the last daily bar returned MUST be the symbol's latest stored bar.
    if isinstance(rows, list) and rows and str(rows[-1].get("date")) != maxd:
        _ops_log("chart_window", "chart_window_mismatch",
                 {"symbol": sym, "last_bar": str(rows[-1].get("date")), "max_price_date": maxd, "days": days})
    return rows


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


@router.get("/api/fo-ban/today")
def get_fo_ban_today():
    """cc#681: symbols currently in the F&O ban (MWPL) list. Latest ban date, recency-guarded (<=4d)
    so a stale MAX(d) never shows a lifted ban as current — an empty result renders NO BANS."""
    row = api_query("SELECT MAX(d)::text AS d FROM fo_ban WHERE d >= CURRENT_DATE - 4", single=True)
    d = row.get("d") if isinstance(row, dict) else None
    if not d:
        return {"date": None, "symbols": []}
    rows = api_query("SELECT symbol FROM fo_ban WHERE d=%s ORDER BY symbol", (d,))
    return {"date": d, "symbols": [r["symbol"] for r in rows] if isinstance(rows, list) else []}


@router.get("/api/cmp/{symbol}")
def get_cmp(symbol: str):
    r = api_query("SELECT symbol, cmp, updated_at, source FROM cmp_prices WHERE symbol=%s", (symbol.upper(),), single=True)
    if not r:
        raise HTTPException(404, f"{symbol} CMP not found")
    return r


@router.get("/api/intraday/{symbol}")
def get_intraday(symbol: str, days: int = 1, sessions: int = 0):
    """cc#626/cc#669: EQUITY 5m bars from the 12-month warehouse — UNION of source IN
    ('fyers_eq','fyers_hist','fyers_ext') deduped on ts (prefer the live fyers_eq feed on collision).

    cc#809: 'fyers_ext' is the extended (non-F&O) equity leg. Without it here the worker would write
    ~2.5M rows a month that no surface can read — the 5m tab would stay blank for every one of the
    ~1,600 newly-live symbols. The mandatory-source-filter invariant below is untouched: ext and eq
    are disjoint symbol sets, so no symbol can return two rows at the same ts.
    Returns the last N TRADING sessions (``sessions`` param — 1D/5D/20D range control; falls back
    to legacy ``days`` when ``sessions`` is 0), anchored to the latest session that actually has
    equity bars so the tab is never empty (off-market/holiday fallback). LightweightCharts spaces
    bars by index, so overnight gaps compress automatically — no flat lines across closed hours.
    ROOT CAUSE of the historic 5m-blank bug (kept fixed here): a source filter is mandatory, else an
    F&O symbol returns fyers_fut rows at the SAME ts and setData() throws on duplicate times."""
    n_sess = sessions if sessions and sessions > 0 else max(days, 1)
    n_sess = min(max(n_sess, 1), 40)
    sym = symbol.upper()
    dts = api_query("""SELECT DISTINCT ts::date AS d FROM intraday_prices
                       WHERE symbol=%s AND source IN ('fyers_eq','fyers_hist','fyers_ext')
                       ORDER BY d DESC LIMIT %s""", (sym, n_sess))
    if not dts:
        return []
    oldest = dts[-1]["d"]   # dts is DESC → last row is the oldest of the N sessions
    return api_query("""SELECT symbol, ts, open, high, low, close, volume FROM (
                          SELECT DISTINCT ON (ts) symbol, ts, open, high, low, close, volume
                          FROM intraday_prices
                          WHERE symbol=%s AND source IN ('fyers_eq','fyers_hist','fyers_ext')
                            AND ts::date >= %s
                          ORDER BY ts, CASE source WHEN 'fyers_eq' THEN 0 ELSE 1 END
                        ) q ORDER BY ts ASC""", (sym, oldest))


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
