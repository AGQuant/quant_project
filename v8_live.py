"""
V8 Live Engine — Scorr
=======================
Two-part live intraday metric computation:

1. build_history_cache(conn)  — run ONCE pre-open (~9:00 IST). Reads raw_prices
   (the heavy 400-day pull) for every active future + sector peers, stores the
   FIXED history arrays into v8_history_cache. One heavy read per day.

2. run_live_tick(conn)        — run every 1 min during market hours. Reads only
   the latest intraday_prices bar per stock (~210 tiny reads), splices the live
   price/volume into the cached history, recomputes all 19 live-moving metrics,
   and upserts today's v8_metrics row. ~210 reads + 210 writes/min — trivial load.

Static intraday (NOT recomputed live): gvm_score, prev_day_change.
Everything else (19 metrics) uses today's live price/volume and IS recomputed.

Formulas match v8_engine.py exactly so EOD and live agree at the close.
RSI periods: Month=6, Weekly=8, Daily=14 (Wilder).
"""

import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional, Dict, List
import pandas as pd
import numpy as np

log = logging.getLogger("scorr.v8live")

RSI_MONTH_PERIOD = 6
RSI_WEEK_PERIOD  = 8
RSI_DAILY_PERIOD = 14


def _wilder_rsi(closes: pd.Series, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return float(val) if pd.notna(val) else None


def _safe_pct(num: float, den: float) -> Optional[float]:
    if den is None or den == 0 or pd.isna(den):
        return None
    return float((num / den - 1) * 100)


# ============================================================
# 1. CACHE BUILDER  (heavy, once/day pre-open)
# ============================================================

def build_history_cache(conn, target_date: date = None) -> Dict:
    """Populate v8_history_cache with fixed daily history for all active futures.
    History EXCLUDES today (live bar splices in at tick time)."""
    target_date = target_date or date.today()

    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe WHERE is_active = TRUE ORDER BY symbol")
        symbols = [r[0] for r in cur.fetchall()]

    built, errors = 0, []
    for sym in symbols:
        try:
            # GVM + segment
            with conn.cursor() as cur:
                cur.execute("SELECT gvm_score, segment FROM gvm_scores WHERE symbol = %s", (sym,))
                g = cur.fetchone()
            gvm = float(g[0]) if g and g[0] is not None else None
            segment = g[1] if g else None

            # 400 daily bars STRICTLY BEFORE today (history is fixed)
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT close, high, low, volume FROM raw_prices
                    WHERE symbol = %s AND price_date < %s
                    ORDER BY price_date DESC LIMIT 400
                """, (sym, target_date))
                rows = cur.fetchall()
            if len(rows) < 21:
                errors.append(f"{sym}: <21 days history")
                continue

            rows = rows[::-1]  # oldest -> newest
            closes  = [float(r[0]) for r in rows if r[0] is not None]
            highs   = [float(r[1]) for r in rows if r[1] is not None]
            lows    = [float(r[2]) for r in rows if r[2] is not None]
            vols    = [float(r[3]) for r in rows if r[3] is not None]

            vol_avg10 = float(np.mean(vols[-10:])) if len(vols) >= 10 else None
            hi_252 = float(max(highs[-252:])) if highs else None
            lo_252 = float(min(lows[-252:])) if lows else None
            hi_21  = float(max(highs[-21:])) if highs else None
            lo_21  = float(min(lows[-21:])) if lows else None

            prev_day_change = None
            if len(closes) >= 2 and closes[-2] > 0:
                prev_day_change = (closes[-1] / closes[-2] - 1) * 100

            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO v8_history_cache
                    (symbol, cache_date, closes, highs, lows, volumes, segment,
                     vol_avg10, hi_252, lo_252, hi_21, lo_21, gvm_score, prev_day_change, built_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                    ON CONFLICT (symbol) DO UPDATE SET
                        cache_date=EXCLUDED.cache_date, closes=EXCLUDED.closes, highs=EXCLUDED.highs,
                        lows=EXCLUDED.lows, volumes=EXCLUDED.volumes, segment=EXCLUDED.segment,
                        vol_avg10=EXCLUDED.vol_avg10, hi_252=EXCLUDED.hi_252, lo_252=EXCLUDED.lo_252,
                        hi_21=EXCLUDED.hi_21, lo_21=EXCLUDED.lo_21, gvm_score=EXCLUDED.gvm_score,
                        prev_day_change=EXCLUDED.prev_day_change, built_at=NOW()
                """, (sym, target_date, json.dumps(closes), json.dumps(highs), json.dumps(lows),
                      json.dumps(vols), segment, vol_avg10, hi_252, lo_252, hi_21, lo_21,
                      gvm, prev_day_change))
                conn.commit()
            built += 1
        except Exception as e:
            errors.append(f"{sym}: {str(e)[:60]}")
            log.warning(f"cache build {sym}: {e}")

    log.info(f"v8_history_cache built: {built}/{len(symbols)}")
    return {"date": str(target_date), "built": built, "total": len(symbols), "errors": errors[:10]}


# ============================================================
# 2. LIVE TICK  (light, every 1 min)
# ============================================================

def _today_live_bar(conn, sym: str, target_date: date):
    """Latest live bar + today's high/low/open/cum-volume from intraday_prices (market hours only)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                (SELECT close FROM intraday_prices
                   WHERE symbol=%s AND ts::date=%s AND ts::time <= '15:30'
                   ORDER BY ts DESC LIMIT 1)                                  AS live_close,
                (SELECT open FROM intraday_prices
                   WHERE symbol=%s AND ts::date=%s AND ts::time >= '09:15'
                   ORDER BY ts ASC LIMIT 1)                                   AS day_open,
                MAX(high) FILTER (WHERE ts::time <= '15:30')                  AS day_high,
                MIN(low)  FILTER (WHERE ts::time <= '15:30')                  AS day_low,
                MAX(volume) FILTER (WHERE ts::time <= '15:30')               AS day_vol
            FROM intraday_prices
            WHERE symbol=%s AND ts::date=%s
        """, (sym, target_date, sym, target_date, sym, target_date))
        r = cur.fetchone()
    if not r or r[0] is None:
        return None
    return {"close": float(r[0]),
            "open": float(r[1]) if r[1] is not None else None,
            "high": float(r[2]) if r[2] is not None else None,
            "low":  float(r[3]) if r[3] is not None else None,
            "volume": float(r[4]) if r[4] is not None else None}


def compute_live_metrics(cache_row: Dict, bar: Dict) -> Dict:
    """Recompute the 19 live-moving metrics from cached history + today's live bar."""
    closes = list(cache_row["closes"]); highs = list(cache_row["highs"]); lows = list(cache_row["lows"])
    live = bar["close"]

    # Splice today's live values onto the fixed history
    c = closes + [live]
    h = highs + [bar["high"] if bar.get("high") else live]
    l = lows  + [bar["low"]  if bar.get("low")  else live]

    out = {
        "gvm_score": cache_row.get("gvm_score"),
        "prev_day_change": cache_row.get("prev_day_change"),
        "dma_20": None, "dma_50": None, "dma_200": None,
        "rsi_month": None, "rsi_weekly": None, "daily_rsi": None,
        "month_return": None, "week_return": None, "year_return": None,
        "month_index": None, "week_index_52": None,
        "range_1d": None, "range_3d": None,
        "upper_bb": None, "lower_bb": None,
        "ma9_vs_ma21": None, "vol_ratio": None,
        "sector_day": None, "sector_week": None,  # filled by caller (sector pass)
    }

    if len(c) >= 20:  out["dma_20"]  = _safe_pct(live, float(np.mean(c[-20:])))
    if len(c) >= 50:  out["dma_50"]  = _safe_pct(live, float(np.mean(c[-50:])))
    if len(c) >= 200: out["dma_200"] = _safe_pct(live, float(np.mean(c[-200:])))

    if len(c) >= 253: out["year_return"]  = _safe_pct(live, c[-253])
    if len(c) >= 22:  out["month_return"] = _safe_pct(live, c[-22])
    if len(c) >= 6:   out["week_return"]  = _safe_pct(live, c[-6])

    s = pd.Series(c)
    out["daily_rsi"] = _wilder_rsi(s, RSI_DAILY_PERIOD)

    if len(c) >= 21:
        ma9, ma21 = float(np.mean(c[-9:])), float(np.mean(c[-21:]))
        if ma21: out["ma9_vs_ma21"] = round((ma9 - ma21) / ma21 * 100, 2)

    if bar.get("volume") and cache_row.get("vol_avg10"):
        va = float(cache_row["vol_avg10"])
        if va: out["vol_ratio"] = round(bar["volume"] / va, 2)

    # 52-week & 21-day index: extend cached H/L with today's live H/L
    hi252 = max([x for x in [cache_row.get("hi_252"), bar.get("high"), live] if x is not None])
    lo252 = min([x for x in [cache_row.get("lo_252"), bar.get("low"),  live] if x is not None])
    if hi252 > lo252: out["week_index_52"] = (live - lo252) / (hi252 - lo252) * 100
    hi21 = max([x for x in [cache_row.get("hi_21"), bar.get("high"), live] if x is not None])
    lo21 = min([x for x in [cache_row.get("lo_21"), bar.get("low"),  live] if x is not None])
    if hi21 > lo21: out["month_index"] = (live - lo21) / (hi21 - lo21) * 100

    # range_1d: today's intraday (high-low)/open, signed by close vs open
    if bar.get("open") and bar.get("high") is not None and bar.get("low") is not None and bar["open"] > 0:
        raw = (bar["high"] - bar["low"]) / bar["open"] * 100
        out["range_1d"] = raw if live >= bar["open"] else -raw

    # range_3d: last 2 prior closes + today
    if len(c) >= 4:
        h3 = max(h[-3:]); l3 = min(l[-3:]); base = c[-4]
        if base > 0:
            raw = (h3 - l3) / base * 100
            out["range_3d"] = raw if live >= base else -raw

    # Monthly/Weekly RSI need resampled series — approximate with daily proxy is wrong,
    # so leave rsi_month / rsi_weekly to the EOD engine value (they barely move intraday).
    # Caller preserves existing v8_metrics rsi_month/rsi_weekly rather than overwriting.

    if len(c) >= 20:
        last20 = c[-20:]
        ma, sd = float(np.mean(last20)), float(np.std(last20, ddof=1))
        if live > 0:
            out["upper_bb"] = (live - (ma + 2*sd)) / live * 100
            out["lower_bb"] = (live - (ma - 2*sd)) / live * 100

    return out


def run_live_tick(conn, target_date: date = None) -> Dict:
    """Every-minute: live bar -> recompute 19 metrics -> upsert v8_metrics. Sector pass after."""
    target_date = target_date or date.today()

    with conn.cursor() as cur:
        cur.execute("SELECT symbol, closes, highs, lows, segment, vol_avg10, hi_252, lo_252, hi_21, lo_21, gvm_score, prev_day_change FROM v8_history_cache")
        cache = {}
        for r in cur.fetchall():
            cache[r[0]] = {
                "closes": r[1], "highs": r[2], "lows": r[3], "segment": r[4],
                "vol_avg10": r[5], "hi_252": r[6], "lo_252": r[7],
                "hi_21": r[8], "lo_21": r[9], "gvm_score": r[10], "prev_day_change": r[11],
            }
    if not cache:
        return {"status": "warn", "msg": "history cache empty — run build_history_cache first"}

    updated, no_bar = 0, 0
    computed = {}  # symbol -> metrics (for sector pass)
    for sym, crow in cache.items():
        bar = _today_live_bar(conn, sym, target_date)
        if not bar:
            no_bar += 1
            continue
        m = compute_live_metrics(crow, bar)
        m["_segment"] = crow.get("segment")
        m["_live"] = bar["close"]
        computed[sym] = m

    # ---- Sector pass: sector_day / sector_week = avg of constituents' live moves ----
    # day move = live vs prev close ; week move = live vs close 5d ago, per stock.
    seg_day, seg_wk = {}, {}
    for sym, m in computed.items():
        seg = m.get("_segment")
        if not seg: continue
        crow = cache[sym]
        cl = crow["closes"]
        live = m["_live"]
        if len(cl) >= 1 and cl[-1] > 0:
            seg_day.setdefault(seg, []).append((live / cl[-1] - 1) * 100)
        if len(cl) >= 5 and cl[-5] > 0:
            seg_wk.setdefault(seg, []).append((live / cl[-5] - 1) * 100)
    seg_day_avg = {k: float(np.mean(v)) for k, v in seg_day.items() if v}
    seg_wk_avg  = {k: float(np.mean(v)) for k, v in seg_wk.items() if v}

    for sym, m in computed.items():
        seg = m.get("_segment")
        m["sector_day"]  = seg_day_avg.get(seg)
        m["sector_week"] = seg_wk_avg.get(seg)

        # Upsert — preserve EOD rsi_month/rsi_weekly (not recomputed live)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO v8_metrics
                (symbol, score_date, gvm_score, dma_20, dma_50, dma_200,
                 daily_rsi, month_return, week_return, year_return, prev_day_change,
                 sector_day, sector_week, month_index, week_index_52,
                 range_1d, range_3d, upper_bb, lower_bb, ma9_vs_ma21, vol_ratio)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, score_date) DO UPDATE SET
                    gvm_score=EXCLUDED.gvm_score, dma_20=EXCLUDED.dma_20, dma_50=EXCLUDED.dma_50,
                    dma_200=EXCLUDED.dma_200, daily_rsi=EXCLUDED.daily_rsi,
                    month_return=EXCLUDED.month_return, week_return=EXCLUDED.week_return,
                    year_return=EXCLUDED.year_return, prev_day_change=EXCLUDED.prev_day_change,
                    sector_day=EXCLUDED.sector_day, sector_week=EXCLUDED.sector_week,
                    month_index=EXCLUDED.month_index, week_index_52=EXCLUDED.week_index_52,
                    range_1d=EXCLUDED.range_1d, range_3d=EXCLUDED.range_3d,
                    upper_bb=EXCLUDED.upper_bb, lower_bb=EXCLUDED.lower_bb,
                    ma9_vs_ma21=EXCLUDED.ma9_vs_ma21, vol_ratio=EXCLUDED.vol_ratio
            """, (sym, target_date, m["gvm_score"], m["dma_20"], m["dma_50"], m["dma_200"],
                  m["daily_rsi"], m["month_return"], m["week_return"], m["year_return"], m["prev_day_change"],
                  m["sector_day"], m["sector_week"], m["month_index"], m["week_index_52"],
                  m["range_1d"], m["range_3d"], m["upper_bb"], m["lower_bb"], m["ma9_vs_ma21"], m["vol_ratio"]))
            conn.commit()
        updated += 1

    return {"date": str(target_date), "updated": updated, "no_bar": no_bar, "cached": len(cache)}
