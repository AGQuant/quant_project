"""
gvm_page_extras.py — DB-only enrichment block for the GVM company page (v2).

v2 (12-Jun-2026 evening):
  + segment context block (sector_ratings + computed sector rank + sector
    week/month % from v8_metrics peer-average + stock-vs-sector delta)
  + universe z-score (μ/σ across full scored universe)
  + ladder enriched with G/V/M components, mcap, p/b, roe, opm, div_yield,
    rsi_month, week_return, month_return, ret_1y, upside_raw, peer-segment
    averages for "vs segment" compare toggle
  + volume.cum_pct_series for cumulative-return overlay on A/D panel

v1: trend / volume A/D / pivot / 52W / percentile / flow / valuation /
    earnings blackout / tier1 auto / ladder_extra basics.

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

BLACKOUT_DAYS = 5          # days before a results/ex event = blackout
AD_ACCUM_RATIO = 1.30      # up-vol / down-vol >= this  -> ACCUMULATION
AD_DIST_RATIO = 0.77       # up-vol / down-vol <= this  -> DISTRIBUTION


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


# ── Tier-1 auto-checkable subset (price-action facts, NOT a trade verdict) ──
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

            # daily bars + biggest day + cumulative % series for main symbol
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
                cum_series = []
                base_close = _f(rows[0][1]) if rows else None
                for d, close, vol, chg in rows:
                    bars.append({"d": d, "v": _f(vol) or 0.0,
                                 "up": (_f(chg) or 0.0) > 0,
                                 "close": _f(close)})
                    c = _f(close)
                    cum_pct = (round((c / base_close - 1) * 100, 2)
                               if c is not None and base_close else None)
                    cum_series.append({"d": d, "cum_pct": cum_pct})
                ad = ad_map.get(symbol, {})
                biggest = max(bars, key=lambda b: b["v"]) if bars else None
                total_ret = cum_series[-1]["cum_pct"] if cum_series else None
                vol_block = {
                    "up_vol": ad.get("up"), "down_vol": ad.get("dn"),
                    "ratio": ad.get("ratio"),
                    "verdict": _ad_verdict(ad.get("ratio")),
                    "bars": bars,
                    "cum_pct_series": cum_series,
                    "total_return_pct": total_ret,
                    "biggest_day": ({"d": biggest["d"], "v": biggest["v"],
                                     "direction": "UP" if biggest["up"] else "DOWN"}
                                    if biggest else None),
                }
            except Exception as e:
                log.warning(f"vol bars failed {symbol}: {e}")
            extras["volume"] = vol_block or None

            # ── 3. Pivot levels (latest rolling-5d) ───────────────────────
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
            # one query pulls everything needed for main stock AND every peer
            try:
                cur.execute("""
                    SELECT nse_code, pe, historical_pe, segment_pe,
                           "Price to book value", "PEG Ratio", "EVEBITDA",
                           "Cfo by Pat", dividend_yield,
                           fii_holding, fii_change, dii_holding, dii_change,
                           "Promoter holding", return_1y, "Return on equity",
                           opm
                    FROM screener_raw
                    WHERE nse_code = ANY(%s)
                """, (syms,))
                for r in cur.fetchall():
                    (s, pe, hpe, spe, pb, peg, ev, cfo, dy,
                     fii, fii_c, dii, dii_c, prom, r1y, roe, opm) = r
                    ladder_extra.setdefault(s, {})
                    ladder_extra[s].update({
                        "pe": _r(pe, 1), "ret_1y": _r(r1y, 1),
                        "pb": _r(pb, 2), "roe": _r(roe, 1),
                        "opm": _r(opm, 1), "div_yield": _r(dy, 2),
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

            # ── 8. v8_metrics for ALL ladder symbols -> Tier-1 + ladder ───
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
                    ladder_extra[s]["rsi_month"] = _r(m.get("rsi_month"), 0)
                    ladder_extra[s]["week_return"] = _r(m.get("week_return"), 2)
                    ladder_extra[s]["month_return"] = _r(m.get("month_return"), 2)
                    ladder_extra[s]["year_return"] = _r(m.get("year_return"), 1)
                    if s == symbol:
                        extras["tier1"] = t1
                        extras["vol_ratio_10_30"] = _r(m.get("vol_ratio"))

            except Exception as e:
                log.warning(f"tier1 failed: {e}")

            # ── 9. Pull G/V/M components + mcap + upside per ladder ───────
            try:
                cur.execute("""
                    SELECT symbol, g_score, v_score, m_score, market_cap, upside_raw
                    FROM gvm_scores
                    WHERE symbol = ANY(%s)
                      AND score_date = (SELECT MAX(score_date) FROM gvm_scores)
                """, (syms,))
                for s, g, v, m_s, mcap, up in cur.fetchall():
                    ladder_extra.setdefault(s, {})
                    ladder_extra[s]["g"] = _r(g, 2)
                    ladder_extra[s]["v"] = _r(v, 2)
                    ladder_extra[s]["m"] = _r(m_s, 2)
                    ladder_extra[s]["market_cap"] = _r(mcap, 0)
                    ladder_extra[s]["upside"] = _r(up, 1)
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

            # ── 11. SEGMENT CONTEXT ──────────────────────────────────────
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

                    cur.execute("""
                        SELECT AVG(week_return)::float, AVG(month_return)::float,
                               AVG(year_return)::float
                        FROM v8_metrics
                        WHERE symbol = ANY(%s)
                          AND score_date = (SELECT MAX(score_date) FROM v8_metrics
                                             WHERE symbol = ANY(%s))
                    """, (syms, syms))
                    sw_avg, sm_avg, sy_avg = cur.fetchone()

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
                            "week_return_avg": _r(sw_avg, 2),
                            "month_return_avg": _r(sm_avg, 2),
                            "year_return_avg": _r(sy_avg, 1),
                            "stock_vs_sector": (round(stock_gvm - sect_gvm, 2)
                                                if stock_gvm is not None and sect_gvm is not None
                                                else None),
                        }
            except Exception as e:
                log.warning(f"segment_ctx failed: {e}")
                extras["segment_ctx"] = None

            # ── 12. Segment averages for "vs Segment" compare toggle ─────
            try:
                def avg(key):
                    vals = [v[key] for v in ladder_extra.values()
                            if v.get(key) is not None]
                    return round(sum(vals) / len(vals), 2) if vals else None

                seg_avg = {
                    k: avg(k) for k in [
                        "pe", "pb", "roe", "opm", "div_yield",
                        "ret_1y", "rsi_month", "week_return", "month_return",
                        "year_return", "upside", "market_cap",
                    ]
                }
                extras["segment_avg"] = seg_avg
            except Exception as e:
                log.warning(f"segment_avg failed: {e}")

    except Exception as e:
        log.error(f"build_page_extras connection failed: {e}")

    return {"extras": extras, "ladder_extra": ladder_extra}
