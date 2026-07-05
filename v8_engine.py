"""
V8 Signal Engine -- Scorr
=========================
Computes ~23 metrics per stock from raw_prices + gvm_scores,
then writes pre-filtered signals to DB (compute-on-write architecture).

Universe = futures_universe (is_active = TRUE)

Tables written:
  v8_metrics        -- EOD metrics per symbol per day
  v8_qualified      -- stocks passing each basket's filters TODAY (live read source)
  v8_signal_history -- append-only archive of every signal ever generated (backtest source)
  v8_funnel_counts  -- waterfall step counts per basket per day (Sheet funnel display)

Filter thresholds are CANONICAL in v8_endpoints.py (FILTER_CONFIG dict).
This engine computes raw metrics + writes signals; v8_endpoints serves pure reads.

RSI periods: Month=6, Weekly=8, Daily=14 (Wilder).

mom_2d formula (renamed from day_change 10-Jun-2026):
  EOD:  (latest_close / close_2_days_ago - 1) * 100
  Live: (cmp / close_2_days_ago - 1) * 100
  2-day momentum. close_2_days_ago = raw_prices iloc[-3] (today not yet in EOD).
  NOTE: This is intentionally a 2-candle gap (T vs T-2), NOT a 1-day change.
        Renamed from 'day_change' to 'mom_2d' to remove naming confusion.

day_1d / eod_chg (added 11-Jun-2026 -- DISPLAY ONLY, never filters):
  day_1d:  owned by v8_signal_writer. Live CMP vs yesterday's close = true intraday day change.
           EOD engine sets this to None (cannot compute today's return from raw_prices).
           Fix 18-Jun-2026: EOD must not overwrite signal_writer's live value.
  eod_chg: frozen yesterday's 1D change. Computed by EOD engine (latest_close/prior_close).
  Both stored in v8_metrics only. NOT in FILTER_CONFIG, NOT in v8_qualified.

store_metrics ON CONFLICT uses COALESCE for day_1d, mom_2d, sector_week, sector_month:
  COALESCE(v8_metrics.col, EXCLUDED.col) = prefer existing (signal_writer) over EOD.
  All four are owned by signal_writer (live, every 5-min). EOD cannot overwrite them.

SEGMENT_OVERRIDES (11-Jun-2026): symbols without a gvm_scores row get
  NIFTY50/BANKNIFTY -> 'Index', *BEES -> 'ETF'. Own bucket = no sector-average pollution.

sector_week  = live avg week_return  of peers -- computed by v8_signal_writer every 5-min
sector_month = live avg month_return of peers -- computed by v8_signal_writer every 5-min
sector_day   = live avg mom_2d of peers      -- computed by v8_signal_writer every 5-min
"""

import logging
import json
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List
import pandas as pd
import numpy as np

log = logging.getLogger("scorr.v8")

RSI_MONTH_PERIOD = 6
RSI_WEEK_PERIOD  = 8
RSI_DAILY_PERIOD = 14

# Segment overrides for instruments without gvm_scores rows
INDEX_SYMBOLS = {"NIFTY50", "BANKNIFTY"}

def _segment_override(symbol: str, segment: Optional[str]) -> Optional[str]:
    """Indices -> 'Index', ETFs (*BEES) -> 'ETF' when no gvm segment exists."""
    if segment:
        return segment
    if symbol in INDEX_SYMBOLS:
        return "Index"
    if symbol.endswith("BEES"):
        return "ETF"
    return segment

# ============================================================
# SCHEMA -- V8-native (compute-on-write)
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
    mom_2d NUMERIC,
    day_1d NUMERIC, eod_chg NUMERIC,
    sector_day NUMERIC, sector_week NUMERIC, sector_month NUMERIC,
    month_index NUMERIC, week_index_52 NUMERIC,
    range_1d NUMERIC, range_3d NUMERIC,
    upper_bb NUMERIC, lower_bb NUMERIC,
    ma9_vs_ma21 NUMERIC, vol_ratio NUMERIC,
    computed_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(symbol, score_date)
);
ALTER TABLE v8_metrics ADD COLUMN IF NOT EXISTS sector_month NUMERIC;
ALTER TABLE v8_metrics ADD COLUMN IF NOT EXISTS mom_2d NUMERIC;
ALTER TABLE v8_metrics ADD COLUMN IF NOT EXISTS day_1d NUMERIC;
ALTER TABLE v8_metrics ADD COLUMN IF NOT EXISTS eod_chg NUMERIC;
CREATE INDEX IF NOT EXISTS idx_v8_metrics_symbol_date ON v8_metrics(symbol, score_date DESC);

-- v8_qualified: live signals for today -- overwritten on every engine run.
-- API endpoints read this directly (pure SELECT). Never compute on read.
CREATE TABLE IF NOT EXISTS v8_qualified (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    basket TEXT NOT NULL,
    signal_date DATE NOT NULL,
    signal_ts TIMESTAMP DEFAULT NOW(),
    gvm_score NUMERIC,
    cmp NUMERIC,
    mom_2d NUMERIC,
    week_return NUMERIC,
    month_return NUMERIC,
    dma_200 NUMERIC,
    dma_50 NUMERIC,
    rsi_month NUMERIC,
    rsi_weekly NUMERIC,
    sector_week NUMERIC,
    sector_day NUMERIC,
    month_index NUMERIC,
    week_index_52 NUMERIC,
    daily_rsi NUMERIC,
    range_3d NUMERIC,
    metrics JSONB,
    source TEXT DEFAULT 'eod',
    UNIQUE(symbol, basket, signal_date)
);
ALTER TABLE v8_qualified ADD COLUMN IF NOT EXISTS mom_2d NUMERIC;
CREATE INDEX IF NOT EXISTS idx_v8_qual_date_basket ON v8_qualified(signal_date DESC, basket);
CREATE INDEX IF NOT EXISTS idx_v8_qual_basket_today ON v8_qualified(basket, signal_date DESC);

-- v8_signal_history: append-only archive. Every signal ever generated.
CREATE TABLE IF NOT EXISTS v8_signal_history (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    basket TEXT NOT NULL,
    signal_date DATE NOT NULL,
    gvm_score NUMERIC,
    cmp NUMERIC,
    mom_2d NUMERIC,
    week_return NUMERIC,
    month_return NUMERIC,
    dma_200 NUMERIC,
    dma_50 NUMERIC,
    rsi_month NUMERIC,
    rsi_weekly NUMERIC,
    metrics JSONB,
    source TEXT DEFAULT 'eod',
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(symbol, basket, signal_date)
);
ALTER TABLE v8_signal_history ADD COLUMN IF NOT EXISTS mom_2d NUMERIC;
CREATE INDEX IF NOT EXISTS idx_v8_history_basket_date ON v8_signal_history(basket, signal_date DESC);
CREATE INDEX IF NOT EXISTS idx_v8_history_date ON v8_signal_history(signal_date DESC);

-- v8_funnel_counts: waterfall step counts per basket per day.
CREATE TABLE IF NOT EXISTS v8_funnel_counts (
    id SERIAL PRIMARY KEY,
    basket TEXT NOT NULL,
    score_date DATE NOT NULL,
    counts JSONB NOT NULL,
    computed_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(basket, score_date)
);
CREATE INDEX IF NOT EXISTS idx_v8_funnel_date ON v8_funnel_counts(score_date DESC);
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
        "sector_day": None, "sector_week": None, "sector_month": None,
        "month_index": None, "week_index_52": None, "range_1d": None,
        "dma_20": None, "range_3d": None, "mom_2d": None,
        "day_1d": None, "eod_chg": None,
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
    out["_segment"] = _segment_override(symbol, segment)   # used by sector pass in run_v8_engine

    with conn.cursor() as cur:
        cur.execute("""
            SELECT price_date, close, high, low, volume, open FROM raw_prices
            WHERE symbol = %s AND price_date <= %s
            ORDER BY price_date DESC LIMIT 400
        """, (symbol, target_date))
        rows = cur.fetchall()
    if not rows:
        return out

    df = pd.DataFrame(rows, columns=["date", "close", "high", "low", "volume", "open"])
    df["close"]  = pd.to_numeric(df["close"])
    df["high"]   = pd.to_numeric(df["high"])
    df["low"]    = pd.to_numeric(df["low"])
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["open"]   = pd.to_numeric(df["open"], errors="coerce")
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

    # mom_2d: 2-day momentum (latest close vs close 2 days ago -- iloc[-3])
    if len(df) >= 3:
        base = float(df["close"].iloc[-3])
        if base > 0:
            out["mom_2d"] = (latest_close / base - 1) * 100

    # day_1d / eod_chg: true 1-day change (latest close vs prior close).
    # EOD writes eod_chg only (frozen yesterday's return, correctly computable from raw_prices).
    # day_1d is owned by signal_writer (live CMP vs yesterday) -- EOD cannot compute today's return.
    # Fix 18-Jun-2026: EOD must NOT set day_1d; signal_writer's value must survive the 15:45 run.
    if len(df) >= 2:
        base1 = float(df["close"].iloc[-2])
        if base1 > 0:
            chg1 = (latest_close / base1 - 1) * 100
            out["day_1d"]  = None    # owned by signal_writer (live CMP/yesterday); EOD never sets
            out["eod_chg"] = chg1   # frozen: last completed day's 1D change

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
    out["rsi_month"]  = _wilder_rsi(df_indexed["close"].resample("ME").last().dropna(), RSI_MONTH_PERIOD)
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

    # cc#172: range_1d was NEVER computed on the EOD path (only initialized None),
    # so the 15:45 upsert nulled the live writer's value for all 212 symbols every
    # day. Same formula as the live writer: (high-low)/open * 100, signed by
    # close>=open, from the latest raw_prices day.
    last = df.iloc[-1]
    if pd.notna(last["open"]) and float(last["open"]) > 0 \
       and pd.notna(last["high"]) and pd.notna(last["low"]):
        op = float(last["open"])
        raw = (float(last["high"]) - float(last["low"])) / op * 100
        out["range_1d"] = raw if latest_close >= op else -raw

    if len(df) >= 20:
        last20 = df["close"].tail(20)
        ma, sd = float(last20.mean()), float(last20.std())
        if latest_close > 0:
            out["upper_bb"] = (latest_close - (ma + 2*sd)) / latest_close * 100
            out["lower_bb"] = (latest_close - (ma - 2*sd)) / latest_close * 100

    return out


def store_metrics(conn, m: Dict):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO v8_metrics
            (symbol, score_date, gvm_score, dma_50, dma_200, dma_20,
             rsi_month, rsi_weekly, daily_rsi,
             month_return, week_return, year_return, mom_2d,
             day_1d, eod_chg,
             sector_day, sector_week, sector_month,
             month_index, week_index_52,
             range_1d, range_3d, upper_bb, lower_bb,
             ma9_vs_ma21, vol_ratio)
            VALUES
            (%(symbol)s, %(score_date)s, %(gvm_score)s, %(dma_50)s, %(dma_200)s, %(dma_20)s,
             %(rsi_month)s, %(rsi_weekly)s, %(daily_rsi)s,
             %(month_return)s, %(week_return)s, %(year_return)s, %(mom_2d)s,
             %(day_1d)s, %(eod_chg)s,
             %(sector_day)s, %(sector_week)s, %(sector_month)s,
             %(month_index)s, %(week_index_52)s,
             %(range_1d)s, %(range_3d)s, %(upper_bb)s, %(lower_bb)s,
             %(ma9_vs_ma21)s, %(vol_ratio)s)
            ON CONFLICT (symbol, score_date) DO UPDATE SET
                gvm_score=EXCLUDED.gvm_score, dma_50=EXCLUDED.dma_50, dma_200=EXCLUDED.dma_200, dma_20=EXCLUDED.dma_20,
                rsi_month=EXCLUDED.rsi_month, rsi_weekly=EXCLUDED.rsi_weekly, daily_rsi=EXCLUDED.daily_rsi,
                month_return=EXCLUDED.month_return, week_return=EXCLUDED.week_return,
                year_return=EXCLUDED.year_return,
                mom_2d=COALESCE(v8_metrics.mom_2d, EXCLUDED.mom_2d),
                day_1d=COALESCE(v8_metrics.day_1d, EXCLUDED.day_1d), eod_chg=EXCLUDED.eod_chg,
                sector_day=EXCLUDED.sector_day,
                sector_week=COALESCE(v8_metrics.sector_week, EXCLUDED.sector_week),
                sector_month=COALESCE(v8_metrics.sector_month, EXCLUDED.sector_month),
                month_index=EXCLUDED.month_index, week_index_52=EXCLUDED.week_index_52,
                range_1d=COALESCE(EXCLUDED.range_1d, v8_metrics.range_1d), range_3d=EXCLUDED.range_3d,
                upper_bb=EXCLUDED.upper_bb, lower_bb=EXCLUDED.lower_bb,
                ma9_vs_ma21=EXCLUDED.ma9_vs_ma21, vol_ratio=EXCLUDED.vol_ratio
        """, m)
        conn.commit()


# ============================================================
# SIGNAL WRITE -- compute-on-write core
# ============================================================

def write_signals_to_db(conn, all_metrics: List[Dict], target_date: date, source: str = 'eod'):
    from v8_endpoints import FILTER_CONFIG

    # cc#171 fix 2: EOD qual-writes are gated to trading days. The 15:45 scheduler
    # trigger has no weekday guard, so this ran on Sat 27-Jun + Sun 28-Jun and wrote
    # weekend qual rows. Gating here (not in the scheduler) also covers manual
    # MCP/endpoint triggers of run_v8_engine on non-trading days.
    from nse_holidays import is_trading_day
    if not is_trading_day(target_date):
        log.warning(f"write_signals: {target_date} is not a trading day -- skipping qual writes")
        return

    cmp_map = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, cmp FROM cmp_prices")
            for row in cur.fetchall():
                cmp_map[row[0]] = float(row[1]) if row[1] else None
    except Exception as e:
        log.warning(f"write_signals: cmp fetch failed: {e}")

    for basket, filters in FILTER_CONFIG.items():
        if basket == 'sell_overbought':
            continue

        universe = all_metrics[:]
        funnel = {}
        for metric, bounds in filters.items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            universe = [s for s in universe if _passes(s.get(metric), mn, mx)]
            funnel[metric] = len(universe)

        qualified_symbols = universe

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO v8_funnel_counts (basket, score_date, counts)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (basket, score_date) DO UPDATE SET
                        counts=EXCLUDED.counts, computed_at=NOW()
                """, (basket, target_date, json.dumps(funnel)))
            conn.commit()
        except Exception as e:
            log.warning(f"write_signals funnel {basket}: {e}")

        # cc#171 fix 1: the DELETE-and-rewrite here erased the intraday audit trail --
        # live-writer qual rows (with their real qualification signal_ts) were wiped at
        # 15:45 whenever the EOD pass didn't re-qualify them (03-Jul: INDHOTEL+DIVISLAB
        # entered paper positions, zero qual trail left). Per spec 1403 the EOD run is a
        # BACKSTOP: it now only ADDS quals the live writer missed (DO NOTHING below),
        # never deletes or overwrites a live row.

        for s in qualified_symbols:
            sym = s['symbol']
            cmp = cmp_map.get(sym)
            metrics_snap = {k: s.get(k) for k in [
                'gvm_score','dma_50','dma_200','dma_20','rsi_month','rsi_weekly',
                'daily_rsi','month_return','week_return','year_return','mom_2d',
                'week_index_52','range_3d','ma9_vs_ma21','vol_ratio',
                'sector_week','sector_month',
            ]}
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO v8_qualified
                        (symbol, basket, signal_date, signal_ts, gvm_score, cmp,
                         mom_2d, week_return, month_return, dma_200, dma_50,
                         rsi_month, rsi_weekly, sector_week, sector_day, month_index,
                         week_index_52, daily_rsi, range_3d, metrics, source)
                        VALUES (%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (symbol, basket, signal_date) DO NOTHING
                    """, (
                        sym, basket, target_date, s.get('gvm_score'), cmp,
                        s.get('mom_2d'), s.get('week_return'), s.get('month_return'),
                        s.get('dma_200'), s.get('dma_50'), s.get('rsi_month'), s.get('rsi_weekly'),
                        s.get('sector_week'), s.get('sector_day'), s.get('month_index'),
                        s.get('week_index_52'), s.get('daily_rsi'), s.get('range_3d'),
                        json.dumps(metrics_snap), source
                    ))
                conn.commit()
            except Exception as e:
                log.warning(f"write_signals qualified {basket} {sym}: {e}")

            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO v8_signal_history
                        (symbol, basket, signal_date, gvm_score, cmp,
                         mom_2d, week_return, month_return,
                         dma_200, dma_50, rsi_month, rsi_weekly, metrics, source)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (symbol, basket, signal_date) DO NOTHING
                    """, (
                        sym, basket, target_date, s.get('gvm_score'), cmp,
                        s.get('mom_2d'), s.get('week_return'), s.get('month_return'),
                        s.get('dma_200'), s.get('dma_50'), s.get('rsi_month'), s.get('rsi_weekly'),
                        json.dumps(metrics_snap), source
                    ))
                conn.commit()
            except Exception as e:
                log.warning(f"write_signals history {basket} {sym}: {e}")

    # sell_overbought funnel (cc#98): SO is skipped in the loop above because its
    # qualification uses pivot-based OR logic (5d high vs 0.9*R1/R2) that the simple
    # FILTER_CONFIG funnel cannot express. Reuse the canonical, locked 5-stage helper
    # in v8_endpoints (SO signal logic unchanged -- counting only) and write the same
    # {metric: survivors} shape the other baskets store, so the dashboard SO tab renders.
    # Gated to same-day EOD: the helper reads current-day metrics/pivots, so writing it
    # under a historical replay date would be inaccurate -- skip in that case.
    if target_date == date.today():
        try:
            from v8_endpoints import _so_funnel_stages
            so_stages, _so_total = _so_funnel_stages()
            so_counts = {st["metric"]: st["survivors"] for st in so_stages}
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO v8_funnel_counts (basket, score_date, counts)
                    VALUES ('sell_overbought', %s, %s)
                    ON CONFLICT (basket, score_date) DO UPDATE SET
                        counts=EXCLUDED.counts, computed_at=NOW()
                """, (target_date, json.dumps(so_counts)))
            conn.commit()
            log.info(f"write_signals: sell_overbought funnel written {so_counts}")
        except Exception as e:
            log.warning(f"write_signals SO funnel: {e}")
    else:
        log.info(f"write_signals: skip SO funnel (target_date {target_date} != today)")

    log.info(f"write_signals done: date={target_date} source={source}")


def _passes(value, mn, mx) -> bool:
    if value is None:
        return False
    v = float(value)
    if mn is not None and v < mn:
        return False
    if mx is not None and v > mx:
        return False
    return True


# ============================================================
# ENGINE ENTRY POINT
# ============================================================

def run_v8_engine(conn, symbols: List[str] = None, target_date: date = None) -> Dict:
    target_date = target_date or date.today()
    # cc#211: gate the EOD METRICS store too. cc#171 only gated the QUAL writes inside
    # write_signals(), leaving store_metrics free to write v8_metrics rows on a non-trading
    # day (the 15:45 trigger + MCP/endpoint run_v8_engine have no weekday guard). Same
    # canonical is_trading_day (weekday + NSE holidays) as the live writer.
    from nse_holidays import is_trading_day
    if not is_trading_day(target_date):
        log.warning(f"run_v8_engine: {target_date} is not a trading day -- skipping (no v8_metrics/qual writes)")
        return {"date": str(target_date), "skipped": "nontrading_day",
                "universe": "futures_universe", "symbols_processed": 0,
                "signals_written": 0, "errors": []}
    if symbols is None:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol FROM futures_universe WHERE is_active = TRUE ORDER BY symbol")
            symbols = [r[0] for r in cur.fetchall()]

    results = {
        "date": str(target_date),
        "universe": "futures_universe",
        "symbols_processed": 0,
        "errors": [],
        "signals_written": 0,
    }

    # Pass 1: compute per-symbol metrics
    all_metrics = []
    for sym in symbols:
        try:
            m = compute_metrics_for_symbol(conn, sym, target_date)
            all_metrics.append(m)
            results["symbols_processed"] += 1
        except Exception as e:
            results["errors"].append(f"{sym}: {str(e)[:80]}")
            log.warning(f"V8 engine error on {sym}: {e}")

    # Pass 2: sector_week + sector_month -- EOD peer avg by segment
    # sector_week  = avg week_return  of peers in same segment
    # sector_month = avg month_return of peers in same segment
    # NOTE: These are also computed live by v8_signal_writer._update_sector_aggregates_sql.
    # The EOD values here serve as initial baseline; COALESCE in store_metrics ensures
    # signal_writer's live values take priority once set.
    from collections import defaultdict
    seg_week:  dict = defaultdict(list)
    seg_month: dict = defaultdict(list)
    for m in all_metrics:
        seg = m.get("_segment")
        if not seg:
            continue
        wk = m.get("week_return")
        mo = m.get("month_return")
        if wk  is not None: seg_week[seg].append(wk)
        if mo  is not None: seg_month[seg].append(mo)

    seg_week_avg  = {seg: float(np.mean(v)) for seg, v in seg_week.items()  if v}
    seg_month_avg = {seg: float(np.mean(v)) for seg, v in seg_month.items() if v}

    for m in all_metrics:
        seg = m.get("_segment")
        m["sector_week"]  = seg_week_avg.get(seg)
        m["sector_month"] = seg_month_avg.get(seg)
        m["sector_day"]   = None   # set live by v8_signal_writer every 5-min

    # Pass 3: store all metrics (with sector values)
    for m in all_metrics:
        try:
            store_metrics(conn, m)
        except Exception as e:
            results["errors"].append(f"{m.get('symbol','?')}: store {str(e)[:80]}")
            log.warning(f"store_metrics error {m.get('symbol')}: {e}")

    # Pass 4: write signals
    try:
        write_signals_to_db(conn, all_metrics, target_date, source='eod')
        results["signals_written"] = len(all_metrics)
    except Exception as e:
        log.error(f"write_signals_to_db failed: {e}")
        results["errors"].append(f"write_signals: {str(e)[:120]}")

    log.info(f"V8 engine done: {results['symbols_processed']} symbols, signals written to DB")
    return results
