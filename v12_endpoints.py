"""
v12_endpoints.py -- Scorr V12 Custom Equity Screener (CC_TASK_103).
Mounted in main.py via: app.include_router(v12_router)

  GET /api/v12/screen        -- filter ~1700 stocks across GVM / V8 / fundamentals
                                / technicals / pivots / TC; ranked + paginated.
  GET /api/v12/filters/meta  -- live min/max ranges + option lists for the
                                frontend to set slider bounds / dropdowns.
  GET /api/v12/filters       -- alias of /filters/meta.

Public (no auth). Read-only. All SQL is parameterised (no injection). All
filters optional -- no filter returns the full universe, paginated. Default
sort: gvm_score DESC NULLS LAST. Logic lives here, not in main.py.
"""
import os
import json
from typing import Optional, List
import psycopg
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(DATABASE_URL)


# BFSI rule: when a leverage filter (de_max) is applied, auto-exclude financial
# segments -- debt/equity & interest-coverage are not meaningful for Banks /
# NBFCs / Insurance / AMC / Exchanges. The live DB stores granular segment names
# ("Private Banks", "NBFC - Large", "Housing Finance", "Life Insurance",
# "Capital Markets - Large", ...), so we exclude by pattern, not a fixed list.
_BFSI_PATTERNS = ["%bank%", "%nbfc%", "%financ%", "%insur%",
                  "%capital market%", "%exchange%", "%microfin%", "%asset manag%"]

# Sortable output columns (alias names). Column identifiers cannot be
# parameterised, so any sort_by outside this allow-list falls back to gvm_score.
_SORTABLE = {
    "gvm_score", "g_score", "v_score", "m_score", "rank", "market_cap", "price",
    "pe", "opm", "roce", "roe", "de_ratio", "promoter_holding", "dividend_yield",
    "pb_ratio", "rsi_weekly", "rsi_month", "daily_rsi", "dma_50", "dma_200",
    "week_return", "month_return", "year_return", "week_index_52",
    "profit_growth_3y", "profit_growth_5y", "sales_growth_3y", "sector_gvm",
    "tc_score", "company_name", "symbol",
}

# Canonical 8-table join. Ends at "WHERE 1=1" so callers append " AND ...".
# cc#154: LEFT JOIN ut (universe_technicals, full ~1766-symbol GVM universe) and
# COALESCE technicals+pivots -- v8_metrics/v8_paper_pivots (5-min-fresh,
# futures-only) still wins when present; ut fills the ~1557-row gap for
# non-futures stocks with EOD-frozen values. sector_week/month/day, vol_ratio,
# ma9_vs_ma21, eod_chg stay futures-only (not
# computed by universe_technicals) -- unchanged from before.
# cc#232: 3 dead range/BB metrics dropped from this SELECT.
_BASE_SQL = """SELECT g.symbol, g.company_name, g.segment, g.gvm_score, g.g_score, g.v_score, g.m_score, g.verdict, g.rank, g.market_cap, g.price, g.gvm_overall_label, COALESCE(m.rsi_weekly, ut.rsi_weekly) as rsi_weekly, COALESCE(m.rsi_month, ut.rsi_month) as rsi_month, COALESCE(m.daily_rsi, ut.daily_rsi) as daily_rsi, COALESCE(m.dma_50, ut.dma_50) as dma_50, COALESCE(m.dma_200, ut.dma_200) as dma_200, COALESCE(m.dma_20, ut.dma_20) as dma_20, COALESCE(m.week_return, ut.week_return) as week_return, COALESCE(m.month_return, ut.month_return) as month_return, COALESCE(m.year_return, ut.year_return) as year_return, COALESCE(m.mom_2d, ut.mom_2d) as mom_2d, COALESCE(m.week_index_52, ut.week_index_52) as week_index_52, m.sector_week, m.sector_month, m.sector_day, m.vol_ratio, m.ma9_vs_ma21, m.eod_chg, s.pe, s.opm, s.roce, s."Debt to equity" as de_ratio, s."Promoter holding" as promoter_holding, s."Return on equity" as roe, s.profit_growth_3y, s.profit_growth_5y, s.sales_growth_3y, s.sales_growth_5y, s."Sales growth" as sales_growth_1y, s.dividend_yield, s.fii_change, s.dii_change, s."Price to book value" as pb_ratio, s.interest_coverage, s.fixed_asset_growth, s."EPS growth 5Years" as eps_growth_5y, s.opm_latest_q, s.qoq_profit_growth, s.qoq_sales_growth, v.basket as v8_basket, v.signal_date as v8_signal_date, COALESCE(p.pp, ut.pp) as pp, COALESCE(p.r1, ut.r1) as r1, COALESCE(p.r2, ut.r2) as r2, COALESCE(p.s1, ut.s1) as s1, COALESCE(p.s2, ut.s2) as s2, tc.score as tc_score, tc.verdict as tc_verdict, tc.side as tc_side, sr.mcap_weighted_gvm as sector_gvm, sr.verdict as sector_rating_verdict, CASE WHEN ec.ticker IS NOT NULL THEN true ELSE false END as in_blackout FROM gvm_scores g LEFT JOIN v8_metrics m ON g.symbol = m.symbol AND m.score_date = (SELECT MAX(score_date) FROM v8_metrics) LEFT JOIN universe_technicals ut ON g.symbol = ut.symbol AND ut.score_date = (SELECT MAX(score_date) FROM universe_technicals) LEFT JOIN screener_raw s ON g.symbol = s.nse_code LEFT JOIN v8_qualified v ON g.symbol = v.symbol AND v.signal_date = CURRENT_DATE LEFT JOIN v8_paper_pivots p ON g.symbol = p.symbol AND p.pivot_date = (SELECT MAX(pivot_date) FROM v8_paper_pivots) LEFT JOIN tc_screener_cache tc ON g.symbol = tc.symbol AND tc.run_date = (SELECT MAX(run_date) FROM tc_screener_cache) LEFT JOIN sector_ratings sr ON g.segment = sr.segment AND sr.score_date = (SELECT MAX(score_date) FROM sector_ratings) LEFT JOIN earnings_calendar ec ON g.symbol = ec.ticker AND ec.ex_date BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '7 days' WHERE 1=1"""


@router.get("/api/v12/screen")
def v12_screen(
    # GVM / classification
    gvm_min: Optional[float] = None, gvm_max: Optional[float] = None,
    market_cap_min: Optional[float] = None, market_cap_max: Optional[float] = None,
    sector_gvm_min: Optional[float] = None,
    verdict: Optional[List[str]] = Query(None),
    segment: Optional[List[str]] = Query(None),
    category: Optional[List[str]] = Query(None),
    # fundamentals
    pe_min: Optional[float] = None, pe_max: Optional[float] = None,
    de_max: Optional[float] = None,
    roce_min: Optional[float] = None,
    promoter_min: Optional[float] = None,
    profit_growth_3y_min: Optional[float] = None,
    # technicals
    rsi_weekly_min: Optional[float] = None, rsi_weekly_max: Optional[float] = None,
    rsi_month_min: Optional[float] = None, rsi_month_max: Optional[float] = None,
    dma_50_min: Optional[float] = None, dma_50_max: Optional[float] = None,
    dma_200_min: Optional[float] = None, dma_200_max: Optional[float] = None,
    week_index_52_min: Optional[float] = None, week_index_52_max: Optional[float] = None,
    week_return_min: Optional[float] = None, week_return_max: Optional[float] = None,
    month_return_min: Optional[float] = None, month_return_max: Optional[float] = None,
    # V8 / Trade Check
    v8_basket: Optional[List[str]] = Query(None),
    v8_qualified_only: bool = False,
    tc_side: Optional[str] = None,
    tc_verdict: Optional[List[str]] = Query(None),
    futures_only: bool = False,
    exclude_blackout: bool = True,
    # sort + pagination
    sort_by: str = "gvm_score",
    sort_dir: str = "desc",
    page: int = 0,
    size: int = 50,
):
    """Custom equity screener -- every filter optional, parameterised, paginated."""
    conds: list = []
    params: list = []
    applied: list = []

    def rng(col, lo, hi, name):
        if lo is not None:
            conds.append(f"{col} >= %s"); params.append(lo); applied.append(f"{name}_min")
        if hi is not None:
            conds.append(f"{col} <= %s"); params.append(hi); applied.append(f"{name}_max")

    rng("g.gvm_score", gvm_min, gvm_max, "gvm")
    rng("g.market_cap", market_cap_min, market_cap_max, "market_cap")
    rng("s.pe", pe_min, pe_max, "pe")
    # cc#154: filter conditions COALESCE the same way as the SELECT list, so a
    # non-futures stock only visible via universe_technicals filters consistently
    # with what it displays (was m.x-only, silently excluding ~1557 rows).
    rng("COALESCE(m.rsi_weekly, ut.rsi_weekly)", rsi_weekly_min, rsi_weekly_max, "rsi_weekly")
    rng("COALESCE(m.rsi_month, ut.rsi_month)", rsi_month_min, rsi_month_max, "rsi_month")
    rng("COALESCE(m.dma_50, ut.dma_50)", dma_50_min, dma_50_max, "dma_50")
    rng("COALESCE(m.dma_200, ut.dma_200)", dma_200_min, dma_200_max, "dma_200")
    rng("COALESCE(m.week_index_52, ut.week_index_52)", week_index_52_min, week_index_52_max, "week_index_52")
    rng("COALESCE(m.week_return, ut.week_return)", week_return_min, week_return_max, "week_return")
    rng("COALESCE(m.month_return, ut.month_return)", month_return_min, month_return_max, "month_return")

    if roce_min is not None:
        conds.append("s.roce >= %s"); params.append(roce_min); applied.append("roce_min")
    if promoter_min is not None:
        conds.append('s."Promoter holding" >= %s'); params.append(promoter_min); applied.append("promoter_min")
    if profit_growth_3y_min is not None:
        conds.append("s.profit_growth_3y >= %s"); params.append(profit_growth_3y_min); applied.append("profit_growth_3y_min")
    if sector_gvm_min is not None:
        conds.append("sr.mcap_weighted_gvm >= %s"); params.append(sector_gvm_min); applied.append("sector_gvm_min")

    if de_max is not None:
        conds.append('s."Debt to equity" <= %s'); params.append(de_max); applied.append("de_max")
        # BFSI rule -- leverage filter auto-excludes financial segments
        conds.append("NOT (" + " OR ".join(["g.segment ILIKE %s"] * len(_BFSI_PATTERNS)) + ")")
        params.extend(_BFSI_PATTERNS); applied.append("bfsi_excluded")

    if verdict:
        conds.append("UPPER(g.verdict) = ANY(%s)"); params.append([v.upper() for v in verdict]); applied.append("verdict")
    if segment:
        conds.append("g.segment = ANY(%s)"); params.append(segment); applied.append("segment")
    if tc_verdict:
        conds.append("UPPER(tc.verdict) = ANY(%s)"); params.append([v.upper() for v in tc_verdict]); applied.append("tc_verdict")
    if tc_side:
        conds.append("UPPER(tc.side) = %s"); params.append(tc_side.upper()); applied.append("tc_side")
    if v8_basket:
        conds.append("v.basket = ANY(%s)"); params.append(v8_basket); applied.append("v8_basket")
    if v8_qualified_only:
        conds.append("v.basket IS NOT NULL"); applied.append("v8_qualified_only")
    if futures_only:
        conds.append("g.symbol IN (SELECT symbol FROM futures_universe WHERE is_active = true)")
        applied.append("futures_only")
    if exclude_blackout:
        conds.append("ec.ticker IS NULL"); applied.append("exclude_blackout")

    if category:
        cat_parts = []
        for c in category:
            cl = (c or "").strip().lower()
            if cl.startswith("large"):
                cat_parts.append("g.market_cap > 20000")
            elif cl.startswith("mid"):
                cat_parts.append("(g.market_cap >= 5000 AND g.market_cap <= 20000)")
            elif cl.startswith("small"):
                cat_parts.append("g.market_cap < 5000")
        if cat_parts:
            conds.append("(" + " OR ".join(cat_parts) + ")"); applied.append("category")

    where_extra = (" AND " + " AND ".join(conds)) if conds else ""

    # sort -- whitelisted column, quoted to dodge reserved words (e.g. rank)
    sb = sort_by if sort_by in _SORTABLE else "gvm_score"
    sd = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    order_sql = f' ORDER BY "{sb}" {sd} NULLS LAST'

    # pagination
    size = max(1, min(int(size), 200))
    page = max(0, int(page))
    offset = page * size

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM (" + _BASE_SQL + where_extra + ") sub", params)
        total = cur.fetchone()[0]
        cur.execute(_BASE_SQL + where_extra + order_sql + " LIMIT %s OFFSET %s",
                    params + [size, offset])
        cols = [d[0] for d in cur.description]
        stocks = [dict(zip(cols, r)) for r in cur.fetchall()]

    return {
        "page": page,
        "size": size,
        "total": total,
        "count": len(stocks),
        "filters_applied": applied,
        "stocks": stocks,
    }


@router.get("/api/v12/filters/meta")
@router.get("/api/v12/filters")
def v12_filters_meta():
    """Live min/max ranges + option lists -- frontend uses these to bound
    sliders and populate dropdowns dynamically."""
    out: dict = {}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT MIN(gvm_score), MAX(gvm_score), MIN(market_cap), MAX(market_cap)
            FROM gvm_scores WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)
        """)
        gmin, gmax, mcmin, mcmax = cur.fetchone()
        out["gvm_score"] = {"min": gmin, "max": gmax}
        out["market_cap"] = {"min": mcmin, "max": mcmax}

        cur.execute('SELECT MIN(pe), MAX(pe), MIN(roce), MAX(roce) FROM screener_raw')
        pmin, pmax, rcmin, rcmax = cur.fetchone()
        out["pe"] = {"min": pmin, "max": pmax}
        out["roce"] = {"min": rcmin, "max": rcmax}

        cur.execute("""
            SELECT MIN(rsi_weekly), MAX(rsi_weekly), MIN(rsi_month), MAX(rsi_month),
                   MIN(week_return), MAX(week_return), MIN(month_return), MAX(month_return)
            FROM v8_metrics WHERE score_date = (SELECT MAX(score_date) FROM v8_metrics)
        """)
        rw0, rw1, rm0, rm1, wr0, wr1, mr0, mr1 = cur.fetchone()
        out["rsi_weekly"] = {"min": rw0, "max": rw1}
        out["rsi_month"] = {"min": rm0, "max": rm1}
        out["week_return"] = {"min": wr0, "max": wr1}
        out["month_return"] = {"min": mr0, "max": mr1}

        cur.execute("""
            SELECT segment, COUNT(*) FROM gvm_scores
            WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores) AND segment IS NOT NULL
            GROUP BY segment ORDER BY segment
        """)
        out["segments"] = [{"segment": s, "count": c} for s, c in cur.fetchall()]

        cur.execute("""
            SELECT verdict, COUNT(*) FROM gvm_scores
            WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores) AND verdict IS NOT NULL
            GROUP BY verdict ORDER BY COUNT(*) DESC
        """)
        out["verdicts"] = [{"verdict": v, "count": c} for v, c in cur.fetchall()]

    out["categories"] = ["Large Cap", "Mid Cap", "Small Cap"]
    out["v8_baskets"] = ["buy_reversal", "buy_momentum", "sell_reversal",
                         "sell_momentum", "sell_overbought"]
    out["tc_verdicts"] = ["STRONG", "VALID", "WATCH"]
    out["tc_sides"] = ["LONG", "SHORT"]
    return out


@router.get("/screener", response_class=HTMLResponse)
def screener_page():
    """V12 screener frontend (CC_TASK_106). Auth-gated via PROTECTED in main.py middleware."""
    with open("screener.html", "r", encoding="utf-8") as f:
        return f.read()


# ===========================================================================
# cc#394 V12 QUANT BASKET BUILDER (master spec id=2970) — MODULE 1: universe API
# ===========================================================================
# component_1: universe = screener_raw + gvm_scores + input_raw. All filter keys reuse the V13
# FUNDAMENTAL vocabulary (one vocabulary, never a second list). BFSI rule enforced server-side:
# a leverage filter (D/E or interest coverage) auto-excludes financial segments. Universe is a
# saved object (v12_universes), dynamic (re-evaluated) or frozen (symbol list snapshotted at save).

# filter key -> SQL expression. g=gvm_scores, s=screener_raw, i=input_raw
_UNI_COLS = {
    "price":            "g.price",
    "market_cap":       "g.market_cap",
    "mcap_rank":        "i.mcap_rank",
    "gvm":              "g.gvm_score",
    "g_score":          "g.g_score",
    "v_score":          "g.v_score",
    "m_score":          "g.m_score",
    "roe":              's."Return on equity"',
    "roce":             "s.roce",
    "opm":              "s.opm",
    "de":               's."Debt to equity"',
    "pb":               's."Price to book value"',
    "pe":               "s.pe",
    "div_yield":        "s.dividend_yield",
    "int_cov":          "s.interest_coverage",
    "sales_growth_3y":  "s.sales_growth_3y",
    "sales_growth_5y":  "s.sales_growth_5y",
    "profit_growth_3y": "s.profit_growth_3y",
    "profit_growth_5y": "s.profit_growth_5y",
    "qoq_sales":        "s.qoq_sales_growth",
    "qoq_profit":       "s.qoq_profit_growth",
    "promoter":         's."Promoter holding"',
    "fii_change":       "s.fii_change",
    "dii_change":       "s.dii_change",
    "w52_index":        "s.return_52w_vs_index",
}
_UNI_LEVERAGE_KEYS = {"de", "int_cov"}   # any of these triggers the BFSI exclusion

_UNI_BASE = """
    FROM gvm_scores g
    LEFT JOIN screener_raw s ON UPPER(s.nse_code) = UPPER(g.symbol)
    LEFT JOIN input_raw i    ON UPPER(i.nse_code) = UPPER(g.symbol)
    WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
"""


def _uni_where(filters: dict):
    """Build the WHERE tail + params from a component_1 filter dict. Each numeric key takes
    {min?, max?}; `segments` takes a list. BFSI auto-exclusion when a leverage key is used."""
    conds, params, applied = [], [], []
    bfsi = False
    segs = (filters or {}).get("segments")
    if segs:
        conds.append("g.segment = ANY(%s)"); params.append(list(segs)); applied.append("segments")
    for key, expr in _UNI_COLS.items():
        rng = (filters or {}).get(key)
        if not isinstance(rng, dict):
            continue
        lo, hi = rng.get("min"), rng.get("max")
        if lo is not None:
            conds.append(f"{expr} >= %s"); params.append(lo); applied.append(key + "_min")
        if hi is not None:
            conds.append(f"{expr} <= %s"); params.append(hi); applied.append(key + "_max")
        if key in _UNI_LEVERAGE_KEYS and (lo is not None or hi is not None):
            bfsi = True
    if bfsi:
        conds.append("NOT (" + " OR ".join(["g.segment ILIKE %s"] * len(_BFSI_PATTERNS)) + ")")
        params.extend(_BFSI_PATTERNS); applied.append("bfsi_excluded")
    where = (" AND " + " AND ".join(conds)) if conds else ""
    return where, params, applied


class V12UniverseReq(BaseModel):
    name: Optional[str] = None
    filters: dict = {}
    mode: str = "dynamic"     # dynamic (re-evaluated at use) | frozen (symbols snapshotted at save)
    limit: int = 500


@router.post("/api/v12/universe/preview")
def v12_universe_preview(body: V12UniverseReq):
    """Preview a universe from a filter set — count + ranked sample. No write."""
    where, params, applied = _uni_where(body.filters or {})
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) " + _UNI_BASE + where, params)
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT g.symbol, g.company_name, g.segment, g.gvm_score, g.g_score, g.v_score, "
            "g.m_score, g.verdict, g.market_cap, g.price, i.mcap_rank "
            + _UNI_BASE + where + " ORDER BY g.gvm_score DESC NULLS LAST LIMIT %s",
            params + [min(max(int(body.limit or 500), 1), 2000)])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"total": total, "count": len(rows), "filters_applied": applied,
            "bfsi_excluded": ("bfsi_excluded" in applied), "stocks": rows}


@router.post("/api/v12/universe/save")
def v12_universe_save(body: V12UniverseReq):
    """Persist a universe object (v12_universes). mode='frozen' snapshots the symbol list."""
    if not body.name:
        return {"error": "name required"}
    where, params, applied = _uni_where(body.filters or {})
    definition = {"filters": body.filters or {}, "mode": body.mode, "filters_applied": applied}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) " + _UNI_BASE + where, params)
        total = cur.fetchone()[0]
        if body.mode == "frozen":
            cur.execute("SELECT g.symbol " + _UNI_BASE + where + " ORDER BY g.symbol", params)
            definition["frozen_symbols"] = [r[0] for r in cur.fetchall()]
        cur.execute("INSERT INTO v12_universes (name, definition, created_at, updated_at) "
                    "VALUES (%s, %s::jsonb, NOW(), NOW()) RETURNING id",
                    (body.name, json.dumps(definition)))
        uid = cur.fetchone()[0]
        conn.commit()
    return {"id": uid, "name": body.name, "total": total, "mode": body.mode,
            "filters_applied": applied}


@router.get("/api/v12/universe")
def v12_universe_list():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, definition, created_at FROM v12_universes ORDER BY id DESC LIMIT 100")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"count": len(rows), "universes": rows}


@router.get("/api/v12/universe/{uid}")
def v12_universe_get(uid: int):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, definition, created_at FROM v12_universes WHERE id=%s", (uid,))
        r = cur.fetchone()
        if not r:
            return {"error": "not found"}
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, r))


# ===========================================================================
# cc#394 V12 — MODULE 2: basket CRUD + definition JSONB validator (spec fix_3)
# ===========================================================================
# ONE basket definition JSONB consumed by TWO executors (bt walker + paper walker). The validator
# is the single gate both share, so a saved basket is always structurally runnable.
_ROC_LOOKBACKS = {"1M", "3M", "6M", "12M"}
_REBAL_FREQ = {"weekly", "monthly", "quarterly"}


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_basket_def(d: dict) -> list:
    """Validate the basket definition per spec fix_3. Returns a list of human-readable errors
    ([] = valid). entry (mandatory momentum rank + optional RSI/EMA gates), exit (all optional),
    rebalance, costs, universe_ref."""
    errs = []
    if not isinstance(d, dict):
        return ["definition must be an object"]
    e = d.get("entry")
    if not isinstance(e, dict):
        errs.append("entry is required")
        e = {}
    else:
        roc = e.get("roc_lookback")
        if isinstance(roc, str):
            if roc not in _ROC_LOOKBACKS:
                errs.append(f"entry.roc_lookback must be one of {sorted(_ROC_LOOKBACKS)} or a blend list")
        elif isinstance(roc, list):
            if not roc or any(not isinstance(x, dict) or x.get("lookback") not in _ROC_LOOKBACKS
                              or not _isnum(x.get("weight")) for x in roc):
                errs.append("entry.roc_lookback blend items need {lookback in set, weight number}")
        else:
            errs.append("entry.roc_lookback required (a lookback string or a blend list)")
        manual = e.get("manual_list")
        if manual is not None:
            if not isinstance(manual, list) or not all(isinstance(s, str) for s in manual):
                errs.append("entry.manual_list must be a list of symbols")
        elif not (isinstance(e.get("top_x"), int) and e["top_x"] >= 1):
            errs.append("entry.top_x must be an int >= 1 (or provide entry.manual_list)")
        for k in ("min_stocks", "max_stocks"):
            if k in e and not (isinstance(e[k], int) and e[k] >= 1):
                errs.append(f"entry.{k} must be a positive int")
        if isinstance(e.get("min_stocks"), int) and isinstance(e.get("max_stocks"), int) and e["min_stocks"] > e["max_stocks"]:
            errs.append("entry.min_stocks cannot exceed entry.max_stocks")
        rsi = e.get("rsi_gate")
        if rsi is not None and not (isinstance(rsi, dict) and rsi.get("tf") in ("D", "W", "M")
                                    and isinstance(rsi.get("period"), int) and _isnum(rsi.get("threshold"))
                                    and rsi.get("dir") in ("above", "below")):
            errs.append("entry.rsi_gate needs {tf D/W/M, period int, threshold num, dir above/below}")
        ema = e.get("ema_gate")
        if ema is not None and not (isinstance(ema, dict) and ema.get("tf") in ("D", "W", "M")
                                    and isinstance(ema.get("ema1"), int) and isinstance(ema.get("ema2"), int)):
            errs.append("entry.ema_gate needs {tf D/W/M, ema1 int, ema2 int, ema3? int}")
    x = d.get("exit") or {}
    if not isinstance(x, dict):
        errs.append("exit must be an object")
    else:
        if "trailing_peak_pct" in x and not _isnum(x["trailing_peak_pct"]):
            errs.append("exit.trailing_peak_pct must be a number")
        if "rank_fall_y" in x and not (isinstance(x["rank_fall_y"], int) and x["rank_fall_y"] >= 1):
            errs.append("exit.rank_fall_y must be a positive int")
        if isinstance(x.get("rank_fall_y"), int) and isinstance(e.get("top_x"), int) and x["rank_fall_y"] < e["top_x"]:
            errs.append("exit.rank_fall_y must be >= entry.top_x")
        for k in ("weight_max_pct", "weight_cushion"):
            if k in x and not _isnum(x[k]):
                errs.append(f"exit.{k} must be a number")
        if "gate_mirror" in x and not isinstance(x["gate_mirror"], bool):
            errs.append("exit.gate_mirror must be true/false")
    rb = d.get("rebalance")
    if not (isinstance(rb, dict) and rb.get("freq") in _REBAL_FREQ):
        errs.append(f"rebalance.freq must be one of {sorted(_REBAL_FREQ)}")
    c = d.get("costs") or {}
    if not isinstance(c, dict):
        errs.append("costs must be an object")
    else:
        for k in ("txn_pct", "slippage_pct"):
            if k in c and not _isnum(c[k]):
                errs.append(f"costs.{k} must be a number")
    u = d.get("universe_ref")
    if u is None:
        errs.append("universe_ref is required (a v12_universes id or an inline {filters} object)")
    elif not (isinstance(u, int) or isinstance(u, dict)):
        errs.append("universe_ref must be a universe id (int) or an object with filters")
    return errs


class V12BasketReq(BaseModel):
    name: str
    definition: dict
    status: Optional[str] = None


@router.post("/api/v12/basket/validate")
def v12_basket_validate(body: V12BasketReq):
    errs = _validate_basket_def(body.definition or {})
    return {"valid": not errs, "errors": errs}


@router.post("/api/v12/basket")
def v12_basket_create(body: V12BasketReq):
    errs = _validate_basket_def(body.definition or {})
    if errs:
        return {"error": "invalid definition", "errors": errs}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO v12_baskets (name, definition, status, created_at, updated_at) "
                    "VALUES (%s, %s::jsonb, 'draft', NOW(), NOW()) RETURNING id",
                    (body.name, json.dumps(body.definition)))
        bid = cur.fetchone()[0]
        conn.commit()
    return {"id": bid, "name": body.name, "status": "draft"}


@router.get("/api/v12/basket")
def v12_basket_list():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, status, definition, created_at, updated_at "
                    "FROM v12_baskets ORDER BY id DESC LIMIT 200")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"count": len(rows), "baskets": rows}


@router.get("/api/v12/basket/{bid}")
def v12_basket_get(bid: int):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, status, definition, created_at, updated_at FROM v12_baskets WHERE id=%s", (bid,))
        r = cur.fetchone()
        if not r:
            return {"error": "not found"}
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, r))


@router.put("/api/v12/basket/{bid}")
def v12_basket_update(bid: int, body: V12BasketReq):
    errs = _validate_basket_def(body.definition or {})
    if errs:
        return {"error": "invalid definition", "errors": errs}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE v12_baskets SET name=%s, definition=%s::jsonb, status=COALESCE(%s,status), "
                    "updated_at=NOW() WHERE id=%s RETURNING id",
                    (body.name, json.dumps(body.definition), body.status, bid))
        r = cur.fetchone()
        conn.commit()
    return {"id": bid, "name": body.name, "updated": True} if r else {"error": "not found"}


@router.delete("/api/v12/basket/{bid}")
def v12_basket_delete(bid: int):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM v12_baskets WHERE id=%s RETURNING id", (bid,))
        r = cur.fetchone()
        conn.commit()
    return {"deleted": bool(r), "id": bid}
