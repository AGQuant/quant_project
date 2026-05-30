"""
V8 Signal Engine — Scorr
=========================
Computes ~21 metrics per stock from raw_prices + intraday_prices + gvm_scores,
then runs the V8 AND-gate baskets.

Tables:
  v8_universe  — the tradeable futures universe (290 stocks) + signal context
  v8_metrics   — daily computed indicator values per stock
  v8_qualified — stocks passing each basket's AND-gate

Filter thresholds are CANONICAL in v8_endpoints.py (FILTER_CONFIG dict).
This engine computes the raw metrics; v8_endpoints applies the live thresholds.

RSI periods: Month=6, Weekly=8, Daily=14 (Wilder).
"""

import logging
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List
import pandas as pd
import numpy as np

log = logging.getLogger("scorr.v8")

RSI_MONTH_PERIOD = 6
RSI_WEEK_PERIOD  = 8
RSI_DAILY_PERIOD = 14

# ============================================================
# SCHEMA — V8-native
# ============================================================
V8_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS v8_universe (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    cap_type TEXT,
    loaded_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(symbol)
);

CREATE TABLE IF NOT EXISTS v8_metrics (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    score_date DATE NOT NULL,
    gvm_score NUMERIC,
    dma_50 NUMERIC, dma_200 NUMERIC, dma_20 NUMERIC,
    rsi_month NUMERIC, rsi_weekly NUMERIC, daily_rsi NUMERIC,
    month_return NUMERIC, week_return NUMERIC, year_return NUMERIC,
    prev_day_change NUMERIC,
    sector_day NUMERIC, sector_week NUMERIC,
    month_index NUMERIC, week_index_52 NUMERIC,
    range_1d NUMERIC, range_3d NUMERIC,
    upper_bb NUMERIC, lower_bb NUMERIC,
    ma9_vs_ma21 NUMERIC, vol_ratio NUMERIC,
    computed_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(symbol, score_date)
);
CREATE INDEX IF NOT EXISTS idx_v8_metrics_symbol_date ON v8_metrics(symbol, score_date DESC);

CREATE TABLE IF NOT EXISTS v8_qualified (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    score_date DATE NOT NULL,
    gvm_score NUMERIC,
    cmp NUMERIC,
    metrics JSONB,
    qualified_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(symbol, signal_type, score_date)
);
CREATE INDEX IF NOT EXISTS idx_v8_qual_date_type ON v8_qualified(score_date DESC, signal_type);
"""


# ============================================================
# METRIC COMPUTATION
# ============================================================

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


def _safe_pct(numerator: float, denominator: float) -> Optional[float]:
    if denominator is None or denominator == 0 or pd.isna(denominator):
        return None
    return float((numerator / denominator - 1) * 100)


def compute_metrics_for_symbol(conn, symbol: str, target_date: date = None) -> Dict:
    target_date = target_date or date.today()
    out = {
        "symbol": symbol, "score_date": target_date,
        "gvm_score": None, "dma_50": None, "dma_200": None,
        "rsi_month": None, "rsi_weekly": None,
        "month_return": None, "week_return": None, "year_return": None,
        "sector_day": None, "sector_week": None,
        "month_index": None, "week_index_52": None, "range_1d": None,
        "dma_20": None, "range_3d": None, "prev_day_change": None,
        "daily_rsi": None, "upper_bb": None, "lower_bb": None,
        "ma9_vs_ma21": None, "vol_ratio": None,
    }

    with conn.cursor() as cur:
        cur.execute("SELECT gvm_score, segment FROM gvm_scores WHERE symbol = %s", (symbol,))
        row = cur.fetchone()
        if row:
            out["gvm_score"] = float(row[0]) if row[0] is not None else None
            segment = row[1]
        else:
            segment = None

    with conn.cursor() as cur:
        cur.execute("""
            SELECT price_date, close, high, low, volume FROM raw_prices
            WHERE symbol = %s AND price_date <= %s
            ORDER BY price_date DESC LIMIT 400
        """, (symbol, target_date))
        rows = cur.fetchall()
    if not rows:
        return out

    df = pd.DataFrame(rows, columns=["date", "close", "high", "low", "volume"])
    df["close"] = pd.to_numeric(df["close"]); df["high"] = pd.to_numeric(df["high"]); df["low"] = pd.to_numeric(df["low"])
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)
    if len(df) < 5:
        return out

    latest_close = float(df["close"].iloc[-1])

    if len(df) >= 50:  out["dma_50"]  = _safe_pct(latest_close, df["close"].tail(50).mean())
    if len(df) >= 200: out["dma_200"] = _safe_pct(latest_close, df["close"].tail(200).mean())
    if len(df) >= 20:  out["dma_20"]  = _safe_pct(latest_close, df["close"].tail(20).mean())

    if len(df) >= 252: out["year_return"]  = _safe_pct(latest_close, float(df["close"].iloc[-252]))
    if len(df) >= 21:  out["month_return"] = _safe_pct(latest_close, float(df["close"].iloc[-21]))
    if len(df) >= 5:   out["week_return"]  = _safe_pct(latest_close, float(df["close"].iloc[-5]))

    if len(df) >= 2:
        prev1, prev2 = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
        if prev2 > 0: out["prev_day_change"] = (prev1 / prev2 - 1) * 100

    # ma9_vs_ma21 & vol_ratio — matches v8_endpoints sell_overbought formula
    if len(df) >= 21:
        ma9  = float(df["close"].tail(9).mean())
        ma21 = float(df["close"].tail(21).mean())
        if ma21: out["ma9_vs_ma21"] = round((ma9 - ma21) / ma21 * 100, 2)
    if len(df) >= 10:
        vol_avg10 = float(df["volume"].tail(10).mean())
        vol_now   = float(df["volume"].iloc[-1])
        if vol_avg10 and not pd.isna(vol_avg10) and not pd.isna(vol_now):
            out["vol_ratio"] = round(vol_now / vol_avg10, 2)

    df_indexed = df.set_index(pd.to_datetime(df["date"]))
    out["rsi_month"]  = _wilder_rsi(df_indexed["close"].resample("M").last().dropna(), RSI_MONTH_PERIOD)
    out["rsi_weekly"] = _wilder_rsi(df_indexed["close"].resample("W").last().dropna(), RSI_WEEK_PERIOD)
    out["daily_rsi"]  = _wilder_rsi(df["close"], RSI_DAILY_PERIOD)

    if len(df) >= 252:
        r252 = df.tail(252)
        hi, lo = float(r252["high"].max()), float(r252["low"].min())
        if hi > lo: out["week_index_52"] = (latest_close - lo) / (hi - lo) * 100
    if len(df) >= 21:
        r21 = df.tail(21)
        hi, lo = float(r21["high"].max()), float(r21["low"].min())
        if hi > lo: out["month_index"] = (latest_close - lo) / (hi - lo) * 100

    if len(df) >= 4:
        r3 = df.tail(3)
        h3, l3 = float(r3["high"].max()), float(r3["low"].min())
        base = float(df["close"].iloc[-4])
        if base > 0:
            raw = (h3 - l3) / base * 100
            out["range_3d"] = raw if latest_close >= base else -raw

    if len(df) >= 20:
        last20 = df["close"].tail(20)
        ma, sd = float(last20.mean()), float(last20.std())
        if latest_close > 0:
            out["upper_bb"] = (latest_close - (ma + 2*sd)) / latest_close * 100
            out["lower_bb"] = (latest_close - (ma - 2*sd)) / latest_close * 100

    if segment:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT AVG(CASE WHEN t.close>0 AND w.close>0 THEN (t.close/w.close-1)*100 END),
                       AVG(CASE WHEN t.close>0 AND d.close>0 THEN (t.close/d.close-1)*100 END)
                FROM gvm_scores g
                JOIN LATERAL (SELECT close FROM raw_prices WHERE symbol=g.symbol ORDER BY price_date DESC LIMIT 1) t ON true
                JOIN LATERAL (SELECT close FROM raw_prices WHERE symbol=g.symbol ORDER BY price_date DESC OFFSET 5 LIMIT 1) w ON true
                JOIN LATERAL (SELECT close FROM raw_prices WHERE symbol=g.symbol ORDER BY price_date DESC OFFSET 1 LIMIT 1) d ON true
                WHERE g.segment = %s
            """, (segment,))
            s = cur.fetchone()
            if s:
                out["sector_week"] = float(s[0]) if s[0] is not None else None
                out["sector_day"]  = float(s[1]) if s[1] is not None else None

    with conn.cursor() as cur:
        cur.execute("""
            SELECT MAX(high), MIN(low),
                   (SELECT open FROM intraday_prices WHERE symbol=%s ORDER BY ts ASC LIMIT 1)
            FROM intraday_prices
            WHERE symbol=%s AND ts::date=(SELECT MAX(ts::date) FROM intraday_prices WHERE symbol=%s)
        """, (symbol, symbol, symbol))
        ir = cur.fetchone()
        if ir and ir[0] and ir[2]:
            hi, lo, op = float(ir[0]), float(ir[1]), float(ir[2])
            if op > 0:
                out["range_1d"] = ((hi-lo)/op*100) * (1 if latest_close >= op else -1)

    return out


def store_metrics(conn, m: Dict):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO v8_metrics
            (symbol, score_date, gvm_score, dma_50, dma_200, dma_20,
             rsi_month, rsi_weekly, daily_rsi,
             month_return, week_return, year_return, prev_day_change,
             sector_day, sector_week, month_index, week_index_52,
             range_1d, range_3d, upper_bb, lower_bb,
             ma9_vs_ma21, vol_ratio)
            VALUES
            (%(symbol)s, %(score_date)s, %(gvm_score)s, %(dma_50)s, %(dma_200)s, %(dma_20)s,
             %(rsi_month)s, %(rsi_weekly)s, %(daily_rsi)s,
             %(month_return)s, %(week_return)s, %(year_return)s, %(prev_day_change)s,
             %(sector_day)s, %(sector_week)s, %(month_index)s, %(week_index_52)s,
             %(range_1d)s, %(range_3d)s, %(upper_bb)s, %(lower_bb)s,
             %(ma9_vs_ma21)s, %(vol_ratio)s)
            ON CONFLICT (symbol, score_date) DO UPDATE SET
                gvm_score=EXCLUDED.gvm_score, dma_50=EXCLUDED.dma_50, dma_200=EXCLUDED.dma_200, dma_20=EXCLUDED.dma_20,
                rsi_month=EXCLUDED.rsi_month, rsi_weekly=EXCLUDED.rsi_weekly, daily_rsi=EXCLUDED.daily_rsi,
                month_return=EXCLUDED.month_return, week_return=EXCLUDED.week_return,
                year_return=EXCLUDED.year_return, prev_day_change=EXCLUDED.prev_day_change,
                sector_day=EXCLUDED.sector_day, sector_week=EXCLUDED.sector_week,
                month_index=EXCLUDED.month_index, week_index_52=EXCLUDED.week_index_52,
                range_1d=EXCLUDED.range_1d, range_3d=EXCLUDED.range_3d,
                upper_bb=EXCLUDED.upper_bb, lower_bb=EXCLUDED.lower_bb,
                ma9_vs_ma21=EXCLUDED.ma9_vs_ma21, vol_ratio=EXCLUDED.vol_ratio
        """, m)
        conn.commit()


def run_v8_engine(conn, symbols: List[str] = None, target_date: date = None) -> Dict:
    """Compute metrics for the universe. Universe = v8_universe, fallback futures_universe."""
    target_date = target_date or date.today()
    if symbols is None:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM v8_universe ORDER BY symbol")
            symbols = [r[0] for r in cur.fetchall()]
            if not symbols:
                cur.execute("SELECT symbol FROM futures_universe WHERE is_active=TRUE ORDER BY symbol")
                symbols = [r[0] for r in cur.fetchall()]

    results = {"date": str(target_date), "symbols_processed": 0, "errors": []}
    for sym in symbols:
        try:
            m = compute_metrics_for_symbol(conn, sym, target_date)
            store_metrics(conn, m)
            results["symbols_processed"] += 1
        except Exception as e:
            results["errors"].append(f"{sym}: {str(e)[:80]}")
            log.warning(f"V8 engine error on {sym}: {e}")
    log.info(f"V8 engine done: {results['symbols_processed']} symbols")
    return results
