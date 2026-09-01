"""
chart_peers.py — cc#845 CHART PEER TAB V1 (founder 03-Aug).

Powers the CHART | PEERS tab pair on the shared stock chart card. READ-ONLY: gvm_scores +
v8_metrics + raw_prices + the shared live-CMP resolver. No new tables, no new fetch jobs.

THE ONE THING THIS FILE EXISTS TO GET RIGHT — CASH PEERS MUST NOT BE DROPPED.
v8_metrics only covers the ~209-symbol FUTURES universe. A peer segment is drawn from gvm_scores,
which covers ~1,789 symbols, so most peers are NOT in v8_metrics. The founder's own example proves
how bad an inner join would be: of the five City Gas Distribution peers, exactly ONE (PETRONET)
has a v8_metrics row — an inner join would render a "peer table" containing only the symbol you
were already looking at. That is the TC root-cause lesson restated, and it is why every return
below is LEFT-joined with an explicit raw_prices fallback computed per symbol.

RETURN BASIS. day_1d / week_return / month_return from v8_metrics where present; otherwise computed
from raw_prices over each symbol's OWN sessions (1 / 5 / 21 bars back), never a shared calendar and
never a calendar-day offset — a peer that did not trade yesterday must not read as a crash. The
source of each row's returns is returned as `ret_src` so the card can never imply a precision it
does not have.

VERDICT BAND. The founder specified explicit cutoffs for this table — Strong Buy >=8, Buy 7-8,
Watch 6-7, Exit <6 — which are NOT the same labels gvm_scores.verdict carries (Good / Average /
Weak). Both are returned: `band` is the founder's, `verdict` is the stored one. They are kept
distinct rather than reconciled here, because silently relabelling a stored verdict on one surface
is how two surfaces start disagreeing about the same stock.
"""

import os
import logging
from typing import Dict, Any, Optional

import psycopg2
from fastapi import APIRouter

log = logging.getLogger("scorr.chart_peers")
router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")

WEEK_BARS, MONTH_BARS = 5, 21


def _conn():
    return psycopg2.connect(_DB)


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _band(gvm: Optional[float]) -> Optional[str]:
    if gvm is None:
        return None
    if gvm >= 8:
        return "Strong Buy"
    if gvm >= 7:
        return "Buy"
    if gvm >= 6:
        return "Watch"
    return "Exit"


def build_peers(cur, symbol: str) -> Dict[str, Any]:
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "symbol required", "peers": []}

    cur.execute("""SELECT segment FROM gvm_scores
                   WHERE symbol=%s AND score_date=(SELECT MAX(score_date) FROM gvm_scores)""", (sym,))
    r = cur.fetchone()
    if not r or not r[0]:
        return {"symbol": sym, "segment": None, "peers": [],
                "reason": f"{sym} has no GVM segment on the latest score_date — no peer set to draw."}
    segment = r[0]

    # LEFT JOIN, deliberately. See the module docstring: an inner join here would delete most of
    # the table. `score_date` is pinned to each source's own latest so a lagging v8_metrics run
    # cannot silently empty the column for everyone.
    cur.execute("""
        SELECT g.symbol, g.company_name, g.gvm_score, g.verdict, g.market_cap,
               m.day_1d, m.week_return, m.month_return
        FROM gvm_scores g
        LEFT JOIN v8_metrics m
               ON m.symbol = g.symbol
              AND m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
        WHERE g.segment = %s AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
        ORDER BY g.gvm_score DESC NULLS LAST, g.symbol
    """, (segment,))
    rows = cur.fetchall()
    if not rows:
        return {"symbol": sym, "segment": segment, "peers": [],
                "reason": f"No GVM-scored members in {segment}."}

    syms = [x[0] for x in rows]

    # cc#1500: futures-tradability marker. futures_universe.is_active is the ONE source — the same
    # table the trade card's lot_size and the strip's D-button already read. NOT inferred from
    # v8_metrics presence (a lagging metrics run would strip the markers).
    cur.execute("SELECT symbol FROM futures_universe WHERE is_active AND symbol = ANY(%s)", (syms,))
    fut_set = {x[0] for x in cur.fetchall()}

    # ── raw_prices fallback, computed per symbol over ITS OWN bars ────────────────────────────
    cur.execute("""
        WITH ranked AS (
            SELECT symbol, close, price_date,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) rn
            FROM raw_prices WHERE symbol = ANY(%s) AND close > 0
        )
        SELECT symbol,
               MAX(close) FILTER (WHERE rn = 1) AS c0,
               MAX(close) FILTER (WHERE rn = 2) AS c1,
               MAX(close) FILTER (WHERE rn = %s) AS cw,
               MAX(close) FILTER (WHERE rn = %s) AS cm,
               MAX(price_date) FILTER (WHERE rn = 1) AS d0
        FROM ranked WHERE rn <= %s GROUP BY symbol
    """, (syms, WEEK_BARS + 1, MONTH_BARS + 1, MONTH_BARS + 1))
    fb = {}
    for s_, c0, c1, cw, cm, d0 in cur.fetchall():
        def _p(base):
            b, c = _f(base), _f(c0)
            return round((c / b - 1) * 100, 2) if (c is not None and b) else None
        fb[s_] = {"day": _p(c1), "week": _p(cw), "month": _p(cm), "as_of": d0}

    # ── live CMP through the SHARED resolver (cc#811/#835), never a private price path ────────
    live = {}
    try:
        import cmp_resolver
        live = cmp_resolver.resolve_cmp_many(cur, syms)
    except Exception as e:
        log.warning("cc#845 live CMP unavailable, falling back to EOD close: %s", e)

    peers, n_v8, n_fallback = [], 0, 0
    for s_, name, gvm, verdict, mcap, d1, wr, mr in rows:
        f = fb.get(s_, {})
        has_v8 = any(v is not None for v in (d1, wr, mr))
        if has_v8:
            n_v8 += 1
            day, week, month, src = _f(d1), _f(wr), _f(mr), "v8_metrics"
        else:
            n_fallback += 1
            day, week, month, src = f.get("day"), f.get("week"), f.get("month"), "raw_prices"
        lv = live.get(s_) or {}
        peers.append({
            "symbol": s_, "company_name": name,
            "gvm": round(float(gvm), 2) if gvm is not None else None,
            "verdict": verdict,                 # the STORED verdict
            "band": _band(_f(gvm)),             # the founder's band for THIS table
            "market_cap": _f(mcap),
            "day_pct": day, "week_pct": week, "month_pct": month,
            "ret_src": src,
            "is_futures": s_ in fut_set,   # cc#1500
            "cmp": lv.get("cmp"), "cmp_live": bool(lv.get("live")),
            "as_of": str(f.get("as_of")) if f.get("as_of") else None,
            "is_self": s_ == sym,
        })

    return {
        "symbol": sym, "segment": segment, "peers": peers, "count": len(peers),
        "returns_from_v8_metrics": n_v8, "returns_from_raw_prices": n_fallback,
        "basis": (f"gvm_scores latest score_date · returns v8_metrics where available "
                  f"({n_v8}/{len(peers)}), raw_prices fallback for the rest ({n_fallback}/{len(peers)})"),
        "band_rule": "Strong Buy ≥8 · Buy 7-8 · Watch 6-7 · Exit <6",
    }


@router.get("/api/chart/peers/{symbol}")
def chart_peers(symbol: str):
    try:
        with _conn() as conn, conn.cursor() as cur:
            return build_peers(cur, symbol)
    except Exception as e:
        log.exception("chart_peers failed")
        # Always JSON (cc#835 doctrine) — the tab renders a readable message, never a parse error.
        return {"symbol": symbol, "peers": [], "error": f"{type(e).__name__}: {str(e)[:200]}"}

# ── cc#845 INLINE TRADE CARD ──────────────────────────────────────────────────────────────────
# The spec asks the CARD drawer for the standard vertical 2-col trade card
# (ENTRY/TARGET/SL/LOT SIZE/REWARD/RISK/R:R/PRICE/TIME). Two facts had to be reconciled:
#   * /api/trade-check/v4 returns a VERDICT and pivots — it carries no entry/target/SL/lot/R:R.
#   * grep finds no existing renderer for those fields anywhere in the repo, so there was nothing
#     to reuse; the format comes from a screenshot, not from a shipped component.
# Rather than invent the numbers in JS, the levels are derived HERE, once, from the platform's own
# pivot doctrine — the same PP/R1/S1 the chart card already draws: ENTRY = live CMP, TARGET = R1,
# SL = S1. Reward/risk/R:R follow from those and the lot size.
#
# LOT SIZE IS NOT FAKED. It comes from futures_universe and is NULL for a cash-only peer — which is
# most peers in most segments. A cash peer therefore shows levels and R:R (both lot-independent)
# and an em dash for lot/reward/risk, instead of a fabricated single-share "position".
# This renders LEVELS, not advice: no recommendation, in line with the RA-safe doctrine.

def build_trade_card(cur, symbol: str) -> Dict[str, Any]:
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "symbol required"}

    cur.execute("""SELECT pp, r1, s1, r2, s2, score_date FROM universe_technicals
                   WHERE symbol=%s ORDER BY score_date DESC LIMIT 1""", (sym,))
    piv = cur.fetchone()

    cmp_v = None
    try:
        import cmp_resolver
        cmp_v = (cmp_resolver.resolve_cmp_many(cur, [sym]).get(sym) or {}).get("cmp")
    except Exception as e:
        log.warning("cc#845 trade card CMP resolve failed for %s: %s", sym, e)
    if cmp_v is None:
        cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND close>0
                       ORDER BY price_date DESC LIMIT 1""", (sym,))
        r = cur.fetchone()
        cmp_v = _f(r[0]) if r else None

    cur.execute("SELECT lot_size FROM futures_universe WHERE symbol=%s AND is_active", (sym,))
    lr = cur.fetchone()
    lot = int(lr[0]) if (lr and lr[0]) else None

    if not piv or cmp_v is None:
        return {"symbol": sym, "cmp": cmp_v, "lot_size": lot,
                "error": None if cmp_v is not None else "no price available",
                "reason": "No stored pivots for this symbol — levels cannot be derived.",
                "basis": "universe_technicals pivots + shared live-CMP resolver"}

    pp, r1, s1 = _f(piv[0]), _f(piv[1]), _f(piv[2])
    entry, target, sl = cmp_v, r1, s1
    reward = risk = rr = None
    if target is not None and sl is not None and entry is not None:
        up, dn = target - entry, entry - sl
        rr = round(up / dn, 2) if dn > 0 else None
        if lot:
            reward, risk = round(up * lot, 0), round(dn * lot, 0)

    verdict = None
    try:
        from tc_resolver import get_primary_tc   # cc#1549: resolver, not a hardcoded tc_v4_endpoints import
        tc = get_primary_tc()(sym, "LONG")
        verdict = tc.get("final_verdict") if isinstance(tc, dict) else None
    except Exception as e:
        log.warning("cc#845 trade card verdict for %s: %s", sym, e)

    return {
        "symbol": sym, "cmp": round(cmp_v, 2),
        "entry": round(entry, 2),
        "target": round(target, 2) if target is not None else None,
        "sl": round(sl, 2) if sl is not None else None,
        "pivot": round(pp, 2) if pp is not None else None,
        "lot_size": lot, "reward": reward, "risk": risk, "rr": rr,
        "verdict": verdict,
        "as_of": str(piv[5]) if piv[5] else None,
        "basis": ("levels from universe_technicals pivots (ENTRY=CMP, TARGET=R1, SL=S1); "
                  "lot from futures_universe (— for cash peers); price via the shared live-CMP resolver"),
    }


@router.get("/api/chart/tradecard/{symbol}")
def chart_trade_card(symbol: str):
    try:
        with _conn() as conn, conn.cursor() as cur:
            return build_trade_card(cur, symbol)
    except Exception as e:
        log.exception("chart_trade_card failed")
        return {"symbol": symbol, "error": f"{type(e).__name__}: {str(e)[:200]}"}
