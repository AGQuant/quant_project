"""
gvm_report_endpoints.py — GVM company report + search endpoints.

v2.9.32: wired into main.py as gvm_report_router.

Endpoints:
  GET /api/gvm/company/{symbol}  — full peer-benchmarked company report with extras
  GET /api/gvm/search            — autocomplete search by symbol or company name
"""

from fastapi import APIRouter, HTTPException
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import psycopg
import os
import logging

from gvm_company_report import build_company_report, search_companies
from gvm_page_extras import build_page_extras

log = logging.getLogger("scorr.gvm_report")
router = APIRouter(tags=["gvm_report"])

_GROUP_TO_PILLAR: Dict[str, str] = {
    "Trackrecord": "G",
    "Valuation":   "V",
    "Outlook":     "V",
    "Reliability": "G",
    "Technicals":  "M",
}


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _ist_now() -> str:
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M:%S IST")


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _transform_param(p: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a PARAMS item from build_company_report into benchmark format."""
    group = p.get("group", "")
    return {
        "key":        p.get("key"),
        "label":      p.get("label"),
        "group":      group,
        "pillar":     _GROUP_TO_PILLAR.get(group, "_"),
        "unit":       p.get("unit", ""),
        "company":    p.get("raw"),       # raw = company's own value
        "peer_avg":   p.get("peer_avg"),
        "rating":     p.get("rating"),
        "rank":       p.get("rank"),
        "peer_n":     p.get("peer_count"),
        "best":       p.get("best"),
        "worst":      p.get("worst"),
        "beats_peer": p.get("beats_peer"),
    }


def _peer_block(key: str, label: str, unit: str, value_map: Dict[str, float],
                sym: str, lower_is_better: bool) -> Optional[Dict[str, Any]]:
    """Build a self-contained, peer-benchmarked Valuation block from a
    {symbol: value} map. Mirrors the Forward PE rating logic. pillar=V.
    Returns None if fewer than 2 peers have a value."""
    pairs = [(s, v) for s, v in value_map.items() if v is not None]
    if len(pairs) < 2:
        return None
    vals     = [v for _, v in pairs]
    peer_avg = round(sum(vals) / len(vals), 2)
    ordered  = sorted(pairs, key=lambda x: x[1], reverse=not lower_is_better)  # best first
    best     = {"symbol": ordered[0][0],  "value": round(ordered[0][1], 2)}
    worst    = {"symbol": ordered[-1][0], "value": round(ordered[-1][1], 2)}
    n        = len(ordered)
    sym_val  = value_map.get(sym)
    rank = rating = None
    beats = False
    if sym_val is not None:
        pos    = next((i for i, (s_i, _) in enumerate(ordered) if s_i == sym), 0)
        rank   = pos + 1
        rating = round(10.0 - (pos / max(1, n - 1)) * 8.0, 2)
        beats  = (sym_val <= peer_avg) if lower_is_better else (sym_val >= peer_avg)
    return {
        "key":        key,
        "label":      label,
        "group":      "Valuation",
        "pillar":     "V",
        "unit":       unit,
        "company":    round(sym_val, 2) if sym_val is not None else None,
        "peer_avg":   peer_avg,
        "rating":     rating,
        "rank":       rank,
        "peer_n":     n,
        "best":       best,
        "worst":      worst,
        "beats_peer": beats,
    }


def _insert_after_valuation(benchmark: List[Dict[str, Any]], block: Dict[str, Any]) -> None:
    """Insert a block right after the last existing Valuation-pillar row so the
    V section stays contiguous; else append."""
    last_v = -1
    for i, b in enumerate(benchmark):
        if b.get("pillar") == "V":
            last_v = i
    if last_v >= 0:
        benchmark.insert(last_v + 1, block)
    else:
        benchmark.append(block)


@router.get("/api/gvm/company/{symbol}")
def gvm_company_report(symbol: str):
    """Full peer-benchmarked GVM analytics report for one company."""
    sym = symbol.strip().upper()

    # --- 1. Core report (parameters, positives, negatives, ladder) ---
    with _conn() as conn:
        base = build_company_report(conn, sym)

    if "error" in base:
        raise HTTPException(404, base["error"])

    segment      = base.get("segment")
    ladder_syms  = [row["symbol"] for row in (base.get("segment_ladder") or [])]

    # --- 2. Extras (trend, volume, pivot, segment_ctx, etc.) ---
    page         = build_page_extras(sym, ladder_syms, segment=segment)
    extras       = page.get("extras", {})
    ladder_extra = page.get("ladder_extra", {})

    # --- 3. Benchmark list ---
    benchmark = [_transform_param(p) for p in (base.get("parameters") or [])]

    # --- 4. Forward PE — peer benchmark (computed from pe + fy27_growth) ---
    try:
        if ladder_syms:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    SELECT s.nse_code, s.pe, i.fy27_growth
                    FROM screener_raw s
                    LEFT JOIN input_raw i ON i.nse_code = s.nse_code
                    WHERE s.nse_code = ANY(%s)
                """, (ladder_syms,))
                fwd_pe_map: Dict[str, float] = {}
                for nse_code, pe_v, fy27_v in cur.fetchall():
                    pe_f, fy27_f = _f(pe_v), _f(fy27_v)
                    if pe_f is not None and fy27_f is not None and fy27_f > -100:
                        fwd_pe_map[nse_code] = round(pe_f / (1 + fy27_f / 100), 1)

            fwd_pairs = [(s, v) for s, v in fwd_pe_map.items() if v is not None]
            if len(fwd_pairs) >= 2:
                vals            = [v for _, v in fwd_pairs]
                peer_avg_fwd    = round(sum(vals) / len(vals), 1)
                sym_fwd         = fwd_pe_map.get(sym)
                sorted_asc      = sorted(fwd_pairs, key=lambda x: x[1])   # lower = better
                best_fwd        = {"symbol": sorted_asc[0][0],  "value": sorted_asc[0][1]}
                worst_fwd       = {"symbol": sorted_asc[-1][0], "value": sorted_asc[-1][1]}
                rank_fwd        = next((i + 1 for i, (s_i, _) in enumerate(sorted_asc) if s_i == sym), None)
                n               = len(sorted_asc)
                rating_fwd      = None
                if sym_fwd is not None:
                    pos         = next((i for i, (s_i, _) in enumerate(sorted_asc) if s_i == sym), 0)
                    rating_fwd  = round(10.0 - (pos / max(1, n - 1)) * 8.0, 2)

                fwd_pe_bench = {
                    "key":        "fwd_pe",
                    "label":      "Forward PE",
                    "group":      "Valuation",
                    "pillar":     "V",
                    "unit":       "x",
                    "company":    sym_fwd,
                    "peer_avg":   peer_avg_fwd,
                    "rating":     rating_fwd,
                    "rank":       rank_fwd,
                    "peer_n":     n,
                    "best":       best_fwd,
                    "worst":      worst_fwd,
                    "beats_peer": (sym_fwd is not None and sym_fwd <= peer_avg_fwd),
                }
                # Insert right after PE
                pe_idx = next((i for i, b in enumerate(benchmark) if b.get("key") == "pe"), -1)
                if pe_idx >= 0:
                    benchmark.insert(pe_idx + 1, fwd_pe_bench)
                else:
                    benchmark.append(fwd_pe_bench)
    except Exception as e:
        log.warning(f"forward_pe benchmark failed for {sym}: {e}")

    # --- 4b. PB + EV/EBITDA + Annual Upside (FY27e) — peer-benchmarked V blocks ---
    try:
        if ladder_syms:
            pb_map: Dict[str, float] = {}
            ev_map: Dict[str, float] = {}
            ups_map: Dict[str, float] = {}
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    SELECT nse_code, "Price to book value", "EVEBITDA"
                    FROM screener_raw
                    WHERE nse_code = ANY(%s)
                """, (ladder_syms,))
                for code, pb_v, ev_v in cur.fetchall():
                    pb_f, ev_f = _f(pb_v), _f(ev_v)
                    if pb_f is not None and pb_f > 0:
                        pb_map[code] = pb_f
                    if ev_f is not None and ev_f > 0:
                        ev_map[code] = ev_f
                cur.execute("""
                    SELECT nse_code, fy27_growth
                    FROM input_raw
                    WHERE nse_code = ANY(%s)
                """, (ladder_syms,))
                for code, ups_v in cur.fetchall():
                    ups_f = _f(ups_v)
                    if ups_f is not None and ups_f > -100:
                        ups_map[code] = ups_f

            pb_block  = _peer_block("pb", "Price / Book", "x", pb_map, sym, lower_is_better=True)
            ev_block  = _peer_block("ev_ebitda", "EV / EBITDA", "x", ev_map, sym, lower_is_better=True)
            ups_block = _peer_block("annual_upside", "Annual Upside (FY27e)", "%", ups_map, sym, lower_is_better=False)
            for blk in (pb_block, ev_block, ups_block):
                if blk:
                    _insert_after_valuation(benchmark, blk)
    except Exception as e:
        log.warning(f"valuation extras benchmark failed for {sym}: {e}")

    # --- 5. Positives / negatives (add company alias + pillar) ---
    positives = [_transform_param(p) for p in (base.get("positives") or [])]
    negatives = [_transform_param(p) for p in (base.get("negatives") or [])]

    # --- 6. Ladder — enrich with extras, add verdict ---
    verdict_map: Dict[str, str] = {}
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, verdict
                FROM gvm_scores
                WHERE symbol = ANY(%s)
                  AND score_date = (SELECT MAX(score_date) FROM gvm_scores)
            """, (ladder_syms,))
            verdict_map = {s: v for s, v in cur.fetchall()}
    except Exception as e:
        log.warning(f"verdict fetch failed for {sym}: {e}")

    ladder: List[Dict[str, Any]] = []
    for row in (base.get("segment_ladder") or []):
        s      = row["symbol"]
        merged = {
            "rank":         row.get("rank"),
            "symbol":       s,
            "company_name": row.get("company_name"),
            "gvm":          row.get("gvm"),
            "verdict":      verdict_map.get(s),
            "is_self":      s == sym,
        }
        le = ladder_extra.get(s, {})
        merged.update(le)
        merged["is_self"] = s == sym   # restore after update
        ladder.append(merged)

    # --- 7. Content + mcap_rank + cap_category ---
    content: Dict[str, Any] = {}
    mcap_rank    = None
    cap_category = None
    persist_error = None

    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT overview, key_takeaway, result_analysis,
                       last_overview_updated::text,
                       last_takeaway_updated::text,
                       last_result_analysis_updated::text,
                       mcap_rank, cap_category
                FROM input_raw
                WHERE nse_code = %s
                LIMIT 1
            """, (sym,))
            row = cur.fetchone()
            if row:
                content = {
                    "overview":                   row[0],
                    "key_takeaway":               row[1],
                    "result_analysis":            row[2],
                    "last_overview_updated":      row[3],
                    "last_takeaway_updated":      row[4],
                    "last_result_analysis_updated": row[5],
                }
                mcap_rank    = row[6]
                cap_category = row[7]
    except Exception as e:
        log.warning(f"content fetch failed for {sym}: {e}")
        persist_error = str(e)

    # --- 8. Assemble final response ---
    return {
        "symbol":        sym,
        "company_name":  base.get("company_name"),
        "segment":       segment,
        "is_bfsi":       base.get("is_bfsi", False),
        "verdict":       base.get("verdict"),
        "punchline":     base.get("punchline"),
        "price":         base.get("price"),
        "market_cap":    base.get("market_cap"),
        "score_date":    base.get("score_date"),
        "mcap_rank":     mcap_rank,
        "cap_category":  cap_category,
        "segment_rank":  base.get("segment_rank"),
        "segment_total": base.get("segment_size"),
        "peer_count":    base.get("segment_size"),
        "scores": {
            "gvm": base.get("gvm_score"),
            "g":   base.get("g_score"),
            "v":   base.get("v_score"),
            "m":   base.get("m_score"),
        },
        "benchmark":    benchmark,
        "positives":    positives,
        "negatives":    negatives,
        "ladder":       ladder,
        "extras":       extras,
        "content":      content,
        "generated_at": _ist_now(),
        "persisted":    persist_error is None,
        "persist_error": persist_error,
    }


@router.get("/api/gvm/search")
def gvm_search(q: str = "", limit: int = 8):
    """Autocomplete: search companies by symbol or company name."""
    lim = min(max(int(limit), 1), 50)
    with _conn() as conn:
        results = search_companies(conn, q, limit=lim)
    return {"q": q, "results": results}
