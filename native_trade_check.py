"""
Native v3.3 Trade Check — zero-token, pure Railway DB.

Computes the OBJECTIVE subset of the canonical v3.3 framework
(session_log id=143 + 209 + 240) directly from v8_metrics + gvm_scores
+ v8_paper_pivots. Subjective/chart rules (5-min strength, 1D pattern,
news/events, fib bounce) are surfaced as HUMAN-INPUT rows the trader
confirms — never machine-guessed.

The caller may pass gate1 (5-min strength) and gate2 (1-Day reversal/breakout)
as booleans (human-in-the-AI-loop). When supplied:
  gate1 -> resolves R10 (5-min recovery/weakness)
  gate2 -> resolves R12 (30-day pattern) AND F3 (fib bounce proxy)
Unsupplied gates stay as 🟡 chart rows.

Scope (per Arpit 11-Jun): v3.3 ONLY. Not v3.4.1.

Tier1: LONG 11 rules (min 8) / SHORT 10 rules (min 8, GVM N/A)
Tier2: 7 filters (min 5/7)

Verdict:
  Tier1 < 8                  -> REJECT
  Tier1 >=8 & Tier2 >=5      -> VALID (STRONG if Tier1>=10 & Tier2>=6)
  Tier1 >=8 & Tier2 <5       -> WEAK / WATCH
"""

import os
import re
from datetime import datetime
import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "")


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _yn(ok):
    if ok is None:
        return "—"
    return "PASS" if ok else "FAIL"


def _resolve_symbol(cur, raw: str):
    """Find symbol from free text. Returns (symbol, company, segment) or None."""
    raw = raw.strip().upper()
    cur.execute(
        "SELECT symbol, company_name, segment FROM gvm_scores WHERE UPPER(symbol)=%s LIMIT 1",
        (raw,),
    )
    r = cur.fetchone()
    if r:
        return r
    words = sorted([w for w in re.split(r"\s+", raw) if len(w) >= 2], key=len, reverse=True)
    for w in words:
        cur.execute(
            """SELECT symbol, company_name, segment FROM gvm_scores
               WHERE UPPER(symbol) LIKE %s OR UPPER(company_name) LIKE %s
               ORDER BY LENGTH(symbol) LIMIT 1""",
            (f"%{w}%", f"%{w}%"),
        )
        r = cur.fetchone()
        if r:
            return r
    return None


def _parse_side(q: str) -> str:
    ql = q.lower()
    if "short" in ql or "sell" in ql:
        return "SHORT"
    return "LONG"


def compute_trade_check(symbol_text: str, side: str = None,
                        gate1: bool = None, gate2: bool = None) -> dict:
    """
    Structured native v3.3 trade check. Returns a dict:
      { ok, symbol, company, segment, side, gvm, ts,
        tier1:[{rule,cond,val,state}], tier2:[...],
        t1_pass, t1_auto_n, t1_human_n, t1_total,
        t2_pass, t2_auto_n, t2_human_n,
        verdict, verdict_class }
    state in {'pass','fail','chart'}.
    gate1/gate2 (bool|None): human chart confirmations.
    """
    if side is None:
        side = _parse_side(symbol_text)
    side = side.upper()
    cleaned = re.sub(
        r"\b(trade\s*check|trade\s*journal|journal\s*check|check|evaluate|review|analyse|analyze|stock|on|for|a|the|long|short|buy|sell)\b",
        " ", symbol_text, flags=re.I,
    ).strip()
    if not cleaned:
        return {"ok": False, "error": "Specify a symbol, e.g. RELIANCE."}

    try:
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            resolved = _resolve_symbol(cur, cleaned)
            if not resolved:
                return {"ok": False, "error": f"No stock found for '{cleaned}'."}
            symbol, company, segment = resolved

            cur.execute(
                """SELECT gvm_score, dma_20, dma_50, dma_200, rsi_month, rsi_weekly,
                          daily_rsi, week_return, month_return, year_return, mom_2d,
                          day_1d, sector_week, sector_month, week_index_52, vol_ratio
                   FROM v8_metrics
                   WHERE symbol=%s AND score_date=(SELECT MAX(score_date) FROM v8_metrics)
                   LIMIT 1""",
                (symbol,),
            )
            m = cur.fetchone()
            if not m:
                return {"ok": False, "symbol": symbol, "company": company,
                        "error": f"No V8 metrics for {symbol} (may be outside futures universe)."}
            (gvm, dma20, dma50, dma200, rsi_m, rsi_w, rsi_d,
             wk_ret, mo_ret, yr_ret, mom2d, day1d, sec_w, sec_m, wk52, vol) = m

            cur.execute("SELECT adr FROM adr_daily ORDER BY price_date DESC LIMIT 1")
            adr_row = cur.fetchone()
            adr_val = _f(adr_row[0]) if adr_row else 1.0
            mood_bearish = adr_val is not None and adr_val < 0.8

            cur.execute(
                """SELECT COUNT(*) FROM v8_metrics v
                   JOIN gvm_scores g ON g.symbol=v.symbol
                   WHERE g.segment=%s AND v.symbol<>%s
                     AND v.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                     AND v.day_1d IS NOT NULL AND v.day_1d %s 0""" % ("%s", "%s", ">" if side == "LONG" else "<"),
                (segment, symbol),
            )
            peers_aligned = (cur.fetchone()[0] or 0) >= 2

            cur.execute(
                """SELECT pp, r1, s1 FROM v8_paper_pivots
                   WHERE symbol=%s AND pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)
                   LIMIT 1""",
                (symbol,),
            )
            piv = cur.fetchone()
            cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (symbol,))
            cmp_row = cur.fetchone()
            cmp = _f(cmp_row[0]) if cmp_row else None

            cur.execute(
                """SELECT 1 FROM earnings_calendar
                   WHERE UPPER(ticker)=%s AND ex_date IN (CURRENT_DATE, CURRENT_DATE+INTERVAL '1 day') LIMIT 1""",
                (symbol,),
            )
            in_blackout = cur.fetchone() is not None

            cur.execute(
                """SELECT basis, basis_pct FROM futures_basis
                   WHERE symbol=%s ORDER BY ts DESC LIMIT 5""",
                (symbol,),
            )
            basis_rows = cur.fetchall()

            def st(ok):
                return "chart" if ok is None else ("pass" if ok else "fail")

            # ════════ TIER 1 ════════
            t1 = []

            t1.append(("R1 Market", "not extremely " + ("bearish" if side == "LONG" else "bullish"),
                       f"ADR {adr_val:.2f}" if adr_val is not None else "—",
                       (not mood_bearish) if side == "LONG" else True))

            if side == "LONG":
                t1.append(("R2 Sector", "sec_week>0 & month>0",
                           f"W {_f(sec_w):.1f} / M {_f(sec_m):.1f}" if sec_w is not None else "—",
                           (sec_w is not None and sec_m is not None and float(sec_w) > 0 and float(sec_m) > 0)))
            else:
                t1.append(("R2 Sector", "sec_week<0 & month<0",
                           f"W {_f(sec_w):.1f} / M {_f(sec_m):.1f}" if sec_w is not None else "—",
                           (sec_w is not None and sec_m is not None and float(sec_w) < 0 and float(sec_m) < 0)))

            t1.append(("R3 Peers", f"2+ peers {'up' if side == 'LONG' else 'down'}",
                       "yes" if peers_aligned else "no", peers_aligned))

            if side == "LONG":
                t1.append(("R4 GVM", "GVM>=7.0", f"{_f(gvm):.2f}" if gvm is not None else "—",
                           (gvm is not None and float(gvm) >= 7.0)))

            mas = [dma20, dma50, dma200]
            if side == "LONG":
                above = sum(1 for x in mas if x is not None and float(x) > 0)
                t1.append(("R6 MAs", "above 2 of 3 MAs", f"{above}/3 above", above >= 2))
            else:
                below = sum(1 for x in mas if x is not None and float(x) < 0)
                t1.append(("R6 MAs", "below 2 of 3 MAs", f"{below}/3 below", below >= 2))

            t1.append(("R7 Volume", f"1-mo {'buying' if side == 'LONG' else 'selling'}",
                       "chart", None))

            if side == "LONG":
                t1.append(("R8 RSI M/W", "RSI_m>=50 & w>=50",
                           f"M {_f(rsi_m):.0f} / W {_f(rsi_w):.0f}" if rsi_m is not None else "—",
                           (rsi_m is not None and rsi_w is not None and float(rsi_m) >= 50 and float(rsi_w) >= 50)))
                t1.append(("R9 Returns", "week>0 & month>0",
                           f"W {_f(wk_ret):.1f}% / M {_f(mo_ret):.1f}%" if wk_ret is not None else "—",
                           (wk_ret is not None and mo_ret is not None and float(wk_ret) > 0 and float(mo_ret) > 0)))
            else:
                t1.append(("R8 RSI M/W", "RSI_m<=50 & w<=50",
                           f"M {_f(rsi_m):.0f} / W {_f(rsi_w):.0f}" if rsi_m is not None else "—",
                           (rsi_m is not None and rsi_w is not None and float(rsi_m) <= 50 and float(rsi_w) <= 50)))
                t1.append(("R9 Returns", "week<0 & month<0",
                           f"W {_f(wk_ret):.1f}% / M {_f(mo_ret):.1f}%" if wk_ret is not None else "—",
                           (wk_ret is not None and mo_ret is not None and float(wk_ret) < 0 and float(mo_ret) < 0)))

            # R10 5-min — resolved by gate1 if supplied
            r10_val = "Gate1 ✓" if gate1 is True else ("Gate1 ✗" if gate1 is False else "chart")
            t1.append(("R10 5-min", f"5M {'recovery+strong' if side == 'LONG' else 'weak+weak close'}",
                       r10_val, gate1))

            if side == "LONG":
                t1.append(("R11 RSI room", "daily RSI < 80",
                           f"{_f(rsi_d):.0f}" if rsi_d is not None else "—",
                           (rsi_d is not None and float(rsi_d) < 80)))
            else:
                t1.append(("R11 RSI room", "daily RSI > 20",
                           f"{_f(rsi_d):.0f}" if rsi_d is not None else "—",
                           (rsi_d is not None and float(rsi_d) > 20)))

            # R12 Pattern — resolved by gate2 if supplied
            r12_val = "Gate2 ✓" if gate2 is True else ("Gate2 ✗" if gate2 is False else "chart")
            t1.append(("R12 Pattern", f"30-day {'accum/consol' if side == 'LONG' else 'distrib/brkdn'}",
                       r12_val, gate2))

            t1_total = 11 if side == "LONG" else 10
            t1_auto = [r for r in t1 if r[3] is not None]
            t1_human = [r for r in t1 if r[3] is None]
            t1_pass = sum(1 for r in t1_auto if r[3])
            t1_human_n = len(t1_human)

            # ════════ TIER 2 ════════
            t2 = []
            t2.append(("F1 News", "no blackout/ex-date", "blackout" if in_blackout else "clear", not in_blackout))

            if piv and cmp:
                pp, r1, s1 = _f(piv[0]), _f(piv[1]), _f(piv[2])
                if side == "LONG" and pp and r1:
                    room = (r1 - cmp) / cmp * 100 if cmp else 0
                    t2.append(("F2 Pivot", "above PP, room>1%",
                               f"CMP {cmp:.0f} PP {pp:.0f} R1 {r1:.0f}", cmp > pp and room > 1.0))
                elif side == "SHORT" and pp and s1:
                    room = (cmp - s1) / cmp * 100 if cmp else 0
                    t2.append(("F2 Pivot", "below PP, room>1%",
                               f"CMP {cmp:.0f} PP {pp:.0f} S1 {s1:.0f}", cmp < pp and room > 1.0))
                else:
                    t2.append(("F2 Pivot", "pivot room", "no pivot", None))
            else:
                t2.append(("F2 Pivot", "pivot room", "no pivot/cmp", None))

            # F3 Fibonacci — proxy via gate2 (1D structure) if supplied
            f3_val = "Gate2 ✓" if gate2 is True else ("Gate2 ✗" if gate2 is False else "chart")
            t2.append(("F3 Fib", "at 38.2/50/61.8", f3_val, gate2))

            if piv and cmp:
                pp, r1, s1 = _f(piv[0]), _f(piv[1]), _f(piv[2])
                if side == "LONG" and r1 and s1 and cmp:
                    risk = cmp - s1
                    rr = (r1 - cmp) / risk if risk and risk > 0 else None
                    t2.append(("F4 R:R", ">=1:2", f"{rr:.2f}" if rr else "—", (rr is not None and rr >= 2.0)))
                elif side == "SHORT" and s1 and r1 and cmp:
                    risk = r1 - cmp
                    rr = (cmp - s1) / risk if risk and risk > 0 else None
                    t2.append(("F4 R:R", ">=1:2", f"{rr:.2f}" if rr else "—", (rr is not None and rr >= 2.0)))
                else:
                    t2.append(("F4 R:R", ">=1:2", "no pivot", None))
            else:
                t2.append(("F4 R:R", ">=1:2", "no pivot", None))

            win = "14:00-15:30" if side == "LONG" else "10:30-12:00"
            t2.append(("F5 Window", f"{win} IST", "manual", None))
            t2.append(("F6 Instrument", "Futures 1-5d DTE>=3", "manual", None))

            if basis_rows and len(basis_rows) >= 2:
                lb = _f(basis_rows[0][1]); ob = _f(basis_rows[-1][1])
                if lb is not None and ob is not None:
                    f7_ok = lb >= ob if side == "LONG" else lb <= ob
                    t2.append(("F7 Deriv", "OI + basis", f"basis {lb:.1f}", f7_ok))
                else:
                    t2.append(("F7 Deriv", "OI + basis", "no basis", None))
            else:
                t2.append(("F7 Deriv", "OI + basis", "no basis", None))

            t2_auto = [r for r in t2 if r[3] is not None]
            t2_human = [r for r in t2 if r[3] is None]
            t2_pass = sum(1 for r in t2_auto if r[3])
            t2_human_n = len(t2_human)

            # ── verdict ──
            best_t1 = t1_pass + t1_human_n
            best_t2 = t2_pass + t2_human_n
            if best_t1 < 8:
                verdict = f"REJECT — Tier1 max {best_t1}/{t1_total}, can't reach 8."
                vclass = "reject"
            elif t1_pass >= 8 and t2_pass >= 5:
                if t1_pass >= 10 and t2_pass >= 6:
                    verdict = f"STRONG — Tier1 {t1_pass}/{t1_total}, Tier2 {t2_pass}/7. Clean setup."
                    vclass = "strong"
                else:
                    verdict = f"VALID — Tier1 {t1_pass}/{t1_total}, Tier2 {t2_pass}/7. Entry allowed."
                    vclass = "valid"
            else:
                if t1_human_n or t2_human_n:
                    verdict = (f"CONDITIONAL — objective Tier1 {t1_pass}/{t1_total}, Tier2 {t2_pass}/7. "
                               f"Depends on remaining chart confirmations.")
                    vclass = "conditional"
                else:
                    verdict = f"WEAK — Tier1 {t1_pass}/{t1_total}, Tier2 {t2_pass}/7 below threshold."
                    vclass = "weak"

            ts = datetime.now().strftime("%d-%b %H:%M IST")
            return {
                "ok": True, "symbol": symbol, "company": company, "segment": segment,
                "side": side, "gvm": _f(gvm), "ts": ts,
                "tier1": [{"rule": r[0], "cond": r[1], "val": r[2], "state": st(r[3])} for r in t1],
                "tier2": [{"rule": r[0], "cond": r[1], "val": r[2], "state": st(r[3])} for r in t2],
                "t1_pass": t1_pass, "t1_auto_n": len(t1_auto), "t1_human_n": t1_human_n, "t1_total": t1_total,
                "t2_pass": t2_pass, "t2_auto_n": len(t2_auto), "t2_human_n": t2_human_n,
                "verdict": verdict, "verdict_class": vclass,
            }
    except Exception as e:
        return {"ok": False, "error": f"DB error: {str(e)[:160]}"}


def native_trade_check(query: str, gate1: bool = None, gate2: bool = None) -> str:
    """Markdown wrapper (used by native_router for /ask text path)."""
    side = _parse_side(query)
    d = compute_trade_check(query, side, gate1, gate2)
    if not d.get("ok"):
        return f"**Trade Check — v3.3**\n{d.get('error', 'error')}"

    def mark(state):
        return {"pass": "PASS", "fail": "FAIL", "chart": "🟡 chart"}[state]

    out = [f"**Trade Check v3.3 — {d['company']} ({d['symbol']}) · {d['side']}**"]
    gv = f"GVM {d['gvm']:.2f} · " if d.get("gvm") is not None else ""
    out.append(f"_{d['segment']} · {gv}{d['ts']} · native $0_")
    out.append("\n**TIER 1** _(objective from DB; chart rows need confirmation)_")
    out.append("| Rule | Condition | Value | State |")
    out.append("| --- | --- | --- | --- |")
    for r in d["tier1"]:
        out.append(f"| {r['rule']} | {r['cond']} | {r['val']} | {mark(r['state'])} |")
    out.append(f"\n**Tier1: {d['t1_pass']}/{d['t1_auto_n']} auto-pass** "
               f"({d['t1_human_n']} chart) · need 8/{d['t1_total']}")
    out.append("\n**TIER 2** _(min 5/7 to enter)_")
    out.append("| Filter | Condition | Value | State |")
    out.append("| --- | --- | --- | --- |")
    for r in d["tier2"]:
        out.append(f"| {r['rule']} | {r['cond']} | {r['val']} | {mark(r['state'])} |")
    out.append(f"\n**Tier2: {d['t2_pass']}/{d['t2_auto_n']} auto-pass** "
               f"({d['t2_human_n']} manual) · need 5/7")
    out.append("\n---")
    out.append(f"**Verdict: {d['verdict']}**")
    out.append("_Personal trade context — not a V8 algo signal. v3.3 objective subset._")
    return "\n".join(out)
