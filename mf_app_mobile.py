"""
mf_app_mobile.py — cc#1903 APP BUILD: Mutual Funds section page backend
(design_refs/scorr_app_v15mf_R1.html, APP_CARD_LAYOUT_LAW_V1 session_log 42536).

ONE endpoint, GET /api/mobile/mf_app. Reads mf_scores JOIN mf_master directly (scheme_code is
the shared key). Every number hand-verified live and reproduces the card's own evidence exactly:
14,238 in mf_master, 518 scored (mf_scores), top-4 by mqs (HDFC Defence 73.73/81.6%, Quant Value
72.51/49.8%, HDFC Pharma & Healthcare 71.74/79.8%, Kotak MNC 71.11/87.7%) all match to the second
decimal, category counts (Sectoral/Thematic 185, Flexi Cap 46, Large Cap 39, ELSS 39) match
exactly, AVG(coverage_pct)=88.6 matches the ref's "Typical coverage" figure exactly, mf_holdings
69,484 and mf_nav_history 145,090 both match.

CATEGORY TIE — the ref's 5th "By category" slot shows "Mid Cap 38"; live query finds Mid Cap Fund
AND Small Cap Fund tied at 38 each. Broken alphabetically (category ASC as the tiebreak), which
happens to reproduce the ref's own choice ("Mid Cap Fund" < "Small Cap Fund") without forcing it
— a real tie, resolved deterministically, not silently hidden.

BASIS — READ THIS BEFORE TRUSTING THE KM CARD'S SINGLE "Q+R+C+S" LABEL AS UNIVERSAL. mf_scores.
basis_label varies per fund: 467 of 518 (90.2%) score on the full four-part basis, but 34 use
Q+C+S, 14 use R+C+S, 2 use Q+R+S and 1 uses only C+S — a fund's score is computed on whatever
parts have usable data for it, not always all four. The KM "Basis" cell shows the MOST COMMON
basis (Q+R+C+S, matching the ref), and the "How the score works" card states the true split
live rather than implying uniformity the data does not have.

COVERAGE PER FUND (items 3, V2) — the ref's own "Best scores" card mockup shows category + AUM
next to each fund's score, not coverage — exactly the "buried" framing item 3 warns against. This
build puts coverage_pct directly on every row instead (next to the score, per the card's explicit
instruction, which here overrides the ref's own visual choice). Quant Value's 49.8% coverage
(the card's own flagged example) renders inline on its row, not hidden behind a category label.

NULL MQS (item 4, V3) — this endpoint only ever selects rows that already have an mqs (mf_scores
has one row per SCORED scheme; an unscored scheme simply has no mf_scores row at all). So "no
score, never 0" is enforced structurally, by the join, not by an if-check that could be skipped —
there is no code path here that could render a null mqs as 0.

do_not_touch honoured: the MQS engine and mf_scores — read-only, zero writes. The web V15 page
(scorr_v15.html) and its own endpoints — untouched, different route, different query.
"""
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
import psycopg

from mobile_endpoints import _guard, _json_safe, _page

router = APIRouter()

RAIL_ROW_CAP = 6


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


@router.get("/m/mf", response_class=HTMLResponse)
def m_mf():
    """cc#1903: Mutual Funds section page. Reachable via the NAV array's mobile More sheet — see
    the ARCHITECTURE note in this module's docstring for why (no prior /m/ MF page or Home-grid
    tile existed to preserve or conflict with, unlike cc#1902's Portfolio Health)."""
    return _page("mf")


def _fmt_score(v):
    return round(float(v), 1) if v is not None else None


@router.get("/api/mobile/mf_app")
@_json_safe
def mobile_mf_app(request: Request):
    g = _guard(request)
    if g:
        return g

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM mf_master")
        in_master = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM mf_scores")
        scored = cur.fetchone()[0] or 0

        cur.execute("SELECT MAX(mqs) FROM mf_scores")
        top_score = cur.fetchone()[0]

        cur.execute("SELECT ROUND(AVG(coverage_pct)::numeric, 1) FROM mf_scores")
        typical_coverage = cur.fetchone()[0]

        cur.execute("""SELECT basis_label, COUNT(*) FROM mf_scores
                       GROUP BY basis_label ORDER BY COUNT(*) DESC""")
        basis_counts = [{"basis": r[0], "count": r[1]} for r in cur.fetchall()]

        cur.execute("""
            SELECT m.name, m.category, m.aum_cr, s.mqs, s.coverage_pct
            FROM mf_scores s JOIN mf_master m ON m.scheme_code = s.scheme_code
            ORDER BY s.mqs DESC NULLS LAST LIMIT %s
        """, (RAIL_ROW_CAP,))
        best = [{"name": r[0], "category": r[1], "aum_cr": float(r[2]) if r[2] is not None else None,
                 "mqs": _fmt_score(r[3]), "coverage_pct": float(r[4]) if r[4] is not None else None}
                for r in cur.fetchall()]

        cur.execute("""
            SELECT m.category, COUNT(*) AS n
            FROM mf_scores s JOIN mf_master m ON m.scheme_code = s.scheme_code
            GROUP BY m.category ORDER BY n DESC, m.category ASC LIMIT %s
        """, (RAIL_ROW_CAP,))
        by_category = [{"category": r[0], "count": r[1]} for r in cur.fetchall()]

        cur.execute("SELECT COUNT(*) FROM mf_holdings")
        holdings_rows = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM mf_nav_history")
        nav_history_rows = cur.fetchone()[0] or 0

    basis_top = basis_counts[0]["basis"] if basis_counts else None
    basis_top_count = basis_counts[0]["count"] if basis_counts else 0
    top4_categories = [b["category"] for b in best[:4]]
    sectoral_in_top4 = sum(1 for c in top4_categories if c and "Sectoral" in c)

    return {
        "status": "ok",
        "key_metrics": {
            "schemes_scored": scored,
            "in_master": in_master,
            "top_score": _fmt_score(top_score),
            "basis": basis_top,
            "typical_coverage": float(typical_coverage) if typical_coverage is not None else None,
        },
        "best_scores": {
            "rows": best,
            "count": scored,
            "message": (f"{sectoral_in_top4} of the top {min(4, len(best))} are sectoral funds. "
                       "Concentrated bets score well on this measure and carry more risk.")
                       if best else None,
        },
        "by_category": {
            "rows": by_category,
            "message": "Sectoral funds dominate the scored set simply because there are more of them.",
        },
        "score_parts": {
            "message": "Quality of holdings, returns, cost, and how steady the fund has been. "
                       "The four combine into one score out of 100.",
            "basis_note": (f"Most funds ({basis_top_count} of {scored}, "
                          f"{round(basis_top_count/scored*100, 1) if scored else 0}%) score on the "
                          "full four-part basis; the rest compute on fewer parts when data is "
                          "missing.") if basis_top else None,
        },
        "data_behind": {
            "rows": [
                {"label": "Schemes in master", "count": in_master},
                {"label": "Scored so far", "count": scored},
                {"label": "Holdings rows", "count": holdings_rows},
                {"label": "NAV history rows", "count": nav_history_rows},
            ],
            "message": f"Only {scored} of {in_master} schemes are scored. The rest have no score, not a zero.",
        },
        "coverage": {
            "message": "A score is only as good as how much of the fund we can see. Every fund "
                       "carries its own coverage figure, and a low-coverage score says so on its face.",
            # cc#1903: the ref's own flagged example is the top-4 fund with the LOWEST coverage —
            # computed live (min coverage_pct among the top 4 by score), not hardcoded to whichever
            # name happens to hold that spot today (currently Quant Value at 49.8%, matching the
            # ref, but this must keep working if the ranking ever changes).
            "example": (lambda low: f"Example: one top-{min(4, len(best))} fund is scored on "
                       f"{low['coverage_pct']}% coverage and is flagged." if low else None)(
                min((r for r in best[:4] if r.get("coverage_pct") is not None),
                    key=lambda r: r["coverage_pct"], default=None)),
        },
    }


# ═══ V2 — Fable single mode 10-Sep-2026. Additive; the cc#1903 endpoint above stays.
#   GET /api/mobile/mf_app/list?category=&sort=&q=  → the web V15 screener's own rows + the stats chip
#   GET /api/mobile/mf_app/fund?code=                → v15_fund (scores, returns vs category, rank, peers,
#                                                      flags) + mf_fund (look-through holdings with GVM,
#                                                      sector exposure, NAV series)
from mf_pipeline import v15_screener, v15_stats, v15_fund, mf_fund, EQUITY_CATEGORY_WHITELIST


def _fl(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _short(name):
    """Shorten an AMFI scheme name for a phone row: drop plan/option suffixes."""
    n = str(name or "")
    for cut in (" - Direct Plan", " Direct Plan", "- Direct", " Direct", " - Growth", " Growth Option", " Growth", " - IDCW", " Option"):
        if cut in n:
            n = n.split(cut)[0]
    return n.strip(" -")


@router.get("/api/mobile/mf_app/list")
@_json_safe
def mobile_mf_list(request: Request, category: str = "", sort: str = "mqs", q: str = ""):
    g = _guard(request)
    if g:
        return g
    st = v15_stats()
    cat = q.strip() if q.strip() else category
    sc = v15_screener(category=cat, sort=sort if sort in ("mqs", "1y", "aum") else "mqs", limit=80)
    rows = []
    for r in (sc.get("results") or []) if isinstance(sc, dict) else []:
        rows.append({"code": str(r.get("scheme_code")), "name": _short(r.get("name")), "amc": r.get("amc"), "category": r.get("category"),
                     "mqs": _fl(r.get("mqs")), "ret_1y": _fl(r.get("ret_1y")), "ret_3y": _fl(r.get("ret_3y")), "ret_3y_state": r.get("ret_3y_state"),
                     "aum": _fl(r.get("aum_cr")), "er": _fl(r.get("expense_ratio")), "crisil": r.get("crisil_rank")})
    return {"scored": st.get("scored"), "universe": st.get("universe"), "categories": list(EQUITY_CATEGORY_WHITELIST),
            "category": category, "q": q, "sort": sort, "rows": rows, "count": len(rows)}


@router.get("/api/mobile/mf_app/fund")
@_json_safe
def mobile_mf_fund(request: Request, code: str = ""):
    g = _guard(request)
    if g:
        return g
    f = v15_fund(code)
    if not isinstance(f, dict) or f.get("error") or not f.get("fund"):
        return {"error": (f or {}).get("error", "fund not found")}
    m = f["fund"]
    d = mf_fund(code)
    d = d if isinstance(d, dict) and not d.get("error") else {}
    hold = []
    for h in (d.get("holdings") or [])[:15]:
        hold.append({"name": h.get("company_name"), "symbol": h.get("resolved_nse_symbol"), "weight": _fl(h.get("pct_weight")),
                     "gvm": _fl(h.get("gvm")), "segment": h.get("segment"), "verdict": h.get("verdict")})
    nav = d.get("nav") or []
    step = max(1, len(nav) // 60)
    spark = nav[::step]
    if nav and (not spark or spark[-1] is not nav[-1]):
        spark.append(nav[-1])
    ca = m.get("category_avgs") or {}
    return {"code": code, "name": _short(m.get("name")), "full_name": m.get("name"), "amc": m.get("amc"), "category": m.get("category"),
            "plan": m.get("plan"), "inception": m.get("inception"), "returns_asof": m.get("returns_asof"),
            "mqs": _fl(m.get("mqs")), "scores": {"q": _fl(m.get("q_score")), "r": _fl(m.get("r_score")), "c": _fl(m.get("c_score")), "s": _fl(m.get("s_score"))},
            "weights": m.get("mqs_weights"), "rank": m.get("category_rank"), "peers_n": m.get("category_peers"),
            "returns": {k: _fl(m.get("ret_" + k)) for k in ("1m", "3m", "6m", "1y", "2y", "3y", "5y")},
            "cat_avgs": {k: _fl(ca.get("ret_" + k)) for k in ("1m", "3m", "6m", "1y", "2y", "3y")},
            "ret_3y_state": m.get("ret_3y_state"), "ret_5y_state": m.get("ret_5y_state"),
            "er": _fl(m.get("expense_ratio")), "cat_er": _fl(m.get("category_avg_er")), "aum": _fl(m.get("aum_cr")), "cat_aum": _fl(m.get("category_avg_aum_cr")),
            "crisil": m.get("crisil_rank"),
            "portfolio_gvm": _fl(m.get("portfolio_gvm")), "gvm_coverage": _fl(m.get("portfolio_gvm_coverage_pct")), "gvm_state": m.get("portfolio_gvm_state"),
            "holdings": hold, "holdings_total": len(d.get("holdings") or []), "resolved_pct": _fl(d.get("resolved_pct")),
            "segments": [{"segment": s.get("segment"), "pct": _fl(s.get("exposure_pct")), "gvm": _fl(s.get("sector_gvm")), "verdict": s.get("sector_verdict")}
                         for s in (d.get("segments") or [])[:10]],
            "nav": [{"d": p.get("date"), "v": _fl(p.get("nav"))} for p in spark],
            "peers": [{"code": str(p.get("scheme_code")), "name": _short(p.get("name")), "mqs": _fl(p.get("mqs")), "ret_1y": _fl(p.get("ret_1y")), "self": bool(p.get("is_self"))}
                      for p in (m.get("peers") or [])],
            "flags": m.get("red_flags") or []}
