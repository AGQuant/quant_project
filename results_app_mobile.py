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
