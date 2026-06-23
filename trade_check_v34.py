"""
Trade Check v3.5 — Weighted Tier-1 + Core-Gate scoring engine.
UPDATE v3.5 (spec id=370): R4 GVM_MIN 7.0->6.6 | R9 week+month tri-state
  (MODERATE when exactly one positive) | F2 pivot-room ratio gate (reads
  v8_paper_pivots PP/R1/S1/R2/S2 — pivot levels only, authorised) | F3
  dynamic fibonacci bounce from raw_prices | F7 reworked to basis+OI
  (PASS basis-on-side+OI, MODERATE basis-on-side+no-OI, FAIL otherwise).
UPDATE v3.4.1: 0.5 partial-pass rule for core gates.
- Both conditions pass (1.0x) → advance, no cap
- 1/2 pass (0.5x) → NEUTRAL, advance but weighted
- Both fail (0.0x) → WATCH (capped)

FIRST CODE implementation of Trade Check (v3.3 was conversational-only,
spec at session_log id=143 + output format id=209).

WHAT v3.4 CHANGES vs v3.3 (faithful re-weighting, NOT new rules):
  - Flat X/11 count  ->  core-gate + weighted score (fixes DIVISLAB 9/11
    outranking EICHERMOT 7/11 despite worse quality).
  - CORE GATES (pass/fail/partial): GVM>=6.6 (LONG, R4) + week&month (R9).
    Fail both => capped at WATCH. Fail one => 0.5x multiplier (neutral, advance).
  - OI/price quadrant (was Tier-2 F7 sub_A in v3.3) PROMOTED into Tier1
    as a weighted rule. basis-delta = strength grade. null-OI => abstain
    (neutral, never false-fail).
  - Side-aware LONG (11 data+chart rules) / SHORT (10, GVM N/A).

HUMAN-IN-AI-LOOP: the 2 chart gates (5-min strength, 1-Day structure) are
NOT machine-readable. Caller passes them as booleans ("you are the gate").
Module auto-scores the 8 data-derivable rules from v8_metrics + futures_basis.

SEPARATION: This module does NOT read v8_qualified or V8 paper signal/journal
tables. Trade Check and the V8 paper engine remain independent systems
(session_log id=210). Reads ONLY: v8_metrics (metrics snapshot), gvm_scores
(segment/peers), futures_basis (basis), raw_prices (F3 fibonacci), and
v8_paper_pivots (F2 — pivot LEVELS ONLY, authorised per cc_task #64 / spec
id=370; same pivot source the screener already uses). No V8-engine coupling.

Runs SIDE-BY-SIDE with v3.3. Does not replace it. Replay-tunable weights.
Optional personal_journal promote is manual-only (caller-triggered).
"""

import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "")

VERSION = "v3.5"
SPEC_PARENT = "session_log id=143 (v3.3) — v3.4 weighted re-impl + v3.4.1 0.5 partial + v3.5 spec id=370 (R4/R9/F2/F3/F7)"

# ─── v3.5 RULE CONSTANTS (spec id=370) ───────────────────────────────────
GVM_MIN_LONG    = 6.6    # R4: minimum GVM for LONG core gate (was 7.0)
PIVOT_RATIO_MIN = 1.25   # F2: (R2-CMP)/(CMP-S2) must exceed this
FIB_TOL         = 0.02   # F3: within 2% of a fib level = bounce

# ─── WEIGHTS (starting values, replay-tunable — NOT locked) ──────────────
# High = quality/trend/structure. Medium = environment. Low = confirmation.
W_MA_STACK      = 3   # 2 of 3 DMAs on correct side
W_RSI_MW        = 3   # RSI month & weekly both on side
W_STRUCT_1D     = 3   # chart gate 2 (1-Day reversal/breakout) — caller-passed
W_OI_QUADRANT   = 3   # PROMOTED from v3.3 Tier2 F7: price+OI buildup
W_SECTOR_MONTH  = 2   # sector_month aligned
W_5MIN          = 2   # chart gate 1 (5-min strength) — caller-passed
W_MARKET_GATE   = 2   # market not extremely against side
W_PEER          = 1   # peers confirming today
W_VOLUME        = 1   # vol_ratio buying/selling conviction
W_RSI_ROOM      = 1   # daily RSI not exhausted
W_PIVOT_ROOM    = 2   # F2 (v3.5) — pivot-room ratio gate
W_FIB           = 2   # F3 (v3.5) — fibonacci bounce

MAX_WEIGHTED = (W_MA_STACK + W_RSI_MW + W_STRUCT_1D + W_OI_QUADRANT +
                W_SECTOR_MONTH + W_5MIN + W_MARKET_GATE + W_PEER +
                W_VOLUME + W_RSI_ROOM + W_PIVOT_ROOM + W_FIB)  # = 25

STRONG_MIN = 15   # core pass + >=15/21
VALID_MIN  = 11   # core pass + >=11/21


def get_conn():
    return psycopg.connect(DATABASE_URL)


def _f(v) -> float:
    return float(v) if v is not None else 0.0


# ─── DATA FETCH (read-only, NO V8-engine tables) ─────────────────────────

def _fetch_metrics(cur, symbol: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """SELECT symbol, gvm_score, dma_20, dma_50, dma_200,
                  rsi_month, rsi_weekly, month_return, week_return,
                  sector_day, sector_month, daily_rsi, vol_ratio, eod_chg
           FROM v8_metrics
           WHERE symbol = %s AND score_date = (
               SELECT MAX(score_date) FROM v8_metrics WHERE symbol = %s)
           LIMIT 1""",
        (symbol.upper(), symbol.upper()),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _fetch_segment(cur, symbol: str) -> Optional[str]:
    cur.execute("SELECT segment FROM gvm_scores WHERE symbol = %s LIMIT 1",
                (symbol.upper(),))
    r = cur.fetchone()
    return r[0] if r else None


def _peer_confirm(cur, symbol: str, segment: str, side: str) -> Optional[bool]:
    """>=2 peers in same segment moving same direction today (eod_chg).
    Returns None if insufficient peer data (rule abstains)."""
    if not segment:
        return None
    cur.execute(
        """SELECT v.eod_chg
           FROM v8_metrics v JOIN gvm_scores g ON v.symbol = g.symbol
           WHERE g.segment = %s AND v.symbol <> %s
             AND v.score_date = (SELECT MAX(score_date) FROM v8_metrics)
             AND v.eod_chg IS NOT NULL""",
        (segment, symbol.upper()),
    )
    rows = cur.fetchall()
    if len(rows) < 2:
        return None
    if side == "LONG":
        return sum(1 for r in rows if _f(r[0]) > 0) >= 2
    return sum(1 for r in rows if _f(r[0]) < 0) >= 2


def _fetch_basis(cur, symbol: str) -> Optional[Dict[str, Any]]:
    """F7 primary source — latest futures_basis row (basis, basis_pct),
    regardless of whether OI is present. None if no row at all -> F7 FAIL."""
    cur.execute(
        """SELECT basis, basis_pct FROM futures_basis
           WHERE symbol = %s ORDER BY ts DESC LIMIT 1""",
        (symbol.upper(),),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {"basis": _f(r[0]), "basis_pct": _f(r[1])}


def _has_oi(cur, symbol: str) -> bool:
    """F7 — does option_chain carry any OI for this symbol? (Only top-mcap
    names have option_chain rows; absence -> MODERATE, not FAIL.)"""
    cur.execute(
        """SELECT 1 FROM option_chain
           WHERE (symbol = %s OR underlying = %s) AND oi IS NOT NULL LIMIT 1""",
        (symbol.upper(), symbol.upper()),
    )
    return cur.fetchone() is not None


def _fetch_cmp(cur, symbol: str) -> Optional[float]:
    """Latest EOD close from raw_prices — used as CMP for F2 + F3."""
    cur.execute(
        """SELECT close FROM raw_prices WHERE symbol = %s
           ORDER BY price_date DESC LIMIT 1""",
        (symbol.upper(),),
    )
    r = cur.fetchone()
    return _f(r[0]) if r and r[0] is not None else None


def _fetch_pivots(cur, symbol: str) -> Optional[Dict[str, float]]:
    """F2 — latest pivot LEVELS from v8_paper_pivots (PP/R1/S1/R2/S2).
    Pivot levels only; no V8 signal/state coupling (cc_task #64 / spec id=370)."""
    cur.execute(
        """SELECT pp, r1, s1, r2, s2 FROM v8_paper_pivots
           WHERE symbol = %s AND pivot_date = (
               SELECT MAX(pivot_date) FROM v8_paper_pivots WHERE symbol = %s)
           LIMIT 1""",
        (symbol.upper(), symbol.upper()),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {"pp": _f(r[0]), "r1": _f(r[1]), "s1": _f(r[2]),
            "r2": _f(r[3]), "s2": _f(r[4])}


# ─── RULE EVALUATORS ─────────────────────────────────────────────────────

def _eval_f7(side: str, basis: Optional[Dict], has_oi: bool) -> Dict:
    """F7 — OI + basis confirmation (spec id=370 v3.5).
      PASS:     basis on side AND OI signal present (option_chain has rows)
      MODERATE: basis on side BUT no OI data (most non-top-mcap stocks)
      FAIL:     basis against side, or no futures_basis row at all
    LONG favours basis>0 (futures premium); SHORT mirrors (basis<0)."""
    if basis is None:
        return {"weight": W_OI_QUADRANT, "earned": 0, "status": "FAIL",
                "detail": "no futures_basis data"}
    b, bp = basis["basis"], basis["basis_pct"]
    favourable = b > 0 if side == "LONG" else b < 0
    want = ">0" if side == "LONG" else "<0"
    if not favourable:
        return {"weight": W_OI_QUADRANT, "earned": 0, "status": "FAIL",
                "detail": f"basis {b:+.2f} ({bp:+.3f}%) not {want}"}
    if has_oi:
        return {"weight": W_OI_QUADRANT, "earned": W_OI_QUADRANT, "status": "PASS",
                "detail": f"basis {b:+.2f} ({bp:+.3f}%) {want} + OI signal present"}
    return {"weight": W_OI_QUADRANT, "earned": round(W_OI_QUADRANT * 0.5, 2),
            "status": "MODERATE",
            "detail": f"basis {b:+.2f} ({bp:+.3f}%) {want}, no OI data (option_chain empty)"}


def _eval_pivot_room(side: str, cmp_v: Optional[float],
                     piv: Optional[Dict]) -> Dict:
    """F2 — pivot-room ratio gate (spec id=370 v3.5).
      LONG: ratio = (R2-CMP)/(CMP-S2); PASS if ratio>1.25 AND CMP>PP.
      SHORT: ratio = (CMP-S2)/(R2-CMP); PASS if ratio>1.25 AND CMP<PP.
      MODERATE = exactly one condition true. FAIL = neither / no data."""
    if piv is None or cmp_v is None:
        return {"earned": 0, "status": "FAIL", "detail": "no pivot/CMP data"}
    pp, r2, s2 = piv["pp"], piv["r2"], piv["s2"]
    if side == "LONG":
        denom = cmp_v - s2
        ratio = (r2 - cmp_v) / denom if denom > 0 else 0.0
        cond_side = cmp_v > pp
    else:
        denom = r2 - cmp_v
        ratio = (cmp_v - s2) / denom if denom > 0 else 0.0
        cond_side = cmp_v < pp
    hits = int(ratio > PIVOT_RATIO_MIN) + int(cond_side)
    detail = (f"ratio={ratio:.2f} (need>{PIVOT_RATIO_MIN}); "
              f"CMP {cmp_v:.1f} vs PP {pp:.1f}")
    if hits == 2:
        return {"earned": W_PIVOT_ROOM, "status": "PASS", "detail": detail}
    if hits == 1:
        return {"earned": round(W_PIVOT_ROOM * 0.5, 2), "status": "MODERATE",
                "detail": detail}
    return {"earned": 0, "status": "FAIL", "detail": detail}


def _fib_bounce(cur, symbol: str, cmp_v: Optional[float]) -> Dict:
    """F3 — dynamic swing fibonacci from raw_prices ONLY (spec id=370 v3.5).
    Swing high = max(high) last 90d; swing low = min(low) in the 60d before it.
    PASS = pullback low (after swing high) within 2% of a 38.2/50/61.8 level;
    MODERATE = CMP within 2% of a level; FAIL = neither."""
    cur.execute(
        """SELECT price_date, high, low FROM raw_prices
           WHERE symbol = %s AND price_date >= CURRENT_DATE - INTERVAL '90 days'
           ORDER BY price_date""",
        (symbol.upper(),),
    )
    rows = cur.fetchall()
    if len(rows) < 10:
        return {"earned": 0, "status": "FAIL", "detail": "insufficient price history"}
    sh_i = max(range(len(rows)), key=lambda i: _f(rows[i][1]))
    swing_high, sh_date = _f(rows[sh_i][1]), rows[sh_i][0]
    lows_before = [_f(r[2]) for r in rows
                   if r[0] < sh_date and r[0] >= sh_date - timedelta(days=60)]
    if not lows_before:
        return {"earned": 0, "status": "FAIL", "detail": "no swing low before swing high"}
    swing_low = min(lows_before)
    rng = swing_high - swing_low
    if rng <= 0:
        return {"earned": 0, "status": "FAIL", "detail": "degenerate swing"}
    levels = {"38.2%": swing_high - rng * 0.382,
              "50%":   swing_high - rng * 0.5,
              "61.8%": swing_high - rng * 0.618}

    def nearest(px):
        return min(((abs(px - lv) / lv, name) for name, lv in levels.items()),
                   key=lambda t: t[0])

    lows_after = [_f(r[2]) for r in rows if r[0] > sh_date]
    if lows_after:
        pb = min(lows_after)
        diff, lname = nearest(pb)
        if diff <= FIB_TOL:
            return {"earned": W_FIB, "status": "PASS",
                    "detail": f"pullback {pb:.1f} bounced {lname} fib ({diff*100:.1f}% off)"}
    if cmp_v:
        diff, lname = nearest(cmp_v)
        if diff <= FIB_TOL:
            return {"earned": round(W_FIB * 0.5, 2), "status": "MODERATE",
                    "detail": f"CMP {cmp_v:.1f} at {lname} fib ({diff*100:.1f}% off)"}
    return {"earned": 0, "status": "FAIL", "detail": "no fib bounce (pullback/CMP not at level)"}


def _score(symbol: str, side: str,
           gate_5min: bool, gate_1day: bool) -> Dict[str, Any]:
    side = side.upper()
    assert side in ("LONG", "SHORT")

    with get_conn() as conn, conn.cursor() as cur:
        m = _fetch_metrics(cur, symbol)
        if not m:
            return {"error": f"No v8_metrics row for {symbol.upper()}"}
        segment = _fetch_segment(cur, symbol)
        peer_ok = _peer_confirm(cur, symbol, segment, side)
        basis = _fetch_basis(cur, symbol)     # F7 primary source
        has_oi = _has_oi(cur, symbol)         # F7 OI presence
        cmp_v = _fetch_cmp(cur, symbol)       # latest close (F2 + F3)
        piv = _fetch_pivots(cur, symbol)      # F2 pivot levels
        f3 = _fib_bounce(cur, symbol, cmp_v)  # F3 fibonacci (raw_prices only)

    rows: List[Dict] = []
    abstain_weight = 0

    # ── CORE GATES (R4 GVM + R9 direction; tri-state, 0.5 = MODERATE) ─────
    # R4 (spec id=370 v3.5): GVM_MIN_LONG lowered 7.0 -> 6.6.
    # R9 (spec id=370 v3.5): week+month is tri-state — both on side -> PASS(1.0),
    #   exactly one -> MODERATE(0.5), neither -> FAIL(0).
    core = []  # each: {"gate","value","score" in {0,0.5,1.0},"status"}
    if side == "LONG":
        g = _f(m["gvm_score"])
        ok = g >= GVM_MIN_LONG
        core.append({"gate": f"GVM >= {GVM_MIN_LONG} (R4)", "value": f"{g:.2f}",
                     "score": 1.0 if ok else 0.0, "status": "PASS" if ok else "FAIL"})
    wk, mo = _f(m["week_return"]), _f(m["month_return"])
    if side == "LONG":
        pos = int(wk > 0) + int(mo > 0)
        gate_name = "Week>0 AND Month>0 (R9)"
    else:
        pos = int(wk < 0) + int(mo < 0)
        gate_name = "Week<0 AND Month<0 (R9)"
    r9_status = "PASS" if pos == 2 else ("MODERATE" if pos == 1 else "FAIL")
    r9_score = 1.0 if pos == 2 else (0.5 if pos == 1 else 0.0)
    core.append({"gate": gate_name, "value": f"wk {wk:+.2f} / mo {mo:+.2f}",
                 "score": r9_score, "status": r9_status})

    # Multiplier from total deficit (preserves old "one short -> 0.5x";
    # a single MODERATE gate now yields 0.5 instead of a hard fail).
    deficit = len(core) - sum(c["score"] for c in core)
    if deficit == 0:
        core_pass, core_weight_multiplier = True, 1.0
    elif deficit <= 1.0:
        core_pass, core_weight_multiplier = True, 0.5
    else:
        core_pass, core_weight_multiplier = False, 0.0

    # ── WEIGHTED RULES ──────────────────────────────────────────────────
    def add(name, cond_txt, val_txt, passed, weight, abstain=False):
        earned = weight if passed else 0
        rows.append({"rule": name, "cond": cond_txt, "value": val_txt,
                     "weight": weight, "earned": earned,
                     "status": "ABSTAIN" if abstain else ("PASS" if passed else "FAIL")})

    # MA stack (2 of 3 on correct side)
    mas = [_f(m["dma_20"]), _f(m["dma_50"]), _f(m["dma_200"])]
    if side == "LONG":
        ma_ok = sum(1 for x in mas if x > 0) >= 2
    else:
        ma_ok = sum(1 for x in mas if x < 0) >= 2
    add("MA stack 2/3", "2 of 3 DMA on side",
        f"20:{mas[0]:+.1f} 50:{mas[1]:+.1f} 200:{mas[2]:+.1f}", ma_ok, W_MA_STACK)

    # RSI month & weekly
    rm, rwk = _f(m["rsi_month"]), _f(m["rsi_weekly"])
    rsi_ok = (rm >= 50 and rwk >= 50) if side == "LONG" else (rm <= 50 and rwk <= 50)
    add("RSI Mo & Wk", ">=50 both (LONG) / <=50 (SHORT)",
        f"Mo {rm:.1f} / Wk {rwk:.1f}", rsi_ok, W_RSI_MW)

    # 1-Day structure — chart gate 2 (caller passes)
    add("1D structure (gate)", "reversal/breakout (LONG) — your read",
        "YES" if gate_1day else "NO", gate_1day, W_STRUCT_1D)

    # F7 — OI + basis confirmation (spec id=370 v3.5)
    f7 = _eval_f7(side, basis, has_oi)
    rows.append({"rule": "OI+basis (F7)",
                 "cond": "basis on side (+OI = PASS, no OI = MODERATE)",
                 "value": f7["detail"], "weight": f7["weight"],
                 "earned": f7["earned"], "status": f7["status"]})

    # F2 — pivot-room ratio gate (spec id=370 v3.5; reads v8_paper_pivots levels)
    f2 = _eval_pivot_room(side, cmp_v, piv)
    rows.append({"rule": "Pivot room (F2)",
                 "cond": "(R2-CMP)/(CMP-S2) > 1.25 AND CMP > PP",
                 "value": f2["detail"], "weight": W_PIVOT_ROOM,
                 "earned": f2["earned"], "status": f2["status"]})

    # F3 — fibonacci bounce (spec id=370 v3.5; raw_prices only, pre-computed)
    rows.append({"rule": "Fibonacci bounce (F3)",
                 "cond": "pullback/CMP within 2% of 38.2/50/61.8 fib",
                 "value": f3["detail"], "weight": W_FIB,
                 "earned": f3["earned"], "status": f3["status"]})

    # Sector month aligned
    sm = _f(m["sector_month"])
    sec_ok = sm > 0 if side == "LONG" else sm < 0
    add("Sector month", ">0 (LONG) / <0 (SHORT)", f"{sm:+.2f}", sec_ok, W_SECTOR_MONTH)

    # 5-min strength — chart gate 1 (caller passes)
    add("5-min strength (gate)", "strength/close on side — your read",
        "YES" if gate_5min else "NO", gate_5min, W_5MIN)

    # Market gate — use sector_day proxy if no mood; conservative
    # (market mood read kept out to avoid V8 coupling; sector_day as tape proxy)
    sd = _f(m["sector_day"])
    mkt_ok = sd > -1.0 if side == "LONG" else sd < 1.0
    add("Market/tape gate", "not extremely against side (sector_day proxy)",
        f"{sd:+.2f}", mkt_ok, W_MARKET_GATE)

    # Peer confirm
    if peer_ok is None:
        rows.append({"rule": "Peer confirm", "cond": ">=2 peers same dir today",
                     "value": "insufficient peer data", "weight": W_PEER,
                     "earned": 0, "status": "ABSTAIN"})
        abstain_weight += W_PEER
    else:
        add("Peer confirm", ">=2 peers same dir today",
            "yes" if peer_ok else "no", peer_ok, W_PEER)

    # Volume conviction
    vr = _f(m["vol_ratio"])
    vol_ok = vr >= 1.0
    add("Volume conviction", "vol_ratio >= 1.0", f"{vr:.2f}", vol_ok, W_VOLUME)

    # RSI room
    dr = _f(m["daily_rsi"])
    room_ok = dr < 80 if side == "LONG" else dr > 20
    add("RSI room", "<80 (LONG) / >20 (SHORT)", f"{dr:.1f}", room_ok, W_RSI_ROOM)

    # ── TALLY ───────────────────────────────────────────────────────────
    earned = sum(r["earned"] for r in rows)
    # Effective max excludes abstained weights (fair denominator)
    eff_max = MAX_WEIGHTED - abstain_weight
    veto_hit = any(r["status"] == "VETO" for r in rows)

    if core_weight_multiplier == 0.0:
        verdict = "WATCH"
        reason = "core gate failed — " + ", ".join(
            f"{c['gate']}={c['value']}" for c in core if c["score"] < 1.0)
    elif veto_hit:
        verdict = "WATCH"
        reason = "OI veto — fresh opposing buildup"
    elif earned >= STRONG_MIN:
        verdict = "STRONG" if core_weight_multiplier == 1.0 else "VALID"
        reason = f"core {'pass' if core_weight_multiplier == 1.0 else 'partial (0.5x)'} + {earned}/{eff_max} weighted"
    elif earned >= VALID_MIN:
        verdict = "VALID" if core_weight_multiplier == 1.0 else "WATCH"
        reason = f"core {'pass' if core_weight_multiplier == 1.0 else 'partial (0.5x)'} + {earned}/{eff_max} weighted"
    else:
        verdict = "WATCH"
        reason = f"core {'pass but ' if core_pass else ''}only {earned}/{eff_max} weighted (<{VALID_MIN})"

    return {
        "version": VERSION,
        "symbol": symbol.upper(),
        "side": side,
        "as_of": datetime.now().strftime("%d-%b-%Y %H:%M IST"),
        "core_gates": [{"gate": c["gate"], "value": c["value"],
                        "pass": c["score"] == 1.0, "status": c["status"]} for c in core],
        "core_pass": core_pass,
        "core_weight_multiplier": core_weight_multiplier,
        "rules": rows,
        "earned": earned,
        "effective_max": eff_max,
        "abstained_weight": abstain_weight,
        "veto": veto_hit,
        "verdict": verdict,
        "reason": reason,
        "separation_note": "Independent of V8 paper engine — no v8_paper/v8_qualified read.",
    }


# ─── PUBLIC API ──────────────────────────────────────────────────────────

def trade_check(symbol: str, side: str = "LONG",
                gate_5min: bool = False, gate_1day: bool = False) -> Dict[str, Any]:
    """Score a symbol. Caller ('the gate') passes the 2 chart-gate booleans.

    Returns full weighted breakdown + verdict (STRONG / VALID / WATCH).
    """
    try:
        return _score(symbol, side, gate_5min, gate_1day)
    except AssertionError:
        return {"error": "side must be LONG or SHORT"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}


def render_table(result: Dict[str, Any]) -> str:
    """Plain-text score table for chat/MCP output (v3.3 output-format style)."""
    if "error" in result:
        return f"Trade Check v3.4.1 error: {result['error']}"
    L = [f"**Trade Check {result['version']} — {result['symbol']} {result['side']}**",
         f"_{result['as_of']}_", ""]
    L.append("**Core Gates:**")
    for c in result["core_gates"]:
        st = c.get("status", "PASS" if c["pass"] else "FAIL")
        L.append(f"  {st} {c['gate']} = {c['value']}")
    if result.get("core_weight_multiplier") is not None:
        mult = result["core_weight_multiplier"]
        mult_txt = "1.0 (both pass)" if mult == 1.0 else "0.5 (1 pass, 1 fail — neutral)" if mult == 0.5 else "0.0 (both fail)"
        L.append(f"  Core multiplier: {mult_txt}")
    L.append("")
    L.append("**Weighted Rules:**")
    L.append("| Rule | Value | Wt | Earned | Status |")
    L.append("|---|---|---|---|---|")
    for r in result["rules"]:
        L.append(f"| {r['rule']} | {r['value']} | {r['weight']} | {r['earned']} | {r['status']} |")
    L.append("")
    L.append(f"**Score: {result['earned']}/{result['effective_max']}** "
             f"(abstained wt {result['abstained_weight']}) -> "
             f"**{result['verdict']}** — {result['reason']}")
    L.append(f"_{result['separation_note']}_")
    return "\n".join(L)


# Optional manual promote to personal_journal — caller-triggered ONLY.
def promote_to_personal_journal(result: Dict[str, Any], qty: int,
                                entry_price: float, notes: str = "") -> Dict[str, Any]:
    """Manual-only. Never auto-fires. Writes to personal_journal, never v8_journal."""
    if "error" in result:
        return {"error": "cannot promote an errored check"}
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO personal_journal
                   (trade_date, symbol, direction, entry_price, qty,
                    setup_quality, notes)
                   VALUES (CURRENT_DATE, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (result["symbol"], result["side"], entry_price, qty,
                 f"TC{VERSION} {result['verdict']} {result['earned']}/{result['effective_max']}",
                 notes or result["reason"]),
            )
            jid = cur.fetchone()[0]
            conn.commit()
        return {"ok": True, "personal_journal_id": jid}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}
