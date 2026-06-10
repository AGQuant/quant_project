"""
V8 Signal Writer — Single Live Engine (v2.0.0)
===============================================
Unified 5-min engine. Replaces v8_live.py + old v8_signal_writer.py.

What it does every 5-min during market hours:
  1. Loads latest EOD v8_metrics row per symbol (slow metrics: GVM, RSI M/W, sector_week, sector_month)
  2. Reads intraday_prices (today's bars) per symbol
  3. Recomputes all 19 live-moving metrics from intraday close spliced onto EOD history
  4. Preserves EOD-frozen metrics: gvm_score, rsi_month, rsi_weekly, sector_week, sector_month
  5. Upserts v8_metrics (today's row) with live values
  6. Applies FILTER_CONFIG → writes v8_qualified (today only)
  7. Writes v8_funnel_counts (cumulative step counts)

Frozen at EOD (never recomputed live):
  - gvm_score       (weekly screener + daily M, recomputes 22:00 IST)
  - rsi_month       (monthly-resampled Wilder, recomputes 15:45 IST)
  - rsi_weekly      (weekly-resampled Wilder, recomputes 15:45 IST)
  - sector_week     (peer avg 5-day return, recomputes 15:45 IST)
  - sector_month    (peer avg 21-day return, recomputes 15:45 IST)

Live (recomputed every 5-min from intraday_prices):
  dma_20, dma_50, dma_200, daily_rsi, month_return, week_return,
  year_return, mom_2d, ma9_vs_ma21, vol_ratio, week_index_52,
  month_index, range_1d, range_3d, upper_bb, lower_bb,
  sector_day (live peer avg mom_2d across segment)

v8_live.py and v8_history_cache are archived — no longer used.

RSI periods: Month=6 (monthly bars), Weekly=8 (weekly bars), Daily=14 (Wilder).
mom_2d = (cmp / close_2_days_ago - 1) * 100  [2-day momentum, T vs T-2]
  Renamed from 'day_change' 10-Jun-2026 — it was never a 1-day change.

08-Jun-2026 FIX: _load_eod_metrics no longer reads frozen metrics from today's own
half-built v8_metrics row (NULL pre-15:45 -> circular NULL -> funnel collapse). It now
pulls gvm_score from gvm_scores (authoritative) and rsi/sector from the most recent
v8_metrics row PER SYMBOL where they are non-null (last EOD freeze carried forward).

08-Jun-2026 buy_reversal GATE-ADAPTIVE qualification (Buy Reversal ONLY):
  Bullish gate (0-1 fails)  -> 11/11 strict
  Sideways gate (2 fails)   -> 10/11 (any 1 filter may fail)
  Bearish gate (3+ fails)   -> 9/11  (any 2 filters may fail)
  mom_2d > 0 is MANDATORY in every tier (never one of the allowed fails).
  Other baskets keep strict all-pass. Funnel counts still show strict waterfall.
"""

import logging
import json
from datetime import datetime, date, timedelta, timezone
from typing import Dict, List, Optional
from collections import defaultdict
import pandas as pd
import numpy as np
import psycopg
import os

log = logging.getLogger("scorr.signal_writer")

IST = timezone(timedelta(hours=5, minutes=30))

RSI_DAILY_PERIOD = 14


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None

def _safe_pct(num: float, den: float) -> Optional[float]:
    if den is None or den == 0:
        return None
    try:
        if np.isnan(den):
            return None
    except Exception:
        pass
    return float((num / den - 1) * 100)

def _wilder_rsi(closes: pd.Series, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    rsi      = 100 - (100 / (1 + rs))
    val      = rsi.iloc[-1]
    return float(val) if pd.notna(val) else None

def _passes(value, mn, mx) -> bool:
    if value is None:
        return False
    v = float(value)
    if mn is not None and v < mn:
        return False
    if mx is not None and v > mx:
        return False
    return True


# ── Step 1: Load EOD metrics snapshot ─────────────────────────────────────────

def _load_eod_metrics(conn) -> Dict[str, dict]:
    """
    Frozen slow metrics per symbol, sourced from LAST-GOOD values (not today's
    possibly-empty row):
      - gvm_score + segment  ← latest gvm_scores (authoritative daily source)
      - rsi_month, rsi_weekly, sector_week, sector_month
            ← most recent v8_metrics row PER SYMBOL where each is non-null
              (DISTINCT ON carries yesterday's EOD freeze forward intraday).

    Rationale: the 5-min writer must never read frozen metrics from today's own
    half-built row (circular NULL). It pulls the last computed EOD value forward
    until the 15:45 EOD engine refreshes them. gvm/rsi/sector are EOD by design
    and do not change intraday.
    """
    # gvm_score + segment from authoritative gvm_scores (latest score_date)
    gvm_map: Dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, gvm_score, segment
            FROM gvm_scores
            WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)
        """)
        for sym, gvm, seg in cur.fetchall():
            gvm_map[sym] = {"gvm_score": _safe_float(gvm), "segment": seg}

    # Last-good frozen metrics per symbol (latest non-null row, any date)
    frozen_map: Dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (symbol)
                symbol, rsi_month, rsi_weekly, sector_week, sector_month,
                mom_2d AS eod_mom_2d
            FROM v8_metrics
            WHERE rsi_month   IS NOT NULL
               OR rsi_weekly  IS NOT NULL
               OR sector_week IS NOT NULL
               OR sector_month IS NOT NULL
            ORDER BY symbol, score_date DESC
        """)
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            frozen_map[d["symbol"]] = d

    # Merge: every symbol that has either a gvm row or a frozen row
    out: Dict[str, dict] = {}
    for sym in set(gvm_map) | set(frozen_map):
        g = gvm_map.get(sym, {})
        f = frozen_map.get(sym, {})
        out[sym] = {
            "symbol":        sym,
            "gvm_score":     g.get("gvm_score"),
            "segment":       g.get("segment"),
            "rsi_month":     _safe_float(f.get("rsi_month")),
            "rsi_weekly":    _safe_float(f.get("rsi_weekly")),
            "sector_week":   _safe_float(f.get("sector_week")),
            "sector_month":  _safe_float(f.get("sector_month")),
            "eod_mom_2d":    _safe_float(f.get("eod_mom_2d")),
        }
    return out


# ── Step 2: Load EOD history per symbol (bulk) ────────────────────────────────

def _load_eod_history(conn, symbols: List[str]) -> Dict[str, dict]:
    """
    Last 400 raw_prices rows per symbol strictly before today.
    Returns dict: symbol -> {closes, highs, lows, vols, vol_avg10,
                              hi_252, lo_252, hi_21, lo_21, close_2d_ago}
    """
    today = datetime.now(IST).date()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, close, high, low, volume
            FROM raw_prices
            WHERE symbol = ANY(%s) AND price_date < %s
            ORDER BY symbol, price_date DESC
        """, (symbols, today))
        rows = cur.fetchall()

    by_sym: Dict[str, list] = defaultdict(list)
    for sym, close, high, low, vol in rows:
        by_sym[sym].append((close, high, low, vol))

    history = {}
    for sym, data in by_sym.items():
        data    = data[:400][::-1]   # cap + oldest-first
        closes  = [float(r[0]) for r in data if r[0] is not None]
        highs   = [float(r[1]) for r in data if r[1] is not None]
        lows    = [float(r[2]) for r in data if r[2] is not None]
        vols    = [float(r[3]) for r in data if r[3] is not None]

        history[sym] = {
            "closes":      closes,
            "highs":       highs,
            "lows":        lows,
            "vols":        vols,
            "vol_avg10":   float(np.mean(vols[-10:])) if len(vols) >= 10 else None,
            "hi_252":      float(max(highs[-252:])) if len(highs) >= 252 else (float(max(highs)) if highs else None),
            "lo_252":      float(min(lows[-252:]))  if len(lows)  >= 252 else (float(min(lows))  if lows  else None),
            "hi_21":       float(max(highs[-21:]))  if len(highs) >= 21  else (float(max(highs)) if highs else None),
            "lo_21":       float(min(lows[-21:]))   if len(lows)  >= 21  else (float(min(lows))  if lows  else None),
            "close_2d_ago": closes[-2] if len(closes) >= 2 else None,
        }
    return history


# ── Step 3: Load today's intraday bars (bulk) ─────────────────────────────────

def _load_intraday_bars(conn, symbols: List[str]) -> Dict[str, dict]:
    """Latest intraday close + today's H/L/open/vol per symbol."""
    today = datetime.now(IST).date()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                symbol,
                (SELECT close FROM intraday_prices i2
                 WHERE i2.symbol = ip.symbol AND i2.ts::date = %s
                 ORDER BY ts DESC LIMIT 1)                       AS live_close,
                (SELECT open  FROM intraday_prices i3
                 WHERE i3.symbol = ip.symbol AND i3.ts::date = %s
                 ORDER BY ts ASC  LIMIT 1)                       AS day_open,
                MAX(high)   FILTER (WHERE ts::date = %s)         AS day_high,
                MIN(low)    FILTER (WHERE ts::date = %s)         AS day_low,
                MAX(volume) FILTER (WHERE ts::date = %s)         AS day_vol
            FROM intraday_prices ip
            WHERE symbol = ANY(%s) AND ts::date = %s
            GROUP BY symbol
        """, (today, today, today, today, today, symbols, today))
        bars = {}
        for sym, lc, op, hi, lo, vol in cur.fetchall():
            if lc is None:
                continue
            bars[sym] = {
                "close":  _safe_float(lc),
                "open":   _safe_float(op),
                "high":   _safe_float(hi),
                "low":    _safe_float(lo),
                "volume": _safe_float(vol),
            }
    return bars


# ── Step 4: Load CMP ──────────────────────────────────────────────────────────

def _load_cmp(conn) -> Dict[str, float]:
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, cmp FROM cmp_prices")
        return {r[0]: _safe_float(r[1]) for r in cur.fetchall()}


# ── Step 5: Compute 19 live metrics ───────────────────────────────────────────

def _compute_live_metrics(hist: dict, bar: dict, cmp: Optional[float],
                           eod: dict) -> dict:
    """
    Splice today's live bar onto EOD history. Recompute 19 live metrics.
    EOD-frozen: gvm_score, rsi_month, rsi_weekly, sector_week, sector_month.
    """
    closes = hist["closes"][:]
    highs  = hist["highs"][:]
    lows   = hist["lows"][:]
    live   = bar["close"]

    c = closes + [live]
    h = highs  + [bar["high"] if bar.get("high") else live]
    l = lows   + [bar["low"]  if bar.get("low")  else live]

    out = {
        # EOD-frozen
        "gvm_score":    _safe_float(eod.get("gvm_score")),
        "rsi_month":    _safe_float(eod.get("rsi_month")),
        "rsi_weekly":   _safe_float(eod.get("rsi_weekly")),
        "sector_week":  _safe_float(eod.get("sector_week")),
        "sector_month": _safe_float(eod.get("sector_month")),
        # Live-recomputed
        "dma_20": None, "dma_50": None, "dma_200": None,
        "daily_rsi": None,
        "month_return": None, "week_return": None, "year_return": None,
        "mom_2d": None,
        "ma9_vs_ma21": None, "vol_ratio": None,
        "week_index_52": None, "month_index": None,
        "range_1d": None, "range_3d": None,
        "upper_bb": None, "lower_bb": None,
        "sector_day": None,   # filled in sector pass
    }

    if len(c) >= 20:  out["dma_20"]  = _safe_pct(live, float(np.mean(c[-20:])))
    if len(c) >= 50:  out["dma_50"]  = _safe_pct(live, float(np.mean(c[-50:])))
    if len(c) >= 200: out["dma_200"] = _safe_pct(live, float(np.mean(c[-200:])))

    if len(c) >= 253: out["year_return"]  = _safe_pct(live, c[-253])
    if len(c) >= 22:  out["month_return"] = _safe_pct(live, c[-22])
    if len(c) >= 6:   out["week_return"]  = _safe_pct(live, c[-6])

    # mom_2d: use cmp if available, else live close, vs close_2_days_ago (2-candle gap)
    base_2d = hist.get("close_2d_ago")
    price   = cmp if cmp else live
    if base_2d and base_2d > 0:
        out["mom_2d"] = (price / base_2d - 1) * 100

    out["daily_rsi"] = _wilder_rsi(pd.Series(c), RSI_DAILY_PERIOD)

    if len(c) >= 21:
        ma9 = float(np.mean(c[-9:])); ma21 = float(np.mean(c[-21:]))
        if ma21:
            out["ma9_vs_ma21"] = round((ma9 - ma21) / ma21 * 100, 2)

    vol_now   = bar.get("volume")
    vol_avg10 = hist.get("vol_avg10")
    if vol_now and vol_avg10 and vol_avg10 > 0:
        out["vol_ratio"] = round(vol_now / vol_avg10, 2)

    hi252 = max(x for x in [hist.get("hi_252"), bar.get("high"), live] if x)
    lo252 = min(x for x in [hist.get("lo_252"), bar.get("low"),  live] if x)
    if hi252 > lo252:
        out["week_index_52"] = (live - lo252) / (hi252 - lo252) * 100

    hi21 = max(x for x in [hist.get("hi_21"), bar.get("high"), live] if x)
    lo21 = min(x for x in [hist.get("lo_21"), bar.get("low"),  live] if x)
    if hi21 > lo21:
        out["month_index"] = (live - lo21) / (hi21 - lo21) * 100

    op = bar.get("open")
    if op and bar.get("high") is not None and bar.get("low") is not None and op > 0:
        raw = (bar["high"] - bar["low"]) / op * 100
        out["range_1d"] = raw if live >= op else -raw

    if len(c) >= 4:
        h3 = max(h[-3:]); l3 = min(l[-3:]); base3 = c[-4]
        if base3 > 0:
            raw = (h3 - l3) / base3 * 100
            out["range_3d"] = raw if live >= base3 else -raw

    if len(c) >= 20:
        last20 = c[-20:]
        ma, sd = float(np.mean(last20)), float(np.std(last20, ddof=1))
        if live > 0:
            out["upper_bb"] = (live - (ma + 2*sd)) / live * 100
            out["lower_bb"] = (live - (ma - 2*sd)) / live * 100

    out["_live"] = live
    return out


# ── Step 6: Sector day pass ───────────────────────────────────────────────────

def _add_sector_day(computed: Dict[str, dict], eod_metrics: Dict[str, dict]):
    """sector_day = avg mom_2d of all peers in same segment (live)."""
    seg_moves: Dict[str, list] = defaultdict(list)
    for sym, m in computed.items():
        seg     = eod_metrics.get(sym, {}).get("segment")
        day_chg = m.get("mom_2d")
        if seg and day_chg is not None:
            seg_moves[seg].append(day_chg)

    seg_avg = {seg: float(np.mean(vals)) for seg, vals in seg_moves.items() if vals}
    for sym, m in computed.items():
        seg = eod_metrics.get(sym, {}).get("segment")
        m["sector_day"] = seg_avg.get(seg)


# ── Step 7: Upsert v8_metrics ─────────────────────────────────────────────────

def _upsert_metrics(conn, sym: str, m: dict, target_date: date):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO v8_metrics
            (symbol, score_date, gvm_score,
             dma_20, dma_50, dma_200, daily_rsi,
             rsi_month, rsi_weekly,
             month_return, week_return, year_return, mom_2d,
             sector_day, sector_week, sector_month,
             month_index, week_index_52,
             range_1d, range_3d, upper_bb, lower_bb,
             ma9_vs_ma21, vol_ratio)
            VALUES (%s,%s,%s, %s,%s,%s,%s, %s,%s, %s,%s,%s,%s,
                    %s,%s,%s, %s,%s, %s,%s,%s,%s, %s,%s)
            ON CONFLICT (symbol, score_date) DO UPDATE SET
                gvm_score     = EXCLUDED.gvm_score,
                dma_20        = EXCLUDED.dma_20,
                dma_50        = EXCLUDED.dma_50,
                dma_200       = EXCLUDED.dma_200,
                daily_rsi     = EXCLUDED.daily_rsi,
                rsi_month     = EXCLUDED.rsi_month,
                rsi_weekly    = EXCLUDED.rsi_weekly,
                month_return  = EXCLUDED.month_return,
                week_return   = EXCLUDED.week_return,
                year_return   = EXCLUDED.year_return,
                mom_2d        = EXCLUDED.mom_2d,
                sector_day    = EXCLUDED.sector_day,
                sector_week   = EXCLUDED.sector_week,
                sector_month  = EXCLUDED.sector_month,
                month_index   = EXCLUDED.month_index,
                week_index_52 = EXCLUDED.week_index_52,
                range_1d      = EXCLUDED.range_1d,
                range_3d      = EXCLUDED.range_3d,
                upper_bb      = EXCLUDED.upper_bb,
                lower_bb      = EXCLUDED.lower_bb,
                ma9_vs_ma21   = EXCLUDED.ma9_vs_ma21,
                vol_ratio     = EXCLUDED.vol_ratio
        """, (
            sym, target_date, m.get("gvm_score"),
            m.get("dma_20"), m.get("dma_50"), m.get("dma_200"), m.get("daily_rsi"),
            m.get("rsi_month"), m.get("rsi_weekly"),
            m.get("month_return"), m.get("week_return"), m.get("year_return"), m.get("mom_2d"),
            m.get("sector_day"), m.get("sector_week"), m.get("sector_month"),
            m.get("month_index"), m.get("week_index_52"),
            m.get("range_1d"), m.get("range_3d"), m.get("upper_bb"), m.get("lower_bb"),
            m.get("ma9_vs_ma21"), m.get("vol_ratio"),
        ))
    conn.commit()


# ── Market gate (for adaptive buy_reversal threshold) ─────────────────────────

def _market_gate_fails(conn) -> int:
    """
    Count of failed market-mood checks (ADR + Nifty Day/Week/Month), mirroring
    /api/v8/market_mood. 0-1=Bullish, 2=Neutral/Sideways, 3+=Bearish.
    Returns fails (0-4); defaults to 0 (strict) if data insufficient.
    """
    try:
        with conn.cursor() as cur:
            # ADR: live intraday breadth, else eod fallback
            cur.execute("""
                WITH li AS (
                    SELECT DISTINCT ON (symbol) symbol, close AS cmp
                    FROM intraday_prices WHERE ts::date = CURRENT_DATE
                    ORDER BY symbol, ts DESC
                ),
                pc AS (
                    SELECT DISTINCT ON (symbol) symbol, close AS pclose
                    FROM raw_prices WHERE price_date < CURRENT_DATE
                    ORDER BY symbol, price_date DESC
                )
                SELECT COUNT(*) FILTER (WHERE li.cmp > pc.pclose),
                       COUNT(*) FILTER (WHERE li.cmp < pc.pclose),
                       COUNT(*)
                FROM li JOIN pc ON pc.symbol = li.symbol
            """)
            adv, dec, tot = cur.fetchone()
            if tot and tot >= 50:
                adr = (adv / dec) if dec else float(adv)
            else:
                cur.execute("SELECT adr FROM adr_daily ORDER BY price_date DESC LIMIT 1")
                r = cur.fetchone()
                adr = float(r[0]) if r and r[0] is not None else 1.0

            # Nifty D/W/M from intraday live vs raw_prices history
            cur.execute("""
                SELECT close FROM intraday_prices
                WHERE symbol='NIFTY50' AND ts::date=CURRENT_DATE
                ORDER BY ts DESC LIMIT 1
            """)
            lv = cur.fetchone()
            cur.execute("""
                SELECT close FROM raw_prices
                WHERE symbol='NIFTY50' AND price_date < CURRENT_DATE
                ORDER BY price_date DESC LIMIT 30
            """)
            hist = [float(x[0]) for x in cur.fetchall()]
            if lv and lv[0] is not None and len(hist) >= 22:
                latest = float(lv[0])
                nday   = (latest / hist[0]  - 1) * 100
                nweek  = (latest / hist[4]  - 1) * 100
                nmonth = (latest / hist[20] - 1) * 100
            elif len(hist) >= 22:
                latest = hist[0]
                nday   = (latest / hist[1]  - 1) * 100
                nweek  = (latest / hist[5]  - 1) * 100 if len(hist) > 5 else 0.0
                nmonth = (latest / hist[21] - 1) * 100 if len(hist) > 21 else 0.0
            else:
                return 0  # insufficient data → strict

            checks = [adr >= 1.0, nday >= 0, nweek >= 0, nmonth >= 0]
            return sum(1 for c in checks if not c)
    except Exception as e:
        log.warning(f"_market_gate_fails: {e}")
        return 0  # safe default → strict


def _gate_threshold(fails: int, n_filters: int) -> int:
    """
    Adaptive buy_reversal threshold by market gate:
      0-1 fails (Bullish)  → strict n_filters (all pass)
      2 fails (Sideways)   → n_filters - 1
      3+ fails (Bearish)   → n_filters - 2
    """
    if fails <= 1:
        return n_filters
    if fails == 2:
        return n_filters - 1
    return n_filters - 2


# ── Step 8: Write v8_qualified + funnel ───────────────────────────────────────

def _write_qualified(conn, all_metrics: List[dict], target_date: date):
    from v8_endpoints import FILTER_CONFIG

    gate_fails = _market_gate_fails(conn)

    for basket, filters in FILTER_CONFIG.items():
        if basket == "sell_overbought":
            continue

        # Funnel counts always reflect the strict sequential waterfall (display).
        universe = all_metrics[:]
        funnel   = {}
        for metric, bounds in filters.items():
            mn, mx   = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            universe = [s for s in universe if _passes(s.get(metric), mn, mx)]
            funnel[metric] = len(universe)

        # buy_reversal: gate-adaptive qualification (Bullish 11/11, Sideways 10/11,
        # Bearish 9/11) with mom_2d>0 MANDATORY in every tier. Other baskets
        # keep the strict all-pass set computed above.
        if basket == "buy_reversal":
            n_filters = len(filters)
            need = _gate_threshold(gate_fails, n_filters)
            adaptive = []
            for s in all_metrics:
                dc = s.get("mom_2d")
                if dc is None or float(dc) <= 0:   # mom_2d>0 mandatory
                    continue
                passed = sum(
                    1 for metric, bounds in filters.items()
                    if _passes(s.get(metric),
                               *(bounds if isinstance(bounds, list) else (bounds[0], bounds[1])))
                )
                if passed >= need:
                    adaptive.append(s)
            universe = adaptive
            log.info(f"buy_reversal adaptive: gate_fails={gate_fails} need={need}/{n_filters} "
                     f"mom_2d>0 mandatory → {len(universe)} qualified")

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO v8_funnel_counts (basket, score_date, counts)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (basket, score_date) DO UPDATE SET
                        counts = EXCLUDED.counts, computed_at = NOW()
                """, (basket, target_date, json.dumps(funnel)))
            conn.commit()
        except Exception as e:
            log.warning(f"funnel {basket}: {e}")

        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM v8_qualified WHERE basket=%s AND signal_date=%s",
                            (basket, target_date))
            conn.commit()
        except Exception as e:
            log.warning(f"clear qualified {basket}: {e}")

        for s in universe:
            sym  = s["symbol"]
            snap = {k: s.get(k) for k in [
                "gvm_score", "dma_50", "dma_200", "dma_20",
                "rsi_month", "rsi_weekly", "daily_rsi",
                "month_return", "week_return", "year_return", "mom_2d",
                "week_index_52", "range_3d", "ma9_vs_ma21", "vol_ratio",
                "sector_week", "sector_month", "sector_day",
            ]}
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO v8_qualified
                        (symbol, basket, signal_date, signal_ts, gvm_score, cmp,
                         mom_2d, week_return, month_return, dma_200, dma_50,
                         rsi_month, rsi_weekly, sector_week, sector_day,
                         month_index, week_index_52, daily_rsi, range_3d,
                         metrics, source)
                        VALUES (%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (symbol, basket, signal_date) DO UPDATE SET
                            signal_ts  = NOW(),
                            cmp        = EXCLUDED.cmp,
                            mom_2d     = EXCLUDED.mom_2d,
                            metrics    = EXCLUDED.metrics,
                            source     = EXCLUDED.source
                    """, (
                        sym, basket, target_date,
                        s.get("gvm_score"), s.get("_cmp"),
                        s.get("mom_2d"), s.get("week_return"), s.get("month_return"),
                        s.get("dma_200"), s.get("dma_50"),
                        s.get("rsi_month"), s.get("rsi_weekly"),
                        s.get("sector_week"), s.get("sector_day"),
                        s.get("month_index"), s.get("week_index_52"),
                        s.get("daily_rsi"), s.get("range_3d"),
                        json.dumps(snap), "live_5min",
                    ))
                conn.commit()
            except Exception as e:
                log.warning(f"qualified insert {basket} {sym}: {e}")


# ── Main entry point ──────────────────────────────────────────────────────────

def run_live_signal_writer(conn) -> dict:
    """
    Called every 5-min from scheduler._live_loop.
    Single live engine — computes all 19 intraday metrics + writes v8_metrics,
    v8_qualified, v8_funnel_counts.
    """
    today = datetime.now(IST).date()

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol FROM futures_universe WHERE is_active = TRUE ORDER BY symbol")
            symbols = [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.error(f"signal_writer: symbols load failed: {e}")
        return {"error": str(e)}

    if not symbols:
        return {"qualified": {}, "msg": "no symbols"}

    eod_metrics = _load_eod_metrics(conn)
    eod_history = _load_eod_history(conn, symbols)
    intraday    = _load_intraday_bars(conn, symbols)
    cmp_map     = _load_cmp(conn)

    if not intraday:
        log.warning("signal_writer: no intraday bars — fyers_feed not running, using EOD fallback")
        all_metrics = []
        for sym in symbols:
            eod  = eod_metrics.get(sym, {})
            cmp  = cmp_map.get(sym)
            c2d  = eod_history.get(sym, {}).get("close_2d_ago")
            row  = dict(eod)
            row["symbol"]  = sym
            row["mom_2d"]  = (cmp / c2d - 1) * 100 if (cmp and c2d and c2d > 0) else eod.get("eod_mom_2d")
            row["_cmp"]    = cmp
            all_metrics.append(row)
        _write_qualified(conn, all_metrics, today)
        return {"source": "eod_fallback", "msg": "no intraday bars"}

    computed: Dict[str, dict] = {}
    no_bar = 0
    for sym in symbols:
        bar  = intraday.get(sym)
        hist = eod_history.get(sym)
        if not bar or not hist or len(hist["closes"]) < 5:
            no_bar += 1
            continue
        eod = eod_metrics.get(sym, {})
        cmp = cmp_map.get(sym)
        m   = _compute_live_metrics(hist, bar, cmp, eod)
        m["symbol"] = sym
        m["_cmp"]   = cmp if cmp else bar["close"]
        computed[sym] = m

    _add_sector_day(computed, eod_metrics)

    all_metrics = []
    for sym, m in computed.items():
        try:
            _upsert_metrics(conn, sym, m, today)
        except Exception as e:
            log.warning(f"upsert_metrics {sym}: {e}")
        all_metrics.append(m)

    _write_qualified(conn, all_metrics, today)

    log.info(f"signal_writer: {len(computed)} updated, {no_bar} no_bar, source=live_5min")
    return {
        "date":    str(today),
        "updated": len(computed),
        "no_bar":  no_bar,
        "total":   len(symbols),
        "source":  "live_5min",
    }
