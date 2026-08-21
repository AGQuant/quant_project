"""invest_check_v2.py — cc#1174: Investment Check V2, the weighted /10 engine.

SPEC: session_log 27979 (INVESTMENT_CHECK_V2_LOCKED, founder-locked 20-Aug-2026 ~23:05 IST).
Nine components, weights summing to 100, graded not binary, zero-veto, bands on the 10-scale.

V1 IS NOT TOUCHED. investment_check.py (v3.0 gate+conviction) keeps serving /api/investment-check
byte-for-byte until the founder retires it. This is a NEW file on a NEW endpoint, and it does not
import v1's scorer — only its BFSI exemption keywords, which are a founder list rather than logic.

CONTEXT ISOLATION (session_log id 244, carried by 324, restated in 27979)
    Investment Check shares the SCORE GRAMMAR with Trade Check — /10, graded, zero-veto, report
    ordered heaviest-first — and shares nothing else. Not a rule, not a weight table, not a data
    path. So:
      * weights live in ic_rule_weights, NOT tc_rule_weights;
      * DMA stack, 1-year returns, the monthly RSI and the segment month are computed HERE from
        raw_prices, not read off v8_metrics, even though v8_metrics carries columns with those
        names. Borrowing V8's numbers would be borrowing V8's data path, which is the thing the
        isolation rule forbids. It also keeps every component reproducible from one table.
    The single exception is gvm_scores / gvm_history, which is Investment Check's own primary
    source and always has been.

THE DENOMINATOR IS THE HONEST PART
    Score = 10 * sum(weight * credit) / sum(weight over COMPUTABLE components).

    A component that cannot be computed for a symbol — gvm_history too shallow for a 90-day delta,
    fewer than 200 sessions for a DMA stack — is EXCLUDED from that symbol's denominator and named
    in the payload. It is never scored zero. A missing measurement and a bad measurement are
    different statements, and a silent zero turns the first into the second: it would read as "this
    company fails on quality direction" when the truth is "we have not been watching long enough".
    Every response carries computable_weight, excluded_components and a per-component reason.

GRADING, exactly as locked (credit is a fraction of the component's own weight):
    gvm_level     w40  >=8 -> 1.00 | 7-8 -> 0.80 | 6-7 -> 0.50 | <6 -> 0.20
    fundamentals  w12  count of 5 passing / 5
    dgvm_90d      w10  >=+0.5 -> 1.00 | >0 -> 0.50 | <=0 -> 0
    dma_stack     w8   both legs -> 1.00 | exactly one -> 0.50 | none -> 0
    accumulation  w8   ratio >=1.1 -> 1.00 | 0.9-1.1 -> 0.50 | <0.9 -> 0
    s1_reclaim    w8   touched within 3 sessions -> 1.00 | within 7 -> 0.50, and ONLY if CMP >= PP
    rs_1y         w8   excess >0 -> 1.00 | >=-3pp -> 0.50 | else 0
    segment_month w4   avg >0 -> 1.00 | >=-0.5 -> 0.50 | else 0
    rsi_monthly   w2   35-75 -> 1.00 | else 0

    The S1 gate is COMPONENT-LEVEL only. It zeroes its own component and never vetoes the verdict —
    zero-veto is the locked grammar (cc#677 for Trade Check, restated for Investment Check in
    27979).

BANDS: STRONG_BUY >= 8.4 | ACCUMULATE >= 6.5 | WATCH >= 5.0 | AVOID < 5.0.

DISPLAY: components come back PRE-SORTED BY WEIGHT DESC with the weight on every row (founder
    display ruling, 27977 and 27979). The order is applied in the ENGINE, not in each surface, so
    no consumer can drift from the ruling by forgetting to sort.

IC V2 INFORMS, IT NEVER ACTS. Nothing here reads or writes a basket, a position or an exit.
"""

import os
import logging
from datetime import timedelta
from typing import Optional, List, Dict, Any

import psycopg
from fastapi import APIRouter, Query

from investment_check import BFSI_KEYWORDS   # founder's exemption list, carried over verbatim

router = APIRouter()
log = logging.getLogger("invest_check_v2")

VERSION = "IC-V2"
SPEC_REF = "session_log 27979 (INVESTMENT_CHECK_V2_LOCKED, founder-locked 20-Aug-2026)"

_STRONG = 8.4
_ACCUM = 6.5
_WATCH = 5.0

# The founder lock, in code, used ONLY when the registry cannot be read. It is not a second source
# of truth: when this fallback is used the response says weighted=False and names the reason, so a
# score computed off it can never be mistaken for a registry-calibrated one.
_LOCKED_WEIGHTS = {
    "gvm_level": 40.0, "fundamentals": 12.0, "dgvm_90d": 10.0, "dma_stack": 8.0,
    "accumulation": 8.0, "s1_reclaim": 8.0, "rs_1y": 8.0, "segment_month": 4.0,
    "rsi_monthly": 2.0,
}
_LABELS = {
    "gvm_level": "GVM level (graded)",
    "fundamentals": "Fundamentals cluster (5 checks)",
    "dgvm_90d": "GVM direction, 90 days",
    "dma_stack": "DMA stack 20>50>200",
    "accumulation": "Accumulation, 21 sessions",
    "s1_reclaim": "S1 touch + reclaim",
    "rs_1y": "RS 1 year vs NIFTY50",
    "segment_month": "Segment month",
    "rsi_monthly": "Monthly RSI guard (35-75)",
}

BENCHMARK = "NIFTY50"          # the index the 1-year excess is measured against, per 27979


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _r(v, dp=2):
    return round(v, dp) if v is not None else None


def is_bfsi(segment: str) -> bool:
    seg = (segment or "").lower()
    return any(k in seg for k in BFSI_KEYWORDS)


# ── registry ─────────────────────────────────────────────────────────────────────────────────
_W_CACHE: Dict[str, Any] = {"at": None, "val": None}


def _weights(cur) -> Optional[Dict[str, float]]:
    """Active component weights from ic_rule_weights. None when the registry cannot be read, so the
    caller can fall back AND say that it did — a fallback that hides itself is the whole problem."""
    try:
        cur.execute("SELECT rule_key, weight FROM ic_rule_weights "
                    "WHERE bucket='invest' AND active = TRUE")
        rows = cur.fetchall()
    except Exception as e:
        log.warning("ic_rule_weights unreadable: %s", e)
        return None
    if not rows:
        return None
    return {k: float(w) for k, w in rows}


# ── component maths, each one small enough to check by eye ───────────────────────────────────
def _grade_gvm(gvm):
    if gvm is None:
        return None
    if gvm >= 8:
        return 1.0
    if gvm >= 7:
        return 0.8
    if gvm >= 6:
        return 0.5
    return 0.2


def _grade_dgvm(delta):
    if delta is None:
        return None
    if delta >= 0.5:
        return 1.0
    return 0.5 if delta > 0 else 0.0


def _dma(closes: List[float], n: int) -> Optional[float]:
    return (sum(closes[-n:]) / n) if len(closes) >= n else None


def _rsi(values: List[float], period: int = 14) -> Optional[float]:
    """Wilder RSI. Needs period+1 points; returns None rather than a number off a short series."""
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for a, b in zip(values, values[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def _monthly_closes(bars) -> List[float]:
    """TRUE calendar monthly closes (27979): the LAST traded close of each calendar month, in order.
    Not a 21-bar resample — a resample drifts against the calendar and the founder asked for the
    calendar. The current, incomplete month is included: its close is the latest price, which is
    what a guard against a blow-off top has to look at."""
    out, cur_key, cur_close = [], None, None
    for d, _o, _h, _l, c, _v in bars:
        key = (d.year, d.month)
        if cur_key is None:
            cur_key, cur_close = key, c
        elif key != cur_key:
            out.append(cur_close)
            cur_key, cur_close = key, c
        else:
            cur_close = c
    if cur_close is not None:
        out.append(cur_close)
    return out


def _ret_over(bars, days: int) -> Optional[float]:
    """Percent return from the last bar at or before (last_date - days) to the last bar.

    Anchored on the series' OWN last date, not on today. A stock that stopped trading three days
    ago would otherwise silently get a 368-day window while its peers get 365."""
    if len(bars) < 2:
        return None
    last_d, last_c = bars[-1][0], bars[-1][4]
    target = last_d - timedelta(days=days)
    prior = [b for b in bars if b[0] <= target]
    if not prior or not last_c:
        return None
    base = prior[-1][4]
    if not base:
        return None
    return (last_c / base - 1.0) * 100.0


def _accumulation(bars) -> Optional[Dict[str, Any]]:
    """21-session average volume on up-closes over average volume on down-closes."""
    w = bars[-22:]
    if len(w) < 22:
        return None                      # needs 21 sessions PLUS the prior close to judge direction
    up_v, dn_v = [], []
    for prev, cur in zip(w, w[1:]):
        if cur[4] is None or prev[4] is None or cur[5] is None:
            continue
        if cur[4] > prev[4]:
            up_v.append(float(cur[5]))
        elif cur[4] < prev[4]:
            dn_v.append(float(cur[5]))
        # an unchanged close is neither accumulation nor distribution, so it votes in neither
    if not up_v and not dn_v:
        return None
    if not dn_v:
        # No down-close in 21 sessions. The ratio is undefined, but the reading is not ambiguous —
        # every session's volume was accumulation. Full credit, and the payload says why.
        return {"ratio": None, "credit": 1.0, "note": "no down-close session in the window"}
    if not up_v:
        return {"ratio": 0.0, "credit": 0.0, "note": "no up-close session in the window"}
    ratio = (sum(up_v) / len(up_v)) / (sum(dn_v) / len(dn_v))
    credit = 1.0 if ratio >= 1.1 else (0.5 if ratio >= 0.9 else 0.0)
    return {"ratio": ratio, "credit": credit, "note": None}


def _pivot_from(window):
    """Classic pivot off a 5-session window: PP=(H+L+C)/3, S1=2*PP-H, using the window's high, low
    and its LAST close."""
    highs = [b[2] for b in window if b[2] is not None]
    lows = [b[3] for b in window if b[3] is not None]
    close = window[-1][4]
    if not highs or not lows or close is None:
        return None, None
    h, l = max(highs), min(lows)
    pp = (h + l + close) / 3.0
    return pp, (2.0 * pp - h)


def _s1_reclaim(bars) -> Optional[Dict[str, Any]]:
    """Touched S1 recently AND back above the pivot now.

    The level a session could touch must be known BEFORE that session, so each session i is tested
    against the pivot built from the 5 sessions ending at i-1. Testing a past low against today's
    pivot would be reading the future backwards into the chart.
    """
    if len(bars) < 12:
        return None
    pp_now, _s1_now = _pivot_from(bars[-5:])
    cmp_now = bars[-1][4]
    if pp_now is None or cmp_now is None:
        return None
    touched_at = None                                  # sessions ago, 1 = the latest session
    for back in range(1, 8):                           # the last 7 sessions
        i = len(bars) - back
        window = bars[i - 5:i]
        if len(window) < 5:
            break
        _pp, s1 = _pivot_from(window)
        low = bars[i][3]
        if s1 is None or low is None:
            continue
        if low <= s1 * 1.005:
            touched_at = back
            break
    above_pp = cmp_now >= pp_now
    if touched_at is None:
        credit = 0.0
    elif not above_pp:
        credit = 0.0                                   # component-level gate only, never a veto
    else:
        credit = 1.0 if touched_at <= 3 else 0.5
    return {"credit": credit, "touched_sessions_ago": touched_at,
            "pp": pp_now, "cmp": cmp_now, "above_pp": above_pp}


# ── loaders ──────────────────────────────────────────────────────────────────────────────────
_BARS_SQL = """
    SELECT price_date, open, high, low, close, volume
    FROM raw_prices
    WHERE symbol = %s AND close IS NOT NULL
      AND price_date >= CURRENT_DATE - INTERVAL '3 years'
    ORDER BY price_date
"""


def _bars(cur, symbol):
    cur.execute(_BARS_SQL, (symbol,))
    return [(d, _f(o), _f(h), _f(l), _f(c), _f(v)) for d, o, h, l, c, v in cur.fetchall()]


def _segment_month(cur, segment):
    """Average 1-month return across the segment's members, computed from raw_prices in one pass.

    Self is INCLUDED, and takes no symbol argument so it cannot quietly become otherwise: 27979
    calls this the SEGMENT's month, and an average that changes depending on who is asking is not
    the segment's month.
    """
    cur.execute("""
        WITH mem AS (SELECT symbol FROM gvm_scores WHERE segment = %s),
        px AS (
            SELECT r.symbol, r.price_date, r.close,
                   ROW_NUMBER() OVER (PARTITION BY r.symbol ORDER BY r.price_date DESC) rn_new,
                   ROW_NUMBER() OVER (PARTITION BY r.symbol ORDER BY r.price_date DESC)
                     FILTER (WHERE r.price_date <= CURRENT_DATE - 30) rn_old
            FROM raw_prices r JOIN mem m ON m.symbol = r.symbol
            WHERE r.close IS NOT NULL AND r.price_date >= CURRENT_DATE - INTERVAL '120 days'
        ),
        pair AS (
            SELECT n.symbol, n.close AS c_new, o.close AS c_old
            FROM (SELECT symbol, close FROM px WHERE rn_new = 1) n
            JOIN (SELECT symbol, close FROM px WHERE rn_old = 1) o USING (symbol)
            WHERE o.close > 0
        )
        SELECT AVG((c_new / c_old - 1) * 100), COUNT(*) FROM pair
    """, (segment,))
    row = cur.fetchone() or (None, 0)
    return _f(row[0]), int(row[1] or 0)


def _peer_segment_avgs(cur, segment, self_symbol):
    """Full-segment averages for the two peer-relative fundamentals, self EXCLUDED.

    27979 says peer-SEGMENT average, which is the whole segment — not v1's top-3-by-GVM average.
    That is a real difference from v1 and it is deliberate: V2 asks "is this company above its
    segment", where v1 asked "does it beat the segment's leaders".
    """
    cur.execute("""SELECT AVG(s.sales_growth_5y), AVG(s.opm), COUNT(*)
                   FROM gvm_scores g JOIN screener_raw s ON g.symbol = s.nse_code
                   WHERE g.segment = %s AND g.symbol <> %s""", (segment, self_symbol))
    r = cur.fetchone() or (None, None, 0)
    return _f(r[0]), _f(r[1]), int(r[2] or 0)


def _load(cur, symbol):
    cur.execute("SELECT gvm_score, segment, verdict FROM gvm_scores WHERE symbol = %s", (symbol,))
    g = cur.fetchone()
    if not g:
        return None, f"{symbol} is not in the GVM universe"
    cur.execute('''SELECT sales_growth_5y, profit_growth_5y, opm, roce,
                          qoq_sales_growth, qoq_profit_growth, market_cap, company_name
                   FROM screener_raw WHERE nse_code = %s''', (symbol,))
    s = cur.fetchone()

    # dGVM: the nearest row at or before 90 days back. If there is none, the OLDEST row is used
    # only when it is at least 80 days old (27979's tolerance floor) — and the actual lookback is
    # reported, so nobody reads a 82-day delta as a 90-day one.
    cur.execute("""SELECT score_date, gvm_score FROM gvm_history
                   WHERE symbol = %s AND gvm_score IS NOT NULL
                     AND score_date <= CURRENT_DATE - 90
                   ORDER BY score_date DESC LIMIT 1""", (symbol,))
    gh = cur.fetchone()
    if not gh:
        cur.execute("""SELECT score_date, gvm_score FROM gvm_history
                       WHERE symbol = %s AND gvm_score IS NOT NULL
                         AND score_date <= CURRENT_DATE - 80
                       ORDER BY score_date ASC LIMIT 1""", (symbol,))
        gh = cur.fetchone()

    return {
        "symbol": symbol, "gvm": _f(g[0]), "segment": g[1], "gvm_verdict": g[2],
        "sales_5y": _f(s[0]) if s else None, "profit_5y": _f(s[1]) if s else None,
        "opm": _f(s[2]) if s else None, "roce": _f(s[3]) if s else None,
        "qoq_sales": _f(s[4]) if s else None, "qoq_profit": _f(s[5]) if s else None,
        "market_cap": _f(s[6]) if s else None, "company": s[7] if s else None,
        "has_screener": bool(s),
        "gvm_then": _f(gh[1]) if gh else None,
        "gvm_then_date": gh[0] if gh else None,
    }, None


# ── the scorer ───────────────────────────────────────────────────────────────────────────────
def _band(score10):
    if score10 is None:
        return "NO_DATA"
    if score10 >= _STRONG:
        return "STRONG_BUY"
    if score10 >= _ACCUM:
        return "ACCUMULATE"
    if score10 >= _WATCH:
        return "WATCH"
    return "AVOID"


def compute(cur, symbol) -> Dict[str, Any]:
    rec, err = _load(cur, symbol)
    if err:
        return {"error": err, "symbol": symbol, "version": VERSION}

    bars = _bars(cur, symbol)
    closes = [b[4] for b in bars if b[4] is not None]
    reg = _weights(cur)
    weights = reg or dict(_LOCKED_WEIGHTS)
    weighted = reg is not None

    comps: List[Dict[str, Any]] = []

    def add(key, credit, detail, skip=None):
        """credit None => not computable. `skip` says WHY, in words a reader can act on."""
        comps.append({"key": key, "label": _LABELS.get(key, key),
                      "weight": float(weights.get(key, _LOCKED_WEIGHTS.get(key, 0))),
                      "credit": credit, "computable": credit is not None,
                      "excluded_reason": skip, "detail": detail})

    # 1 — GVM level, w40
    add("gvm_level", _grade_gvm(rec["gvm"]), {"gvm": rec["gvm"], "verdict": rec["gvm_verdict"]},
        None if rec["gvm"] is not None else "no GVM score for this symbol")

    # 2 — fundamentals cluster, w12
    if not rec["has_screener"]:
        add("fundamentals", None, {}, "no screener_raw row for this symbol")
    else:
        seg_sales, seg_opm, peer_n = _peer_segment_avgs(cur, rec["segment"], symbol)
        bfsi = is_bfsi(rec["segment"])
        checks = [
            {"name": "Sales 5Y above segment", "value": rec["sales_5y"], "vs": _r(seg_sales),
             "pass": (None if rec["sales_5y"] is None or seg_sales is None
                      else rec["sales_5y"] > seg_sales)},
            {"name": "Profit 5Y above 10%", "value": rec["profit_5y"], "vs": 10,
             "pass": (None if rec["profit_5y"] is None else rec["profit_5y"] > 10)},
            {"name": "OPM above segment", "value": rec["opm"], "vs": _r(seg_opm),
             "pass": (None if rec["opm"] is None or seg_opm is None else rec["opm"] > seg_opm)},
            {"name": "ROCE 15 or better" + (" (BFSI exempt)" if bfsi else ""),
             "value": rec["roce"], "vs": 15,
             "pass": (True if bfsi else (None if rec["roce"] is None else rec["roce"] >= 15))},
            {"name": "QoQ sales and profit both positive",
             "value": [rec["qoq_sales"], rec["qoq_profit"]], "vs": 0,
             "pass": (None if rec["qoq_sales"] is None or rec["qoq_profit"] is None
                      else (rec["qoq_sales"] > 0 and rec["qoq_profit"] > 0))},
        ]
        passed = sum(1 for c in checks if c["pass"] is True)
        add("fundamentals", passed / 5.0,
            {"passed": passed, "of": 5, "checks": checks,
             "segment_peers": peer_n, "segment_sales_avg": _r(seg_sales),
             "segment_opm_avg": _r(seg_opm), "bfsi_exempt": bfsi})

    # 3 — dGVM 90d, w10
    if rec["gvm_then"] is None or rec["gvm"] is None:
        add("dgvm_90d", None, {},
            "gvm_history is shallower than 80 days for this symbol, so a 90-day delta cannot be "
            "measured — excluded from the denominator, not scored zero")
    else:
        delta = rec["gvm"] - rec["gvm_then"]
        add("dgvm_90d", _grade_dgvm(delta),
            {"delta": _r(delta), "gvm_now": rec["gvm"], "gvm_then": rec["gvm_then"],
             "as_of": rec["gvm_then_date"].isoformat() if rec["gvm_then_date"] else None,
             "direction": ("rising" if delta > 0 else ("flat" if delta == 0 else "fading"))})

    # 4 — DMA stack, w8
    d20, d50, d200 = _dma(closes, 20), _dma(closes, 50), _dma(closes, 200)
    if None in (d20, d50, d200):
        add("dma_stack", None, {"sessions": len(closes)},
            f"needs 200 sessions of closes, has {len(closes)}")
    else:
        legs = int(d20 > d50) + int(d50 > d200)
        add("dma_stack", 1.0 if legs == 2 else (0.5 if legs == 1 else 0.0),
            {"dma20": _r(d20), "dma50": _r(d50), "dma200": _r(d200), "legs": legs})

    # 5 — accumulation, w8
    acc = _accumulation(bars)
    if acc is None:
        add("accumulation", None, {"sessions": len(bars)},
            f"needs 21 sessions with volume, has {len(bars)}")
    else:
        add("accumulation", acc["credit"],
            {"up_down_vol_ratio": _r(acc["ratio"]), "note": acc["note"]})

    # 6 — S1 touch + reclaim, w8
    s1 = _s1_reclaim(bars)
    if s1 is None:
        add("s1_reclaim", None, {"sessions": len(bars)},
            f"needs 12 sessions for rolling 5-day pivots, has {len(bars)}")
    else:
        add("s1_reclaim", s1["credit"],
            {"touched_sessions_ago": s1["touched_sessions_ago"], "pp": _r(s1["pp"]),
             "cmp": _r(s1["cmp"]), "above_pivot": s1["above_pp"],
             "gate": "credit only when CMP is at or above the pivot"})

    # 7 — RS 1 year vs NIFTY50, w8
    stock_1y = _ret_over(bars, 365)
    cur.execute(_BARS_SQL, (BENCHMARK,))
    bench_bars = [(d, _f(o), _f(h), _f(l), _f(c), _f(v)) for d, o, h, l, c, v in cur.fetchall()]
    bench_1y = _ret_over(bench_bars, 365)
    if stock_1y is None or bench_1y is None:
        add("rs_1y", None, {"stock_1y": _r(stock_1y), "benchmark_1y": _r(bench_1y)},
            "no close a year back for the stock or for " + BENCHMARK)
    else:
        excess = stock_1y - bench_1y
        add("rs_1y", 1.0 if excess > 0 else (0.5 if excess >= -3 else 0.0),
            {"stock_1y": _r(stock_1y), "benchmark_1y": _r(bench_1y),
             "excess_pp": _r(excess), "benchmark": BENCHMARK})

    # 8 — segment month, w4
    if not rec["segment"]:
        add("segment_month", None, {}, "this symbol carries no segment in gvm_scores")
    else:
        seg_m, seg_n = _segment_month(cur, rec["segment"])
        if seg_m is None or seg_n < 2:
            add("segment_month", None, {"segment": rec["segment"], "members_priced": seg_n},
                "fewer than 2 priced members in the segment")
        else:
            add("segment_month", 1.0 if seg_m > 0 else (0.5 if seg_m >= -0.5 else 0.0),
                {"segment": rec["segment"], "month_avg_pct": _r(seg_m), "members_priced": seg_n})

    # 9 — monthly RSI guard, w2
    mcloses = _monthly_closes(bars)
    mrsi = _rsi(mcloses, 14)
    if mrsi is None:
        add("rsi_monthly", None, {"monthly_closes": len(mcloses)},
            f"needs 15 calendar-monthly closes, has {len(mcloses)}")
    else:
        add("rsi_monthly", 1.0 if 35 <= mrsi <= 75 else 0.0,
            {"rsi_monthly": _r(mrsi, 1), "band": "35-75",
             "monthly_closes": len(mcloses)})

    live = [c for c in comps if c["computable"]]
    denom = sum(c["weight"] for c in live)
    earned = sum(c["weight"] * c["credit"] for c in live)
    score10 = round(10.0 * earned / denom, 2) if denom else None

    # FOUNDER DISPLAY RULING (27977 / 27979): heaviest component first, everywhere. Sorted here so
    # every consumer inherits the order and no surface can drift from it by forgetting.
    comps.sort(key=lambda c: -c["weight"])

    excluded = [{"key": c["key"], "weight": c["weight"], "reason": c["excluded_reason"]}
                for c in comps if not c["computable"]]
    return {
        "ok": True, "symbol": symbol, "company": rec["company"], "segment": rec["segment"],
        "gvm": rec["gvm"], "market_cap_cr": _r(rec["market_cap"], 0),
        "score10": score10, "band": _band(score10),
        "bands": f"STRONG_BUY>={_STRONG} / ACCUMULATE>={_ACCUM} / WATCH>={_WATCH} / AVOID below",
        "earned_weight": _r(earned), "computable_weight": _r(denom), "total_weight": 100.0,
        "excluded_components": excluded,
        # FALSE means the score came off the in-code founder lock because the registry could not be
        # read. It is still the locked numbers, but it is not a registry-calibrated score, and a
        # reader is told rather than left to assume.
        "weighted": weighted,
        "components": comps,
        "as_of": {
            "price_date": bars[-1][0].isoformat() if bars else None,
            "gvm_then_date": rec["gvm_then_date"].isoformat() if rec["gvm_then_date"] else None,
            "benchmark_price_date": bench_bars[-1][0].isoformat() if bench_bars else None,
        },
        "version": VERSION, "spec_ref": SPEC_REF,
        "weight_source": "ic_rule_weights" if weighted else "in-code founder lock (registry unread)",
    }


# ── endpoints ────────────────────────────────────────────────────────────────────────────────
@router.get("/api/investment-check-v2")
def investment_check_v2(symbol: str = Query(..., description="NSE symbol")):
    """V2 score for one symbol. V1's /api/investment-check is untouched and still serving."""
    sym = (symbol or "").strip().upper()
    try:
        with _conn() as conn, conn.cursor() as cur:
            return compute(cur, sym)
    except Exception as e:
        log.exception("investment_check_v2 %s", sym)
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "symbol": sym, "version": VERSION}


@router.get("/api/investment-check-v2/batch")
def investment_check_v2_batch(symbols: str = Query(..., description="comma-separated NSE symbols")):
    """Score a list in one call — the validation batch runs through this.

    Returns the slim row per symbol (score, band, weakest computable component) plus every error,
    listed rather than dropped. A batch that silently omits what it could not score reads as a
    clean sweep.
    """
    syms = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()][:200]
    rows, errors = [], []
    try:
        with _conn() as conn, conn.cursor() as cur:
            for s in syms:
                try:
                    d = compute(cur, s)
                except Exception as e:
                    errors.append({"symbol": s, "error": f"{type(e).__name__}: {str(e)[:120]}"})
                    continue
                if d.get("error"):
                    errors.append({"symbol": s, "error": d["error"]})
                    continue
                live = [c for c in d["components"] if c["computable"]]
                weakest = min(live, key=lambda c: (c["credit"], -c["weight"]), default=None)
                rows.append({
                    "symbol": s, "company": d["company"], "segment": d["segment"],
                    "gvm": d["gvm"], "score10": d["score10"], "band": d["band"],
                    "computable_weight": d["computable_weight"],
                    "excluded": [e["key"] for e in d["excluded_components"]],
                    "weakest_component": (weakest["label"] if weakest else None),
                    "weakest_credit": (weakest["credit"] if weakest else None),
                })
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "rows": rows, "errors": errors}
    rows.sort(key=lambda r: -(r["score10"] or 0))
    return {"ok": True, "count": len(rows), "scored": len(rows), "failed": len(errors),
            "rows": rows, "errors": errors, "version": VERSION, "spec_ref": SPEC_REF}


@router.get("/api/investment-check-v2/weights")
def investment_check_v2_weights():
    """The live registry, so a surface or a reviewer can diff it against 27979 without a DB login."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT rule_key, rule_label, weight, active, source
                           FROM ic_rule_weights WHERE bucket='invest'
                           ORDER BY weight DESC, rule_key""")
            rows = [{"key": k, "label": lb, "weight": float(w), "active": a, "source": src}
                    for k, lb, w, a, src in cur.fetchall()]
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "rows": []}
    active_total = sum(r["weight"] for r in rows if r["active"])
    return {"ok": True, "count": len(rows), "active_weight_total": active_total,
            "expected_total": 100.0, "matches_lock": abs(active_total - 100.0) < 1e-9,
            "rows": rows, "spec_ref": SPEC_REF}
