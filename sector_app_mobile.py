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
