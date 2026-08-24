"""cc#1286 · INVESTMENT SCANNER TAB — page route + board feed (spec session_log 30147/30129).

/inv-scanner            the page (template scorr_inv_scanner.html, token-contract styled)
/api/inv-scanner/board  one payload per track: ranked scores joined with universe tags,
                        pillar block, overlay readings, open-state badge and — for the
                        reversal track — the S1 trigger state read from the score row's own
                        components jsonb, so the page can say "touched N sessions ago,
                        reclaim pending" instead of implying it.

Read-only over the cc#1283/1284/1285 tables. TC Scanner surfaces untouched.
"""

import os
import logging
from typing import Optional

import psycopg
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

log = logging.getLogger("scorr.inv_scanner_page")
router = APIRouter(tags=["investment_scanner"])

_DIR = os.path.dirname(os.path.abspath(__file__))


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


@router.get("/inv-scanner", response_class=HTMLResponse)
def inv_scanner_page():
    with open(os.path.join(_DIR, "scorr_inv_scanner.html"), encoding="utf-8") as f:
        return f.read()


@router.get("/api/inv-scanner/symbol/{symbol}")
def symbol_card(symbol: str):
    """cc#1287: one symbol's scanner standing for the /check INVEST mode section. Honest
    absence when the name is not in the universe — never a fake score."""
    sym = (symbol or "").strip().upper()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT MAX(run_date) FROM investment_scanner_scores")
        r = cur.fetchone()
        d = r[0] if r else None
        if d is None:
            return {"symbol": sym, "in_universe": False, "run_date": None}
        cur.execute("""
            SELECT s.mom_score, s.band_mom, s.rev_score, s.band_rev,
                   u.tags, u.insufficient_history, st.status, st.entry_track, st.entered_at
            FROM investment_scanner_scores s
            JOIN investment_scanner_universe u ON u.symbol=s.symbol AND u.run_date=s.run_date
            LEFT JOIN investment_scanner_state st ON st.symbol=s.symbol
            WHERE s.run_date=%s AND s.symbol=%s""", (d, sym))
        row = cur.fetchone()
    if not row:
        return {"symbol": sym, "in_universe": False, "run_date": str(d)}
    mom, bm, rev, br, tags, insuff, st_status, st_track, st_at = row
    return {"symbol": sym, "in_universe": True, "run_date": str(d),
            "mom_score": _f(mom), "band_mom": bm,
            "rev_score": _f(rev), "band_rev": br,
            "tags": tags or [], "insufficient_history": bool(insuff),
            "state": ({"status": st_status, "track": st_track, "entered_at": str(st_at)}
                      if st_status else None)}


@router.get("/api/inv-scanner/board")
def board(track: str = "momentum", limit: int = 100):
    """Ranked board for one track. Every row carries everything the table renders — the page
    computes nothing."""
    col, band_col, comp_col = (("mom_score", "band_mom", "mom_components")
                               if track != "reversal" else
                               ("rev_score", "band_rev", "rev_components"))
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT MAX(run_date) FROM investment_scanner_scores")
        r = cur.fetchone()
        d = r[0] if r else None
        if d is None:
            return {"run_date": None, "track": track, "rows": []}
        cur.execute(f"""
            SELECT s.symbol, s.{col}, s.{band_col}, s.mom_score, s.rev_score,
                   u.gvm, u.g, u.v, u.m, u.tags, u.insufficient_history,
                   ut.week_index_52, ut.rsi_month, ut.vol_ratio_21, ut.month_return,
                   st.status, st.entry_track, st.entered_at,
                   s.{comp_col}->'s1_touch_reclaim'->'input' AS s1_input
            FROM investment_scanner_scores s
            JOIN investment_scanner_universe u ON u.symbol=s.symbol AND u.run_date=s.run_date
            LEFT JOIN universe_technicals ut ON ut.symbol=s.symbol
                 AND ut.score_date=(SELECT MAX(score_date) FROM universe_technicals)
            LEFT JOIN investment_scanner_state st ON st.symbol=s.symbol
            WHERE s.run_date=%s AND s.{col} IS NOT NULL
            ORDER BY s.{col} DESC, s.symbol
            LIMIT %s""", (d, max(1, min(limit, 250))))
        rows = []
        for r in cur.fetchall():
            (sym, score, band, mom, rev, gvm, g, v, m, tags, insuff,
             wk52, rsi, vol, mret, st_status, st_track, st_at, s1) = r
            rows.append({
                "symbol": sym, "score": _f(score), "band": band,
                "mom_score": _f(mom), "rev_score": _f(rev),
                "gvm": _f(gvm), "g": _f(g), "v": _f(v), "m": _f(m),
                "tags": tags or [], "insufficient_history": bool(insuff),
                "wk52": _f(wk52), "rsi_month": _f(rsi), "vol_ratio_21": _f(vol),
                "month_return": _f(mret),
                "state": ({"status": st_status, "track": st_track, "entered_at": str(st_at)}
                          if st_status else None),
                "s1": s1 if isinstance(s1, dict) else None,
            })
    return {"run_date": str(d), "track": track, "count": len(rows), "rows": rows}
