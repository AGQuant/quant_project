"""
gvm_page_extras.py — DB-only enrichment block for the GVM company page (v3).

v3 (12-Jun-2026 night):
  + DMA-50 / DMA-200 displayed as deviation % from price (not raw price level).
  + Week/Month/Year returns: fallback to raw_prices for non-futures stocks
    (v8_metrics only covers the 210 futures universe).
  + Annual Upside computed via engine formula: fy27_growth × (pe/hist_pe).
    Uses input_raw.fy27_growth (1,536 stocks populated).
  + Universe pivot lookup: v8_paper_pivots now has rows for ALL 1,720 stocks
    (gvm_universe_pivots job), so pivot block works for non-futures too.

v2 (12-Jun-2026 evening):
  + segment context block, z-score, ladder enrichment, cumulative price series.

Design rules honoured:
  - own file, main.py untouched (wiring-only rule)
  - informational facts only — NO verdicts (Trade Check stays independent)
"""

import os
import logging
from datetime import date
from typing import Optional, Dict, Any, List

import psycopg

log = logging.getLogger("scorr.gvm_extras")

BLACKOUT_DAYS = 5
AD_ACCUM_RATIO = 1.30
AD_DIST_RATIO = 0.77


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _r(v, d=2) -> Optional[float]:
    f = _f(v)
    return round(f, d) if f is not None else None


def _tier1_auto(m: Dict[str, Any], ad_ratio: Optional[float]) -> Dict[str, Any]:
    """7 Tier-1 rules computable per-symbol from v8_metrics + volume A/D.
    Manual chart-judgment rules (5M recovery, 1D pattern, reversal-only,
    market mood) are intentionally excluded so the count stays honest."""
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: Optional[bool]):
        checks.append({"name": name, "pass": ok})

    gvm = _f(m.get("gvm_score"))
    add("GVM >= 7.0", None if gvm is None else gvm >= 7.0)

    sw, sm = _f(m.get("sector_week")), _f(m.get("sector_month"))
    add("Sector aligned (wk & mo > 0)",
        None if sw is None or sm is None else (sw > 0 and sm > 0))

    mas = [_f(m.get("dma_20")), _f(m.get("dma_50")), _f(m.get("dma_200"))]
    known = [x for x in mas if x is not None]
    add("2 of 3 MAs above",
        None if len(known) < 2 else sum(1 for x in known if x > 0) >= 2)

    rm, rw = _f(m.get("rsi_month")), _f(m.get("rsi_weekly"))
    add("RSI M>=50 & W>=50",
        None if rm is None or rw is None else (rm >= 50 and rw >= 50))

    wr, mr = _f(m.get("week_return")), _f(m.get("month_return"))
    add("Week & Month return > 0",
        None if wr is None or mr is None else (wr > 0 and mr > 0))

    dr = _f(m.get("daily_rsi"))
    add("Daily RSI < 80", None if dr is None else dr < 80)

    add("Volume buying (A/D >= 1)",
        None if ad_ratio is None else ad_ratio >= 1.0)

    passed = sum(1 for c in checks if c["pass"] is True)
    return {"passed": passed, "total": len(checks), "checks": checks}


def _ad_verdict(ratio: Optional[float]) -> Optional[str]:
    if ratio is None:
        return None
    if ratio >= AD_ACCUM_RATIO:
        return "ACCUMULATION"
    if ratio <= AD_DIST_RATIO:
        return "DISTRIBUTION"
    return "NEUTRAL"


def _compute_upside(fy27, pe, hist_pe):
    """Engine formula: upside = fy27_growth * (pe / hist_pe).
    Multiplier capped at 1.0 if hist_pe is invalid (matches gvm_nightly._pu)."""
    fy = _f(fy27)
    if fy is None:
        return None
    if fy == 0:
        return 0.0
    p, h = _f(pe), _f(hist_pe)
    mult = (p / h) if (p is not None and h is not None and h > 0) else 1.0
    return round(fy * mult, 2)


def _compute_returns_from_prices(cur, syms: List[str]) -> Dict[str, Dict[str, Optional[float]]]:
    """One query: week (5d), month (22d), year (252d) % returns from raw_prices.
    Works for ALL stocks (universal fallback for non-futures)."""
    out: Dict[str, Dict[str, Optional[float]]] = {}
    if not syms:
        return out
    try:
        cur.execute("""
            WITH ranked AS (
                SELECT symbol, price_date, close,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
                FROM raw_prices
                WHERE symbol = ANY(%s)
                  AND price_date >= CURRENT_DATE - INTERVAL '400 days'
            )
            SELECT symbol,
                   MAX(CASE WHEN rn = 1 THEN close END)   AS latest,
                   MAX(CASE WHEN rn = 6 THEN close END)   AS c_5d,
                   MAX(CASE WHEN rn = 23 THEN close END)  AS c_22d,
                   MAX(CASE WHEN rn = 253 THEN close END) AS c_252d
            FROM ranked
            GROUP BY symbol
        """, (syms,))
        for s, latest, c5, c22, c252 in cur.fetchall():
            l, c5f, c22f, c252f = _f(latest), _f(c5), _f(c22), _f(c252)
            out[s] = {
                "week_return":  round((l / c5f - 1) * 100, 2) if l and c5f else None,
                "month_return": round((l / c22f - 1) * 100, 2) if l and c22f else None,
                "year_return":  round((l / c252f - 1) * 100, 1) if l and c252f else None,
            }
    except Exception as e:
        log.warning(f"raw_prices returns failed: {e}")
    return out


def build_page_extras(symbol: str, ladder_symbols: List[str],
                      segment: Optional[str] = None) -> Dict[str, Any]:
    """Compute the full extras block. Every sub-block is best-effort —
    one failed query never kills the page."""
    symbol = (symbol or "").strip().upper()
    syms = sorted({s for s in (ladder_symbols or []) if s} | {symbol})

    extras: Dict[str, Any] = {}
    ladder_extra: Dict[str, Dict[str, Any]] = {s: {} for s in syms}

    try:
        with _conn() as conn, conn.cursor() as cur:

            # ── 1. GVM trend 13d, G/V/M decomposed ────────────────────────
            try:
                cur.execute("""
                    SELECT score_date::text, gvm_score, g_score, v_score, m_score
                    FROM gvm_history
                    WHERE symbol = %s
                    ORDER BY score_date DESC LIMIT 13
                """, (symbol,))
                rows = cur.fetchall()
                extras["trend"] = [
                    {"d": r[0], "gvm": _r(r[1]), "g": _r(r[2]),
                     "v": _r(r[3]), "m": _r(r[4])}
                    for r in reversed(rows)
                ]
            except Exception as e:
                log.warning(f"trend failed {symbol}: {e}")
                extras["trend"] = []

            # ── 2. 30-day volume A/D for ALL ladder symbols (one query) ───
            ad_map: Dict[str, Dict[str, float]] = {}
            try:
                cur.execute("""
                    WITH r AS (
                        SELECT symbol, price_date, close, volume,
                               close - LAG(close) OVER
                                   (PARTITION BY symbol ORDER BY price_date) AS chg,
                               ROW_NUMBER() OVER
                                   (PARTITION BY symbol ORDER BY price_date DESC) AS rn
                        FROM raw_prices
                        WHERE symbol = ANY(%s)
                          AND price_date >= CURRENT_DATE - INTERVAL '75 days'
                    )
                    SELECT symbol,
                           SUM(CASE WHEN chg > 0 THEN volume ELSE 0 END) AS up_vol,
                           SUM(CASE WHEN chg < 0 THEN volume ELSE 0 END) AS down_vol
                    FROM r
                    WHERE rn <= 30 AND chg IS NOT NULL
                    GROUP BY symbol
                """, (syms,))
                for s, up, dn in cur.fetchall():
                    up, dn = _f(up) or 0.0, _f(dn) or 0.0
                    ratio = round(up / dn, 2) if dn > 0 else (None if up == 0 else 99.0)
                    ad_map[s] = {"up": up, "dn": dn, "ratio": ratio}
            except Exception as e:
                log.warning(f"ad_map failed: {e}")

            # main symbol daily bars (no cumulative line — removed v3 per feedback)
            vol_block: Dict[str, Any] = {}
            try:
                cur.execute("""
                    WITH r AS (
                        SELECT price_date, close, volume,
                               close - LAG(close) OVER (ORDER BY price_date) AS chg
                        FROM raw_prices
                        WHERE symbol = %s
                          AND price_date >= CURRENT_DATE - INTERVAL '75 days'
                        ORDER BY price_date
                    )
                    SELECT price_date::text, close, volume, chg
                    FROM r WHERE chg IS NOT NULL
                    ORDER BY price_date DESC LIMIT 30
                """, (symbol,))
                rows = list(reversed(cur.fetchall()))
                bars = []
                base_close = _f(rows[0][1]) if rows else None
                last_close = _f(rows[-1][1]) if rows else None
                for d, close, vol, chg in rows:
                    bars.append({"d": d, "v": _f(vol) or 0.0,
                                 "up": (_f(chg) or 0.0) > 0,
                                 "close": _f(close)})
                ad = ad_map.get(symbol, {})
                biggest = max(bars, key=lambda b: b["v"]) if bars else None
                total_ret = (round((last_close / base_close - 1) * 100, 2)
                             if last_close and base_close else None)
                vol_block = {
                    "up_vol": ad.get("up"), "down_vol": ad.get("dn"),
                    "ratio": ad.get("ratio"),
                    "verdict": _ad_verdict(ad.get("ratio")),
                    "bars": bars,
                    "total_return_pct": total_ret,
                    "biggest_day": ({"d": biggest["d"], "v": biggest["v"],
                                     "direction": "UP" if biggest["up"] else "DOWN"}
                                    if biggest else None),
                }
            except Exception as e:
                log.warning(f"vol bars failed {symbol}: {e}")
            extras["volume"] = vol_block or None

            # ── 3. Pivot levels (rolling-5d, now universe-wide via universe pivots job)
            try:
                cur.execute("""
                    SELECT pivot_date::text, pp, r1, s1, r2, s2
                    FROM v8_paper_pivots
                    WHERE symbol = %s
                    ORDER BY pivot_date DESC LIMIT 1
                """, (symbol,))
                row = cur.fetchone()
                extras["pivot"] = ({
                    "pivot_date": row[0], "pp": _r(row[1]), "r1": _r(row[2]),
                    "s1": _r(row[3]), "r2": _r(row[4]), "s2": _r(row[5]),
                } if row else None)
            except Exception as e:
                log.warning(f"pivot failed {symbol}: {e}")
                extras["pivot"] = None

            # ── 4. 52-week range position ─────────────────────────────────
            try:
                cur.execute("""
                    SELECT MAX(high), MIN(low)
                    FROM raw_prices
                    WHERE symbol = %s
                      AND price_date >= CURRENT_DATE - INTERVAL '365 days'
                """, (symbol,))
                row = cur.fetchone()
                hi, lo = _f(row[0]), _f(row[1])
                extras["range52"] = ({"hi": _r(hi), "lo": _r(lo)}
                                     if hi and lo and hi > lo else None)
            except Exception as e:
                log.warning(f"range52 failed {symbol}: {e}")
                extras["range52"] = None

            # ── 5. GVM universe percentile + z-score ──────────────────────
            try:
                cur.execute("""
                    SELECT AVG(gvm_score)::float, STDDEV_SAMP(gvm_score)::float,
                           COUNT(*) AS total
                    FROM gvm_scores
                    WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)
                      AND gvm_score IS NOT NULL
                """)
                mu, sd, total = cur.fetchone()
                cur.execute("""
                    SELECT COUNT(*) FROM gvm_scores
                    WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)
                      AND gvm_score > (SELECT gvm_score FROM gvm_scores
                                        WHERE symbol = %s
                                          AND score_date = (SELECT MAX(score_date)
                                                            FROM gvm_scores))
                """, (symbol,))
                better = cur.fetchone()[0]
                cur.execute("""
                    SELECT gvm_score FROM gvm_scores
                    WHERE symbol = %s
                      AND score_date = (SELECT MAX(score_date) FROM gvm_scores)
                """, (symbol,))
                row = cur.fetchone()
                my_gvm = _f(row[0]) if row else None
                z = (round((my_gvm - mu) / sd, 2)
                     if my_gvm is not None and mu and sd else None)
                extras["percentile"] = ({
                    "universe_rank": int(better) + 1,
                    "universe_total": int(total),
                    "top_pct": round((int(better) + 1) / int(total) * 100, 1),
                    "mean": _r(mu, 2), "stdev": _r(sd, 2),
                    "z_score": z,
                } if total else None)
            except Exception as e:
                log.warning(f"percentile/z failed {symbol}: {e}")
                extras["percentile"] = None

            # ── 6. Flow + valuation extras + ladder enrichment ────────────
            # Also pull fy27_growth from input_raw for Annual Upside formula.
            try:
                cur.execute("""
                    SELECT s.nse_code, s.pe, s.historical_pe, s.segment_pe,
                           s."Price to book value", s."PEG Ratio", s."EVEBITDA",
                           s."Cfo by Pat", s.dividend_yield,
                           s.fii_holding, s.fii_change, s.dii_holding, s.dii_change,
                           s."Promoter holding", s.return_1y, s."Return on equity",
                           s.opm, s.price, s.dma_50, s.dma_200,
                           i.fy27_growth
                    FROM screener_raw s
                    LEFT JOIN input_raw i ON i.nse_code = s.nse_code
                    WHERE s.nse_code = ANY(%s)
                """, (syms,))
                for r in cur.fetchall():
                    (s, pe, hpe, spe, pb, peg, ev, cfo, dy,
                     fii, fii_c, dii, dii_c, prom, r1y, roe, opm,
                     price, dma50, dma200, fy27) = r
                    ladder_extra.setdefault(s, {})
                    upside = _compute_upside(fy27, pe, hpe)
                    # DMA deviation = (price - dma)/dma * 100  (engine formula)
                    pf, d50, d200 = _f(price), _f(dma50), _f(dma200)
                    dma50_dev = (round((pf - d50) / d50 * 100, 2)
                                 if pf is not None and d50 and d50 > 0 else None)
                    dma200_dev = (round((pf - d200) / d200 * 100, 2)
                                  if pf is not None and d200 and d200 > 0 else None)
                    ladder_extra[s].update({
                        "pe": _r(pe, 1), "ret_1y": _r(r1y, 1),
                        "pb": _r(pb, 2), "roe": _r(roe, 1),
                        "opm": _r(opm, 1), "div_yield": _r(dy, 2),
                        "upside": upside,
                        "dma_50_dev": dma50_dev, "dma_200_dev": dma200_dev,
                    })
                    if s == symbol:
                        pe_f, hpe_f = _f(pe), _f(hpe)
                        extras["flow"] = {
                            "fii": _r(fii, 2), "fii_chg": _r(fii_c, 2),
                            "dii": _r(dii, 2), "dii_chg": _r(dii_c, 2),
                            "inst_chg": (round((_f(fii_c) or 0) + (_f(dii_c) or 0), 2)
                                          if fii_c is not None or dii_c is not None else None),
                            "promoter": _r(prom, 2),
                        }
                        extras["valuation"] = {
                            "pe": _r(pe, 2), "historical_pe": _r(hpe, 2),
                            "segment_pe": _r(spe, 2),
                            "rerating_pct": (round((pe_f / hpe_f - 1) * 100, 1)
                                             if pe_f and hpe_f else None),
                            "pb": _r(pb, 2), "peg": _r(peg, 2),
                            "ev_ebitda": _r(ev, 2), "cfo_pat": _r(cfo, 2),
                            "div_yield": _r(dy, 2),
                            "annual_upside": upside,
                            "fy27_growth": _r(fy27, 1),
                        }
            except Exception as e:
                log.warning(f"flow/valuation failed: {e}")

            # ── 7. Earnings blackout ──────────────────────────────────────
            try:
                cur.execute("""
                    SELECT ex_date::text, event_type,
                           (ex_date - CURRENT_DATE) AS days_to
                    FROM earnings_calendar
                    WHERE ticker = %s AND ex_date >= CURRENT_DATE
                    ORDER BY ex_date ASC LIMIT 1
                """, (symbol,))
                row = cur.fetchone()
                if row:
                    days_to = int(row[2]) if row[2] is not None else None
                    extras["earnings"] = {
                        "next_date": row[0], "event_type": row[1],
                        "days_to": days_to,
                        "blackout": (days_to is not None
                                     and days_to <= BLACKOUT_DAYS),
                    }
                else:
                    extras["earnings"] = None
            except Exception as e:
                log.warning(f"earnings failed {symbol}: {e}")
                extras["earnings"] = None

            # ── 8. v8_metrics for futures peers -> Tier-1 + ladder ────────
            try:
                cur.execute("""
                    SELECT DISTINCT ON (symbol)
                           symbol, gvm_score, sector_week, sector_month,
                           dma_20, dma_50, dma_200, rsi_month, rsi_weekly,
                           week_return, month_return, year_return,
                           daily_rsi, vol_ratio
                    FROM v8_metrics
                    WHERE symbol = ANY(%s)
                    ORDER BY symbol, score_date DESC
                """, (syms,))
                cols = [d[0] for d in cur.description]
                for r in cur.fetchall():
                    m = dict(zip(cols, r))
                    s = m["symbol"]
                    ad = ad_map.get(s, {})
                    t1 = _tier1_auto(m, ad.get("ratio"))
                    ladder_extra.setdefault(s, {})
                    ladder_extra[s]["tier1_passed"] = t1["passed"]
                    ladder_extra[s]["tier1_total"] = t1["total"]
                    # cc#224: RSIM no longer sourced from v8_metrics (futures-only ~212) —
                    # see section 8c (universe_technicals, full universe).
                    # v8_metrics returns are point-in-time EOD-frozen; prefer
                    # these when available, fallback below fills the rest.
                    if m.get("week_return") is not None:
                        ladder_extra[s]["week_return"] = _r(m.get("week_return"), 2)
                    if m.get("month_return") is not None:
                        ladder_extra[s]["month_return"] = _r(m.get("month_return"), 2)
                    if m.get("year_return") is not None:
                        ladder_extra[s]["year_return"] = _r(m.get("year_return"), 1)
                    if s == symbol:
                        extras["tier1"] = t1
                        extras["vol_ratio_10_30"] = _r(m.get("vol_ratio"))
            except Exception as e:
                log.warning(f"tier1 failed: {e}")

            # ── 8b. raw_prices fallback for Wk/Mo/Yr returns (non-futures) ─
            try:
                missing = [s for s in syms
                           if ladder_extra.get(s, {}).get("week_return") is None
                           or ladder_extra.get(s, {}).get("month_return") is None
                           or ladder_extra.get(s, {}).get("year_return") is None]
                if missing:
                    ret_map = _compute_returns_from_prices(cur, missing)
                    for s, vals in ret_map.items():
                        ladder_extra.setdefault(s, {})
                        for k in ("week_return", "month_return", "year_return"):
                            if ladder_extra[s].get(k) is None and vals.get(k) is not None:
                                ladder_extra[s][k] = vals[k]
            except Exception as e:
                log.warning(f"returns fallback failed: {e}")

            # ── 8c. RSIM from universe_technicals (full universe, cc#224) ──
            # v8_metrics only populates ~212 futures stocks, so its rsi_month left RSIM blank
            # for ~80% of ladder rows (all non-futures). universe_technicals.rsi_month covers
            # the full ~1766-stock universe; take the latest computed_at per symbol. A dash
            # remains ONLY where genuinely null (per-stock data gap, e.g. MONOLITH).
            try:
                cur.execute("""
                    SELECT DISTINCT ON (symbol) symbol, rsi_month
                    FROM universe_technicals
                    WHERE symbol = ANY(%s)
                    ORDER BY symbol, computed_at DESC
                """, (syms,))
                for s, rm in cur.fetchall():
                    if rm is not None:
                        ladder_extra.setdefault(s, {})
                        ladder_extra[s]["rsi_month"] = _r(rm, 0)
            except Exception as e:
                log.warning(f"ladder rsi_month (universe_technicals) failed: {e}")

            # ── 9. Pull G/V/M components + mcap per ladder ────────────────
            try:
                cur.execute("""
                    SELECT symbol, g_score, v_score, m_score, market_cap
                    FROM gvm_scores
                    WHERE symbol = ANY(%s)
                      AND score_date = (SELECT MAX(score_date) FROM gvm_scores)
                """, (syms,))
                for s, g, v, m_s, mcap in cur.fetchall():
                    ladder_extra.setdefault(s, {})
                    ladder_extra[s]["g"] = _r(g, 2)
                    ladder_extra[s]["v"] = _r(v, 2)
                    ladder_extra[s]["m"] = _r(m_s, 2)
                    ladder_extra[s]["market_cap"] = _r(mcap, 0)
            except Exception as e:
                log.warning(f"ladder gvm components failed: {e}")

            # ── 10. GVM delta 13d for ladder (one query) ──────────────────
            try:
                cur.execute("""
                    WITH h AS (
                        SELECT symbol, score_date, gvm_score,
                               ROW_NUMBER() OVER (PARTITION BY symbol
                                   ORDER BY score_date DESC) AS rn,
                               COUNT(*) OVER (PARTITION BY symbol) AS n
                        FROM gvm_history
                        WHERE symbol = ANY(%s)
                          AND score_date >= CURRENT_DATE - INTERVAL '25 days'
                    )
                    SELECT a.symbol, a.gvm_score AS latest, b.gvm_score AS oldest
                    FROM h a
                    JOIN h b ON b.symbol = a.symbol
                           AND b.rn = LEAST(a.n, 13)
                    WHERE a.rn = 1
                """, (syms,))
                for s, latest, oldest in cur.fetchall():
                    lf, of = _f(latest), _f(oldest)
                    if lf is not None and of is not None:
                        ladder_extra.setdefault(s, {})
                        ladder_extra[s]["gvm_d13"] = round(lf - of, 2)
            except Exception as e:
                log.warning(f"gvm_d13 failed: {e}")

            # ── 11. SEGMENT CONTEXT — sector_ratings + computed averages ─
            try:
                if segment:
                    cur.execute("""
                        SELECT mcap_weighted_gvm, weighted_g, weighted_v, weighted_m,
                               simple_avg_gvm, stocks_count, total_mcap,
                               top_stock, top_stock_gvm, verdict, score_date::text
                        FROM sector_ratings
                        WHERE segment = %s
                        ORDER BY score_date DESC LIMIT 1
                    """, (segment,))
                    sr = cur.fetchone()

                    cur.execute("""
                        SELECT COUNT(*) AS total,
                               (SELECT COUNT(*) FROM sector_ratings
                                 WHERE score_date = (SELECT MAX(score_date) FROM sector_ratings)
                                   AND mcap_weighted_gvm > (SELECT mcap_weighted_gvm FROM sector_ratings
                                                             WHERE segment = %s
                                                             ORDER BY score_date DESC LIMIT 1)) AS better
                        FROM sector_ratings
                        WHERE score_date = (SELECT MAX(score_date) FROM sector_ratings)
                    """, (segment,))
                    sect_total, sect_better = cur.fetchone()

                    # Sector Week/Month/Year — average across peer ladder_extra
                    # (already covers v8_metrics + raw_prices fallback).
                    def _avg_ladder(key):
                        vals = [v.get(key) for v in ladder_extra.values()
                                if v.get(key) is not None]
                        return round(sum(vals) / len(vals), 2) if vals else None

                    sw_avg = _avg_ladder("week_return")
                    sm_avg = _avg_ladder("month_return")
                    sy_avg = _avg_ladder("year_return")
                    if sy_avg is not None:
                        sy_avg = round(sy_avg, 1)

                    if sr:
                        cur.execute("""
                            SELECT gvm_score FROM gvm_scores
                            WHERE symbol = %s
                              AND score_date = (SELECT MAX(score_date) FROM gvm_scores)
                        """, (symbol,))
                        rr = cur.fetchone()
                        stock_gvm = _f(rr[0]) if rr else None
                        sect_gvm = _f(sr[0])
                        extras["segment_ctx"] = {
                            "segment": segment,
                            "mcap_wtd_gvm": _r(sr[0]),
                            "weighted_g": _r(sr[1]),
                            "weighted_v": _r(sr[2]),
                            "weighted_m": _r(sr[3]),
                            "simple_avg_gvm": _r(sr[4]),
                            "stocks_count": sr[5],
                            "total_mcap": _r(sr[6], 0),
                            "top_stock": sr[7],
                            "top_stock_gvm": _r(sr[8]),
                            "verdict": sr[9],
                            "score_date": sr[10],
                            "sector_rank": int(sect_better) + 1 if sect_total else None,
                            "sector_total": int(sect_total) if sect_total else None,
                            "week_return_avg": sw_avg,
                            "month_return_avg": sm_avg,
                            "year_return_avg": sy_avg,
                            "stock_vs_sector": (round(stock_gvm - sect_gvm, 2)
                                                if stock_gvm is not None and sect_gvm is not None
                                                else None),
                        }
            except Exception as e:
                log.warning(f"segment_ctx failed: {e}")
                extras["segment_ctx"] = None

    except Exception as e:
        log.error(f"build_page_extras connection failed: {e}")

    return {"extras": extras, "ladder_extra": ladder_extra}
