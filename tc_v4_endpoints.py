"""
Trade Check v4 — two-tier framework (session_log id=959, TC_CANONICAL_SPEC_V4).

POST /api/trade-check/v4   body: {"symbol": "...", "direction": "LONG|SHORT"}

Tier-1 = process gates  (LONG 12 rules / SHORT 11 rules, binary, advance >= 8)
Tier-2 = entry filters  (8 filters F1-F8, binary, enter >= 5)

Note: the canonical spec states SHORT denominator = 10, but the SHORT rule list
is R1-R12 with only R4 (GVM gate) marked N/A = 11 scored rules. Resolved (Arpit,
29-Jun) to score all 11 defined SHORT rules; the spec's "10" is an arithmetic
slip. Verdict thresholds are absolute (advance >= 8, STRONG >= 10) so this only
affects the displayed denominator, not pass/fail.

Verdict:
  FAIL   : Tier1 < 8
  WATCH  : Tier1 >= 8 AND Tier2 below entry gate
  VALID  : Tier1 >= 8 AND Tier2 >= 5
  STRONG : Tier1 >= 10 AND Tier2 >= 5

cc#119: Tier-2 gains F8 (intraday volume confirmation). The deployed code already
carried 7 filters (F1-F7) but mislabelled max as 6 (a cc#116 slip); the
denominator is now the true scored count. With F8 that is 8 filters, ENTER gate
5/8 (Arpit, 29-Jun: keep all filters, truthful denominator). If a symbol has < 3
prior days of intraday history, F8 returns N/A and is excluded from BOTH the
score and the denominator (never a FAIL); Tier-2 then reads 7 filters and the
ENTER gate falls back to 4/7. STRONG keeps its Tier-2 bar at 5: only ENTER moved.

Data rules (spec data_rule + critical_data_rules):
  * RSI (month/weekly) and week/month returns are recomputed LIVE from raw_prices
    (v8_metrics copies are stale intraday).
  * v8_metrics is used ONLY for dma_20/dma_50/dma_200, daily_rsi, sector_week,
    sector_month and day_1d (EOD-frozen values are acceptable for these).
  * CMP = latest 5m bar today from intraday_prices, fallback cmp_prices.
  * Peers segment comes from gvm_scores (v8_metrics has no segment column).

SHORT is the mirror of LONG with inversions. R4 (GVM gate) is NOT APPLICABLE for
SHORT, so the SHORT Tier-1 denominator is 11 (R1-R12 minus R4). The
direction-dependent Tier-2 filters (F2 pivot-room, F3 fib, F4 R:R, F5
entry-window) are inverted for SHORT; F1/F6/F7 are direction-neutral and
unchanged.
"""

import os
from datetime import datetime, timedelta, date

import psycopg
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

_DB = os.getenv("DATABASE_URL", "")

VERSION = "v4.0"
SPEC_REF = "session_log id=959 / TC_CANONICAL_SPEC_V4 (locked 2026-06-29)"


# ── small helpers ─────────────────────────────────────────────────────────────

def _ist():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _r(v, n=2):
    return round(v, n) if isinstance(v, (int, float)) else None


def _rule(rid, label, condition, value, passed):
    return {"rule": rid, "label": label, "condition": condition,
            "value": value, "pass": bool(passed)}


def _rsi(closes, period=14):
    """Wilder RSI on a close series. Needs period+1 points; returns None otherwise."""
    closes = [c for c in closes if c is not None]
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _weekly_closes(rows):
    """rows = list of dicts ascending by price_date → last close per ISO week."""
    by_week = {}
    order = []
    for row in rows:
        d = row["price_date"]
        key = d.isocalendar()[:2]  # (iso_year, iso_week)
        if key not in by_week:
            order.append(key)
        by_week[key] = row["close"]
    return [by_week[k] for k in order]


def _last_tuesday(year, month):
    """Last Tuesday of the given month (NSE monthly expiry, weekday()==1)."""
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() != 1:
        d -= timedelta(days=1)
    return d


def _current_expiry(today):
    """Monthly expiry = last Tuesday this month; roll to next month if past."""
    exp = _last_tuesday(today.year, today.month)
    if today > exp:
        ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        exp = _last_tuesday(ny, nm)
    return exp


# ── data loader ───────────────────────────────────────────────────────────────

def _load(cur, symbol):
    d = {}

    cur.execute("""SELECT price_date, open, high, low, close, volume
                   FROM raw_prices WHERE symbol=%s
                   ORDER BY price_date DESC LIMIT 130""", (symbol,))
    rows = [{"price_date": r[0], "open": _f(r[1]), "high": _f(r[2]),
             "low": _f(r[3]), "close": _f(r[4]), "volume": _f(r[5])}
            for r in cur.fetchall()]
    rows.reverse()
    d["daily"] = rows

    cur.execute("""SELECT close FROM raw_prices WHERE symbol='NIFTY50'
                   ORDER BY price_date DESC LIMIT 23""")
    d["nifty"] = [_f(r[0]) for r in cur.fetchall()][::-1]

    cur.execute("""SELECT dma_20, dma_50, dma_200, daily_rsi,
                          sector_week, sector_month, day_1d
                   FROM v8_metrics WHERE symbol=%s
                   ORDER BY score_date DESC LIMIT 1""", (symbol,))
    m = cur.fetchone()
    d["v8"] = ({"dma_20": _f(m[0]), "dma_50": _f(m[1]), "dma_200": _f(m[2]),
                "daily_rsi": _f(m[3]), "sector_week": _f(m[4]),
                "sector_month": _f(m[5]), "day_1d": _f(m[6])} if m else {})

    cur.execute("""SELECT gvm_score, segment FROM gvm_scores WHERE symbol=%s
                   ORDER BY score_date DESC LIMIT 1""", (symbol,))
    g = cur.fetchone()
    d["gvm_score"] = _f(g[0]) if g else None
    d["segment"] = g[1] if g else None

    d["peers_pos"] = d["peers_neg"] = 0
    if d["segment"]:
        cur.execute("""
            SELECT COUNT(*) FILTER (WHERE v.day_1d > 0),
                   COUNT(*) FILTER (WHERE v.day_1d < 0)
            FROM gvm_scores g
            JOIN v8_metrics v ON v.symbol = g.symbol
            WHERE g.segment = %s
              AND g.symbol <> %s
              AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
              AND v.score_date = (SELECT MAX(score_date) FROM v8_metrics)
        """, (d["segment"], symbol))
        pp_ = cur.fetchone()
        d["peers_pos"], d["peers_neg"] = int(pp_[0] or 0), int(pp_[1] or 0)

    cur.execute("""SELECT pp, r1, s1, r2, s2 FROM v8_paper_pivots WHERE symbol=%s
                   ORDER BY pivot_date DESC LIMIT 1""", (symbol,))
    p = cur.fetchone()
    d["pivots"] = ({"pp": _f(p[0]), "r1": _f(p[1]), "s1": _f(p[2]),
                    "r2": _f(p[3]), "s2": _f(p[4])} if p else
                   {"pp": None, "r1": None, "s1": None, "r2": None, "s2": None})

    cur.execute("""SELECT open, high, low, close FROM intraday_prices
                   WHERE symbol=%s AND source='fyers_eq' AND timeframe='5m'
                     AND ts::date = CURRENT_DATE
                   ORDER BY ts""", (symbol,))
    d["bars"] = [{"open": _f(r[0]), "high": _f(r[1]), "low": _f(r[2]),
                  "close": _f(r[3])} for r in cur.fetchall()]

    # CMP: latest 5m bar today → fallback cmp_prices
    cmp_v = d["bars"][-1]["close"] if d["bars"] else None
    if cmp_v is None:
        cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (symbol,))
        c = cur.fetchone()
        cmp_v = _f(c[0]) if c else None
    if cmp_v is None and rows:
        cmp_v = rows[-1]["close"]
    d["cmp"] = cmp_v

    cur.execute("SELECT adr FROM adr_daily ORDER BY price_date DESC LIMIT 1")
    a = cur.fetchone()
    d["adr"] = _f(a[0]) if a else None

    cur.execute("""SELECT LOWER(COALESCE(headline,'')||' '||COALESCE(description,''))
                   FROM raw_news WHERE symbol=%s
                     AND published_at >= NOW() - INTERVAL '7 days'""", (symbol,))
    d["news"] = [r[0] for r in cur.fetchall()]

    cur.execute("""SELECT basis_pct, oi_chg FROM futures_basis WHERE symbol=%s
                   ORDER BY ts DESC LIMIT 3""", (symbol,))
    d["basis"] = [{"basis_pct": _f(r[0]), "oi_chg": _f(r[1])} for r in cur.fetchall()]

    # F8 (cc#119) — intraday volume vs trailing 7-day avg over the SAME window.
    # today_vol = SUM(volume) 09:15 -> latest bar; avg_7d = AVG of the same
    # 09:15 -> window_end sum across the last up-to-7 trading days. If today has
    # no bars, window_end is NULL and hist_vol yields zero rows -> days_found=0
    # -> F8 is N/A (handled in _tier2). spec_id 959.
    cur.execute("""
        WITH today_vol AS (
            SELECT COALESCE(SUM(volume), 0) AS vol, MAX(ts::time) AS window_end
            FROM intraday_prices
            WHERE symbol=%s AND source IN ('fyers_eq','fyers') AND timeframe='5m'
              AND ts::date = CURRENT_DATE AND ts::time >= '09:15:00'
        ),
        hist_vol AS (
            SELECT ts::date AS d, SUM(volume) AS day_vol
            FROM intraday_prices
            WHERE symbol=%s AND source IN ('fyers_eq','fyers') AND timeframe='5m'
              AND ts::date >= CURRENT_DATE - INTERVAL '14 days'
              AND ts::date < CURRENT_DATE
              AND ts::time >= '09:15:00'
              AND ts::time <= (SELECT window_end FROM today_vol)
            GROUP BY ts::date
            ORDER BY ts::date DESC
            LIMIT 7
        )
        SELECT (SELECT vol FROM today_vol),
               (SELECT window_end FROM today_vol),
               ROUND(AVG(day_vol)),
               COUNT(*)
        FROM hist_vol
    """, (symbol, symbol))
    iv = cur.fetchone()
    if iv:
        d["intra_vol"] = {"today_vol": _f(iv[0]), "window_end": iv[1],
                          "avg_7d": _f(iv[2]), "days_found": int(iv[3] or 0)}
    else:
        d["intra_vol"] = {"today_vol": None, "window_end": None,
                          "avg_7d": None, "days_found": 0}

    return d


# ── tier 1 ────────────────────────────────────────────────────────────────────

def _tier1(d, direction):
    LONG = direction == "LONG"
    rules = []
    daily = d["daily"]
    closes = [r["close"] for r in daily]
    cmp_v = d["cmp"]
    piv = d["pivots"]
    v8 = d["v8"]

    # R1 — Market mood (same for both directions)
    nf = d["nifty"]
    fails = 0
    if d["adr"] is not None and d["adr"] < 1.0:
        fails += 1
    if len(nf) >= 2 and nf[-1] / nf[-2] - 1 < 0:
        fails += 1
    if len(nf) >= 6 and nf[-1] / nf[-6] - 1 < 0:
        fails += 1
    if len(nf) >= 23 and nf[-1] / nf[-23] - 1 < 0:
        fails += 1
    rules.append(_rule("R1", "Market mood", "Nifty mood fails <= 2",
                       f"{fails} fails (ADR {_r(d['adr'])})", fails <= 2))

    # R2 — Sector aligned
    sw, sm = v8.get("sector_week"), v8.get("sector_month")
    if LONG:
        r2 = sw is not None and sm is not None and sw > 0 and sm > 0
        cond = "sector_week>0 AND sector_month>0"
    else:
        r2 = sw is not None and sm is not None and sw < 0 and sm < 0
        cond = "sector_week<0 AND sector_month<0"
    rules.append(_rule("R2", "Sector aligned", cond,
                       f"week {_r(sw)}, month {_r(sm)}", r2))

    # R3 — Sector peers
    if LONG:
        r3 = d["peers_pos"] >= 2
        rules.append(_rule("R3", "Sector peers positive", "2+ peers day_1d > 0",
                           f"{d['peers_pos']} positive peers", r3))
    else:
        r3 = d["peers_neg"] >= 2
        rules.append(_rule("R3", "Sector peers negative", "2+ peers day_1d < 0",
                           f"{d['peers_neg']} negative peers", r3))

    # R4 — GVM gate (LONG only; skipped for SHORT)
    if LONG:
        gv = d["gvm_score"]
        r4 = gv is not None and gv >= 7.0
        rules.append(_rule("R4", "GVM quality gate", "gvm_score >= 7.0",
                           f"{_r(gv)}", r4))

    # R5 — Reversal, no chase
    pp, r1, s1 = piv.get("pp"), piv.get("r1"), piv.get("s1")
    if LONG:
        r5 = None not in (cmp_v, pp, r1) and pp < cmp_v <= r1
        cond = "PP < CMP <= R1"
    else:
        r5 = None not in (cmp_v, pp, s1) and s1 <= cmp_v < pp
        cond = "S1 <= CMP < PP"
    rules.append(_rule("R5", "Reversal — no chase", cond,
                       f"CMP {_r(cmp_v)} (PP {_r(pp)}, R1 {_r(r1)}, S1 {_r(s1)})", r5))

    # R6 — 2 of 3 MAs aligned
    mas = [v8.get("dma_20"), v8.get("dma_50"), v8.get("dma_200")]
    if LONG:
        cnt = sum(1 for x in mas if x is not None and x > 0)
        cond, lbl = "2+ of dma_20/50/200 above (positive)", "2 of 3 MAs above"
    else:
        cnt = sum(1 for x in mas if x is not None and x < 0)
        cond, lbl = "2+ of dma_20/50/200 below (negative)", "2 of 3 MAs below"
    rules.append(_rule("R6", lbl, cond,
                       f"{cnt}/3 (20:{_r(mas[0])} 50:{_r(mas[1])} 200:{_r(mas[2])})",
                       cnt >= 2))

    # R7 — Volume pattern (last 22 sessions)
    up_v, dn_v = [], []
    for i in range(max(1, len(daily) - 22), len(daily)):
        if daily[i]["close"] is None or daily[i - 1]["close"] is None or daily[i]["volume"] is None:
            continue
        (up_v if daily[i]["close"] > daily[i - 1]["close"] else dn_v).append(daily[i]["volume"])
    au = sum(up_v) / len(up_v) if up_v else 0.0
    ad = sum(dn_v) / len(dn_v) if dn_v else 0.0
    if LONG:
        r7 = au > ad and au > 0
        cond = "avg vol up-days > down-days"
    else:
        r7 = ad > au and ad > 0
        cond = "avg vol down-days > up-days"
    rules.append(_rule("R7", "Volume pattern", cond,
                       f"up {_r(au, 0)} vs down {_r(ad, 0)}", r7))

    # R8 — RSI month + weekly (LIVE from raw_prices)
    rsi_w = _rsi(closes)                       # "RSI weekly" = RSI14 on daily closes
    rsi_m = _rsi(_weekly_closes(daily))        # "RSI month"  = RSI14 on weekly closes
    if LONG:
        r8 = rsi_m is not None and rsi_w is not None and rsi_m >= 50 and rsi_w >= 50
        cond = "RSI month >= 50 AND weekly >= 50"
    else:
        r8 = rsi_m is not None and rsi_w is not None and rsi_m <= 50 and rsi_w <= 50
        cond = "RSI month <= 50 AND weekly <= 50"
    rules.append(_rule("R8", "RSI alignment", cond,
                       f"month {_r(rsi_m)}, weekly {_r(rsi_w)}", r8))

    # R9 — Week + month returns (LIVE)
    wk = closes[-1] / closes[-6] - 1 if len(closes) >= 6 else None
    mo = closes[-1] / closes[-23] - 1 if len(closes) >= 23 else None
    if LONG:
        r9 = wk is not None and mo is not None and wk > 0 and mo > 0
        cond = "week_return>0 AND month_return>0"
    else:
        r9 = wk is not None and mo is not None and wk < 0 and mo < 0
        cond = "week_return<0 AND month_return<0"
    rules.append(_rule("R9", "Return trend", cond,
                       f"week {_r((wk or 0) * 100)}%, month {_r((mo or 0) * 100)}%", r9))

    # R10 — 5-min intraday strength (2 of 3 sub-conditions)
    bars = d["bars"]
    sub = 0
    detail = "no intraday bars"
    if bars:
        day_open = bars[0]["open"]
        hi = max(b["high"] for b in bars if b["high"] is not None)
        lo = min(b["low"] for b in bars if b["low"] is not None)
        cur_close = bars[-1]["close"]
        rng = (hi - lo) if hi is not None and lo is not None else 0.0
        last6 = [b["close"] for b in bars[-6:] if b["close"] is not None]
        if LONG:
            a = cur_close is not None and day_open is not None and cur_close > day_open
            b_ = rng > 0 and cur_close >= lo + 0.70 * rng
            c = len(last6) >= 2 and last6[-1] > last6[0]
        else:
            a = cur_close is not None and day_open is not None and cur_close < day_open
            b_ = rng > 0 and cur_close <= lo + 0.30 * rng
            c = len(last6) >= 2 and last6[-1] < last6[0]
        sub = sum([bool(a), bool(b_), bool(c)])
        detail = f"A={int(bool(a))} B={int(bool(b_))} C={int(bool(c))}"
    rules.append(_rule("R10", "5-min intraday strength", "2 of 3 sub-conditions",
                       detail, sub >= 2))

    # R11 — Daily RSI extreme guard
    drsi = v8.get("daily_rsi")
    if LONG:
        r11 = drsi is not None and drsi < 80
        cond = "daily_rsi < 80"
    else:
        r11 = drsi is not None and drsi > 20
        cond = "daily_rsi > 20"
    rules.append(_rule("R11", "Daily RSI guard", cond, f"{_r(drsi)}", r11))

    # R12 — 30-day structure
    w30 = daily[-30:]
    r12 = False
    detail = "insufficient history"
    if len(w30) >= 30:
        thirds = [w30[0:10], w30[10:20], w30[20:30]]
        lows = [min(x["low"] for x in t if x["low"] is not None) for t in thirds]
        highs = [max(x["high"] for x in t if x["high"] is not None) for t in thirds]
        cl = [x["close"] for x in w30 if x["close"] is not None]
        avg_c = sum(cl) / len(cl) if cl else 0.0
        tight = avg_c > 0 and (max(cl) - min(cl)) / avg_c < 0.10
        if LONG:
            structure = lows[0] < lows[1] < lows[2]   # higher lows
            detail = f"higher_lows={int(structure)} tight={int(tight)}"
            lbl = "30-day accumulation/consolidation"
        else:
            structure = highs[0] > highs[1] > highs[2]  # lower highs
            detail = f"lower_highs={int(structure)} tight={int(tight)}"
            lbl = "30-day distribution/consolidation"
        r12 = structure or tight
    else:
        lbl = "30-day accumulation/consolidation" if LONG else "30-day distribution/consolidation"
    rules.append(_rule("R12", lbl, "higher/lower swing structure OR tight range",
                       detail, r12))

    score = sum(1 for r in rules if r["pass"])
    # LONG = 12 rules; SHORT = 11 (R4/GVM gate is N/A for shorts). Denominator
    # tracks the rules actually scored so the display can never read "11/10".
    mx = len(rules)
    return {"score": score, "max": mx, "advance": score >= 8, "rules": rules}


# ── tier 2 ────────────────────────────────────────────────────────────────────

def _tier2(d, direction):
    LONG = direction == "LONG"
    rules = []
    cmp_v = d["cmp"]
    piv = d["pivots"]
    pp, r1, s1 = piv.get("pp"), piv.get("r1"), piv.get("s1")
    daily = d["daily"]

    # F1 — News + events clear (direction-neutral)
    bad = ("blackout", "trading window", "ex-date", "ex date", "ex-dividend",
           "record date")
    hits = [k for k in bad if any(k in n for n in d["news"])]
    rules.append(_rule("F1", "News + events clear",
                       "no blackout / trading-window / ex-date / record-date",
                       ("clear" if not hits else "flags: " + ", ".join(hits)), not hits))

    # F2 — Pivot room
    if LONG:
        f2 = None not in (cmp_v, pp, r1) and cmp_v > pp and (r1 - cmp_v) / cmp_v > 0.01
        cond, val = "CMP>PP AND (R1-CMP)/CMP > 1%", f"CMP {_r(cmp_v)} PP {_r(pp)} R1 {_r(r1)}"
    else:
        f2 = None not in (cmp_v, pp, s1) and cmp_v < pp and (cmp_v - s1) / cmp_v > 0.01
        cond, val = "CMP<PP AND (CMP-S1)/CMP > 1%", f"CMP {_r(cmp_v)} PP {_r(pp)} S1 {_r(s1)}"
    rules.append(_rule("F2", "Pivot room", cond, val, f2))

    # F3 — Fibonacci level (last 60 sessions)
    w60 = daily[-60:]
    f3 = False
    val = "insufficient history"
    if len(w60) >= 20 and cmp_v:
        sh = max(x["high"] for x in w60 if x["high"] is not None)
        sl = min(x["low"] for x in w60 if x["low"] is not None)
        rng = sh - sl
        if rng > 0:
            if LONG:   # retracement of an up-move, measured down from the high
                levels = {r: sh - rng * r for r in (0.382, 0.5, 0.618)}
            else:      # retracement of a down-move, measured up from the low
                levels = {r: sl + rng * r for r in (0.382, 0.5, 0.618)}
            near = [f"{int(r * 1000) / 10}%" for r, lv in levels.items()
                    if abs(cmp_v - lv) / cmp_v <= 0.015]
            f3 = bool(near)
            val = ("near " + ", ".join(near)) if near else "no fib level within 1.5%"
    rules.append(_rule("F3", "Fibonacci level", "CMP within 1.5% of 38.2/50/61.8%",
                       val, f3))

    # F4 — Risk:Reward >= 1:2
    if LONG:
        f4 = None not in (cmp_v, r1, s1) and (cmp_v - s1) > 0 and (r1 - cmp_v) / (cmp_v - s1) >= 2.0
        rr = ((r1 - cmp_v) / (cmp_v - s1)) if (None not in (cmp_v, r1, s1) and (cmp_v - s1) > 0) else None
        cond = "(R1-CMP)/(CMP-S1) >= 2.0"
    else:
        f4 = None not in (cmp_v, r1, s1) and (r1 - cmp_v) > 0 and (cmp_v - s1) / (r1 - cmp_v) >= 2.0
        rr = ((cmp_v - s1) / (r1 - cmp_v)) if (None not in (cmp_v, r1, s1) and (r1 - cmp_v) > 0) else None
        cond = "(CMP-S1)/(R1-CMP) >= 2.0"
    rules.append(_rule("F4", "Risk:Reward >= 1:2", cond, f"R:R {_r(rr)}", f4))

    # F5 — Entry window (IST)
    now = _ist().time()
    if LONG:
        win_lo, win_hi = (14, 0), (15, 20)
        cond = "Entry window 14:00-15:20 IST"
    else:
        win_lo, win_hi = (10, 0), (12, 0)
        cond = "Entry window 10:00-12:00 IST"
    cur_min = now.hour * 60 + now.minute
    f5 = win_lo[0] * 60 + win_lo[1] <= cur_min <= win_hi[0] * 60 + win_hi[1]
    rules.append(_rule("F5", "Entry window", cond,
                       _ist().strftime("%H:%M IST"), f5))

    # F6 — Futures DTE >= 3 (direction-neutral)
    today = _ist().date()
    exp = _current_expiry(today)
    dte = (exp - today).days
    rules.append(_rule("F6", "Futures DTE >= 3", "days to monthly expiry >= 3",
                       f"{dte} days (expiry {exp.isoformat()})", dte >= 3))

    # F7 — OI + basis aligned (direction-neutral; at least one)
    basis = d["basis"]
    bp = basis[0]["basis_pct"] if basis else None
    oc = basis[0]["oi_chg"] if basis else None
    f7 = (bp is not None and bp > 0) or (oc is not None and oc > 0)
    rules.append(_rule("F7", "OI + basis aligned", "basis_pct>0 OR oi_chg>0",
                       f"basis {_r(bp)}% oi_chg {_r(oc, 0)}", f7))

    # F8 (cc#119) — Intraday volume confirmation (direction-neutral). Today's
    # cumulative volume (09:15 -> latest bar) vs the trailing 7-day average over
    # the SAME window. Graceful: < 3 prior days of intraday history -> N/A, which
    # is excluded from both the score and the denominator (never a FAIL).
    iv = d.get("intra_vol", {})
    tv, a7, dfound = iv.get("today_vol"), iv.get("avg_7d"), iv.get("days_found", 0) or 0
    we = iv.get("window_end")
    we_str = we.strftime("%H:%M") if we is not None else "--:--"
    if dfound >= 3 and a7 and a7 > 0 and tv is not None:
        ratio = tv / a7
        f8 = _rule("F8", "Intraday volume confirmation",
                   "today vol > 7-day avg (same window)",
                   f"{_r(ratio)}x avg · window 09:15-{we_str}", tv > a7)
    else:
        f8 = _rule("F8", "Intraday volume confirmation",
                   "today vol > 7-day avg (same window)",
                   f"N/A · only {dfound} prior day(s) of intraday history", False)
        f8["na"] = True
        f8["state"] = "N/A"
    rules.append(f8)

    # N/A rules (F8 with insufficient history) are excluded from BOTH score and
    # denominator. ENTER gate steps up by one when F8 is actually scored (cc#119):
    # F8 scored -> gate 5; F8 N/A -> gate falls back to 4 so the missing intraday
    # history never penalises. STRONG keeps its Tier-2 bar at 5 (Arpit, 29-Jun).
    scorable = [r for r in rules if not r.get("na")]
    score = sum(1 for r in scorable if r["pass"])
    mx = len(scorable)
    f8_scored = not f8.get("na")
    enter = score >= (5 if f8_scored else 4)
    strong = score >= 5
    return {"score": score, "max": mx, "enter": enter, "strong": strong,
            "rules": rules}


# ── verdict + interpretation ──────────────────────────────────────────────────

def _verdict(t1, t2):
    s1 = t1["score"]
    # cc#119: Tier-2 gates are dynamic (enter 5/8, or 4/7 when F8 is N/A);
    # tier2 exposes pre-computed enter/strong flags. STRONG bar kept at 5.
    if s1 >= 10 and t2.get("strong"):
        return "STRONG"
    if s1 >= 8 and t2.get("enter"):
        return "VALID"
    if s1 >= 8:
        return "WATCH"
    return "FAIL"


def _interpretation(symbol, direction, verdict, t1, t2, cmp_v):
    failed1 = [r["rule"] for r in t1["rules"] if not r["pass"]]
    # N/A filters (F8 with insufficient history) are not "failed" entry filters.
    failed2 = [r["rule"] for r in t2["rules"] if not r["pass"] and not r.get("na")]
    # ENTER gate is 5 when F8 scored (max 8), else 4 (max 7, F8 N/A).
    thr = 5 if t2["max"] >= 8 else 4
    head = f"{symbol} {direction} @ {_r(cmp_v)} — {verdict}."
    if verdict == "STRONG":
        body = (f"Tier-1 {t1['score']}/{t1['max']} and Tier-2 {t2['score']}/{t2['max']} both "
                f"clear the high bar — process and entry timing align. "
                f"Highest-conviction {direction.lower()} setup.")
    elif verdict == "VALID":
        body = (f"Tier-1 {t1['score']}/{t1['max']} confirms the process is sound and "
                f"Tier-2 {t2['score']}/{t2['max']} clears entry filters. Tradeable now; "
                f"weak filters: {', '.join(failed2) or 'none'}.")
    elif verdict == "WATCH":
        body = (f"Tier-1 {t1['score']}/{t1['max']} qualifies the setup, but Tier-2 "
                f"{t2['score']}/{t2['max']} is below {thr} — entry timing not ready "
                f"(missing: {', '.join(failed2) or 'none'}). Keep on watch.")
    else:
        body = (f"Tier-1 {t1['score']}/{t1['max']} is below the 8-gate threshold — "
                f"the setup fails process screening "
                f"(failed: {', '.join(failed1) or 'none'}). Skip.")
    return head + " " + body


def _pivot_bar(piv, cmp_v):
    order = [("S2", piv.get("s2")), ("S1", piv.get("s1")), ("PP", piv.get("pp")),
             ("R1", piv.get("r1")), ("R2", piv.get("r2"))]
    parts = [f"{lbl} {_r(v)}" for lbl, v in order if v is not None]
    bar = "  ..  ".join(parts)
    if cmp_v is not None:
        bar += f"   [CMP {_r(cmp_v)}]"
    return bar


# ── public compute ────────────────────────────────────────────────────────────

def trade_check_v4(symbol, direction):
    symbol = (symbol or "").strip().upper()
    direction = (direction or "LONG").strip().upper()
    if direction not in ("LONG", "SHORT"):
        return {"error": f"direction must be LONG or SHORT, got {direction!r}"}
    if not symbol:
        return {"error": "symbol required"}

    try:
        with psycopg.connect(_DB) as conn, conn.cursor() as cur:
            d = _load(cur, symbol)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}

    if not d["daily"]:
        return {"error": f"no raw_prices history for {symbol}"}
    if d["cmp"] is None:
        return {"error": f"no CMP available for {symbol}"}

    t1 = _tier1(d, direction)
    t2 = _tier2(d, direction)
    verdict = _verdict(t1, t2)

    return {
        "symbol": symbol,
        "direction": direction,
        "cmp": _r(d["cmp"]),
        "pivots": {k: _r(v) for k, v in d["pivots"].items()},
        "pivot_bar": _pivot_bar(d["pivots"], d["cmp"]),
        "tier1": t1,
        "tier2": t2,
        "final_verdict": verdict,
        "interpretation": _interpretation(symbol, direction, verdict, t1, t2, d["cmp"]),
        "computed_at": _ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        "spec_ref": SPEC_REF,
    }


# ── routes ────────────────────────────────────────────────────────────────────

class TCV4Request(BaseModel):
    symbol: str
    direction: str = "LONG"


@router.post("/api/trade-check/v4")
def trade_check_v4_post(req: TCV4Request):
    return trade_check_v4(req.symbol, req.direction)


@router.get("/api/trade-check/v4")
def trade_check_v4_get(symbol: str, direction: str = "LONG"):
    return trade_check_v4(symbol, direction)


@router.get("/api/trade-check/v4/health")
def trade_check_v4_health():
    return {
        "version": VERSION, "spec_ref": SPEC_REF,
        "tier1": {"LONG": 12, "SHORT": 11, "advance": 8},
        "tier2": {"max": 8, "enter": 5,
                  "na_fallback": "F8 N/A (<3d intraday) -> 7 filters, enter 4"},
        "verdict": {"FAIL": "T1<8", "WATCH": "T1>=8 & T2 below gate",
                    "VALID": "T1>=8 & T2>=5", "STRONG": "T1>=10 & T2>=5"},
        "status": "ok",
    }
