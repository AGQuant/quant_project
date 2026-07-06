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
from typing import Optional, List
import psycopg
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

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
