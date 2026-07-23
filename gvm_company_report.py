"""
gvm_company_report.py — Full company analytics report with peer benchmarking.

This is Scorr's ORIGINAL quant idea: rate every company against its listed peers in the same
fine-grained segment (gvm_scores.segment), across ~16 fundamental + technical parameters. Each
parameter yields:
    raw    = the company's own value
    peer   = segment peer MEDIAN (key: peer_avg for back-compat + peer_median)
    rank   = company's rank within segment (1 = best) -- positional, informational only
    rating = 0..10 score, computed by the SAME gvm_engine.py functions the nightly G/V/M engine
             uses (param_score / score_pe / score_opm_expansion / score_inst_holding_abs /
             score_interest_coverage / score_dma), fed the segment MEDIAN as peer input.

cc#506 (18-Jul-2026, founder-locked): retired the old percentile-rank rating (_rate_within,
worst->~2/best->10 positional score) -- it measured "beat how many peers" rather than "how good
is this number", and drifted from the nightly engine's own G/V/M inputs. Rating is now identical
in philosophy AND arithmetic to the nightly engine: absolute-value bands blended with a
peer-median-relative band. Report ratings and nightly engine pillar inputs are the SAME numbers
by construction (same functions, same median).

Source of truth:
    - gvm_scores.segment  -> peer grouping (fine-grained, e.g. "IT - Large")
    - screener_raw        -> raw fundamentals (joined on nse_code = symbol) -- ALL fundamental +
      valuation parameters compute EXCLUSIVELY from this weekly snapshot table (cc#506 data
      source rule): no live/intraday inputs, no market-hours dependency. screener_raw.pe used
      AS-IS; segment medians are computed over these same snapshot columns at scoring time.
    - momentum_scores      -> the 5 M-pillar technicals (ret_1m, dma_50, dma_200, rsi_month,
      vol_trend) -- EXCEPTION to the screener_raw-only rule, unchanged momentum pipeline.
    - get_gvm payload      -> overview / key_takeaway / result_analysis / verdict

The computed detail is ALSO persisted back into the (currently empty) detail
columns of gvm_scores so it survives and can be queried directly tomorrow.

BFSI rule: for Banks / NBFCs / Insurance / AMC / Exchanges, D/E and Interest
Coverage are IRRELEVANT and are dropped from the parameter set.
"""

import logging
import statistics
from datetime import date
from typing import Optional, Dict, Any, List

from gvm_engine import (
    param_score, score_pe, score_opm_expansion, score_inst_holding_abs,
    score_interest_coverage, score_dma,
)

log = logging.getLogger("scorr.gvm_report")

# ─── Parameter definitions ──────────────────────────────────────────────────
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
    # cc#223: Promoter Holding is NOT a G/V/M engine metric (was a phantom scored row) -> removed.
    # G scores TWO institutional metrics, both Net (FII+DII); their source columns are computed
    # inline (screener_raw stores fii/dii separately; the net cols live only in loader memory).
    ("inst_abs",   "Institutional Holding (FII+DII)", "Reliability", "inst_holding_abs",    True, "inst_abs",    "%"),
    ("inst_chg",   "Change in Net Instl (FII+DII)",   "Reliability", "inst_holding_change", True, "inst_change", "%"),
    ("ret_1y",     "Return over 1 Year",     "Technicals",  "return_1y",              True,  "ret_1y",     "%"),
    ("ret_3y",     "Return over 3 Years",    "Technicals",  "return_3y",              True,  "ret_3y",     "%"),
    ("ret_52w_idx","52W vs Index",           "Technicals",  "return_52w_vs_index",    True,  "ret_52w_idx","%"),
]

# cc#223: Net (FII+DII) institutional source columns. screener_raw stores fii_holding/
# dii_holding and fii_change/dii_change SEPARATELY; the net values (inst_holding_abs /
# inst_holding_change) live only in screener_loader's in-memory df, never persisted. Compute
# them inline for the peer query, mirroring the loader's both-null -> NULL semantics.
_COMPUTED_COLS = {
    "inst_holding_abs":    ('CASE WHEN s."fii_holding" IS NULL AND s."dii_holding" IS NULL THEN NULL '
                            'ELSE COALESCE(s."fii_holding",0)+COALESCE(s."dii_holding",0) END'),
    "inst_holding_change": ('CASE WHEN s."fii_change" IS NULL AND s."dii_change" IS NULL THEN NULL '
                            'ELSE COALESCE(s."fii_change",0)+COALESCE(s."dii_change",0) END'),
}

# 5 additional M metrics sourced from momentum_scores (not screener_raw)
_M_EXTRA = [
    ("ret_1m",    "1M Return",    "%", "ret_1m",    True),
    ("dma_50",    "% vs DMA-50",  "%", "dma_50",    True),
    ("dma_200",   "% vs DMA-200", "%", "dma_200",   True),
    ("rsi_month", "RSI Monthly",  "",  "rsi_month", True),
    ("vol_trend", "Volume Trend", "x", "vol_trend", True),
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


def _median(values: List[float]) -> Optional[float]:
    return round(statistics.median(values), 4) if values else None


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

        # Pull every peer in the same segment + raw fundamentals. cc#223: the two Net
        # institutional columns are computed inline (screener_raw only stores fii/dii apart).
        # cc#506: "historical_pe" added -- not a PARAMS row itself, only needed as score_pe's
        # first (vs-own-history) benchmark for the "pe" row's special-case scoring below.
        screener_cols = list({p[3] for p in PARAMS} | {"historical_pe"})
        col_sql = ", ".join(
            (f'{_COMPUTED_COLS[c]} AS "{c}"' if c in _COMPUTED_COLS else f's."{c}" AS "{c}"')
            for c in screener_cols
        )
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

        # cc#506: LIVE segment median PE -- score_pe's second (vs-segment) benchmark. Computed
        # once from the SAME peer set as every other param (never the stale screener_raw
        # "Industry PE" / segment_pe column).
        pe_non_null = [v for v in (_f(p.get("pe")) for p in peers) if v is not None]
        live_segment_median_pe = _median(pe_non_null)

        # Build per-parameter benchmark
        params_out = []
        pillar_acc: Dict[str, List[float]] = {}
        persist_vals: Dict[str, Any] = {}

        for key, label, group, scol, hib, prefix, unit in PARAMS:
            if bfsi and key in ("int_cov",):
                continue  # BFSI rule: interest coverage irrelevant

            col_vals = [_f(p.get(scol)) for p in peers]
            non_null = [v for v in col_vals if v is not None]
            peer_median = _median(non_null)

            raw = rating = rank = None
            best_sym = worst_sym = None
            if me_idx is not None:
                raw = col_vals[me_idx]
                rank = _rank_within(col_vals, me_idx, hib)
                # cc#506: rating source = gvm_engine, same functions the nightly G/V/M engine
                # uses, fed the segment MEDIAN -- retires the old percentile-rank _rate_within.
                if key == "pe":
                    hist_pe = _f(peers[me_idx].get("historical_pe"))
                    rating = score_pe(raw, hist_pe, live_segment_median_pe)
                elif key == "opm_exp":
                    rating = score_opm_expansion(raw, peer_median)
                elif key == "inst_abs":
                    rating = score_inst_holding_abs(raw)
                elif key == "int_cov":
                    rating = score_interest_coverage(raw, peer_median, bfsi)
                else:
                    rating = param_score(raw, peer_median)
            # best / worst peer for the ladder + cc#507: full sorted peer list for the
            # per-metric peer-ladder chart (best -> worst, direction-aware).
            peers_list = []
            if non_null:
                pairs = [(p["symbol"], _f(p.get(scol))) for p in peers if _f(p.get(scol)) is not None]
                pairs.sort(key=lambda x: x[1], reverse=hib)
                best_sym = {"symbol": pairs[0][0], "value": round(pairs[0][1], 2)}
                worst_sym = {"symbol": pairs[-1][0], "value": round(pairs[-1][1], 2)}
                peers_list = [{"symbol": s, "value": round(v, 2)} for s, v in pairs]

            row = {
                "key": key, "label": label, "group": group, "unit": unit,
                "raw": round(raw, 2) if raw is not None else None,
                "peer_avg": peer_median, "peer_median": peer_median,
                "rank": rank, "peer_count": len(non_null),
                "rating": rating, "higher_is_better": hib,
                "best": best_sym, "worst": worst_sym, "peers": peers_list,
                "beats_peer": (raw is not None and peer_median is not None and
                               ((raw >= peer_median) if hib else (raw <= peer_median))),
            }
            # cc#507: PE row carries an extra chart marker at the company's OWN historical PE
            # (the "own 5y avg" dashed line) -- the re-rating story the deleted Ownership Flow
            # strip used to carry now lives here + the new Historical PE table row.
            if key == "pe" and me_idx is not None:
                hist_pe_marker = _f(peers[me_idx].get("historical_pe"))
                if hist_pe_marker is not None:
                    row["extra_marker"] = {"label": "own 5y avg", "value": round(hist_pe_marker, 2)}
            params_out.append(row)

            if rating is not None:
                pillar_acc.setdefault(group, []).append(rating)
                persist_vals[f"{prefix}_raw"] = round(raw, 2) if raw is not None else None
                persist_vals[f"{prefix}_peer"] = peer_median
                persist_vals[f"{prefix}_rating"] = rating

        # ── Missing M metrics: DMA50/200, RSI Monthly, 1M Return, Volume Trend ──
        # These come from momentum_scores (not screener_raw), queried separately.
        try:
            cur.execute("""
                SELECT symbol, ret_1m, dma_50, dma_200, rsi_month, vol_trend
                FROM momentum_scores
                WHERE symbol = ANY(%s)
                  AND score_date = (SELECT MAX(score_date) FROM momentum_scores)
            """, (symbols,))
            _mom = {r[0]: r for r in cur.fetchall()}
            _mcols = ["symbol", "ret_1m", "dma_50", "dma_200", "rsi_month", "vol_trend"]
            for _key, _label, _unit, _col, _hib in _M_EXTRA:
                _ci = _mcols.index(_col)
                _cv = [_f(_mom[s][_ci]) if s in _mom else None for s in symbols]
                _nn = [v for v in _cv if v is not None]
                _median_v = _median(_nn)
                _raw = _cv[me_idx] if me_idx is not None else None
                _rnk = _rank_within(_cv, me_idx, _hib) if me_idx is not None else None
                _rat = None
                if me_idx is not None:
                    # cc#506: DMA 50/200 -> gvm_engine.score_dma (absolute deviation bands, no
                    # peer input). momentum_scores.dma_50/200 already STORE a deviation % (not a
                    # raw MA price level), so synthesize a price/dma pair that reduces score_dma's
                    # own (price-dma)/dma*100 formula back to exactly that stored deviation --
                    # reuses the engine's bands without re-deriving raw price history here. RSI
                    # Monthly / Vol Trend / 1M Return have no dedicated engine fn -> param_score
                    # vs the segment median, same as the fundamentals above (data source for all
                    # 5 stays momentum_scores/raw_prices, unchanged per the momentum exception).
                    if _key in ("dma_50", "dma_200"):
                        _rat = score_dma(100 + _raw, 100) if _raw is not None else 5.0
                    else:
                        _rat = param_score(_raw, _median_v)
                _best = _worst = None
                _peers_list = []
                if _nn:
                    _pp = sorted([(symbols[i], v) for i, v in enumerate(_cv) if v is not None],
                                 key=lambda x: x[1], reverse=_hib)
                    _best  = {"symbol": _pp[0][0],  "value": round(_pp[0][1], 2)}
                    _worst = {"symbol": _pp[-1][0], "value": round(_pp[-1][1], 2)}
                    _peers_list = [{"symbol": s, "value": round(v, 2)} for s, v in _pp]
                params_out.append({
                    "key": _key, "label": _label, "group": "Technicals", "unit": _unit,
                    "raw": round(_raw, 2) if _raw is not None else None,
                    "peer_avg": _median_v, "peer_median": _median_v,
                    "rank": _rnk, "peer_count": len(_nn),
                    "rating": _rat, "higher_is_better": _hib,
                    "best": _best, "worst": _worst, "peers": _peers_list,
                    "beats_peer": (_raw is not None and _median_v is not None and
                                   ((_raw >= _median_v) if _hib else (_raw <= _median_v))),
                })
                if _rat is not None:
                    pillar_acc.setdefault("Technicals", []).append(_rat)
        except Exception as _me:
            log.warning(f"momentum_scores M extras failed for {symbol}: {_me}")

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


# ─── cc#518: FINANCIALS section (screener.in-style horizontal tables) ──────────────────────────
# "Indians love screener because of this simplicity" (founder, 18-Jul-2026) -- periods as columns
# (oldest left -> newest right), line items as rows. Data source: fundamentals_history ONLY
# (symbol, consolidated, section, period_type, period_label, period_end, metrics jsonb). Zero new
# scraping. One SQL pass per symbol (6 small indexed section reads) -- no N+1 queries.

def _parse_metric(v) -> Optional[float]:
    """fundamentals_history.metrics values are strings like "34,309", "11%", "1.42", "-16" --
    strip formatting uniformly. The unit (Rs Cr / % / days / EPS) is a property of the ROW
    definition below, not re-derived from the string each time."""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "--", "-"):
        return None
    s = s.rstrip("%").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


# (row_key, unit, chart_type) -- row_key is the metrics dict key VERBATIM (already screener's own
# names, per spec: "render mapping table in code, not string transforms").
# unit: "cr" (Rs Cr, Indian comma grouping) | "pct" | "eps" | "days"
# chart_type: "bar" (absolute Rs values) | "line" (%, days, EPS)
_QUARTER_ROW_DEFS = [
    ("Sales",             "cr",  "bar"),
    ("Expenses",          "cr",  "bar"),
    ("Operating Profit",  "cr",  "bar"),
    ("OPM %",             "pct", "line"),
    ("Other Income",      "cr",  "bar"),
    ("Interest",          "cr",  "bar"),
    ("Depreciation",      "cr",  "bar"),
    ("Profit before tax", "cr",  "bar"),
    ("Tax %",             "pct", "line"),
    ("Net Profit",        "cr",  "bar"),
    ("EPS in Rs",         "eps", "line"),
]
_PL_ROW_DEFS = _QUARTER_ROW_DEFS   # same key set, annual + TTM
# cc#518 REVISION (founder, same session): P&L display trims to 6 key rows -- the rest stay in the
# payload (display=False) for the row charts / future use, never dropped from computation.
_PL_DISPLAY_KEYS = ("Sales", "Operating Profit", "OPM %", "Interest", "Net Profit", "EPS in Rs")

_BS_ROW_DEFS = [
    ("Equity Capital",    "cr", "bar"),
    ("Reserves",          "cr", "bar"),
    ("Borrowings",        "cr", "bar"),
    ("Other Liabilities", "cr", "bar"),
    ("Total Liabilities", "cr", "bar"),
    ("Fixed Assets",      "cr", "bar"),
    ("CWIP",              "cr", "bar"),
    ("Investments",       "cr", "bar"),
    ("Other Assets",      "cr", "bar"),
    ("Total Assets",      "cr", "bar"),
]
_BS_DISPLAY_KEYS = ("Equity Capital", "Reserves", "Borrowings", "Fixed Assets", "Investments", "Total Assets")

_CF_ROW_DEFS = [
    ("Cash from Operating Activity", "cr",  "bar"),
    ("Cash from Investing Activity", "cr",  "bar"),
    ("Cash from Financing Activity", "cr",  "bar"),
    ("Net Cash Flow",                "cr",  "bar"),
    ("Free Cash Flow",               "cr",  "bar"),
    ("CFO/OP",                       "pct", "line"),
]

_RATIO_ROW_DEFS = [
    ("ROCE %",                "pct",  "line"),
    ("Debtor Days",           "days", "line"),
    ("Inventory Days",        "days", "line"),
    ("Days Payable",          "days", "line"),
    ("Cash Conversion Cycle", "days", "line"),
    ("Working Capital Days",  "days", "line"),
]

# Shareholding has no fixed row set ("whatever keys exist") -- this is a sort order, not a filter.
_SHAREHOLDING_ORDER = ["Promoters", "FIIs", "DIIs", "Public", "Others", "No. of Shareholders"]


def _fh_rows(cur, symbol: str, section: str, period_type: str, limit: int):
    """One indexed read for one section. consolidated=true preferred, standalone fallback -- the
    choice is made PER SYMBOL (a company's rows don't mix consolidated/standalone across periods)."""
    cur.execute("""SELECT consolidated, period_label, period_end, metrics FROM fundamentals_history
                   WHERE symbol=%s AND section=%s AND period_type=%s
                   ORDER BY period_end ASC""", (symbol, section, period_type))
    rows = cur.fetchall()
    if not rows:
        return [], None
    has_consolidated = any(bool(r[0]) for r in rows)
    basis = "Consolidated" if has_consolidated else "Standalone"
    filtered = [r for r in rows if bool(r[0]) == has_consolidated][-limit:]
    return [{"period_label": r[1], "period_end": str(r[2]), "metrics": r[3] or {}} for r in filtered], basis


def _build_table(periods: List[Dict[str, Any]], row_defs, display_keys=None) -> Dict[str, Any]:
    """periods: oldest -> newest. A row absent for EVERY period is dropped entirely (never an
    all-blank row); a row present for SOME periods keeps "--" cells for the missing ones."""
    rows = []
    for key, unit, chart_type in row_defs:
        values = [_parse_metric(p["metrics"].get(key)) for p in periods]
        if all(v is None for v in values):
            continue
        rows.append({"label": key, "unit": unit, "chart_type": chart_type,
                     "display": (display_keys is None or key in display_keys), "values": values})
    return {"periods": [p["period_label"] for p in periods], "rows": rows}


def _build_ttm(quarters: List[Dict[str, Any]]) -> Optional[Dict[str, Optional[float]]]:
    """TTM = SUM of the last 4 quarters (Sales/Expenses/.../Net Profit/EPS); TTM OPM% = TTM OP /
    TTM Sales. Tax% and Dividend Payout% stay blank in TTM (spec). Only when 4 full quarters exist."""
    if len(quarters) < 4:
        return None
    last4 = quarters[-4:]

    def _sum(key):
        vals = [_parse_metric(q["metrics"].get(key)) for q in last4]
        return round(sum(vals), 2) if len(vals) == 4 and all(v is not None for v in vals) else None

    ttm = {k: _sum(k) for k in ("Sales", "Expenses", "Operating Profit", "Other Income", "Interest",
                                 "Depreciation", "Profit before tax", "Net Profit", "EPS in Rs")}
    ttm["OPM %"] = (round(ttm["Operating Profit"] / ttm["Sales"] * 100.0, 2)
                     if (ttm.get("Operating Profit") is not None and ttm.get("Sales")) else None)
    ttm["Tax %"] = None
    ttm["Dividend Payout %"] = None
    return ttm


def _shareholding_table(periods: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    all_keys = set()
    for p in periods:
        all_keys.update(p["metrics"].keys())
    if not all_keys:
        return None
    order = lambda k: (_SHAREHOLDING_ORDER.index(k) if k in _SHAREHOLDING_ORDER else len(_SHAREHOLDING_ORDER), k)
    rows = []
    for key in sorted(all_keys, key=order):
        unit = "count" if key == "No. of Shareholders" else "pct"
        values = [_parse_metric(p["metrics"].get(key)) for p in periods]
        rows.append({"label": key, "unit": unit, "chart_type": "line", "display": True, "values": values})
    return {"periods": [p["period_label"] for p in periods], "rows": rows}


_FH_SECTIONS = [("quarters", "quarter"), ("profit-loss", "annual"), ("balance-sheet", "annual"),
                ("cash-flow", "annual"), ("ratios", "annual"), ("shareholding", "quarter")]

# cc#638: canonical BFSI rule — the entire Solvency bucket is hidden for financials (D/E + interest
# coverage are meaningless there). Segment keyword match on gvm_scores.segment.
_BFSI_KEYS = ("Bank", "NBFC", "Insurance", "Finance", "AMC", "Broking", "Capital Markets",
              "Exchanges", "Microfinance", "Housing Finance", "Financial")


def _is_bfsi(segment):
    s = segment or ""
    return any(k in s for k in _BFSI_KEYS)


def _cagr(latest, base, years):
    if latest is None or base is None or base <= 0 or latest <= 0:
        return None
    return round((pow(latest / base, 1.0 / years) - 1.0) * 100.0, 1)


def _g1y(latest, prev):
    if latest is None or prev in (None, 0):
        return None
    return round((latest - prev) / abs(prev) * 100.0, 1)


def _annual_vals(cur, symbol, section, key):
    """Oldest->newest series of one metric across ANNUAL periods (consolidated-preferred)."""
    cur.execute("""SELECT consolidated, metrics FROM fundamentals_history
                   WHERE symbol=%s AND section=%s AND period_type='annual' ORDER BY period_end ASC""",
                (symbol, section))
    rows = cur.fetchall()
    if not rows:
        return []
    has_c = any(bool(r[0]) for r in rows)
    return [_parse_metric((r[1] or {}).get(key)) for r in rows if bool(r[0]) == has_c]


def _rat_row(label, unit, values):
    if all(v is None for v in values):
        return None
    return {"label": label, "unit": unit, "chart_type": "line", "display": True, "values": values}


def build_ratios_v2(cur, symbol: str, segment: str) -> Dict[str, Any]:
    """cc#638 RATIOS V2: five bucketed sub-tables (Growth, Profitability, Valuation, Solvency,
    Efficiency) from fundamentals_history annual series + screener_raw current. Solvency is HIDDEN for
    BFSI. Missing metric = skipped row; an empty bucket is omitted. Zero new scraping."""
    sym = (symbol or "").strip().upper()
    cur.execute('''SELECT pe, "Price to book value", "EVEBITDA", dividend_yield, market_cap, "Sales",
                          "Debt to equity", interest_coverage, roce, "Return on equity", opm,
                          "Sales growth", sales_growth_3y, sales_growth_5y
                   FROM screener_raw WHERE nse_code=%s LIMIT 1''', (sym,))
    r = cur.fetchone()
    k = {}
    if r:
        for name, val in zip(["pe", "pb", "evebitda", "divyield", "mcap", "sales", "de", "intcov",
                              "roce", "roe", "opm", "sg1", "sg3", "sg5"], r):
            k[name] = _parse_metric(val)
    np_s = _annual_vals(cur, sym, "profit-loss", "Net Profit")
    eps_s = _annual_vals(cur, sym, "profit-loss", "EPS in Rs")
    sales_s = _annual_vals(cur, sym, "profit-loss", "Sales")
    ta_s = _annual_vals(cur, sym, "balance-sheet", "Total Assets")

    def _last(s, n=1):
        return s[-n] if len(s) >= n else None
    npm = (round(_last(np_s) / _last(sales_s) * 100.0, 1)
           if (_last(np_s) is not None and _last(sales_s)) else None)
    asset_turn = (round(_last(sales_s) / _last(ta_s), 2)
                  if (_last(sales_s) is not None and _last(ta_s)) else None)
    mcap_sales = (round(k.get("mcap") / k.get("sales"), 2)
                  if (k.get("mcap") is not None and k.get("sales")) else None)

    buckets = []

    def _add(title, periods, rows):
        rows = [x for x in rows if x is not None]
        if rows:
            buckets.append({"title": title, "table": {"periods": periods, "rows": rows}})

    # 1) GROWTH — 1Y / 3Y CAGR / 5Y CAGR
    _add("Growth", ["1Y", "3Y CAGR", "5Y CAGR"], [
        _rat_row("Sales Growth", "pct", [k.get("sg1") if k.get("sg1") is not None else _g1y(_last(sales_s), _last(sales_s, 2)),
                                          k.get("sg3") if k.get("sg3") is not None else _cagr(_last(sales_s), _last(sales_s, 4), 3),
                                          k.get("sg5") if k.get("sg5") is not None else _cagr(_last(sales_s), _last(sales_s, 6), 5)]),
        _rat_row("Net Profit Growth", "pct", [_g1y(_last(np_s), _last(np_s, 2)),
                                              _cagr(_last(np_s), _last(np_s, 4), 3), _cagr(_last(np_s), _last(np_s, 6), 5)]),
        _rat_row("EPS Growth", "pct", [_g1y(_last(eps_s), _last(eps_s, 2)), None, None]),
    ])
    # 2) PROFITABILITY (current)
    _add("Profitability", ["Current"], [
        _rat_row("ROCE %", "pct", [k.get("roce")]), _rat_row("ROE %", "pct", [k.get("roe")]),
        _rat_row("OPM %", "pct", [k.get("opm")]), _rat_row("NPM %", "pct", [npm]),
    ])
    # 3) VALUATION (current)
    _add("Valuation", ["Current"], [
        _rat_row("PE", "x", [k.get("pe")]), _rat_row("PB", "x", [k.get("pb")]),
        _rat_row("EV/EBITDA", "x", [k.get("evebitda")]), _rat_row("MCap/Sales", "x", [mcap_sales]),
        _rat_row("Dividend Yield", "pct", [k.get("divyield")]),
    ])
    # 4) SOLVENCY (current) — HIDDEN for BFSI
    if not _is_bfsi(segment):
        _add("Solvency", ["Current"], [
            _rat_row("Debt / Equity", "x", [k.get("de")]),
            _rat_row("Interest Coverage", "x", [k.get("intcov")]),
        ])
    # 5) EFFICIENCY — existing annual ratios series + Asset Turnover (current)
    _ra_all = _all_section(cur, sym, "ratios", "annual")
    eff_rows = _variant_rows(_ra_all, any(bool(x[0]) for x in _ra_all), 12) if _ra_all else []
    eff_tbl = _build_table(eff_rows, _RATIO_ROW_DEFS) if eff_rows else {"periods": [], "rows": []}
    eff_periods = eff_tbl.get("periods") or ["Current"]
    eff_final = list(eff_tbl.get("rows") or [])
    if asset_turn is not None:
        eff_final.append({"label": "Asset Turnover", "unit": "x", "chart_type": "line", "display": True,
                          "values": [None] * (len(eff_periods) - 1) + [asset_turn]})
    if eff_final:
        buckets.append({"title": "Efficiency", "table": {"periods": eff_periods, "rows": eff_final}})

    return {"buckets": buckets, "bfsi": _is_bfsi(segment)}


def _all_section(cur, symbol, section, ptype):
    cur.execute("""SELECT consolidated, period_label, period_end, metrics FROM fundamentals_history
                   WHERE symbol=%s AND section=%s AND period_type=%s ORDER BY period_end ASC""",
                (symbol, section, ptype))
    return cur.fetchall()


def _variant_rows(all_rows, want_consolidated, limit):
    """cc#636: rows for ONE variant (consolidated|standalone), oldest->newest, capped at `limit`."""
    filt = [r for r in all_rows if bool(r[0]) == want_consolidated][-limit:]
    return [{"period_label": r[1], "period_end": str(r[2]), "metrics": r[3] or {}} for r in filt]


def _build_one_variant(sections, want_consolidated):
    """cc#636: full financials block for one variant, or None if the variant has no rows at all.
    change_2: annual P&L renders the SAME canonical row set as Quarterly (no display trim)."""
    q_rows  = _variant_rows(sections["quarters"],      want_consolidated, 13)
    pl_rows = _variant_rows(sections["profit-loss"],   want_consolidated, 12)
    bs_rows = _variant_rows(sections["balance-sheet"], want_consolidated, 12)
    cf_rows = _variant_rows(sections["cash-flow"],     want_consolidated, 12)
    ra_rows = _variant_rows(sections["ratios"],        want_consolidated, 12)
    sh_rows = _variant_rows(sections["shareholding"],  want_consolidated, 12)
    if not any([q_rows, pl_rows, bs_rows, cf_rows, ra_rows, sh_rows]):
        return None
    out = {"basis": "Consolidated" if want_consolidated else "Standalone",
           "quarterly": None, "profit_loss": None, "balance_sheet": None,
           "cash_flow": None, "ratios": None, "shareholding": None}
    if q_rows:
        out["quarterly"] = _build_table(q_rows, _QUARTER_ROW_DEFS)
    if pl_rows:
        # cc#636 change_2: NO display_keys -> annual P&L shows the full canonical row order, identical
        # to Quarterly Results (missing keys still skip the row via _build_table's all-None drop).
        pl_table = _build_table(pl_rows, _PL_ROW_DEFS)
        ttm = _build_ttm(q_rows) if q_rows else None
        pl_table["ttm"] = ttm is not None
        if ttm is not None:
            pl_table["periods"].append("TTM")
            for row in pl_table["rows"]:
                row["values"].append(ttm.get(row["label"]))
        out["profit_loss"] = pl_table
    if bs_rows:
        out["balance_sheet"] = _build_table(bs_rows, _BS_ROW_DEFS, display_keys=_BS_DISPLAY_KEYS)
    if cf_rows:
        out["cash_flow"] = _build_table(cf_rows, _CF_ROW_DEFS)
    if ra_rows:
        out["ratios"] = _build_table(ra_rows, _RATIO_ROW_DEFS)
    if sh_rows:
        out["shareholding"] = _shareholding_table(sh_rows)
    return out


def build_financials_block(conn, symbol: str) -> Dict[str, Any]:
    """cc#518 / cc#636: screener.in-style horizontal financial tables. Now returns BOTH variants
    (consolidated + standalone) keyed under `variants` so the page toggle switches all tables from one
    state, plus the default variant's tables at the top level for backward compatibility. Each table is
    None when that section has no rows. One indexed read per section (no N+1)."""
    symbol = (symbol or "").strip().upper()
    sections = {}
    with conn.cursor() as cur:
        for section, ptype in _FH_SECTIONS:
            cur.execute("""SELECT consolidated, period_label, period_end, metrics FROM fundamentals_history
                           WHERE symbol=%s AND section=%s AND period_type=%s ORDER BY period_end ASC""",
                        (symbol, section, ptype))
            sections[section] = cur.fetchall()
    has_cons = any(bool(r[0]) for rows in sections.values() for r in rows)
    has_std = any(not bool(r[0]) for rows in sections.values() for r in rows)
    variants = {}
    if has_cons:
        v = _build_one_variant(sections, True)
        if v:
            variants["consolidated"] = v
    if has_std:
        v = _build_one_variant(sections, False)
        if v:
            variants["standalone"] = v
    available = [k for k in ("consolidated", "standalone") if k in variants]
    default = "consolidated" if "consolidated" in variants else ("standalone" if "standalone" in variants else None)
    base = variants.get(default) or {"basis": None, "quarterly": None, "profit_loss": None,
                                     "balance_sheet": None, "cash_flow": None, "ratios": None,
                                     "shareholding": None}
    # cc#638: RATIOS V2 buckets (variant-agnostic — screener_raw current + fundamentals annual series).
    ratios_v2 = None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT segment FROM gvm_scores WHERE symbol=%s ORDER BY score_date DESC LIMIT 1", (symbol,))
            seg = cur.fetchone()
            ratios_v2 = build_ratios_v2(cur, symbol, seg[0] if seg else None)
    except Exception:
        ratios_v2 = None
    # backward-compat: default variant's tables at top level, + the toggle metadata + ratios_v2
    return {**base, "variants": variants, "variants_available": available, "default_variant": default,
            "ratios_v2": ratios_v2}


# ─── cc#541: OPERATIONAL METRICS section — per-sector KPIs (NIM/GNPA/CASA for banks,
# volumes/realization for cement, etc), quarterly trend + QoQ/YoY. Source: the
# LLM-extracted sector_ops_metrics + concall_summaries. HONEST empty state: a company
# with no extracted KPIs returns has_data=False (the frontend hides the section — nothing faked).

_OPS_ACRONYMS = {"NIM", "GNPA", "NNPA", "CASA", "PCR", "ROA", "ROE", "ROCE", "EBIT",
                 "EBITDA", "TCV", "EV", "OPM", "NPA", "AUM", "NII", "CC", "QOQ", "YOY"}


def _ops_quarter_key(q: str):
    """'Q4FY26' -> (2026, 4) for chronological sort; unrecognised -> (0, 0) (sorts oldest)."""
    import re
    m = re.match(r"Q([1-4])FY(\d{2,4})", str(q or "").upper().replace(" ", ""))
    if not m:
        return (0, 0)
    quarter, fy = int(m.group(1)), int(m.group(2))
    if fy < 100:
        fy += 2000
    return (fy, quarter)


def _ops_pretty(metric_name: str) -> str:
    """Human label from the stored metric_name (LLM writes varied casings like 'GNPA_pct',
    'provision_coverage', 'NIM'): strip a trailing _pct/_ratio, split on '_', upper-case known
    acronyms, title-case the rest. Falls back to the raw name — never blank."""
    s = str(metric_name or "").strip()
    if not s:
        return metric_name
    for suf in ("_pct", "_ratio", "_percent", "_pcnt"):
        if s.lower().endswith(suf):
            s = s[: -len(suf)]
            break
    parts = [p for p in s.replace("-", "_").split("_") if p]
    out = []
    for p in parts:
        out.append(p.upper() if p.upper() in _OPS_ACRONYMS else p.capitalize())
    return " ".join(out) or metric_name


def build_ops_block(conn, symbol: str) -> Dict[str, Any]:
    """cc#541: operational KPI block for the GVM company report. Pivots sector_ops_metrics into
    quarters-as-columns rows (last 6 quarters, oldest -> newest, mirroring the FINANCIALS shape),
    with a latest-quarter QoQ and YoY delta per metric where the prior periods exist, plus the
    latest concall summary/guidance/tone. has_data=False when nothing is extracted (honest empty)."""
    symbol = (symbol or "").strip().upper()
    out: Dict[str, Any] = {"has_data": False, "sector": None, "periods": [], "rows": [], "concall": None}
    with conn.cursor() as cur:
        cur.execute("""SELECT sector, metric_name, unit, quarter, metric_value, confidence
                       FROM sector_ops_metrics
                       WHERE symbol=%s AND metric_value IS NOT NULL""", (symbol,))
        rows = cur.fetchall()
        if rows:
            out["sector"] = rows[0][0]
            quarters = sorted({r[3] for r in rows if r[3]}, key=_ops_quarter_key)[-6:]
            by_metric: Dict[str, Dict[str, Any]] = {}
            units: Dict[str, Any] = {}
            for _sector, mname, unit, q, val, _conf in rows:
                if q not in quarters:
                    continue
                by_metric.setdefault(mname, {})[q] = _f(val)
                units.setdefault(mname, unit)
            metric_rows = []
            for mname, qmap in by_metric.items():
                values = [qmap.get(q) for q in quarters]
                if all(v is None for v in values):
                    continue
                latest = values[-1]
                qoq = (round(latest - values[-2], 2) if (len(values) >= 2 and latest is not None
                       and values[-2] is not None) else None)
                yoy = (round(latest - values[-5], 2) if (len(values) >= 5 and latest is not None
                       and values[-5] is not None) else None)
                metric_rows.append({"label": _ops_pretty(mname), "metric_name": mname,
                                    "unit": units.get(mname), "values": values,
                                    "latest": latest, "qoq": qoq, "yoy": yoy})
            metric_rows.sort(key=lambda r: r["label"])
            if metric_rows:
                out.update({"has_data": True, "periods": quarters, "rows": metric_rows})

        cur.execute("""SELECT quarter, summary, guidance, tone FROM concall_summaries
                       WHERE symbol=%s ORDER BY computed_at DESC LIMIT 1""", (symbol,))
        c = cur.fetchone()
        if c and (c[1] or c[2]):
            out["concall"] = {"quarter": c[0], "summary": c[1], "guidance": c[2], "tone": c[3]}
    return out


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
