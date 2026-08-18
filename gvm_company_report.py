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
    ("qoq_sales",  "YoY Sales Growth",       "Trackrecord", "qoq_sales_growth",       True,  "qoq_sales",  "%"),
    ("qoq_profit", "YoY Profit Growth",      "Trackrecord", "qoq_profit_growth",      True,  "qoq_profit", "%"),
    ("opm",        "Operating Margin",       "Trackrecord", "opm",                    True,  "opm",        "%"),
    # cc#1094: source repointed to the computed opm_expansion above, and the unit corrected to bps —
    # the number was always going to be basis points once it had a value, and labelling 798 as "%"
    # would have been a second defect sitting behind the first.
    ("opm_exp",    "OPM Expansion",          "Trackrecord", "opm_expansion",          True,  "opm_exp",    "bps"),
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
# cc#828 part_2: screener_raw column -> the universe_technicals column that computes the SAME
# quantity natively from raw_prices. Read as `screener value COALESCE native value`, so a narrower
# CSV export (02-Aug: return_1y/3y/52w blanked for all 1,801 rows) degrades the metric to computed
# truth instead of NULL -> a silently-neutral 5.0 in the M pillar.
_NATIVE_FALLBACK = {
    "return_1y":           "year_return",
    "return_3y":           "return_3y",
    "return_52w_vs_index": "return_52w_vs_index",
}

_COMPUTED_COLS = {
    "inst_holding_abs":    ('CASE WHEN s."fii_holding" IS NULL AND s."dii_holding" IS NULL THEN NULL '
                            'ELSE COALESCE(s."fii_holding",0)+COALESCE(s."dii_holding",0) END'),
    "inst_holding_change": ('CASE WHEN s."fii_change" IS NULL AND s."dii_change" IS NULL THEN NULL '
                            'ELSE COALESCE(s."fii_change",0)+COALESCE(s."dii_change",0) END'),
    # cc#1094: OPM Expansion had no usable source. The PARAMS row pointed at
    # screener_raw."Operating profit growth", which the current Screener export populates for 0 of
    # 1816 rows — so raw was None for EVERY symbol, peer_count was 0 (the #-/0 on the page), rating
    # was None, and the cc#828 part_3 rule correctly excluded the metric. Trackrecord has therefore
    # been averaging 7 of 8 metrics across the whole universe, not 8.
    #
    # This computes the SAME quantity the nightly engine already computes and scores:
    # gvm_nightly._load_merged_df does (opm_latest_q - opm_prev_year_q) * 100. The *100 is a
    # percentage-points-to-BASIS-POINTS conversion, NOT a scaling bug, and score_opm_expansion's
    # bands (>100 / >50 / >=0) are bps bands — >100bps = >1pp. Founder confirmed that 18-Aug.
    # Nothing in gvm_nightly.py or the bands is touched here.
    #
    # OR, NOT AND, and the difference matters. The two institutional rows above use AND because a
    # missing FII with a present DII is still a real net figure. An expansion is a DIFFERENCE: with
    # either quarter missing there is no expansion to report, and COALESCE-ing the gap to 0 would
    # invent a flat margin. That also matches the nightly, where NaN - x is NaN.
    "opm_expansion":       ('CASE WHEN s."opm_latest_q" IS NULL OR s."opm_prev_year_q" IS NULL THEN NULL '
                            'ELSE (s."opm_latest_q" - s."opm_prev_year_q") * 100 END'),
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
        # cc#828 part_2: pull the native equivalents alongside, under a _nat_ alias, and resolve the
        # COALESCE in Python (screener_raw is a wide TEXT dump — a SQL COALESCE of text against
        # numeric would not even type-check; _f() already normalises both sides here).
        nat_sql = "".join(f', u."{n}" AS "_nat_{s}"' for s, n in _NATIVE_FALLBACK.items())
        cur.execute(f"""
            SELECT g.symbol, g.company_name, g.gvm_score, g.market_cap, {col_sql}{nat_sql}
            FROM gvm_scores g
            JOIN screener_raw s ON s.nse_code = g.symbol
            LEFT JOIN universe_technicals u
                   ON u.symbol = g.symbol
                  AND u.score_date = (SELECT MAX(score_date) FROM universe_technicals)
            WHERE g.segment = %s AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
        """, (segment,))
        pcols = [d[0] for d in cur.description]
        peers = [dict(zip(pcols, r)) for r in cur.fetchall()]

        # Apply the fallback ONCE, here, so every downstream consumer (own value, peer median,
        # rank, peer ladder, best/worst) sees one consistent resolved number.
        native_used = {}
        for scol, ncol in _NATIVE_FALLBACK.items():
            used = 0
            for p in peers:
                if _f(p.get(scol)) is None and _f(p.get(f"_nat_{scol}")) is not None:
                    p[scol] = p[f"_nat_{scol}"]
                    used += 1
            if used:
                native_used[scol] = used

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
        pillar_total: Dict[str, int] = {}   # cc#828 part_3: denominator incl. excluded metrics
        persist_vals: Dict[str, Any] = {}

        for key, label, group, scol, hib, prefix, unit in PARAMS:
            if bfsi and key in ("int_cov",):
                continue  # BFSI rule: interest coverage irrelevant

            col_vals = [_f(p.get(scol)) for p in peers]
            non_null = [v for v in col_vals if v is not None]
            peer_median = _median(non_null)

            raw = rating = rank = None
            best_sym = worst_sym = None
            pillar_total[group] = pillar_total.get(group, 0) + 1
            if me_idx is not None:
                raw = col_vals[me_idx]
                rank = _rank_within(col_vals, me_idx, hib)
                # cc#828 part_3: NO VALUE => NO SCORE. Every gvm_engine scorer returns
                # BLANK_SCORE (5.0) for a blank input, which is indistinguishable from a
                # genuinely average stock — that is exactly how the 02-Aug blanked return
                # columns rode along inside M=8.91 without anyone noticing. A metric with no
                # value is now EXCLUDED: rating None, tile greyed, pillar averaged over the
                # metrics that do have data with the denominator carried to the UI.
                if raw is None:
                    rating = None
                # cc#506: rating source = gvm_engine, same functions the nightly G/V/M engine
                # uses, fed the segment MEDIAN -- retires the old percentile-rank _rate_within.
                elif key == "pe":
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
                "excluded": rating is None,   # cc#828 part_3: no value -> out of the pillar average
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
                pillar_total["Technicals"] = pillar_total.get("Technicals", 0) + 1
                if me_idx is not None and _raw is not None:
                    # cc#506: DMA 50/200 -> gvm_engine.score_dma (absolute deviation bands, no
                    # peer input). momentum_scores.dma_50/200 already STORE a deviation % (not a
                    # raw MA price level), so synthesize a price/dma pair that reduces score_dma's
                    # own (price-dma)/dma*100 formula back to exactly that stored deviation --
                    # reuses the engine's bands without re-deriving raw price history here. RSI
                    # Monthly / Vol Trend / 1M Return have no dedicated engine fn -> param_score
                    # vs the segment median, same as the fundamentals above (data source for all
                    # 5 stays momentum_scores/raw_prices, unchanged per the momentum exception).
                    # cc#828 part_3: the `else 5.0` that used to sit on this line is gone — a
                    # missing DMA is now an excluded tile, not a neutral score.
                    if _key in ("dma_50", "dma_200"):
                        _rat = score_dma(100 + _raw, 100)
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
                    "excluded": _rat is None,   # cc#828 part_3
                    "best": _best, "worst": _worst, "peers": _peers_list,
                    "beats_peer": (_raw is not None and _median_v is not None and
                                   ((_raw >= _median_v) if _hib else (_raw <= _median_v))),
                })
                if _rat is not None:
                    pillar_acc.setdefault("Technicals", []).append(_rat)
        except Exception as _me:
            log.warning(f"momentum_scores M extras failed for {symbol}: {_me}")

        # Pillar (headline) scores = avg of param ratings in that group.
        # cc#828 part_3: the average is over the metrics that HAVE data, and the denominator that
        # produced it travels with the score so the UI can print "M over 5/8 metrics" instead of
        # implying all eight were measured.
        pillars = {}
        pillar_coverage = {}
        for group, score_key in PILLAR_MAP.items():
            vals = pillar_acc.get(group, [])
            total = pillar_total.get(group, len(vals))
            pillars[score_key] = round(sum(vals) / len(vals), 2) if vals else None
            pillar_coverage[score_key] = {"scored": len(vals), "total": total,
                                          "excluded": max(total - len(vals), 0)}

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
        "pillar_coverage": pillar_coverage,      # cc#828 part_3: {score_key:{scored,total,excluded}}
        "native_fallback_used": native_used,     # cc#828 part_2: {screener_col: n_peers_backfilled}
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
# cc#833 BFSI ALIAS REGISTRY. Screener publishes NO Sales / Operating Profit / OPM % for financing
# companies — it publishes Revenue / Financing Profit / Financing Margin %. The fixed industrial row
# template above therefore rendered BAJFINANCE's P&L against real stored data and printed nothing.
# The registry is a small explicit map, not a string transform (same doctrine as the row defs).
#
# Insertion, not substitution: each alias row is slotted immediately after the industrial row it
# stands in for. _build_table already DROPS any row that is None across every period, so a BFSI
# symbol loses the industrial row and keeps the alias, an industrial symbol does exactly the reverse,
# and neither needs a segment test. That is what makes the non-BFSI path byte-identical.
_BFSI_ALIAS_ROWS = {
    "Sales":            [("Revenue",           "cr",  "bar")],
    "Operating Profit": [("Financing Profit",  "cr",  "bar")],
    "OPM %":            [("Financing Margin %", "pct", "line")],
}


def _with_bfsi_aliases(row_defs):
    out = []
    for d in row_defs:
        out.append(d)
        out.extend(_BFSI_ALIAS_ROWS.get(d[0], []))
    return out


_QUARTER_ROW_DEFS = _with_bfsi_aliases(_QUARTER_ROW_DEFS)
_PL_ROW_DEFS = _QUARTER_ROW_DEFS   # same key set, annual + TTM
# cc#518 REVISION (founder, same session): P&L display trims to 6 key rows -- the rest stay in the
# payload (display=False) for the row charts / future use, never dropped from computation.
_PL_DISPLAY_KEYS = ("Sales", "Operating Profit", "OPM %", "Interest", "Net Profit", "EPS in Rs",
                    # cc#833: the BFSI aliases are display rows too — otherwise a financing company's
                    # P&L collapses to Interest / Net Profit / EPS with its top line hidden.
                    "Revenue", "Financing Profit", "Financing Margin %")

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


def _quarter_vals(cur, symbol, keys):
    """cc#832: oldest->newest series of one metric across QUARTERLY periods.

    Same dominant-variant rule as _annual_vals. `keys` is tried in priority order and the ONE key
    with the most non-null readings wins for the WHOLE series — resolving per row would silently
    splice a Sales quarter next to a Revenue quarter and call the join a growth rate. (BFSI files
    print "Revenue", industrials print "Sales"; BAJFINANCE has no Sales key at all.)"""
    cur.execute("""SELECT consolidated, metrics FROM fundamentals_history
                   WHERE symbol=%s AND section='quarters' AND period_type='quarter'
                   ORDER BY period_end ASC""", (symbol,))
    rows = cur.fetchall()
    if not rows:
        return []
    has_c = any(bool(r[0]) for r in rows)
    ms = [(r[1] or {}) for r in rows if bool(r[0]) == has_c]
    best, best_n = None, 0
    for key in keys:
        n = sum(1 for m in ms if _parse_metric(m.get(key)) is not None)
        if n > best_n:
            best, best_n = key, n
    if not best:
        return []
    return [_parse_metric(m.get(best)) for m in ms]


def _ttm_growth(series):
    """cc#832: TTM growth = sum(latest 4 quarters) vs sum(the 4 quarters before those).

    Returns None — rendered as "--" — rather than a number whenever the answer would be invented:
      * fewer than 8 quarters in the series (includes symbols with no quarters series at all)
      * any gap inside either 4-quarter window
      * a prior window that is negative or zero (cc#804 QoQ doctrine: a swing through zero has no
        meaningful percent)
    Deliberately NO fallback to the annual 1Y figure it replaces — a silent substitution would put
    an annual number under a column headed TTM."""
    vals = series or []
    if len(vals) < 8:
        return None
    cur4, prev4 = vals[-4:], vals[-8:-4]
    if any(v is None for v in cur4) or any(v is None for v in prev4):
        return None
    base = sum(prev4)
    if base <= 0:
        return None
    return round((sum(cur4) - base) / base * 100.0, 1)


def _rat_row(label, unit, values):
    if all(v is None for v in values):
        return None
    return {"label": label, "unit": unit, "chart_type": "line", "display": True, "values": values}


def _annual_frame(cur, symbol, section, limit=11):
    """cc#640: oldest->newest ANNUAL periods for the dominant variant of a section.
    Returns (labels, period_ends[date], [metrics dict]) so ratios can be derived year-wise."""
    cur.execute("""SELECT consolidated, period_label, period_end, metrics FROM fundamentals_history
                   WHERE symbol=%s AND section=%s AND period_type='annual' ORDER BY period_end ASC""",
                (symbol, section))
    rows = cur.fetchall()
    if not rows:
        return [], [], []
    has_c = any(bool(r[0]) for r in rows)
    filt = [r for r in rows if bool(r[0]) == has_c][-limit:]
    return ([r[1] for r in filt], [r[2] for r in filt], [(r[3] or {}) for r in filt])


def _label_map(cur, symbol, section):
    """{period_label: metrics} for the dominant annual variant — used to align BS/ratios to the P&L axis."""
    labels, _ends, metr = _annual_frame(cur, symbol, section)
    return {labels[i]: metr[i] for i in range(len(labels))}


def _mv(m, key):
    return _parse_metric(m.get(key)) if m else None


def _safe_div(a, b, pct=False, nd=2):
    if a is None or b in (None, 0) or b == 0.0:
        return None
    return round(a / b * (100.0 if pct else 1.0), nd)


def build_ratios_v2(cur, symbol: str, segment: str) -> Dict[str, Any]:
    """cc#638/640 RATIOS V2: bucketed sub-tables. cc#640 makes Profitability / Valuation / Solvency
    YEAR-WISE — annual FY columns (oldest->newest) plus a final 'Current' column from screener_raw —
    just like the financials tables, instead of the single 'Current' column cc#638 hardcoded. Derived
    from fundamentals_history annual series (P&L + BS + ratios) and FY-end close (raw_prices, last
    trading day <= period_end). Growth stays 1Y/3Y/5Y. Efficiency = the annual ratios section (Debtor/
    Inventory/Payable/CCC/WC days) minus ROCE (which moves into Profitability). Solvency HIDDEN for
    BFSI. Zero new scraping — every column comes from data already in the DB."""
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
    sales_s = _annual_vals(cur, sym, "profit-loss", "Sales")
    # cc#832: the Growth table's first column is TTM, built from the quarterly series.
    q_sales_s = _quarter_vals(cur, sym, ["Sales", "Revenue"])
    q_np_s = _quarter_vals(cur, sym, ["Net Profit"])

    def _last(s, n=1):
        return s[-n] if len(s) >= n else None
    mcap_sales = (round(k.get("mcap") / k.get("sales"), 2)
                  if (k.get("mcap") is not None and k.get("sales")) else None)

    # --- year-wise axis: P&L annual labels/ends; BS + ratios aligned by label; FY-end closes ----------
    pl_labels, pl_ends, pl_m = _annual_frame(cur, sym, "profit-loss", 11)
    bs_map = _label_map(cur, sym, "balance-sheet")
    ra_map = _label_map(cur, sym, "ratios")
    closes = {}
    if pl_ends:
        cur.execute("""SELECT t.e, (SELECT close FROM raw_prices rp WHERE rp.symbol=%s
                          AND rp.price_date <= t.e ORDER BY rp.price_date DESC LIMIT 1)
                       FROM unnest(%s::date[]) AS t(e)""", (sym, pl_ends))
        for pe, c in cur.fetchall():
            closes[str(pe)] = _parse_metric(c)

    n = len(pl_labels)
    roce_y, roe_y, opm_y, npm_y, at_y = [], [], [], [], []
    pe_y, pb_y, de_y, ic_y = [], [], [], []
    evebitda_y, mcaps_y, dy_y = [], [], []   # cc#691: year-wise EV/EBITDA, MCap/Sales, Div Yield
    for i in range(n):
        m = pl_m[i]
        bs = bs_map.get(pl_labels[i], {})
        ra = ra_map.get(pl_labels[i], {})
        np_ = _mv(m, "Net Profit"); sales = _mv(m, "Sales"); op = _mv(m, "Operating Profit")
        if sales is None:
            sales = _mv(m, "Revenue")   # cc#691: banks/NBFCs report the top line as "Revenue", not "Sales"
        eps = _mv(m, "EPS in Rs"); intr = _mv(m, "Interest"); pbt = _mv(m, "Profit before tax")
        eqcap = _mv(bs, "Equity Capital"); res = _mv(bs, "Reserves")
        ta = _mv(bs, "Total Assets"); borrow = _mv(bs, "Borrowings")
        nw = (eqcap + res) if (eqcap is not None and res is not None) else None
        close = closes.get(str(pl_ends[i]))
        # Profitability
        roce_y.append(_mv(ra, "ROCE %"))
        roe_y.append(_safe_div(np_, nw, pct=True, nd=1))
        opm_y.append(_safe_div(op, sales, pct=True, nd=1))
        npm_y.append(_safe_div(np_, sales, pct=True, nd=1))
        at_y.append(_safe_div(sales, ta, nd=2))
        # Valuation (FY-end close basis). BVPS = net worth per share = (nw/np)*eps (avoids share count).
        pe_y.append(_safe_div(close, eps, nd=1) if (eps and eps > 0) else None)
        bvps = ((nw / np_) * eps if (nw is not None and np_ and np_ > 0 and eps) else None)
        pb_y.append(_safe_div(close, bvps, nd=2) if (bvps and bvps > 0) else None)
        # cc#691: MCap(cr) = close * shares, shares = np/eps (avoids a share-count series). EV = MCap +
        # Borrowings (gross debt — the historical BS series has no clean cash line, so net debt isn't
        # reconstructable; a consistent gross-debt basis across years). EBITDA = Operating Profit.
        # DivYield% = DPS/price = (EPS * payout%/100)/price*100 = EPS*payout/price. All None pre-price-window.
        mcap_cr = (close * np_ / eps) if (close and np_ is not None and eps and eps > 0) else None
        payout = _mv(m, "Dividend Payout %")
        ev = (mcap_cr + borrow) if (mcap_cr is not None and borrow is not None) else mcap_cr
        evebitda_y.append(_safe_div(ev, op, nd=1) if (ev is not None and op and op > 0) else None)
        mcaps_y.append(_safe_div(mcap_cr, sales, nd=2) if (mcap_cr is not None and sales) else None)
        dy_y.append(round(payout * eps / close, 2) if (payout is not None and eps and close and close > 0) else None)
        # Solvency
        de_y.append(_safe_div(borrow, nw, nd=2))
        ebit = (pbt + intr) if (pbt is not None and intr is not None) else None
        ic_y.append(_safe_div(ebit, intr, nd=1) if (intr and intr > 0) else None)

    yr_periods = pl_labels + ["Current"]
    buckets = []

    def _add(title, periods, rows):
        rows = [x for x in rows if x is not None]
        if rows:
            buckets.append({"title": title, "table": {"periods": periods, "rows": rows}})

    def _yr_row(label, unit, hist, current):
        """cc#833: a year-wise row whose HISTORY is empty across every FY does not render, even when
        a Current value exists. That combination — no history, one Current cell — is precisely the
        all-dash ROCE/OPM row BAJFINANCE showed: screener_raw carries an opm/roce number for every
        listed company, so the Current cell alone kept a row alive whose metric the filing never
        publishes. An all-dash row against real stored data reads as missing data; it is actually
        the wrong metric."""
        if all(v is None for v in hist):
            return None
        return _rat_row(label, unit, list(hist) + [current])

    # cc#833: BFSI margin history, sourced from the SAME annual P&L frame the other year-wise rows
    # use, so it lines up column-for-column with them.
    fin_margin_y = [_mv(m, "Financing Margin %") for m in pl_m]
    fin_profit_y = [_mv(m, "Financing Profit") for m in pl_m]

    # 1) GROWTH — cc#832: TTM / 3Y CAGR / 5Y CAGR, two rows.
    # The 1Y column is now TTM (latest 4 quarters vs the 4 before), which is what "the last year of
    # trading" actually means once a symbol has reported past its FY end — the annual 1Y went stale
    # the moment Q1 landed. The 3Y/5Y CAGR columns are untouched (annual series, screener CAGR
    # preferred where present). EPS Growth is DELETED: Net Profit and EPS are the same story told
    # twice, and its 3Y/5Y cells were hardcoded None anyway — a row that was 2/3 empty by design.
    _add("Growth", ["TTM", "3Y CAGR", "5Y CAGR"], [
        _rat_row("Sales Growth", "pct", [_ttm_growth(q_sales_s),
                                          k.get("sg3") if k.get("sg3") is not None else _cagr(_last(sales_s), _last(sales_s, 4), 3),
                                          k.get("sg5") if k.get("sg5") is not None else _cagr(_last(sales_s), _last(sales_s, 6), 5)]),
        _rat_row("Net Profit Growth", "pct", [_ttm_growth(q_np_s),
                                              _cagr(_last(np_s), _last(np_s, 4), 3), _cagr(_last(np_s), _last(np_s, 6), 5)]),
    ])
    # 2) PROFITABILITY — YEAR-WISE + Current.
    # cc#833: rows now survive on the strength of their HISTORY, not on a Current cell screener_raw
    # hands out to everyone, and the BFSI margin row sits where OPM % would be. Its Current cell is
    # deliberately BLANK: screener_raw's opm for BAJFINANCE is 68.1 (a quarterly figure on a
    # different base) against an annual Financing Margin history of ~33 — printing it at the end of
    # this row would be a basis break dressed as a trend. Rule chosen: DROP the Current cell rather
    # than mix bases. NPM % and Asset Turnover already did exactly this.
    _add("Profitability", yr_periods, [
        _yr_row("ROCE %", "pct", roce_y, k.get("roce")),
        _yr_row("ROE %",  "pct", roe_y,  k.get("roe")),
        _yr_row("OPM %",  "pct", opm_y,  k.get("opm")),
        _yr_row("Financing Margin %", "pct", fin_margin_y, None),
        _yr_row("Financing Profit",   "cr",  fin_profit_y, None),
        _yr_row("NPM %",  "pct", npm_y,  None),
        _yr_row("Asset Turnover", "x", at_y, None),
    ])
    # 3) VALUATION — cc#691: PE/PB/EV-EBITDA/MCap-Sales/Div-Yield ALL year-wise now (FY-end close +
    # Current). EV/EBITDA is SUPPRESSED for BFSI (meaningless for financials — same _is_bfsi gate as the
    # Solvency D/E + Interest-Coverage exclusion); MCap/Sales + Div Yield stay for BFSI.
    _val_rows = [
        _yr_row("PE", "x", pe_y, k.get("pe")),
        _yr_row("PB", "x", pb_y, k.get("pb")),
    ]
    if not _is_bfsi(segment):
        _val_rows.append(_yr_row("EV/EBITDA", "x", evebitda_y, k.get("evebitda")))
    _val_rows.append(_yr_row("MCap/Sales", "x", mcaps_y, mcap_sales))
    _val_rows.append(_yr_row("Dividend Yield", "pct", dy_y, k.get("divyield")))
    _add("Valuation", yr_periods, _val_rows)
    # 4) SOLVENCY — YEAR-WISE + Current, HIDDEN for BFSI
    if not _is_bfsi(segment):
        _add("Solvency", yr_periods, [
            _yr_row("Debt / Equity", "x", de_y, k.get("de")),
            _yr_row("Interest Coverage", "x", ic_y, k.get("intcov")),
        ])
    # 5) EFFICIENCY — annual ratios section (days metrics), ROCE excluded (now under Profitability)
    _ra_all = _all_section(cur, sym, "ratios", "annual")
    eff_rows = _variant_rows(_ra_all, any(bool(x[0]) for x in _ra_all), 12) if _ra_all else []
    if eff_rows:
        eff_defs = [d for d in _RATIO_ROW_DEFS if d[0] != "ROCE %"]
        eff_tbl = _build_table(eff_rows, eff_defs)
        if eff_tbl["rows"]:
            buckets.append({"title": "Efficiency", "table": eff_tbl})

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


def _variant_or_fallback(all_rows, want_consolidated, limit):
    """cc#639: rows for the requested variant; if that variant has ZERO rows for this section but the
    OTHER variant has rows, fall back to the other variant and return a basis_note ("Consolidated" /
    "Standalone") so single-basis sections (SHP, screener ratios) render on BOTH toggles with a tag.
    Founder rule: a section not split standalone/consolidated shows in both places."""
    rows = _variant_rows(all_rows, want_consolidated, limit)
    if rows:
        return rows, None
    other = _variant_rows(all_rows, not want_consolidated, limit)
    if other:
        return other, ("Consolidated" if not want_consolidated else "Standalone")
    return [], None


def _build_one_variant(sections, want_consolidated):
    """cc#636/639: full financials block for one variant. Each section falls back to the other variant
    when the requested one has no rows (with a per-section basis_note). Returns None only if NEITHER
    variant has any rows anywhere. change_2: annual P&L renders the full canonical row set (no trim)."""
    q_rows,  q_note  = _variant_or_fallback(sections["quarters"],      want_consolidated, 13)
    pl_rows, pl_note = _variant_or_fallback(sections["profit-loss"],   want_consolidated, 12)
    bs_rows, bs_note = _variant_or_fallback(sections["balance-sheet"], want_consolidated, 12)
    cf_rows, cf_note = _variant_or_fallback(sections["cash-flow"],     want_consolidated, 12)
    ra_rows, ra_note = _variant_or_fallback(sections["ratios"],        want_consolidated, 12)
    sh_rows, sh_note = _variant_or_fallback(sections["shareholding"],  want_consolidated, 12)
    if not any([q_rows, pl_rows, bs_rows, cf_rows, ra_rows, sh_rows]):
        return None
    out = {"basis": "Consolidated" if want_consolidated else "Standalone",
           "quarterly": None, "profit_loss": None, "balance_sheet": None,
           "cash_flow": None, "ratios": None, "shareholding": None}

    def _tag(tbl, note):
        if tbl is not None and note:
            tbl["basis_note"] = note
        return tbl
    if q_rows:
        out["quarterly"] = _tag(_build_table(q_rows, _QUARTER_ROW_DEFS), q_note)
    if pl_rows:
        pl_table = _build_table(pl_rows, _PL_ROW_DEFS)
        ttm = _build_ttm(q_rows) if q_rows else None
        pl_table["ttm"] = ttm is not None
        if ttm is not None:
            pl_table["periods"].append("TTM")
            for row in pl_table["rows"]:
                row["values"].append(ttm.get(row["label"]))
        out["profit_loss"] = _tag(pl_table, pl_note)
    if bs_rows:
        out["balance_sheet"] = _tag(_build_table(bs_rows, _BS_ROW_DEFS, display_keys=_BS_DISPLAY_KEYS), bs_note)
    if cf_rows:
        out["cash_flow"] = _tag(_build_table(cf_rows, _CF_ROW_DEFS), cf_note)
    if ra_rows:
        out["ratios"] = _tag(_build_table(ra_rows, _RATIO_ROW_DEFS), ra_note)
    if sh_rows:
        out["shareholding"] = _tag(_shareholding_table(sh_rows), sh_note)
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


import re as _ops_re
_OPS_Q_RE = _ops_re.compile(r"^Q([1-4])FY(\d{2,4})$", _ops_re.I)
_OPS_FY_RE = _ops_re.compile(r"^FY(\d{2,4})$", _ops_re.I)


def _ops_is_quarter(q) -> bool:
    return bool(_OPS_Q_RE.match(str(q or "").upper().replace(" ", "")))


def _ops_is_annual(q) -> bool:
    return bool(_OPS_FY_RE.match(str(q or "").upper().replace(" ", "")))


def _ops_annual_key(q):
    """'FY26' -> 2026 for chronological sort; unrecognised -> 0."""
    m = _OPS_FY_RE.match(str(q or "").upper().replace(" ", ""))
    if not m:
        return 0
    fy = int(m.group(1))
    return fy + 2000 if fy < 100 else fy


def _ops_yoy_label(q):
    """cc#715: 'Q1FY27' -> 'Q1FY26' (same quarter, one FY back). None if not a quarter label.
    Preserves the FY digit-width (canonical spine is 2-digit FYyy) so the lookup matches exactly —
    NEVER substitute a different quarter (YoY is blank when the year-ago quarter is absent)."""
    s = str(q or "").upper().replace(" ", "")
    m = _OPS_Q_RE.match(s)
    if not m:
        return None
    fy = m.group(2)
    return "Q%sFY%s" % (m.group(1), str(int(fy) - 1).zfill(len(fy)))


def _ops_growth_pct(cur_v, prev_v):
    """cc#715: percent growth cur vs prev (None if either missing or prev==0). Rounded 1 dp."""
    if cur_v is None or prev_v is None or prev_v == 0:
        return None
    return round((cur_v - prev_v) / abs(prev_v) * 100.0, 1)


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


# cc#780 / id=7132 BFSI split. Banking_NBFC is a REGISTRY sector, never a peer pool: banks and
# MFI-NBFCs have structurally different NIM/ROA, so a combined pool makes every benchmark false.
_BANK_INDUSTRIES = {"Private Sector Bank", "Public Sector Bank", "Other Bank", "Other Sector Bank"}
_NBFC_INDUSTRIES = {"Non Banking Financial Company (NBFC)", "Housing Finance Company",
                    "Financial Institution", "Microfinance Institutions"}
_BANK_ONLY_OPS = {"casa_pct", "casa", "deposit_growth", "deposit_growth_yoy", "cost_to_income"}


def _bfsi_segment(cur, symbol: str) -> Optional[str]:
    """BANKS | NBFC | None (non-BFSI or unclassified). id=7132 fallback: an unmatched Industry is
    NBFC when the symbol has no CASA row, else BANKS."""
    try:
        cur.execute('SELECT "Industry" FROM screener_raw WHERE UPPER(nse_code)=%s LIMIT 1', (symbol,))
        r = cur.fetchone()
        ind = (r[0] or "").strip() if r else ""
    except Exception:
        return None
    if ind in _BANK_INDUSTRIES:
        return "BANKS"
    if ind in _NBFC_INDUSTRIES:
        return "NBFC"
    try:
        cur.execute("""SELECT 1 FROM sector_ops_metrics WHERE symbol=%s
                        AND metric_name IN ('casa_pct','casa') AND metric_value IS NOT NULL LIMIT 1""", (symbol,))
        return "BANKS" if cur.fetchone() else None
    except Exception:
        return None


def _bfsi_segment_symbols(cur, seg: str) -> Optional[List[str]]:
    """All symbols in the BANKS / NBFC segment, so the peer pool never crosses the boundary."""
    inds = _BANK_INDUSTRIES if seg == "BANKS" else _NBFC_INDUSTRIES if seg == "NBFC" else None
    if not inds:
        return None
    try:
        cur.execute('SELECT UPPER(nse_code) FROM screener_raw WHERE "Industry" = ANY(%s) AND nse_code IS NOT NULL',
                    (list(inds),))
        return [r[0] for r in cur.fetchall()] or None
    except Exception:
        return None


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
            # cc#715: split the QxFYyy quarterly spine from FYyy annual-only labels. Quarterly rows get
            # trailing-5 columns + QoQ%/YoY% (label-aware, % growth); FY annual rows render in a separate
            # section with NO growth math. `direction` (sector_kpi_registry) is passed through so the
            # frontend can colour growth direction-aware (lower-is-better KPIs inverted; unknown -> none).
            all_qs = {r[3] for r in rows if r[3]}
            q_labels = sorted([q for q in all_qs if _ops_is_quarter(q)], key=_ops_quarter_key)
            fy_labels = sorted([q for q in all_qs if _ops_is_annual(q)], key=_ops_annual_key)
            disp_quarters = q_labels[-5:]
            disp_fy = fy_labels[-3:]
            by_metric: Dict[str, Dict[str, Any]] = {}
            units: Dict[str, Any] = {}
            for _sector, mname, unit, q, val, _conf in rows:
                by_metric.setdefault(mname, {})[q] = _f(val)
                units.setdefault(mname, unit)
            dir_map: Dict[str, Any] = {}
            try:
                cur.execute("SELECT metric_name, direction FROM sector_kpi_registry WHERE sector=%s", (out["sector"],))
                dir_map = {m: d for m, d in cur.fetchall()}
            except Exception:
                dir_map = {}
            prev_q_label = q_labels[-2] if len(q_labels) >= 2 else None
            latest_q_label = q_labels[-1] if q_labels else None
            yoy_q_label = _ops_yoy_label(latest_q_label)
            metric_rows = []
            annual_rows = []
            for mname, qmap in by_metric.items():
                direction = dir_map.get(mname)
                q_vals = [qmap.get(q) for q in disp_quarters]
                if any(v is not None for v in q_vals):
                    # cc#780 fix_1: the COMPANY column gets the same per-metric fallback as the peers —
                    # its own newest quarter FOR THIS METRIC, tagged with that quarter, rather than a
                    # single card-wide quarter that blanks every metric not reported in it.
                    _own_qs = [q for q, v in qmap.items() if v is not None and _ops_is_quarter(q)]
                    own_q = max(_own_qs, key=_ops_quarter_key) if _own_qs else None
                    latest = qmap.get(own_q) if own_q else qmap.get(latest_q_label)
                    qoq = _ops_growth_pct(latest, qmap.get(prev_q_label)) if prev_q_label else None
                    yoy = _ops_growth_pct(latest, qmap.get(yoy_q_label)) if (yoy_q_label in qmap) else None
                    metric_rows.append({"label": _ops_pretty(mname), "metric_name": mname,
                                        "unit": units.get(mname), "values": q_vals,
                                        "latest": latest, "as_of": own_q,   # cc#780 per-cell as-of
                                        "qoq": qoq, "yoy": yoy, "direction": direction})
                elif disp_fy and any(qmap.get(fy) is not None for fy in disp_fy):
                    annual_rows.append({"label": _ops_pretty(mname), "metric_name": mname,
                                        "unit": units.get(mname), "direction": direction,
                                        "values": [qmap.get(fy) for fy in disp_fy]})
            metric_rows.sort(key=lambda r: r["label"])
            annual_rows.sort(key=lambda r: r["label"])
            if metric_rows or annual_rows:
                out.update({"has_data": True, "periods": disp_quarters, "rows": metric_rows,
                            "annual_periods": disp_fy, "annual_rows": annual_rows})
                # cc#683/687: peer selection for the dual-mode (Peers / Standalone) card. The peer window
                # is the last 4 reported quarters GLOBALLY. cc#687 fix: pool at the COARSE OPS SECTOR, not
                # the fine gvm_scores.segment — a narrow segment ("Pharma - Bulk & API" = 2 names) collapsed
                # the Peers table to 1 column, while the sector ("Pharma" = 33) is the correct broad peer
                # group. Pool = top-10 same-sector names BY GVM with ANY row in the window; then RANK by
                # quarter-recency first (freshest data wins), GVM as the tiebreak, and take up to 5 so the
                # table shows >=4 columns (company + >=3 peers) whenever that many exist — else all available
                # (honest). Each peer carries its own most-recent quarter tag; a missing KPI = null (--).
                # cc#715: window widened 4 -> 5 quarters so each peer's QoQ (adjacent) and YoY
                # (same quarter one FY back) can be computed per its OWN series, not just its latest.
                # cc#780: window = last 2 REPORTED quarters (id=7132 quarter_window.peer_pool), not 5.
                cur.execute("SELECT DISTINCT quarter FROM sector_ops_metrics WHERE metric_value IS NOT NULL AND quarter IS NOT NULL")
                window = sorted([q for q in (r[0] for r in cur.fetchall()) if _ops_is_quarter(q)], key=_ops_quarter_key)[-2:]
                sector = out.get("sector")
                # cc#780 / id=7132 DISPLAY_SEPARATE: Banking_NBFC is NOT a peer pool. Split it into
                # BANKS vs NBFC by screener_raw.Industry and benchmark within the OWN segment only —
                # the 01-Aug card mixed KARURVYSYA/FEDERALBNK (banks) with CREDITACC/SATIN (MFI-NBFCs),
                # whose NIM/ROA are structurally different, making every comparison meaningless.
                seg = _bfsi_segment(cur, symbol)
                out["segment"] = seg or (sector.replace("_", " ") if sector else None)
                seg_syms = _bfsi_segment_symbols(cur, seg) if seg else None
                # cc#780 fix_3: bank-only metrics never render on an NBFC company card.
                if seg == "NBFC":
                    metric_rows = [r for r in metric_rows if r["metric_name"] not in _BANK_ONLY_OPS]
                    out["rows"] = metric_rows
                core_metrics = [r["metric_name"] for r in metric_rows]
                pool: List[tuple] = []   # (symbol, gvm_score)
                if sector and window and core_metrics:
                    # cc#780 fix_4: a symbol only earns a peer column if it holds >=1 IN-WINDOW value on
                    # the segment's core metrics; otherwise take the next GVM name. Ranked by GVM (the
                    # locked rule) — NOT by recency, which previously let a stale-but-fresh-looking name
                    # outrank a better peer.
                    q = """SELECT g.symbol, g.gvm_score FROM gvm_scores g
                           WHERE g.score_date=(SELECT MAX(score_date) FROM gvm_scores)
                             AND g.symbol<>%s
                             AND EXISTS (SELECT 1 FROM sector_ops_metrics s
                                         WHERE s.symbol=g.symbol AND s.sector=%s AND s.quarter=ANY(%s)
                                           AND s.metric_name=ANY(%s) AND s.metric_value IS NOT NULL)"""
                    params: List[Any] = [symbol, sector, window, core_metrics]
                    if seg_syms:
                        q += " AND g.symbol = ANY(%s)"
                        params.append(seg_syms)
                    q += " ORDER BY g.gvm_score DESC NULLS LAST LIMIT 5"
                    cur.execute(q, params)
                    pool = [(r[0], r[1] if r[1] is not None else -1.0) for r in cur.fetchall()]
                if pool:
                    peers = [p for p, _ in pool]
                    cur.execute("""SELECT symbol, quarter, metric_name, metric_value FROM sector_ops_metrics
                                   WHERE symbol = ANY(%s) AND sector=%s AND quarter = ANY(%s) AND metric_value IS NOT NULL""",
                                (peers, sector, window))
                    peer_series: Dict[tuple, Dict[str, Optional[float]]] = {}   # (peer,metric) -> {quarter: value}
                    peer_q: Dict[str, str] = {}   # per peer -> newest quarter seen (header tag only)
                    for psym, q_, mname, val in cur.fetchall():
                        peer_series.setdefault((psym, mname), {})[q_] = _f(val)
                        if psym not in peer_q or _ops_quarter_key(q_) > _ops_quarter_key(peer_q[psym]):
                            peer_q[psym] = q_
                    # cc#780 fix_1 PER-METRIC QUARTER FALLBACK. The old code read ser.get(lq) where lq was
                    # the peer's newest quarter across ALL metrics — so ONE stray newer row blanked every
                    # other metric for that peer (the 01-Aug all-"--" SATIN column, while the DB held its
                    # Q4FY26 credit_cost/credit_growth/gnpa). Each CELL now independently takes the latest
                    # quarter available FOR THAT (peer, metric) and carries its OWN as-of tag.
                    for row in metric_rows:
                        pv, pa = [], []
                        for p in peers:
                            ser = peer_series.get((p, row["metric_name"]), {})
                            if ser:
                                cq = max(ser.keys(), key=_ops_quarter_key)
                                pv.append(ser[cq]); pa.append(cq)
                            else:
                                pv.append(None); pa.append(None)
                        row["peers"] = pv
                        row["peer_as_of"] = pa      # cc#780: per-CELL as-of quarter
                        row.pop("peer_qoq", None)   # cc#780 fix_2: QoQ/YoY killed (id=7117 single level)
                        row.pop("peer_yoy", None)
                    out["peers"] = peers
                    out["peer_quarters"] = {p: peer_q[p] for p in peers}
                    out["peer_quarter"] = window[-1]                        # legacy field retained
                    # cc#780 fix_5: percentile/benchmark badge needs >=2 in-window peers holding the
                    # metric (id=7132 min_peers=2); below that the UI shows value only + thin-peers.
                    for row in metric_rows:
                        row["peer_n"] = sum(1 for v in row.get("peers") or [] if v is not None)
                        row["thin_peers"] = row["peer_n"] < 2

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
