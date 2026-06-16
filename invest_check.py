"""
invest_check.py v2.0 — Investment Check. INVEST toggle on Trade Check page.

v2.0 (16-Jun-2026): Full redesign.
  - ALL fundamentals peer-relative (sector avg as benchmark, not absolute)
  - Archetype classification FIRST (COMPOUNDER / VALUE-QUALITY / BLEND)
    → archetype changes pass thresholds, not just labels
  - Result check: QoQ sales+profit BOTH beat sector avg AND OPM expanding
    (trending up = opm_exp beats peer, above sector = qoq beats peer)
  - Net FII+DII flow check (net > 0 = institutions net buying, absolute signal)
  - Interpretation layer: rule-based default, use_api opt-in (claude-sonnet-4-6)
  - 10 rules: Group A Quality+Growth (6) + Group B Valuation+Momentum (4)

Context isolation (id=244): NEVER mix with trade_check v3.3 or V8 rules.
Separate context. BFSI rule: Q5 ROCE exempt for bank/nbfc/insurance/amc/finance.

Archetype classification (from GVM sub-scores):
  COMPOUNDER   : G >= 7.0 AND G > V  → growth-driven, 3-5yr hold
  VALUE-QUALITY: V >= 6.5 AND V >= G → cheap vs peers, quality anchor
  BLEND        : neither dominant     → balanced, moderate conviction

Archetype-gated thresholds:
  COMPOUNDER    → Group A min 4/6, Group B min 3/4, STRONG = 6+4
  VALUE-QUALITY → Group A min 3/6, Group B min 3/4, STRONG = 5+4
  BLEND         → Group A min 3/6, Group B min 2/4, STRONG = 4+3
"""

import os
import re
from datetime import datetime, timedelta
import psycopg

from gvm_company_report import build_company_report

DATABASE_URL = os.getenv("DATABASE_URL", "")

_BUY_VERDICTS = {"STRONG BUY", "BUY"}
_BFSI_KEYWORDS = ("bank", "nbfc", "finance", "insurance", "amc", "exchange",
                  "capital market", "broking", "wealth", "microfinance",
                  "housing finance", "msme finance", "fintech")


def _ist_now():
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%d-%b %H:%M IST")


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _beats(params, key):
    """Return (beats_peer, raw, peer_avg) for a parameter key."""
    for p in params or []:
        if p.get("key") == key:
            return p.get("beats_peer"), p.get("raw"), p.get("peer_avg")
    return None, None, None


def _is_bfsi(segment):
    s = (segment or "").lower()
    return any(k in s for k in _BFSI_KEYWORDS)


# ─────────────────────────────── archetype ───────────────────────────────────

def _classify_archetype(g, v):
    """Classify from GVM sub-scores. Returns (archetype, note)."""
    g, v = _f(g), _f(v)
    if g is not None and v is not None:
        if g >= 7.0 and g > v:
            return "COMPOUNDER", "Growth-driven · hold 3–5yr · valuation secondary"
        if v >= 6.5 and v >= g:
            return "VALUE-QUALITY", "Cheap vs peers · quality anchor · momentum emerging"
    return "BLEND", "Balanced · moderate conviction · neither pillar dominant"


# ─────────────────────────────── interpretation ──────────────────────────────

def _istate_inv(rows, prefix):
    for r in rows:
        if r["rule"].upper().startswith(prefix.upper()):
            return r["state"], r["val"]
    return None, None


def _interpret_invest_rulebased(d):
    """Full narrative paragraph for INVEST mode."""
    archetype = d.get("archetype", "BLEND")
    vclass = d["verdict_class"]
    t1, t2 = d["tier1"], d["tier2"]

    q1_s, q1_v = _istate_inv(t1, "Q1")
    q2_s, q2_v = _istate_inv(t1, "Q2")
    q3_s, _ = _istate_inv(t1, "Q3")
    q4_s, _ = _istate_inv(t1, "Q4")
    q5_s, q5_v = _istate_inv(t1, "Q5")
    q6_s, q6_v = _istate_inv(t1, "Q6")
    v1_s, v1_v = _istate_inv(t2, "V1")
    v2_s, _ = _istate_inv(t2, "V2")
    m1_s, m1_v = _istate_inv(t2, "M1")
    m2_s, _ = _istate_inv(t2, "M2")

    arch_map = {
        "COMPOUNDER": "high-conviction compounder",
        "VALUE-QUALITY": "value-quality hold",
        "BLEND": "blend setup",
    }
    arch_note_map = {
        "COMPOUNDER": "growth pillar leads segment, justified for a 3–5yr hold where valuation is secondary",
        "VALUE-QUALITY": "valuation cheaper than sector peers with quality anchor — a patience play waiting for price discovery",
        "BLEND": "neither growth nor value is dominant; moderate conviction either way",
    }
    archetype_label = arch_map.get(archetype, "setup")
    arch_note = arch_note_map.get(archetype, "")

    drivers = []
    if q1_s == "pass":
        drivers.append(f"GVM above sector ({q1_v})")
    if q2_s == "pass":
        drivers.append(f"growth pillar leading segment ({q2_v})")
    if q3_s == "pass" and q4_s == "pass":
        drivers.append("latest results beating sector on both sales and profit")
    elif q3_s == "pass":
        drivers.append("sales QoQ beating sector")
    elif q4_s == "pass":
        drivers.append("profit QoQ beating sector")
    if q5_s == "pass":
        drivers.append(f"ROCE above peers ({q5_v})")
    if q6_s == "pass":
        drivers.append(f"net FII+DII positive ({q6_v}) — institutions net buying")
    if m1_s == "pass":
        drivers.append(f"momentum building vs segment ({m1_v})")
    if m2_s == "pass":
        drivers.append("1Y return beats segment peers")

    # binding risk
    risk = None
    if archetype == "COMPOUNDER":
        if v1_s == "fail":
            risk = f"valuation stretched well below sector avg ({v1_v}) — growth must sustain to justify; watch for deceleration"
        elif q2_s == "fail":
            risk = f"growth pillar not leading segment ({q2_v}) — core compounder thesis at risk"
        elif m1_s == "fail":
            risk = "momentum lagging segment — market hasn't confirmed the growth story yet"
    elif archetype == "VALUE-QUALITY":
        if v1_s == "fail":
            risk = f"value pillar below sector avg ({v1_v}) — stock is not cheap enough for a value-quality thesis"
        elif m1_s == "fail":
            risk = f"momentum lagging ({m1_v}) — value trap risk; no price catalyst visible yet, patience required"
        elif q5_s == "fail":
            risk = f"ROCE not beating peers ({q5_v}) — quality anchor is weak; review capital efficiency trend"
    else:
        if v2_s == "fail":
            risk = "OPM below sector peers — margin quality is the weak link"
        elif q5_s == "fail":
            risk = f"ROCE not beating peers ({q5_v}) — no clear capital efficiency edge"
        elif q3_s == "fail" or q4_s == "fail":
            risk = "recent results not beating sector — execution needs to improve before conviction increases"

    if risk is None:
        gaps = sum(1 for r in t1 + t2 if r["state"] == "na")
        risk = (f"{gaps} rules have no fundamental data — read is partial, check screener_raw coverage"
                if gaps >= 2 else "no single dominant risk — clean on the axes measured")

    conv = {
        "strong": "High conviction",
        "valid": "Tradeable conviction",
        "weak": "Marginal — some merit but below buy-and-hold bar",
        "reject": "Not actionable — fails the investment quality threshold",
    }.get(vclass, "Mixed")

    drv = ", ".join(drivers) if drivers else "few rules confirmed"
    return (f"{conv}. This reads as a {archetype_label} — {arch_note}. "
            f"Supporting it: {drv}. Watch: {risk}.")


def _interpret_invest_api(d, model="claude-sonnet-4-6"):
    """Richer interpretation via Anthropic API. Falls back to rule-based on failure."""
    try:
        import anthropic
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            return _interpret_invest_rulebased(d)
        client = anthropic.Anthropic(api_key=key)
        rows = [f"{r['rule']}: {r['state']} ({r['val']})" for r in d["tier1"] + d["tier2"]]
        prompt = (
            f"You are a disciplined equity analyst. An INVESTMENT check on "
            f"{d['company']} ({d['symbol']}, {d['segment']}) returned archetype "
            f"'{d.get('archetype','BLEND')}' with these peer-relative rule states:\n"
            + "\n".join(rows)
            + f"\n\nVerdict: {d['verdict']}\n\n"
            "Write ONE tight paragraph (max 80 words): name the archetype and what it means "
            "for holding horizon, the top conviction drivers, and the single binding risk. "
            "Fundamentals and peer-relative only. No price action. Be decisive.")
        msg = client.messages.create(model=model, max_tokens=220,
                                     messages=[{"role": "user", "content": prompt}])
        txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        return txt or _interpret_invest_rulebased(d)
    except Exception:
        return _interpret_invest_rulebased(d)


def interpret_invest(d, use_api=False):
    """Public entry. Rule-based default; API when use_api=True."""
    if not d.get("ok"):
        return None
    return _interpret_invest_api(d) if use_api else _interpret_invest_rulebased(d)


# ─────────────────────────────── main compute ────────────────────────────────

def compute_invest_check(symbol_text, use_api=False):
    sym = re.sub(
        r"\b(investment|invest|check|review|analyse|analyze|stock|on|for|a|the)\b",
        " ", (symbol_text or ""), flags=re.I).strip().upper()
    if not sym:
        return {"ok": False, "error": "Specify a symbol, e.g. RELIANCE."}

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            base = build_company_report(conn, sym)
    except Exception as e:
        return {"ok": False, "error": f"DB error: {str(e)[:160]}"}

    if "error" in base:
        return {"ok": False, "error": base["error"]}

    # ── raw inputs ──
    gvm        = _f(base.get("gvm_score"))
    g          = _f(base.get("g_score"))
    v          = _f(base.get("v_score"))
    m          = _f(base.get("m_score"))
    verdict_db = (base.get("verdict") or "").strip().upper()
    seg_rank   = base.get("segment_rank")
    seg_size   = base.get("segment_size") or 0
    params     = base.get("parameters") or []
    bfsi       = base.get("is_bfsi", False)

    # peer-relative values via _beats()
    qoq_s_ok,  qoq_s_raw,  qoq_s_peer  = _beats(params, "qoq_sales")
    qoq_p_ok,  qoq_p_raw,  qoq_p_peer  = _beats(params, "qoq_profit")
    opm_exp_ok, opm_exp_raw, _          = _beats(params, "opm_exp")
    roce_ok,   roce_raw,   roce_peer    = _beats(params, "roce")
    opm_ok,    opm_raw,    opm_peer     = _beats(params, "opm")
    pe_ok,     pe_raw,     pe_peer      = _beats(params, "pe")
    ret1y_ok,  ret1y_raw,  ret1y_peer   = _beats(params, "ret_1y")
    fii_ok,    fii_raw,    fii_peer     = _beats(params, "inst_change")  # kept for legacy; Q6 uses net below

    # Q6: net institutional (FII+DII) — computed inline from screener_raw
    # Net > 0 = institutions net buying (absolute, not peer-relative)
    _fii_raw = _dii_raw = _net_inst = None
    try:
        with psycopg.connect(DATABASE_URL) as _conn, _conn.cursor() as _cur:
            _cur.execute(
                'SELECT fii_change, dii_change FROM screener_raw WHERE nse_code=%s LIMIT 1',
                (base.get("symbol", sym),))
            _r = _cur.fetchone()
            if _r:
                _fii_raw = _f(_r[0])
                _dii_raw = _f(_r[1])
                if _fii_raw is not None and _dii_raw is not None:
                    _net_inst = round(_fii_raw + _dii_raw, 2)
    except Exception:
        pass

    # segment rank: top third
    top_third = (seg_rank is not None and seg_size > 0
                 and seg_rank <= max(1, -(-seg_size // 3)))

    # ── archetype ──
    archetype, arch_note = _classify_archetype(g, v)

    # ── archetype-gated thresholds ──
    if archetype == "COMPOUNDER":
        q1_pass = gvm is not None and g is not None and g >= 7.0 and g > (v or 0)
        q2_pass = g is not None and top_third and g >= 7.0
        q3_pass = bool(qoq_s_ok) and bool(opm_exp_ok)
        q4_pass = bool(qoq_p_ok) and bool(opm_exp_ok)
        q5_pass = bool(roce_ok) if not bfsi else None
        v1_pass = v is not None and v >= 4.5
        v2_pass = bool(opm_ok)
        m1_pass = m is not None and m >= 5.5
        a_min, b_min = 4, 3
        strong_a, strong_b = 6, 4
    elif archetype == "VALUE-QUALITY":
        q1_pass = gvm is not None and gvm >= 7.0
        q2_pass = g is not None and g >= 6.0
        q3_pass = bool(qoq_s_ok)
        q4_pass = bool(qoq_p_ok)
        q5_pass = (roce_raw is not None and roce_peer is not None
                   and roce_raw >= roce_peer + 5.0) if not bfsi else None
        v1_pass = v is not None and v >= 6.5
        v2_pass = (opm_raw is not None and opm_peer is not None
                   and opm_raw >= opm_peer + 2.0)
        m1_pass = m is not None and m >= 4.5
        a_min, b_min = 3, 3
        strong_a, strong_b = 5, 4
    else:  # BLEND
        q1_pass = gvm is not None and gvm >= 7.0
        q2_pass = g is not None and g >= 6.0
        q3_pass = bool(qoq_s_ok)
        q4_pass = bool(qoq_p_ok)
        q5_pass = bool(roce_ok) if not bfsi else None
        v1_pass = v is not None and v >= 5.0
        v2_pass = bool(opm_ok)
        m1_pass = m is not None and m >= 5.0
        a_min, b_min = 3, 2
        strong_a, strong_b = 4, 3

    # Q6 net FII+DII: same across all archetypes — net > 0
    q6_pass = (_net_inst is not None and _net_inst > 0)

    # M2 1Y return beats peer — same across all archetypes
    m2_pass = bool(ret1y_ok)

    def row(rule, cond, val, ok):
        state = "na" if ok is None else ("pass" if ok else "fail")
        return {"rule": rule, "cond": cond, "val": val, "state": state, "method": "auto"}

    # ── Group A — Quality & Growth (6 rules) ──
    a = [
        row("Q1 GVM",
            "GVM ≥7.0 + G leads V (COMPOUNDER) / GVM ≥7.0 (others)",
            f"{gvm:.2f}" if gvm is not None else "—", q1_pass),
        row("Q2 Growth",
            "G ≥7.0 + top-third (COMP) / G ≥6.0 (others)",
            f"G {g:.2f}" if g is not None else "—", q2_pass),
        row("Q3 Sales QoQ",
            "beats sector avg + OPM expanding (COMP) / beats sector (others)",
            (f"{qoq_s_raw:.1f}% vs peer {qoq_s_peer:.1f}%"
             if qoq_s_raw is not None and qoq_s_peer is not None else "—"),
            q3_pass),
        row("Q4 Profit QoQ",
            "beats sector avg + OPM expanding (COMP) / beats sector (others)",
            (f"{qoq_p_raw:.1f}% vs peer {qoq_p_peer:.1f}%"
             if qoq_p_raw is not None and qoq_p_peer is not None else "—"),
            q4_pass),
        row("Q5 ROCE",
            "beats peer (BLEND/COMP) / peer+5% (V-Q) [BFSI exempt]",
            (f"{roce_raw:.1f}% vs peer {roce_peer:.1f}%"
             if roce_raw is not None and roce_peer is not None
             else "BFSI exempt" if bfsi else "—"),
            q5_pass),
        row("Q6 Net Inst",
            "FII+DII net flow > 0 (institutions net buying)",
            (f"FII {_fii_raw:+.2f} / DII {_dii_raw:+.2f} = net {_net_inst:+.2f}"
             if _net_inst is not None else "—"),
            q6_pass),
    ]

    # ── Group B — Valuation & Momentum (4 rules) ──
    b = [
        row("V1 Value",
            "V ≥4.5 (COMP) / V ≥6.5 (V-Q) / V ≥5.0 (BLEND)",
            f"V {v:.2f}" if v is not None else "—", v1_pass),
        row("V2 OPM",
            "beats peer (COMP/BLEND) / peer+2% (V-Q)",
            (f"{opm_raw:.1f}% vs peer {opm_peer:.1f}%"
             if opm_raw is not None and opm_peer is not None else "—"),
            v2_pass),
        row("M1 Momentum",
            "M ≥5.5 (COMP) / M ≥4.5 (V-Q) / M ≥5.0 (BLEND)",
            f"M {m:.2f}" if m is not None else "—", m1_pass),
        row("M2 1Y Return",
            "beats segment peers (all archetypes)",
            (f"{ret1y_raw:.1f}% vs peer {ret1y_peer:.1f}%"
             if ret1y_raw is not None and ret1y_peer is not None else "—"),
            m2_pass),
    ]

    a_pass = sum(1 for r in a if r["state"] == "pass")
    b_pass = sum(1 for r in b if r["state"] == "pass")
    a_total, b_total = len(a), len(b)

    # ── verdict ──
    is_exit = verdict_db in ("EXIT", "AVOID", "SELL", "WATCH")
    if a_pass >= strong_a and b_pass >= strong_b and not is_exit:
        vclass = "strong"
        verdict = (f"★ STRONG INVEST [{archetype}] — Quality {a_pass}/{a_total}, "
                   f"Value+Mom {b_pass}/{b_total}, GVM {gvm:.1f}.")
    elif a_pass >= a_min and b_pass >= b_min and gvm is not None and gvm >= 7.0 and not is_exit:
        vclass = "valid"
        verdict = (f"✓ INVEST [{archetype}] — Quality {a_pass}/{a_total}, "
                   f"Value+Mom {b_pass}/{b_total}. Buy-and-hold candidate.")
    elif a_pass >= 2 and not is_exit:
        vclass = "weak"
        verdict = (f"⚠ WATCH [{archetype}] — Quality {a_pass}/{a_total}, "
                   f"Value+Mom {b_pass}/{b_total}. Partial merit; wait for confirmation.")
    else:
        vclass = "reject"
        verdict = (f"✗ AVOID [{archetype}] — Quality {a_pass}/{a_total}, "
                   f"Value+Mom {b_pass}/{b_total}. Fails buy-and-hold bar.")

    result = {
        "ok": True,
        "symbol": base.get("symbol", sym),
        "company": base.get("company_name"),
        "segment": base.get("segment"),
        "side": "INVEST",
        "gvm": gvm, "g": g, "v": v, "m": m,
        "archetype": archetype,
        "archetype_note": arch_note,
        "ts": _ist_now(),
        "tier1": a, "tier2": b,
        "t1_pass": a_pass, "t1_auto_n": a_total, "t1_human_n": 0, "t1_total": a_total,
        "t2_pass": b_pass, "t2_auto_n": b_total, "t2_human_n": 0,
        "t2_total": b_total, "t2_min": b_min,
        "tier1_label": "Group A — Quality & Growth",
        "tier2_label": "Group B — Valuation & Momentum",
        "need1": f"need {a_min}", "need2": f"need {b_min}",
        "verdict": verdict, "verdict_class": vclass,
        "foot": "Fundamental / buy-and-hold lens · GVM peer-benchmarked · not a V8 signal.",
        "version": "invest-v2.0",
        "scoring": "peer-relative — all fundamentals vs segment avg · archetype-gated thresholds",
    }
    result["interpretation"] = interpret_invest(result, use_api=use_api)
    result["interpretation_mode"] = "api" if use_api else "rule-based"
    return result


def native_invest_check(query, use_api=False):
    """Markdown wrapper for /ask + native_router."""
    d = compute_invest_check(query, use_api=use_api)
    if not d.get("ok"):
        return f"**Investment Check — v2**\n{d.get('error', 'error')}"

    def mark(r):
        return {"pass": "PASS", "fail": "FAIL", "na": "🟡 no data"}.get(r["state"], r["state"])

    arch = d.get("archetype", "—")
    arch_note = d.get("archetype_note", "")
    out = [f"**Investment Check v2.0 — {d['company']} ({d['symbol']})**"]
    gline = (f"GVM {d['gvm']:.2f} · G {d['g']:.2f} / V {d['v']:.2f} / M {d['m']:.2f}"
             if all(d.get(k) is not None for k in ["gvm", "g", "v", "m"]) else "")
    out.append(f"_{d['segment']} · {gline} · {d['ts']}_")
    out.append(f"\n**Archetype: {arch}** — _{arch_note}_")
    out.append(f"\n**{d.get('tier1_label','Group A')}** · {d.get('need1','need 3')}")
    out.append("| Rule | Condition | Value | State |")
    out.append("| --- | --- | --- | --- |")
    for r in d["tier1"]:
        out.append(f"| {r['rule']} | {r['cond']} | {r['val']} | {mark(r)} |")
    out.append(f"\n**Group A: {d['t1_pass']}/{d['t1_total']} confirmed**")
    out.append(f"\n**{d.get('tier2_label','Group B')}** · {d.get('need2','need 2')}")
    out.append("| Rule | Condition | Value | State |")
    out.append("| --- | --- | --- | --- |")
    for r in d["tier2"]:
        out.append(f"| {r['rule']} | {r['cond']} | {r['val']} | {mark(r)} |")
    out.append(f"\n**Group B: {d['t2_pass']}/{d['t2_total']} confirmed**")
    out.append(f"\n---\n**Verdict: {d['verdict']}**")
    if d.get("interpretation"):
        mode = " (AI)" if d.get("interpretation_mode") == "api" else ""
        out.append(f"\n**Interpretation{mode}:** {d['interpretation']}")
    out.append(f"_{d.get('foot','')}_")
    return "\n".join(out)
