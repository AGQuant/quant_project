"""
mobile_ext.py — cc#892 breadth screens + cc#893 depth endpoints (Fable direct push,
CHARTER_OVERRIDE_08AUG2026, session_log 17783; program MOBILE_REBUILD_IN_PLACE_V1 17782).

OWN FILE, wired by the preview_endpoints tail shim exactly like mobile_home2 — CC relocates both
include pairs into main.py on its next main.py touch (cc#893 item 3).

What lives here and what deliberately does NOT:
  * /m/screeners, /m/sector, /m/holdings, /m/fpc page routes (templates in mobile/).
  * /api/mobile/holdings — smartgain_holdings rows (the ONLY breadth source with no existing API).
  * /api/mobile/result_analysis — per-symbol plain-words analysis from result_analysis_v2
    (columns verified 08-Aug: symbol, quarter, analysis_text, sections, polished_at).
  * NO screeners endpoint and NO sector endpoint: the templates call the EXISTING web APIs
    (/api/screeners, /api/screeners/{id}, /api/sectors, /api/sector/rotation) — one
    implementation, DISPLAY_PARITY 16202 by construction. Same reason /m/gvm's full report
    calls /api/gvm/company/{symbol}.
  * smartgain_holdings.updated_at is TIMESTAMPTZ -> AT TIME ZONE in SQL (cc#887 doctrine).
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mobile_endpoints import (
    rail_state, _conn, _rows, _ist_now, _guard, _json_safe, _page,
)

log = logging.getLogger("scorr.mobile.ext")
router = APIRouter()


@router.get("/api/mobile/holdings")
@_json_safe
def mobile_holdings(request: Request):
    """My Portfolio, full screen — every SmartGain holding with entry, LTP and MTM."""
    g = _guard(request)
    if g:
        return g
    now = _ist_now()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, direction, qty, entry_price, ltp, mtm,
                   (updated_at AT TIME ZONE 'Asia/Kolkata') AS updated_at
            FROM smartgain_holdings
            ORDER BY ABS(COALESCE(mtm, 0)) DESC
        """)
        rows = _rows(cur)

    def f(v):
        return float(v) if v is not None else None

    newest = max((r["updated_at"] for r in rows if r["updated_at"]), default=None)
    out = []
    for r in rows:
        entry, ltp, qty = f(r["entry_price"]), f(r["ltp"]), f(r["qty"])
        out.append({
            "symbol": r["symbol"],
            "direction": (r["direction"] or "").upper(),
            "qty": qty, "entry": entry, "ltp": ltp,
            "mtm": f(r["mtm"]),
            "value": round(qty * ltp, 2) if qty is not None and ltp is not None else None,
            "ret_pct": (round((ltp - entry) / entry * 100.0
                              * (-1.0 if (r["direction"] or "").upper().startswith("S") else 1.0), 2)
                        if entry and ltp is not None else None),
        })
    total_mtm = sum((x["mtm"] or 0.0) for x in out) if out else None
    total_val = sum((x["value"] or 0.0) for x in out) if out else None
    return {
        "empty": not out,
        "rows": out, "count": len(out),
        "total_mtm": round(total_mtm, 2) if total_mtm is not None else None,
        "total_value": round(total_val, 2) if total_val is not None else None,
        "message": "No positions open." if not out else None,
        "rail": rail_state(newest, 1440, now, now.weekday() < 5),
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/api/mobile/result_analysis")
@_json_safe
def mobile_result_analysis(request: Request, symbol: str = ""):
    """cc#893 item 2 — the plain-words result analysis the web Results Corner shows, per symbol.
    Newest quarter first. Absent symbol answers honestly with found:false, never a stub text."""
    g = _guard(request)
    if g:
        return g
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "symbol required"}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, quarter, analysis_text,
                   (polished_at AT TIME ZONE 'Asia/Kolkata') AS polished_at
            FROM result_analysis_v2
            WHERE symbol = %s
            ORDER BY polished_at DESC NULLS LAST
            LIMIT 1
        """, (sym,))
        rows = _rows(cur)
    if not rows:
        return {"symbol": sym, "found": False}
    r = rows[0]
    return {
        "symbol": r["symbol"], "found": True,
        "quarter": r["quarter"],
        "text": r["analysis_text"],
        "polished_at": r["polished_at"].strftime("%d %b %H:%M") if r["polished_at"] else None,
    }


@router.get("/api/mobile/result_analysis_index")
@_json_safe
def mobile_result_analysis_index(request: Request):
    """Which symbols HAVE analysis — so the Results screen only shows a tap affordance where
    tapping will actually produce something (no dead affordances, cc#893 verify c)."""
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM result_analysis_v2")
        rows = _rows(cur)
    return {"symbols": [r["symbol"] for r in rows], "count": len(rows)}


@router.get("/m/screeners", response_class=HTMLResponse)
def m_screeners():
    return _page("screeners")


@router.get("/m/sector", response_class=HTMLResponse)
def m_sector():
    return _page("sector")


@router.get("/m/holdings", response_class=HTMLResponse)
def m_holdings():
    return _page("holdings")


@router.get("/m/fpc", response_class=HTMLResponse)
def m_fpc():
    return _page("fpc")
