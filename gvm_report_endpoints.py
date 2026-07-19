"""
gvm_report_endpoints.py — GVM company report + search endpoints.

v2.9.32: wired into main.py as gvm_report_router.

Endpoints:
  GET /api/gvm/company/{symbol}  — full peer-benchmarked company report with extras
  GET /api/gvm/search            — autocomplete search by symbol or company name
"""

from fastapi import APIRouter, HTTPException
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, date
from decimal import Decimal
import math
import statistics
import psycopg
import os
import logging

from gvm_company_report import build_company_report, search_companies, build_financials_block, build_ops_block
from gvm_page_extras import build_page_extras
from gvm_engine import param_score, score_relative_inverse, score_peg

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
        f = float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
    if f is not None and not math.isfinite(f):   # NaN / Inf -> None
        return None
    return f


def _json_safe(o):
    """Recursively replace non-finite floats (NaN/Inf) with None.

    Starlette's JSONResponse serializes with allow_nan=False, so a single
    NaN/Inf anywhere in the payload raises ValueError -> 500 AFTER the endpoint
    returns (uncatchable by in-handler try/except). This neutralizes them.
    """
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, Decimal):
        return float(o) if o.is_finite() else None
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o


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
        "peer_median": p.get("peer_median", p.get("peer_avg")),
        "rating":     p.get("rating"),
        "rank":       p.get("rank"),
        "peer_n":     p.get("peer_count"),
        "best":       p.get("best"),
        "worst":      p.get("worst"),
        "peers":      p.get("peers", []),          # cc#507: peer-ladder chart data
        "extra_marker": p.get("extra_marker"),     # cc#507: e.g. PE row's "own 5y avg" line
        "beats_peer": p.get("beats_peer"),
    }


def _peer_block(key: str, label: str, unit: str, value_map: Dict[str, float],
                sym: str, lower_is_better: bool, rate_fn=None) -> Optional[Dict[str, Any]]:
    """Build a self-contained, peer-benchmarked Valuation block from a
    {symbol: value} map. pillar=V. Returns None if fewer than 2 peers have a value.

    cc#506: rating source switched from percentile-rank to gvm_engine, same as every other
    report parameter -- score_relative_inverse (lower-is-better: Price/Book, EV/EBITDA) or
    param_score (higher-is-better: Annual Upside) by default, fed the segment MEDIAN. Forward PE
    has its own inline block below (needs a peer map built from a derived value, not a straight
    screener_raw column) but uses the same engine functions for "ONE rating methodology
    everywhere" (cc#506 spec).
    cc#507: rate_fn lets a caller override the default rating function (PEG uses score_peg,
    its own custom absolute bands + the shared inverse-relative half). Also emits "peers" --
    the full sorted {symbol,value} list -- for the per-metric peer-ladder chart."""
    pairs = [(s, v) for s, v in value_map.items() if v is not None]
    if len(pairs) < 2:
        return None
    vals        = [v for _, v in pairs]
    peer_median = round(statistics.median(vals), 2)
    ordered     = sorted(pairs, key=lambda x: x[1], reverse=not lower_is_better)  # best first
    best        = {"symbol": ordered[0][0],  "value": round(ordered[0][1], 2)}
    worst       = {"symbol": ordered[-1][0], "value": round(ordered[-1][1], 2)}
    n           = len(ordered)
    sym_val     = value_map.get(sym)
    rank = rating = None
    beats = False
    if sym_val is not None:
        pos    = next((i for i, (s_i, _) in enumerate(ordered) if s_i == sym), 0)
        rank   = pos + 1
        if rate_fn is not None:
            rating = rate_fn(sym_val, peer_median)
        else:
            rating = (score_relative_inverse(sym_val, peer_median) if lower_is_better
                      else param_score(sym_val, peer_median))
        beats  = (sym_val <= peer_median) if lower_is_better else (sym_val >= peer_median)
    return {
        "key":        key,
        "label":      label,
        "group":      "Valuation",
        "pillar":     "V",
        "unit":       unit,
        "company":    round(sym_val, 2) if sym_val is not None else None,
        "peer_avg":   peer_median,
        "peer_median": peer_median,
        "rating":     rating,
        "rank":       rank,
        "peer_n":     n,
        "best":       best,
        "worst":      worst,
        "peers":      [{"symbol": s, "value": round(v, 2)} for s, v in ordered],
        "beats_peer": beats,
    }


_PE_VERDICT_BANDS = (
    (1.5, "Expensive", "red"),
    (1.0, "Reasonable", "amber"),
)   # ratio < 1.0 falls through to Cheap/green

def _pe_verdict(ratio: Optional[float]) -> Optional[Dict[str, str]]:
    """cc#512: current_pe/historical_pe ratio -> founder-locked verdict band.
    >1.5 Expensive (red) | 1.0-1.5 Reasonable (amber) | <1.0 Cheap (green)."""
    if ratio is None:
        return None
    for bound, label, color in _PE_VERDICT_BANDS:
        if ratio > bound:
            return {"label": label, "color": color}
    return {"label": "Cheap", "color": "green"}


def _pe_trend(cur, sym: str) -> List[Dict[str, Any]]:
    """cc#521: quarterly TTM PE trend (upgrades cc#512's 5-pt annual line to a Trendlyne-style
    quarterly view). EPS from fundamentals_history (section=quarters, period_type=quarter,
    "EPS in Rs"); TTM EPS at quarter i = sum of quarters [i-3..i] (needs 4 consecutive quarters,
    so the first 3 stored quarters never produce a point). Price = nearest raw_prices close
    on/before each quarter's period_end (same EOD-snapshot rule as the retired annual version).
    Skips points where TTM EPS<=0 (never plots a negative PE -- renders as a gap, matching the
    cc#512 negative-EPS convention). Depth auto-extends with whatever fundamentals_history has
    stored (~13 quarters today -> ~9-10 plottable points); no hardcoded count.
    Flags any point >3x the series median as a likely data artifact (hollow point client-side)."""
    try:
        cur.execute("""
            SELECT period_end, metrics->>'EPS in Rs' AS eps, consolidated
            FROM fundamentals_history
            WHERE symbol=%s AND section='quarters' AND period_type='quarter'
            ORDER BY period_end ASC, consolidated DESC
        """, (sym,))
        by_period: Dict[Any, Any] = {}
        for period_end, eps, consolidated in cur.fetchall():
            if period_end not in by_period:   # first hit per period wins -- consolidated sorts first
                by_period[period_end] = eps

        periods = sorted(by_period.keys())
        eps_f = [(_f(str(by_period[p]).replace(",", "")) if by_period[p] is not None else None)
                 for p in periods]

        points = []
        for i in range(3, len(periods)):
            window = eps_f[i - 3:i + 1]
            if any(v is None for v in window):
                continue   # incomplete trailing-4 window -- skip, not fabricate
            ttm_eps = sum(window)
            if ttm_eps <= 0:
                continue   # skip TTM-loss quarters -- never plot a negative PE
            period_end = periods[i]
            cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND price_date<=%s
                           ORDER BY price_date DESC LIMIT 1""", (sym, period_end))
            r = cur.fetchone()
            close = _f(r[0]) if r else None
            if close is None:
                continue
            points.append({"q": period_end.strftime("%b %y"), "period_end": str(period_end),
                            "ttm_eps": round(ttm_eps, 2), "close": close, "pe": round(close / ttm_eps, 2)})

        if points:
            med = statistics.median([p["pe"] for p in points])
            for p in points:
                p["is_artifact"] = bool(med > 0 and p["pe"] > 3 * med)
        return points
    except Exception as e:
        log.warning(f"_pe_trend failed for {sym}: {e}")
        return []


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


def _minimal_base(conn, sym: str) -> Optional[Dict[str, Any]]:
    """Degraded fallback when full report computation crashes — header only,
    so the page still renders instead of a blank 500."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT company_name, segment, gvm_score, g_score, v_score, m_score,
                   verdict, punchline, price, market_cap, score_date
            FROM gvm_scores
            WHERE symbol = %s AND score_date = (SELECT MAX(score_date) FROM gvm_scores)
            LIMIT 1
        """, (sym,))
        r = cur.fetchone()
    if not r:
        return None
    return {
        "company_name": r[0], "segment": r[1],
        "gvm_score": _f(r[2]), "g_score": _f(r[3]), "v_score": _f(r[4]), "m_score": _f(r[5]),
        "verdict": r[6], "punchline": r[7], "price": _f(r[8]), "market_cap": _f(r[9]),
        "score_date": str(r[10]) if r[10] is not None else None,
        "parameters": [], "positives": [], "negatives": [], "segment_ladder": [],
        "segment_rank": None, "segment_size": 0, "is_bfsi": False, "degraded": True,
    }


@router.get("/api/gvm/company/{symbol}")
def gvm_company_report(symbol: str):
    """Full peer-benchmarked GVM analytics report for one company."""
    sym = symbol.strip().upper()

    # --- 1. Core report (parameters, positives, negatives, ladder) ---
    # Crash-isolated: if full computation fails, serve a degraded header so the
    # page loads (not a blank 500), and log the traceback for root-cause fix.
    try:
        with _conn() as conn:
            base = build_company_report(conn, sym)
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"build_company_report crashed for {sym}: {e}", exc_info=True)
        with _conn() as conn:
            base = _minimal_base(conn, sym)
        if base is None:
            raise HTTPException(404, f"{sym} not found in gvm_scores")

    if "error" in base:
        raise HTTPException(404, base["error"])

    segment      = base.get("segment")
    ladder_syms  = [row["symbol"] for row in (base.get("segment_ladder") or [])]

    # --- 2. Extras (trend, volume, pivot, segment_ctx, etc.) ---
    try:
        page = build_page_extras(sym, ladder_syms, segment=segment)
    except Exception as e:
        log.error(f"build_page_extras crashed for {sym}: {e}", exc_info=True)
        page = {"extras": {}, "ladder_extra": {}}
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
                peer_median_fwd = round(statistics.median(vals), 1)
                sym_fwd         = fwd_pe_map.get(sym)
                sorted_asc      = sorted(fwd_pairs, key=lambda x: x[1])   # lower = better
                best_fwd        = {"symbol": sorted_asc[0][0],  "value": sorted_asc[0][1]}
                worst_fwd       = {"symbol": sorted_asc[-1][0], "value": sorted_asc[-1][1]}
                rank_fwd        = next((i + 1 for i, (s_i, _) in enumerate(sorted_asc) if s_i == sym), None)
                n               = len(sorted_asc)
                # cc#506: rating source = gvm_engine.score_relative_inverse (lower-is-better,
                # segment median), same "ONE rating methodology everywhere" as the rest of the
                # report -- retires the old percentile-rank formula.
                rating_fwd      = score_relative_inverse(sym_fwd, peer_median_fwd) if sym_fwd is not None else None

                fwd_pe_bench = {
                    "key":        "fwd_pe",
                    "label":      "Forward PE",
                    "group":      "Valuation",
                    "pillar":     "V",
                    "unit":       "x",
                    "company":    sym_fwd,
                    "peer_avg":   peer_median_fwd,
                    "peer_median": peer_median_fwd,
                    "rating":     rating_fwd,
                    "rank":       rank_fwd,
                    "peer_n":     n,
                    "best":       best_fwd,
                    "worst":      worst_fwd,
                    "peers":      [{"symbol": s, "value": v} for s, v in sorted_asc],
                    "beats_peer": (sym_fwd is not None and sym_fwd <= peer_median_fwd),
                }
                # Insert right after PE
                pe_idx = next((i for i, b in enumerate(benchmark) if b.get("key") == "pe"), -1)
                if pe_idx >= 0:
                    benchmark.insert(pe_idx + 1, fwd_pe_bench)
                else:
                    benchmark.append(fwd_pe_bench)
    except Exception as e:
        log.warning(f"forward_pe benchmark failed for {sym}: {e}")

    # --- 4b. Historical PE (5Y avg) — context row, NOT scored (cc#507/512). Does NOT enter the V
    # pillar average -- pillars were already computed inside build_company_report() before this
    # block ever touches `benchmark`. cc#512: the comparison column is the COMPANY'S OWN CURRENT
    # PE (not the segment median of historical_pe, which was low-relevance) -- "peer_avg" here
    # means "current PE" for this row only, relabeled client-side. RANK/best/worst still rank on
    # re-rating % vs peers (most de-rated = best), unchanged from cc#507. A verdict chip
    # (Cheap/Reasonable/Expensive, ratio = current_pe/historical_pe) replaces the plain re-rating
    # chip, and a quarterly TTM PE trend chart (pe_trend, cc#521 dual-axis upgrade of the cc#512
    # 5-pt annual line) replaces the peer-ladder chart for this row only.
    try:
        if ladder_syms:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    SELECT nse_code, pe, historical_pe FROM screener_raw
                    WHERE nse_code = ANY(%s)
                """, (ladder_syms,))
                hist_map: Dict[str, float] = {}
                pe_map: Dict[str, float] = {}
                rerating_map: Dict[str, float] = {}
                for code, pe_v, hpe_v in cur.fetchall():
                    pe_f, hpe_f = _f(pe_v), _f(hpe_v)
                    if pe_f is not None:
                        pe_map[code] = pe_f
                    if hpe_f is not None and hpe_f > 0:
                        hist_map[code] = hpe_f
                        if pe_f is not None:
                            rerating_map[code] = round((pe_f / hpe_f - 1) * 100, 1)

            hist_pairs = [(s, v) for s, v in hist_map.items()]
            if len(hist_pairs) >= 2:
                sym_hist         = hist_map.get(sym)
                sym_current_pe   = pe_map.get(sym)
                sym_rerating     = rerating_map.get(sym)
                sym_ratio        = (sym_current_pe / sym_hist) if (sym_current_pe is not None and sym_hist) else None

                # cc#522: peer column + rank/best/worst switched to the SAME basis as every other
                # V row -- peers' own 5Y-avg historical PE (hist_pairs, lower = cheaper = better),
                # not the company's own current PE (that was a table-grammar bug: every other row
                # reads Company | Segment-peer, this one was reading Company | own current PE) and
                # not the re-rating % (that story now lives only in the verdict chip). hist_pairs
                # already excludes peers lacking a usable hist PE (hpe_f>0 filter above), so the
                # peer_n shown here is honest about how many peers actually have 5Y hist PE data.
                peer_median_hist = round(statistics.median([v for _, v in hist_pairs]), 2)
                sorted_hist = sorted(hist_pairs, key=lambda x: x[1])   # lower hist PE = "best" (cheaper peer)
                best_h  = {"symbol": sorted_hist[0][0],  "value": round(sorted_hist[0][1], 2)}
                worst_h = {"symbol": sorted_hist[-1][0], "value": round(sorted_hist[-1][1], 2)}
                rank_h  = (next((i + 1 for i, (s_i, _) in enumerate(sorted_hist) if s_i == sym), None)
                           if sym_hist is not None else None)

                pe_trend = []
                try:
                    with _conn() as _ptc, _ptc.cursor() as _ptcur:
                        pe_trend = _pe_trend(_ptcur, sym)
                except Exception as _pte:
                    log.warning(f"pe_trend fetch failed for {sym}: {_pte}")
                median_pe = round(statistics.median([p["pe"] for p in pe_trend]), 2) if pe_trend else None

                hist_pe_bench = {
                    "key":        "hist_pe",
                    "label":      "Historical PE (5Y avg)",
                    "group":      "Valuation",
                    "pillar":     "V",
                    "unit":       "x",
                    "company":    round(sym_hist, 2) if sym_hist is not None else None,
                    "peer_avg":   peer_median_hist,
                    "peer_median": peer_median_hist,
                    "peer_label": "peer median",   # cc#522: fixed -- was showing the company's own current PE
                    "rating":     None,      # context row -- see docstring above
                    "is_context": True,
                    "rerating_pct": sym_rerating,
                    "verdict":    _pe_verdict(sym_ratio),
                    "rank":       rank_h,
                    "peer_n":     len(hist_pairs),
                    "best":       best_h,
                    "worst":      worst_h,
                    "peers":      [{"symbol": s, "value": round(v, 2)} for s, v in sorted_hist],
                    "chart_type": "pe_trend",
                    "pe_trend":   pe_trend,
                    "median_pe":  median_pe,
                    "current_pe": round(sym_current_pe, 2) if sym_current_pe is not None else None,
                    "historical_pe": round(sym_hist, 2) if sym_hist is not None else None,
                    "beats_peer": (sym_hist is not None and sym_hist <= peer_median_hist),
                }
                anchor_key = "fwd_pe" if any(b.get("key") == "fwd_pe" for b in benchmark) else "pe"
                anchor_idx = next((i for i, b in enumerate(benchmark) if b.get("key") == anchor_key), -1)
                if anchor_idx >= 0:
                    benchmark.insert(anchor_idx + 1, hist_pe_bench)
                else:
                    benchmark.append(hist_pe_bench)
    except Exception as e:
        log.warning(f"historical_pe benchmark failed for {sym}: {e}")

    # --- 4c. PB + EV/EBITDA + PEG + Annual Upside (FY27e) — peer-benchmarked V blocks.
    # Inserted in THIS order (each _insert_after_valuation lands right after the previous one,
    # so calling them in sequence fixes the final row order per cc#507 spec item 4:
    # PE -> Forward PE -> Historical PE -> Price/Book -> EV/EBITDA -> PEG -> Annual Upside).
    try:
        if ladder_syms:
            pb_map: Dict[str, float] = {}
            ev_map: Dict[str, float] = {}
            peg_map: Dict[str, float] = {}
            ups_map: Dict[str, float] = {}
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    SELECT nse_code, "Price to book value", "EVEBITDA", "PEG Ratio"
                    FROM screener_raw
                    WHERE nse_code = ANY(%s)
                """, (ladder_syms,))
                for code, pb_v, ev_v, peg_v in cur.fetchall():
                    pb_f, ev_f, peg_f = _f(pb_v), _f(ev_v), _f(peg_v)
                    if pb_f is not None and pb_f > 0:
                        pb_map[code] = pb_f
                    if ev_f is not None and ev_f > 0:
                        ev_map[code] = ev_f
                    if peg_f is not None:
                        peg_map[code] = peg_f   # cc#507: PEG can be legitimately negative (loss-making) -- keep
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
            peg_block = _peer_block("peg", "PEG Ratio", "x", peg_map, sym, lower_is_better=True, rate_fn=score_peg)
            ups_block = _peer_block("annual_upside", "Annual Upside (FY27e)", "%", ups_map, sym, lower_is_better=False)
            for blk in (pb_block, ev_block, peg_block, ups_block):
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
    # cc#450: header MCap value. gvm_scores/input_raw carry the score-snapshot market_cap (in Cr) that
    # mcap_rank/cap_category were ranked on; screener_raw holds the freshest fundamentals scrape (the
    # founder-verified "truth"). Prefer screener_raw for the displayed value, fall back to the snapshot.
    market_cap_disp = base.get("market_cap")

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
            # cc#450: freshest market_cap (Cr) from screener_raw for the header display
            cur.execute("SELECT market_cap FROM screener_raw WHERE nse_code = %s LIMIT 1", (sym,))
            _mc = cur.fetchone()
            if _mc and _mc[0] is not None:
                market_cap_disp = _f(_mc[0])
    except Exception as e:
        log.warning(f"content fetch failed for {sym}: {e}")
        persist_error = str(e)

    # --- 7b. cc#518: FINANCIALS section (screener.in-style tables), one SQL pass, own file ---
    try:
        with _conn() as conn:
            financials = build_financials_block(conn, sym)
    except Exception as e:
        log.warning(f"financials block failed for {sym}: {e}")
        financials = {"basis": None, "quarterly": None, "profit_loss": None,
                      "balance_sheet": None, "cash_flow": None, "ratios": None, "shareholding": None}

    # --- 7c. cc#541: OPERATIONAL METRICS section (per-sector KPIs from concalls/decks) ---
    try:
        with _conn() as conn:
            operational_metrics = build_ops_block(conn, sym)
    except Exception as e:
        log.warning(f"ops metrics block failed for {sym}: {e}")
        operational_metrics = {"has_data": False, "sector": None, "periods": [], "rows": [], "concall": None}

    # cc#343: unify the card price with Fibcheck via the ONE shared resolver — FEED symbols show
    # live CMP, NON-FEED symbols the latest COMPLETED close (Prev Close, never a partial row), so
    # the card, fibcheck and pivot-range dot can never disagree again (RAMCOIND 362.65 vs 336.1).
    try:
        import price_resolver
        with _conn() as _pc, _pc.cursor() as _pcur:
            _pr = price_resolver.resolve_price(_pcur, sym)
    except Exception as _pe:
        log.warning(f"price resolve failed for {sym}: {_pe}")
        _pr = {"price": base.get("price"), "label": "CMP", "date": None, "is_live": True}
    _resolved_price = _pr.get("price") if _pr.get("price") is not None else base.get("price")

    # --- 8. Assemble final response (NaN/Inf-scrubbed for JSON safety) ---
    return _json_safe({
        "symbol":        sym,
        "company_name":  base.get("company_name"),
        "segment":       segment,
        "is_bfsi":       base.get("is_bfsi", False),
        "verdict":       base.get("verdict"),
        "punchline":     base.get("punchline"),
        "price":         _resolved_price,
        "price_label":   _pr.get("label"),
        "price_date":    _pr.get("date"),
        "price_is_live": _pr.get("is_live"),
        "market_cap":    market_cap_disp,   # cc#450: fresh screener_raw value (Cr), snapshot fallback
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
        "financials":   financials,   # cc#518: screener.in-style FINANCIALS section
        "operational_metrics": operational_metrics,   # cc#541: per-sector ops KPIs (concall-extracted)
        "generated_at": _ist_now(),
        "persisted":    persist_error is None,
        "persist_error": persist_error,
        "degraded":     base.get("degraded", False),
    })


@router.get("/api/gvm/search")
def gvm_search(q: str = "", limit: int = 8):
    """Autocomplete: search companies by symbol or company name."""
    lim = min(max(int(limit), 1), 50)
    with _conn() as conn:
        results = search_companies(conn, q, limit=lim)
    return {"q": q, "results": results}
