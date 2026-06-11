"""
GVM Company Report — peer-benchmark analytics endpoint (Model 2).

Self-contained router. Computes the full company analytics report
(the original Arpit "GVM" peer-benchmarking idea) LIVE from screener_raw:
for every parameter -> company raw value, segment peer average,
rank-in-segment, and a 0-10 rating. BFSI rule applied (D/E + interest
coverage dropped for financial segments). Persists computed detail back
into gvm_scores.*_raw / *_peer / *_rating so data survives for testing.

Endpoints:
  GET /api/gvm/search?q=...          - symbol/company autocomplete
  GET /api/gvm/company/{symbol}      - full report payload (compute + persist)

Path note: included AFTER gvm_market_router so the static /search and
/company/{symbol} routes are matched before the /{symbol} catch-all.
"""

from fastapi import APIRouter, HTTPException
from datetime import datetime, timedelta, date
from typing import Optional, List, Dict, Any
import psycopg
import os

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


# ── BFSI segments: D/E + interest coverage irrelevant ────────────────────────
_BFSI_KEYWORDS = (
    "bank", "nbfc", "finance", "financial", "insurance", "amc",
    "exchange", "capital market", "broking", "wealth", "housing finance",
    "microfinance", "msme finance", "life insurance",
)


def _is_bfsi(segment: str) -> bool:
    s = (segment or "").lower()
    return any(k in s for k in _BFSI_KEYWORDS)


# ── Parameter definitions ────────────────────────────────────────────────────
# key, label, screener_raw column, group, higher_is_better, unit
PARAMS = [
    ("sales_5y",   "Sales 5Y CAGR",        "sales_growth_5y",   "Trackrecord", True,  "%"),
    ("sales_3y",   "Sales 3Y CAGR",        "sales_growth_3y",   "Trackrecord", True,  "%"),
    ("qoq_sales",  "QoQ Sales Growth",     "qoq_sales_growth",  "Trackrecord", True,  "%"),
    ("qoq_profit", "QoQ Profit Growth",    "qoq_profit_growth", "Trackrecord", True,  "%"),
    ("profit_3y",  "Profit 3Y Growth",     "profit_growth_3y",  "Trackrecord", True,  "%"),
    ("profit_5y",  "Profit 5Y Growth",     "profit_growth_5y",  "Trackrecord", True,  "%"),
    ("opm",        "Operating Margin",     "opm",               "Trackrecord", True,  "%"),
    ("roce",       "ROCE",                 "roce",              "Reliability", True,  "%"),
    ("roe",        "Return on Equity",     "Return on equity",  "Reliability", True,  "%"),
    ("pe",         "PE Multiple",          "pe",                "Valuation",   False, "x"),
    ("div_yield",  "Dividend Yield",       "dividend_yield",    "Reliability", True,  "%"),
    ("int_cov",    "Interest Coverage",    "interest_coverage", "Reliability", True,  "x"),
    ("de",         "Debt to Equity",       "Debt to equity",    "Reliability", False, "x"),
    ("promoter",   "Promoter Holding",     "Promoter holding",  "Reliability", True,  "%"),
    ("fii_change", "FII Change",           "fii_change",        "Reliability", True,  "%"),
    ("dii_change", "DII Change",           "dii_change",        "Reliability", True,  "%"),
    ("ret_1y",     "Return 1 Year",        "return_1y",         "Technicals",  True,  "%"),
    ("ret_3y",     "Return 3 Year",        "return_3y",         "Technicals",  True,  "%"),
    ("dma_50",     "Price vs 50 DMA",      "dma_50",            "Technicals",  True,  "%"),
    ("dma_200",    "Price vs 200 DMA",     "dma_200",           "Technicals",  True,  "%"),
    ("ret_52w_idx","52W vs Index",         "return_52w_vs_index","Technicals", True,  "%"),
]

# columns that must be quoted in SQL (have spaces / mixed case)
def _col_sql(col: str) -> str:
    if col == col.lower() and " " not in col:
        return col
    return f'"{col}"'


def _rate(value: Optional[float], peer_values: List[float], higher_is_better: bool) -> Optional[float]:
    """0-10 rating: where the company sits within the peer distribution."""
    if value is None or not peer_values:
        return None
    lo = min(peer_values)
    hi = max(peer_values)
    if hi == lo:
        return 5.0
    pos = (value - lo) / (hi - lo)            # 0..1, higher=better-on-raw-axis
    if not higher_is_better:
        pos = 1.0 - pos
    return round(2.0 + pos * 8.0, 2)          # map to 2..10 band (like original report)


def _rank(value: Optional[float], peer_values: List[float], higher_is_better: bool) -> Optional[int]:
    if value is None or not peer_values:
        return None
    ordered = sorted(peer_values, reverse=higher_is_better)
    # rank = 1-based position of company's value
    for i, v in enumerate(ordered):
        if abs(v - value) < 1e-9:
            return i + 1
    # value not exactly in list (shouldn't happen) -> count better peers
    better = sum(1 for v in peer_values if (v > value if higher_is_better else v < value))
    return better + 1


# ── Search ───────────────────────────────────────────────────────────────────
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


# ── Full company report ──────────────────────────────────────────────────────
@router.get("/api/gvm/company/{symbol}")
def gvm_company_report(symbol: str, persist: bool = True):
    symbol = symbol.upper().strip()

    # 1) headline GVM + content
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

    # 2) full peer set in segment from screener_raw
    sel_cols = ", ".join(
        f"s.{_col_sql(col)} AS {key}" for key, _, col, *_ in PARAMS
    )
    peers = _query_all(f"""
        SELECT g.symbol, g.company_name, g.market_cap, {sel_cols}
        FROM gvm_scores g
        JOIN screener_raw s ON s.nse_code = g.symbol
        WHERE g.segment = %s
        ORDER BY g.market_cap DESC NULLS LAST
    """, (segment,))

    peer_count = len(peers)
    peer_names = [p["symbol"] for p in peers]

    # 3) compute per-parameter benchmark
    benchmark = []
    company_row = next((p for p in peers if p["symbol"] == symbol), None)

    for key, label, col, group, hib, unit in PARAMS:
        # BFSI rule: drop D/E and interest coverage for financials
        if is_bfsi and key in ("de", "int_cov"):
            continue

        peer_vals = [_f(p.get(key)) for p in peers]
        peer_vals = [v for v in peer_vals if v is not None]
        comp_val = _f(company_row.get(key)) if company_row else None
        peer_avg = round(sum(peer_vals) / len(peer_vals), 2) if peer_vals else None

        benchmark.append({
            "key": key,
            "label": label,
            "group": group,
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

    # 4) group-level ratings (Trackrecord / Valuation / Outlook / Reliability / Technicals)
    groups = {}
    for b in benchmark:
        if b["rating"] is None:
            continue
        groups.setdefault(b["group"], []).append(b["rating"])
    group_scores = {g: round(sum(v) / len(v), 2) for g, v in groups.items()}

    # 5) segment rank ladder (by gvm)
    ladder = _query_all("""
        SELECT symbol, company_name, gvm_score, verdict
        FROM gvm_scores WHERE segment=%s ORDER BY gvm_score DESC
    """, (segment,))
    seg_rank = next((i + 1 for i, r in enumerate(ladder) if r["symbol"] == symbol), None)

    # 6) positives / negatives auto-derived
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
             "is_self": r["symbol"] == symbol}
            for r in ladder
        ],
        "peers": peer_names,
        "peer_count": peer_count,
        "positives": [{"label": b["label"], "rating": b["rating"], "company": b["company"],
                       "peer_avg": b["peer_avg"], "unit": b["unit"]} for b in positives],
        "negatives": [{"label": b["label"], "rating": b["rating"], "company": b["company"],
                       "peer_avg": b["peer_avg"], "unit": b["unit"]} for b in negatives],
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

    # 7) persist computed detail into gvm_scores (best-effort, non-fatal)
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


# columns in gvm_scores that map to benchmark keys (raw/peer/rating triplets)
_PERSIST_MAP = {
    "sales_5y": "sales_5y", "sales_3y": "sales_3y", "profit_5y": "profit_5y",
    "profit_3y": "profit_3y", "qoq_sales": "qoq_sales", "qoq_profit": "qoq_profit",
    "opm": "opm", "fa_growth": "fa_growth", "roce": "roce", "int_cov": "int_cov",
    "div_yield": "div_yield", "pe": "pe", "upside": "upside",
    "ret_1y": "ret_1y", "ret_3y": "ret_3y", "dma_50": "dma_50", "dma_200": "dma_200",
    "promoter": "promoter", "inst_change": "inst_change",
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
