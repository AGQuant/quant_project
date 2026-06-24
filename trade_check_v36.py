"""
trade_check_v36.py — Trade Check v3.6 (Tier-1 process gate).

LOCKED 24-Jun-2026 (spec: session_log id=600, parent session_log id=143 v3.3).
Standalone engine for the /api/trade-check endpoint. Runs SIDE-BY-SIDE with
trade_check_v34.py (v3.5 weighted) — that engine and /api/trade-check/v34 are
left untouched.

WHAT v3.6 CHANGES vs v3.3:
  - R1 DROPPED (was market gate) — Arpit is the market gate.
  - R3 GVM is now quality + momentum: GVM >= 6.5 AND M-score >= 7.0
    (was a hard GVM >= 7.0 floor). LONG only — N/A for SHORT.
  - R6 (RSI Month + Weekly) gains a PARTIAL pass: 0.5 when exactly one of the
    two RSI legs fails (was binary 0/1). The ONLY partial-pass rule.
  - R8 UPGRADED to multiframe MA hierarchy avg_1hr > avg_3hr > avg_1day
    (was a subjective 5-min recovery check). SHORT mirrors inverted.
  - R11 NEW — "room left to run": (15d_high - CMP)/CMP > 2.5% (LONG),
    (CMP - 15d_low)/CMP > 2.5% (SHORT).

SCORING:
  LONG  = 11 rules, nominal max 11.5, advance to Tier 2 at score >= 8.0.
  SHORT = 10 rules (R3 GVM skipped), nominal max 10.5, advance at >= 8.0.
  Only R6 is partial (0.0 / 0.5 / 1.0); every other rule is binary 1.0/0.0.

SEPARATION (session_log id=210): reads ONLY v8_metrics, gvm_scores,
intraday_prices, raw_prices, cmp_prices. Does NOT read or write any
v8_paper_* / v8_qualified table. No V8-engine coupling.
"""

import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "")

VERSION = "v3.6"
SPEC_REF = "session_log id=600 (locked 24-Jun-2026) — parent id=143 (v3.3)"

# ─── RULE CONSTANTS (session_log id=600) ─────────────────────────────────
GVM_MIN      = 6.5     # R3: GVM floor (LONG)
MSCORE_MIN   = 7.0     # R3: momentum-score floor (LONG)
RSI_GATE     = 50.0    # R6: month & weekly RSI side gate
RSI_ROOM_HI  = 80.0    # R9: LONG daily RSI ceiling
RSI_ROOM_LO  = 20.0    # R9: SHORT daily RSI floor
ROOM_MIN_PCT = 2.5     # R11: minimum % room left to run / fall
ADVANCE_MIN  = 8.0     # score >= this advances to Tier 2

# Nominal denominators per the locked spec (id=600). Achievable max is 11.0 /
# 10.0 since every rule tops at 1.0; the spec fixes the display denominator.
MAX_LONG  = 11.5
MAX_SHORT = 10.5


def get_conn():
    return psycopg.connect(DATABASE_URL)


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


def _avg(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ─── DATA FETCH (read-only, NO V8-engine tables) ─────────────────────────

def _resolve(cur, raw: str):
    """Resolve a free-typed symbol via gvm_scores. Returns
    (symbol, gvm_score, m_score, segment) or None."""
    s = raw.strip().upper()
    cur.execute("""SELECT symbol, gvm_score, m_score, segment
                   FROM gvm_scores WHERE UPPER(symbol)=%s LIMIT 1""", (s,))
    r = cur.fetchone()
    if not r:
        cur.execute("""SELECT symbol, gvm_score, m_score, segment
                       FROM gvm_scores WHERE UPPER(symbol) LIKE %s
                       ORDER BY symbol LIMIT 1""", (s + "%",))
        r = cur.fetchone()
    if not r:
        return None
    return r[0], r[1], r[2], r[3]


def _metrics(cur, symbol: str) -> Optional[Dict[str, Any]]:
    cur.execute("""SELECT dma_20, dma_50, dma_200, rsi_month, rsi_weekly,
                          daily_rsi, week_return, month_return,
                          sector_week, sector_month, day_1d
                   FROM v8_metrics WHERE symbol=%s
                     AND score_date=(SELECT MAX(score_date) FROM v8_metrics
                                     WHERE symbol=%s) LIMIT 1""",
                (symbol, symbol))
    r = cur.fetchone()
    if not r:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, r))


def _peer_dir(cur, symbol: str, segment: Optional[str], side: str) -> int:
    """Count same-segment peers moving on side today (day_1d)."""
    if not segment:
        return 0
    op = ">" if side == "LONG" else "<"
    cur.execute(f"""SELECT COUNT(*) FROM v8_metrics v
                    JOIN gvm_scores g ON g.symbol=v.symbol
                    WHERE g.segment=%s AND v.symbol<>%s
                      AND v.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                      AND v.day_1d {op} 0""", (segment, symbol))
    return cur.fetchone()[0] or 0


def _cmp(cur, symbol: str) -> Optional[float]:
    cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (symbol,))
    r = cur.fetchone()
    if r and r[0] is not None:
        return _f(r[0])
    cur.execute("""SELECT close FROM intraday_prices WHERE symbol=%s
                   ORDER BY ts DESC LIMIT 1""", (symbol,))
    r = cur.fetchone()
    if r and r[0] is not None:
        return _f(r[0])
    cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s
                   ORDER BY price_date DESC LIMIT 1""", (symbol,))
    r = cur.fetchone()
    return _f(r[0]) if r and r[0] is not None else None


def _intraday_closes(cur, symbol: str) -> List[float]:
    """Today's 5-min closes, chronological (oldest -> newest)."""
    cur.execute("""SELECT close FROM intraday_prices WHERE symbol=%s
                   AND ts::date=(SELECT MAX(ts::date) FROM intraday_prices
                                 WHERE symbol=%s)
                   ORDER BY ts""", (symbol, symbol))
    return [_f(r[0]) for r in cur.fetchall() if r[0] is not None]


def _raw_recent(cur, symbol: str, days: int = 30) -> List[Dict[str, float]]:
    """Last `days` EOD bars, chronological."""
    cur.execute("""SELECT price_date, open, high, low, close, volume
                   FROM raw_prices WHERE symbol=%s
                   ORDER BY price_date DESC LIMIT %s""", (symbol, days))
    rows = cur.fetchall()[::-1]
    return [{"date": r[0], "open": _f(r[1]), "high": _f(r[2]),
             "low": _f(r[3]), "close": _f(r[4]), "vol": _f(r[5])} for r in rows]


def _room_extreme(cur, symbol: str, side: str) -> Optional[float]:
    """R11 — 15d_high (LONG) / 15d_low (SHORT) over the prior 21 calendar
    days (~15 trading days), EXCLUDING today, from raw_prices."""
    agg = "MAX(high)" if side == "LONG" else "MIN(low)"
    cur.execute(f"""SELECT {agg} FROM raw_prices WHERE symbol=%s
                    AND price_date >= CURRENT_DATE - INTERVAL '21 days'
                    AND price_date <  CURRENT_DATE""", (symbol,))
    r = cur.fetchone()
    return _f(r[0]) if r and r[0] is not None else None


# ─── SCORE ───────────────────────────────────────────────────────────────

def _score(symbol_text: str, side: str) -> Dict[str, Any]:
    side = side.upper()
    if side not in ("LONG", "SHORT"):
        return {"error": "side must be LONG or SHORT"}

    with get_conn() as conn, conn.cursor() as cur:
        resolved = _resolve(cur, symbol_text)
        if not resolved:
            return {"error": f"No stock found for '{symbol_text}'."}
        symbol, gvm, mscore, segment = resolved
        m = _metrics(cur, symbol)
        if not m:
            return {"error": f"No v8_metrics for {symbol} (outside futures universe)."}
        peers = _peer_dir(cur, symbol, segment, side)
        cmp_v = _cmp(cur, symbol)
        intra = _intraday_closes(cur, symbol)
        raw30 = _raw_recent(cur, symbol, 30)
        room_extreme = _room_extreme(cur, symbol, side)

    is_long = side == "LONG"
    rules: List[Dict[str, Any]] = []

    def add(rid, name, cond, val, score):
        rules.append({"rule": rid, "name": name, "cond": cond,
                      "val": val, "score": score})

    # R1 — Sector aligned
    sw, sm = _f(m["sector_week"]), _f(m["sector_month"])
    r1 = (sw > 0 and sm > 0) if is_long else (sw < 0 and sm < 0)
    add("R1", "Sector aligned",
        "week & month >0" if is_long else "week & month <0",
        f"W {sw:+.2f} / M {sm:+.2f}", 1.0 if r1 else 0.0)

    # R2 — Sector peers on side
    r2 = peers >= 2
    add("R2", "Sector peers on side",
        ">=2 peers " + ("up" if is_long else "down") + " today",
        f"{peers} peers", 1.0 if r2 else 0.0)

    # R3 — GVM quality + momentum (LONG only)
    if is_long:
        g, ms = _f(gvm), _f(mscore)
        r3 = g >= GVM_MIN and ms >= MSCORE_MIN
        add("R3", "GVM quality + momentum",
            f"GVM>={GVM_MIN} AND M>={MSCORE_MIN}",
            f"GVM {g:.2f} / M {ms:.2f}", 1.0 if r3 else 0.0)

    # R4 — 2 of 3 MAs on side
    mas = [_f(m["dma_20"]), _f(m["dma_50"]), _f(m["dma_200"])]
    on_side = sum(1 for x in mas if (x > 0 if is_long else x < 0))
    r4 = on_side >= 2
    add("R4", "2 of 3 MAs " + ("above" if is_long else "below"),
        ">=2 of DMA 20/50/200 " + ("positive" if is_long else "negative"),
        f"20 {mas[0]:+.1f} / 50 {mas[1]:+.1f} / 200 {mas[2]:+.1f}",
        1.0 if r4 else 0.0)

    # R5 — Volume pattern over last 22 sessions
    cur_dir_vol_up = cur_dir_vol_dn = 0.0
    nu = nd = 0
    for i in range(1, len(raw30)):
        prev_c, c, v = raw30[i - 1]["close"], raw30[i]["close"], raw30[i]["vol"]
        if c > prev_c:
            cur_dir_vol_up += v; nu += 1
        elif c < prev_c:
            cur_dir_vol_dn += v; nd += 1
    avg_up = cur_dir_vol_up / nu if nu else 0.0
    avg_dn = cur_dir_vol_dn / nd if nd else 0.0
    r5 = (avg_up > avg_dn) if is_long else (avg_dn > avg_up)
    add("R5", "Volume " + ("buying" if is_long else "selling") + " pattern",
        "avg vol up-days " + (">" if is_long else "<") + " down-days (22d)",
        f"up {avg_up:,.0f} vs dn {avg_dn:,.0f}", 1.0 if r5 else 0.0)

    # R6 — RSI Month + Weekly (PARTIAL: 0.0 / 0.5 / 1.0)
    rm, rwk = _f(m["rsi_month"]), _f(m["rsi_weekly"])
    if is_long:
        legs = int(rm >= RSI_GATE) + int(rwk >= RSI_GATE)
        cond = f"both >= {RSI_GATE:.0f} (one fails = 0.5)"
    else:
        legs = int(rm <= RSI_GATE) + int(rwk <= RSI_GATE)
        cond = f"both <= {RSI_GATE:.0f} (one fails = 0.5)"
    r6 = 1.0 if legs == 2 else (0.5 if legs == 1 else 0.0)
    add("R6", "RSI Month + Weekly", cond, f"Mo {rm:.1f} / Wk {rwk:.1f}", r6)

    # R7 — Week + Month returns on side
    wkr, mor = _f(m["week_return"]), _f(m["month_return"])
    r7 = (wkr > 0 and mor > 0) if is_long else (wkr < 0 and mor < 0)
    add("R7", "Week + Month returns",
        "both >0" if is_long else "both <0",
        f"Wk {wkr:+.2f} / Mo {mor:+.2f}", 1.0 if r7 else 0.0)

    # R8 — MA hierarchy (multiframe strength/weakness)
    if len(intra) >= 12:
        a1 = _avg(intra[-12:]); a3 = _avg(intra[-36:]); ad = _avg(intra)
        if is_long:
            r8 = (a1 is not None and a3 is not None and ad is not None
                  and a1 > a3 > ad)
        else:
            r8 = (a1 is not None and a3 is not None and ad is not None
                  and a1 < a3 < ad)
        v8val = f"1hr {a1:.1f} / 3hr {a3:.1f} / 1d {ad:.1f}"
    else:
        r8 = False
        v8val = f"insufficient intraday bars ({len(intra)})"
    add("R8", "MA hierarchy multiframe",
        "1hr>3hr>1day" if is_long else "1hr<3hr<1day", v8val,
        1.0 if r8 else 0.0)

    # R9 — Daily RSI room
    rd = _f(m["daily_rsi"])
    r9 = (rd < RSI_ROOM_HI) if is_long else (rd > RSI_ROOM_LO)
    add("R9", "Daily RSI room",
        f"< {RSI_ROOM_HI:.0f}" if is_long else f"> {RSI_ROOM_LO:.0f}",
        f"{rd:.1f}", 1.0 if r9 else 0.0)

    # R10 — 1D 30-day pattern
    r10, r10val = _pattern_30d(raw30, is_long)
    add("R10", "1D 30-day pattern",
        "accumulation/consolidation" if is_long else "distribution/breakdown",
        r10val, 1.0 if r10 else 0.0)

    # R11 — Room left to run / fall
    if cmp_v and room_extreme:
        if is_long:
            room_pct = (room_extreme - cmp_v) / cmp_v * 100.0
        else:
            room_pct = (cmp_v - room_extreme) / cmp_v * 100.0
        r11 = room_pct > ROOM_MIN_PCT
        r11val = (f"{'15d_high' if is_long else '15d_low'} {room_extreme:.1f} "
                  f"vs CMP {cmp_v:.1f} -> {room_pct:+.2f}%")
    else:
        r11 = False
        r11val = "no CMP / 15d extreme data"
    add("R11", "Room left to " + ("run" if is_long else "fall"),
        f"> {ROOM_MIN_PCT}%", r11val, 1.0 if r11 else 0.0)

    # ── TALLY ───────────────────────────────────────────────────────────
    score = round(sum(r["score"] for r in rules), 2)
    max_score = MAX_LONG if is_long else MAX_SHORT
    advance = score >= ADVANCE_MIN
    verdict = "ADVANCE" if advance else "WATCH"
    reason = (f"{score}/{max_score} (>= {ADVANCE_MIN} advances to Tier 2)"
              if advance else
              f"{score}/{max_score} — below {ADVANCE_MIN} Tier-2 bar")

    return {
        "version": VERSION,
        "symbol": symbol,
        "side": side,
        "as_of": datetime.now().strftime("%d-%b-%Y %H:%M IST"),
        "rules": rules,
        "score": score,
        "max_score": max_score,
        "threshold": ADVANCE_MIN,
        "advance": advance,
        "verdict": verdict,
        "reason": reason,
        "separation_note": "Independent of V8 paper engine — no v8_paper/v8_qualified read.",
    }


def _pattern_30d(raw30: List[Dict[str, float]], is_long: bool):
    """R10 — deterministic 30-day EOD structure read.
    LONG  PASS = higher lows (accumulation) OR tight range (consolidation).
    SHORT PASS = lower highs (distribution) OR close below prior support
                 (breakdown)."""
    if len(raw30) < 10:
        return False, f"insufficient history ({len(raw30)}d)"
    half = len(raw30) // 2
    older, recent = raw30[:half], raw30[half:]
    lows_o = min(r["low"] for r in older)
    lows_r = min(r["low"] for r in recent)
    highs_o = max(r["high"] for r in older)
    highs_r = max(r["high"] for r in recent)
    hi_all = max(r["high"] for r in raw30)
    lo_all = min(r["low"] for r in raw30)
    avg_c = _avg([r["close"] for r in raw30]) or 1.0
    rng_pct = (hi_all - lo_all) / avg_c * 100.0
    last_c = raw30[-1]["close"]
    if is_long:
        higher_lows = lows_r > lows_o
        tight = rng_pct < 10.0
        ok = higher_lows or tight
        tag = ("higher-lows" if higher_lows else
               ("tight-range" if tight else "no accumulation"))
        return ok, f"{tag} (range {rng_pct:.1f}%, lows {lows_o:.1f}->{lows_r:.1f})"
    lower_highs = highs_r < highs_o
    breakdown = last_c < lows_o
    ok = lower_highs or breakdown
    tag = ("lower-highs" if lower_highs else
           ("breakdown" if breakdown else "no distribution"))
    return ok, f"{tag} (highs {highs_o:.1f}->{highs_r:.1f}, close {last_c:.1f})"


# ─── PUBLIC API ───────────────────────────────────────────────────────────

def trade_check_v36(symbol: str, side: str = "LONG") -> Dict[str, Any]:
    """Score `symbol` against the v3.6 Tier-1 rule set. All 11 LONG / 10 SHORT
    rules are data-derived — no caller chart gates."""
    try:
        return _score(symbol, side)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}


def render_table(result: Dict[str, Any]) -> str:
    if "error" in result:
        return f"Trade Check {VERSION} error: {result['error']}"
    L = [f"**Trade Check {result['version']} — {result['symbol']} {result['side']}**",
         f"_{result['as_of']}_", "",
         "| Rule | Name | Condition | Value | Score |",
         "|---|---|---|---|---|"]
    for r in result["rules"]:
        L.append(f"| {r['rule']} | {r['name']} | {r['cond']} | {r['val']} | {r['score']} |")
    L.append("")
    L.append(f"**Score: {result['score']}/{result['max_score']}** -> "
             f"**{result['verdict']}** — {result['reason']}")
    L.append(f"_{result['separation_note']}_")
    return "\n".join(L)
