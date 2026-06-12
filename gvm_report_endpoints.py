"""
GVM Company Report — peer-benchmark analytics endpoint (Model 2).

v4 (12-Jun-2026 night):
  - Annual Upside (Potential Upside) computed live from
    input_raw.fy27_growth × (pe / historical_pe)  — engine formula,
    matches gvm_nightly._pu() verbatim. No dependency on gvm_scores.upside_raw.
  - DMA-50 / DMA-200 changed to synthetic deviation % columns
    (_dma50_dev, _dma200_dev) = ((price - dma) / dma) × 100.
    Raw screener columns dma_50 / dma_200 store the DMA price LEVEL, not %.
  - dropped segment_avg from extras (vs-Segment toggle removed in UI).

v3 (12-Jun-2026 eve):
  - PARAMS mirrors canonical engine scored set (G13 + V2 + M5 + IC).
"""

from fastapi import APIRouter, HTTPException
from datetime import datetime, timedelta, date
from typing import Optional, List, Dict, Any
import psycopg
import os

from gvm_page_extras import build_page_extras

router = APIRouter(tags=["gvm_report"])


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _ist_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


_BFSI_KEYWORDS = (
    "bank", "nbfc", "finance", "financial", "insurance", "amc",
    "exchange", "capital market", "broking", "wealth", "housing finance",
    "microfinance", "msme finance", "life insurance",
)


def _is_bfsi(segment: str) -> bool:
    s = (segment or "").lower()
    return any(k in s for k in _BFSI_KEYWORDS)


# ── Canonical scored parameters (mirrors gvm_engine.py) ─────────────────
# key, label, screener_raw column, pillar (G/V/M), higher_is_better, unit
# Columns starting with "_" are synthetic (computed in Python after fetch).
PARAMS = [
    # G — Growth / Fundamentals / Profitability
    ("sales_5y",     "Sales 5Y CAGR",         "sales_growth_5y",        "G", True,  "%"),
    ("sales_3y",     "Sales 3Y CAGR",         "sales_growth_3y",        "G", True,  "%"),
    ("profit_5y",    "Profit 5Y CAGR",        "profit_growth_5y",       "G", True,  "%"),
    ("profit_3y",    "Profit 3Y CAGR",        "profit_growth_3y",       "G", True,  "%"),
    ("qoq_sales",    "QoQ Sales Growth",      "qoq_sales_growth",       "G", True,  "%"),
    ("qoq_profit",   "QoQ Profit Growth",     "qoq_profit_growth",      "G", True,  "%"),
    ("opm",          "Operating Margin",      "opm",                    "G", True,  "%"),
    ("opm_exp",      "OPM Expansion",         "Operating profit growth","G", True,  "%"),
    ("fa_growth",    "Fixed Asset Growth",    "fixed_asset_growth",     "G", True,  "%"),
    ("inst_abs",     "Inst Holding (abs)",    "_inst_combined_abs",     "G", True,  "%"),
    ("inst_chg",     "Inst Holding Change",   "_inst_combined_chg",     "G", True,  "%"),
    ("roce",         "ROCE",                  "roce",                   "G", True,  "%"),
    ("div_yield",    "Dividend Yield",        "dividend_yield",         "G", True,  "%"),
    ("int_cov",      "Interest Coverage",     "interest_coverage",      "G", True,  "x"),
    # V — Value
    ("pe",           "PE Multiple",           "pe",                     "V", False, "x"),
    ("upside",       "Annual Upside",         "_upside_computed",       "V", True,  "%"),
    # M — Momentum
    ("ret_1y",       "Return 1 Year",         "return_1y",              "M", True,  "%"),
    ("ret_3y",       "Return 3 Year",         "return_3y",              "M", True,  "%"),
    ("dma_50",       "Price vs 50 DMA",       "_dma50_dev",             "M", True,  "%"),
    ("dma_200",      "Price vs 200 DMA",      "_dma200_dev",            "M", True,  "%"),
    ("ret_52w_idx",  "52W vs Index",          "return_52w_vs_index",    "M", True,  "%"),
]


def _col_sql(col: str) -> Optional[str]:
    """Return quoted-if-needed SQL column, or None for synthetic columns."""
    if col.startswith("_"):
        return None
    if col == col.lower() and " " not in col:
        return col
    return f'"{col}"'


def _rate(value: Optional[float], peer_values: List[float], higher_is_better: bool) -> Optional[float]:
    if value is None or not peer_values:
        return None
    lo = min(peer_values)
    hi = max(peer_values)
    if hi == lo:
        return 5.0
    pos = (value - lo) / (hi - lo)
    if not higher_is_better:
        pos = 1.0 - pos
    return round(2.0 + pos * 8.0, 2)


def _rank(value: Optional[float], peer_values: List[float], higher_is_better: bool) -> Optional[int]:
    if value is None or not peer_values:
        return None
    ordered = sorted(peer_values, reverse=higher_is_better)
    for i, v in enumerate(ordered):
        if abs(v - value) < 1e-9:
            return i + 1
    better = sum(1 for v in peer_values if (v > value if higher_is_better else v < value))
    return better + 1


def _compute_upside(fy27, pe, hist_pe):
    """Engine formula: upside = fy27_growth × (pe / hist_pe). Multiplier 1.0 if hist invalid."""
    fy = _f(fy27)
    if fy is None:
        return None
    if fy == 0:
        return 0.0
    p, h = _f(pe), _f(hist_pe)
    mult = (p / h) if (p is not None and h is not None and h > 0) else 1.0
    return round(fy * mult, 2)


# ── Search ─────────────────────────────────────────────────────────────────
@router.get("/api/gvm/search")
def gvm_search(q: str, limit: int = 10):
    q = (q or "").strip()
    if len(q) < 1:
        return {"query": q, "results": []}
    limit = min(max(limit, 1), 25)
    like = f"%{q}%"
    pref = f"{q}%"
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, company_name, segment, gvm_score, verdict, market_cap
                FROM gvm_scores
                WHERE symbol ILIKE %s OR company_name ILIKE %s
                ORDER BY (symbol ILIKE %s) DESC, market_cap DESC NULLS LAST
                LIMIT %s
            """, (like, like, pref, limit))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            r["gvm_score"] = _f(r.get("gvm_score"))
            r["market_cap"] = _f(r.get("market_cap"))
        return {"query": q, "count": len(rows), "results": rows}
    except Exception as e:
        raise HTTPException(500, f"search failed: {e}")


# ── Full company report ────────────────────────────────────────────────────
@router.get("/api/gvm/company/{symbol}")
def gvm_company_report(symbol: str, persist: bool = True):
    symbol = symbol.upper().strip()

    head = _query_single("""
        SELECT g.symbol, g.company_name, g.segment, g.price,
               g.g_score, g.v_score, g.m_score, g.gvm_score,
               g.verdict, g.punchline, g.market_cap,
               i.overview, i.key_takeaway, i.result_analysis,
               i.instrument_type, i.cap_category, i.mcap_rank,
               i.last_overview_updated::text AS last_overview_updated,
               i.last_takeaway_updated::text AS last_takeaway_updated,
               i.last_result_analysis_updated::text AS last_result_analysis_updated
        FROM gvm_scores g
        LEFT JOIN input_raw i ON i.nse_code = g.symbol
        WHERE g.symbol = %s
    """, (symbol,))
    if not head:
        raise HTTPException(404, f"{symbol} not found")

    segment = head.get("segment")
    is_bfsi = _is_bfsi(segment)

    # Peer fetch — pull all real columns + raw bits needed to derive synthetics.
    real_cols = [(key, col) for key, _, col, *_ in PARAMS if not col.startswith("_")]
    sel_cols = ", ".join(f"s.{_col_sql(col)} AS {key}" for key, col in real_cols)
    peers = _query_all(f"""
        SELECT g.symbol, g.company_name, g.market_cap,
               s.price AS _price, s.dma_50 AS _dma50, s.dma_200 AS _dma200,
               s.fii_holding AS _fii_abs, s.dii_holding AS _dii_abs,
               s.fii_change AS _fii_chg, s.dii_change AS _dii_chg,
               s.pe AS _pe_raw, s.historical_pe AS _hpe_raw,
               i.fy27_growth AS _fy27,
               {sel_cols}
        FROM gvm_scores g
        JOIN screener_raw s ON s.nse_code = g.symbol
        LEFT JOIN input_raw i ON i.nse_code = g.symbol
        WHERE g.segment = %s
        ORDER BY g.market_cap DESC NULLS LAST
    """, (segment,))

    # Compute synthetic columns per peer
    for p in peers:
        # Inst holding combined
        fa, da = _f(p.get("_fii_abs")), _f(p.get("_dii_abs"))
        p["inst_abs"] = (round((fa or 0) + (da or 0), 2)
                         if (fa is not None or da is not None) else None)
        fc, dc = _f(p.get("_fii_chg")), _f(p.get("_dii_chg"))
        p["inst_chg"] = (round((fc or 0) + (dc or 0), 2)
                         if (fc is not None or dc is not None) else None)
        # Annual Upside (engine formula)
        p["upside"] = _compute_upside(p.get("_fy27"), p.get("_pe_raw"), p.get("_hpe_raw"))
        # DMA deviation % (price vs DMA level)
        price, d50, d200 = _f(p.get("_price")), _f(p.get("_dma50")), _f(p.get("_dma200"))
        p["dma_50"] = (round((price - d50) / d50 * 100, 2)
                       if price is not None and d50 and d50 > 0 else None)
        p["dma_200"] = (round((price - d200) / d200 * 100, 2)
                        if price is not None and d200 and d200 > 0 else None)

    peer_count = len(peers)
    peer_names = [p["symbol"] for p in peers]

    benchmark = []
    company_row = next((p for p in peers if p["symbol"] == symbol), None)

    for key, label, col, pillar, hib, unit in PARAMS:
        if is_bfsi and key == "int_cov":
            continue

        peer_vals = [_f(p.get(key)) for p in peers]
        peer_vals = [v for v in peer_vals if v is not None]
        comp_val = _f(company_row.get(key)) if company_row else None
        peer_avg = round(sum(peer_vals) / len(peer_vals), 2) if peer_vals else None

        if comp_val is None and peer_avg is None:
            continue

        benchmark.append({
            "key": key,
            "label": label,
            "group": pillar,
            "pillar": pillar,
            "unit": unit,
            "higher_is_better": hib,
            "company": comp_val,
            "peer_avg": peer_avg,
            "rating": _rate(comp_val, peer_vals, hib),
            "rank": _rank(comp_val, peer_vals, hib),
            "peer_n": len(peer_vals),
            "best": _best_peer(peers, key, hib),
            "worst": _best_peer(peers, key, not hib),
        })

    groups: Dict[str, List[float]] = {}
    for b in benchmark:
        if b["rating"] is None:
            continue
        groups.setdefault(b["pillar"], []).append(b["rating"])
    group_scores = {g: round(sum(v) / len(v), 2) for g, v in groups.items()}

    ladder = _query_all("""
        SELECT symbol, company_name, gvm_score, verdict
        FROM gvm_scores WHERE segment=%s ORDER BY gvm_score DESC
    """, (segment,))
    seg_rank = next((i + 1 for i, r in enumerate(ladder) if r["symbol"] == symbol), None)

    try:
        px = build_page_extras(symbol, [r["symbol"] for r in ladder], segment=segment)
        extras_block = px.get("extras") or {}
        ladder_extra = px.get("ladder_extra") or {}
    except Exception as e:
        extras_block = {"error": str(e)[:200]}
        ladder_extra = {}

    rated = [b for b in benchmark if b["rating"] is not None]
    positives = sorted([b for b in rated if b["rating"] >= 6.5], key=lambda x: -x["rating"])[:5]
    negatives = sorted([b for b in rated if b["rating"] < 5.0], key=lambda x: x["rating"])[:5]

    payload = {
        "symbol": symbol,
        "company_name": head.get("company_name"),
        "segment": segment,
        "is_bfsi": is_bfsi,
        "price": _f(head.get("price")),
        "market_cap": _f(head.get("market_cap")),
        "mcap_rank": head.get("mcap_rank"),
        "cap_category": head.get("cap_category"),
        "instrument_type": head.get("instrument_type"),
        "scores": {
            "g": _f(head.get("g_score")),
            "v": _f(head.get("v_score")),
            "m": _f(head.get("m_score")),
            "gvm": _f(head.get("gvm_score")),
        },
        "verdict": head.get("verdict"),
        "punchline": head.get("punchline"),
        "segment_rank": seg_rank,
        "segment_total": len(ladder),
        "group_scores": group_scores,
        "benchmark": benchmark,
        "ladder": [
            {"symbol": r["symbol"], "company_name": r["company_name"],
             "gvm": _f(r["gvm_score"]), "verdict": r["verdict"],
             "is_self": r["symbol"] == symbol,
             **(ladder_extra.get(r["symbol"], {}))}
            for r in ladder
        ],
        "peers": peer_names,
        "peer_count": peer_count,
        "positives": [{"label": b["label"], "rating": b["rating"], "company": b["company"],
                       "peer_avg": b["peer_avg"], "unit": b["unit"], "pillar": b["pillar"]} for b in positives],
        "negatives": [{"label": b["label"], "rating": b["rating"], "company": b["company"],
                       "peer_avg": b["peer_avg"], "unit": b["unit"], "pillar": b["pillar"]} for b in negatives],
        "extras": extras_block,
        "content": {
            "overview": head.get("overview"),
            "key_takeaway": head.get("key_takeaway"),
            "result_analysis": head.get("result_analysis"),
            "last_overview_updated": head.get("last_overview_updated"),
            "last_takeaway_updated": head.get("last_takeaway_updated"),
            "last_result_analysis_updated": head.get("last_result_analysis_updated"),
        },
        "generated_at": _ist_now().strftime("%Y-%m-%d %H:%M:%S IST"),
    }

    if persist:
        try:
            _persist_detail(symbol, benchmark)
            payload["persisted"] = True
        except Exception as e:
            payload["persisted"] = False
            payload["persist_error"] = str(e)[:200]

    return payload


def _best_peer(peers, key, higher_is_better):
    vals = [(p["symbol"], _f(p.get(key))) for p in peers]
    vals = [(s, v) for s, v in vals if v is not None]
    if not vals:
        return None
    s, v = (max(vals, key=lambda x: x[1]) if higher_is_better else min(vals, key=lambda x: x[1]))
    return {"symbol": s, "value": round(v, 2)}


_PERSIST_MAP = {
    "sales_5y": "sales_5y", "sales_3y": "sales_3y", "profit_5y": "profit_5y",
    "profit_3y": "profit_3y", "qoq_sales": "qoq_sales", "qoq_profit": "qoq_profit",
    "opm": "opm", "fa_growth": "fa_growth", "roce": "roce", "int_cov": "int_cov",
    "div_yield": "div_yield", "pe": "pe", "upside": "upside",
    "ret_1y": "ret_1y", "ret_3y": "ret_3y", "dma_50": "dma_50", "dma_200": "dma_200",
    "inst_chg": "inst_change",
}


def _persist_detail(symbol: str, benchmark: List[Dict[str, Any]]):
    sets = []
    vals = []
    for b in benchmark:
        col = _PERSIST_MAP.get(b["key"])
        if not col:
            continue
        if b["company"] is not None:
            sets.append(f"{col}_raw = %s"); vals.append(b["company"])
        if b["peer_avg"] is not None:
            sets.append(f"{col}_peer = %s"); vals.append(b["peer_avg"])
        if b["rating"] is not None:
            sets.append(f"{col}_rating = %s"); vals.append(b["rating"])
    if not sets:
        return
    vals.append(symbol)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE gvm_scores SET {', '.join(sets)} WHERE symbol = %s", vals)
        conn.commit()


def _query_single(sql, params):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        if not cur.description:
            return None
        cols = [d[0] for d in cur.description]
        r = cur.fetchone()
        return dict(zip(cols, r)) if r else None


def _query_all(sql, params):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
