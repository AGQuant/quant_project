"""
V8 Signal Writer — Single Live Engine (v2.1.1)
===============================================
Unified 5-min engine. Replaces v8_live.py + old v8_signal_writer.py.

What it does every 5-min during market hours:
  1. Loads latest EOD v8_metrics row per symbol (slow metrics: GVM, RSI M/W, sector_week, sector_month)
  2. Reads intraday_prices (today's bars) per symbol
  3. Recomputes all live-moving metrics from intraday close spliced onto EOD history
  4. Only EOD-frozen metric: gvm_score (22:00 nightly). All others now live.
  5. Upserts v8_metrics (today's row) with live values
  6. Applies score-based FILTER_CONFIG → writes v8_qualified (latch semantics)
  7. Writes v8_funnel_counts (strict cumulative — diagnostic only)
  8. Writes adr_intraday (live ADR every 5-min tick) — 11-Jun-2026

Score-based qualification (15-Jun-2026):
  BUY  threshold = n - 1 - min(fails, 2)  [tight in bull, loose in bear]
  SELL threshold = n - 3 + min(fails, 2)  [loose in bull, tight in bear — inverse]
  buy_reversal  (10): 9/8/7   buy_momentum  (11): 10/9/8
  sell_reversal  (9): 6/7/8   sell_momentum (12):  9/10/11

All-live filters (15-Jun-2026):
  rsi_month, rsi_weekly, sector_week, sector_month — all recomputed every 5-min.
  GVM stays EOD-frozen (22:00 nightly, screener fundamentals).

Pivot-room gate (15-Jun-2026): pure CMP gate, latch semantics.
  BUY:  pp < cmp <= r1  AND  (r1-cmp) >= 0.5*(r1-pp)
  SELL: s1 <= cmp < pp  AND  (cmp-s1) >= 0.5*(pp-s1)

adr_intraday (11-Jun-2026): per spec id=165.
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

INDEX_SYMBOLS = {"NIFTY50", "BANKNIFTY"}

def _segment_override(symbol: str, segment: Optional[str]) -> Optional[str]:
    if segment:
        return segment
    if symbol in INDEX_SYMBOLS:
        return "Index"
    if symbol.endswith("BEES"):
        return "ETF"
    return segment


# ── helpers ──────────────────────────────────────────────────────────────────

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


# ── Pivot-room gate ───────────────────────────────────────────────────────────

BASKET_SIDE = {
    "buy_reversal":  "BUY",
    "buy_momentum":  "BUY",
    "sell_reversal": "SELL",
    "sell_momentum": "SELL",
}


def _load_pivots(conn) -> Dict[str, dict]:
    """Load latest rolling-5-day pivot levels for all symbols."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, pp, r1, s1
            FROM v8_paper_pivots
            WHERE pivot_date = (SELECT MAX(pivot_date) FROM v8_paper_pivots)
              AND pp IS NOT NULL AND r1 IS NOT NULL AND s1 IS NOT NULL
        """)
        return {r[0]: {"pp": float(r[1]), "r1": float(r[2]), "s1": float(r[3])}
                for r in cur.fetchall()}


def _pivot_room_ok(side: str, cmp: Optional[float],
                    pp: Optional[float], r1: Optional[float],
                    s1: Optional[float]) -> bool:
    """
    Paper-engine pivot-room gate — pure CMP gate.
    BUY:  pp < cmp <= r1  AND  (r1 - cmp) >= 0.5 * (r1 - pp)
    SELL: s1 <= cmp < pp  AND  (cmp - s1) >= 0.5 * (pp - s1)
    """
    if cmp is None or pp is None:
        return False
    if side == "BUY":
        if r1 is None:
            return False
        band = r1 - pp
        return band > 0 and pp < cmp <= r1 and (r1 - cmp) >= 0.5 * band
    else:
        if s1 is None:
            return False
        band = pp - s1
        return band > 0 and s1 <= cmp < pp and (cmp - s1) >= 0.5 * band


# ── ADR intraday write ────────────────────────────────────────────────────────

def _write_adr_intraday(conn):
    try:
        with conn.cursor() as cur:
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
                SELECT
                    COUNT(*) FILTER (WHERE li.cmp > pc.pclose) AS advances,
                    COUNT(*) FILTER (WHERE li.cmp < pc.pclose) AS declines,
                    COUNT(*) FILTER (WHERE li.cmp = pc.pclose) AS unchanged,
                    COUNT(*) AS total
                FROM li JOIN pc ON pc.symbol = li.symbol
            """)
            row = cur.fetchone()
            if not row or (row[3] or 0) < 50:
                return
            adv, dec, unc, tot = row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0
            adr = round(adv / dec, 3) if dec else float(adv)
            now_ist = datetime.now(IST).replace(tzinfo=None)
            ts_5m = now_ist.replace(second=0, microsecond=0)
            ts_5m = ts_5m.replace(minute=(ts_5m.minute // 5) * 5)
            cur.execute("""
                INSERT INTO adr_intraday (ts, advances, declines, unchanged, adr, universe_count)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (ts) DO UPDATE SET
                    advances       = EXCLUDED.advances,
                    declines       = EXCLUDED.declines,
                    unchanged      = EXCLUDED.unchanged,
                    adr            = EXCLUDED.adr,
                    universe_count = EXCLUDED.universe_count,
                    computed_at    = NOW()
            """, (ts_5m, adv, dec, unc, adr, tot))
        conn.commit()
        log.debug(f"adr_intraday: {adv}A/{dec}D adr={adr} universe={tot}")
    except Exception as e:
        log.warning(f"_write_adr_intraday: {e}")


# ── Step 1: Load EOD metrics snapshot ────────────────────────────────────────

def _load_eod_metrics(conn) -> Dict[str, dict]:
    gvm_map: Dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, gvm_score, segment
            FROM gvm_scores
            WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)
        """)
        for sym, gvm, seg in cur.fetchall():
            gvm_map[sym] = {"gvm_score": _safe_float(gvm), "segment": seg}

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

    out: Dict[str, dict] = {}
    for sym in set(gvm_map) | set(frozen_map):
        g = gvm_map.get(sym, {})
        f = frozen_map.get(sym, {})
        out[sym] = {
            "symbol":        sym,
            "gvm_score":     g.get("gvm_score"),
            "segment":       _segment_override(sym, g.get("segment")),
            "rsi_month":     _safe_float(f.get("rsi_month")),
            "rsi_weekly":    _safe_float(f.get("rsi_weekly")),
            "sector_week":   _safe_float(f.get("sector_week")),
            "sector_month":  _safe_float(f.get("sector_month")),
            "eod_mom_2d":    _safe_float(f.get("eod_mom_2d")),
        }
    return out


# ── Step 2: Load EOD history per symbol (bulk) ───────────────────────────────

def _load_eod_history(conn, symbols: List[str]) -> Dict[str, dict]:
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
        data    = data[:400][::-1]
        closes  = [float(r[0]) for r in data if r[0] is not None]
        highs   = [float(r[1]) for r in data if r[1] is not None]
        lows    = [float(r[2]) for r in data if r[2] is not None]
        vols    = [float(r[3]) for r in data if r[3] is not None]

        history[sym] = {
            "closes":       closes,
            "highs":        highs,
            "lows":         lows,
            "vols":         vols,
            "vol_avg10":    float(np.mean(vols[-10:])) if len(vols) >= 10 else None,
            "hi_252":       float(max(highs[-252:])) if len(highs) >= 252 else (float(max(highs)) if highs else None),
            "lo_252":       float(min(lows[-252:]))  if len(lows)  >= 252 else (float(min(lows))  if lows  else None),
            "hi_21":        float(max(highs[-21:]))  if len(highs) >= 21  else (float(max(highs)) if highs else None),
            "lo_21":        float(min(lows[-21:]))   if len(lows)  >= 21  else (float(min(lows))  if lows  else None),
            "close_1d_ago": closes[-1] if len(closes) >= 1 else None,
            "close_2d_ago": closes[-2] if len(closes) >= 2 else None,
        }
    return history


# ── Step 3: Load today's intraday bars (bulk) ─────────────────────────────────

def _load_intraday_bars(conn, symbols: List[str]) -> Dict[str, dict]:
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


# ── Step 5: Compute live metrics ──────────────────────────────────────────────

def _compute_live_metrics(hist: dict, bar: dict, cmp: Optional[float],
                           eod: dict) -> dict:
    closes = hist["closes"][:]
    highs  = hist["highs"][:]
    lows   = hist["lows"][:]
    live   = bar["close"]

    c = closes + [live]
    h = highs  + [bar["high"] if bar.get("high") else live]
    l = lows   + [bar["low"]  if bar.get("low")  else live]

    out = {
        "gvm_score":    _safe_float(eod.get("gvm_score")),
        "rsi_month":    None,
        "rsi_weekly":   None,
        "sector_week":  _safe_float(eod.get("sector_week")),
        "sector_month": _safe_float(eod.get("sector_month")),
        "dma_20": None, "dma_50": None, "dma_200": None,
        "daily_rsi": None,
        "month_return": None, "week_return": None, "year_return": None,
        "mom_2d": None, "day_1d": None, "eod_chg": None,
        "ma9_vs_ma21": None, "vol_ratio": None,
        "week_index_52": None, "month_index": None,
        "range_1d": None, "range_3d": None,
        "upper_bb": None, "lower_bb": None,
        "sector_day": None,
    }

    if len(c) >= 20:  out["dma_20"]  = _safe_pct(live, float(np.mean(c[-20:])))
    if len(c) >= 50:  out["dma_50"]  = _safe_pct(live, float(np.mean(c[-50:])))
    if len(c) >= 200: out["dma_200"] = _safe_pct(live, float(np.mean(c[-200:])))

    if len(c) >= 253: out["year_return"]  = _safe_pct(live, c[-253])
    if len(c) >= 22:  out["month_return"] = _safe_pct(live, c[-22])
    if len(c) >= 6:   out["week_return"]  = _safe_pct(live, c[-6])

    price = cmp if cmp else live

    base_2d = hist.get("close_2d_ago")
    if base_2d and base_2d > 0:
        out["mom_2d"] = (price / base_2d - 1) * 100

    base_1d = hist.get("close_1d_ago")
    if base_1d and base_1d > 0:
        out["day_1d"] = (price / base_1d - 1) * 100
        if base_2d and base_2d > 0:
            out["eod_chg"] = (base_1d / base_2d - 1) * 100

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

    # Live RSI: monthly RSI(6) + weekly RSI(8)
    MONTH_BARS, WEEK_BARS = 22, 5
    if len(c) >= MONTH_BARS * 7:
        monthly = pd.Series([c[i] for i in range(-MONTH_BARS * 7, 0, MONTH_BARS)] + [c[-1]])
        out["rsi_month"] = _wilder_rsi(monthly, 6)
    else:
        out["rsi_month"] = _safe_float(eod.get("rsi_month"))

    if len(c) >= WEEK_BARS * 9:
        weekly_s = pd.Series([c[i] for i in range(-WEEK_BARS * 9, 0, WEEK_BARS)] + [c[-1]])
        out["rsi_weekly"] = _wilder_rsi(weekly_s, 8)
    else:
        out["rsi_weekly"] = _safe_float(eod.get("rsi_weekly"))

    out["_live"] = live
    return out


# ── Step 6: Sector aggregates (live) ─────────────────────────────────────────

def _add_sector_aggregates(computed: Dict[str, dict], eod_metrics: Dict[str, dict]):
    """Live sector_day, sector_week, sector_month — all 5-min."""
    seg_day:   Dict[str, list] = defaultdict(list)
    seg_week:  Dict[str, list] = defaultdict(list)
    seg_month: Dict[str, list] = defaultdict(list)

    for sym, m in computed.items():
        seg = eod_metrics.get(sym, {}).get("segment")
        if not seg:
            continue
        if m.get("mom_2d")       is not None: seg_day[seg].append(m["mom_2d"])
        if m.get("week_return")  is not None: seg_week[seg].append(m["week_return"])
        if m.get("month_return") is not None: seg_month[seg].append(m["month_return"])

    day_avg   = {seg: float(np.mean(v)) for seg, v in seg_day.items()   if v}
    week_avg  = {seg: float(np.mean(v)) for seg, v in seg_week.items()  if v}
    month_avg = {seg: float(np.mean(v)) for seg, v in seg_month.items() if v}

    for sym, m in computed.items():
        seg = eod_metrics.get(sym, {}).get("segment")
        m["sector_day"]   = day_avg.get(seg)
        m["sector_week"]  = week_avg.get(seg)
        m["sector_month"] = month_avg.get(seg)


# ── Step 7: Upsert v8_metrics ─────────────────────────────────────────────────

def _upsert_metrics(conn, sym: str, m: dict, target_date: date):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO v8_metrics
            (symbol, score_date, gvm_score,
             dma_20, dma_50, dma_200, daily_rsi,
             rsi_month, rsi_weekly,
             month_return, week_return, year_return, mom_2d,
             day_1d, eod_chg,
             sector_day, sector_week, sector_month,
             month_index, week_index_52,
             range_1d, range_3d, upper_bb, lower_bb,
             ma9_vs_ma21, vol_ratio)
            VALUES (%s,%s,%s, %s,%s,%s,%s, %s,%s, %s,%s,%s,%s,
                    %s,%s, %s,%s,%s, %s,%s, %s,%s,%s,%s, %s,%s)
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
                day_1d        = EXCLUDED.day_1d,
                eod_chg       = EXCLUDED.eod_chg,
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
            m.get("day_1d"), m.get("eod_chg"),
            m.get("sector_day"), m.get("sector_week"), m.get("sector_month"),
            m.get("month_index"), m.get("week_index_52"),
            m.get("range_1d"), m.get("range_3d"), m.get("upper_bb"), m.get("lower_bb"),
            m.get("ma9_vs_ma21"), m.get("vol_ratio"),
        ))
    conn.commit()


# ── Market gate ───────────────────────────────────────────────────────────────

def _market_gate_fails(conn) -> int:
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT advances, declines, universe_count
                FROM adr_intraday
                WHERE ts::date = CURRENT_DATE
                ORDER BY ts DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row and (row[2] or 0) >= 50:
                adv, dec = row[0] or 0, row[1] or 0
                adr = (adv / dec) if dec else float(adv)
            else:
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
                adv_row = cur.fetchone()
                if adv_row and (adv_row[2] or 0) >= 50:
                    adv, dec = adv_row[0] or 0, adv_row[1] or 0
                    adr = (adv / dec) if dec else float(adv)
                else:
                    cur.execute("SELECT adr FROM adr_daily ORDER BY price_date DESC LIMIT 1")
                    r = cur.fetchone()
                    adr = float(r[0]) if r and r[0] is not None else 1.0

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
                return 0

            checks = [adr >= 1.0, nday >= 0, nweek >= 0, nmonth >= 0]
            return sum(1 for c in checks if not c)
    except Exception as e:
        log.warning(f"_market_gate_fails: {e}")
        return 0


def _gate_threshold(fails: int, n_filters: int, side: str = "BUY") -> int:
    """
    Score-based adaptive threshold.
    BUY:  Strong Bullish → n-1 (tight), Neutral/Bear → n-3 (loose)
          Formula: n_filters - 1 - min(fails, 2)
    SELL: Strong Bullish → n-3 (loose — sells scarce in bull market)
          Neutral/Bear   → n-1 (tight — sells plentiful, be selective)
          Formula: n_filters - 3 + min(fails, 2)   [inverse of BUY]
    """
    if side == "SELL":
        return max(n_filters - 3 + min(fails, 2), 1)
    return n_filters - 1 - min(fails, 2)


# ── Step 8: Write v8_qualified + funnel ──────────────────────────────────────

def _write_qualified(conn, all_metrics: List[dict], target_date: date):
    from v8_endpoints import FILTER_CONFIG

    gate_fails = _market_gate_fails(conn)
    pivots     = _load_pivots(conn)

    for basket, filters in FILTER_CONFIG.items():
        if basket == "sell_overbought":
            continue

        # ── Score-based qualification — all 4 baskets ──────────────────────
        n_filters = len(filters)
        side      = BASKET_SIDE.get(basket, "BUY")
        need      = _gate_threshold(gate_fails, n_filters, side)

        universe = []
        for s in all_metrics:
            score = sum(
                1 for metric, bounds in filters.items()
                if _passes(s.get(metric),
                           *(bounds if isinstance(bounds, list) else (bounds[0], bounds[1])))
            )
            s["_filter_score"] = score
            if score >= need:
                universe.append(s)

        # Funnel counts (strict cumulative — diagnostic display, not used for qualification)
        funnel    = {}
        survivors = all_metrics[:]
        for metric, bounds in filters.items():
            mn, mx    = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            survivors = [s for s in survivors if _passes(s.get(metric), mn, mx)]
            funnel[metric] = len(survivors)
        funnel["_score_threshold"] = need
        funnel["_score_qualified"] = len(universe)

        log.info(f"{basket}: score-gate need={need}/{n_filters} "
                 f"gate_fails={gate_fails} → {len(universe)} score-qualified")

        # Pivot-room gate — latch semantics (side already set above)
        universe = [
            s for s in universe
            if (pv := pivots.get(s["symbol"])) and _pivot_room_ok(
                side, s.get("_cmp"), pv["pp"], pv["r1"], pv["s1"]
            )
        ]
        log.info(f"{basket}: pivot-room gate ({side}) → {len(universe)} with room")

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

        # NO DELETE — latch semantics
        for s in universe:
            sym  = s["symbol"]
            snap = {k: s.get(k) for k in [
                "gvm_score", "dma_50", "dma_200", "dma_20",
                "rsi_month", "rsi_weekly", "daily_rsi",
                "month_return", "week_return", "year_return", "mom_2d",
                "week_index_52", "range_3d", "ma9_vs_ma21", "vol_ratio",
                "sector_week", "sector_month", "sector_day",
            ]}
            snap["filter_score"] = s.get("_filter_score")
            snap["filter_total"] = n_filters
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
                        ON CONFLICT (symbol, basket, signal_date) DO NOTHING
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

    _add_sector_aggregates(computed, eod_metrics)

    all_metrics = []
    for sym, m in computed.items():
        try:
            _upsert_metrics(conn, sym, m, today)
        except Exception as e:
            log.warning(f"upsert_metrics {sym}: {e}")
        all_metrics.append(m)

    _write_qualified(conn, all_metrics, today)

    _write_adr_intraday(conn)

    log.info(f"signal_writer: {len(computed)} updated, {no_bar} no_bar, source=live_5min")
    return {
        "date":    str(today),
        "updated": len(computed),
        "no_bar":  no_bar,
        "total":   len(symbols),
        "source":  "live_5min",
    }
