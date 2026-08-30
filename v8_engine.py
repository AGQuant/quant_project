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
    ma9_vs_ma21 NUMERIC, vol_ratio NUMERIC,
    -- cc#855: IST is canonical (see v8_signal_writer TIMEZONE CANON note). A bare NOW()
    -- default casts to the SESSION timezone (UTC on Railway) and reintroduces the mismatch.
    computed_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'Asia/Kolkata'),
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
    -- cc#855: IST is canonical (see v8_signal_writer TIMEZONE CANON note). A bare NOW()
    -- default casts to the SESSION timezone (UTC on Railway) and reintroduces the mismatch.
    computed_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'Asia/Kolkata'),
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
        "month_index": None, "week_index_52": None,
        "dma_20": None, "mom_2d": None,
        "day_1d": None, "eod_chg": None,
        "daily_rsi": None,
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

    # cc#232: 4 dead range/BB metrics removed (0 readers, gated nothing, display-only).
    # daily_rsi + ma9_vs_ma21 KEPT (active external readers — trade-check, GVM, paper).

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
             ma9_vs_ma21, vol_ratio,
             -- cc#1194 scope 5: THE INSERT BRANCH STAMPS IST TOO. cc#855 (1a12ef8) set
             -- computed_at explicitly in the DO UPDATE SET below and stopped there, so a row
             -- that did not already exist fell through to the LIVE column default, which is a
             -- bare now() = UTC on Railway. Same statement, two conventions, decided by whether
             -- the row happened to be there already.
             computed_at)
            VALUES
            (%(symbol)s, %(score_date)s, %(gvm_score)s, %(dma_50)s, %(dma_200)s, %(dma_20)s,
             %(rsi_month)s, %(rsi_weekly)s, %(daily_rsi)s,
             %(month_return)s, %(week_return)s, %(year_return)s, %(mom_2d)s,
             %(day_1d)s, %(eod_chg)s,
             %(sector_day)s, %(sector_week)s, %(sector_month)s,
             %(month_index)s, %(week_index_52)s,
             %(ma9_vs_ma21)s, %(vol_ratio)s,
             NOW() AT TIME ZONE 'Asia/Kolkata')
            ON CONFLICT (symbol, score_date) DO UPDATE SET
                gvm_score=EXCLUDED.gvm_score, dma_50=EXCLUDED.dma_50, dma_200=EXCLUDED.dma_200, dma_20=EXCLUDED.dma_20,
                rsi_month=EXCLUDED.rsi_month, rsi_weekly=EXCLUDED.rsi_weekly, daily_rsi=EXCLUDED.daily_rsi,
                month_return=EXCLUDED.month_return, week_return=EXCLUDED.week_return,
                year_return=EXCLUDED.year_return,
                mom_2d=COALESCE(v8_metrics.mom_2d, EXCLUDED.mom_2d),
                day_1d=COALESCE(v8_metrics.day_1d, EXCLUDED.day_1d), eod_chg=EXCLUDED.eod_chg,
                -- cc#1461: sector_day gets the SAME guard its two siblings below already have.
                -- This EOD pass computes sector_day=None (it is a live-only metric, written by
                -- v8_signal_writer every 5-min per cc#1102's theme grouping) and runs AFTER the
                -- writer's last tick — the bare EXCLUDED overwrite here wiped the live value back
                -- to null every single trading day.
                sector_day=COALESCE(v8_metrics.sector_day, EXCLUDED.sector_day),
                sector_week=COALESCE(v8_metrics.sector_week, EXCLUDED.sector_week),
                sector_month=COALESCE(v8_metrics.sector_month, EXCLUDED.sector_month),
                month_index=EXCLUDED.month_index, week_index_52=EXCLUDED.week_index_52,
                ma9_vs_ma21=EXCLUDED.ma9_vs_ma21, vol_ratio=EXCLUDED.vol_ratio,
                -- cc#855: set EXPLICITLY. This upsert previously left computed_at to the column
                -- DEFAULT, which on the live table is still a bare now() (= UTC on Railway).
                -- Changing that default needs ALTER TABLE, which MAINTENANCE_LOCK_RULE id=3041
                -- confines to a weekend Railway-console window — so it is PROPOSED, not run here.
                -- Writing the value explicitly makes the code correct regardless of the default.
                computed_at=NOW() AT TIME ZONE 'Asia/Kolkata'
        """, m)
        conn.commit()


# ============================================================
# SIGNAL WRITE -- compute-on-write core
# ============================================================

# cc#238 (V8_EOD_NO_REQUALIFICATION_V1, session_log 1651 + addendum 1652): the EOD backstop
# pass must NEVER write a new v8_qualified row via re-scoring after 15:30 IST. A qualification
# is only real if the LIVE 5-min writer (source=live_5min) found it during 09:15-15:30 on that
# day's live-ticking metrics; a symbol that would ONLY qualify because of the overnight/EOD GVM
# refresh the live writer never saw is a ghost/optic signal (06-Jul FORTIS+BIOCON). Applies to
# ALL SIX baskets (incl. SO/S1B strict-AND handlers). Branch A (clean session) = zero EOD quals
# (this flag off). Branch B (any missing 5-min tick) = _heal_morning_gaps backfills the missing
# bar/metric completion data only (main.py, full-session), NEVER a v8_qualified re-score.
# The cc#171 persistence-safety guard (trading-day gate + ON CONFLICT DO NOTHING, never
# delete/overwrite a live row) below stays intact — it simply now has nothing to act on.
_EOD_REQUALIFICATION_WRITES = False


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

        # cc#1025 FUNNEL_TRUTH_V1: THE EOD FUNNEL WRITE IS GONE. This loop used to walk
        # FILTER_CONFIG, shrink the universe cumulatively, and upsert the result into
        # v8_funnel_counts at 15:45 — clobbering the correct row the LIVE 5-min writer had built
        # during the session, every single day.
        #
        # Four things were wrong with it at once:
        #   * CUMULATIVE, not independent. cc#364 fixed the funnel to count each gate against the
        #     whole universe; this loop still narrowed one gate into the next, so the numbers meant
        #     something different from the ones the dashboard was built to read.
        #   * STALE REGISTRY. It iterated FILTER_CONFIG, which has no s1_touch leg and no _universe
        #     key, so the row it wrote was missing fields the endpoint expects.
        #   * IT DIED AT day_1d. The 18-Jun rule sets day_1d=None at EOD, so _passes filtered
        #     everything out and every later stage recorded 0. The 14-Aug row is the proof:
        #     day_1d 0, gvm_score 0, and no _universe at all.
        #   * IT OVERWROTE. ON CONFLICT DO UPDATE, at 15:46, on top of the live row.
        # The net effect on screen was UNIVERSE 0 -> FINAL 0 with three empty bars, off-hours,
        # every day. The live writer in v8_signal_writer.py owns this table (one independent-count
        # funnel per handler); the EOD engine now writes NOTHING to it.
        qualified_symbols = [s for s in all_metrics
                             if all(_passes(s.get(metric),
                                            *(bounds if isinstance(bounds, list) else (bounds[0], bounds[1])))
                                    for metric, bounds in filters.items())]

        # cc#171 fix 1: the DELETE-and-rewrite here erased the intraday audit trail --
        # live-writer qual rows (with their real qualification signal_ts) were wiped at
        # 15:45 whenever the EOD pass didn't re-qualify them (03-Jul: INDHOTEL+DIVISLAB
        # entered paper positions, zero qual trail left). Per spec 1403 the EOD run is a
        # BACKSTOP: it now only ADDS quals the live writer missed (DO NOTHING below),
        # never deletes or overwrites a live row.

        # cc#238: EOD re-qualification writes are disabled (Branch A). The iterable is emptied
        # so the cc#171-guarded v8_qualified/v8_signal_history INSERTs below stay physically
        # present (persistence-safety guard preserved) but never execute — zero EOD quals.
        for s in (qualified_symbols if _EOD_REQUALIFICATION_WRITES else []):
            sym = s['symbol']
            cmp = cmp_map.get(sym)
            metrics_snap = {k: s.get(k) for k in [
                'gvm_score','dma_50','dma_200','dma_20','rsi_month','rsi_weekly',
                'daily_rsi','month_return','week_return','year_return','mom_2d',
                'week_index_52','ma9_vs_ma21','vol_ratio',
                'sector_week','sector_month',
            ]}
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO v8_qualified
                        (symbol, basket, signal_date, signal_ts, gvm_score, cmp,
                         mom_2d, week_return, month_return, dma_200, dma_50,
                         rsi_month, rsi_weekly, sector_week, sector_day, month_index,
                         week_index_52, daily_rsi, metrics, source)
                        VALUES (%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (symbol, basket, signal_date) DO NOTHING
                    """, (
                        sym, basket, target_date, s.get('gvm_score'), cmp,
                        s.get('mom_2d'), s.get('week_return'), s.get('month_return'),
                        s.get('dma_200'), s.get('dma_50'), s.get('rsi_month'), s.get('rsi_weekly'),
                        s.get('sector_week'), s.get('sector_day'), s.get('month_index'),
                        s.get('week_index_52'), s.get('daily_rsi'),
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

    # cc#1025: the sell_overbought funnel write (cc#98) is DELETED. SO was retired on 17-Jul
    # (SELL_OVERBOUGHT_KILLED_17JUL, session_log 5642) and the founder reconfirmed it on 15-Aug.
    #
    # WORTH KNOWING WHAT THIS BLOCK ACTUALLY WAS: it imported _so_funnel_stages from v8_endpoints,
    # and that helper had ALREADY been deleted in the SO retirement. So since 17-Jul the block
    # raised ImportError on every EOD run and its `except Exception` swallowed it into a log
    # warning — it wrote nothing, and v8_funnel_counts confirms it: sell_overbought's last row is
    # 2026-07-17. It was not a dead basket still being written; it was a daily failure nobody had
    # to see. Removed rather than left as a caught exception with a comment on it.
    #
    # This file now writes NOTHING to v8_funnel_counts. The live 5-min writer is the only author.

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

    # Pass 2: sector_week + sector_month -- EOD peer average by FUTURES THEME.
    #
    # cc#1102 (founder ruling 19-Aug-2026). This used to group by m["_segment"], the GVM segment.
    # It is a SECOND write path onto the same three columns as the live writer, and it runs at
    # 15:45 — so leaving it on the old taxonomy would have quietly restored GVM-segment values over
    # the theme values every evening, and the next morning's gates would have read them. That is the
    # kind of half-migration that produces a bug nobody can reproduce during market hours.
    #
    # It goes through theme_change.aggregate, the SAME function the live writer and the display
    # endpoints use, so there is one grouping rule with three data sources and never three rules.
    # The equal weighting is unchanged — this pass was already an unweighted mean over the futures
    # symbol set; only the TAXONOMY moves, from GVM segment to futures theme.
    try:
        import theme_change
        with conn.cursor() as _tc:
            theme_map = theme_change.theme_of(_tc)
        themes = theme_change.aggregate(
            theme_map,
            {m["symbol"]: {"day_1d": m.get("day_1d"), "week_return": m.get("week_return"),
                           "month_return": m.get("month_return")} for m in all_metrics})
    except Exception as e:
        log.warning(f"v8_engine sector themes failed ({e}); sector_* left None — gates fail closed")
        theme_map, themes = {}, {}

    for m in all_metrics:
        info = themes.get(theme_map.get(m["symbol"])) if theme_map.get(m["symbol"]) else None
        ok = bool(info) and not info.get("thin")
        m["sector_week"]  = info.get("week") if ok else None
        m["sector_month"] = info.get("month") if ok else None
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
