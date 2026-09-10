"""
results_app_mobile.py — cc#1901 APP BUILD: Results section page backend
(design_refs/scorr_app_results_R1.html, APP_CARD_LAYOUT_LAW_V1 session_log 42536).

ONE endpoint, GET /api/mobile/results_app. Reads result_analysis_v2 (written-up results),
result_coverage_gaps and result_analysis_skipped (what's missing, kept as TWO separate figures
per the card's own item 3 — a gap is not-yet-done, a skip is deliberate, and they are never
summed into one number) and earnings_calendar (upcoming). Every figure hand-verified live and
reproduces the card's own evidence exactly: 774 total (773 Q1FY27 + 1 Q4FY26), MAX(polished_at)
29-Aug-2026, 309 coverage gaps, 101 skipped, 2,577 calendar rows.

STALENESS — computed live from MAX(polished_at), never hardcoded (item 2). At the time of this
build that is still 29-Aug-2026, ten days behind the score/build date — the page says so plainly
rather than looking current, exactly as the card requires.

LATEST WRITTEN — the ref shows 4 names (AXISCADES/CAMPUS/JKLAKSHMI/NEPHROPLUS) at the 29-Aug
timestamp; live query finds SEVEN tied at that exact polished_at (those four plus RCF, SFL,
SPARC) — the ref's four are a partial illustrative sample, not the full tied set. This build
shows all up to the layout law's 6-row cap ("+1 more"), not a copy of the ref's shorter list.

CALENDAR — the existing /m/results page (mobile/results.html, cc#1090/1645) already IS a fuller,
forward-looking earnings calendar (next-10-days view with tap-to-read polished analysis per
company). Rather than duplicate or replace it, "See the calendar ›" on the new Calendar card
scrolls down to that existing, untouched view — same additive pattern as cc#1900's Sector Intel
build, for the same reason: real, different-in-kind functionality already exists, and nothing in
this card's scope asks for it to be replaced.

do_not_touch honoured: result_analysis_v2 content and the polish pipeline — read-only, zero
writes. NEWS_POLISH_CANON_V1 rules — untouched, this file only reads what the pipeline already
wrote. The existing /m/results calendar view (mobile_endpoints.py, /api/mobile/results,
/api/mobile/result_analysis, /api/mobile/result_analysis_index) — untouched, different endpoints.
"""
import os

from fastapi import APIRouter, Request
import psycopg

from mobile_endpoints import _guard, _json_safe

router = APIRouter()

RAIL_ROW_CAP = 6


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


@router.get("/api/mobile/results_app")
@_json_safe
def mobile_results_app(request: Request):
    g = _guard(request)
    if g:
        return g

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT quarter, COUNT(*) FROM result_analysis_v2 GROUP BY quarter ORDER BY COUNT(*) DESC")
        by_quarter = [{"quarter": r[0], "count": r[1]} for r in cur.fetchall()]
        total_written = sum(r["count"] for r in by_quarter)

        cur.execute("SELECT MAX(polished_at) FROM result_analysis_v2")
        latest_polished = cur.fetchone()[0]

        latest_rows = []
        if latest_polished is not None:
            cur.execute("""SELECT symbol, quarter FROM result_analysis_v2
                           WHERE polished_at=%s ORDER BY symbol""", (latest_polished,))
            latest_rows = [{"symbol": r[0], "quarter": r[1]} for r in cur.fetchall()]

        cur.execute("SELECT COUNT(*) FROM result_coverage_gaps")
        gaps = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM result_analysis_skipped")
        skipped = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM earnings_calendar")
        calendar_rows = cur.fetchone()[0] or 0

    q1fy27 = next((r["count"] for r in by_quarter if r["quarter"] == "Q1FY27"), 0)

    return {
        "status": "ok",
        "latest_polished_at": latest_polished.isoformat() if latest_polished else None,
        "key_metrics": {
            "results_written": total_written,
            "q1fy27": q1fy27,
            "not_yet_written": gaps,
            "skipped": skipped,
            "last_written": latest_polished.date().isoformat() if latest_polished else None,
        },
        "written_up": {
            "rows": [{"label": r["quarter"],
                      "note": "current season" if r["quarter"] == "Q1FY27" else "carried over",
                      "count": r["count"]} for r in by_quarter],
            "total": total_written,
            "message": "One quarter carries essentially the whole set. Q1FY27 is the live season."
                       if q1fy27 else None,
        },
        "latest_written": {
            "rows": latest_rows[:RAIL_ROW_CAP],
            "count": len(latest_rows),
            "more": max(0, len(latest_rows) - RAIL_ROW_CAP),
            "as_of": latest_polished.date().isoformat() if latest_polished else None,
            "message": (f"Nothing new written since {latest_polished.date().isoformat()}. "
                       "The page says so rather than looking current.") if latest_polished else None,
        },
        "coverage_gaps": {
            "rows": [
                {"label": "Not yet written", "note": "companies with results", "count": gaps},
                {"label": "Skipped on purpose", "note": "no usable data", "count": skipped},
            ],
            "message": "Skipped means the filing had nothing worth writing about, not that we "
                       "failed to fetch it.",
        },
        "why_skipped": {
            "count": skipped,
            "message": "If there is no investor call, no presentation and nothing unusual in "
                       "the numbers, we skip it rather than write filler.",
        },
        "calendar": {
            "rows": [
                {"label": "Earnings calendar", "note": "dates on file", "count": calendar_rows},
                {"label": "Season", "note": "Q1FY27", "value": "live"},
            ],
            "count": calendar_rows,
        },
    }


# ═══ V2 — Fable single mode 10-Sep-2026 (RESULTS_APP_R2, founder: read the web APIs, build intuitively).
# Additive; the cc#1901 endpoint above stays. All numbers come from the web Result Corner's own
# result_corner_v2() and the Results page's own result_analysis_v2 / _list — nothing re-derived.
#   GET /api/mobile/results_app/season           → season summary, PAT split, movers, sector ladder,
#                                                   written-up list, next-10-day calendar
#   GET /api/mobile/results_app/companies        → every reporter this season (sortable table feed)
#   GET /api/mobile/results_app/analysis?symbol= → one written analysis, full text + sections
from datetime import date, timedelta
from result_corner import result_corner_v2
from results_endpoints import result_analysis_v2, result_analysis_v2_list


def _fl(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _rc():
    rc = result_corner_v2()
    if not isinstance(rc, dict):
        try:
            import json as _j
            rc = _j.loads(rc.body)
        except Exception:
            rc = {}
    return rc


def _written_set(cur, quarter):
    cur.execute("SELECT UPPER(symbol) FROM result_analysis_v2 WHERE quarter=%s", (quarter,))
    return {r[0] for r in cur.fetchall()}


def _co_row(c, written):
    return {"symbol": c.get("symbol"), "company": c.get("company"), "segment": c.get("segment"), "tier": c.get("tier"),
            "gvm": _fl(c.get("gvm")), "verdict": c.get("verdict"), "reported": c.get("reported_date"),
            "sales_yoy": _fl(c.get("sales_yoy")), "pat_yoy": _fl(c.get("pat_yoy")),
            "sales_qoq": _fl(c.get("sales_qoq")), "pat_qoq": _fl(c.get("pat_qoq")),
            "sales": _fl(c.get("sales")), "pat": _fl(c.get("pat")), "opm": _fl(c.get("opm")), "opm_ly": _fl(c.get("opm_ly")),
            "pe": _fl(c.get("pe")), "seg_pe": _fl(c.get("segment_pe")), "basis": c.get("basis"),
            "written": (c.get("symbol") or "").upper() in written}


@router.get("/api/mobile/results_app/season")
@_json_safe
def mobile_results_season(request: Request):
    g = _guard(request)
    if g:
        return g
    rc = _rc()
    season = rc.get("season") or {}
    summ = rc.get("summary") or {}
    quarter = season.get("quarter")
    with _conn() as conn, conn.cursor() as cur:
        written = _written_set(cur, quarter) if quarter else set()
        cur.execute("""SELECT ticker, company_name, ex_date, status FROM earnings_calendar
                       WHERE ex_date BETWEEN CURRENT_DATE AND CURRENT_DATE + 10 AND verified <> 'false'
                       ORDER BY ex_date, ticker LIMIT 40""")
        upcoming = [{"symbol": r[0], "company": r[1], "date": str(r[2]), "status": r[3]} for r in cur.fetchall()]
        cur.execute("SELECT MAX(polished_at) FROM result_analysis_v2")
        last_written = cur.fetchone()[0]
    companies = [_co_row(c, written) for c in (rc.get("companies") or [])]
    reported = [c for c in companies if c["pat_yoy"] is not None or c["sales_yoy"] is not None]
    movers_up = sorted([c for c in reported if c["pat_yoy"] is not None and c["pat_yoy"] > 0], key=lambda c: -c["pat_yoy"])[:6]
    movers_dn = sorted([c for c in reported if c["pat_yoy"] is not None and c["pat_yoy"] < 0], key=lambda c: c["pat_yoy"])[:6]
    sectors = []
    for s in rc.get("sectors") or []:
        sectors.append({"sector": s.get("sector"), "reported": s.get("reported"), "total": s.get("total"),
                        "sales_yoy": _fl(s.get("sales_yoy")), "pat_yoy": _fl(s.get("pat_yoy")),
                        "pct_positive": s.get("pct_positive"), "gvm": _fl(s.get("gvm")), "verdict": s.get("gvm_verdict"),
                        "tiny": bool(s.get("tiny_base")), "avg_mcap": _fl(s.get("avg_mcap")), "n_used": s.get("n_used")})
    wl = result_analysis_v2_list(limit=12, quarter=quarter or "")
    written_rows = [{"symbol": r.get("symbol"), "company": r.get("company"), "quarter": r.get("quarter"),
                     "polished_at": r.get("polished_at"), "teaser": r.get("teaser"), "result_date": r.get("result_date")}
                    for r in (wl.get("results") or [])] if isinstance(wl, dict) else []
    return {"season": {"quarter": quarter, "quarter_end": season.get("quarter_end")},
            "summary": {"reported": summ.get("reported"), "total": summ.get("total"), "pct": summ.get("pct"),
                        "growing": summ.get("pat_growing"), "flat": summ.get("pat_flat"), "declining": summ.get("pat_declining"),
                        "beats_sector": summ.get("beats_sector"),
                        "median_sales_yoy": _fl(summ.get("median_sales_yoy")), "median_pat_yoy": _fl(summ.get("median_pat_yoy")),
                        "pat_n": summ.get("pat_n_detailed"), "tiers": summ.get("tiers"), "basis": summ.get("basis_split")},
            "movers": {"up": movers_up, "down": movers_dn},
            "sectors": sectors,
            "written": {"rows": written_rows, "total": (wl.get("total_polished") if isinstance(wl, dict) else None),
                        "last": last_written.isoformat() if last_written else None},
            "upcoming": upcoming,
            "note": "Season numbers cover only companies that have filed this quarter. A dash means not filed yet, never zero."}


@router.get("/api/mobile/results_app/companies")
@_json_safe
def mobile_results_companies(request: Request):
    g = _guard(request)
    if g:
        return g
    rc = _rc()
    quarter = (rc.get("season") or {}).get("quarter")
    with _conn() as conn, conn.cursor() as cur:
        written = _written_set(cur, quarter) if quarter else set()
    rows = [_co_row(c, written) for c in (rc.get("companies") or [])]
    return {"quarter": quarter, "rows": rows, "count": len(rows)}


@router.get("/api/mobile/results_app/analysis")
@_json_safe
def mobile_results_analysis(request: Request, symbol: str = ""):
    g = _guard(request)
    if g:
        return g
    a = result_analysis_v2(symbol)
    if not isinstance(a, dict):
        return {"error": "analysis unavailable"}
    rc = _rc()
    row = next((c for c in (rc.get("companies") or []) if (c.get("symbol") or "").upper() == symbol.upper()), None)
    return {"symbol": symbol.upper(), "has_analysis": bool(a.get("has_analysis")), "quarter": a.get("quarter"),
            "analysis": a.get("analysis"), "sections": a.get("sections"), "polished_at": a.get("polished_at"),
            "basis": a.get("basis"), "numbers": _co_row(row, {symbol.upper()} if a.get("has_analysis") else set()) if row else None}
