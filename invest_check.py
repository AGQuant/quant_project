"""
invest_check.py — INVEST mode for Trade Check page (3rd toggle alongside LONG/SHORT).

A buy-and-hold FUNDAMENTAL lens, fully distinct from the v3.3.2 price-action
trade check (LONG/SHORT). Context isolation (id=244): this NEVER touches V8
rules or Tier1/Tier2 price-action rules. It is a GVM / Quant-Basket style
quality+valuation+momentum scorecard sourced entirely from build_company_report.

Returns the SAME response shape as compute_trade_check (tier1/tier2 arrays,
verdict, scores) so the existing frontend renders it with minimal changes.

Group A — Quality & Growth   (5 checks, min 3)
Group B — Valuation & Momentum (4 checks, min 2)
"""

import os
from datetime import datetime, timedelta
import psycopg

from gvm_company_report import build_company_report

DATABASE_URL = os.getenv("DATABASE_URL", "")

_BUY_VERDICTS = {"STRONG BUY", "BUY"}


def _ist_now():
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%d-%b %H:%M IST")


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _beats(params, key):
    """Return (beats_peer_bool_or_None, raw, peer_avg) for a parameter key."""
    for p in params or []:
        if p.get("key") == key:
            return p.get("beats_peer"), p.get("raw"), p.get("peer_avg")
    return None, None, None


def compute_invest_check(symbol_text):
    sym = (symbol_text or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "Specify a symbol, e.g. RELIANCE."}

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            base = build_company_report(conn, sym)
    except Exception as e:
        return {"ok": False, "error": f"DB error: {str(e)[:160]}"}

    if "error" in base:
        return {"ok": False, "error": base["error"]}

    gvm   = _f(base.get("gvm_score"))
    g     = _f(base.get("g_score"))
    v     = _f(base.get("v_score"))
    m     = _f(base.get("m_score"))
    verdict_db = (base.get("verdict") or "").strip()
    seg_rank   = base.get("segment_rank")
    seg_size   = base.get("segment_size") or 0
    params     = base.get("parameters") or []
    n_pos      = len(base.get("positives") or [])
    n_neg      = len(base.get("negatives") or [])

    roce_beat, roce_raw, _ = _beats(params, "roce")
    pe_beat,   pe_raw,   _ = _beats(params, "pe")
    ret1y_beat, ret1y_raw, _ = _beats(params, "ret_1y")

    top_third = (seg_rank is not None and seg_size > 0
                 and seg_rank <= max(1, -(-seg_size // 3)))  # ceil(size/3)

    def row(rule, cond, val, ok):
        return {"rule": rule, "cond": cond, "val": val,
                "state": "pass" if ok else ("fail" if ok is False else "na"),
                "method": "auto"}

    # ── Group A — Quality & Growth (maps to tier1) ──
    a = [
        row("Q1 GVM",     "GVM >= 7.0 (quality gate)",
            f"{gvm:.2f}" if gvm is not None else "—",
            gvm is not None and gvm >= 7.0),
        row("Q2 Growth",  "G pillar >= 7.0",
            f"{g:.2f}" if g is not None else "—",
            g is not None and g >= 7.0),
        row("Q3 Verdict", "Buy / Strong Buy",
            verdict_db or "—",
            verdict_db.upper() in _BUY_VERDICTS),
        row("Q4 Peer Rank", "top third of segment",
            f"#{seg_rank}/{seg_size}" if seg_rank else "—",
            top_third),
        row("Q5 ROCE",    "beats peer average",
            f"{roce_raw:.1f}%" if roce_raw is not None else "—",
            roce_beat),
    ]

    # ── Group B — Valuation & Momentum (maps to tier2) ──
    b = [
        row("V1 Value",    "V pillar >= 5.0 (not stretched)",
            f"{v:.2f}" if v is not None else "—",
            v is not None and v >= 5.0),
        row("V2 PE",       "PE beats peer (cheaper)",
            f"{pe_raw:.1f}x" if pe_raw is not None else "—",
            pe_beat),
        row("M1 Momentum", "M pillar >= 5.0",
            f"{m:.2f}" if m is not None else "—",
            m is not None and m >= 5.0),
        row("M2 1Y Return","beats peer average",
            f"{ret1y_raw:.1f}%" if ret1y_raw is not None else "—",
            ret1y_beat),
    ]

    a_pass = sum(1 for r in a if r["state"] == "pass")
    b_pass = sum(1 for r in b if r["state"] == "pass")
    a_total, b_total = len(a), len(b)
    a_min, b_min = 3, 2

    # ── Verdict ──
    is_buy = verdict_db.upper() in _BUY_VERDICTS
    is_exit = verdict_db.upper() in ("EXIT", "AVOID", "SELL")
    if a_pass >= 4 and b_pass >= 3 and gvm is not None and gvm >= 8.0 and is_buy:
        v_label, vclass = "✦ STRONG INVEST", "strong"
        verdict = f"STRONG INVEST — Quality {a_pass}/{a_total}, Value+Mom {b_pass}/{b_total}, GVM {gvm:.1f}."
    elif a_pass >= a_min and b_pass >= b_min and gvm is not None and gvm >= 7.0 and not is_exit:
        v_label, vclass = "✓ INVEST", "valid"
        verdict = f"INVEST — Quality {a_pass}/{a_total}, Value+Mom {b_pass}/{b_total}, GVM {gvm:.1f}. Buy-and-hold candidate."
    elif a_pass >= 2 and not is_exit:
        v_label, vclass = "⚠ WATCH", "weak"
        verdict = f"WATCH — Quality {a_pass}/{a_total}, Value+Mom {b_pass}/{b_total}. Partial merit; wait for confirmation."
    else:
        v_label, vclass = "✗ AVOID", "reject"
        verdict = f"AVOID — Quality {a_pass}/{a_total}, Value+Mom {b_pass}/{b_total}. Fails the buy-and-hold bar."

    return {
        "ok": True, "symbol": base.get("symbol", sym),
        "company": base.get("company_name"), "segment": base.get("segment"),
        "side": "INVEST", "gvm": gvm, "ts": _ist_now(),
        "tier1": a, "tier2": b,
        "t1_pass": a_pass, "t1_auto_n": a_total, "t1_human_n": 0, "t1_total": a_total,
        "t2_pass": b_pass, "t2_auto_n": b_total, "t2_human_n": 0,
        "t2_total": b_total, "t2_min": b_min,
        "tier1_label": "Group A — Quality & Growth",
        "tier2_label": "Group B — Valuation & Momentum",
        "need1": f"need {a_min}", "need2": f"need {b_min}",
        "verdict": verdict, "verdict_class": vclass, "v_label": v_label,
        "foot": "Fundamental / buy-and-hold lens · GVM peer-benchmarked · not a V8 signal.",
        "version": "invest-v1",
        "scoring": "fundamental — GVM quality + valuation + momentum vs segment peers",
    }
