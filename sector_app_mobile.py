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
                     "change": _fl(r.get("gvm_change")), "names": int(r.get("stocks_count") or 0),
                     "mcap": _fl(r.get("total_mcap")), "size": r.get("size_class"), "verdict": r.get("verdict"),
                     "inst": _fl(r.get("inst_change")), "qoq": _fl(r.get("qoq_profit")), "upside": _fl(r.get("annual_upside")),
                     "top": [{"symbol": p.get("symbol"), "gvm": _fl(p.get("gvm")), "day": _fl(p.get("day_ret"))}
                             for p in (r.get("top_stocks") or [])[:2]]})
    verdicts = {}
    for r in rows:
        verdicts[r["verdict"] or "—"] = verdicts.get(r["verdict"] or "—", 0) + 1
    th = sector_themes()
    themes = []
    for t in (th.get("themes") or []) if isinstance(th, dict) else []:
        themes.append({"rank": t.get("rank"), "name": t.get("theme_name"), "tagline": t.get("tagline"),
                       "segments": t.get("related_segments") or [],
                       "top": [{"symbol": c.get("symbol"), "gvm": _fl(c.get("gvm_score")), "segment": c.get("segment")}
                               for c in (t.get("companies") or [])[:3]]})
    return {"score_date": rot.get("score_date"), "segments": len(rows), "raw_segments": rot.get("raw_segments"),
            "verdicts": verdicts, "rows": rows, "themes": themes,
            "note": "Mcap-weighted GVM per segment — one big weak name pulls its whole segment down. Change = move since the first scored day on record."}


@router.get("/api/mobile/sector_app/segment")
@_json_safe
async def mobile_sector_segment(request: Request, name: str = ""):
    g = _guard(request)
    if g:
        return g
    rot = sector_rotation()
    row = None
    for r in (rot.get("all") or []) if isinstance(rot, dict) else []:
        if (r.get("display_segment") or r.get("segment")) == name:
            row = r
            break
    if row is None:
        return {"error": "no such segment"}
    # a merged display segment's brief lives under its own raw name when it absorbed nothing; when
    # it absorbed others, the first absorbed raw name carries the brief (cc#827 merge keeps briefs raw)
    brief_name = name if not row.get("absorbed") else (row.get("absorbed") or [name])[0]
    try:
        b = await sector_brief(brief_name)
    except Exception as e:
        b = {"error": str(e)}
    if not isinstance(b, dict) or b.get("error"):
        b = {}
    # members = every raw segment the display row covers (absorbed list, else its own name)
    raw_names = row.get("absorbed") or [name]
    members = []
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT g.symbol, g.company_name, ROUND(g.gvm_score::numeric,2), g.verdict,
                   ROUND(g.g_score::numeric,2), ROUND(g.v_score::numeric,2), ROUND(g.m_score::numeric,2),
                   ROUND(g.market_cap::numeric,0), ROUND(s.pe::numeric,1)
            FROM gvm_scores g LEFT JOIN screener_raw s ON s.nse_code = g.symbol
            WHERE g.segment = ANY(%s) AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
        """, (raw_names,))
        for sym, cn, gvm, vd, gg, vv, mm, mc, pe in cur.fetchall():
            members.append({"symbol": sym, "name": cn, "gvm": _fl(gvm), "verdict": vd, "g": _fl(gg), "v": _fl(vv), "m": _fl(mm),
                            "mcap": _fl(mc), "pe": _fl(pe)})
    members.sort(key=lambda x: -(x["gvm"] or 0))
    return {"segment": name, "score_date": row.get("score_date"),
            "scorecard": {"gvm": _fl(row.get("gvm")), "g": _fl(row.get("g_score")), "v": _fl(row.get("v_score")), "m": _fl(row.get("m_score")),
                          "verdict": row.get("verdict"), "change": _fl(row.get("gvm_change")), "size": row.get("size_class")},
            "evidence": {"names": int(row.get("stocks_count") or 0), "mcap": _fl(row.get("total_mcap")),
                         "inst": _fl(row.get("inst_change")), "qoq": _fl(row.get("qoq_profit")), "upside": _fl(row.get("annual_upside"))},
            "absorbed": row.get("absorbed") or [],
            "brief": {"what": b.get("what_is_it"), "drivers": b.get("growth_drivers"), "model": b.get("business_model"),
                      "risks": b.get("key_risks"), "application": b.get("application_type"), "generated_at": b.get("generated_at")},
            "members": {"rows": members, "count": len(members)}}
