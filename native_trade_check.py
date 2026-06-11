"""
Native v3.3 Trade Check — zero-token, pure Railway DB.

Computes the OBJECTIVE subset of the canonical v3.3 framework
(session_log id=143 + 209 + 240) directly from v8_metrics + gvm_scores
+ v8_paper_pivots. Subjective/chart rules (5-min strength, 1D pattern,
news/events, fib bounce) are surfaced as HUMAN-INPUT rows the trader
confirms — never machine-guessed.

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
    # direct symbol hit
    cur.execute(
        "SELECT symbol, company_name, segment FROM gvm_scores WHERE UPPER(symbol)=%s LIMIT 1",
        (raw,),
    )
    r = cur.fetchone()
    if r:
        return r
    # try each word as a symbol / company fragment, longest first
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


def native_trade_check(query: str) -> str:
    """
    Entry point. query like 'trade check RELIANCE long' or 'check INFY short'.
    Returns markdown string.
    """
    side = _parse_side(query)
    # strip command + side words to leave the symbol text
    cleaned = re.sub(
        r"\b(trade\s*check|trade\s*journal|journal\s*check|check|evaluate|review|analyse|analyze|stock|on|for|a|the|long|short|buy|sell)\b",
        " ", query, flags=re.I,
    )
    cleaned = cleaned.strip()
    if not cleaned:
        return ("**Trade Check — v3.3**\nSpecify a symbol, e.g. "
                "`trade check RELIANCE long` or `check INFY short`.")

    try:
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            resolved = _resolve_symbol(cur, cleaned)
            if not resolved:
                return f"**Trade Check — v3.3**\nNo stock found for '{cleaned.strip()}'."
            symbol, company, segment = resolved

            # ── pull metrics ──
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
                return (f"**{company} ({symbol})**\n"
                        f"No V8 metrics today (may be outside futures universe). "
                        f"Trade check needs v8_metrics data.")
            (gvm, dma20, dma50, dma200, rsi_m, rsi_w, rsi_d,
             wk_ret, mo_ret, yr_ret, mom2d, day1d, sec_w, sec_m, wk52, vol) = m

            # ── market mood (R1) — count fails via ADR + nifty d/w/m proxy ──
            cur.execute("SELECT adr FROM adr_daily ORDER BY price_date DESC LIMIT 1")
            adr_row = cur.fetchone()
            adr_val = _f(adr_row[0]) if adr_row else 1.0
            # crude mood: bearish if adr<0.8
            mood_bearish = adr_val is not None and adr_val < 0.8

            # ── sector peers same direction (R3) ──
            cur.execute(
                """SELECT COUNT(*) FROM v8_metrics v
                   JOIN gvm_scores g ON g.symbol=v.symbol
                   WHERE g.segment=%s AND v.symbol<>%s
                     AND v.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                     AND v.day_1d IS NOT NULL AND v.day_1d %s 0""",
                (segment, symbol, ">" if side == "LONG" else "<"),
            )
            peers_aligned = (cur.fetchone()[0] or 0) >= 2

            # ── pivots (Tier2 F2/F3) ──
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

            # ── blackout (Tier2 F1) ──
            cur.execute(
                """SELECT 1 FROM earnings_calendar
                   WHERE UPPER(ticker)=%s AND ex_date IN (CURRENT_DATE, CURRENT_DATE+INTERVAL '1 day') LIMIT 1""",
                (symbol,),
            )
            in_blackout = cur.fetchone() is not None

            # ── derivatives (Tier2 F7) — OI quadrant + basis ──
            cur.execute(
                """SELECT basis, basis_pct FROM futures_basis
                   WHERE symbol=%s ORDER BY ts DESC LIMIT 5""",
                (symbol,),
            )
            basis_rows = cur.fetchall()

            # ════════ TIER 1 ════════
            t1 = []  # (rule, condition, value, pass|None)

            t1.append(("R1 Market", "not extremely " + ("bearish" if side == "LONG" else "bullish"),
                       f"ADR {adr_val:.2f}" if adr_val is not None else "—",
                       (not mood_bearish) if side == "LONG" else True))

            if side == "LONG":
                t1.append(("R2 Sector", "sec_week>0 AND sec_month>0",
                           f"W {_f(sec_w):.1f} / M {_f(sec_m):.1f}" if sec_w is not None else "—",
                           (sec_w is not None and sec_m is not None and float(sec_w) > 0 and float(sec_m) > 0)))
            else:
                t1.append(("R2 Sector", "sec_week<0 AND sec_month<0",
                           f"W {_f(sec_w):.1f} / M {_f(sec_m):.1f}" if sec_w is not None else "—",
                           (sec_w is not None and sec_m is not None and float(sec_w) < 0 and float(sec_m) < 0)))

            t1.append(("R3 Peers", f"2+ peers {'up' if side == 'LONG' else 'down'} in {segment[:14] if segment else '?'}",
                       "yes" if peers_aligned else "no", peers_aligned))

            if side == "LONG":
                t1.append(("R4 GVM", "GVM>=7.0", f"{_f(gvm):.2f}" if gvm is not None else "—",
                           (gvm is not None and float(gvm) >= 7.0)))

            # R6 MAs
            mas = [dma20, dma50, dma200]
            if side == "LONG":
                above = sum(1 for x in mas if x is not None and float(x) > 0)
                t1.append(("R6 MAs", "price above 2 of 3 MAs", f"{above}/3 above", above >= 2))
            else:
                below = sum(1 for x in mas if x is not None and float(x) < 0)
                t1.append(("R6 MAs", "price below 2 of 3 MAs", f"{below}/3 below", below >= 2))

            t1.append(("R7 Volume", f"1-mo {'buying' if side == 'LONG' else 'selling'} pattern",
                       "chart", None))  # human-input

            if side == "LONG":
                t1.append(("R8 RSI M/W", "RSI_m>=50 AND RSI_w>=50",
                           f"M {_f(rsi_m):.0f} / W {_f(rsi_w):.0f}" if rsi_m is not None else "—",
                           (rsi_m is not None and rsi_w is not None and float(rsi_m) >= 50 and float(rsi_w) >= 50)))
                t1.append(("R9 Returns", "week>0 AND month>0",
                           f"W {_f(wk_ret):.1f}% / M {_f(mo_ret):.1f}%" if wk_ret is not None else "—",
                           (wk_ret is not None and mo_ret is not None and float(wk_ret) > 0 and float(mo_ret) > 0)))
            else:
                t1.append(("R8 RSI M/W", "RSI_m<=50 AND RSI_w<=50",
                           f"M {_f(rsi_m):.0f} / W {_f(rsi_w):.0f}" if rsi_m is not None else "—",
                           (rsi_m is not None and rsi_w is not None and float(rsi_m) <= 50 and float(rsi_w) <= 50)))
                t1.append(("R9 Returns", "week<0 AND month<0",
                           f"W {_f(wk_ret):.1f}% / M {_f(mo_ret):.1f}%" if wk_ret is not None else "—",
                           (wk_ret is not None and mo_ret is not None and float(wk_ret) < 0 and float(mo_ret) < 0)))

            t1.append(("R10 5-min", f"5M {'recovery+strong close' if side == 'LONG' else 'weakness+weak close'}",
                       "chart", None))  # human-input

            if side == "LONG":
                t1.append(("R11 RSI room", "daily RSI < 80",
                           f"{_f(rsi_d):.0f}" if rsi_d is not None else "—",
                           (rsi_d is not None and float(rsi_d) < 80)))
            else:
                t1.append(("R11 RSI room", "daily RSI > 20",
                           f"{_f(rsi_d):.0f}" if rsi_d is not None else "—",
                           (rsi_d is not None and float(rsi_d) > 20)))

            t1.append(("R12 Pattern", f"30-day {'accum/consol' if side == 'LONG' else 'distrib/breakdown'}",
                       "chart", None))  # human-input

            # ── score Tier1 ──
            t1_total = 11 if side == "LONG" else 10
            t1_auto = [r for r in t1 if r[3] is not None]
            t1_human = [r for r in t1 if r[3] is None]
            t1_pass = sum(1 for r in t1_auto if r[3])
            t1_human_n = len(t1_human)
            # objective pass count; human rules shown separately
            t1_advance = (t1_pass + t1_human_n) >= 8  # optimistic if human gates pass

            # ════════ TIER 2 ════════
            t2 = []
            t2.append(("F1 News/Events", "no blackout/ex-date", "blackout" if in_blackout else "clear", not in_blackout))

            # F2 pivot + room
            if piv and cmp:
                pp, r1, s1 = _f(piv[0]), _f(piv[1]), _f(piv[2])
                if side == "LONG" and pp and r1:
                    room = (r1 - cmp) / cmp * 100 if cmp else 0
                    f2_ok = cmp > pp and room > 1.0
                    t2.append(("F2 Pivot/Fib", "above PP, room>1% to R1",
                               f"CMP {cmp:.0f} PP {pp:.0f} R1 {r1:.0f}", f2_ok))
                elif side == "SHORT" and pp and s1:
                    room = (cmp - s1) / cmp * 100 if cmp else 0
                    f2_ok = cmp < pp and room > 1.0
                    t2.append(("F2 Pivot/Fib", "below PP, room>1% to S1",
                               f"CMP {cmp:.0f} PP {pp:.0f} S1 {s1:.0f}", f2_ok))
                else:
                    t2.append(("F2 Pivot/Fib", "pivot room", "no pivot data", None))
            else:
                t2.append(("F2 Pivot/Fib", "pivot room", "no pivot/cmp", None))

            t2.append(("F3 Fibonacci", "at 38.2/50/61.8 level", "chart", None))  # human

            # F4 R:R (pivot based)
            if piv and cmp:
                pp, r1, s1 = _f(piv[0]), _f(piv[1]), _f(piv[2])
                if side == "LONG" and r1 and s1 and cmp:
                    reward = r1 - cmp
                    risk = cmp - s1
                    rr = reward / risk if risk and risk > 0 else None
                    t2.append(("F4 R:R", ">=1:2", f"{rr:.2f}" if rr else "—",
                               (rr is not None and rr >= 2.0)))
                elif side == "SHORT" and s1 and r1 and cmp:
                    reward = cmp - s1
                    risk = r1 - cmp
                    rr = reward / risk if risk and risk > 0 else None
                    t2.append(("F4 R:R", ">=1:2", f"{rr:.2f}" if rr else "—",
                               (rr is not None and rr >= 2.0)))
                else:
                    t2.append(("F4 R:R", ">=1:2", "no pivot", None))
            else:
                t2.append(("F4 R:R", ">=1:2", "no pivot", None))

            win = "14:00-15:30" if side == "LONG" else "10:30-12:00"
            t2.append(("F5 Entry window", f"{win} IST", "manual", None))  # human / time-of-day
            t2.append(("F6 Instrument", "Futures, 1-5d, DTE>=3", "manual", None))  # human

            # F7 derivatives — basis trend
            if basis_rows and len(basis_rows) >= 2:
                latest_basis = _f(basis_rows[0][1])
                older_basis = _f(basis_rows[-1][1])
                if latest_basis is not None and older_basis is not None:
                    if side == "LONG":
                        f7_ok = latest_basis >= older_basis  # widening premium
                    else:
                        f7_ok = latest_basis <= older_basis  # discount growing
                    t2.append(("F7 Derivatives", "OI + basis align",
                               f"basis {latest_basis:.1f}", f7_ok))
                else:
                    t2.append(("F7 Derivatives", "OI + basis align", "no basis", None))
            else:
                t2.append(("F7 Derivatives", "OI + basis align", "no basis data", None))

            t2_auto = [r for r in t2 if r[3] is not None]
            t2_human = [r for r in t2 if r[3] is None]
            t2_pass = sum(1 for r in t2_auto if r[3])
            t2_human_n = len(t2_human)

            # ── build output ──
            ts = datetime.now().strftime("%d-%b %H:%M IST")
            out = [f"**Trade Check v3.3 — {company} ({symbol}) · {side}**",
                   f"_{segment} · GVM {_f(gvm):.2f} · {ts} · native $0_" if gvm is not None
                   else f"_{segment} · {ts} · native $0_"]

            # Tier1 table
            out.append("\n**TIER 1** _(objective from DB; chart rows need your confirmation)_")
            out.append("| Rule | Condition | Value | Auto |")
            out.append("| --- | --- | --- | --- |")
            for rule, cond, val, ok in t1:
                mark = "🟡 chart" if ok is None else _yn(ok)
                out.append(f"| {rule} | {cond} | {val} | {mark} |")
            out.append(f"\n**Tier1 objective: {t1_pass}/{len(t1_auto)} pass** "
                       f"({t1_human_n} chart rules you confirm) · need 8/{t1_total}")

            # Tier2 table
            out.append("\n**TIER 2** _(min 5/7 to enter)_")
            out.append("| Filter | Condition | Value | Auto |")
            out.append("| --- | --- | --- | --- |")
            for rule, cond, val, ok in t2:
                mark = "🟡 manual" if ok is None else _yn(ok)
                out.append(f"| {rule} | {cond} | {val} | {mark} |")
            out.append(f"\n**Tier2 objective: {t2_pass}/{len(t2_auto)} pass** "
                       f"({t2_human_n} manual) · need 5/7")

            # verdict (objective-only, honest)
            out.append("\n---")
            if t1_pass < (8 - t1_human_n):
                out.append(f"**Verdict: REJECT** — objective Tier1 too low ({t1_pass} auto-pass, "
                           f"can't reach 8 even if all {t1_human_n} chart rules pass).")
            else:
                best_t1 = t1_pass + t1_human_n
                best_t2 = t2_pass + t2_human_n
                if best_t1 >= 10 and best_t2 >= 6:
                    out.append("**Verdict: potentially STRONG** — if your chart confirmations pass. "
                               "Confirm 5-min strength, 1D pattern, fib & news.")
                else:
                    out.append("**Verdict: CONDITIONAL** — objective base is OK; final call depends on "
                               "your chart-rule confirmations (5-min, 1D, fib, news, entry window).")
            out.append("_Personal trade context — not a V8 algo signal. v3.3 objective subset; "
                       "chart/subjective rules are yours to confirm._")

            return "\n".join(out)

    except Exception as e:
        return f"**Trade Check v3.3**\nDB error: {str(e)[:160]}"
