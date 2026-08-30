"""
hr_report.py — cc#398 Portfolio Health Report (spec id=2994), MODULE 2: the report engine.

GET /api/health/report/{portfolio_id} -> computes ALL 13 v1 sections + the founder-approved v2
additions (sector table, replacement engine, upcoming results, per-holding result analysis, red-flag
scanner, enriched holdings) from native Scorr sources only. Every insight line is rule-generated from
thresholds (never hardcoded). GVM verdict -> action band mapping is the Scorr taxonomy.

Data sources: hr_holdings, cmp_prices, raw_prices, gvm_history (latest + 30d trend), input_raw
(segment/cap/result_analysis/fwd-growth), screener_raw (pe/yield/holding/fii), sector_ratings,
earnings_calendar. NIFTY50 AND NIFTY500 (cc#1015: real ^CRSLDX) index series both live in
raw_prices; the synthetic nifty500_benchmark basket is NO LONGER read here (it is QB-only).
"""
import os
import re
from datetime import date, timedelta

import psycopg
from fastapi import APIRouter

# cc#1426: screener_raw.segment_pe ("Industry PE") is a dead column (0/1872 rows, CSV export
# dropped it) — falls back to Scorr's own segment peer-avg PE, honestly labelled. See
# segment_pe_fallback.py.
from segment_pe_fallback import segment_pe_map, lookup as _pe_lookup
# cc#1484: the Breakup tab's BFSI Interest-Coverage skip must use the SAME segment set the
# nightly engine scores with — importing it keeps the two gates one definition, not a copy.
from gvm_nightly import BFSI_SEGMENTS

router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(_DB)


# ---- null-safe numeric helpers -------------------------------------------------
def _f(v):
    """float or None."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _r(v, d=2):
    f = _f(v)
    return round(f, d) if f is not None else None


def _wavg(pairs):
    """Weighted average of [(value, weight), ...], skipping None values. Returns None if no weight."""
    num = den = 0.0
    for val, w in pairs:
        val = _f(val)
        w = _f(w)
        if val is None or w is None or w <= 0:
            continue
        num += val * w
        den += w
    return (num / den) if den > 0 else None


# ---- GVM verdict -> Scorr action band -----------------------------------------
_VERDICT_ACTION = {
    "Excellent": "Buy Systematically",
    "Good": "Accumulate-Hold",
    "Average": "Wait & Watch",
    "Weak": "Avoid-Exit",
}


def _action_for(verdict):
    return _VERDICT_ACTION.get(verdict or "", "Wait & Watch")


def _band_from_score(gvm):
    """0-10 GVM score -> verdict band (matches gvm_history thresholds)."""
    g = _f(gvm)
    if g is None:
        return None
    if g >= 8.0:
        return "Excellent"
    if g >= 7.0:
        return "Good"
    if g >= 6.0:
        return "Average"
    return "Weak"


# ---- batch reference loaders ---------------------------------------------------
def _load_gvm(cur, syms):
    """Latest gvm row per symbol + gvm_score ~30 trading days back (for the trend arrow)."""
    cur.execute("""
        SELECT DISTINCT ON (symbol) symbol, g_score, v_score, m_score, gvm_score, verdict, segment
        FROM gvm_history WHERE symbol = ANY(%s) ORDER BY symbol, score_date DESC
    """, (syms,))
    latest = {r[0]: {"g": _f(r[1]), "v": _f(r[2]), "m": _f(r[3]), "gvm": _f(r[4]),
                     "verdict": r[5], "segment": r[6]} for r in cur.fetchall()}
    cur.execute("""
        SELECT DISTINCT ON (symbol) symbol, gvm_score FROM gvm_history
        WHERE symbol = ANY(%s) AND score_date <= (CURRENT_DATE - INTERVAL '30 days')
        ORDER BY symbol, score_date DESC
    """, (syms,))
    prev = {r[0]: _f(r[1]) for r in cur.fetchall()}
    for s, row in latest.items():
        p = prev.get(s)
        if p is not None and row["gvm"] is not None:
            d = row["gvm"] - p
            row["gvm_trend"] = "up" if d > 0.05 else ("down" if d < -0.05 else "flat")
            row["gvm_trend_delta"] = round(d, 2)
        else:
            row["gvm_trend"] = "flat"
            row["gvm_trend_delta"] = None
    return latest


def _load_cmp(cur, syms):
    cur.execute("SELECT symbol, cmp FROM cmp_prices WHERE symbol = ANY(%s)", (syms,))
    out = {r[0]: _f(r[1]) for r in cur.fetchall()}
    missing = [s for s in syms if out.get(s) is None]
    if missing:  # fallback to latest raw_prices close
        cur.execute("""SELECT DISTINCT ON (symbol) symbol, close FROM raw_prices
                       WHERE symbol = ANY(%s) ORDER BY symbol, price_date DESC""", (missing,))
        for r in cur.fetchall():
            out[r[0]] = _f(r[1])
    return out


def _load_screener(cur, syms):
    cur.execute("""
        SELECT nse_code, pe, segment_pe, dividend_yield, "Promoter holding",
               "Unpledged promoter holding", fii_change, dii_change, return_1y, "High price", market_cap,
               "Sales growth", "Operating profit growth"
        FROM screener_raw WHERE nse_code = ANY(%s)
    """, (syms,))
    out = {}
    for r in cur.fetchall():
        out[r[0]] = {"pe": _f(r[1]), "segment_pe": _f(r[2]), "yield": _f(r[3]),
                     "promoter": _f(r[4]), "unpledged": _f(r[5]), "fii_change": _f(r[6]),
                     "dii_change": _f(r[7]), "return_1y": _f(r[8]), "high52": _f(r[9]),
                     "mcap": _f(r[10]),
                     "sales_growth": _f(r[11]), "profit_growth": _f(r[12])}   # cc#662 Results-This-Quarter
    return out


def _load_input(cur, syms):
    # cc#1104: market_cap is GONE from this SELECT. It was the second reader of
    # input_raw.market_cap in the codebase and the only one outside gvm_nightly — found by reading
    # rather than by grep, because the column name and the table name sit on different lines.
    # It was also DEAD: the "mcap" key it produced is never consumed anywhere in this module.
    # Removing it is what lets the column be dropped at all; leaving it would have turned a planned
    # ALTER TABLE into a broken HR report.
    # (_load_screener above still selects screener_raw.market_cap into an equally unused "mcap" key.
    # That one is harmless — it reads the surviving source — so it is named here rather than tidied
    # away inside a card about which table owns the number.)
    # cap_category and mcap_rank STAY. They are stored band columns this card does not move.
    cur.execute("""SELECT nse_code, company_name, gvm_segment, cap_category, mcap_rank,
                          result_analysis, fy27_growth,
                          (last_result_analysis_updated >= (CURRENT_DATE - INTERVAL '45 days')) AS ra_fresh
                   FROM input_raw WHERE nse_code = ANY(%s)""", (syms,))
    return {r[0]: {"company_name": r[1], "segment": r[2], "cap": r[3], "mcap_rank": r[4],
                   "result_analysis": r[5], "fy27_growth": _f(r[6]),
                   "ra_fresh": bool(r[7])}
            for r in cur.fetchall()}


def _load_ath(cur, syms):
    cur.execute("SELECT symbol, MAX(high) FROM raw_prices WHERE symbol = ANY(%s) GROUP BY symbol", (syms,))
    return {r[0]: _f(r[1]) for r in cur.fetchall()}


# ---- benchmark returns ---------------------------------------------------------
def _nifty50_1y(cur):
    cur.execute("SELECT close FROM raw_prices WHERE symbol='NIFTY50' ORDER BY price_date DESC LIMIT 1")
    now = cur.fetchone()
    cur.execute("""SELECT close FROM raw_prices WHERE symbol='NIFTY50'
                   AND price_date <= (CURRENT_DATE - INTERVAL '365 days') ORDER BY price_date DESC LIMIT 1""")
    then = cur.fetchone()
    n, t = (_f(now[0]) if now else None), (_f(then[0]) if then else None)
    return round((n - t) / t * 100, 2) if (n and t) else None


def _nifty500_1y(cur):
    """cc#1015 P1 DATA HONESTY: REAL Nifty 500 index 1y return from raw_prices 'NIFTY500' (Yahoo
    ^CRSLDX), mirroring _nifty50_1y. Replaces the nifty500_benchmark mcap-weighted proxy (current
    constituent weights applied BACKWARDS = composition/survivorship bias). Real series absent -> None
    (rendered 'unavailable'); the synthetic basket is NEVER read here."""
    cur.execute("SELECT close FROM raw_prices WHERE symbol='NIFTY500' ORDER BY price_date DESC LIMIT 1")
    now = cur.fetchone()
    cur.execute("""SELECT close FROM raw_prices WHERE symbol='NIFTY500'
                   AND price_date <= (CURRENT_DATE - INTERVAL '365 days') ORDER BY price_date DESC LIMIT 1""")
    then = cur.fetchone()
    n, t = (_f(now[0]) if now else None), (_f(then[0]) if then else None)
    return round((n - t) / t * 100, 2) if (n and t) else None


def _price_on_or_before(cur, sym, d):
    """cc#653: latest raw_prices close on/before date d for a symbol (the alpha-window anchor price)."""
    cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND price_date<=%s
                   ORDER BY price_date DESC LIMIT 1""", (sym, d))
    r = cur.fetchone()
    return _f(r[0]) if r else None


def _nifty500_return_since(cur, start_date):
    """cc#1015 P1 DATA HONESTY: the REAL Nifty 500 index return from start_date -> live, read from
    raw_prices 'NIFTY500' (Yahoo ^CRSLDX) — exactly like the NIFTY50 path (nearest prior trading day on
    a holiday via price_date<=start_date). HARD GUARD: the synthetic nifty500_benchmark (current
    constituent weights applied BACKWARDS) is NEVER read here. That composition/survivorship bias
    reported NEGATIVE alpha for windows before its 01-Jun-2026 base_date where the real index is
    positive (Ritesh -3.7% vs a real +2.6% since 10-Sep-2025; founder chart 11-Aug). The synthetic stays
    confined to QB benchmarking from its base forward. Real series absent -> None => 'vs Nifty 500
    unavailable', never a reconstructed/back-projected number (data-honesty doctrine)."""
    cur.execute("SELECT close FROM raw_prices WHERE symbol='NIFTY500' ORDER BY price_date DESC LIMIT 1")
    now = cur.fetchone()
    cur.execute("""SELECT close FROM raw_prices WHERE symbol='NIFTY500' AND price_date<=%s
                   ORDER BY price_date DESC LIMIT 1""", (start_date,))
    then = cur.fetchone()
    n, t = (_f(now[0]) if now else None), (_f(then[0]) if then else None)
    return round((n - t) / t * 100.0, 2) if (n and t) else None


def _nifty50_return_since(cur, start_date):
    """cc#656: NIFTY50 index return from start_date -> live (raw_prices 'NIFTY50' close on/before the
    start vs the latest close)."""
    cur.execute("SELECT close FROM raw_prices WHERE symbol='NIFTY50' ORDER BY price_date DESC LIMIT 1")
    now = cur.fetchone()
    cur.execute("""SELECT close FROM raw_prices WHERE symbol='NIFTY50' AND price_date<=%s
                   ORDER BY price_date DESC LIMIT 1""", (start_date,))
    then = cur.fetchone()
    n, t = (_f(now[0]) if now else None), (_f(then[0]) if then else None)
    return round((n - t) / t * 100.0, 2) if (n and t) else None


def _segment_aggs(cur, segments):
    """Per-segment avg PE + avg dividend yield across all stocks (sector benchmark)."""
    if not segments:
        return {}
    cur.execute("""
        SELECT i.gvm_segment, AVG(s.pe) FILTER (WHERE s.pe > 0 AND s.pe < 200),
               AVG(s.dividend_yield) FILTER (WHERE s.dividend_yield >= 0)
        FROM input_raw i JOIN screener_raw s ON s.nse_code = i.nse_code
        WHERE i.gvm_segment = ANY(%s) GROUP BY i.gvm_segment
    """, (segments,))
    return {r[0]: {"pe": _f(r[1]), "yield": _f(r[2])} for r in cur.fetchall()}


def _market_pe_yield(cur):
    """Large-cap median PE + yield as a Nifty proxy."""
    cur.execute("""
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY s.pe),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY s.dividend_yield)
        FROM screener_raw s JOIN input_raw i ON i.nse_code = s.nse_code
        WHERE i.cap_category = 'large' AND s.pe > 0 AND s.pe < 200
    """)
    r = cur.fetchone()
    return (_f(r[0]) if r else None), (_f(r[1]) if r else None)


def _sector_ratings(cur, segments):
    if not segments:
        return {}
    cur.execute("""
        SELECT DISTINCT ON (segment) segment, mcap_weighted_gvm, verdict, score_date
        FROM sector_ratings WHERE segment = ANY(%s) ORDER BY segment, score_date DESC
    """, (segments,))
    return {r[0]: {"score": _f(r[1]), "verdict": r[2]} for r in cur.fetchall()}


# ── cc#1484: Breakup tab — the 21 scored parameters per holding, read from gvm_scores ────────────
# (plain-English label, gvm_scores column), in the exact order api_g_score / api_v_score /
# api_m_score build their breakdowns (session_log 34218). promoter_rating is NOT a scoring input
# (it feeds the pledge red-flag only) and is deliberately excluded.
RATING_PARAMS = {
    "Growth": [
        ("Sales Growth 5Y", "sales_5y_rating"), ("Sales Growth 3Y", "sales_3y_rating"),
        ("Profit Growth 5Y", "profit_5y_rating"), ("Profit Growth 3Y", "profit_3y_rating"),
        ("YoY Sales Growth", "qoq_sales_rating"), ("YoY Profit Growth", "qoq_profit_rating"),
        ("OPM", "opm_rating"), ("OPM Expansion", "opm_exp_rating"),
        ("Fixed Asset Growth", "fa_growth_rating"), ("Inst Holding Abs", "inst_abs_rating"),
        ("Inst Holding Change", "inst_change_rating"), ("ROCE", "roce_rating"),
        ("Dividend Yield", "div_yield_rating"), ("Interest Coverage", "int_cov_rating"),
    ],
    "Value": [("PE Ratio", "pe_rating"), ("Potential Upside", "upside_rating")],
    "Momentum": [
        ("1Y Return", "ret_1y_rating"), ("3Y Return", "ret_3y_rating"),
        ("DMA 50", "dma_50_rating"), ("DMA 200", "dma_200_rating"),
        ("52W vs Index", "ret_52w_idx_rating"),
    ],
}


def _load_rating_breakup(cur, syms):
    """cc#1484: latest gvm_scores row per symbol (read-only) → {symbol: {bfsi, params:{label: rating}}}.
    Interest Coverage is forced to None for BFSI segments — the engine skips it there, so a number
    would be fabricated."""
    pairs = RATING_PARAMS["Growth"] + RATING_PARAMS["Value"] + RATING_PARAMS["Momentum"]
    cols = ", ".join(c for _, c in pairs)
    cur.execute(f"""
        SELECT DISTINCT ON (symbol) symbol, segment, {cols}
        FROM gvm_scores WHERE symbol = ANY(%s) ORDER BY symbol, score_date DESC
    """, (syms,))
    out = {}
    for row in cur.fetchall():
        sym, seg = row[0], row[1]
        bfsi = seg in BFSI_SEGMENTS
        params = {}
        for (label, _col), val in zip(pairs, row[2:]):
            params[label] = None if (bfsi and label == "Interest Coverage") else _r(val)
        out[sym] = {"bfsi": bfsi, "params": params}
    return out


def _replacements(cur, avoid_segments, held_syms):
    """Top-2 GVM peers per segment (latest gvm_history), excluding held names."""
    if not avoid_segments:
        return {}
    cur.execute("""
        SELECT DISTINCT ON (symbol) symbol, gvm_score, verdict, segment
        FROM gvm_history WHERE segment = ANY(%s) ORDER BY symbol, score_date DESC
    """, (avoid_segments,))
    by_seg = {}
    for r in cur.fetchall():
        sym, gvm, verdict, seg = r[0], _f(r[1]), r[2], r[3]
        if sym in held_syms:
            continue
        by_seg.setdefault(seg, []).append({"symbol": sym, "gvm": gvm, "verdict": verdict})
    for seg in by_seg:
        by_seg[seg] = sorted(by_seg[seg], key=lambda x: (x["gvm"] or 0), reverse=True)[:2]
    return by_seg


def _upcoming(cur, syms):
    cur.execute("""
        SELECT ticker, event_type, ex_date, (ex_date - CURRENT_DATE) AS days
        FROM earnings_calendar WHERE ticker = ANY(%s) AND ex_date >= CURRENT_DATE
        ORDER BY ex_date ASC
    """, (syms,))
    return [{"symbol": r[0], "event": r[1] or "Results", "date": str(r[2]), "days_to": r[3]}
            for r in cur.fetchall()]


def _announced(cur, syms):
    """cc#662 V1.1: holdings that ANNOUNCED results in the current quarter (earnings_calendar ex_date
    within the last 60 days). Returns {symbol: latest announced date}."""
    cur.execute("""SELECT ticker, MAX(ex_date) FROM earnings_calendar
                   WHERE ticker = ANY(%s) AND ex_date <= CURRENT_DATE
                     AND ex_date >= (CURRENT_DATE - INTERVAL '60 days')
                   GROUP BY ticker""", (syms,))
    return {r[0]: str(r[1]) for r in cur.fetchall()}


# ---- result-analysis strength chip --------------------------------------------
_STRONG = ("strong", "beat", "robust", "healthy", "outperform", "record", "surge", "jump")
_WEAK = ("weak", "miss", "decline", "fall", "drop", "muted", "disappoint", "loss", "de-grow", "degrow")


def _strength_chip(text):
    t = (text or "").lower()
    if not t.strip():
        return None
    s = sum(1 for k in _STRONG if k in t)
    w = sum(1 for k in _WEAK if k in t)
    if s > w:
        return "Strong"
    if w > s:
        return "Weak"
    return "Moderate"


# ---- the engine ----------------------------------------------------------------
def build_report(cur, pid):
    cur.execute("SELECT id, name, source, created_at, alpha_start_date FROM hr_portfolios WHERE id=%s", (pid,))
    p = cur.fetchone()
    if not p:
        return {"error": "portfolio not found"}
    alpha_start = p[4]   # cc#653: per-portfolio alpha window start (None -> earliest holding entry date)
    cur.execute("SELECT symbol, company_name, qty, avg_price FROM hr_holdings WHERE portfolio_id=%s ORDER BY id", (pid,))
    raw = cur.fetchall()
    if not raw:
        return {"error": "no holdings in portfolio"}

    syms = [r[0] for r in raw]
    gvm = _load_gvm(cur, syms)
    cmp = _load_cmp(cur, syms)
    scr = _load_screener(cur, syms)
    inp = _load_input(cur, syms)
    ath = _load_ath(cur, syms)
    # cc#1426: ONE batched segment_pe_map query for this whole report, reused per holding below —
    # a portfolio-sized loop, never a per-symbol query.
    pe_map = segment_pe_map(cur)

    # ---- per-holding assembly (current-value weighted) ----
    holdings = []
    total_current = total_invested = 0.0
    for sym, cname, qty, avg in raw:
        qty = _f(qty) or 0.0
        avg = _f(avg)
        c = cmp.get(sym)
        g = gvm.get(sym, {})
        s = scr.get(sym, {})
        i = inp.get(sym, {})
        current = (c or 0) * qty
        invested = (avg or 0) * qty
        total_current += current
        total_invested += invested
        a = ath.get(sym)
        from_ath = round((c - a) / a * 100, 1) if (c and a and a > 0) else None
        pnl_pct = round((c - avg) / avg * 100, 2) if (c and avg and avg > 0) else None
        verdict = g.get("verdict")
        _segment = g.get("segment") or i.get("segment")
        # cc#1426: screener_raw.segment_pe is dead — fall back to Scorr's own peer-avg PE for this
        # holding's segment, and carry the basis label so the report never claims it as Screener's
        # Industry PE.
        _seg_pe, _seg_pe_basis = s.get("segment_pe"), None
        if _seg_pe is None:
            _seg_pe, _seg_pe_basis = _pe_lookup(pe_map, _segment)
        holdings.append({
            "symbol": sym, "company_name": i.get("company_name") or cname,
            "cmp": _r(c), "qty": qty, "avg_price": _r(avg),
            "invested": _r(invested), "current": _r(current),
            "pnl_pct": pnl_pct, "pnl_abs": _r(current - invested),
            "segment": _segment,
            "cap": (i.get("cap") or "").title() or None,
            "g": g.get("g"), "v": g.get("v"), "m": g.get("m"), "gvm": g.get("gvm"),
            "verdict": verdict, "action": _action_for(verdict),
            "gvm_trend": g.get("gvm_trend"), "gvm_trend_delta": g.get("gvm_trend_delta"),
            "pe": s.get("pe"), "segment_pe": _seg_pe, "segment_pe_basis": _seg_pe_basis, "yield": s.get("yield"),
            "return_1y": s.get("return_1y"), "from_ath": from_ath,
            "fwd_growth": i.get("fy27_growth"),
            "result_analysis": i.get("result_analysis"),
            "result_chip": _strength_chip(i.get("result_analysis")),
            "result_fresh": i.get("ra_fresh", False),
            "sales_growth": s.get("sales_growth"), "profit_growth": s.get("profit_growth"),   # cc#662
        })

    # weights
    for h in holdings:
        h["weight"] = round((h["current"] or 0) / total_current * 100, 2) if total_current > 0 else 0.0

    # ---- cc#1484: rating breakup (Breakup tab) — weight-desc, one entry per holding ----
    _rb = _load_rating_breakup(cur, syms)
    rating_breakup = []
    for h in sorted(holdings, key=lambda x: x["weight"] or 0, reverse=True):
        e = _rb.get(h["symbol"])
        rating_breakup.append({
            "symbol": h["symbol"], "company_name": h["company_name"], "weight": h["weight"],
            "bfsi": bool(e and e["bfsi"]), "params": e["params"] if e else {},
        })

    def _wpairs(key):
        return [(h[key], h["current"]) for h in holdings]

    # ---- (1) snapshot ----
    port_1y = _wavg(_wpairs("return_1y"))
    n50 = _nifty50_1y(cur)
    n500 = _nifty500_1y(cur)

    # ---- (1b) realised P&L integration (cc#654) ----------------------------------------------------
    cur.execute("SELECT COALESCE(SUM(gainloss), 0) FROM hr_realised WHERE portfolio_id=%s", (pid,))
    realised = _f(cur.fetchone()[0]) or 0.0
    holdings_cost = total_invested
    unrealised = total_current - holdings_cost

    # ---- (1c) cc#1013 HEADLINE BASIS (founder rule, locked 11-Aug-2026) -----------------------------
    # When a portfolio carries a meta invested_amount, THAT is the headline Invested and headline P&L
    # (Rs + %) = current_value + cash - invested_amount, % on invested. It is the only honest basis when
    # per-holding buy prices are unknown: all 26 of Vishal's holdings have avg_price NULL, so the
    # holdings-cost basis is ~0 and current/cost produced +1880.5%. Holdings cost (SUM qty*avg_price) is
    # demoted to a secondary Holdings-cost/Unrealised line. With NO meta row: keep the holdings-cost
    # basis ONLY if every avg_price is known; if ANY avg_price is NULL and there is no meta row, refuse
    # to invent a % — pnl is None (em-dash + needs-invested hint downstream), never a fabricated number.
    has_null_avg = any(_f(r[3]) is None for r in raw)
    cur.execute("SELECT invested_amount, cash, tracking_date FROM hr_portfolio_meta WHERE portfolio_id=%s", (pid,))
    _meta = cur.fetchone()
    meta_invested = _f(_meta[0]) if _meta else None
    meta_cash = (_f(_meta[1]) or 0.0) if _meta else 0.0
    meta_track = _meta[2] if _meta else None
    needs_invested = False
    cash = 0.0
    if meta_invested is not None and meta_invested > 0:
        basis = "meta"
        invested_adj = meta_invested
        cash = meta_cash
        total_pnl = total_current + cash - invested_adj
        port_money_ret = round(total_pnl / invested_adj * 100.0, 2)
    elif not has_null_avg:
        basis = "holdings_cost"
        invested_adj = holdings_cost - realised   # cc#654 invested_adjusted
        total_pnl = total_current - invested_adj
        port_money_ret = round(total_pnl / invested_adj * 100.0, 2) if invested_adj else None
    else:
        basis = "unknown_cost"                     # some avg_price NULL AND no meta -> no honest %
        invested_adj = None
        total_pnl = None
        port_money_ret = None
        needs_invested = True

    # ---- (1a) alpha = MONEY return vs BOTH benchmarks (cc#656) --------------------------------------
    # cc#653 used the current-value-weighted PRICE return of surviving holdings -> survivorship bias
    # (262 closed trades, net realised -1.1L, were invisible; a -1.2% book showed +13% alpha). Now the
    # portfolio side IS the money return (total_pnl / invested_adj), and alpha is shown vs BOTH indices
    # measured over the same window from alpha_start_date -> live. Fallback start = earliest holding
    # entry date. Tile label carries the date once.
    a_start = alpha_start
    if a_start is None:
        cur.execute("SELECT MIN(created_at)::date FROM hr_holdings WHERE portfolio_id=%s", (pid,))
        _m = cur.fetchone()
        a_start = _m[0] if (_m and _m[0]) else None
    alpha_n50 = alpha_n500 = alpha_since = alpha_label = None
    if a_start is not None and port_money_ret is not None:
        n50_ret = _nifty50_return_since(cur, a_start)
        n500_ret = _nifty500_return_since(cur, a_start)
        if n50_ret is not None:
            alpha_n50 = round(port_money_ret - n50_ret, 2)
        if n500_ret is not None:
            alpha_n500 = round(port_money_ret - n500_ret, 2)
        alpha_since = str(a_start)
        try:
            alpha_label = a_start.strftime("%d %b %y").upper()
        except Exception:
            alpha_label = alpha_since

    snapshot = {
        "invested": _r(invested_adj), "current": _r(total_current),
        "cash": _r(cash), "current_total": _r(total_current + cash),   # cc#1013: value incl cash where cash>0
        "pnl_abs": _r(total_pnl) if total_pnl is not None else None,
        "pnl_pct": port_money_ret,
        "basis": basis,                                # cc#1013: meta | holdings_cost | unknown_cost
        "needs_invested_amount": needs_invested,       # cc#1013: em-dash + hint when true
        "tracking_date": str(meta_track) if meta_track else None,
        "realised_pnl": _r(realised), "unrealised_pnl": _r(unrealised), "holdings_cost": _r(holdings_cost),
        "alpha": alpha_n500, "alpha_n50": alpha_n50, "alpha_n500": alpha_n500,
        "alpha_since": alpha_since, "alpha_label": alpha_label,
        "holdings_count": len(holdings),
    }

    # ---- (2) ratings ----
    wg, wv, wm, wq = _wavg(_wpairs("g")), _wavg(_wpairs("v")), _wavg(_wpairs("m")), _wavg(_wpairs("gvm"))
    overall = wq
    _legs = {"Growth": wg, "Value": wv, "Momentum": wm}
    _soft = min((k for k in _legs if _legs[k] is not None), key=lambda k: _legs[k], default=None)
    ratings = {"growth": _r(wg), "value": _r(wv), "momentum": _r(wm), "quality": _r(wq),
               "overall": _r(overall), "verdict": _band_from_score(overall),
               "insight": (f"{_soft} is the softest leg at {_legs[_soft]:.1f} — "
                           + ("quality-heavy but late-trend." if _soft == "Momentum" else
                              "watch this dimension.")) if _soft else ""}

    # ---- (3) gainers / losers ----
    ranked = [h for h in holdings if h["pnl_pct"] is not None]
    ranked.sort(key=lambda h: h["pnl_pct"], reverse=True)
    _slim = lambda h: {"symbol": h["symbol"], "company_name": h["company_name"],
                       "pnl_pct": h["pnl_pct"], "pnl_abs": h["pnl_abs"], "cmp": h["cmp"], "weight": h["weight"]}
    gainers = [_slim(h) for h in ranked if h["pnl_pct"] > 0][:5]
    losers = [_slim(h) for h in reversed(ranked) if h["pnl_pct"] < 0][:5]

    # ---- (4) winners vs losers ----
    win = sum(1 for h in ranked if h["pnl_pct"] > 0)
    loss = sum(1 for h in ranked if h["pnl_pct"] < 0)
    wl_ratio = round(win / loss, 2) if loss > 0 else (float(win) if win else 0.0)
    wl_insight = (f"{win} winners vs {loss} losers — "
                  + ("a healthy hit-rate; let winners run." if win > loss else
                     ("evenly split; conviction review due." if win == loss else
                      "more losers than winners; prune the laggards.")))

    # ---- (5) cap diversification ----
    cap_bands, cap_w = {}, {}
    for h in holdings:
        cap_bands[h["cap"] or "Unknown"] = cap_bands.get(h["cap"] or "Unknown", 0) + 1
        cap_w[h["cap"] or "Unknown"] = round(cap_w.get(h["cap"] or "Unknown", 0) + (h["weight"] or 0), 2)
    top_cap = max(cap_w, key=cap_w.get) if cap_w else None
    cap_insight = (f"{top_cap} caps dominate at {cap_w.get(top_cap, 0):.0f}% — "
                   + ("well spread across sizes." if (cap_w.get(top_cap, 0) < 60) else
                      "concentrated; consider size diversification.")) if top_cap else "No cap data."

    # ---- (6) quality distribution ----
    q_bands, q_w = {}, {}
    for h in holdings:
        b = h["verdict"] or "Unrated"
        q_bands[b] = q_bands.get(b, 0) + 1
        q_w[b] = round(q_w.get(b, 0) + (h["weight"] or 0), 2)
    strong_w = round(q_w.get("Excellent", 0) + q_w.get("Good", 0), 1)
    weak_w = round(q_w.get("Weak", 0), 1)
    q_insight = (f"{strong_w:.0f}% in Good/Excellent quality, {weak_w:.0f}% in Weak — "
                 + ("quality-tilted book." if strong_w >= 50 else
                    "quality is thin; upgrade the Weak sleeve."))

    # ---- (7) benchmark ----
    benchmark = {"portfolio_1y": _r(port_1y), "nifty50_1y": n50, "nifty500_1y": n500}

    # ---- (8) valuation + (9) yield ----
    segs = sorted({h["segment"] for h in holdings if h["segment"]})
    seg_aggs = _segment_aggs(cur, segs)
    mkt_pe, mkt_yield = _market_pe_yield(cur)
    seg_w = {}
    for h in holdings:
        if h["segment"]:
            seg_w[h["segment"]] = seg_w.get(h["segment"], 0) + (h["weight"] or 0)
    sector_pe = _wavg([(seg_aggs.get(s, {}).get("pe"), w) for s, w in seg_w.items()])
    sector_yield = _wavg([(seg_aggs.get(s, {}).get("yield"), w) for s, w in seg_w.items()])
    port_pe = _wavg(_wpairs("pe"))
    _val_ins = ""
    if port_pe is not None and mkt_pe is not None and sector_pe is not None:
        vs_n = "premium to" if port_pe > mkt_pe else "discount to"
        vs_s = "discount to" if port_pe < sector_pe else "premium to"
        _val_ins = f"{vs_n.capitalize()} Nifty, {vs_s} own sectors — " + (
            "growth-priced, not bubble-priced." if port_pe < sector_pe else "richly valued; demand growth.")
    valuation = {"portfolio_pe": _r(port_pe), "sector_pe": _r(sector_pe),
                 "nifty_pe": _r(mkt_pe), "nifty_pe_note": "large-cap median proxy", "insight": _val_ins}
    yield_sec = {"portfolio_yield": _r(_wavg(_wpairs("yield"))), "sector_yield": _r(sector_yield),
                 "nifty_yield": _r(mkt_yield), "nifty_yield_note": "large-cap median proxy"}

    # ---- (10) sector strip + table ----
    sr = _sector_ratings(cur, segs)
    sector_strip = sorted([{"segment": s, "weight": round(w, 2)} for s, w in seg_w.items()],
                          key=lambda x: x["weight"], reverse=True)
    sector_table = []
    for s in sector_strip:
        seg = s["segment"]
        r = sr.get(seg, {})
        v = r.get("verdict")
        call = ("Strong" if v in ("Excellent", "Good") else ("Weak" if v == "Weak" else "Neutral")) if v else "—"
        sector_table.append({"segment": seg, "weight": s["weight"], "score": _r(r.get("score")),
                             "call": call})
    top_seg = sector_strip[0] if sector_strip else None
    sector_insight = (f"{top_seg['segment']} is the largest sleeve at {top_seg['weight']:.0f}% — "
                      + ("balanced sector mix." if top_seg["weight"] < 35 else
                         "high single-sector concentration; watch correlated drawdowns.")) if top_seg else "No sector data."

    # ---- (11) replacement engine ----
    avoid = [h for h in holdings if h["verdict"] == "Weak"]
    avoid_segs = sorted({h["segment"] for h in avoid if h["segment"]})
    peers = _replacements(cur, avoid_segs, set(syms))
    replacements = []
    for h in avoid:
        cand = peers.get(h["segment"], [])
        if cand:
            replacements.append({"holding": h["symbol"], "segment": h["segment"],
                                 "action": h["action"], "peers": cand})
    replacement_note = ("Peer ideas are same-segment GVM leaders for research framing only — "
                        "not buy/sell advice; do your own due diligence.")

    # ---- (12) upcoming results ----
    upcoming = _upcoming(cur, syms)

    # ---- (13) latest result analysis (only results < 45 days old, per template) ----
    result_analysis = [{"symbol": h["symbol"], "company_name": h["company_name"],
                        "analysis": h["result_analysis"], "chip": h["result_chip"]}
                       for h in holdings if h["result_analysis"] and h["result_fresh"]]

    # ---- (13b) cc#662 / FORMAT_V1.1: Results-This-Quarter compact table (replaces the analysis pane
    # in the client PDF). Holdings that announced this quarter (earnings_calendar <=60d) OR carry fresh
    # result text; columns Sales Growth % | Profit Growth % | FY27 Est. Announced-not-yet-extracted rows
    # render with a pending marker — never silently dropped. Holdings not yet announced are absent. ----
    announced = _announced(cur, syms)
    results_tq = []
    for h in holdings:
        adate = announced.get(h["symbol"])
        if not adate and not h.get("result_fresh"):
            continue
        sg, pg, fy27 = h.get("sales_growth"), h.get("profit_growth"), h.get("fwd_growth")
        results_tq.append({"symbol": h["symbol"], "sales_growth": sg, "profit_growth": pg,
                           "fy27_est": fy27, "announced": adate,
                           "pending": (sg is None and pg is None)})
    results_tq.sort(key=lambda x: (x["pending"], x["symbol"]))
    results_yet_to_report = max(0, len(holdings) - len(results_tq))

    # ---- red flags ----
    red_flags = []
    for h in holdings:
        s = scr.get(h["symbol"], {})
        pledge = None
        if s.get("promoter") is not None and s.get("unpledged") is not None:
            pledge = round(s["promoter"] - s["unpledged"], 2)
        if pledge is not None and pledge > 0:
            red_flags.append({"symbol": h["symbol"], "flag": "Pledged shares", "detail": f"{pledge:.1f}% of equity pledged"})
        if s.get("fii_change") is not None and s["fii_change"] < -0.5:
            red_flags.append({"symbol": h["symbol"], "flag": "FII selling", "detail": f"FII stake down {s['fii_change']:.1f}% QoQ"})
        if h["from_ath"] is not None and h["from_ath"] < -40:
            red_flags.append({"symbol": h["symbol"], "flag": "Deep drawdown", "detail": f"{h['from_ath']:.0f}% from all-time high"})

    # ---- (16) highlights (from the section insights) ----
    highlights = []
    if snapshot["pnl_pct"] is not None:
        highlights.append(f"Portfolio is {'up' if snapshot['pnl_pct'] >= 0 else 'down'} "
                          f"{abs(snapshot['pnl_pct']):.1f}% (₹{snapshot['pnl_abs']:,.0f} P&L) on ₹{snapshot['invested']:,.0f} invested.")
    if ratings["verdict"]:
        highlights.append(f"Overall Scorr quality rating: {ratings['overall']}/10 ({ratings['verdict']}).")
    if snapshot.get("alpha_n50") is not None or snapshot.get("alpha_n500") is not None:
        _since = f" since {snapshot['alpha_label']}" if snapshot.get("alpha_label") else ""
        _parts = []
        if snapshot.get("alpha_n50") is not None:
            _parts.append(f"{snapshot['alpha_n50']:+.1f}% vs Nifty 50")
        if snapshot.get("alpha_n500") is not None:
            _parts.append(f"{snapshot['alpha_n500']:+.1f}% vs Nifty 500")
        highlights.append(f"Money-return alpha{_since}: " + ", ".join(_parts) + ".")
    highlights.append(wl_insight)
    highlights.append(q_insight)
    highlights.append(cap_insight)
    highlights.append(sector_insight)
    if avoid:
        highlights.append(f"{len(avoid)} holding(s) flagged Avoid-Exit on GVM — replacement peers suggested.")
    if red_flags:
        highlights.append(f"{len(red_flags)} red flag(s) detected across pledge/FII/drawdown screens.")

    # ---- (17) expert take (rule-generated) ----
    take = []
    if snapshot["pnl_pct"] is not None:
        take.append(f"This {len(holdings)}-stock book is {'in profit' if snapshot['pnl_pct'] >= 0 else 'underwater'} "
                    f"at {snapshot['pnl_pct']:+.1f}% overall.")
    if ratings["verdict"]:
        take.append(f"On Scorr's GVM framework the portfolio scores {ratings['overall']}/10 — a {ratings['verdict']} "
                    f"quality profile (Growth {ratings['growth']}, Value {ratings['value']}, Momentum {ratings['momentum']}).")
    if benchmark["portfolio_1y"] is not None and n50 is not None:
        rel = "ahead of" if benchmark["portfolio_1y"] > n50 else "behind"
        take.append(f"Over 1 year it is {rel} the NIFTY50 ({benchmark['portfolio_1y']:+.1f}% vs {n50:+.1f}%).")
    if top_seg and top_seg["weight"] >= 35:
        take.append(f"Concentration risk sits in {top_seg['segment']} ({top_seg['weight']:.0f}%).")
    if avoid:
        take.append(f"Priority action: review the {len(avoid)} Avoid-Exit name(s) and rotate toward the higher-GVM peers listed.")
    else:
        take.append("No Avoid-Exit names — hold quality and add on weakness.")
    expert_take = " ".join(take)

    return {
        "portfolio": {"id": p[0], "name": p[1], "source": p[2], "created_at": str(p[3])},
        "snapshot": snapshot,
        "ratings": ratings,
        "gainers": gainers,
        "losers": losers,
        "winners_losers": {"winners": win, "losers": loss, "ratio": wl_ratio, "insight": wl_insight},
        "cap_bands": {"counts": cap_bands, "weights": cap_w, "insight": cap_insight},
        "quality_bands": {"counts": q_bands, "weights": q_w, "insight": q_insight},
        "benchmark": benchmark,
        "valuation": valuation,
        "yield": yield_sec,
        "sector": {"strip": sector_strip, "table": sector_table, "insight": sector_insight},
        "replacements": replacements,
        "replacement_note": replacement_note,
        "upcoming": upcoming,
        "result_analysis": result_analysis,
        "results_this_quarter": results_tq,             # cc#662 V1.1 compact table
        "results_yet_to_report": results_yet_to_report,  # cc#662 V1.1 footnote
        "red_flags": red_flags,
        "holdings": holdings,
        "rating_breakup": rating_breakup,   # cc#1484 Breakup tab (21 params × holdings)
        "highlights": highlights,
        "expert_take": expert_take,
    }


@router.get("/api/health/report/{portfolio_id}")
def health_report(portfolio_id: int):
    """Full Scorr-native Portfolio Health Report for a saved portfolio."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            return build_report(cur, portfolio_id)
    except Exception as e:
        return {"error": f"report engine failed: {str(e)[:300]}"}


@router.post("/api/health/refresh/{portfolio_id}")
def health_refresh(portfolio_id: int):
    """Recompute the report (all metrics are computed live, so this is a fresh rebuild)."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            return build_report(cur, portfolio_id)
    except Exception as e:
        return {"error": f"report engine failed: {str(e)[:300]}"}


# ── boot self-test (cc#398): exercises the real engine against a seeded 5-stock portfolio when
#    app_config key='hr_selftest'='run'; writes a JSON summary back. Guarded/no-op otherwise. ──
@router.on_event("startup")
def _hr_selftest():
    def _run():
        import json as _j
        import threading  # noqa
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config WHERE key='hr_selftest'")
                row = cur.fetchone()
                if not row or row[0] != 'run':
                    return
                cur.execute("UPDATE app_config SET value='running', updated_at=NOW() WHERE key='hr_selftest'")
                conn.commit()
                cur.execute("DELETE FROM hr_portfolios WHERE name='__hr_selftest__'")
                cur.execute("INSERT INTO hr_portfolios (name, source) VALUES ('__hr_selftest__','upload') RETURNING id")
                spid = cur.fetchone()[0]
                for sym, qty, avg in [('RELIANCE', 50, 2400), ('TCS', 20, 3200), ('HDFCBANK', 40, 1500),
                                      ('INFY', 30, 1400), ('ITC', 100, 420)]:
                    cur.execute("INSERT INTO hr_holdings (portfolio_id, symbol, qty, avg_price, resolved) "
                                "VALUES (%s, %s, %s, %s, TRUE)", (spid, sym, qty, avg))
                conn.commit()
                rep = build_report(cur, spid)
                summary = {"ok": "error" not in rep, "error": rep.get("error"),
                           "sections": sorted(list(rep.keys())),
                           "snapshot": rep.get("snapshot"), "ratings": rep.get("ratings"),
                           "benchmark": rep.get("benchmark"), "valuation": rep.get("valuation"),
                           "holdings_n": len(rep.get("holdings", [])),
                           "flags_n": len(rep.get("red_flags", [])),
                           "reps_n": len(rep.get("replacements", [])),
                           "upcoming_n": len(rep.get("upcoming", [])),
                           "highlights_n": len(rep.get("highlights", []))}
                cur.execute("UPDATE app_config SET value=%s, updated_at=NOW() WHERE key='hr_selftest'",
                            (_j.dumps(summary, default=str)[:6000],))
                cur.execute("DELETE FROM hr_portfolios WHERE id=%s", (spid,))
                conn.commit()
        except Exception as e:
            try:
                with _conn() as c2, c2.cursor() as cu2:
                    cu2.execute("UPDATE app_config SET value=%s, updated_at=NOW() WHERE key='hr_selftest'",
                                (f"error: {str(e)[:800]}",))
                    c2.commit()
            except Exception:
                pass
    import threading
    threading.Thread(target=_run, daemon=True).start()
