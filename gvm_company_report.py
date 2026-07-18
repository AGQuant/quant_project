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
