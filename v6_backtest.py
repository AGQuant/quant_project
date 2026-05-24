"""
V6 Backtest + Optimizer — Scorr / Project Quant
=================================================
Tests V6 filter thresholds on 6 months of historical data for top 50 F&O stocks.
Single-parameter sweep optimization to find best thresholds for paper trading.

Forward return window: 5 trading days
Optimization metric: composite = win_rate × avg_return

NOTE: GVM filter held constant (no historical GVM snapshots). 
      Tests the 10 technical filters: dma_50, dma_200, rsi_month, rsi_weekly,
      month_return, week_return, year_return, sector_day, sector_week,
      month_index, range_1d.
"""

import logging
import json as _json
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List, Tuple
import pandas as pd
import numpy as np

log = logging.getLogger("scorr.v6backtest")

# ============================================================
# CONSTANTS
# ============================================================

TOP_50_F_O = [
    "RELIANCE", "HDFCBANK", "BHARTIARTL", "ICICIBANK", "SBIN", "TCS",
    "BAJFINANCE", "LT", "HINDUNILVR", "LICI", "INFY", "SUNPHARMA",
    "ADANIPOWER", "ADANIPORTS", "MARUTI", "AXISBANK", "KOTAKBANK", "ITC",
    "NTPC", "ADANIENT", "M&M", "ONGC", "TITAN", "ULTRACEMCO", "HCLTECH",
    "JSWSTEEL", "BEL", "BAJAJ-AUTO", "HAL", "BAJAJFINSV", "COALINDIA",
    "NESTLEIND", "DMART", "HINDZINC", "TATASTEEL", "HINDALCO", "ADANIGREEN",
    "ETERNAL", "SHRIRAMFIN", "GRASIM", "IOC", "EICHERMOT", "DIVISLAB",
    "VBL", "BSE", "ADANIENSOL", "SOLARINDS", "TVSMOTOR", "POWERINDIA",
    "JIOFIN"
]

FORWARD_DAYS = 5
MIN_SIGNALS_FOR_VALID = 5  # need at least 5 signals to consider a filter combo

# Default V6 thresholds (replicating V5 sheet exactly, GVM swapped for Finkhoz, RSI 6/8)
V6_BASELINE_FILTERS = {
    "Buy_Reversal": {
        "year_return":  (0, None),
        "dma_200":      (0, 20),
        "dma_50":       (0, 8),
        "rsi_month":    (45, 75),
        "rsi_weekly":   (50, 75),
        "month_return": (0, 8),
        "week_return":  (0, 3),
        "sector_week":  (0, 5),
        "sector_day":   (0, 3),
        "month_index":  (50, 100),
        "range_1d":     (0.5, 2),
    },
    "Buy_Momentum": {
        "year_return":  (0, None),     # V5 sheet: Positive (not 10+)
        "dma_200":      (10, 50),      # V5: 10-50
        "dma_50":       (5, 25),       # V5: 5-25
        "rsi_month":    (55, 80),
        "rsi_weekly":   (55, 80),
        "month_return": (3, 15),
        "week_return":  (1, 7),        # V5: 1-7
        "sector_week":  (0, 5),        # V5: 0-5
        "sector_day":   (0, 3),        # V5: 0-3
        "month_index":  (50, 100),     # V5: 50-100
        "range_1d":     (1, 5),        # V5: 1-5
    },
    "Sell_Reversal": {
        "month_index":  (0, 50),
        "sector_week":  (-10, 0),
        "sector_day":   (-2, 0),
        "dma_200":      (-25, 0),
        "dma_50":       (-15, 0),
        "rsi_month":    (20, 55),
        "rsi_weekly":   (15, 50),
        "month_return": (-15, 0),
        "week_return":  (-4, 1),
        "range_1d":     (-2, 0),
    },
    "Sell_Momentum": {
        "month_index":  (0, 35),
        "sector_week":  (-8, -1),
        "sector_day":   (-2, 0),
        "dma_200":      (-40, -2),
        "dma_50":       (-25, -1),
        "rsi_month":    (15, 50),
        "rsi_weekly":   (10, 55),
        "month_return": (-25, -2),
        "week_return":  (-8, -1),
        "range_1d":     (-3, -0.5),
    },
}


# ============================================================
# SCHEMA
# ============================================================

V6_BACKTEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS v6_backtest_metrics (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    score_date DATE NOT NULL,
    dma_50 NUMERIC,
    dma_200 NUMERIC,
    rsi_month NUMERIC,
    rsi_weekly NUMERIC,
    month_return NUMERIC,
    week_return NUMERIC,
    year_return NUMERIC,
    sector_day NUMERIC,
    sector_week NUMERIC,
    month_index NUMERIC,
    range_1d NUMERIC,
    close NUMERIC,
    forward_return_5d NUMERIC,
    UNIQUE(symbol, score_date)
);
CREATE INDEX IF NOT EXISTS idx_v6bt_date ON v6_backtest_metrics(score_date);

CREATE TABLE IF NOT EXISTS v6_backtest_results (
    id SERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    variant_label TEXT NOT NULL,
    filter_config JSONB,
    num_signals INT,
    win_rate NUMERIC,
    avg_return NUMERIC,
    composite_score NUMERIC,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_v6bt_run ON v6_backtest_results(run_id, signal_type);
"""


# ============================================================
# METRIC COMPUTATION (date-aware, no GVM)
# ============================================================

def _wilder_rsi(closes: pd.Series, period: int) -> Optional[float]:
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


def _safe_pct(num, denom):
    if denom is None or denom == 0 or pd.isna(denom):
        return None
    return float((num / denom - 1) * 100)


def compute_backtest_metrics(conn, symbol: str, target_date: date,
                              all_top50_closes: Dict[str, pd.DataFrame] = None) -> Dict:
    """
    Compute 10 technical metrics for a symbol as of target_date.
    Uses ONLY data available up to and including target_date (no look-ahead).
    Sector calc uses pre-fetched all_top50_closes dict to avoid N+1 queries.
    """
    out = {
        "symbol": symbol, "score_date": target_date,
        "dma_50": None, "dma_200": None, "rsi_month": None, "rsi_weekly": None,
        "month_return": None, "week_return": None, "year_return": None,
        "sector_day": None, "sector_week": None, "month_index": None,
        "range_1d": None, "close": None, "forward_return_5d": None,
    }

    # Get this symbol's price history up to target_date
    with conn.cursor() as cur:
        cur.execute("""
            SELECT price_date, close, high, low, open FROM raw_prices
            WHERE symbol = %s AND price_date <= %s
            ORDER BY price_date DESC LIMIT 280
        """, (symbol, target_date))
        rows = cur.fetchall()
    if len(rows) < 30:
        return out

    df = pd.DataFrame(rows, columns=["date", "close", "high", "low", "open"])
    for col in ("close", "high", "low", "open"):
        df[col] = pd.to_numeric(df[col])
    df = df.sort_values("date").reset_index(drop=True)
    latest_close = float(df["close"].iloc[-1])
    out["close"] = latest_close

    # DMA
    if len(df) >= 50:
        out["dma_50"] = _safe_pct(latest_close, df["close"].tail(50).mean())
    if len(df) >= 200:
        out["dma_200"] = _safe_pct(latest_close, df["close"].tail(200).mean())

    # Returns
    if len(df) >= 252:
        out["year_return"] = _safe_pct(latest_close, float(df["close"].iloc[-252]))
    if len(df) >= 21:
        out["month_return"] = _safe_pct(latest_close, float(df["close"].iloc[-21]))
    if len(df) >= 5:
        out["week_return"] = _safe_pct(latest_close, float(df["close"].iloc[-5]))

    # RSI (Month=6, Weekly=8)
    df_idx = df.set_index(pd.to_datetime(df["date"]))
    monthly = df_idx["close"].resample("M").last().dropna()
    weekly  = df_idx["close"].resample("W").last().dropna()
    out["rsi_month"]  = _wilder_rsi(monthly, period=6)
    out["rsi_weekly"] = _wilder_rsi(weekly,  period=8)

    # Month index (range position over last 21 days)
    if len(df) >= 21:
        recent = df.tail(21)
        m_hi = float(recent["high"].max())
        m_lo = float(recent["low"].min())
        if m_hi > m_lo:
            out["month_index"] = (latest_close - m_lo) / (m_hi - m_lo) * 100

    # 1D Range from daily candle (proxy when intraday unavailable)
    today_row = df.iloc[-1]
    o = float(today_row["open"])
    h = float(today_row["high"])
    l = float(today_row["low"])
    c = float(today_row["close"])
    if o > 0:
        signed = ((h - l) / o * 100) * (1 if c >= o else -1)
        out["range_1d"] = signed

    # Sector returns from pre-fetched top50 dict
    if all_top50_closes:
        sec_day_rets, sec_week_rets = [], []
        for other_sym, other_df in all_top50_closes.items():
            if other_sym == symbol:
                continue
            # Filter to target_date
            sub = other_df[other_df["date"] <= target_date]
            if len(sub) < 6:
                continue
            cl_now = float(sub["close"].iloc[-1])
            cl_1d  = float(sub["close"].iloc[-2]) if len(sub) >= 2 else None
            cl_5d  = float(sub["close"].iloc[-6]) if len(sub) >= 6 else None
            if cl_1d:
                sec_day_rets.append((cl_now / cl_1d - 1) * 100)
            if cl_5d:
                sec_week_rets.append((cl_now / cl_5d - 1) * 100)
        if sec_day_rets:
            out["sector_day"] = float(np.mean(sec_day_rets))
        if sec_week_rets:
            out["sector_week"] = float(np.mean(sec_week_rets))

    # Forward 5-day return (used later, requires future data)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT close FROM raw_prices
            WHERE symbol = %s AND price_date > %s
            ORDER BY price_date ASC LIMIT %s
        """, (symbol, target_date, FORWARD_DAYS))
        fut_rows = cur.fetchall()
    if len(fut_rows) >= FORWARD_DAYS:
        fwd_close = float(fut_rows[-1][0])
        out["forward_return_5d"] = (fwd_close / latest_close - 1) * 100

    return out


def fetch_top50_close_history(conn, target_date: date) -> Dict[str, pd.DataFrame]:
    """Pre-fetch all top 50 close histories up to target_date for sector calc."""
    out = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, price_date, close FROM raw_prices
            WHERE symbol = ANY(%s) AND price_date <= %s
            ORDER BY symbol, price_date ASC
        """, (TOP_50_F_O, target_date))
        rows = cur.fetchall()
    for sym, dt, cl in rows:
        if sym not in out:
            out[sym] = {"date": [], "close": []}
        out[sym]["date"].append(dt)
        out[sym]["close"].append(float(cl) if cl else None)
    return {s: pd.DataFrame(d) for s, d in out.items()}


def store_backtest_metric(conn, m: Dict):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO v6_backtest_metrics
            (symbol, score_date, dma_50, dma_200, rsi_month, rsi_weekly,
             month_return, week_return, year_return, sector_day, sector_week,
             month_index, range_1d, close, forward_return_5d)
            VALUES (%(symbol)s, %(score_date)s, %(dma_50)s, %(dma_200)s,
                    %(rsi_month)s, %(rsi_weekly)s, %(month_return)s, %(week_return)s,
                    %(year_return)s, %(sector_day)s, %(sector_week)s, %(month_index)s,
                    %(range_1d)s, %(close)s, %(forward_return_5d)s)
            ON CONFLICT (symbol, score_date) DO UPDATE SET
                dma_50 = EXCLUDED.dma_50, dma_200 = EXCLUDED.dma_200,
                rsi_month = EXCLUDED.rsi_month, rsi_weekly = EXCLUDED.rsi_weekly,
                month_return = EXCLUDED.month_return, week_return = EXCLUDED.week_return,
                year_return = EXCLUDED.year_return,
                sector_day = EXCLUDED.sector_day, sector_week = EXCLUDED.sector_week,
                month_index = EXCLUDED.month_index, range_1d = EXCLUDED.range_1d,
                close = EXCLUDED.close, forward_return_5d = EXCLUDED.forward_return_5d
        """, m)
        conn.commit()


def backfill_backtest_metrics(conn, start_date: date, end_date: date) -> Dict:
    """Compute metrics for top 50 stocks across date range. Stores in v6_backtest_metrics."""
    log.info(f"Backtest backfill: {start_date} → {end_date}")
    # Get all trading dates in range from raw_prices
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT price_date FROM raw_prices
            WHERE symbol = 'RELIANCE' AND price_date >= %s AND price_date <= %s
            ORDER BY price_date
        """, (start_date, end_date))
        dates = [r[0] for r in cur.fetchall()]

    total = 0
    for d in dates:
        # Pre-fetch top50 close history once per date
        top50_hist = fetch_top50_close_history(conn, d)
        for sym in TOP_50_F_O:
            m = compute_backtest_metrics(conn, sym, d, all_top50_closes=top50_hist)
            if m["close"] is not None:
                store_backtest_metric(conn, m)
                total += 1
    log.info(f"Backfill done: {total} metric rows across {len(dates)} dates")
    return {"dates": len(dates), "metric_rows": total}


# ============================================================
# EVALUATOR + OPTIMIZER
# ============================================================

def evaluate_filter_set(conn, signal_type: str, filters: Dict[str, Tuple],
                         start_date: date, end_date: date) -> Dict:
    """
    Apply filter set to stored backtest metrics. 
    Return win_rate, avg_return, composite_score for forward 5d returns.
    For Sell signals, profit = price drop, so invert returns.
    """
    # Build SQL WHERE clause from filters
    where_clauses = ["score_date >= %s", "score_date <= %s", "forward_return_5d IS NOT NULL"]
    params = [start_date, end_date]
    for metric, (mn, mx) in filters.items():
        if mn is not None:
            where_clauses.append(f"{metric} >= %s")
            params.append(mn)
            where_clauses.append(f"{metric} IS NOT NULL")
        if mx is not None:
            where_clauses.append(f"{metric} <= %s")
            params.append(mx)
            where_clauses.append(f"{metric} IS NOT NULL")

    where_sql = " AND ".join(where_clauses)
    q = f"SELECT forward_return_5d FROM v6_backtest_metrics WHERE {where_sql}"
    with conn.cursor() as cur:
        cur.execute(q, params)
        rets = [float(r[0]) for r in cur.fetchall()]

    if len(rets) < MIN_SIGNALS_FOR_VALID:
        return {"num_signals": len(rets), "win_rate": None, "avg_return": None, "composite_score": None}

    arr = np.array(rets)
    # For Sell signals, invert: profit = price drops
    if signal_type.startswith("Sell"):
        arr = -arr

    win_rate = float((arr > 0).sum() / len(arr))
    avg_return = float(arr.mean())
    composite = win_rate * avg_return

    return {
        "num_signals": len(rets),
        "win_rate": round(win_rate, 4),
        "avg_return": round(avg_return, 4),
        "composite_score": round(composite, 4),
    }


def sweep_single_metric(conn, signal_type: str, base_filters: Dict,
                         metric: str, start_date: date, end_date: date) -> List[Dict]:
    """
    Try ±10%, ±20%, ±30% variations on a single metric's min and max.
    Return results for each variation.
    """
    results = []
    current_mn, current_mx = base_filters.get(metric, (None, None))

    # Generate variations
    variations = []
    if current_mn is not None:
        for pct in (-0.3, -0.2, -0.1, 0, 0.1, 0.2, 0.3):
            new_mn = current_mn * (1 + pct) if current_mn != 0 else current_mn + pct * 5
            variations.append((round(new_mn, 2), current_mx, f"min={round(new_mn,2)}"))
    if current_mx is not None:
        for pct in (-0.3, -0.2, -0.1, 0, 0.1, 0.2, 0.3):
            new_mx = current_mx * (1 + pct) if current_mx != 0 else current_mx + pct * 5
            variations.append((current_mn, round(new_mx, 2), f"max={round(new_mx,2)}"))

    # Always include baseline
    variations.insert(0, (current_mn, current_mx, "baseline"))

    for new_mn, new_mx, label in variations:
        trial_filters = {**base_filters, metric: (new_mn, new_mx)}
        res = evaluate_filter_set(conn, signal_type, trial_filters, start_date, end_date)
        res["metric_varied"] = metric
        res["variant_label"] = label
        res["filter_config"] = {metric: [new_mn, new_mx]}
        results.append(res)

    return results


def optimize_signal_type(conn, signal_type: str, start_date: date, end_date: date,
                         run_id: str) -> Dict:
    """
    Iteratively optimize each metric one at a time. Keep winning value, move to next.
    Returns final best filter set + per-metric history.
    """
    base = dict(V6_BASELINE_FILTERS[signal_type])
    log.info(f"Optimizing {signal_type} from baseline: {base}")

    history = {}
    for metric in list(base.keys()):
        sweep = sweep_single_metric(conn, signal_type, base, metric, start_date, end_date)
        # Pick variation with highest composite_score AND >= MIN_SIGNALS
        valid = [s for s in sweep if s.get("composite_score") is not None]
        if not valid:
            continue
        best = max(valid, key=lambda x: x["composite_score"])
        if best["variant_label"] != "baseline":
            # Update base with winning value
            new_mn, new_mx = list(best["filter_config"].values())[0]
            base[metric] = (new_mn, new_mx)
        history[metric] = {
            "best_label": best["variant_label"],
            "best_composite": best["composite_score"],
            "best_signals": best["num_signals"],
            "all_variants": sweep,
        }
        # Store result row
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO v6_backtest_results
                (run_id, signal_type, variant_label, filter_config, 
                 num_signals, win_rate, avg_return, composite_score)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s)
            """, (run_id, signal_type, f"{metric}::{best['variant_label']}",
                  _json.dumps(best["filter_config"]),
                  best["num_signals"], best["win_rate"],
                  best["avg_return"], best["composite_score"]))
            conn.commit()

    # Final baseline run
    final = evaluate_filter_set(conn, signal_type, base, start_date, end_date)
    return {
        "signal_type": signal_type,
        "optimized_filters": {k: list(v) for k, v in base.items()},
        "final_stats": final,
        "history": history,
    }


def run_full_optimization(conn) -> Dict:
    """Main entry: backfill metrics (if needed) + optimize all 4 signal types."""
    end_date = date.today() - timedelta(days=FORWARD_DAYS + 1)  # leave 5d forward buffer
    start_date = end_date - timedelta(days=180)  # 6 months
    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")

    # Check if backfill needed
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(DISTINCT score_date) FROM v6_backtest_metrics WHERE score_date >= %s", (start_date,))
        existing_dates = cur.fetchone()[0]

    if existing_dates < 100:
        log.info(f"Backfilling metrics (existing dates: {existing_dates})")
        backfill_backtest_metrics(conn, start_date, end_date)

    # Optimize each signal type
    results = {"run_id": run_id, "date_range": [str(start_date), str(end_date)],
               "signals": {}}
    for sig in ["Buy_Reversal", "Buy_Momentum", "Sell_Reversal", "Sell_Momentum"]:
        log.info(f"Optimizing {sig}...")
        results["signals"][sig] = optimize_signal_type(conn, sig, start_date, end_date, run_id)

    return results
