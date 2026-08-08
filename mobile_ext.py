"""
mobile_ext.py — cc#892 breadth screens + cc#893/894 depth endpoints (Fable direct push,
CHARTER_OVERRIDE_08AUG2026, session_log 17783; program MOBILE_REBUILD_IN_PLACE_V1 17782).

OWN FILE, wired by the preview_endpoints tail shim exactly like mobile_home2 — the shim moves to
main.py in the FINAL maintenance pass (founder 08-Aug: Fable does it last; CC stood down).

What lives here and what deliberately does NOT:
  * /m/screeners, /m/sector, /m/holdings, /m/fpc page routes (templates in mobile/).
  * /api/mobile/holdings — smartgain_holdings rows (the ONLY breadth source with no existing API).
  * /api/mobile/result_analysis(+_index) — plain-words analysis from result_analysis_v2.
  * /api/mobile/trades — cc#894 items 5+8, PARITY-FIXED 08-Aug (founder caught all-time vs web
    fresh-book drift): defaults to the WEB MASTER DASHBOARD scope — entry_ts >=
    app_config.v8_paper_rebuild_cutover_ts (cc#504/cc#510 era doctrine), basket 's1_reclaim_obs'
    excluded — with a summary block computed the web way: realised NET of Rs.500/closed-trade
    brokerage, W/L = clean result 'TARGET' vs 'SL'. Verified vs DB: 33 trades, gross 366,112,
    net 349,612, 13/5 — identical to the web cards. ?era=all shows the full legacy book, same as
    the web's own daylog escape hatch. entry_ts/exit_ts naive IST — read raw, never converted.
  * NO screeners/sector endpoints: templates call the EXISTING web APIs (16202 by construction).
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mobile_endpoints import (
    rail_state, basket_label, _conn, _rows, _ist_now, _guard, _json_safe, _page,
)

log = logging.getLogger("scorr.mobile.ext")
router = APIRouter()

BROKERAGE_PER_TRADE = 500       # web daylog doctrine: Rs.500 per closed trade


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


@router.get("/api/mobile/trades")
@_json_safe
def mobile_trades(request: Request, limit: int = 20, days: int = 30, era: str = "fresh"):
    """cc#894 items 5+8 — the ledger, WEB-PARITY scope (see module header):
    trades: last-N closed trades; day_log: day-by-day P&L; summary: the Master Dashboard cards
    (trades, gross, brokerage, net realised, TARGET/SL wins/losses). era=fresh (default) matches
    the web book; era=all is the full legacy history, like the web's own escape hatch."""
    g = _guard(request)
    if g:
        return g
    limit = max(1, min(limit, 100))
    days = max(1, min(days, 120))
    now = _ist_now()
    with _conn() as conn, conn.cursor() as cur:
        cutover = None
        if era != "all":
            cur.execute("SELECT value FROM app_config WHERE key='v8_paper_rebuild_cutover_ts'")
            _row = cur.fetchone()
            cutover = _row[0] if _row and _row[0] else None
        scope = {"cut": cutover}
        cur.execute("""
            SELECT symbol, side, basket, entry_price, exit_price, qty, pnl, return_pct,
                   result, entry_ts, exit_ts
            FROM v8_paper_trades
            WHERE (%(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp)
              AND basket IS DISTINCT FROM 's1_reclaim_obs'
            ORDER BY exit_ts DESC NULLS LAST, id DESC
            LIMIT """ + str(limit), scope)
        tr = _rows(cur)
        cur.execute("""
            SELECT exit_ts::date AS d, COUNT(*) AS n, SUM(pnl) AS pnl,
                   COUNT(*) FILTER (WHERE pnl > 0) AS wins,
                   COUNT(*) FILTER (WHERE pnl < 0) AS losses
            FROM v8_paper_trades
            WHERE exit_ts >= (NOW() AT TIME ZONE 'Asia/Kolkata')::date - %(days)s
              AND (%(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp)
              AND basket IS DISTINCT FROM 's1_reclaim_obs'
            GROUP BY exit_ts::date
            ORDER BY d DESC
        """, {"cut": cutover, "days": days})
        dl = _rows(cur)
        cur.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(pnl), 0) AS gross,
                   COUNT(*) FILTER (WHERE result = 'TARGET') AS wins,
                   COUNT(*) FILTER (WHERE result = 'SL') AS losses
            FROM v8_paper_trades
            WHERE (%(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp)
              AND basket IS DISTINCT FROM 's1_reclaim_obs'
        """, scope)
        sm = _rows(cur)[0]

    def f(v):
        return float(v) if v is not None else None

    gross = f(sm["gross"]) or 0.0
    brokerage = (sm["n"] or 0) * BROKERAGE_PER_TRADE
    return {
        "trades": [{
            "symbol": t["symbol"], "side": (t["side"] or "").upper(),
            "basket": basket_label(t["basket"]),
            "entry": f(t["entry_price"]), "exit": f(t["exit_price"]),
            "qty": f(t["qty"]), "pnl": f(t["pnl"]), "ret_pct": f(t["return_pct"]),
            "result": t["result"],
            "when": t["exit_ts"].strftime("%d %b %H:%M") if t["exit_ts"] else None,
        } for t in tr],
        "day_log": [{
            "date": d["d"].strftime("%Y-%m-%d"),
            "label": d["d"].strftime("%a %d %b"),
            "n": d["n"], "pnl": f(d["pnl"]),
            "wins": d["wins"], "losses": d["losses"],
        } for d in dl],
        "summary": {
            "era": era if era == "all" else "fresh",
            "trades": sm["n"],
            "gross": round(gross, 2),
            "brokerage": brokerage,
            "realised": round(gross - brokerage, 2),
            "wins": sm["wins"], "losses": sm["losses"],
        },
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


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
