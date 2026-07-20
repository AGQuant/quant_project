"""
Investment Check v3.0 — Gate + Conviction model.
Canonical spec: session_log id=6632 (INVEST_CHECK_V3_0_GATE_CONVICTION_FINAL, locked 2026-07-20).

REPLACES both prior engines:
  - investment_check.py v1.0 (flat 12-rule 0/12 screener)   -> archived_superseded
  - invest_check.py       v2.0 (COMPOUNDER/VALUE-QUALITY/BLEND archetype count-model) -> archived_superseded

ONE unified weighted scorer powers all three consumers (single / screener / summary) plus the
POST /api/check?side=INVEST + /ask native wiring (via compute_invest_check() below).

MODEL — rule out disqualifiers FIRST (3 hard gates), then rank conviction among survivors.
Layer 1 — 3 hard GATES (must pass ALL to be eligible for INVEST / STRONG_INVEST):
  G1 Valuation      : v_score        >= 6
  G2 Quality        : gvm_score      >= 7.0
  G3 Growth intact  : profit_5Y_CAGR >  10%
  If any gate metric is NULL -> cannot rate (WATCH / insufficient-data).
Layer 2 — 12 CONVICTION filters, max 15 (scored for gate-passers; missing filter data -> that
  filter scores 0, it never fails a gate). Peer comparisons (F3,F4,F5,F6,F10) are vs the TOP-3
  highest-gvm peers in the same gvm_scores.segment (SELF-EXCLUDED, non-null metric required;
  <3 qualifying -> full-segment avg fallback) — beat the leaders, not the laggard-dragged average.

VERDICT:
  AVOID          : fails 2+ gates
  WATCH          : fails exactly 1 gate, OR gates pass but conviction < 9, OR a gate metric is NULL
  INVEST         : all 3 gates pass AND conviction >= 9/15
  STRONG_INVEST  : all 3 gates pass AND conviction >= 12/15

Context isolation (id=244): NEVER mix with trade_check / V8 basket rules. Separate context.
BFSI ROCE exemption preserved (F4 auto-credited for bank/nbfc/insurance/amc/finance segments).
"""

from fastapi import APIRouter, Query
from typing import Optional
import os
import psycopg
import traceback

router = APIRouter()

VERSION = "v3.0"
SPEC_REF = "session_log id=6632 (INVEST_CHECK_V3_0_GATE_CONVICTION_FINAL, locked 2026-07-20)"
MAX_CONVICTION = 15

BFSI_KEYWORDS = ["bank", "nbfc", "insurance", "amc", "finance",
                 "capital market", "housing finance"]


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def is_bfsi(segment: str) -> bool:
    seg = (segment or "").lower()
    return any(k in seg for k in BFSI_KEYWORDS)


def get_cap_category(market_cap: Optional[float]) -> str:
    if not market_cap:
        return "Unknown"
    if market_cap >= 20000:
        return "Large"
    if market_cap >= 5000:
        return "Mid"
    return "Small"


# ── peer averages: TOP-3 by gvm in segment, self-excluded, non-null; <3 -> full-segment avg ──
def _top3_peer_avg(peers, valkey):
    """peers: list of dicts each carrying 'gvm' + the metric. Returns the top-3-by-gvm average
    of `valkey` over non-null peers; if fewer than 3 qualify, the full-segment (all non-null) avg."""
    cand = [(p["gvm"], p[valkey]) for p in peers
            if p.get(valkey) is not None and p.get("gvm") is not None]
    if not cand:
        return None
    cand.sort(key=lambda x: -x[0])
    use = cand[:3] if len(cand) >= 3 else cand   # <3 qualifying -> full-segment avg (all non-null)
    return sum(v for _, v in use) / len(use)


# ── verdict vocabulary ──
_VERDICT_META = {
    "STRONG_INVEST": {"display": "Strong Invest", "emoji": "🟢", "css": "strong-invest"},
    "INVEST":        {"display": "Invest",        "emoji": "🟢", "css": "invest"},
    "WATCH":         {"display": "Watch",         "emoji": "🟠", "css": "watch"},
    "AVOID":         {"display": "Avoid",         "emoji": "🔴", "css": "avoid"},
}


def _rnd(v, dp=2):
    return round(v, dp) if v is not None else None


# ── the ONE scorer ────────────────────────────────────────────────────────────
def _score_record(rec, peers):
    """rec: self metrics dict. peers: list of same-segment peer dicts (self already excluded).
    Returns the full gate+conviction result block (no company/segment framing — caller adds that)."""
    bfsi = is_bfsi(rec.get("segment"))

    gvm = rec.get("gvm"); g = rec.get("g"); v = rec.get("v"); m = rec.get("m")
    profit_5y = rec.get("profit_5y")

    # ── Layer 1 — 3 hard gates ──
    g1_val = v
    g2_val = gvm
    g3_val = profit_5y
    gates = [
        {"gate": "G1", "name": "Valuation (V ≥ 6)", "condition": "V-Score ≥ 6",
         "value": _rnd(g1_val), "pass": (None if g1_val is None else g1_val >= 6.0)},
        {"gate": "G2", "name": "Quality (GVM ≥ 7)", "condition": "GVM ≥ 7.0",
         "value": _rnd(g2_val), "pass": (None if g2_val is None else g2_val >= 7.0)},
        {"gate": "G3", "name": "Growth intact (Profit 5Y > 10%)", "condition": "Profit 5Y CAGR > 10%",
         "value": _rnd(g3_val, 1), "pass": (None if g3_val is None else g3_val > 10.0)},
    ]
    null_gate = any(gt["pass"] is None for gt in gates)
    gate_fails = [gt for gt in gates if gt["pass"] is False]
    gates_passed = sum(1 for gt in gates if gt["pass"] is True)

    # ── Layer 2 — 12 conviction filters (max 15) ──
    filters = []

    # F1 V-depth (max 2)
    if v is None:
        f1 = 0.0
    else:
        f1 = 2.0 if v >= 7.5 else (1.0 if v >= 6.0 else 0.0)
    filters.append({"code": "F1", "name": "Valuation depth (V)", "max": 2, "points": f1,
                    "value": _rnd(v), "peer": None, "note": None})

    # F2 G-score (max 2)
    if g is None:
        f2 = 0.0
    else:
        f2 = 2.0 if g >= 7.5 else (1.0 if g >= 6.0 else 0.0)
    filters.append({"code": "F2", "name": "Growth score (G)", "max": 2, "points": f2,
                    "value": _rnd(g), "peer": None, "note": None})

    # F3 result rating (max 2) — QoQ sales AND profit both > top-3 peer avg -> 2 ; one -> 1 ; else 0
    qs, qp = rec.get("qoq_sales"), rec.get("qoq_profit")
    peer_qs = _top3_peer_avg(peers, "qoq_sales")
    peer_qp = _top3_peer_avg(peers, "qoq_profit")
    s_ok = (qs is not None and peer_qs is not None and qs > peer_qs)
    p_ok = (qp is not None and peer_qp is not None and qp > peer_qp)
    f3 = 2.0 if (s_ok and p_ok) else (1.0 if (s_ok or p_ok) else 0.0)
    filters.append({"code": "F3", "name": "Latest result vs top-3 peers", "max": 2, "points": f3,
                    "value": (f"Sales {_rnd(qs,1)}% · PAT {_rnd(qp,1)}%" if (qs is not None or qp is not None) else None),
                    "peer": (f"Sales {_rnd(peer_qs,1)}% · PAT {_rnd(peer_qp,1)}%"
                             if (peer_qs is not None or peer_qp is not None) else None),
                    "note": None})

    # F4 ROCE (max 1) — > top-3 peer avg -> 1 ; BFSI exempt -> 1
    roce = rec.get("roce")
    if bfsi:
        f4 = 1.0
        f4_peer = None; f4_note = "BFSI exempt"
    else:
        peer_roce = _top3_peer_avg(peers, "roce")
        f4 = 1.0 if (roce is not None and peer_roce is not None and roce > peer_roce) else 0.0
        f4_peer = _rnd(peer_roce, 1); f4_note = None
    filters.append({"code": "F4", "name": "ROCE vs top-3 peers", "max": 1, "points": f4,
                    "value": _rnd(roce, 1), "peer": f4_peer, "note": f4_note})

    # F5 OPM (max 1)
    opm = rec.get("opm")
    peer_opm = _top3_peer_avg(peers, "opm")
    f5 = 1.0 if (opm is not None and peer_opm is not None and opm > peer_opm) else 0.0
    filters.append({"code": "F5", "name": "OPM vs top-3 peers", "max": 1, "points": f5,
                    "value": _rnd(opm, 1), "peer": _rnd(peer_opm, 1), "note": None})

    # F6 sales 5Y CAGR (max 1)
    sales_5y = rec.get("sales_5y")
    peer_sales = _top3_peer_avg(peers, "sales_5y")
    f6 = 1.0 if (sales_5y is not None and peer_sales is not None and sales_5y > peer_sales) else 0.0
    filters.append({"code": "F6", "name": "Sales 5Y CAGR vs top-3 peers", "max": 1, "points": f6,
                    "value": _rnd(sales_5y, 1), "peer": _rnd(peer_sales, 1), "note": None})

    # F7 cashflow quality (max 1) — CFO/PAT >= 0.8
    cfo_pat = rec.get("cfo_pat")
    f7 = 1.0 if (cfo_pat is not None and cfo_pat >= 0.8) else 0.0
    filters.append({"code": "F7", "name": "Cashflow quality (CFO/PAT ≥ 0.8)", "max": 1, "points": f7,
                    "value": _rnd(cfo_pat), "peer": None, "note": None})

    # F8 ΔGVM 180d (max 1) — >0.5 ->1 ; 0-0.5 ->0.5 ; <0 ->0 ; no history on/before today-180d ->0
    gvm180 = rec.get("gvm180")
    if gvm is not None and gvm180 is not None:
        d180 = gvm - gvm180
        f8 = 1.0 if d180 > 0.5 else (0.5 if d180 >= 0 else 0.0)
        f8_val = _rnd(d180)
    else:
        f8 = 0.0
        f8_val = None
    filters.append({"code": "F8", "name": "GVM trend (Δ 180d)", "max": 1, "points": f8,
                    "value": f8_val, "peer": None,
                    "note": (None if gvm180 is not None else "no 180d history")})

    # F9 net institutional (max 1) — FII+DII change > 0
    fii, dii = rec.get("fii"), rec.get("dii")
    if fii is not None and dii is not None:
        net = fii + dii
        f9 = 1.0 if net > 0 else 0.0
        f9_val = _rnd(net)
    else:
        f9 = 0.0
        f9_val = None
    filters.append({"code": "F9", "name": "Net institutional (FII+DII > 0)", "max": 1, "points": f9,
                    "value": f9_val, "peer": None, "note": None})

    # F10 1Y return (max 1) — > top-3-by-GVM peer avg (beat leaders, NOT just >0)
    ret_1y = rec.get("ret_1y")
    peer_ret = _top3_peer_avg(peers, "ret_1y")
    f10 = 1.0 if (ret_1y is not None and peer_ret is not None and ret_1y > peer_ret) else 0.0
    filters.append({"code": "F10", "name": "1Y return vs top-3 peers", "max": 1, "points": f10,
                    "value": _rnd(ret_1y, 1), "peer": _rnd(peer_ret, 1), "note": None})

    # F11 volume accumulation (max 1) — 3d avg vol > 21d avg vol AND price up over those 3 days
    v3, v21 = rec.get("vol_avg3"), rec.get("vol_avg21")
    c0, c3 = rec.get("vol_c0"), rec.get("vol_c3")
    if None not in (v3, v21, c0, c3):
        f11 = 1.0 if (v3 > v21 and c0 > c3) else 0.0
        f11_val = f"3d {round(v3/1e5,1)}L vs 21d {round(v21/1e5,1)}L · {'up' if c0 > c3 else 'flat/down'}"
    else:
        f11 = 0.0
        f11_val = None
    filters.append({"code": "F11", "name": "Volume accumulation (3d>21d & up)", "max": 1, "points": f11,
                    "value": f11_val, "peer": None, "note": None})

    # F12 M-score (max 1) — >=6 ->1 ; 5-6 ->0.5 ; else 0
    if m is None:
        f12 = 0.0
    else:
        f12 = 1.0 if m >= 6.0 else (0.5 if m >= 5.0 else 0.0)
    filters.append({"code": "F12", "name": "Momentum score (M)", "max": 1, "points": f12,
                    "value": _rnd(m), "peer": None, "note": None})

    conviction = round(sum(fl["points"] for fl in filters), 1)

    # ── verdict ──
    if null_gate:
        verdict = "WATCH"; insufficient = True
    elif len(gate_fails) >= 2:
        verdict = "AVOID"; insufficient = False
    elif len(gate_fails) == 1:
        verdict = "WATCH"; insufficient = False
    else:
        insufficient = False
        if conviction >= 12:
            verdict = "STRONG_INVEST"
        elif conviction >= 9:
            verdict = "INVEST"
        else:
            verdict = "WATCH"

    meta = _VERDICT_META[verdict]
    return {
        "gates": gates,
        "gates_passed": gates_passed,
        "gate_fails": [gt["gate"] + " " + gt["name"] for gt in gate_fails],
        "insufficient_data": insufficient,
        "filters": filters,
        "conviction": conviction,
        "max_conviction": MAX_CONVICTION,
        "verdict": verdict,
        "verdict_display": meta["display"],
        "emoji": meta["emoji"],
        "verdict_css": meta["css"],
        "bfsi": bfsi,
    }


# ── single-symbol loaders ─────────────────────────────────────────────────────
def _load_self(cur, symbol):
    cur.execute("""SELECT gvm_score, g_score, v_score, m_score, segment, verdict
                   FROM gvm_scores WHERE symbol=%s""", (symbol,))
    gvm = cur.fetchone()
    if not gvm:
        return None, f"Symbol {symbol} not found in GVM universe"
    cur.execute('''SELECT sales_growth_5y, profit_growth_5y, opm, roce,
                          qoq_sales_growth, qoq_profit_growth, market_cap, company_name,
                          fii_change, dii_change, "Cfo by Pat"
                   FROM screener_raw WHERE nse_code=%s''', (symbol,))
    scr = cur.fetchone()
    if not scr:
        return None, f"Symbol {symbol} not found in screener_raw"
    cur.execute("""SELECT year_return FROM v8_metrics WHERE symbol=%s
                   ORDER BY computed_at DESC LIMIT 1""", (symbol,))
    v8 = cur.fetchone()
    cur.execute("""SELECT gvm_score FROM gvm_history WHERE symbol=%s
                   AND score_date <= CURRENT_DATE - 180 ORDER BY score_date DESC LIMIT 1""", (symbol,))
    gh = cur.fetchone()
    # F11 volume window — last 21 sessions (oldest window bounded by a generous 45-cal-day filter)
    cur.execute("""
        WITH r AS (
            SELECT close, volume,
                   ROW_NUMBER() OVER (ORDER BY price_date DESC) AS rn
            FROM raw_prices
            WHERE symbol=%s AND volume IS NOT NULL AND close IS NOT NULL
              AND price_date >= CURRENT_DATE - INTERVAL '60 days'
        )
        SELECT AVG(volume) FILTER (WHERE rn <= 3)  AS avg3,
               AVG(volume) FILTER (WHERE rn <= 21) AS avg21,
               MAX(close)  FILTER (WHERE rn = 1)   AS c0,
               MAX(close)  FILTER (WHERE rn = 4)   AS c3
        FROM r WHERE rn <= 21
    """, (symbol,))
    vol = cur.fetchone() or (None, None, None, None)

    rec = {
        "symbol": symbol,
        "gvm": _f(gvm[0]), "g": _f(gvm[1]), "v": _f(gvm[2]), "m": _f(gvm[3]),
        "segment": gvm[4], "gvm_verdict": gvm[5],
        "sales_5y": _f(scr[0]), "profit_5y": _f(scr[1]), "opm": _f(scr[2]), "roce": _f(scr[3]),
        "qoq_sales": _f(scr[4]), "qoq_profit": _f(scr[5]), "market_cap": _f(scr[6]),
        "company": scr[7], "fii": _f(scr[8]), "dii": _f(scr[9]), "cfo_pat": _f(scr[10]),
        "ret_1y": _f(v8[0]) if v8 else None,
        "gvm180": _f(gh[0]) if gh else None,
        "vol_avg3": _f(vol[0]), "vol_avg21": _f(vol[1]),
        "vol_c0": _f(vol[2]), "vol_c3": _f(vol[3]),
    }
    return rec, None


def _load_peers(cur, segment, self_symbol):
    """Segment peers (self excluded) carrying gvm + the 5 peer-relative metric columns + 1Y return."""
    cur.execute("""
        SELECT g.symbol, g.gvm_score,
               s.qoq_sales_growth, s.qoq_profit_growth, s.roce, s.opm, s.sales_growth_5y,
               v.year_return
        FROM gvm_scores g
        JOIN screener_raw s ON g.symbol = s.nse_code
        LEFT JOIN LATERAL (SELECT year_return FROM v8_metrics
                           WHERE symbol=g.symbol ORDER BY computed_at DESC LIMIT 1) v ON TRUE
        WHERE g.segment=%s AND g.symbol<>%s
    """, (segment, self_symbol))
    peers = []
    for r in cur.fetchall():
        peers.append({"gvm": _f(r[1]), "qoq_sales": _f(r[2]), "qoq_profit": _f(r[3]),
                      "roce": _f(r[4]), "opm": _f(r[5]), "sales_5y": _f(r[6]), "ret_1y": _f(r[7])})
    return peers


def _compute_single(cur, symbol):
    rec, err = _load_self(cur, symbol)
    if err:
        return {"error": err}
    peers = _load_peers(cur, rec["segment"], symbol) if rec["segment"] else []
    block = _score_record(rec, peers)
    block.update({
        "symbol": symbol,
        "company": rec["company"],
        "segment": rec["segment"],
        "cap_category": get_cap_category(rec["market_cap"]),
        "market_cap_cr": round(rec["market_cap"], 0) if rec["market_cap"] is not None else None,
        "gvm_verdict": rec["gvm_verdict"],
        "gvm": rec["gvm"], "g": rec["g"], "v": rec["v"], "m": rec["m"],
        "version": VERSION, "spec": SPEC_REF, "max_score": MAX_CONVICTION,
    })
    return block


# ── endpoints ─────────────────────────────────────────────────────────────────
@router.get("/api/investment-check")
def investment_check(symbol: str = Query(..., description="NSE symbol")):
    try:
        with _conn() as conn, conn.cursor() as cur:
            return _compute_single(cur, symbol.upper().strip())
    except Exception as e:
        return {"error": str(e), "trace": traceback.format_exc()}


@router.get("/api/investment-check/detail")
def investment_check_detail(symbol: str = Query(..., description="NSE symbol")):
    """Single result + evidence (GVM trend series, peer averages, freshness stamps) for the
    inline 'View Detailed' drill-down. Same v3.0 scorer — evidence is additive, never re-scored."""
    try:
        sym = symbol.upper().strip()
        with _conn() as conn, conn.cursor() as cur:
            res = _compute_single(cur, sym)
            if "error" in res:
                return res
            cur.execute("""SELECT score_date FROM gvm_scores WHERE symbol=%s""", (sym,))
            gsd = cur.fetchone()
            cur.execute("""SELECT loaded_at FROM screener_raw WHERE nse_code=%s""", (sym,))
            lo = cur.fetchone()
            cur.execute("""SELECT score_date, g_score, m_score, gvm_score FROM gvm_history
                           WHERE symbol=%s ORDER BY score_date DESC LIMIT 130""", (sym,))
            trend = [{"date": str(r[0]),
                      "g": _f(r[1]), "m": _f(r[2]), "gvm": _f(r[3])} for r in cur.fetchall()[::-1]]
            res["freshness"] = {
                "gvm_score_date": str(gsd[0]) if gsd and gsd[0] else None,
                "screener_loaded_at": str(lo[0]) if lo and lo[0] else None,
            }
            res["evidence"] = {"gvm_trend": trend}
            return res
    except Exception as e:
        return {"error": str(e), "trace": traceback.format_exc()}


# ── batch scoring for /screener + /summary (SAME scorer, one source) ────────────
def _score_universe(cur):
    """Load the whole universe once, score every symbol through the SAME _score_record used by the
    single endpoint (peers grouped per segment, self-excluded). Returns a list of scored dicts."""
    # self metrics for the whole universe
    cur.execute('''
        SELECT g.symbol, g.gvm_score, g.g_score, g.v_score, g.m_score, g.segment, g.verdict,
               s.company_name, s.market_cap, s.sales_growth_5y, s.profit_growth_5y, s.opm, s.roce,
               s.qoq_sales_growth, s.qoq_profit_growth, s.fii_change, s.dii_change, s."Cfo by Pat"
        FROM gvm_scores g JOIN screener_raw s ON g.symbol = s.nse_code
    ''')
    recs = {}
    for r in cur.fetchall():
        recs[r[0]] = {
            "symbol": r[0], "gvm": _f(r[1]), "g": _f(r[2]), "v": _f(r[3]), "m": _f(r[4]),
            "segment": r[5], "gvm_verdict": r[6], "company": r[7], "market_cap": _f(r[8]),
            "sales_5y": _f(r[9]), "profit_5y": _f(r[10]), "opm": _f(r[11]), "roce": _f(r[12]),
            "qoq_sales": _f(r[13]), "qoq_profit": _f(r[14]), "fii": _f(r[15]), "dii": _f(r[16]),
            "cfo_pat": _f(r[17]),
            "ret_1y": None, "gvm180": None,
            "vol_avg3": None, "vol_avg21": None, "vol_c0": None, "vol_c3": None,
        }
    # 1Y return (latest v8_metrics per symbol)
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, year_return
                   FROM v8_metrics ORDER BY symbol, computed_at DESC""")
    for sym, yr in cur.fetchall():
        if sym in recs:
            recs[sym]["ret_1y"] = _f(yr)
    # ΔGVM 180d (nearest history on/before today-180)
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, gvm_score
                   FROM gvm_history WHERE score_date <= CURRENT_DATE - 180
                   ORDER BY symbol, score_date DESC""")
    for sym, gv in cur.fetchall():
        if sym in recs:
            recs[sym]["gvm180"] = _f(gv)
    # volume accumulation window (3d vs 21d + 3-session price move)
    cur.execute("""
        WITH r AS (
            SELECT symbol, close, volume,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
            FROM raw_prices
            WHERE volume IS NOT NULL AND close IS NOT NULL
              AND price_date >= CURRENT_DATE - INTERVAL '60 days'
        )
        SELECT symbol,
               AVG(volume) FILTER (WHERE rn <= 3)  AS avg3,
               AVG(volume) FILTER (WHERE rn <= 21) AS avg21,
               MAX(close)  FILTER (WHERE rn = 1)   AS c0,
               MAX(close)  FILTER (WHERE rn = 4)   AS c3
        FROM r WHERE rn <= 21 GROUP BY symbol
    """)
    for sym, a3, a21, c0, c3 in cur.fetchall():
        if sym in recs:
            recs[sym].update({"vol_avg3": _f(a3), "vol_avg21": _f(a21),
                              "vol_c0": _f(c0), "vol_c3": _f(c3)})

    # group peers by segment
    by_seg = {}
    for rec in recs.values():
        by_seg.setdefault(rec["segment"], []).append(rec)

    def peer_view(rec):
        return {"gvm": rec["gvm"], "qoq_sales": rec["qoq_sales"], "qoq_profit": rec["qoq_profit"],
                "roce": rec["roce"], "opm": rec["opm"], "sales_5y": rec["sales_5y"],
                "ret_1y": rec["ret_1y"]}

    out = []
    for rec in recs.values():
        seg_members = by_seg.get(rec["segment"], [])
        peers = [peer_view(p) for p in seg_members if p["symbol"] != rec["symbol"]]
        block = _score_record(rec, peers)
        block.update({
            "symbol": rec["symbol"], "company": rec["company"], "segment": rec["segment"],
            "cap_category": get_cap_category(rec["market_cap"]),
            "gvm": rec["gvm"],
        })
        out.append(block)
    return out


_VERDICT_RANK = {"STRONG_INVEST": 3, "INVEST": 2, "WATCH": 1, "AVOID": 0}


@router.get("/api/investment-check/screener")
def investment_screener(
    verdict: Optional[str] = Query(None, description="STRONG_INVEST | INVEST | WATCH | AVOID"),
    cap: Optional[str] = Query(None, description="Large | Mid | Small"),
    limit: int = Query(50, le=300),
):
    try:
        with _conn() as conn, conn.cursor() as cur:
            scored = _score_universe(cur)
        vf = (verdict or "").upper().replace(" ", "_").strip() or None
        cf = (cap or "").capitalize().strip() or None
        rows = []
        for s in scored:
            if vf and s["verdict"] != vf:
                continue
            if cf and s["cap_category"] != cf:
                continue
            rows.append({"symbol": s["symbol"], "company": s["company"], "segment": s["segment"],
                         "cap": s["cap_category"], "gvm": _rnd(s["gvm"], 1),
                         "gates_passed": s["gates_passed"], "conviction": s["conviction"],
                         "max_conviction": MAX_CONVICTION,
                         "verdict": s["verdict"], "verdict_display": s["verdict_display"],
                         "emoji": s["emoji"]})
        rows.sort(key=lambda x: (-_VERDICT_RANK.get(x["verdict"], 0), -x["conviction"],
                                 -(float(x["gvm"] or 0))))
        total = len(rows)
        return {"total_matched": total, "returned": min(total, limit),
                "filters": {"verdict": vf, "cap": cf},
                "version": VERSION, "spec": SPEC_REF, "stocks": rows[:limit]}
    except Exception as e:
        return {"error": str(e), "trace": traceback.format_exc()}


@router.get("/api/investment-check/summary")
def investment_summary():
    try:
        with _conn() as conn, conn.cursor() as cur:
            scored = _score_universe(cur)
        bands = {k: {"total": 0, "large": 0, "mid": 0, "small": 0}
                 for k in ("STRONG_INVEST", "INVEST", "WATCH", "AVOID")}
        capkey = {"Large": "large", "Mid": "mid", "Small": "small"}
        for s in scored:
            b = bands[s["verdict"]]
            b["total"] += 1
            ck = capkey.get(s["cap_category"])
            if ck:
                b[ck] += 1
        universe = len(scored)
        buy_zone = bands["STRONG_INVEST"]["total"] + bands["INVEST"]["total"]
        return {
            "universe": universe,
            "buy_zone": buy_zone,
            "bands": bands,
            "verdict_scale": {
                "STRONG_INVEST": "3 gates pass · conviction ≥ 12/15",
                "INVEST": "3 gates pass · conviction ≥ 9/15",
                "WATCH": "fails 1 gate, or gates pass but conviction < 9, or insufficient data",
                "AVOID": "fails 2+ gates",
            },
            "version": VERSION, "spec": SPEC_REF,
        }
    except Exception as e:
        return {"error": str(e), "trace": traceback.format_exc()}


# ── compat shim: POST /api/check?side=INVEST + /ask native wiring ──────────────
# check_endpoint.py imports this; keeps the /ask + native_router path alive on the v3.0 engine.
def compute_invest_check(symbol_text, use_api=False):
    import re
    sym = re.sub(r"\b(investment|invest|check|review|analyse|analyze|stock|on|for|a|the)\b",
                 " ", (symbol_text or ""), flags=re.I).strip().upper()
    if not sym:
        return {"ok": False, "error": "Specify a symbol, e.g. RELIANCE."}
    try:
        with _conn() as conn, conn.cursor() as cur:
            res = _compute_single(cur, sym)
    except Exception as e:
        return {"ok": False, "error": f"DB error: {str(e)[:160]}"}
    if "error" in res:
        return {"ok": False, "error": res["error"]}
    res["ok"] = True
    res["side"] = "INVEST"
    return res
