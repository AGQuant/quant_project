"""
gvm_company_report.py — Full company analytics report with peer benchmarking.

This is Scorr's ORIGINAL quant idea: rate every company against the average of
its listed peers in the same fine-grained segment (gvm_scores.segment), across
~16 fundamental + technical parameters. Each parameter yields:
    raw    = the company's own value
    peer   = segment peer average
    rank   = company's rank within segment (1 = best)
    rating = 0..10 score (percentile within segment, direction-aware)

Source of truth:
    - gvm_scores.segment  -> peer grouping (fine-grained, e.g. "IT - Large")
    - screener_raw        -> raw fundamentals (joined on nse_code = symbol)
    - get_gvm payload      -> overview / key_takeaway / result_analysis / verdict

The computed detail is ALSO persisted back into the (currently empty) detail
columns of gvm_scores so it survives and can be queried directly tomorrow.

BFSI rule: for Banks / NBFCs / Insurance / AMC / Exchanges, D/E and Interest
Coverage are IRRELEVANT and are dropped from the parameter set.
"""

import logging
from datetime import date
from typing import Optional, Dict, Any, List

log = logging.getLogger("scorr.gvm_report")

# ─── Parameter definitions ────────────────────────────────────────────────────
# Each: (key, label, group, screener_col, higher_is_better, db_prefix, unit)
#   group  : Trackrecord | Valuation | Outlook | Reliability | Technicals
#   db_prefix maps to gvm_scores.<prefix>_raw/_peer/_rating columns (when present)
#   unit   : '%' | 'x' | 'ratio' | '' (for display)
PARAMS = [
    ("sales_5y",   "Sales 5Y CAGR",          "Trackrecord", "sales_growth_5y",        True,  "sales_5y",   "%"),
    ("sales_3y",   "Sales 3Y CAGR",          "Trackrecord", "sales_growth_3y",        True,  "sales_3y",   "%"),
    ("profit_5y",  "Profit 5Y CAGR",         "Trackrecord", "profit_growth_5y",       True,  "profit_5y",  "%"),
    ("profit_3y",  "Profit 3Y CAGR",         "Trackrecord", "profit_growth_3y",       True,  "profit_3y",  "%"),
    ("qoq_sales",  "QoQ Sales Growth",       "Trackrecord", "qoq_sales_growth",       True,  "qoq_sales",  "%"),
    ("qoq_profit", "QoQ Profit Growth",      "Trackrecord", "qoq_profit_growth",      True,  "qoq_profit", "%"),
    ("opm",        "Operating Margin",       "Trackrecord", "opm",                    True,  "opm",        "%"),
    ("opm_exp",    "OPM Expansion",          "Trackrecord", "Operating profit growth",True,  "opm_exp",    "%"),
    ("pe",         "Valuation (PE)",         "Valuation",   "pe",                     False, "pe",         "x"),
    ("div_yield",  "Dividend Yield",         "Reliability", "dividend_yield",         True,  "div_yield",  "%"),
    ("roce",       "Return on Capital (ROCE)","Reliability","roce",                   True,  "roce",       "%"),
    ("int_cov",    "Interest Coverage",      "Reliability", "interest_coverage",      True,  "int_cov",    "x"),
    ("promoter",   "Promoter Holding",       "Reliability", "Unpledged promoter holding", True, "promoter","%"),
    ("inst_change","Change in FII Holding",  "Reliability", "fii_change",             True,  "inst_change","%"),
    ("ret_1y",     "Return over 1 Year",     "Technicals",  "return_1y",              True,  "ret_1y",     "%"),
    ("ret_3y",     "Return over 3 Years",    "Technicals",  "return_3y",              True,  "ret_3y",     "%"),
    ("ret_52w_idx","52W vs Index",           "Technicals",  "return_52w_vs_index",    True,  "ret_52w_idx","%"),
]

# Segments where D/E + Interest Coverage are irrelevant (BFSI rule)
_BFSI_KEYWORDS = ("bank", "nbfc", "finance", "insurance", "amc", "exchange",
                  "capital market", "broking", "wealth", "microfinance",
                  "housing finance", "msme finance", "fintech")

# Group-level rating roll-ups feed these four headline pillar scores
PILLAR_MAP = {
    "Trackrecord": "track_score",
    "Valuation":   "val_score",
    "Outlook":     "outlook_score",
    "Reliability": "reliability_score",
    "Technicals":  "tech_score",
}


def _is_bfsi(segment: str) -> bool:
    s = (segment or "").lower()
    return any(k in s for k in _BFSI_KEYWORDS)


def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _rate_within(values: List[Optional[float]], idx: int, higher_is_better: bool) -> Optional[float]:
    """0..10 rating = percentile of company among non-null segment peers."""
    vals = [(i, v) for i, v in enumerate(values) if v is not None]
    me = values[idx]
    if me is None or len(vals) < 2:
        return None
    ordered = sorted(vals, key=lambda x: x[1], reverse=higher_is_better)
    pos = next(i for i, (oi, _) in enumerate(ordered) if oi == idx)  # 0 = best
    n = len(ordered)
    # best -> 10, worst -> ~2 (keep a floor so worst isn't a flat 0)
    rating = 10.0 - (pos / max(1, n - 1)) * 8.0
    return round(rating, 2)


def _rank_within(values: List[Optional[float]], idx: int, higher_is_better: bool) -> Optional[int]:
    vals = [(i, v) for i, v in enumerate(values) if v is not None]
    if values[idx] is None or not vals:
        return None
    ordered = sorted(vals, key=lambda x: x[1], reverse=higher_is_better)
    return next(i for i, (oi, _) in enumerate(ordered) if oi == idx) + 1


def build_company_report(conn, symbol: str) -> Dict[str, Any]:
    """Compute the full peer-benchmarked report for one company."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return {"error": "symbol required"}

    with conn.cursor() as cur:
        # Resolve company + segment from gvm_scores (latest score_date)
        cur.execute("""
            SELECT symbol, company_name, segment, gvm_score, g_score, v_score, m_score,
                   verdict, punchline, price, market_cap, score_date
            FROM gvm_scores
            WHERE symbol = %s AND score_date = (SELECT MAX(score_date) FROM gvm_scores)
            LIMIT 1
        """, (symbol,))
        row = cur.fetchone()
        if not row:
            return {"error": f"{symbol} not found in gvm_scores"}
        cols = [d[0] for d in cur.description]
        head = dict(zip(cols, row))
        segment = head["segment"]
        bfsi = _is_bfsi(segment)

        # Pull every peer in the same segment + raw fundamentals
        screener_cols = list({p[3] for p in PARAMS})
        col_sql = ", ".join(f's."{c}" AS "{c}"' for c in screener_cols)
        cur.execute(f"""
            SELECT g.symbol, g.company_name, g.gvm_score, g.market_cap, {col_sql}
            FROM gvm_scores g
            JOIN screener_raw s ON s.nse_code = g.symbol
            WHERE g.segment = %s AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
        """, (segment,))
        pcols = [d[0] for d in cur.description]
        peers = [dict(zip(pcols, r)) for r in cur.fetchall()]

        if not any(p["symbol"] == symbol for p in peers):
            # company has gvm row but no screener_raw join — still return header
            peers = [p for p in peers]

        symbols = [p["symbol"] for p in peers]
        try:
            me_idx = symbols.index(symbol)
        except ValueError:
            me_idx = None

        # Build per-parameter benchmark
        params_out = []
        pillar_acc: Dict[str, List[float]] = {}
        persist_vals: Dict[str, Any] = {}

        for key, label, group, scol, hib, prefix, unit in PARAMS:
            if bfsi and key in ("int_cov",):
                continue  # BFSI rule: interest coverage irrelevant

            col_vals = [_f(p.get(scol)) for p in peers]
            non_null = [v for v in col_vals if v is not None]
            peer_avg = round(sum(non_null) / len(non_null), 2) if non_null else None

            raw = rating = rank = None
            best_sym = worst_sym = None
            if me_idx is not None:
                raw = col_vals[me_idx]
                rating = _rate_within(col_vals, me_idx, hib)
                rank = _rank_within(col_vals, me_idx, hib)
            # best / worst peer for the ladder
            if non_null:
                pairs = [(p["symbol"], _f(p.get(scol))) for p in peers if _f(p.get(scol)) is not None]
                pairs.sort(key=lambda x: x[1], reverse=hib)
                best_sym = {"symbol": pairs[0][0], "value": round(pairs[0][1], 2)}
                worst_sym = {"symbol": pairs[-1][0], "value": round(pairs[-1][1], 2)}

            params_out.append({
                "key": key, "label": label, "group": group, "unit": unit,
                "raw": round(raw, 2) if raw is not None else None,
                "peer_avg": peer_avg,
                "rank": rank, "peer_count": len(non_null),
                "rating": rating, "higher_is_better": hib,
                "best": best_sym, "worst": worst_sym,
                "beats_peer": (raw is not None and peer_avg is not None and
                               ((raw >= peer_avg) if hib else (raw <= peer_avg))),
            })

            if rating is not None:
                pillar_acc.setdefault(group, []).append(rating)
                persist_vals[f"{prefix}_raw"] = round(raw, 2) if raw is not None else None
                persist_vals[f"{prefix}_peer"] = peer_avg
                persist_vals[f"{prefix}_rating"] = rating

        # Pillar (headline) scores = avg of param ratings in that group
        pillars = {}
        for group, score_key in PILLAR_MAP.items():
            vals = pillar_acc.get(group, [])
            pillars[score_key] = round(sum(vals) / len(vals), 2) if vals else None

        # Segment rank ladder (all peers by gvm_score)
        ladder = sorted(
            [{"symbol": p["symbol"], "company_name": p["company_name"],
              "gvm": round(_f(p["gvm_score"]) or 0, 2)} for p in peers],
            key=lambda x: x["gvm"], reverse=True
        )
        for i, e in enumerate(ladder):
            e["rank"] = i + 1
        seg_rank = next((e["rank"] for e in ladder if e["symbol"] == symbol), None)

        # Positives / negatives (top-3 strongest & weakest rated params)
        rated = [p for p in params_out if p["rating"] is not None]
        positives = sorted(rated, key=lambda x: x["rating"], reverse=True)[:4]
        negatives = sorted(rated, key=lambda x: x["rating"])[:3]

        # Persist computed detail back into gvm_scores (saved for tomorrow)
        if persist_vals and me_idx is not None:
            try:
                set_sql = ", ".join(f'"{k}" = %s' for k in persist_vals.keys())
                cur.execute(
                    f"UPDATE gvm_scores SET {set_sql} "
                    f"WHERE symbol = %s AND score_date = (SELECT MAX(score_date) FROM gvm_scores)",
                    list(persist_vals.values()) + [symbol]
                )
                conn.commit()
            except Exception as e:
                log.warning(f"persist {symbol} detail failed: {e}")
                conn.rollback()

    return {
        "symbol": symbol,
        "company_name": head["company_name"],
        "segment": segment,
        "is_bfsi": bfsi,
        "gvm_score": round(_f(head["gvm_score"]) or 0, 2),
        "g_score": round(_f(head["g_score"]) or 0, 2),
        "v_score": round(_f(head["v_score"]) or 0, 2),
        "m_score": round(_f(head["m_score"]) or 0, 2),
        "verdict": head["verdict"],
        "punchline": head["punchline"],
        "price": _f(head["price"]),
        "market_cap": _f(head["market_cap"]),
        "score_date": str(head["score_date"]),
        "segment_rank": seg_rank,
        "segment_size": len(ladder),
        "pillars": pillars,
        "parameters": params_out,
        "segment_ladder": ladder,
        "positives": positives,
        "negatives": negatives,
    }


def search_companies(conn, q: str, limit: int = 12) -> List[Dict[str, Any]]:
    """Autocomplete search by symbol or company name."""
    q = (q or "").strip()
    if len(q) < 1:
        return []
    like = f"%{q}%"
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, company_name, segment, gvm_score, verdict, market_cap
            FROM gvm_scores
            WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)
              AND (symbol ILIKE %s OR company_name ILIKE %s)
            ORDER BY
              CASE WHEN symbol ILIKE %s THEN 0 ELSE 1 END,
              market_cap DESC NULLS LAST
            LIMIT %s
        """, (like, like, f"{q}%", limit))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
