"""
Quant Basket (QB) endpoints — EQUITY buy-and-hold baskets.

Extracted from main.py (refactor file 1/5, 04-Jun-2026) to keep pushes small.
Self-contained: own _conn, own api_query, own _check_admin. Imports nothing
from main.py to avoid circular imports.

Endpoints (all /api/qb/*):
  POST /api/qb/eod_check            — run EOD stop-loss + P&L mark for one basket
  POST /api/qb/eod_check_all        — run EOD check for every basket with open positions
  POST /api/qb/mark_intraday        — intraday P&L mark
  POST /api/qb/fix_allocations      — fix allocation column + add NIFTYBEES residual for one basket
  POST /api/qb/fix_all_allocations  — fix all 4 baskets at once
  GET  /api/qb/positions            — open/closed positions with P&L
  GET  /api/qb/summary              — basket summary (market value, unreal/real P&L)
  GET  /api/qb/rebalance_log        — rebalance + EOD check history
  GET  /api/qb/registry             — basket registry
"""

from fastapi import APIRouter, HTTPException, Header
from typing import Optional
import psycopg
import os

import qb_eod_checker
import qb_rebalance
import qb_alpha_select   # cc#553: Alpha Multicap V2 FINAL selection/proposal engine (spec id=6086)
import qb_smallcap_select # cc#554: Small Cap V2 selection/proposal engine (spec id=6094)
import qb_composite_select # cc#555+556: parameterized Large Cap V2 (id=6097) + Mid Cap V2 (id=6098)
import qb_breakout_select  # cc#559: 52-Week Breakout basket (5th QB) selection/proposal (spec id=6103)

router = APIRouter(prefix="/api/qb", tags=["quant_basket"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
BASKETS     = ["large_cap", "mid_cap", "small_cap", "alpha_multicap"]


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _check_admin(token):
    if not ADMIN_TOKEN:
        return True
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


def api_query(sql, params=None, single=False):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params or ())
            cols = [d[0] for d in cur.description] if cur.description else []
            if single:
                r = cur.fetchone()
                return dict(zip(cols, r)) if r else None
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        return {"error": str(e)}


@router.post("/eod_check")
def qb_eod_check_now(basket_name: str = "large_cap", x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with _conn() as conn:
        return qb_eod_checker.run_eod_checker(conn, basket_name=basket_name)


@router.post("/eod_check_all")
def qb_eod_check_all(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    out = []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT basket_name FROM quant_paper_positions WHERE status='open'")
            baskets = [r[0] for r in cur.fetchall()]
        for b in baskets:
            out.append(qb_eod_checker.run_eod_checker(conn, basket_name=b))
    return {"baskets_run": len(out), "results": out}


@router.post("/mark_intraday")
async def qb_mark_intraday_now(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with _conn() as conn:
        return qb_eod_checker.qb_intraday_mark(conn)


@router.post("/rebalance_now")
def qb_rebalance_now(basket_name: str = "large_cap", x_admin_token: Optional[str] = Header(None)):
    """cc#439: run the scheduled rebalance for ONE basket (exits + NIFTYBEES residual + advance
    next_rebalance + log). New-stock entries stay a founder-confirmed step (see run_scheduled_rebalance)."""
    _check_admin(x_admin_token)
    with _conn() as conn:
        return qb_rebalance.run_scheduled_rebalance(conn, basket_name=basket_name)


@router.post("/rebalance_due")
def qb_rebalance_due(x_admin_token: Optional[str] = Header(None)):
    """cc#439: run the scheduled rebalance for every ACTIVE basket whose next_rebalance is due —
    the founder-approved overdue 06-Jul large_cap + mid_cap catch-up runs here (also runs nightly
    via scheduler._bg_qb_eod on trading days)."""
    _check_admin(x_admin_token)
    out = []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT basket_name FROM quant_basket_registry "
                        "WHERE is_active=TRUE AND next_rebalance IS NOT NULL "
                        "AND next_rebalance <= CURRENT_DATE ORDER BY basket_name")
            due = [r[0] for r in cur.fetchall()]
        for b in due:
            out.append(qb_rebalance.run_scheduled_rebalance(conn, basket_name=b))
    return {"due": len(out), "results": out}


@router.post("/fix_allocations")
def qb_fix_allocations(basket_name: str = "large_cap", x_admin_token: Optional[str] = Header(None)):
    """Fix allocation column + add NIFTYBEES residual for one basket."""
    _check_admin(x_admin_token)
    with _conn() as conn:
        return qb_rebalance.fix_basket_overdeployment(conn, basket_name=basket_name)


@router.post("/fix_all_allocations")
def qb_fix_all_allocations(x_admin_token: Optional[str] = Header(None)):
    """Fix all 4 baskets — allocation column + NIFTYBEES residual."""
    _check_admin(x_admin_token)
    results = []
    with _conn() as conn:
        for b in BASKETS:
            results.append(qb_rebalance.fix_basket_overdeployment(conn, basket_name=b))
    return {"baskets_fixed": len(results), "results": results}


@router.get("/positions")
def qb_positions(basket_name: str = "large_cap", status: str = "open"):
    return api_query("""
        SELECT symbol, entry_price, entry_date, qty,
               ROUND(qty*entry_price,2) AS cost_basis,
               current_price, current_value,
               ROUND(pnl,2) AS pnl, ROUND(pnl_pct,2) AS pnl_pct,
               stop_loss_price, gvm_at_entry AS gvm,
               g_at_entry AS g, v_at_entry AS v, m_at_entry AS m,
               status, exit_price, exit_date, notes, updated_at
        FROM quant_paper_positions
        WHERE basket_name=%s AND status=%s
        ORDER BY pnl_pct DESC NULLS LAST
    """, (basket_name, status))


@router.get("/summary")
def qb_summary(basket_name: str = "large_cap"):
    open_pos   = api_query(
        "SELECT COUNT(*) AS cnt, ROUND(SUM(current_value),2) AS mkt_value, ROUND(SUM(pnl),2) AS unreal_pnl "
        "FROM quant_paper_positions WHERE basket_name=%s AND status='open'",
        (basket_name,), single=True)
    closed_pos = api_query(
        "SELECT COUNT(*) AS cnt, ROUND(SUM(pnl),2) AS real_pnl "
        "FROM quant_paper_positions WHERE basket_name=%s AND status LIKE 'exited%%'",
        (basket_name,), single=True)
    return {
        "basket":           basket_name,
        "open_positions":   open_pos.get("cnt", 0),
        "market_value":     open_pos.get("mkt_value", 0),
        "unrealised_pnl":   open_pos.get("unreal_pnl", 0),
        "closed_positions": closed_pos.get("cnt", 0),
        "realised_pnl":     closed_pos.get("real_pnl", 0),
        "total_pnl":        round((open_pos.get("unreal_pnl") or 0) + (closed_pos.get("real_pnl") or 0), 2),
    }


@router.get("/rebalance_log")
def qb_rebalance_log(basket_name: str = "large_cap", limit: int = 30):
    return api_query(
        "SELECT rebalance_date, stocks_in, stocks_out, stocks_held, total_portfolio_value, actions, computed_at "
        "FROM quant_rebalance_log WHERE basket_name=%s ORDER BY computed_at DESC LIMIT %s",
        (basket_name, limit))


@router.get("/registry")
def qb_registry(basket_name: Optional[str] = None):
    if basket_name:
        return api_query("SELECT * FROM quant_basket_registry WHERE basket_name=%s", (basket_name,), single=True)
    return api_query(
        "SELECT basket_name, cap_type, capital, max_stocks, rebalance_freq, weight_band, "
        "next_rebalance, is_active, notes FROM quant_basket_registry ORDER BY basket_name")


@router.get("/alpha/propose")
def qb_alpha_propose(as_of: Optional[str] = None):
    """cc#553 (spec id=6086): DRY-RUN Alpha Multicap V2 FINAL rebalance proposal — top-12 entries
    (0.5*GVM+0.5*M, gates GVM>=7.5/V>=7.5/M>7/dGVM_180d>+0.5, Nifty500), cash slots when <12 pass,
    plus the monthly max-3 exit (held names ranked outside composite top-25, worst first) and
    gate-passing refills. READ-ONLY — execution stays founder-confirmed. `as_of` defaults to today.
    Reproduces the manual SQL replication exactly (acceptance)."""
    try:
        return qb_alpha_select.propose_rebalance(as_of=as_of)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/smallcap/propose")
def qb_smallcap_propose(as_of: Optional[str] = None):
    """cc#554 (spec id=6094): DRY-RUN Small Cap V2 ENTRY proposal — qualifiers with mcap rank>250,
    gates GVM>=8/V>=7.5/dGVM_180d>+0.5/segment-avg-GVM>=6.0, mapped to one of the 8 themes; N-based
    equal sizing (5L/N, N<10 -> 5L/10 per name + cash brake). ENTRY-ONLY — current holdings are
    never flagged for exit here (exits stay HS1/HS2/quarterly). READ-ONLY, founder-confirmed to
    execute. `as_of` defaults to today."""
    try:
        return qb_smallcap_select.propose_rebalance(as_of=as_of)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/largecap/propose")
def qb_largecap_propose(as_of: Optional[str] = None):
    """cc#555 (spec id=6097): DRY-RUN Large Cap V2 proposal — universe mcap rank<=100, score
    0.5*GVM+0.5*M, gates GVM>=7.0 AND dGVM_180d>+0.5 (10-filter gauntlet retired); top-12 equal
    weight 5L/12, <10 -> 5L/10 + cash; monthly max-3 exit outside composite top-20. READ-ONLY,
    founder-confirmed. `as_of` defaults to today."""
    try:
        return qb_composite_select.propose_largecap(as_of=as_of)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/midcap/propose")
def qb_midcap_propose(as_of: Optional[str] = None):
    """cc#556 (spec id=6098): DRY-RUN Mid Cap V2 proposal — universe mcap rank 101-250, gates
    GVM>=7.5 AND G>=7.0 (V gate dropped, no dGVM), ranked by M SCORE desc; top-20 equal weight
    5L/20, <10 -> 5L/10 + cash. ENTRY-ONLY (exits UNCHANGED, HS2 kept). READ-ONLY, founder-
    confirmed. `as_of` defaults to today."""
    try:
        return qb_composite_select.propose_midcap(as_of=as_of)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/breakout/propose")
def qb_breakout_propose(as_of: Optional[str] = None):
    """cc#559 (spec id=6103): DRY-RUN 52-Week Breakout proposal — screen GVM>=7.5 AND week_index_52
    >=90 AND month_index>=90 AND mcap>1000Cr AND vol_ratio_21>=1.0 (universe_technicals x gvm_scores,
    vol computed inline from raw_prices); N>10 -> top 10 by 1y return, 5<=N<=10 -> all, N<5 -> cash;
    Rs 50k/slot. READ-ONLY, founder-confirmed. `as_of` defaults to today."""
    try:
        return qb_breakout_select.propose_rebalance(as_of=as_of)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
