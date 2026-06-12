"""
Native v3.3.2 Trade Check — zero-token, pure Railway DB. ENGINE v3.3 (STRICT).

v3.3.2 (12-Jun-2026): R6+R8 MERGED into single "R6 Trend" rule after
redundancy scan found 88% agreement across 209 futures (paying twice for
the same fact). R13 ATR Ignition KEPT (21.9% pass rate, 63-75% overlap =
lowest in framework = genuinely new volatility axis).
Tier1 back to LONG 11 / SHORT 10, min 8 — original locked denominators,
every rule now on a distinct axis:
  R1 breadth | R2 sector RS | R3 peer confirm | R4 quality (LONG only) |
  R6 trend (MAs+RSI merged) | R7 participation | R9 price momentum |
  R10 timing | R11 room | R12 structure | R13 volatility

ALL parameters auto-computed from DB.
Tier2 (7, min 5): F1 blackout, F2 pivot room, F3 fib proximity,
F4 R:R, F5 entry window (live IST), F6 DTE>=3, F7 basis trend.

STRICT SCORING: only confirmed PASSes count. FAIL and no-data (🟡) both
count as NOT passed.

gate1/gate2 (bool|None) = OPTIONAL human overrides:
  gate1 overrides R10, gate2 overrides R12.

Scope: v3.3.2 (id=143 base + delta id=263 R13 + merge delta). Not v3.4.1.
"""

import os
import re
import calendar
from datetime import datetime, date, timedelta
import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "")


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _last_tuesday(y, m):
    d = date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() != 1:  # Tuesday
        d -= timedelta(days=1)
    return d


def _next_expiry(today):
    e = _last_tuesday(today.year, today.month)
    if today > e:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        e = _last_tuesday(y, m)
    return e


def _resolve_symbol(cur, raw):
    raw = raw.strip().upper()
    cur.execute("SELECT symbol, company_name, segment FROM gvm_scores WHERE UPPER(symbol)=%s LIMIT 1", (raw,))
    r = cur.fetchone()
    if r:
        return r
    words = sorted([w for w in re.split(r"\s+", raw) if len(w) >= 2], key=len, reverse=True)
    for w in words:
        cur.execute(
            """SELECT symbol, company_name, segment FROM gvm_scores
               WHERE UPPER(symbol) LIKE %s OR UPPER(company_name) LIKE %s
               ORDER BY LENGTH(symbol) LIMIT 1""",
            (f"%{w}%", f"%{w}%"))
        r = cur.fetchone()
        if r:
            return r
    return None


def _parse_side(q):
    return "SHORT" if ("short" in q.lower() or "sell" in q.lower()) else "LONG"


# ──────────────────────────── parameter computers ────────────────────────────

def _r7_volume_pattern(cur, symbol, side):
    """30d up-day vol vs down-day vol. LONG: ratio>=1.1. SHORT: ratio<=0.9."""
    cur.execute("""
        WITH d AS (
          SELECT close, volume, LAG(close) OVER (ORDER BY price_date) AS pc
          FROM (SELECT price_date, close, volume FROM raw_prices
                WHERE symbol=%s AND volume>0 ORDER BY price_date DESC LIMIT 31) s
          ORDER BY price_date
        )
        SELECT AVG(volume) FILTER (WHERE close>pc),
               AVG(volume) FILTER (WHERE close<pc)
        FROM d WHERE pc IS NOT NULL""", (symbol,))
    r = cur.fetchone()
    up, dn = _f(r[0]) if r else None, _f(r[1]) if r else None
    if not up or not dn:
        return None, "no vol data"
    ratio = up / dn
    ok = ratio >= 1.1 if side == "LONG" else ratio <= 0.9
    return ok, f"up/dn {ratio:.2f}"


def _r10_intraday(cur, symbol, side):
    """Today's (or latest) 5m bars: day-up + close-position + 2nd-half trend. 2/3."""
    cur.execute("""
        SELECT ts, open, high, low, close FROM intraday_prices
        WHERE symbol=%s AND timeframe='5m'
          AND ts::date=(SELECT MAX(ts::date) FROM intraday_prices
                        WHERE symbol=%s AND timeframe='5m')
        ORDER BY ts""", (symbol, symbol))
    bars = cur.fetchall()
    if len(bars) < 8:
        return None, f"{len(bars)} bars only"
    bar_date = bars[0][0].date()
    o0 = _f(bars[0][1]); cN = _f(bars[-1][4])
    hi = max(_f(b[2]) for b in bars); lo = min(_f(b[3]) for b in bars)
    closes = [_f(b[4]) for b in bars]
    half = len(closes) // 2
    h1 = sum(closes[:half]) / half
    h2 = sum(closes[half:]) / (len(closes) - half)
    pos = (cN - lo) / (hi - lo) if hi > lo else 0.5
    if side == "LONG":
        checks = [cN > o0, pos >= 0.6, h2 > h1]
    else:
        checks = [cN < o0, pos <= 0.4, h2 < h1]
    n = sum(checks)
    stale = "" if bar_date == _ist_now().date() else f" ({bar_date.strftime('%d-%b')})"
    return n >= 2, f"{n}/3 sub{stale}"


def _r12_pattern(cur, symbol, side):
    """30d: breakout OR (higher-lows AND contraction). SHORT inverts."""
    cur.execute("""
        WITH d AS (
          SELECT high, low, close, ROW_NUMBER() OVER (ORDER BY price_date DESC) rn
          FROM raw_prices WHERE symbol=%s AND volume>0
          ORDER BY price_date DESC LIMIT 30)
        SELECT AVG(low)  FILTER (WHERE rn<=10), AVG(low)  FILTER (WHERE rn>20),
               AVG(high) FILTER (WHERE rn<=10), AVG(high) FILTER (WHERE rn>20),
               AVG(high-low) FILTER (WHERE rn<=10), AVG(high-low) FILTER (WHERE rn>20),
               (SELECT close FROM d WHERE rn=1),
               (SELECT MAX(high) FROM d WHERE rn BETWEEN 6 AND 30),
               (SELECT MIN(low)  FROM d WHERE rn BETWEEN 6 AND 30),
               COUNT(*)
        FROM d""", (symbol,))
    r = cur.fetchone()
    if not r or (r[9] or 0) < 25:
        return None, "insufficient history"
    rlo, olo, rhi, ohi, rrng, orng, lc, phi, plo = (_f(x) for x in r[:9])
    contraction = rrng < orng
    if side == "LONG":
        breakout = lc > phi
        higher_lows = rlo > olo
        ok = breakout or (higher_lows and contraction)
        tag = "brkout" if breakout else ("HL+ctr" if ok else
              ("HL only" if higher_lows else ("ctr only" if contraction else "none")))
    else:
        breakdown = lc < plo
        lower_highs = rhi < ohi
        ok = breakdown or (lower_highs and contraction)
        tag = "brkdwn" if breakdown else ("LH+ctr" if ok else
              ("LH only" if lower_highs else ("ctr only" if contraction else "none")))
    return ok, tag


def _r13_atr_ignition(cur, symbol):
    """ATR(5) vs ATR(20). >=1.05 = energy arriving (both sides).
    R12 = structure coiled, R13 = release trigger."""
    cur.execute("""
        WITH d AS (
          SELECT price_date, high, low, close,
                 LAG(close) OVER (ORDER BY price_date) pc
          FROM (SELECT price_date, high, low, close FROM raw_prices
                WHERE symbol=%s AND volume>0 ORDER BY price_date DESC LIMIT 21) s
          ORDER BY price_date),
        t AS (
          SELECT GREATEST(high-low, ABS(high-pc), ABS(low-pc)) AS trng,
                 ROW_NUMBER() OVER (ORDER BY price_date DESC) rn
          FROM d WHERE pc IS NOT NULL)
        SELECT AVG(trng) FILTER (WHERE rn<=5), AVG(trng) FROM t""", (symbol,))
    r = cur.fetchone()
    a5, a20 = (_f(r[0]) if r else None), (_f(r[1]) if r else None)
    if not a5 or not a20:
        return None, "no ATR data"
    ratio = a5 / a20
    return ratio >= 1.05, f"ATR5/20 {ratio:.2f}"


def _f3_fib(cur, symbol, cmp):
    """30d swing hi/lo; PASS if CMP within 1.5% of 38.2/50/61.8 level."""
    if not cmp:
        return None, "no cmp"
    cur.execute("""
        SELECT MAX(high), MIN(low) FROM (
          SELECT high, low FROM raw_prices WHERE symbol=%s AND volume>0
          ORDER BY price_date DESC LIMIT 30) s""", (symbol,))
    r = cur.fetchone()
    hi, lo = _f(r[0]), _f(r[1])
    if not hi or not lo or hi <= lo:
        return None, "no swing"
    best, best_lbl = 99.0, ""
    for ratio, lbl in ((0.382, "38.2"), (0.5, "50"), (0.618, "61.8")):
        lvl = hi - ratio * (hi - lo)
        dist = abs(cmp - lvl) / cmp * 100
        if dist < best:
            best, best_lbl = dist, lbl
    return best <= 1.5, f"{best_lbl}% lvl {best:.1f}% away"


def _f5_window(side):
    now = _ist_now()
    if now.weekday() >= 5:
        return False, "weekend"
    t = now.time()
    if side == "LONG":
        ok = (t >= datetime.strptime("14:00", "%H:%M").time()
              and t <= datetime.strptime("15:30", "%H:%M").time())
        win = "14:00-15:30"
    else:
        ok = (t >= datetime.strptime("10:30", "%H:%M").time()
              and t <= datetime.strptime("12:00", "%H:%M").time())
        win = "10:30-12:00"
    return ok, f"now {now.strftime('%H:%M')} / {win}"


def _f6_dte():
    today = _ist_now().date()
    exp = _next_expiry(today)
    dte = (exp - today).days
    return dte >= 3, f"DTE {dte} (exp {exp.strftime('%d-%b')})"


# ──────────────────────────── main compute ────────────────────────────

def compute_trade_check(symbol_text, side=None, gate1=None, gate2=None):
    if side is None:
        side = _parse_side(symbol_text)
    side = side.upper()
    cleaned = re.sub(
        r"\b(trade\s*check|trade\s*review|trade\s*journal|journal\s*check|check|review|evaluate|analyse|analyze|stock|on|for|a|the|long|short|buy|sell|motors?|ltd|limited)\b",
        " ", symbol_text, flags=re.I).strip()
    if not cleaned:
        return {"ok": False, "error": "Specify a symbol, e.g. RELIANCE."}

    try:
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            resolved = _resolve_symbol(cur, cleaned)
            if not resolved:
                return {"ok": False, "error": f"No stock found for '{cleaned}'."}
            symbol, company, segment = resolved

            cur.execute("""
                SELECT gvm_score, dma_20, dma_50, dma_200, rsi_month, rsi_weekly,
                       daily_rsi, week_return, month_return, day_1d,
                       sector_week, sector_month
                FROM v8_metrics WHERE symbol=%s
                  AND score_date=(SELECT MAX(score_date) FROM v8_metrics) LIMIT 1""", (symbol,))
            m = cur.fetchone()
            if not m:
                return {"ok": False, "symbol": symbol, "company": company,
                        "error": f"No V8 metrics for {symbol} (outside futures universe)."}
            (gvm, dma20, dma50, dma200, rsi_m, rsi_w, rsi_d,
             wk_ret, mo_ret, day1d, sec_w, sec_m) = m

            cur.execute("SELECT adr FROM adr_daily ORDER BY price_date DESC LIMIT 1")
            ar = cur.fetchone()
            adr = _f(ar[0]) if ar else None

            op = ">" if side == "LONG" else "<"
            cur.execute(f"""
                SELECT COUNT(*) FROM v8_metrics v JOIN gvm_scores g ON g.symbol=v.symbol
                WHERE g.segment=%s AND v.symbol<>%s
                  AND v.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                  AND v.day_1d {op} 0""", (segment, symbol))
            peers_n = cur.fetchone()[0] or 0

            cur.execute("""SELECT pp, r1, s1 FROM v8_paper_pivots WHERE symbol=%s
                           AND pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots) LIMIT 1""", (symbol,))
            piv = cur.fetchone()
            cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (symbol,))
            cr = cur.fetchone()
            cmp = _f(cr[0]) if cr else None

            cur.execute("""SELECT 1 FROM earnings_calendar WHERE UPPER(ticker)=%s
                           AND ex_date IN (CURRENT_DATE, CURRENT_DATE+INTERVAL '1 day') LIMIT 1""", (symbol,))
            in_blackout = cur.fetchone() is not None

            cur.execute("""SELECT basis_pct FROM futures_basis WHERE symbol=%s
                           ORDER BY ts DESC LIMIT 5""", (symbol,))
            basis = [_f(x[0]) for x in cur.fetchall()]

            # auto computers
            r7_ok, r7_val = _r7_volume_pattern(cur, symbol, side)
            r10_ok, r10_val = _r10_intraday(cur, symbol, side)
            r12_ok, r12_val = _r12_pattern(cur, symbol, side)
            r13_ok, r13_val = _r13_atr_ignition(cur, symbol)
            f3_ok, f3_val = _f3_fib(cur, symbol, cmp)
            f5_ok, f5_val = _f5_window(side)
            f6_ok, f6_val = _f6_dte()

            # gate overrides (human eyes win)
            r10_method = "auto"
            if gate1 is not None:
                r10_ok, r10_val, r10_method = gate1, f"Gate1 {'✓' if gate1 else '✗'}", "gate"
            r12_method = "auto"
            if gate2 is not None:
                r12_ok, r12_val, r12_method = gate2, f"Gate2 {'✓' if gate2 else '✗'}", "gate"

            def row(rule, cond, val, ok, method="auto"):
                return (rule, cond, val, ok, method)

            # ── TIER 1 (v3.3.2: LONG 11 / SHORT 10, min 8) ──
            t1 = []
            if side == "LONG":
                t1.append(row("R1 Market", "not extremely bearish",
                              f"ADR {adr:.2f}" if adr is not None else "—",
                              (adr is not None and adr >= 0.8) if adr is not None else None))
                t1.append(row("R2 Sector", "week>0 & month>0",
                              f"W {_f(sec_w):.1f} / M {_f(sec_m):.1f}",
                              sec_w is not None and sec_m is not None and float(sec_w) > 0 and float(sec_m) > 0))
            else:
                t1.append(row("R1 Market", "not extremely bullish",
                              f"ADR {adr:.2f}" if adr is not None else "—",
                              (adr is not None and adr <= 1.2) if adr is not None else None))
                t1.append(row("R2 Sector", "week<0 & month<0",
                              f"W {_f(sec_w):.1f} / M {_f(sec_m):.1f}",
                              sec_w is not None and sec_m is not None and float(sec_w) < 0 and float(sec_m) < 0))

            t1.append(row("R3 Peers", f"2+ peers {'up' if side=='LONG' else 'down'}",
                          f"{peers_n} aligned", peers_n >= 2))
            if side == "LONG":
                t1.append(row("R4 GVM", ">=7.0", f"{_f(gvm):.2f}" if gvm is not None else "—",
                              gvm is not None and float(gvm) >= 7.0))

            # R6 Trend — MERGED R6+R8 (v3.3.2): MAs + RSI M/W together
            # (redundancy scan 12-Jun: 88% agreement = same fact paid twice)
            mas = [dma20, dma50, dma200]
            if side == "LONG":
                n = sum(1 for x in mas if x is not None and float(x) > 0)
                rsi_ok = (rsi_m is not None and rsi_w is not None
                          and float(rsi_m) >= 50 and float(rsi_w) >= 50)
                t1.append(row("R6 Trend", "2of3 MAs above + RSI M/W>=50 (merged R6+R8)",
                              f"{n}/3 MAs · RSI M {_f(rsi_m):.0f}/W {_f(rsi_w):.0f}",
                              n >= 2 and rsi_ok))
            else:
                n = sum(1 for x in mas if x is not None and float(x) < 0)
                rsi_ok = (rsi_m is not None and rsi_w is not None
                          and float(rsi_m) <= 50 and float(rsi_w) <= 50)
                t1.append(row("R6 Trend", "2of3 MAs below + RSI M/W<=50 (merged R6+R8)",
                              f"{n}/3 MAs · RSI M {_f(rsi_m):.0f}/W {_f(rsi_w):.0f}",
                              n >= 2 and rsi_ok))

            t1.append(row("R7 Volume", f"1-mo {'buying' if side=='LONG' else 'selling'} (vol ratio)",
                          r7_val, r7_ok))

            if side == "LONG":
                t1.append(row("R9 Returns", "week>0 & month>0",
                              f"W {_f(wk_ret):.1f}% / M {_f(mo_ret):.1f}%",
                              wk_ret is not None and mo_ret is not None and float(wk_ret) > 0 and float(mo_ret) > 0))
            else:
                t1.append(row("R9 Returns", "week<0 & month<0",
                              f"W {_f(wk_ret):.1f}% / M {_f(mo_ret):.1f}%",
                              wk_ret is not None and mo_ret is not None and float(wk_ret) < 0 and float(mo_ret) < 0))

            t1.append(row("R10 5-min", "intraday strength (2/3 sub)", r10_val, r10_ok, r10_method))

            if side == "LONG":
                t1.append(row("R11 RSI room", "daily<80", f"{_f(rsi_d):.0f}",
                              rsi_d is not None and float(rsi_d) < 80))
            else:
                t1.append(row("R11 RSI room", "daily>20", f"{_f(rsi_d):.0f}",
                              rsi_d is not None and float(rsi_d) > 20))

            t1.append(row("R12 Pattern", "brkout OR HL+contraction" if side == "LONG"
                          else "brkdwn OR LH+contraction", r12_val, r12_ok, r12_method))

            t1.append(row("R13 ATR", "ignition ATR5/20>=1.05", r13_val, r13_ok))

            t1_total = 11 if side == "LONG" else 10
            t1_auto = [r for r in t1 if r[3] is not None]
            t1_pass = sum(1 for r in t1_auto if r[3])
            t1_unk = len(t1) - len(t1_auto)

            # ── TIER 2 ──
            t2 = [row("F1 News", "no blackout/ex-date", "blackout" if in_blackout else "clear", not in_blackout)]

            if piv and cmp:
                pp, r1, s1 = _f(piv[0]), _f(piv[1]), _f(piv[2])
                if side == "LONG" and pp and r1:
                    room = (r1 - cmp) / cmp * 100
                    t2.append(row("F2 Pivot", "above PP, room>1%",
                                  f"CMP {cmp:.0f} PP {pp:.0f} R1 {r1:.0f}", cmp > pp and room > 1.0))
                elif side == "SHORT" and pp and s1:
                    room = (cmp - s1) / cmp * 100
                    t2.append(row("F2 Pivot", "below PP, room>1%",
                                  f"CMP {cmp:.0f} PP {pp:.0f} S1 {s1:.0f}", cmp < pp and room > 1.0))
                else:
                    t2.append(row("F2 Pivot", "pivot room", "no pivot", None))
            else:
                t2.append(row("F2 Pivot", "pivot room", "no pivot/cmp", None))

            t2.append(row("F3 Fib", "near 38.2/50/61.8 (±1.5%)", f3_val, f3_ok))

            if piv and cmp:
                pp, r1, s1 = _f(piv[0]), _f(piv[1]), _f(piv[2])
                rr = None
                if side == "LONG" and r1 and s1:
                    risk = cmp - s1
                    rr = (r1 - cmp) / risk if risk > 0 else None
                elif side == "SHORT" and r1 and s1:
                    risk = r1 - cmp
                    rr = (cmp - s1) / risk if risk > 0 else None
                t2.append(row("F4 R:R", ">=1:2", f"{rr:.2f}" if rr is not None else "—",
                              rr is not None and rr >= 2.0))
            else:
                t2.append(row("F4 R:R", ">=1:2", "no pivot", None))

            t2.append(row("F5 Window", "LONG 14:00-15:30 / SHORT 10:30-12:00", f5_val, f5_ok))
            t2.append(row("F6 Instrument", "futures DTE>=3", f6_val, f6_ok))

            if len(basis) >= 2 and basis[0] is not None and basis[-1] is not None:
                f7_ok = basis[0] >= basis[-1] if side == "LONG" else basis[0] <= basis[-1]
                t2.append(row("F7 Deriv", "basis trend aligned", f"basis {basis[0]:.2f}%", f7_ok))
            else:
                t2.append(row("F7 Deriv", "OI + basis", "no basis", None))

            t2_auto = [r for r in t2 if r[3] is not None]
            t2_pass = sum(1 for r in t2_auto if r[3])
            t2_unk = len(t2) - len(t2_auto)

            # ── verdict — STRICT: only confirmed PASSes count.
            if t1_pass >= 8 and t2_pass >= 5:
                if t1_pass >= 10 and t2_pass >= 6:
                    verdict, vclass = f"STRONG — Tier1 {t1_pass}/{t1_total}, Tier2 {t2_pass}/7.", "strong"
                else:
                    verdict, vclass = f"VALID — Tier1 {t1_pass}/{t1_total}, Tier2 {t2_pass}/7. Entry allowed.", "valid"
            elif t1_pass < 8:
                miss = f" ({t1_unk} no-data counted as not-passed)" if t1_unk else ""
                verdict, vclass = f"REJECT — Tier1 {t1_pass}/{t1_total} confirmed, need 8{miss}.", "reject"
            else:
                miss = f" ({t2_unk} no-data counted as not-passed)" if t2_unk else ""
                verdict, vclass = f"WEAK — Tier1 {t1_pass}/{t1_total} ok, Tier2 {t2_pass}/7 below 5{miss}.", "weak"

            def st(ok):
                return "chart" if ok is None else ("pass" if ok else "fail")

            return {
                "ok": True, "symbol": symbol, "company": company, "segment": segment,
                "side": side, "gvm": _f(gvm), "ts": _ist_now().strftime("%d-%b %H:%M IST"),
                "tier1": [{"rule": r[0], "cond": r[1], "val": r[2], "state": st(r[3]), "method": r[4]} for r in t1],
                "tier2": [{"rule": r[0], "cond": r[1], "val": r[2], "state": st(r[3]), "method": r[4]} for r in t2],
                "t1_pass": t1_pass, "t1_auto_n": len(t1_auto), "t1_human_n": t1_unk, "t1_total": t1_total,
                "t2_pass": t2_pass, "t2_auto_n": len(t2_auto), "t2_human_n": t2_unk,
                "verdict": verdict, "verdict_class": vclass,
                "version": "v3.3.2",
                "scoring": "strict — fails and no-data both count as not passed",
            }
    except Exception as e:
        return {"ok": False, "error": f"DB error: {str(e)[:160]}"}


def compute_single_rule(symbol, side, rule):
    """Single-parameter API: returns just one rule's row from the composite."""
    d = compute_trade_check(symbol, side)
    if not d.get("ok"):
        return d
    rule = rule.upper().replace(" ", "")
    for r in d["tier1"] + d["tier2"]:
        if r["rule"].upper().replace(" ", "").startswith(rule):
            return {"ok": True, "symbol": d["symbol"], "side": d["side"], **r}
    return {"ok": False, "error": f"Unknown rule '{rule}'. Use R1-R13 or F1-F7 (R8 merged into R6 in v3.3.2)."}


def native_trade_check(query, gate1=None, gate2=None):
    """Markdown wrapper for /ask + native_router."""
    d = compute_trade_check(query, _parse_side(query), gate1, gate2)
    if not d.get("ok"):
        return f"**Trade Check — v3.3**\n{d.get('error', 'error')}"

    def mark(r):
        s = {"pass": "PASS", "fail": "FAIL", "chart": "🟡 no data (not passed)"}[r["state"]]
        return s + (" (gate)" if r.get("method") == "gate" else "")

    out = [f"**Trade Check v3.3.2 — {d['company']} ({d['symbol']}) · {d['side']}**"]
    gv = f"GVM {d['gvm']:.2f} · " if d.get("gvm") is not None else ""
    out.append(f"_{d['segment']} · {gv}{d['ts']} · native $0 · strict scoring_")
    out.append("\n**TIER 1**")
    out.append("| Rule | Condition | Value | State |")
    out.append("| --- | --- | --- | --- |")
    for r in d["tier1"]:
        out.append(f"| {r['rule']} | {r['cond']} | {r['val']} | {mark(r)} |")
    out.append(f"\n**Tier1: {d['t1_pass']}/{d['t1_total']} confirmed** · need 8")
    out.append("\n**TIER 2**")
    out.append("| Filter | Condition | Value | State |")
    out.append("| --- | --- | --- | --- |")
    for r in d["tier2"]:
        out.append(f"| {r['rule']} | {r['cond']} | {r['val']} | {mark(r)} |")
    out.append(f"\n**Tier2: {d['t2_pass']}/7 confirmed** · need 5")
    out.append(f"\n---\n**Verdict: {d['verdict']}**")
    out.append("_Personal trade context — not a V8 algo signal._")
    return "\n".join(out)
