"""
V5 Filter Engine — Scorr / Project Quant
=========================================
Computes 17 metrics per stock from raw_prices + intraday_prices + gvm_scores.
Runs 4 AND-gate filters: Buy_Reversal, Buy_Momentum, Sell_Reversal, Sell_Momentum.

Replaces V5 Google Sheets logic. GVM replaces Finkhoz everywhere.

RSI periods:
  - RSI Month: 6 periods (6 months)
  - RSI Weekly: 8 periods (8 weeks)
  - RSI Daily: 14 periods (standard Wilder)

Metrics computed (17):
  gvm_score, dma_50, dma_200, dma_20,
  rsi_month, rsi_weekly, daily_rsi,
  month_return, week_return, year_return,
  prev_day_change, range_1d, range_3d,
  sector_day, sector_week,
  month_index, week_index_52,
  upper_bb, lower_bb

Tables created:
  v5_filters   — editable threshold config (min/max per metric per signal type)
  v5_metrics   — daily computed values per stock
  v5_qualified — stocks passing each AND-gate
"""

import logging
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List, Tuple
import pandas as pd
import numpy as np

log = logging.getLogger("scorr.v5")

# RSI periods — user-tuned defaults
RSI_MONTH_PERIOD = 6
RSI_WEEK_PERIOD  = 8
RSI_DAILY_PERIOD = 14

# ============================================================
# SCHEMA
# ============================================================

V5_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS v5_filters (
    id SERIAL PRIMARY KEY,
    signal_type TEXT NOT NULL,
    metric TEXT NOT NULL,
    min_val NUMERIC,
    max_val NUMERIC,
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(signal_type, metric)
);

CREATE TABLE IF NOT EXISTS v5_metrics (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    score_date DATE NOT NULL,
    gvm_score NUMERIC,
    dma_50 NUMERIC,
    dma_200 NUMERIC,
    dma_20 NUMERIC,
    rsi_month NUMERIC,
    rsi_weekly NUMERIC,
    daily_rsi NUMERIC,
    month_return NUMERIC,
    week_return NUMERIC,
    year_return NUMERIC,
    prev_day_change NUMERIC,
    sector_day NUMERIC,
    sector_week NUMERIC,
    month_index NUMERIC,
    week_index_52 NUMERIC,
    range_1d NUMERIC,
    range_3d NUMERIC,
    upper_bb NUMERIC,
    lower_bb NUMERIC,
    computed_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(symbol, score_date)
);
CREATE INDEX IF NOT EXISTS idx_v5_metrics_symbol_date ON v5_metrics(symbol, score_date DESC);

-- Safe migration: add new columns if table already existed without them
ALTER TABLE v5_metrics ADD COLUMN IF NOT EXISTS dma_20 NUMERIC;
ALTER TABLE v5_metrics ADD COLUMN IF NOT EXISTS range_3d NUMERIC;
ALTER TABLE v5_metrics ADD COLUMN IF NOT EXISTS prev_day_change NUMERIC;
ALTER TABLE v5_metrics ADD COLUMN IF NOT EXISTS daily_rsi NUMERIC;
ALTER TABLE v5_metrics ADD COLUMN IF NOT EXISTS upper_bb NUMERIC;
ALTER TABLE v5_metrics ADD COLUMN IF NOT EXISTS lower_bb NUMERIC;

CREATE TABLE IF NOT EXISTS v5_qualified (
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
CREATE INDEX IF NOT EXISTS idx_v5_qual_date_type ON v5_qualified(score_date DESC, signal_type);
"""

# ============================================================
# DEFAULT FILTERS — GVM replaces Finkhoz everywhere
# ============================================================
DEFAULT_FILTERS = [
    # Buy Reversal — uptrend reversal entry
    ("Buy_Reversal", "gvm_score",       7,    10),
    ("Buy_Reversal", "year_return",     0,    None),
    ("Buy_Reversal", "dma_200",         0,    20),
    ("Buy_Reversal", "dma_50",          0,    8),
    ("Buy_Reversal", "rsi_month",       45,   75),
    ("Buy_Reversal", "rsi_weekly",      50,   75),
    ("Buy_Reversal", "month_return",    0,    8),
    ("Buy_Reversal", "week_return",     0,    3),
    ("Buy_Reversal", "sector_week",     0,    5),
    ("Buy_Reversal", "sector_day",      0,    3),
    ("Buy_Reversal", "month_index",     50,   100),
    ("Buy_Reversal", "range_1d",        0.5,  2),

    # Buy Momentum — uptrend continuation
    ("Buy_Momentum", "gvm_score",       7,    10),
    ("Buy_Momentum", "year_return",     10,   None),
    ("Buy_Momentum", "dma_200",         10,   40),
    ("Buy_Momentum", "dma_50",          5,    20),
    ("Buy_Momentum", "rsi_month",       55,   80),
    ("Buy_Momentum", "rsi_weekly",      55,   80),
    ("Buy_Momentum", "month_return",    3,    15),
    ("Buy_Momentum", "week_return",     1,    8),
    ("Buy_Momentum", "sector_week",     0,    8),
    ("Buy_Momentum", "sector_day",      0,    5),
    ("Buy_Momentum", "month_index",     70,   100),
    ("Buy_Momentum", "range_1d",        0.5,  3),

    # Sell Reversal — downtrend reversal entry
    ("Sell_Reversal", "gvm_score",      0,   5),
    ("Sell_Reversal", "month_index",    0,   50),
    ("Sell_Reversal", "sector_week",    -10, 0),
    ("Sell_Reversal", "sector_day",     -2,  0),
    ("Sell_Reversal", "dma_200",        -25, 0),
    ("Sell_Reversal", "dma_50",         -15, 0),
    ("Sell_Reversal", "rsi_month",      20,  55),
    ("Sell_Reversal", "rsi_weekly",     15,  50),
    ("Sell_Reversal", "month_return",   -15, 0),
    ("Sell_Reversal", "week_return",    -4,  1),
    ("Sell_Reversal", "range_1d",       -2,  0),

    # Sell Momentum — downtrend continuation
    ("Sell_Momentum", "gvm_score",      0,   5),
    ("Sell_Momentum", "month_index",    0,   35),
    ("Sell_Momentum", "sector_week",    -8,  -1),
    ("Sell_Momentum", "sector_day",     -2,  0),
    ("Sell_Momentum", "dma_200",        -40, -2),
    ("Sell_Momentum", "dma_50",         -25, -1),
    ("Sell_Momentum", "rsi_month",      15,  50),
    ("Sell_Momentum", "rsi_weekly",     10,  55),
    ("Sell_Momentum", "month_return",   -25, -2),
    ("Sell_Momentum", "week_return",    -8,  -1),
    ("Sell_Momentum", "range_1d",       -3,  -0.5),
]


def seed_default_filters(conn):
    """Insert default filter thresholds. Idempotent — only inserts missing rows."""
    with conn.cursor() as cur:
        for sig, metric, mn, mx in DEFAULT_FILTERS:
            cur.execute("""
                INSERT INTO v5_filters (signal_type, metric, min_val, max_val)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (signal_type, metric) DO NOTHING
            """, (sig, metric, mn, mx))
        conn.commit()
    log.info(f"V5 filters seeded ({len(DEFAULT_FILTERS)} rules)")


# ============================================================
# METRIC COMPUTATION
# ============================================================

def _wilder_rsi(closes: pd.Series, period: int = 14) -> Optional[float]:
    """Standard Wilder RSI on a price series. Returns latest value."""
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
    """
    Compute all V5 metrics for one symbol on a given date.
    Pulls from raw_prices (daily) + intraday_prices (5min) + gvm_scores + sector_ratings.
    """
    target_date = target_date or date.today()
    out = {
        "symbol": symbol,
        "score_date": target_date,
        # Core metrics (original 13)
        "gvm_score": None, "dma_50": None, "dma_200": None,
        "rsi_month": None, "rsi_weekly": None,
        "month_return": None, "week_return": None, "year_return": None,
        "sector_day": None, "sector_week": None,
        "month_index": None, "week_index_52": None, "range_1d": None,
        # New metrics (6 added)
        "dma_20": None, "range_3d": None, "prev_day_change": None,
        "daily_rsi": None, "upper_bb": None, "lower_bb": None,
    }

    # 1. GVM + segment
    with conn.cursor() as cur:
        cur.execute("SELECT gvm_score, segment FROM gvm_scores WHERE symbol = %s", (symbol,))
        row = cur.fetchone()
        if row:
            out["gvm_score"] = float(row[0]) if row[0] is not None else None
            segment = row[1]
        else:
            segment = None

    # 2. Daily prices (need ~1 year for year_return, 200 days for DMA200, etc.)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT price_date, close, high, low FROM raw_prices
            WHERE symbol = %s AND price_date <= %s
            ORDER BY price_date DESC LIMIT 400
        """, (symbol, target_date))
        rows = cur.fetchall()
    if not rows:
        return out

    df = pd.DataFrame(rows, columns=["date", "close", "high", "low"])
    df["close"] = pd.to_numeric(df["close"])
    df["high"] = pd.to_numeric(df["high"])
    df["low"] = pd.to_numeric(df["low"])
    df = df.sort_values("date").reset_index(drop=True)

    if len(df) < 5:
        return out

    latest_close = float(df["close"].iloc[-1])

    # 3. DMA 50 + 200 (% above/below)
    if len(df) >= 50:
        dma50 = df["close"].tail(50).mean()
        out["dma_50"] = _safe_pct(latest_close, dma50)
    if len(df) >= 200:
        dma200 = df["close"].tail(200).mean()
        out["dma_200"] = _safe_pct(latest_close, dma200)

    # 4. DMA 20 (new)
    if len(df) >= 20:
        dma20 = df["close"].tail(20).mean()
        out["dma_20"] = _safe_pct(latest_close, dma20)

    # 5. Returns (Year / Month / Week)
    if len(df) >= 252:
        out["year_return"] = _safe_pct(latest_close, float(df["close"].iloc[-252]))
    if len(df) >= 21:
        out["month_return"] = _safe_pct(latest_close, float(df["close"].iloc[-21]))
    if len(df) >= 5:
        out["week_return"] = _safe_pct(latest_close, float(df["close"].iloc[-5]))

    # 6. Previous Day Change (new) — day-2 to day-1
    if len(df) >= 2:
        prev1 = float(df["close"].iloc[-1])
        prev2 = float(df["close"].iloc[-2])
        if prev2 > 0:
            out["prev_day_change"] = (prev1 / prev2 - 1) * 100

    # 7. RSI — Monthly (period=6) + Weekly (period=8) + Daily (period=14)
    df_indexed = df.set_index(pd.to_datetime(df["date"]))
    monthly_closes = df_indexed["close"].resample("M").last().dropna()
    weekly_closes  = df_indexed["close"].resample("W").last().dropna()
    out["rsi_month"]  = _wilder_rsi(monthly_closes, period=RSI_MONTH_PERIOD)
    out["rsi_weekly"] = _wilder_rsi(weekly_closes,  period=RSI_WEEK_PERIOD)
    out["daily_rsi"]  = _wilder_rsi(df["close"],    period=RSI_DAILY_PERIOD)  # new

    # 8. 52-week index = (price - 52w low) / (52w high - 52w low) * 100
    if len(df) >= 252:
        recent_252 = df.tail(252)
        w52_high = float(recent_252["high"].max())
        w52_low  = float(recent_252["low"].min())
        if w52_high > w52_low:
            out["week_index_52"] = (latest_close - w52_low) / (w52_high - w52_low) * 100

    # 9. Month index = same formula on last 21 days
    if len(df) >= 21:
        recent_21 = df.tail(21)
        m_high = float(recent_21["high"].max())
        m_low  = float(recent_21["low"].min())
        if m_high > m_low:
            out["month_index"] = (latest_close - m_low) / (m_high - m_low) * 100

    # 10. 3-Day Range (new) — signed by direction
    if len(df) >= 4:
        recent_3d = df.tail(3)
        h3 = float(recent_3d["high"].max())
        l3 = float(recent_3d["low"].min())
        base = float(df["close"].iloc[-4])
        if base > 0:
            raw_range = (h3 - l3) / base * 100
            out["range_3d"] = raw_range if latest_close >= base else -raw_range

    # 11. Bollinger Bands (new) — 20-period, 2 std
    # upper_bb / lower_bb = % deviation of price from band (negative = price below band)
    if len(df) >= 20:
        last_20 = df["close"].tail(20)
        bb_ma  = float(last_20.mean())
        bb_std = float(last_20.std())
        bb_upper = bb_ma + 2 * bb_std
        bb_lower = bb_ma - 2 * bb_std
        if latest_close > 0:
            out["upper_bb"] = (latest_close - bb_upper) / latest_close * 100
            out["lower_bb"] = (latest_close - bb_lower) / latest_close * 100

    # 12. Sector returns (Day + Week)
    if segment:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT AVG(
                    CASE WHEN rp_today.close > 0 AND rp_5d.close > 0
                    THEN (rp_today.close / rp_5d.close - 1) * 100 END
                ) as sector_week_avg,
                AVG(
                    CASE WHEN rp_today.close > 0 AND rp_1d.close > 0
                    THEN (rp_today.close / rp_1d.close - 1) * 100 END
                ) as sector_day_avg
                FROM gvm_scores g
                JOIN LATERAL (
                    SELECT close FROM raw_prices WHERE symbol = g.symbol
                    ORDER BY price_date DESC LIMIT 1
                ) rp_today ON true
                JOIN LATERAL (
                    SELECT close FROM raw_prices WHERE symbol = g.symbol
                    ORDER BY price_date DESC OFFSET 5 LIMIT 1
                ) rp_5d ON true
                JOIN LATERAL (
                    SELECT close FROM raw_prices WHERE symbol = g.symbol
                    ORDER BY price_date DESC OFFSET 1 LIMIT 1
                ) rp_1d ON true
                WHERE g.segment = %s
            """, (segment,))
            srow = cur.fetchone()
            if srow:
                out["sector_week"] = float(srow[0]) if srow[0] is not None else None
                out["sector_day"]  = float(srow[1]) if srow[1] is not None else None

    # 13. 1D Range — from intraday_prices (signed: positive = up day, negative = down day)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT MAX(high), MIN(low),
                   (SELECT open FROM intraday_prices WHERE symbol = %s ORDER BY ts ASC LIMIT 1) as day_open
            FROM intraday_prices
            WHERE symbol = %s AND ts::date = (
                SELECT MAX(ts::date) FROM intraday_prices WHERE symbol = %s
            )
        """, (symbol, symbol, symbol))
        irow = cur.fetchone()
        if irow and irow[0] and irow[2]:
            hi, lo, op = float(irow[0]), float(irow[1]), float(irow[2])
            if op > 0:
                signed = ((hi - lo) / op * 100) * (1 if latest_close >= op else -1)
                out["range_1d"] = signed

    return out


def store_metrics(conn, metrics: Dict):
    """Upsert one row into v5_metrics."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO v5_metrics
            (symbol, score_date, gvm_score, dma_50, dma_200, dma_20,
             rsi_month, rsi_weekly, daily_rsi,
             month_return, week_return, year_return, prev_day_change,
             sector_day, sector_week,
             month_index, week_index_52,
             range_1d, range_3d, upper_bb, lower_bb)
            VALUES
            (%(symbol)s, %(score_date)s, %(gvm_score)s, %(dma_50)s, %(dma_200)s, %(dma_20)s,
             %(rsi_month)s, %(rsi_weekly)s, %(daily_rsi)s,
             %(month_return)s, %(week_return)s, %(year_return)s, %(prev_day_change)s,
             %(sector_day)s, %(sector_week)s,
             %(month_index)s, %(week_index_52)s,
             %(range_1d)s, %(range_3d)s, %(upper_bb)s, %(lower_bb)s)
            ON CONFLICT (symbol, score_date) DO UPDATE SET
                gvm_score = EXCLUDED.gvm_score,
                dma_50 = EXCLUDED.dma_50, dma_200 = EXCLUDED.dma_200, dma_20 = EXCLUDED.dma_20,
                rsi_month = EXCLUDED.rsi_month, rsi_weekly = EXCLUDED.rsi_weekly, daily_rsi = EXCLUDED.daily_rsi,
                month_return = EXCLUDED.month_return, week_return = EXCLUDED.week_return,
                year_return = EXCLUDED.year_return, prev_day_change = EXCLUDED.prev_day_change,
                sector_day = EXCLUDED.sector_day, sector_week = EXCLUDED.sector_week,
                month_index = EXCLUDED.month_index, week_index_52 = EXCLUDED.week_index_52,
                range_1d = EXCLUDED.range_1d, range_3d = EXCLUDED.range_3d,
                upper_bb = EXCLUDED.upper_bb, lower_bb = EXCLUDED.lower_bb
        """, metrics)
        conn.commit()


# ============================================================
# AND-GATE EVALUATOR
# ============================================================

def load_filters(conn) -> Dict[str, Dict[str, Tuple[Optional[float], Optional[float]]]]:
    """Load filter config from v5_filters table. Returns {signal_type: {metric: (min, max)}}."""
    filters = {}
    with conn.cursor() as cur:
        cur.execute("SELECT signal_type, metric, min_val, max_val FROM v5_filters")
        for sig, metric, mn, mx in cur.fetchall():
            filters.setdefault(sig, {})[metric] = (
                float(mn) if mn is not None else None,
                float(mx) if mx is not None else None,
            )
    return filters


def evaluate_stock(metrics: Dict, signal_filters: Dict[str, Tuple]) -> bool:
    """Check if a stock's metrics pass ALL filter conditions for a signal type (AND-gate)."""
    for metric, (mn, mx) in signal_filters.items():
        val = metrics.get(metric)
        if val is None:
            return False
        if mn is not None and val < mn:
            return False
        if mx is not None and val > mx:
            return False
    return True


def store_qualified(conn, symbol: str, signal_type: str, score_date: date,
                    metrics: Dict, cmp: Optional[float] = None):
    """Insert/update qualified stock."""
    with conn.cursor() as cur:
        import json as _json
        clean_metrics = {k: float(v) if isinstance(v, (int, float)) and v is not None else None
                         for k, v in metrics.items()
                         if k not in ("symbol", "score_date")}
        cur.execute("""
            INSERT INTO v5_qualified (symbol, signal_type, score_date, gvm_score, cmp, metrics)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (symbol, signal_type, score_date) DO UPDATE SET
                gvm_score = EXCLUDED.gvm_score, cmp = EXCLUDED.cmp, metrics = EXCLUDED.metrics
        """, (symbol, signal_type, score_date, metrics.get("gvm_score"), cmp,
              _json.dumps(clean_metrics)))
        conn.commit()


def run_v5_engine(conn, symbols: List[str] = None, target_date: date = None) -> Dict:
    """
    Main entry point. Compute metrics + run all 4 AND-gates for a universe.
    Default universe = distinct symbols in v5_signals (290 futures stocks).
    """
    target_date = target_date or date.today()

    if symbols is None:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM v5_signals ORDER BY symbol")
            symbols = [r[0] for r in cur.fetchall()]

    filters = load_filters(conn)
    if not filters:
        seed_default_filters(conn)
        filters = load_filters(conn)

    with conn.cursor() as cur:
        cur.execute("DELETE FROM v5_qualified WHERE score_date = %s", (target_date,))
        conn.commit()

    results = {"date": str(target_date), "symbols_processed": 0,
               "Buy_Reversal": 0, "Buy_Momentum": 0,
               "Sell_Reversal": 0, "Sell_Momentum": 0,
               "errors": []}

    cmp_map = {}
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, cmp FROM cmp_prices")
        cmp_map = {r[0]: float(r[1]) for r in cur.fetchall() if r[1] is not None}

    for sym in symbols:
        try:
            metrics = compute_metrics_for_symbol(conn, sym, target_date)
            store_metrics(conn, metrics)
            results["symbols_processed"] += 1

            for signal_type, sig_filters in filters.items():
                if evaluate_stock(metrics, sig_filters):
                    store_qualified(conn, sym, signal_type, target_date,
                                    metrics, cmp_map.get(sym))
                    results[signal_type] = results.get(signal_type, 0) + 1
        except Exception as e:
            results["errors"].append(f"{sym}: {str(e)[:100]}")
            log.warning(f"V5 engine error on {sym}: {e}")

    log.info(f"V5 engine done: {results}")
    return results
