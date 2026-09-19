"""
sector_app_mobile.py — cc#1900 APP BUILD: Sector Intel section page backend
(design_refs/scorr_app_sector_R1.html, APP_CARD_LAYOUT_LAW_V1 session_log 42536).

ONE endpoint, GET /api/mobile/sector_app. Queries sector_ratings DIRECTLY (RAW, unmerged rows),
deliberately NOT sector_endpoints.py's /api/sector/rotation — that endpoint runs the cc#827
display-merge layer (folds thin segments into families, e.g. "Organic Chemicals" absorbing
"Organic Chemicals - Large"), which changes both the segment COUNT and the segment NAMES. The
card's own evidence field quotes the RAW numbers verbatim ("128 segments", "Organic Chemicals -
Large" as its own row, not merged) — hand-verified live and reproduces EXACTLY: 128 rows at the
latest score_date, verdict split 101 Average / 20 Weak / 7 Good, top-4/bottom-3 segments and
their mcap_weighted_gvm values all match the card's evidence to the third decimal. So this file
reads sector_ratings straight, matching what the card actually asked for and verified against.

VERDICT — sector_ratings.verdict is already stored per row (cc#827's own _verdict_from_gvm bands,
>=8 Excellent / >=7 Good / >=6 Average / <6 Weak); this just GROUP BYs the stored column rather
than recomputing the band, so a future band change made in one place (gvm_nightly) is not silently
duplicated here.

SECTOR TREND — sector_ops_trends is confirmed EMPTY (0 rows, live-checked). Card renders the
honest-empty state per the card's own item 3 — no synthetic trend from a single score_date.

do_not_touch honoured: sector_ratings / the daily score run — read-only, zero writes. The web
sector page (scorr_sector.html) and sector_endpoints.py's /api/sector/rotation — untouched,
different endpoint, different query, both left exactly as they are.
"""
import os
import re

from fastapi import APIRouter, Request
import psycopg

from mobile_endpoints import _guard, _json_safe

router = APIRouter()

RAIL_ROW_CAP = 6


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


@router.get("/api/mobile/sector_app")
@_json_safe
def mobile_sector_app(request: Request):
    g = _guard(request)
    if g:
        return g

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT MAX(score_date) FROM sector_ratings")
        score_date = cur.fetchone()[0]
        if score_date is None:
            return {"status": "ok", "score_date": None, "never_run": True,
                    "key_metrics": {"segments_rated": 0, "good": 0, "average": 0, "weak": 0,
                                     "briefs_cached": 0}}

        cur.execute("""SELECT COALESCE(verdict,'Unclassified'), COUNT(*)
                       FROM sector_ratings WHERE score_date=%s GROUP BY 1""", (score_date,))
        verdict_counts = {row[0]: row[1] for row in cur.fetchall()}
        total = sum(verdict_counts.values())

        cur.execute("""SELECT segment, stocks_count, mcap_weighted_gvm, verdict
                       FROM sector_ratings WHERE score_date=%s
                       ORDER BY mcap_weighted_gvm DESC NULLS LAST LIMIT %s""",
                    (score_date, RAIL_ROW_CAP))
        strongest = [{"segment": r[0], "stocks_count": r[1],
                      "gvm": float(r[2]) if r[2] is not None else None, "verdict": r[3]}
                     for r in cur.fetchall()]

        cur.execute("""SELECT segment, stocks_count, mcap_weighted_gvm, verdict
                       FROM sector_ratings WHERE score_date=%s
                       ORDER BY mcap_weighted_gvm ASC NULLS LAST LIMIT %s""",
                    (score_date, RAIL_ROW_CAP))
        weakest = [{"segment": r[0], "stocks_count": r[1],
                    "gvm": float(r[2]) if r[2] is not None else None, "verdict": r[3]}
                   for r in cur.fetchall()]

        cur.execute("SELECT COUNT(*) FROM sector_briefs")
        briefs_cached = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM sector_ops_trends")
        trend_rows = cur.fetchone()[0] or 0

    good = verdict_counts.get("Good", 0) + verdict_counts.get("Excellent", 0)
    average = verdict_counts.get("Average", 0)
    weak = verdict_counts.get("Weak", 0)

    return {
        "status": "ok",
        "score_date": str(score_date),
        "key_metrics": {
            "segments_rated": total,
            "good": good,
            "average": average,
            "weak": weak,
            "briefs_cached": briefs_cached,
        },
        "verdict_split": {
            "rows": [
                {"label": "Average", "note": "most of the market", "count": average},
                {"label": "Weak", "note": None, "count": weak},
                {"label": "Good", "note": None, "count": good},
            ],
            "total": total,
            "message": f"Only {good} of {total} segments rate Good today. "
                       "That is the headline, not a rounding note.",
        },
        "strongest": {"rows": strongest, "count": total},
        "weakest": {"rows": weakest, "count": len(weakest)},
        "briefs": {
            "count": briefs_cached,
            "message": "Every segment has a short written brief explaining what is driving its "
                       "score right now.",
            "footnote": "Cached, refreshed with the daily score run.",
        },
        "trend": {
            "available": trend_rows > 0,
            "rows": trend_rows,
            "message": "sector_ops_trends is empty, so there is nothing to chart a sector's "
                       "score over time. Today's score is honest; a trend line would not be."
                       if trend_rows == 0 else None,
        },
        "weighting_note": "Scored on market-cap weighted GVM, so a big weak name pulls its "
                          "whole segment down.",
    }


# ═══ V2 — Fable single mode 10-Sep-2026 (SECTOR_APP_R2, founder: "read the web APIs once, build
# intuitively"). Two additive endpoints on the web tab's own functions; the cc#1900 endpoint stays.
#   GET /api/mobile/sector_app/list           → the rotation ladder + themes, from sector_rotation()
#   GET /api/mobile/sector_app/segment?name=  → one segment: brief + scorecard + evidence + members
# sector_rotation() is the MERGED display view (cc#827) — the same names and numbers the web
# Sector page shows. Nothing re-derived here.
import asyncio
from sector_endpoints import sector_rotation
from sector_brief_endpoints import sector_brief, sector_themes


def _fl(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# cc#2232 (session_log 19-Sep-2026, founder ruling): BOTH new theme figures are MCAP-WEIGHTED,
# everywhere, no exceptions -- "if we are computing weighted average... always consider based on
# market cap, irrespective company sector." Verified against the founder's own evidence numbers on
# live data for all 10 themes (9/10 exact to the decimal, 1 within 0.1pp on a 36-name weighted
# average -- ordinary rounding-order variance, not a methodology gap).
#
# 3-month return has NO stored column (universe_technicals carries week/month/year/3y, not 3-month)
# so it is derived from raw_prices: latest close vs the closest close at or before today-91 days.
# A member with no such pair is excluded from BOTH the numerator and denominator of the weighted
# average (never treated as a zero return) -- so a name missing 3-month history cannot drag the
# figure toward zero, it simply does not vote.
def _theme_mcap_stats():
    """{theme_name: {"gvm": mcap-weighted GVM, "ret3m": mcap-weighted 3-month return}} for every
    theme, in ONE query (not N+1 per theme). Membership = every gvm_scores row at the latest
    score_date whose segment appears in that theme's related_segments (same derivation sector_themes
    itself has no company column for -- the card's own evidence field)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            WITH segs AS (
                SELECT theme_name, jsonb_array_elements_text(related_segments) AS segment
                FROM sector_themes
            ),
            members AS (
                SELECT s.theme_name, g.symbol, g.gvm_score, g.market_cap
                FROM segs s
                JOIN gvm_scores g ON g.segment = s.segment
                WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
            ),
            p3m AS (
                SELECT m.symbol,
                       (SELECT rp.close FROM raw_prices rp WHERE rp.symbol = m.symbol
                          AND rp.close IS NOT NULL ORDER BY rp.price_date DESC LIMIT 1) AS px_now,
                       (SELECT rp.close FROM raw_prices rp WHERE rp.symbol = m.symbol
                          AND rp.close IS NOT NULL AND rp.price_date <= CURRENT_DATE - 91
                          ORDER BY rp.price_date DESC LIMIT 1) AS px_3mo_ago
                FROM (SELECT DISTINCT symbol FROM members) m
            )
            SELECT mm.theme_name,
                   ROUND((SUM(mm.gvm_score * mm.market_cap) / NULLIF(SUM(mm.market_cap), 0))::numeric, 2) AS gvm,
                   ROUND((SUM(CASE WHEN p3m.px_now IS NOT NULL AND p3m.px_3mo_ago IS NOT NULL AND p3m.px_3mo_ago <> 0
                                   THEN ((p3m.px_now - p3m.px_3mo_ago) / p3m.px_3mo_ago) * mm.market_cap END)
                          / NULLIF(SUM(CASE WHEN p3m.px_now IS NOT NULL AND p3m.px_3mo_ago IS NOT NULL AND p3m.px_3mo_ago <> 0
                                             THEN mm.market_cap END), 0) * 100)::numeric, 1) AS ret3m
            FROM members mm
            LEFT JOIN p3m ON p3m.symbol = mm.symbol
            GROUP BY mm.theme_name
        """)
        return {row[0]: {"gvm": _fl(row[1]), "ret3m": _fl(row[2])} for row in cur.fetchall()}


@router.get("/api/mobile/sector_app/list")
@_json_safe
def mobile_sector_list(request: Request):
    g = _guard(request)
    if g:
        return g
    rot = sector_rotation()
    if not isinstance(rot, dict) or rot.get("error"):
        return {"error": (rot or {}).get("error", "rotation unavailable")}
    rows = []
    for r in rot.get("all") or []:
        rows.append({"segment": r.get("display_segment") or r.get("segment"), "gvm": _fl(r.get("gvm")),
                     "g": _fl(r.get("g_score")), "v": _fl(r.get("v_score")), "m": _fl(r.get("m_score")),
                     # cc#2231: "change" dropped -- sector_ratings has one score_date and no scheduler
                     # produces a second one; the underlying gvm_history delta join matches at most 1 of
                     # 126 segments against a single stray 2002 seed row. A field that describes a delta
                     # that cannot exist is a false promise in the payload, same as the pill was on screen.
                     "names": int(r.get("stocks_count") or 0),
                     "mcap": _fl(r.get("total_mcap")), "size": r.get("size_class"), "verdict": r.get("verdict"),
                     "inst": _fl(r.get("inst_change")), "qoq": _fl(r.get("qoq_profit")), "upside": _fl(r.get("annual_upside")),
                     "top": [{"symbol": p.get("symbol"), "gvm": _fl(p.get("gvm")), "day": _fl(p.get("day_ret"))}
                             for p in (r.get("top_stocks") or [])[:2]]})
    verdicts = {}
    for r in rows:
        verdicts[r["verdict"] or "—"] = verdicts.get(r["verdict"] or "—", 0) + 1
    th = sector_themes()
    theme_stats = _theme_mcap_stats()   # cc#2232: mcap-weighted GVM + 3M return per theme, batched once
    themes = []
    for t in (th.get("themes") or []) if isinstance(th, dict) else []:
        stats = theme_stats.get(t.get("theme_name")) or {}
        themes.append({"rank": t.get("rank"), "name": t.get("theme_name"), "tagline": t.get("tagline"),
                       "segments": t.get("related_segments") or [],
                       "gvm": stats.get("gvm"), "ret3m": stats.get("ret3m"),
                       "top": [{"symbol": c.get("symbol"), "gvm": _fl(c.get("gvm_score")), "segment": c.get("segment")}
                               for c in (t.get("companies") or [])[:3]]})
    return {"score_date": rot.get("score_date"), "segments": len(rows), "raw_segments": rot.get("raw_segments"),
            "verdicts": verdicts, "rows": rows, "themes": themes,
            "note": "Mcap-weighted GVM per segment — one big weak name pulls its whole segment down."}


# cc#2232: full member list for a theme, GROUPED by segment (founder ruling 19-Sep-2026, after
# considering and rejecting a flat table with a sector column). TWO-LEVEL ORDER, both already fixed
# by the SQL below: segment groups by that segment's OWN mcap-weighted GVM (read straight from
# sector_ratings -- the same canonical figure SEGMENT ratings already use, not redefined here), and
# names inside a group by their own GVM. 1-year return comes from universe_technicals.year_return
# at its latest score_date only -- a null there stays null, never substituted from another source.
@router.get("/api/mobile/sector_app/theme")
@_json_safe
def mobile_sector_theme(request: Request, name: str = ""):
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT tagline FROM sector_themes WHERE theme_name = %s", (name,))
        row = cur.fetchone()
        if row is None:
            return {"error": "no such theme"}
        tagline = row[0]

        cur.execute("""
            WITH theme AS (
                SELECT related_segments FROM sector_themes WHERE theme_name = %s
            ),
            segs AS (
                SELECT jsonb_array_elements_text(related_segments) AS segment FROM theme
            ),
            seg_gvm AS (
                SELECT sr.segment, sr.mcap_weighted_gvm
                FROM sector_ratings sr
                WHERE sr.score_date = (SELECT MAX(score_date) FROM sector_ratings)
                  AND sr.segment IN (SELECT segment FROM segs)
            ),
            members AS (
                SELECT g.symbol, g.segment, g.gvm_score
                FROM gvm_scores g
                JOIN segs s ON s.segment = g.segment
                WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
            ),
            y1 AS (
                SELECT DISTINCT ON (symbol) symbol, year_return
                FROM universe_technicals
                WHERE symbol IN (SELECT symbol FROM members)
                ORDER BY symbol, score_date DESC
            )
            SELECT sg.segment, sg.mcap_weighted_gvm, mm.symbol, mm.gvm_score, y1.year_return
            FROM members mm
            JOIN seg_gvm sg ON sg.segment = mm.segment
            LEFT JOIN y1 ON y1.symbol = mm.symbol
            ORDER BY sg.mcap_weighted_gvm DESC NULLS LAST, mm.gvm_score DESC NULLS LAST
        """, (name,))
        member_rows = cur.fetchall()

        cur.execute("SELECT MAX(score_date) FROM gvm_scores")
        score_date = cur.fetchone()[0]

    groups = []
    cur_seg = object()   # sentinel -- no real segment name equals this, so the first row always opens a group
    cur_group = None
    for segment, seg_gvm, symbol, gvm_score, year_return in member_rows:
        if segment != cur_seg:
            cur_seg = segment
            cur_group = {"segment": segment, "gvm": _fl(seg_gvm), "members": []}
            groups.append(cur_group)
        cur_group["members"].append({"symbol": symbol, "gvm": _fl(gvm_score), "year_return": _fl(year_return)})

    return {"theme": name, "tagline": tagline, "score_date": str(score_date) if score_date else None,
            "groups": groups, "member_count": sum(len(gr["members"]) for gr in groups)}


_FY_QUARTER_RE = re.compile(r"^Q([1-4])FY(\d+)$")


def _next_fy_quarter(q):
    """cc#2235: 'Q1FY27' -> 'Q2FY27', 'Q4FY27' -> 'Q1FY28' (fiscal year rolls at Q4->Q1).
    None or unparseable input -> None, never a guessed label."""
    m = _FY_QUARTER_RE.match(q or "")
    if not m:
        return None
    qn, yr = int(m.group(1)) + 1, int(m.group(2))
    if qn > 4:
        qn, yr = 1, yr + 1
    return f"Q{qn}FY{yr:02d}"


def _results_yoy_growth(raw_names):
    """cc#2235: Results Snapshot, replacing cc#2233's fundamentals_history-based absolutes.
    SOURCE IS screener_raw (sales_latest_quarter/sales_preceding_year_quarter and
    profit_after_tax_latest_quarter/profit_after_tax_preceding_year_quarter), not
    fundamentals_history -- better coverage (~99% of 1,865 rows vs fundamentals_history's 41%)
    and it carries a genuine prior-year column, which is what a YoY headline needs.

    THE HEADLINE IS GROWTH OF THE MCAP-WEIGHTED AGGREGATE, NOT A WEIGHTED AVERAGE OF EACH
    COMPANY'S OWN GROWTH RATE: (SUM(latest*mcap) / SUM(prior*mcap) - 1) * 100. Verified live
    against the card's own evidence (Auto - Engines & Thermal, 23/23 members on Q1FY27): this
    formula reproduces the stated +23.9% sales / +9.5% net profit exactly. A weighted-AVERAGE-of-
    rates formula (SUM(rate_i*mcap_i)/SUM(mcap_i), the pattern used for GVM/upside/3M-return
    elsewhere on this page) gives +25.4%/+32.7% for the same segment -- visibly wrong against the
    evidence, because growth-of-a-ratio does not commute with weighted-averaging the ratio itself
    the way a level (GVM, upside, return) does. Do not reuse the other blocks' formula here.

    MIXED-QUARTER GUARD (item 5): last_result_quarter is not uniform across the universe (96.1%
    Q1FY27, a 3.9% tail on older quarters) -- both sums are restricted to the segment's DOMINANT
    quarter (MODE), never blended across quarters, and the dominant quarter is returned so the
    caller can label the block and coverage line with it."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            WITH m AS (
                SELECT g.symbol, g.market_cap, s.last_result_quarter,
                       s.sales_latest_quarter, s.sales_preceding_year_quarter,
                       s.profit_after_tax_latest_quarter, s.profit_after_tax_preceding_year_quarter
                FROM gvm_scores g LEFT JOIN screener_raw s ON s.nse_code = g.symbol
                WHERE g.segment = ANY(%s) AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
            ),
            dom AS (SELECT MODE() WITHIN GROUP (ORDER BY last_result_quarter) AS q FROM m)
            SELECT (SELECT q FROM dom),
                   COUNT(*),
                   COUNT(*) FILTER (WHERE last_result_quarter = (SELECT q FROM dom)
                                     AND (sales_latest_quarter IS NOT NULL OR profit_after_tax_latest_quarter IS NOT NULL)),
                   ROUND(((SUM(CASE WHEN last_result_quarter = (SELECT q FROM dom)
                                          AND sales_latest_quarter IS NOT NULL AND sales_preceding_year_quarter IS NOT NULL
                                     THEN sales_latest_quarter * market_cap END)
                          / NULLIF(SUM(CASE WHEN last_result_quarter = (SELECT q FROM dom)
                                             AND sales_latest_quarter IS NOT NULL AND sales_preceding_year_quarter IS NOT NULL
                                        THEN sales_preceding_year_quarter * market_cap END), 0)) - 1) * 100, 1),
                   ROUND(((SUM(CASE WHEN last_result_quarter = (SELECT q FROM dom)
                                          AND profit_after_tax_latest_quarter IS NOT NULL AND profit_after_tax_preceding_year_quarter IS NOT NULL
                                     THEN profit_after_tax_latest_quarter * market_cap END)
                          / NULLIF(SUM(CASE WHEN last_result_quarter = (SELECT q FROM dom)
                                             AND profit_after_tax_latest_quarter IS NOT NULL AND profit_after_tax_preceding_year_quarter IS NOT NULL
                                        THEN profit_after_tax_preceding_year_quarter * market_cap END), 0)) - 1) * 100, 1)
            FROM m
        """, (raw_names,))
        dominant_q, total, covered, sales_yoy, np_yoy = cur.fetchone()
    return {"quarter": dominant_q, "total": total, "covered": covered,
            "sales_yoy_pct": _fl(sales_yoy), "net_profit_yoy_pct": _fl(np_yoy)}


def _earnings_qoq_estimate(raw_names, anchor_quarter):
    """cc#2235: Earnings Snapshot -- percentage growth, QoQ vs anchor_quarter (the segment's
    dominant last_result_quarter, e.g. Q1FY27), labelled by the quarter being ESTIMATED
    (Q2FY27), not the anchor. NOT YoY: screener_raw carries no year-ago counterpart for the
    estimate (no Q2FY26 actual column exists anywhere in this source) -- QoQ vs the anchor is the
    only available base. That is a data limit, not a design choice, and the caller must say so
    rather than presenting this figure as if it were measured the same way as the Results block's
    YoY headline. Same aggregate-growth formula and mixed-quarter guard as _results_yoy_growth --
    restricted to anchor_quarter so both blocks describe the exact same member population."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE s.last_result_quarter = %s
                                     AND (s.expected_qtr_sales IS NOT NULL OR s.expected_quarterly_net_profit IS NOT NULL)),
                   ROUND(((SUM(CASE WHEN s.last_result_quarter = %s
                                          AND s.expected_qtr_sales IS NOT NULL AND s.sales_latest_quarter IS NOT NULL
                                     THEN s.expected_qtr_sales * g.market_cap END)
                          / NULLIF(SUM(CASE WHEN s.last_result_quarter = %s
                                             AND s.expected_qtr_sales IS NOT NULL AND s.sales_latest_quarter IS NOT NULL
                                        THEN s.sales_latest_quarter * g.market_cap END), 0)) - 1) * 100, 1),
                   ROUND(((SUM(CASE WHEN s.last_result_quarter = %s
                                          AND s.expected_quarterly_net_profit IS NOT NULL AND s.profit_after_tax_latest_quarter IS NOT NULL
                                     THEN s.expected_quarterly_net_profit * g.market_cap END)
                          / NULLIF(SUM(CASE WHEN s.last_result_quarter = %s
                                             AND s.expected_quarterly_net_profit IS NOT NULL AND s.profit_after_tax_latest_quarter IS NOT NULL
                                        THEN s.profit_after_tax_latest_quarter * g.market_cap END), 0)) - 1) * 100, 1)
            FROM gvm_scores g LEFT JOIN screener_raw s ON s.nse_code = g.symbol
            WHERE g.segment = ANY(%s) AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
        """, (anchor_quarter, anchor_quarter, anchor_quarter, anchor_quarter,
              anchor_quarter, anchor_quarter, raw_names))
        total, covered, sales_qoq, np_qoq = cur.fetchone()
    return {"anchor_quarter": anchor_quarter, "estimate_quarter": _next_fy_quarter(anchor_quarter),
            "total": total, "covered": covered,
            "sales_qoq_pct": _fl(sales_qoq), "net_profit_qoq_pct": _fl(np_qoq)}


def _upside_mcap_weighted(raw_names):
    """cc#2233 Block 4: mcap-weighted gvm_scores.upside_raw (the valuation rating's own forward
    potential-upside figure, 83.6% covered). NOT the same measure as sector_rotation()'s existing
    `annual_upside` (screener_agg CTE: a SIMPLE average of (historical_pe-pe)/pe*100, i.e. how far
    current PE sits from the stock's OWN historical PE -- a valuation-compression signal, not a
    growth-based upside estimate). Verified live these are genuinely different quantities, not just
    different aggregation methods -- correlation -0.66 to -0.71 across sampled segments, sign
    flips on more than one (Hospitals - Mid & Small: -18.8% old vs +51.8% new). Per the card's own
    resolution rule ("if derived differently, keep both and label each"): the old field is left
    untouched in sector_rotation()/mobile_sector_list (do_not_touch) and was never rendered
    anywhere on this page before this card, so there is no on-screen collision -- this is the
    first time an annual-upside figure is shown here, and it is this one, clearly labelled."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT ROUND((SUM(CASE WHEN g.upside_raw IS NOT NULL THEN g.upside_raw * g.market_cap END)
                          / NULLIF(SUM(CASE WHEN g.upside_raw IS NOT NULL THEN g.market_cap END), 0))::numeric, 1),
                   COUNT(*) FILTER (WHERE g.upside_raw IS NOT NULL),
                   COUNT(*)
            FROM gvm_scores g
            WHERE g.segment = ANY(%s) AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
        """, (raw_names,))
        upside, covered, total = cur.fetchone()
    return {"upside_mcap_wt": _fl(upside), "covered": covered, "total": total}


@router.get("/api/mobile/sector_app/search")
@_json_safe
def mobile_sector_search(request: Request, q: str = ""):
    """cc#2233 item 1: SEGMENT search (never company/symbol) against the 126 raw segment names in
    sector_ratings at the latest score_date -- case-insensitive, prefix AND substring ('wiring'
    finds 'Auto - Wiring & Electricals'). Prefix matches rank first."""
    g = _guard(request)
    if g:
        return g
    q = (q or "").strip()
    if not q:
        return {"query": q, "results": []}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT segment, stocks_count FROM sector_ratings
            WHERE score_date = (SELECT MAX(score_date) FROM sector_ratings) AND segment ILIKE %s
            ORDER BY (segment ILIKE %s) DESC, segment ASC
            LIMIT 15
        """, (f"%{q}%", f"{q}%"))
        results = [{"segment": r[0], "names": r[1]} for r in cur.fetchall()]
    return {"query": q, "results": results}


@router.get("/api/mobile/sector_app/segment")
@_json_safe
async def mobile_sector_segment(request: Request, name: str = ""):
    g = _guard(request)
    if g:
        return g
    rot = sector_rotation()
    row = None
    for r in (rot.get("all") or []) if isinstance(rot, dict) else []:
        # cc#2233: search suggestions come from the 126 RAW sector_ratings names (item 1), which
        # is not always the same string as a display_segment -- a thin (<5 member) raw segment
        # merges into a family or "Others - Diversified" at display time (cc#827). Matching only
        # display_segment left a search hit on any merged-away raw name resolving to nothing; also
        # matching the absorbed list means every one of the 126 searchable names finds its page.
        if (r.get("display_segment") or r.get("segment")) == name or name in (r.get("absorbed") or []):
            row = r
            break
    if row is None:
        return {"error": "no such segment"}
    disp_name = row.get("display_segment") or row.get("segment")
    # a merged display segment's brief lives under its own raw name when it absorbed nothing; when
    # it absorbed others, the first absorbed raw name carries the brief (cc#827 merge keeps briefs raw)
    brief_name = disp_name if not row.get("absorbed") else (row.get("absorbed") or [disp_name])[0]
    try:
        b = await sector_brief(brief_name)
    except Exception as e:
        b = {"error": str(e)}
    if not isinstance(b, dict) or b.get("error"):
        b = {}
    # members = every raw segment the display row covers (absorbed list, else its own name)
    raw_names = row.get("absorbed") or [disp_name]
    members = []
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT g.symbol, g.company_name, ROUND(g.gvm_score::numeric,2), g.verdict,
                   ROUND(g.g_score::numeric,2), ROUND(g.v_score::numeric,2), ROUND(g.m_score::numeric,2),
                   ROUND(g.market_cap::numeric,0), ROUND(s.pe::numeric,1),
                   (SELECT rp.close FROM raw_prices rp WHERE rp.symbol = g.symbol
                      AND rp.close IS NOT NULL ORDER BY rp.price_date DESC LIMIT 1)
            FROM gvm_scores g LEFT JOIN screener_raw s ON s.nse_code = g.symbol
            WHERE g.segment = ANY(%s) AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
        """, (raw_names,))
        for sym, cn, gvm, vd, gg, vv, mm, mc, pe, px in cur.fetchall():
            members.append({"symbol": sym, "name": cn, "gvm": _fl(gvm), "verdict": vd, "g": _fl(gg), "v": _fl(vv), "m": _fl(mm),
                            "mcap": _fl(mc), "pe": _fl(pe), "price": _fl(px)})   # cc#2233 Block 5: PRICE column added
    members.sort(key=lambda x: -(x["gvm"] or 0))
    # cc#2235: Earnings must be anchored to the SAME dominant quarter Results restricted to --
    # computed here, not independently inside _earnings_qoq_estimate, so the two blocks can never
    # disagree about which quarter they are both keyed off.
    results_yoy = _results_yoy_growth(raw_names)
    return {"segment": disp_name, "score_date": row.get("score_date"),
            # cc#2231: "change" dropped from the scorecard too -- same reason as mobile_sector_list's rows.
            "scorecard": {"gvm": _fl(row.get("gvm")), "g": _fl(row.get("g_score")), "v": _fl(row.get("v_score")), "m": _fl(row.get("m_score")),
                          "verdict": row.get("verdict"), "size": row.get("size_class")},
            "evidence": {"names": int(row.get("stocks_count") or 0), "mcap": _fl(row.get("total_mcap")),
                         "inst": _fl(row.get("inst_change")), "qoq": _fl(row.get("qoq_profit")), "upside": _fl(row.get("annual_upside"))},
            "absorbed": row.get("absorbed") or [],
            "brief": {"what": b.get("what_is_it"), "drivers": b.get("growth_drivers"), "model": b.get("business_model"),
                      "risks": b.get("key_risks"), "application": b.get("application_type"), "generated_at": b.get("generated_at")},
            # cc#2233 items 3-5 / cc#2235: results snapshot (YoY %), earnings snapshot (QoQ % estimate),
            # annual upside -- all mcap-weighted, all state their own member coverage, computed over the
            # SAME raw_names population the holdings table below shows.
            "last_quarter": results_yoy,
            "next_quarter": _earnings_qoq_estimate(raw_names, results_yoy["quarter"]),
            "upside_v2": _upside_mcap_weighted(raw_names),
            "members": {"rows": members, "count": len(members)}}
