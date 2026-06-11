"""
Trade Check v3.4.1 — Weighted Tier-1 + Core-Gate scoring engine.
UPDATE v3.4.1: 0.5 partial-pass rule for core gates.
- Both conditions pass (1.0x) → advance, no cap
- 1/2 pass (0.5x) → NEUTRAL, advance but weighted
- Both fail (0.0x) → WATCH (capped)

FIRST CODE implementation of Trade Check (v3.3 was conversational-only,
spec at session_log id=143 + output format id=209).

WHAT v3.4 CHANGES vs v3.3 (faithful re-weighting, NOT new rules):
  - Flat X/11 count  ->  core-gate + weighted score (fixes DIVISLAB 9/11
    outranking EICHERMOT 7/11 despite worse quality).
  - CORE GATES (pass/fail/partial): GVM>=7 (LONG) + week&month direction.
    Fail both => capped at WATCH. Fail one => 0.5x multiplier (neutral, advance).
  - OI/price quadrant (was Tier-2 F7 sub_A in v3.3) PROMOTED into Tier1
    as a weighted rule. basis-delta = strength grade. null-OI => abstain
    (neutral, never false-fail).
  - Side-aware LONG (11 data+chart rules) / SHORT (10, GVM N/A).

HUMAN-IN-AI-LOOP: the 2 chart gates (5-min strength, 1-Day structure) are
NOT machine-readable. Caller passes them as booleans ("you are the gate").
Module auto-scores the 8 data-derivable rules from v8_metrics + futures_basis.

HARD SEPARATION: This module NEVER reads v8_paper_*, v8_qualified, or any
V8 engine table. Trade Check and the V8 paper engine are independent systems
(session_log id=210). Reads ONLY: v8_metrics (metrics snapshot), gvm_scores
(segment/peers), futures_basis (OI/basis). v8_metrics is a shared read-only
metrics source, NOT a V8-engine coupling.

Runs SIDE-BY-SIDE with v3.3. Does not replace it. Replay-tunable weights.
Optional personal_journal promote is manual-only (caller-triggered).
"""

import os
from datetime import datetime
from typing import Optional, Dict, Any, List

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "")

VERSION = "v3.4.1"
SPEC_PARENT = "session_log id=143 (v3.3) — v3.4 weighted re-implementation + v3.4.1 0.5 partial rule"

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

MAX_WEIGHTED = (W_MA_STACK + W_RSI_MW + W_STRUCT_1D + W_OI_QUADRANT +
                W_SECTOR_MONTH + W_5MIN + W_MARKET_GATE + W_PEER +
                W_VOLUME + W_RSI_ROOM)  # = 21

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


def _fetch_basis_oi(cur, symbol: str) -> Optional[Dict[str, Any]]:
    """Latest + prior few futures_basis bars for OI-quadrant + basis-delta.
    Returns None if no OI data (rule abstains — never false-fail)."""
    cur.execute(
        """SELECT ts, basis, basis_pct, oi, oi_chg, futures_close, spot_close
           FROM futures_basis
           WHERE symbol = %s AND oi IS NOT NULL
           ORDER BY ts DESC LIMIT 5""",
        (symbol.upper(),),
    )
    rows = cur.fetchall()
    if not rows:
        return None
    latest = rows[0]
    basis_series = [_f(r[1]) for r in rows][::-1]  # oldest->newest
    return {
        "ts": latest[0],
        "basis": _f(latest[1]),
        "basis_pct": _f(latest[2]),
        "oi": latest[3],
        "oi_chg": latest[4],
        "basis_series": basis_series,
        "fut": _f(latest[5]),
        "spot": _f(latest[6]),
    }


# ─── RULE EVALUATORS ─────────────────────────────────────────────────────

def _eval_oi_quadrant(side: str, m: Dict, basis: Optional[Dict]) -> Dict:
    """Promoted from v3.3 Tier2 F7. price+OI buildup = full weight.
    basis widening = strength grade. null OI = abstain (neutral)."""
    if basis is None:
        return {"weight": 0, "earned": 0, "status": "ABSTAIN",
                "detail": "no OI data (futures_basis null) — neutral, not penalised",
                "abstain": True}

    price_up = _f(m.get("eod_chg")) > 0
    oi_rising = (basis["oi_chg"] or 0) > 0

    # Quadrant
    if side == "LONG":
        buildup = price_up and oi_rising          # fresh long buildup (strong)
        covering = price_up and not oi_rising      # short covering (weak-ok)
        veto = (not price_up) and oi_rising        # fresh SHORT buildup vs a long
    else:  # SHORT
        buildup = (not price_up) and oi_rising     # fresh short buildup (strong)
        covering = (not price_up) and not oi_rising # long unwinding (weak-ok)
        veto = price_up and oi_rising              # fresh LONG buildup vs a short

    # basis-delta strength grade
    bs = basis["basis_series"]
    basis_widening = len(bs) >= 2 and bs[-1] > bs[0]

    if veto:
        return {"weight": W_OI_QUADRANT, "earned": 0, "status": "VETO",
                "detail": f"opposing buildup (price {'up' if price_up else 'dn'} + OI rising) — positioning AGAINST {side}",
                "abstain": False}
    if buildup:
        earned = W_OI_QUADRANT
        grade = "strong (buildup + basis widening)" if basis_widening else "buildup confirmed"
        return {"weight": W_OI_QUADRANT, "earned": earned, "status": "PASS",
                "detail": f"{side} {grade}; OI_chg={basis['oi_chg']:,} basis={basis['basis']:.2f}",
                "abstain": False}
    if covering:
        earned = W_OI_QUADRANT - 1  # weak-acceptable, partial credit
        return {"weight": W_OI_QUADRANT, "earned": earned, "status": "WEAK",
                "detail": f"{'short covering' if side=='LONG' else 'long unwinding'} — move on exits not fresh positioning",
                "abstain": False}
    return {"weight": W_OI_QUADRANT, "earned": 0, "status": "FAIL",
            "detail": "no clear OI buildup", "abstain": False}


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
        basis = _fetch_basis_oi(cur, symbol)

    rows: List[Dict] = []
    abstain_weight = 0

    # ── CORE GATES (pass/fail/partial; 0.5 for one fail) ──────────────────
    core = []
    if side == "LONG":
        gvm_ok = _f(m["gvm_score"]) >= 7.0
        core.append(("GVM >= 7.0", f"{_f(m['gvm_score']):.2f}", gvm_ok))
    # week & month direction
    wk, mo = _f(m["week_return"]), _f(m["month_return"])
    if side == "LONG":
        dir_ok = wk > 0 and mo > 0
        core.append(("Week>0 AND Month>0", f"wk {wk:+.2f} / mo {mo:+.2f}", dir_ok))
    else:
        dir_ok = wk < 0 and mo < 0
        core.append(("Week<0 AND Month<0", f"wk {wk:+.2f} / mo {mo:+.2f}", dir_ok))
    
    # Core gate: 0.5 for partial pass (1 of 2 conditions)
    core_pass_count = sum(1 for c in core if c[2])
    if core_pass_count == len(core):
        core_pass = True
        core_weight_multiplier = 1.0
    elif core_pass_count == len(core) - 1:
        core_pass = True  # advance, but weighted
        core_weight_multiplier = 0.5  # partial pass = neutral
    else:
        core_pass = False
        core_weight_multiplier = 0.0

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

    # OI/price quadrant (promoted)
    oi = _eval_oi_quadrant(side, m, basis)
    rows.append({"rule": "OI/price quadrant", "cond": "price+OI buildup (basis grade)",
                 "value": oi["detail"], "weight": oi["weight"],
                 "earned": oi["earned"], "status": oi["status"]})
    if oi.get("abstain"):
        abstain_weight += oi["weight"]

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
            f"{c[0]}={c[1]}" for c in core if not c[2])
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
        "core_gates": [{"gate": c[0], "value": c[1], "pass": c[2]} for c in core],
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
        L.append(f"  {'PASS' if c['pass'] else 'FAIL'} {c['gate']} = {c['value']}")
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
