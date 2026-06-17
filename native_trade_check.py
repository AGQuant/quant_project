"""
Native v3.4 Trade Check — zero-token, pure Railway DB. UNIFIED SINGLE-TIER.

v3.4 (17-Jun-2026): MAJOR RESTRUCTURE — flattened from two-tier to ONE list.
  - Tier 1 (process) + Tier 2 (entry) MERGED into a single rule set, single verdict.
  - R2 Sector is now TRI-STATE: both>0 PASS(1.0), one>0 WATCH(0.5), both<=0 FAIL(0).
  - DROPPED F3 Fib, F4 R:R, F5 Window (noisy / redundant / time-of-day).
  - Surviving entry filters merged inline: F1 News, F2 Pivot, F6 Instrument, F7 Deriv.
  - LONG = 11 process + 4 entry = 15 rules. SHORT = 10 + 4 = 14.
  - Scoring: PASS=1.0, WATCH=0.5, FAIL/no-data=0.
  - Verdict bands: LONG PASS>=12 / WATCH 9-11 / FAIL <9.
                   SHORT PASS>=11 / WATCH 8-10 / FAIL <8.
  - R12 label: "none" -> "no setup" + closest-miss hint.

Carried from v3.3.3:
  - R6+R8 merged trend rule, ceiling open.
  - R11 caps daily AND weekly RSI < 80.
  - Interpretation layer (rule-based default + use_api opt-in).

Context isolation (id=244): NEVER mix with V8 basket rules or invest_check.
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
    while d.weekday() != 1:
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


# ─────────────────────────────────────────────── parameter computers ───────────────────────────────────

def _r7_volume_pattern(cur, symbol, side):
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
    """30d structure. LONG: breakout OR (higher-lows AND contraction).
    v3.4: 'none' -> 'no setup' + closest-miss hint."""
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
        if breakout:
            tag = "breakout"
        elif ok:
            tag = "HL+contraction"
        else:
            near = "higher-lows" if higher_lows else ("contraction" if contraction else "lower-lows, no contraction")
            tag = f"no setup ({near})"
    else:
        breakdown = lc < plo
        lower_highs = rhi < ohi
        ok = breakdown or (lower_highs and contraction)
        if breakdown:
            tag = "breakdown"
        elif ok:
            tag = "LH+contraction"
        else:
            near = "lower-highs" if lower_highs else ("contraction" if contraction else "higher-highs, no contraction")
            tag = f"no setup ({near})"
    return ok, tag


def _r13_atr_ignition(cur, symbol):
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


def _f6_dte():
    today = _ist_now().date()
    exp = _next_expiry(today)
    dte = (exp - today).days
    return dte >= 3, f"DTE {dte} (exp {exp.strftime('%d-%b')})"


# ─────────────────────────────────────────────── interpretation layer ───────────────────────────────────

def _istate(rows, prefix):
    for r in rows:
        if r["rule"].upper().startswith(prefix.upper()):
            return r["state"], r["val"]
    return None, None


def _weekly_rsi_from(val):
    if not val:
        return None
    mt = re.search(r"W\s*(\d+)", val)
    return int(mt.group(1)) if mt else None


def _interpret_rulebased(d):
    """Full narrative from the unified rule list. d = compute_trade_check dict."""
    side = d["side"]
    rules = d["rules"]
    vclass = d["verdict_class"]

    r6_s, r6_v = _istate(rules, "R6")
    r7_s, _ = _istate(rules, "R7")
    r9_s, _ = _istate(rules, "R9")
    r11_s, r11_v = _istate(rules, "R11")
    r12_s, r12_v = _istate(rules, "R12")
    r13_s, _ = _istate(rules, "R13")
    r2_s, r2_v = _istate(rules, "R2")
    f2_s, f2_v = _istate(rules, "F2")
    f6_s, _ = _istate(rules, "F6")

    tag = (r12_v or "").lower()
    if side == "LONG":
        if "breakout" in tag:
            archetype, arch_note = "momentum-continuation long", "trigger is a breakout from consolidation"
        elif "hl+" in tag:
            archetype, arch_note = "reversal-from-support long", "higher lows with contraction — coiled pullback within trend"
        else:
            archetype, arch_note = "long with no clear structure", "neither a breakout nor a tight base — structure is unresolved"
    else:
        if "breakdown" in tag:
            archetype, arch_note = "momentum-continuation short", "trigger is a breakdown below support"
        elif "lh+" in tag:
            archetype, arch_note = "reversal-from-resistance short", "lower highs with contraction — failing bounce into resistance"
        else:
            archetype, arch_note = "short with no clear structure", "neither a breakdown nor a tight distribution"

    drivers = []
    if r6_s == "pass":
        drivers.append(f"trend confirmed ({r6_v})")
    if r7_s == "pass":
        drivers.append("volume aligned with direction")
    if r9_s == "pass":
        drivers.append("week and month returns agree")
    if r13_s == "pass":
        drivers.append("ATR igniting")
    if r2_s == "pass":
        drivers.append("sector fully backing the move")
    elif r2_s == "watch":
        drivers.append(f"sector partly supportive ({r2_v})")
    if f2_s == "pass":
        drivers.append("price in the right pivot zone")

    risk = None
    if side == "LONG":
        wk = _weekly_rsi_from(r11_v)
        if r11_s == "fail":
            risk = f"overbought — {r11_v} breaches the weekly-80 ceiling; this is now a short-overbought candidate"
        elif wk is not None and wk >= 75:
            risk = f"weekly RSI elevated ({r11_v}) — limited upside room"
    else:
        if r11_s == "fail":
            risk = f"oversold — daily RSI {r11_v}; bounce risk against the short"

    if risk is None:
        if r12_s == "fail":
            risk = f"no clean structure ({r12_v}) — wait for a breakout or a proper base before entering"
        elif f6_s == "fail":
            risk = "instrument/expiry filter fails — futures not available or expiry too close"
        elif r2_s == "watch":
            risk = "sector only half-aligned — one of week/month is negative"
        elif f2_s == "fail":
            risk = "price not in the right pivot zone — entry timing is off"
        else:
            gaps = sum(1 for r in rules if r["state"] == "na")
            risk = (f"{gaps} rules have no data (counted as not passed) — read is partial"
                    if gaps >= 2 else "no single dominant risk — clean on the axes measured")

    conv = {"pass": "High conviction", "watch": "Mixed — some merit but not a clean entry",
            "fail": "Not actionable — score below the bar"}.get(vclass, "Mixed")
    drv = ", ".join(drivers) if drivers else "few rules confirmed"
    return f"{conv}. Reads as a {archetype} — {arch_note}. Supporting it: {drv}. Watch: {risk}."


def _interpret_api(d, model="claude-sonnet-4-6"):
    try:
        import anthropic
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            return _interpret_rulebased(d)
        client = anthropic.Anthropic(api_key=key)
        rows = [f"{r['rule']}: {r['state']} ({r['val']})" for r in d["rules"]]
        prompt = (
            f"You are a disciplined futures desk analyst. A {d['side']} setup on "
            f"{d['company']} ({d['symbol']}, {d['segment']}) scored under a unified price-action "
            f"checklist:\n" + "\n".join(rows)
            + f"\n\nVerdict: {d['verdict']}\n\nWrite ONE tight paragraph (max 70 words): name "
            "the archetype (reversal vs momentum), the conviction drivers, and the single binding "
            "risk. Price action only. Do not restate the score. Be decisive.")
        msg = client.messages.create(model=model, max_tokens=200,
                                     messages=[{"role": "user", "content": prompt}])
        txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        return txt or _interpret_rulebased(d)
    except Exception:
        return _interpret_rulebased(d)


def interpret(d, use_api=False):
    if not d.get("ok"):
        return None
    return _interpret_api(d) if use_api else _interpret_rulebased(d)


# ─────────────────────────────────────────────── main compute ───────────────────────────────────────────

def compute_trade_check(symbol_text, side=None, gate1=None, gate2=None, use_api=False):
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

            cur.execute("""
                SELECT adr FROM adr_intraday WHERE ts::date = CURRENT_DATE
                ORDER BY ts DESC LIMIT 1""")
            ar = cur.fetchone()
            if ar and ar[0] is not None:
                adr = _f(ar[0])
            else:
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

            cur.execute("""
                SELECT ROUND(((fb.futures_close - ip.close) / ip.close * 100)::numeric, 3),
                       fb.oi, fb.oi_chg, fb.ts
                FROM futures_basis fb
                CROSS JOIN (SELECT close FROM intraday_prices WHERE symbol=%s ORDER BY ts DESC LIMIT 1) ip
                WHERE fb.symbol=%s AND fb.futures_close IS NOT NULL
                ORDER BY fb.ts DESC LIMIT 5""", (symbol, symbol))
            basis_rows = cur.fetchall()
            basis = [_f(r[0]) for r in basis_rows]

            r7_ok, r7_val = _r7_volume_pattern(cur, symbol, side)
            r10_ok, r10_val = _r10_intraday(cur, symbol, side)
            r12_ok, r12_val = _r12_pattern(cur, symbol, side)
            r13_ok, r13_val = _r13_atr_ignition(cur, symbol)
            f6_ok, f6_val = _f6_dte()

            r10_method = "auto"
            if gate1 is not None:
                r10_ok, r10_val, r10_method = gate1, f"Gate1 {'✓' if gate1 else '✗'}", "gate"
            r12_method = "auto"
            if gate2 is not None:
                r12_ok, r12_val, r12_method = gate2, f"Gate2 {'✓' if gate2 else '✗'}", "gate"

            # row builder — ok can be bool|None OR 'watch' (tri-state)
            def row(rule, cond, val, ok, method="auto"):
                if ok == "watch":
                    state = "watch"
                elif ok is None:
                    state = "na"
                else:
                    state = "pass" if ok else "fail"
                return {"rule": rule, "cond": cond, "val": val, "state": state, "method": method}

            rules = []

            # R1 Market
            if side == "LONG":
                rules.append(row("R1 Market", "not extremely bearish",
                                 f"ADR {adr:.2f}" if adr is not None else "—",
                                 (adr is not None and adr >= 0.8) if adr is not None else None))
            else:
                rules.append(row("R1 Market", "not extremely bullish",
                                 f"ADR {adr:.2f}" if adr is not None else "—",
                                 (adr is not None and adr <= 1.2) if adr is not None else None))

            # R2 Sector — TRI-STATE (v3.4)
            sw, sm = _f(sec_w), _f(sec_m)
            if sw is None or sm is None:
                r2_state = None
            elif side == "LONG":
                pos = (sw > 0) + (sm > 0)
                r2_state = True if pos == 2 else ("watch" if pos == 1 else False)
            else:
                neg = (sw < 0) + (sm < 0)
                r2_state = True if neg == 2 else ("watch" if neg == 1 else False)
            r2_cond = "week>0 & month>0" if side == "LONG" else "week<0 & month<0"
            rules.append(row("R2 Sector", r2_cond,
                             f"W {sw:.1f} / M {sm:.1f}" if sw is not None else "—", r2_state))

            # R3 Peers
            rules.append(row("R3 Peers", f"2+ peers {'up' if side=='LONG' else 'down'}",
                             f"{peers_n} aligned", peers_n >= 2))

            # R4 GVM (LONG only)
            if side == "LONG":
                rules.append(row("R4 GVM", ">=7.0", f"{_f(gvm):.2f}" if gvm is not None else "—",
                                 gvm is not None and float(gvm) >= 7.0))

            # R6 Trend
            mas = [dma20, dma50, dma200]
            if side == "LONG":
                n = sum(1 for x in mas if x is not None and float(x) > 0)
                rsi_ok = (rsi_m is not None and rsi_w is not None and float(rsi_m) >= 50 and float(rsi_w) >= 50)
                rules.append(row("R6 Trend", "2of3 MAs above + RSI M/W>=50",
                                 f"{n}/3 MAs · RSI M {_f(rsi_m):.0f}/W {_f(rsi_w):.0f}", n >= 2 and rsi_ok))
            else:
                n = sum(1 for x in mas if x is not None and float(x) < 0)
                rsi_ok = (rsi_m is not None and rsi_w is not None and float(rsi_m) <= 50 and float(rsi_w) <= 50)
                rules.append(row("R6 Trend", "2of3 MAs below + RSI M/W<=50",
                                 f"{n}/3 MAs · RSI M {_f(rsi_m):.0f}/W {_f(rsi_w):.0f}", n >= 2 and rsi_ok))

            # R7 Volume
            rules.append(row("R7 Volume", f"1-mo {'buying' if side=='LONG' else 'selling'}", r7_val, r7_ok))

            # R9 Returns
            if side == "LONG":
                rules.append(row("R9 Returns", "week>0 & month>0",
                                 f"W {_f(wk_ret):.1f}% / M {_f(mo_ret):.1f}%",
                                 wk_ret is not None and mo_ret is not None and float(wk_ret) > 0 and float(mo_ret) > 0))
            else:
                rules.append(row("R9 Returns", "week<0 & month<0",
                                 f"W {_f(wk_ret):.1f}% / M {_f(mo_ret):.1f}%",
                                 wk_ret is not None and mo_ret is not None and float(wk_ret) < 0 and float(mo_ret) < 0))

            # R10 5-min
            rules.append(row("R10 5-min", "intraday strength (2/3 sub)", r10_val, r10_ok, r10_method))

            # R11 RSI room
            if side == "LONG":
                rules.append(row("R11 RSI room", "daily<80 & weekly<80",
                                 f"D {_f(rsi_d):.0f} / W {_f(rsi_w):.0f}",
                                 rsi_d is not None and rsi_w is not None and float(rsi_d) < 80 and float(rsi_w) < 80))
            else:
                rules.append(row("R11 RSI room", "daily>20", f"{_f(rsi_d):.0f}",
                                 rsi_d is not None and float(rsi_d) > 20))

            # R12 Pattern
            rules.append(row("R12 Pattern", "breakout OR HL+contraction" if side == "LONG"
                             else "breakdown OR LH+contraction", r12_val, r12_ok, r12_method))

            # R13 ATR
            rules.append(row("R13 ATR", "ignition ATR5/20>=1.05", r13_val, r13_ok))

            # F1 News (merged)
            rules.append(row("F1 News", "no blackout/ex-date", "blackout" if in_blackout else "clear", not in_blackout))

            # F2 Pivot (merged)
            if piv and cmp:
                pp, r1, s1 = _f(piv[0]), _f(piv[1]), _f(piv[2])
                if side == "LONG" and pp and r1:
                    room = (r1 - cmp) / cmp * 100
                    rules.append(row("F2 Pivot", "above PP, room>1%",
                                     f"CMP {cmp:.0f} PP {pp:.0f} R1 {r1:.0f}", cmp > pp and room > 1.0))
                elif side == "SHORT" and pp and s1:
                    room = (cmp - s1) / cmp * 100
                    rules.append(row("F2 Pivot", "below PP, room>1%",
                                     f"CMP {cmp:.0f} PP {pp:.0f} S1 {s1:.0f}", cmp < pp and room > 1.0))
                else:
                    rules.append(row("F2 Pivot", "pivot room", "no pivot", None))
            else:
                rules.append(row("F2 Pivot", "pivot room", "no pivot/cmp", None))

            # F6 Instrument (merged)
            rules.append(row("F6 Instrument", "futures DTE>=3", f6_val, f6_ok))

            # F7 Deriv (merged)
            if len(basis) >= 2 and basis[0] is not None and basis[-1] is not None:
                oi_chg = basis_rows[0][2] if basis_rows else None
                oi_str = f" · OI {oi_chg:+,}" if oi_chg is not None else ""
                f7_ok = basis[0] >= basis[-1] if side == "LONG" else basis[0] <= basis[-1]
                rules.append(row("F7 Deriv", "basis trend aligned", f"basis {basis[0]:.3f}%{oi_str}", f7_ok))
            else:
                rules.append(row("F7 Deriv", "OI + basis", "no futures data", None))

            # SCORING: PASS=1.0, WATCH=0.5, FAIL/na=0
            total = len(rules)
            score = 0.0
            for r in rules:
                if r["state"] == "pass":
                    score += 1.0
                elif r["state"] == "watch":
                    score += 0.5
            score = round(score, 1)
            n_pass = sum(1 for r in rules if r["state"] == "pass")
            n_watch = sum(1 for r in rules if r["state"] == "watch")
            n_fail = sum(1 for r in rules if r["state"] == "fail")
            n_na = sum(1 for r in rules if r["state"] == "na")

            # VERDICT bands (v3.4)
            if side == "LONG":
                pass_cut, watch_cut = 12, 9   # 15 rules
            else:
                pass_cut, watch_cut = 11, 8   # 14 rules
            if score >= pass_cut:
                vclass = "pass"
                verdict = f"PASS — {score}/{total}. Process and entry both align."
            elif score >= watch_cut:
                vclass = "watch"
                verdict = f"WATCH — {score}/{total}. Some merit; not a clean setup yet."
            else:
                vclass = "fail"
                verdict = f"FAIL — {score}/{total}. Below the bar."

            result = {
                "ok": True, "symbol": symbol, "company": company, "segment": segment,
                "side": side, "gvm": _f(gvm), "ts": _ist_now().strftime("%d-%b %H:%M IST"),
                "rules": rules,
                "score": score, "total": total,
                "n_pass": n_pass, "n_watch": n_watch, "n_fail": n_fail, "n_na": n_na,
                "pass_cut": pass_cut, "watch_cut": watch_cut,
                "verdict": verdict, "verdict_class": vclass,
                "version": "v3.4",
                "scoring": "unified single-tier · PASS=1.0 WATCH=0.5 FAIL/no-data=0",
                "foot": "Personal trade context · not a V8 algo signal · v3.4 · ⚠ Spot price, not futures.",
            }
            result["interpretation"] = interpret(result, use_api=use_api)
            result["interpretation_mode"] = "api" if use_api else "rule-based"
            return result
    except Exception as e:
        return {"ok": False, "error": f"DB error: {str(e)[:160]}"}


def compute_single_rule(symbol, side, rule):
    d = compute_trade_check(symbol, side)
    if not d.get("ok"):
        return d
    rule = rule.upper().replace(" ", "")
    for r in d["rules"]:
        if r["rule"].upper().replace(" ", "").startswith(rule):
            return {"ok": True, "symbol": d["symbol"], "side": d["side"], **r}
    return {"ok": False, "error": f"Unknown rule '{rule}'. v3.4 dropped F3/F4/F5; R8 merged into R6."}


def native_trade_check(query, gate1=None, gate2=None, use_api=False):
    d = compute_trade_check(query, _parse_side(query), gate1, gate2, use_api=use_api)
    if not d.get("ok"):
        return f"**Trade Check — v3.4**\n{d.get('error', 'error')}"

    def mark(r):
        s = {"pass": "PASS", "fail": "FAIL", "watch": "WATCH", "na": "🟡 no data"}[r["state"]]
        return s + (" (gate)" if r.get("method") == "gate" else "")

    out = [f"**Trade Check v3.4 — {d['company']} ({d['symbol']}) · {d['side']}**"]
    gv = f"GVM {d['gvm']:.2f} · " if d.get("gvm") is not None else ""
    out.append(f"_{d['segment']} · {gv}{d['ts']} · native $0 · unified_")
    out.append("\n| Rule | Condition | Value | State |")
    out.append("| --- | --- | --- | --- |")
    for r in d["rules"]:
        out.append(f"| {r['rule']} | {r['cond']} | {r['val']} | {mark(r)} |")
    out.append(f"\n**Score: {d['score']}/{d['total']}** · {d['n_pass']} pass · {d['n_watch']} watch · {d['n_fail']} fail · {d['n_na']} no-data")
    out.append(f"\n---\n**Verdict: {d['verdict']}**")
    if d.get("interpretation"):
        mode = " (AI)" if d.get("interpretation_mode") == "api" else ""
        out.append(f"\n**Interpretation{mode}:** {d['interpretation']}")
    out.append("_Personal trade context — not a V8 algo signal._")
    return "\n".join(out)


# ─────────────────────────────────────────────── NIFTY-50 (mcap proxy) SCREENER ───
# Added 17-Jun-2026. Runs the EXISTING compute_trade_check() across the top-50
# by market cap, both sides, returns top-10 ranked each side. Pure DB, no new
# scoring. Spec: session_log id=371. WATCH label kept (matches live v3.4 engine);
# Moderate rename (id=370) is a SEPARATE future change, not built here.

def _top_mcap_symbols(cur, n=50):
    """Top-N active futures by market cap (mcap proxy for Nifty 50)."""
    cur.execute("""
        SELECT f.symbol
        FROM futures_universe f
        JOIN gvm_scores g ON f.symbol = g.symbol
        WHERE f.is_active = TRUE AND g.market_cap IS NOT NULL
        ORDER BY g.market_cap DESC
        LIMIT %s""", (n,))
    return [r[0] for r in cur.fetchall()]


def _slim_row(d):
    """Compact a full compute_trade_check dict into one screener row.

    Keeps: identity, score, verdict, CMP, pivot zone, and the non-pass rules
    (with cond=required + val=company value) for chip + click-popup.
    """
    if not d.get("ok"):
        return None

    # pull CMP + pivot zone from F2 row (already computed by the engine)
    cmp_val = None
    pivot_zone = None
    pp = r1 = s1 = None
    for r in d["rules"]:
        if r["rule"].startswith("F2"):
            # val like "CMP 600 PP 597 R1 611"  or  "no pivot/cmp"
            import re as _re
            m = _re.findall(r"(CMP|PP|R1|S1)\s+([\d.]+)", r["val"])
            vals = {k: float(v) for k, v in m}
            cmp_val = vals.get("CMP")
            pp = vals.get("PP"); r1 = vals.get("R1"); s1 = vals.get("S1")
            if cmp_val is not None and pp is not None:
                if d["side"] == "LONG":
                    if cmp_val > pp:
                        pivot_zone = "above PP"
                    else:
                        pivot_zone = "below PP"
                else:
                    if cmp_val < pp:
                        pivot_zone = "below PP"
                    else:
                        pivot_zone = "above PP"
            break

    # non-pass rules -> chips (state + required + company-value for popup)
    not_passed = [
        {"rule": r["rule"], "state": r["state"],
         "required": r["cond"], "value": r["val"]}
        for r in d["rules"] if r["state"] in ("fail", "watch", "na")
    ]

    return {
        "symbol": d["symbol"],
        "company": d["company"],
        "segment": d["segment"],
        "side": d["side"],
        "gvm": d.get("gvm"),
        "score": d["score"],
        "total": d["total"],
        "verdict_class": d["verdict_class"],
        "n_pass": d["n_pass"], "n_watch": d["n_watch"],
        "n_fail": d["n_fail"], "n_na": d["n_na"],
        "pass_cut": d["pass_cut"],
        "cmp": cmp_val,
        "pivot": {"pp": pp, "r1": r1, "s1": s1, "zone": pivot_zone},
        "not_passed": not_passed,
    }


def screen_top50(n=50, top=10):
    """Run trade check on top-N mcap stocks, BOTH sides. Return top-`top` each.

    Reuses compute_trade_check unchanged. Heavy (N×2 DB passes) — on-demand only.
    """
    started = _ist_now()
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        symbols = _top_mcap_symbols(cur, n)

    long_rows, short_rows, errors = [], [], []
    for sym in symbols:
        for side in ("LONG", "SHORT"):
            try:
                d = compute_trade_check(sym, side)
                row = _slim_row(d)
                if row is None:
                    errors.append({"symbol": sym, "side": side,
                                   "error": d.get("error", "no data")})
                    continue
                (long_rows if side == "LONG" else short_rows).append(row)
            except Exception as e:
                errors.append({"symbol": sym, "side": side,
                               "error": f"{type(e).__name__}: {str(e)[:80]}"})

    # sort high->low by score; tiebreak by n_pass then gvm
    def _key(r):
        return (r["score"], r["n_pass"], r["gvm"] or 0)
    long_rows.sort(key=_key, reverse=True)
    short_rows.sort(key=_key, reverse=True)

    return {
        "ok": True,
        "label": "Nifty 50 (mcap proxy)",
        "source": "top-50 by market_cap from active futures_universe",
        "universe_count": len(symbols),
        "scored_long": len(long_rows),
        "scored_short": len(short_rows),
        "long_top10": long_rows[:top],
        "short_top10": short_rows[:top],
        "errors": errors[:20],
        "ts": _ist_now().strftime("%d-%b %H:%M IST"),
        "elapsed_sec": round((_ist_now() - started).total_seconds(), 1),
        "version": "v3.4",
        "note": "Same engine as single check, run x50. WATCH=0.5. mcap proxy, not the real index.",
    }
