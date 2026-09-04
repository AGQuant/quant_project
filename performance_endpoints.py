"""
Performance Endpoints — Scorr (cc_task #9 subtask 5)
====================================================
Powers /performance (scorr_performance.html), 3 tabs:
  /api/performance/qb       — Quant Baskets: per-basket P&L + open positions
  /api/performance/alpha    — Alpha vs benchmark (per-basket NAV return - benchmark NAV return)
  /api/performance/options  — Options (V5): per-underlying P&L + trade list

Pure SQL reads. cc#1702 QB_RETURN_BASIS_V1 (session_log 38966): qb/alpha's RETURN, ALPHA and
BASKET VALUE now read qb_nav_daily (the SAME whole-basket-NAV series the cc#839 chart already
stores — nav/benchmark_nav rebased *100 at each basket's own inception), and PNL sums every
position ever taken (open + exited), not just the open book. The old nifty500_benchmark
constituent-weights lookup is retired from this file — it computed a DIFFERENT number than the
chart, on a DIFFERENT benchmark, which is exactly the "2 alpha numbers for 1 basket" this card
fixes.
"""

import os
from decimal import Decimal
import psycopg
from fastapi import APIRouter

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(DATABASE_URL)


def _num(v):
    if isinstance(v, Decimal):
        return float(v)
    return v


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [{k: _num(v) for k, v in zip(cols, r)} for r in cur.fetchall()]


@router.get("/api/performance/qb")
def performance_qb():
    """Quant Baskets — per-basket P&L summary + all open positions.

    cc#1702 QB_RETURN_BASIS_V1 (session_log 38966, founder 04-Sep "why 2 alpha number for 1
    basket"): RETURN and BASKET VALUE now read the SAME whole-basket NAV the cc#839 chart already
    stores (qb_nav_daily, latest row per basket — nav is rebased *100 at inception, so nav-100 IS
    the since-start return in points; holdings_value+cash_value IS the NAV numerator). PNL is now
    summed across EVERY position ever taken (open unrealised + exited realised), not just the open
    book — the old open-only SUM(pnl)/SUM(entry_price*qty) silently dropped every stopped-out
    loser (survivorship) and could never reconcile with the chart once a basket took a stop.
    `positions` stays its own honest concept — how many are open right now — unaffected."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT basket_name AS basket, ROUND(SUM(pnl)::numeric, 0) AS pnl
            FROM quant_paper_positions GROUP BY basket_name
        """)
        pnl_by_basket = {r[0]: _num(r[1]) for r in cur.fetchall()}
        cur.execute("""
            SELECT basket_name AS basket, COUNT(*) AS positions
            FROM quant_paper_positions WHERE status='open' GROUP BY basket_name
        """)
        open_count = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("""
            SELECT DISTINCT ON (basket_name) basket_name, nav, holdings_value, cash_value, nav_date
            FROM qb_nav_daily ORDER BY basket_name, nav_date DESC
        """)
        nav_by_basket = {}
        for name, nav, hv, cv, navd in cur.fetchall():
            nav_by_basket[name] = {
                "return_pct": (_num(nav) - 100.0) if nav is not None else None,
                "market_value": ((_num(hv) or 0) + (_num(cv) or 0)) if (hv is not None and cv is not None) else None,
                "nav_date": str(navd) if navd else None,
            }
        cur.execute("""
            SELECT basket_name AS basket, symbol,
                   ROUND(entry_price::numeric, 2) AS entry_price, qty,
                   ROUND(current_price::numeric, 2) AS current_price,
                   ROUND(pnl::numeric, 0) AS pnl, ROUND(pnl_pct::numeric, 2) AS pnl_pct,
                   gvm_at_entry AS gvm
            FROM quant_paper_positions WHERE status='open'
            ORDER BY pnl_pct DESC NULLS LAST
        """)
        positions = _rows(cur)
        # cc#1584: the latest mark on the open book, as IST wall-clock. updated_at is a naive
        # timestamp written by NOW() on a UTC server (qb_eod_checker), so it is read as UTC and
        # shifted; the Model Portfolio strip prints THIS, never the page clock. Additive only.
        cur.execute("SELECT to_char(MAX(updated_at) AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata', "
                    "'DD-Mon-YYYY HH24:MI') FROM quant_paper_positions WHERE status='open'")
        asof = cur.fetchone()
    names = set(pnl_by_basket) | set(open_count) | set(nav_by_basket)
    baskets = []
    for name in names:
        nv = nav_by_basket.get(name, {})
        ret = nv.get("return_pct")
        mv = nv.get("market_value")
        baskets.append({
            "basket": name, "pnl": pnl_by_basket.get(name),
            "return_pct": round(ret, 2) if ret is not None else None,
            "positions": open_count.get(name, 0),
            "market_value": round(mv, 0) if mv is not None else None,
            "nav_date": nv.get("nav_date"),
        })
    baskets.sort(key=lambda b: (b["pnl"] is None, -(b["pnl"] or 0)))
    total_pnl = round(sum(v for v in pnl_by_basket.values() if v is not None), 0) if pnl_by_basket else 0.0
    total_positions = sum(open_count.values()) if open_count else 0
    return {"tab": "quant_baskets", "total_pnl": total_pnl, "total_positions": total_positions,
            "as_of_ist": (asof[0] if asof else None), "baskets": baskets, "positions": positions}


@router.get("/api/performance/alpha")
def performance_alpha():
    """Alpha vs benchmark — per-basket NAV return minus the SAME basket's own benchmark_nav.

    cc#1702 QB_RETURN_BASIS_V1: rewired onto the whole-basket NAV basis qb_nav_daily already
    stores for the cc#839 chart (nav/benchmark_nav both rebased *100 at each basket's OWN
    inception date — see qb_nav.py's compute_series), instead of a separate open-positions-only
    formula computed against a DIFFERENT benchmark source (nifty500_benchmark, a constituent-
    weights table with a live cmp/raw_prices lookup) that could never agree with the chart and
    needed a one-off carve-out for alpha_multicap's cash-slot capital convention. That carve-out
    is GONE: qb_nav's nav is computed against each basket's own registry capital identically,
    alpha_multicap included, so there is nothing left to special-case.
    do_not_touch respected: the benchmark CHOICE itself (which index qb_nav_daily.benchmark_nav
    tracks per basket, cap_type-dependent — see qb_nav.py's own docstring) is read here, not
    changed; out_of_scope explicitly excludes changing it."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (basket_name) basket_name, nav, benchmark_nav, nav_date
            FROM qb_nav_daily ORDER BY basket_name, nav_date DESC
        """)
        nav_rows = cur.fetchall()
        cur.execute("""
            SELECT basket_name AS basket, ROUND(SUM(pnl)::numeric, 0) AS pnl
            FROM quant_paper_positions GROUP BY basket_name
        """)
        pnl_by_basket = {r[0]: _num(r[1]) for r in cur.fetchall()}
        # cc#1702 sweep (scope item 6): scorr_performance.html's own alpha table reads b.positions
        # off THIS endpoint's basket rows (open-book count) — the pre-cc#1702 query carried it via
        # its own COUNT(*). Keep supplying it so that reader doesn't regress to blank/undefined.
        cur.execute("""
            SELECT basket_name AS basket, COUNT(*) AS positions
            FROM quant_paper_positions WHERE status='open' GROUP BY basket_name
        """)
        open_count = {r[0]: r[1] for r in cur.fetchall()}
        # cc#1702 scope item 3: portfolio-level alpha is CAPITAL-WEIGHTED across active baskets —
        # a basket's own registry capital is its weight, so a bigger basket's return counts more
        # than a flat 12-way average would. Read once, reused for both legs of the weighted mean.
        cur.execute("SELECT basket_name, capital FROM quant_basket_registry WHERE is_active")
        capital_by_basket = {r[0]: _num(r[1]) for r in cur.fetchall() if r[1] is not None}
    baskets = []
    wtd_ret_num = wtd_bench_num = wtd_den = 0.0
    for name, nav, bnav, navd in nav_rows:
        ret = (_num(nav) - 100.0) if nav is not None else None
        bret = (_num(bnav) - 100.0) if bnav is not None else None
        alpha = round(ret - bret, 3) if (ret is not None and bret is not None) else None
        baskets.append({
            "basket": name, "return_pct": round(ret, 2) if ret is not None else None,
            "alpha": alpha, "pnl": pnl_by_basket.get(name), "nav_date": str(navd) if navd else None,
            "positions": open_count.get(name, 0),
        })
        cap = capital_by_basket.get(name)
        if cap and ret is not None and bret is not None:
            wtd_ret_num += cap * ret; wtd_bench_num += cap * bret; wtd_den += cap
    baskets.sort(key=lambda b: (b["return_pct"] is None, -(b["return_pct"] or 0)))
    total_pnl = round(sum(v for v in pnl_by_basket.values() if v is not None), 0) if pnl_by_basket else 0.0
    if wtd_den:
        total_ret = round(wtd_ret_num / wtd_den, 2)
        total_bench = round(wtd_bench_num / wtd_den, 2)
        total_alpha = round(total_ret - total_bench, 3)
    else:
        total_ret = total_bench = total_alpha = None
    total = {"return_pct": total_ret, "pnl": total_pnl, "alpha": total_alpha,
              "benchmark_return": total_bench}
    return {"tab": "alpha", "benchmark": "per-basket (qb_nav_daily.benchmark_sym; NIFTY50 today "
            "for every cap_type — see qb_nav.py, no NIFTY500 daily series exists)",
            "baskets": baskets, "total": total}


@router.get("/api/performance/options")
def performance_options():
    """Options (V5) — per-underlying P&L summary + trade list."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT ticker, ROUND(SUM(pnl)::numeric, 0) AS pnl, COUNT(*) AS trades,
                   COUNT(*) FILTER (WHERE pnl > 0) AS wins,
                   COUNT(*) FILTER (WHERE pnl < 0) AS losses
            FROM options_trades GROUP BY ticker ORDER BY pnl DESC
        """)
        by_ticker = _rows(cur)
        cur.execute("""
            SELECT trade_date, ticker, option_type, ROUND(strike_price::numeric,0) AS strike,
                   action, lots, ROUND(entry_price::numeric,2) AS entry_price,
                   ROUND(exit_price::numeric,2) AS exit_price, ROUND(pnl::numeric,0) AS pnl,
                   status, strategy, remarks
            FROM options_trades ORDER BY trade_date DESC, id DESC
        """)
        trades = _rows(cur)
        for t in trades:
            t["trade_date"] = str(t["trade_date"])
        cur.execute("SELECT ROUND(SUM(pnl)::numeric,0) AS pnl, COUNT(*) AS n FROM options_trades")
        tot = cur.fetchone()
    return {"tab": "options", "strategy": "V5",
            "total_pnl": _num(tot[0]) if tot and tot[0] is not None else 0.0,
            "total_trades": (tot[1] if tot else 0),
            "by_ticker": by_ticker, "trades": trades}
