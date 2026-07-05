"""
V8 Signal Writer -- Single Live Engine (v2.4.0)
===============================================
Unified 5-min engine. Replaces v8_live.py + old v8_signal_writer.py.

What it does every 5-min during market hours:
  1. Loads latest EOD v8_metrics row per symbol (slow metrics: GVM, RSI M/W, sector_week, sector_month)
  2. Reads intraday_prices (today's bars) per symbol
  3. Recomputes all live-moving metrics from intraday close spliced onto EOD history
  4. Only EOD-frozen metric: gvm_score (22:00 nightly). All others now live.
  5. Upserts v8_metrics (today's row) with live values
  6. Applies score-based FILTER_CONFIG -> writes v8_qualified (latch semantics)
  7. On first qualification: auto-logs paper trade in v8_paper_positions
  8. Writes v8_funnel_counts (strict cumulative -- diagnostic only)
  9. Writes adr_intraday (live ADR every 5-min tick) -- 11-Jun-2026

Score-based qualification (15-Jun-2026, tightened 16-Jun-2026):
  BUY threshold (buy_reversal, buy_momentum -- 18-Jun-2026):
    Strong Bullish (0 fails) + Bullish (1 fail): n   (strict AND -- fewer, higher quality)
    Neutral (2 fails) + Bearish (3+ fails):       n-1 (1 miss allowed -- genuine setups rarer)
  SELL threshold (sell_reversal=5, sell_momentum=6):
    Strong Bullish (0 fails) + Bullish (1 fail): n-1 (1 miss allowed -- 18-Jun-2026)
    Neutral (2 fails) + Bearish (3+ fails):       n   (strict AND)
    Rationale: in bull markets genuine weakness is rarer; n-1 still a
    meaningful signal. In bear markets signals fire freely -- keep strict.

Dynamic buy_reversal filters (v2.2.0, 15-Jun-2026):
  Nifty 1-month return used as regime gate. 3 filters adjust dynamically.
  BULL    (Nifty 1M > +2%): week_return<=3, rsi_month<=67, sector_week<=4
  NEUTRAL (Nifty 1M  0-2%): week_return<=2, rsi_month<=62, sector_week<=3
  BEAR    (Nifty 1M  < 0%): week_return<=1, rsi_month<=58, sector_week<=2

Slot architecture (v2.3.0, 16-Jun-2026):
  Standard pool (4 baskets: buy_reversal, buy_momentum, sell_reversal, sell_momentum):
    Strong Bullish: 15B / 5S  | Bullish:  14B / 6S
    Neutral:        12B / 8S  | Bearish:   8B / 13S
  Sell Overbought dedicated ring-fenced pool (never competes with standard sell):
    Strong Bullish: 4 | Bullish: 4 | Neutral: 4 | Bearish: 3
  Total slots always = 24.

buy_s1_bounce V1 (17-Jun-2026):
  Bounce from pivot S1 support. 7 strict filters (1 gate + 6 stages):
    nifty_rsi>=55 (gate), week_return 0-3%, dma_50>0%, vol_ratio>=1.5,
    recovery_2d 2-8%, day_ret>0.5% (implies close>open), week_low<=S1.
  Fixed +1.5%/-1.5% target/stop. Dedicated ring-fenced slots: 3/3/3/2.
  Backtest (Jun25-May26, 211 futures): 88 sigs/yr, 73.9% WR, EV +0.72%.
  New metrics: recovery_2d, week_low, day_ret (live), nifty_rsi (market gate).
  NOTE: buy_s1_bounce is EXCLUDED from score-gate main loop (_write_qualified).
  It is handled exclusively by _write_buy_s1_bounce_qualified (strict AND).
  buy_s1_bounce appears in FILTER_CONFIG (v8_endpoints.py) for endpoint display
  only -- the exclusion here prevents score-gate contamination (18-Jun-2026 fix).

Sector aggregates (18-Jun-2026):
  All 3 sector metrics are now fully LIVE for the 209-symbol futures universe:
    sector_day   = avg mom_2d     (1-day peer avg, every 5-min)
    sector_week  = avg week_return (1-week peer avg, every 5-min)
    sector_month = avg month_return (1-month peer avg, every 5-min)
  Computed via _update_sector_aggregates_sql (single SQL pass) after every tick.
  EOD engine can no longer overwrite (COALESCE protection in v8_engine.store_metrics).
"""

import logging
import json
from datetime import datetime, date, time, timedelta, timezone
from typing import Dict, List, Optional
from collections import defaultdict
import pandas as pd
import numpy as np
import psycopg
import os
import nse_holidays   # cc#211: canonical trading-day guard (weekday + NSE holiday list)

log = logging.getLogger("scorr.signal_writer")

IST = timezone(timedelta(hours=5, minutes=30))


def _ops_log(conn, category: str, title: str, details: dict) -> None:
    """cc#211: lightweight ops_log writer (mirrors news_fetcher._write_ops_log). Used for
    the non-trading-day skip note and the self-defense corruption alert."""
    try:
        from psycopg.types.json import Json
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), %s, %s, %s)""",
                        (category, title, Json(details)))
        conn.commit()
    except Exception as e:
        log.error(f"_ops_log failed ({category}/{title}): {e}")


def _assert_no_nontrading_metrics(conn) -> None:
    """cc#211 self-defense: if the latest v8_metrics row is dated a non-trading day,
    something bypassed the write gate — make the silent corruption LOUD."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(score_date) FROM v8_metrics")
            row = cur.fetchone()
        latest = row[0] if row else None
        if latest and not nse_holidays.is_trading_day(latest):
            _ops_log(conn, "alert", "nontrading_metrics_row",
                     {"message": f"v8_metrics has rows dated {latest} which is NOT a trading day "
                                 f"(weekend/holiday) — write gate bypassed somewhere",
                      "score_date": str(latest)})
            log.error(f"signal_writer SELF-DEFENSE: v8_metrics latest score_date {latest} is non-trading")
    except Exception as e:
        log.error(f"_assert_no_nontrading_metrics: {e}")

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


# -- helpers ------------------------------------------------------------------

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

def _now_ist() -> datetime:
    """Current datetime in IST as a naive datetime (for DB storage)."""
    return datetime.now(IST).replace(tzinfo=None)


# -- Pivot-room gate ----------------------------------------------------------

BASKET_SIDE = {
    "buy_reversal":  "BUY",
    "buy_momentum":  "BUY",
    "sell_reversal": "SELL",
    "sell_momentum": "SELL",
}


def _load_pivots(conn) -> Dict[str, dict]:
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


# -- ADR intraday write -------------------------------------------------------

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
            now_ist = _now_ist()
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


# -- Step 1: Load EOD metrics snapshot ----------------------------------------

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


# -- Step 2: Load EOD history per symbol (bulk) --------------------------------

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
            "hi_5":         float(max(highs[-5:]))   if len(highs) >= 5   else (float(max(highs)) if highs else None),
            "lo_5":         float(min(lows[-5:]))    if len(lows)  >= 5   else (float(min(lows))  if lows  else None),
            "hi_3":         float(max(highs[-3:]))   if len(highs) >= 3   else (float(max(highs)) if highs else None),
            "close_1d_ago": closes[-1] if len(closes) >= 1 else None,
            "close_2d_ago": closes[-2] if len(closes) >= 2 else None,
            "lo_2d":        float(min(lows[-2:])) if len(lows) >= 2 else (float(lows[-1]) if lows else None),
        }
    return history


# -- Step 3: Load today's intraday bars (bulk) ---------------------------------

def _load_intraday_bars(conn, symbols: List[str]) -> Dict[str, dict]:
    """source='fyers_eq' pinned throughout (cc#140, 01-Jul-2026): intraday_prices
    carries both fyers_eq (equity) and fyers_fut (futures contract) rows per
    symbol/day. Without a source filter, MAX(volume) etc. silently pick
    whichever series is numerically larger, mixing equity-scale price/volume
    with futures-scale price/volume for the same symbol."""
    today = datetime.now(IST).date()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                symbol,
                (SELECT close FROM intraday_prices i2
                 WHERE i2.symbol = ip.symbol AND i2.ts::date = %s AND i2.source = 'fyers_eq'
                 ORDER BY ts DESC LIMIT 1)                       AS live_close,
                (SELECT open  FROM intraday_prices i3
                 WHERE i3.symbol = ip.symbol AND i3.ts::date = %s AND i3.source = 'fyers_eq'
                 ORDER BY ts ASC  LIMIT 1)                       AS day_open,
                MAX(high)   FILTER (WHERE ts::date = %s)         AS day_high,
                MIN(low)    FILTER (WHERE ts::date = %s)         AS day_low,
                MAX(volume) FILTER (WHERE ts::date = %s)         AS day_vol
            FROM intraday_prices ip
            WHERE symbol = ANY(%s) AND ts::date = %s AND source = 'fyers_eq'
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


def _round_down_5min(ts: datetime) -> time:
    """Round a naive IST datetime down to the nearest 5-min bar boundary,
    matching the signal_writer's own tick cadence."""
    minute = (ts.minute // 5) * 5
    return ts.replace(minute=minute, second=0, microsecond=0).time()


# cc#170 (VOL X v2): once-daily precomputed baseline curve. Keyed by IST date --
# rebuilt lazily on the first tick of each day (spec: "09:00 IST daily or first
# tick") instead of re-aggregating history on every 5-min tick.
# Shape: {"date": date, "curve": {sym: {time: avg_cum_vol}}, "days": {sym: n},
#         "full_day": {sym: avg_full_day_vol}}
_VOL_BASELINE: dict = {"date": None, "curve": {}, "days": {}, "full_day": {}}

_VOL_BUCKETS = [time(9, 15)]
while _VOL_BUCKETS[-1] < time(15, 25):
    _m = _VOL_BUCKETS[-1].minute + 5
    _VOL_BUCKETS.append(time(_VOL_BUCKETS[-1].hour + (_m // 60), _m % 60))

_VOL_MIN_CLEAN_DAYS = 4     # spec: <4 clean baseline days -> fallback, never fabricate
_VOL_BASELINE_DAYS  = 7     # spec: last 7 trading days
_VOL_MIN_BARS_CLEAN = 60    # a baseline day needs >=60/75 bars to count as clean


def _build_vol_baseline(conn, symbols: List[str]) -> None:
    """cc#170: build the 7-trading-day same-time cumulative-volume baseline curve
    for every symbol, once per day. For each symbol/day the best clean source is
    used (fyers_eq WS > fyers REST > yahoo) with per-day SEMANTICS AUTO-DETECT
    (cc#150 pattern): a monotonic non-decreasing volume series is a cumulative
    day counter -> cum at t = latest value <= t; otherwise volumes are per-bar
    -> cum at t = SUM(bars <= t). Never mixes the two interpretations."""
    today = datetime.now(IST).date()
    _VOL_BASELINE["date"] = today
    _VOL_BASELINE["curve"] = {}
    _VOL_BASELINE["days"] = {}
    _VOL_BASELINE["full_day"] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, source, ts::date, ts::time, volume
            FROM intraday_prices
            WHERE symbol = ANY(%s) AND ts::date < CURRENT_DATE
              AND ts::date >= CURRENT_DATE - INTERVAL '11 days'
              AND source IN ('fyers_eq', 'fyers', 'yahoo')
              AND volume IS NOT NULL
              AND ts::time BETWEEN '09:15' AND '15:30'
            ORDER BY symbol, source, ts
        """, (symbols,))
        rows = cur.fetchall()

    groups: Dict[tuple, list] = {}
    for sym, src, d, t, vol in rows:
        groups.setdefault((sym, d, src), []).append((t, float(vol)))

    SRC_PRIO = {"fyers_eq": 0, "fyers": 1, "yahoo": 2}
    best: Dict[tuple, tuple] = {}   # (sym, day) -> (prio, cum_curve list[(time, cum)])
    for (sym, d, src), bars in groups.items():
        if len(bars) < _VOL_MIN_BARS_CLEAN:
            continue
        vols = [v for _, v in bars]
        monotonic = all(b >= a for a, b in zip(vols, vols[1:]))
        cum, run = [], 0.0
        for t, v in bars:
            run = v if monotonic else run + v
            cum.append((t, run))
        if cum[-1][1] <= 0:
            continue
        prio = SRC_PRIO[src]
        cur_best = best.get((sym, d))
        if cur_best is None or prio < cur_best[0]:
            best[(sym, d)] = (prio, cum)

    per_sym_days: Dict[str, list] = {}
    for (sym, d), (_prio, cum) in best.items():
        per_sym_days.setdefault(sym, []).append((d, cum))

    for sym, days in per_sym_days.items():
        days = sorted(days, key=lambda x: x[0], reverse=True)[:_VOL_BASELINE_DAYS]
        # forward-fill each day's cumulative curve onto the canonical 5-min buckets
        sums = [0.0] * len(_VOL_BUCKETS)
        for _d, cum in days:
            i, last = 0, 0.0
            for bi, bt in enumerate(_VOL_BUCKETS):
                while i < len(cum) and cum[i][0] <= bt:
                    last = cum[i][1]; i += 1
                sums[bi] += last
        n = len(days)
        _VOL_BASELINE["curve"][sym] = {bt: sums[bi] / n for bi, bt in enumerate(_VOL_BUCKETS)}
        _VOL_BASELINE["days"][sym] = n
        _VOL_BASELINE["full_day"][sym] = sums[-1] / n
    log.info(f"vol_baseline built for {today}: {len(per_sym_days)} symbols, "
             f"{sum(1 for v in _VOL_BASELINE['days'].values() if v >= _VOL_MIN_CLEAN_DAYS)} with >={_VOL_MIN_CLEAN_DAYS} clean days")


def _load_vol_ratio_time_normalized(conn, symbols: List[str], cutoff: time) -> Dict[str, dict]:
    """VOL X v2 (cc#170, supersedes cc#140 v1.5): today's cumulative volume at
    time x vs AVG cumulative volume at the same time x over the last 7 clean
    trading days (precomputed curve, source-semantics safe -- see
    _build_vol_baseline). After close the comparison is full-day vs 7-day avg
    full-day (cutoff clamps to the last bucket), so v2 stays consistent EOD.
    <4 clean baseline days -> ratio None here; _compute_live_metrics falls back
    to the v1 formula (cum / 10d full-day avg) and flags vol_ratio_fallback."""
    if _VOL_BASELINE["date"] != datetime.now(IST).date():
        try:
            _build_vol_baseline(conn, symbols)
        except Exception as e:
            log.error(f"vol_baseline build failed: {e}")
            _VOL_BASELINE["date"] = None
    # clamp to the canonical bucket range: pre-open -> first bucket, post-close -> full day
    bucket = _VOL_BUCKETS[0]
    for bt in _VOL_BUCKETS:
        if bt <= cutoff:
            bucket = bt
        else:
            break
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, MAX(volume) AS vol_today
            FROM intraday_prices
            WHERE source = 'fyers_eq' AND symbol = ANY(%s)
              AND ts::date = CURRENT_DATE AND ts::time <= %s
            GROUP BY symbol
        """, (symbols, cutoff))
        today_map = {r[0]: _safe_float(r[1]) for r in cur.fetchall()}
    out = {}
    for sym in symbols:
        vol_today = today_map.get(sym)
        n_days = _VOL_BASELINE["days"].get(sym, 0)
        base = _VOL_BASELINE["curve"].get(sym, {}).get(bucket)
        ratio = None
        if n_days >= _VOL_MIN_CLEAN_DAYS and vol_today is not None and base and base > 0:
            ratio = round(vol_today / base, 3)
        out[sym] = {
            "vol_today": vol_today,
            "avg_vol_at_t": round(base, 0) if base else None,
            "days_available": n_days,
            "vol_ratio_time_normalized": ratio,
        }
    return out


# -- Step 4: Load CMP ---------------------------------------------------------

def _load_cmp(conn) -> Dict[str, float]:
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, cmp FROM cmp_prices")
        return {r[0]: _safe_float(r[1]) for r in cur.fetchall()}


def _load_hourly_fut(conn, symbols: List[str]) -> Dict[str, Optional[float]]:
    """cc#158: hourly momentum on the FUTURES series (spec id 1263-1267).
    (last 5m close - close 12 bars ago)/close_12_ago * 100, from
    intraday_prices source='fyers_fut' timeframe='5m', single tick at
    qualification. 12 bars * 5min = 60min = "hourly". NULL when the 12-bars-ago
    bar does not exist yet (first ~hour of the session) so the hard gate
    NULL-passes rather than blocking early signals."""
    today = datetime.now(IST).date()
    out: Dict[str, Optional[float]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH ranked AS (
                    SELECT symbol, close,
                           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
                    FROM intraday_prices
                    WHERE source = 'fyers_fut' AND timeframe = '5m'
                      AND ts::date = %s AND symbol = ANY(%s)
                )
                SELECT symbol,
                       MAX(close) FILTER (WHERE rn = 1)  AS last_close,
                       MAX(close) FILTER (WHERE rn = 13) AS close_12_ago
                FROM ranked
                WHERE rn IN (1, 13)
                GROUP BY symbol
            """, (today, symbols))
            for sym, last_close, close_12_ago in cur.fetchall():
                if last_close is not None and close_12_ago and float(close_12_ago) > 0:
                    out[sym] = (float(last_close) / float(close_12_ago) - 1) * 100
                else:
                    out[sym] = None
    except Exception as e:
        log.warning(f"_load_hourly_fut: {e} -- hourly NULL-passes this tick")
    return out


def _load_filter_state(conn) -> Dict[str, bool]:
    """cc#158: per-basket V2.1 enable state (v8_filter_state). Read live each
    tick so a kill-switch disable takes effect on the next signal pass. FAIL-SAFE:
    on any error, return {} -> every basket's hard gate treats itself as DISABLED
    (exact locked behavior), never accidentally-on."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT basket, enabled FROM v8_filter_state")
            return {b: bool(e) for b, e in cur.fetchall()}
    except Exception as e:
        log.warning(f"_load_filter_state: {e} -- V2.1 hard gates OFF (locked behavior)")
        return {}


# -- Step 5: Compute live metrics ---------------------------------------------

def _compute_live_metrics(hist: dict, bar: dict, cmp: Optional[float],
                           eod: dict, vol_tn: Optional[dict] = None) -> dict:
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
        "vol_ratio_legacy": None, "vol_ratio_time_normalized": None,
        "vol_ratio_days_available": 0, "vol_ratio_fallback": False,
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

    # vol_ratio_legacy: vol_now (any time-of-day) / 10-day FULL-DAY average (raw_prices
    # EOD). Kept for audit only (cc#140, 01-Jul-2026) -- not time-of-day matched, so it
    # mechanically rises through the day regardless of real buying intensity. Superseded
    # by vol_ratio_time_normalized below, which is now the live-filter value.
    vol_now   = bar.get("volume")
    vol_avg10 = hist.get("vol_avg10")
    if vol_now and vol_avg10 and vol_avg10 > 0:
        out["vol_ratio_legacy"] = round(vol_now / vol_avg10, 2)

    if vol_tn:
        out["vol_ratio_time_normalized"] = vol_tn.get("vol_ratio_time_normalized")
        out["vol_ratio_days_available"]  = vol_tn.get("days_available", 0)
    # cc#170 (VOL X v2): v2 time-matched ratio is THE vol_ratio. When the 7-day
    # baseline has <4 clean days for this symbol, fall back to the v1 formula
    # (cumulative / 10d full-day avg) and FLAG it -- never fabricate, never blank
    # a basket filter input just because baseline history is thin.
    if out["vol_ratio_time_normalized"] is not None:
        out["vol_ratio"] = out["vol_ratio_time_normalized"]
        out["vol_ratio_fallback"] = False
    else:
        out["vol_ratio"] = out["vol_ratio_legacy"]
        out["vol_ratio_fallback"] = out["vol_ratio_legacy"] is not None

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

    # -- New metrics for buy_s1_bounce (v2.4.0) --------------------------------
    if op and op > 0:
        out["day_ret"] = round((live - op) / op * 100, 3)

    lo_2d = hist.get("lo_2d")
    if lo_2d and lo_2d > 0:
        out["recovery_2d"] = round((live - lo_2d) / lo_2d * 100, 3)

    lo5 = hist.get("lo_5")
    today_bar_low = bar.get("low")
    if lo5 and today_bar_low:
        out["week_low"] = min(float(lo5), float(today_bar_low))
    elif lo5:
        out["week_low"] = float(lo5)
    elif today_bar_low:
        out["week_low"] = float(today_bar_low)
    else:
        out["week_low"] = None

    # cc#158: fall_from_day_high — (live - today high)/today high * 100, always
    # <= 0. today high = fyers_eq day high (bar["high"]), same source as live.
    # Sell Overbought V2.1 trigger-timing filter (spec id=1268). NULL if no
    # intraday high yet.
    day_high = bar.get("high")
    if day_high and float(day_high) > 0:
        out["fall_from_day_high"] = (live - float(day_high)) / float(day_high) * 100

    # cc#158: hourly_pct is injected in run_live_signal_writer from the fyers_fut
    # 5m loader (single tick at qualification, NULL first hour / <12 bars).
    out["hourly_pct"] = None

    out["_live"] = live
    return out


# -- Step 6: Sector aggregates (live) -----------------------------------------

def _add_sector_aggregates(computed: Dict[str, dict], eod_metrics: Dict[str, dict]):
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


# -- Step 7: Upsert v8_metrics ------------------------------------------------

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
                vol_ratio     = EXCLUDED.vol_ratio,
                computed_at   = NOW()
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


# -- Step 7b: Bulk sector aggregates SQL update (18-Jun-2026) ------------------

def _update_sector_aggregates_sql(conn, target_date) -> int:
    """
    Compute sector_day, sector_week, sector_month for all 209 futures symbols
    via a single SQL UPDATE after every signal_writer tick. All three are
    live peer-averages grouped by gvm_scores segment.
      sector_day   = avg mom_2d     (live 1-day peer avg, every 5-min)
      sector_week  = avg week_return (live 1-week peer avg, every 5-min)
      sector_month = avg month_return (live 1-month peer avg, every 5-min)
    Single SQL pass for all three. Added: 18-Jun-2026.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE v8_metrics
                SET sector_day   = seg_avgs.avg_mom_2d,
                    sector_week  = seg_avgs.avg_week_return,
                    sector_month = seg_avgs.avg_month_return
                FROM (
                    SELECT m.symbol,
                           AVG(m2.mom_2d)       AS avg_mom_2d,
                           AVG(m2.week_return)  AS avg_week_return,
                           AVG(m2.month_return) AS avg_month_return
                    FROM v8_metrics m
                    JOIN gvm_scores g  ON g.symbol  = m.symbol
                        AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
                    JOIN v8_metrics m2 ON m2.score_date = m.score_date
                    JOIN gvm_scores g2 ON g2.symbol  = m2.symbol
                        AND g2.score_date = (SELECT MAX(score_date) FROM gvm_scores)
                        AND g2.segment   = g.segment
                    WHERE m.score_date = %s
                    GROUP BY m.symbol
                ) seg_avgs
                WHERE v8_metrics.symbol     = seg_avgs.symbol
                  AND v8_metrics.score_date = %s
            """, (target_date, target_date))
            updated = cur.rowcount
        conn.commit()
        log.info(f"_update_sector_aggregates_sql: {updated} rows sector_day/week/month for {target_date}")
        return updated
    except Exception as e:
        log.warning(f"_update_sector_aggregates_sql: {e}")
        return 0


# -- Market gate --------------------------------------------------------------

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
                    # cc_task #82: never inherit a STALE prior-day ADR at the first
                    # tick (09:15-09:16). adr_intraday and the live intraday/raw compute
                    # above are both empty then, so this branch fires and yesterday's
                    # adr_daily (e.g. Bearish -> 13 sell-slots) forced the wrong mood at
                    # open (6 SHORTs entered under Bearish limits 25-Jun). Only trust
                    # adr_daily when it is TODAY's row; otherwise default 1.0 (neutral --
                    # passes adr>=1.0, adds no gate fail). Mood self-corrects as today's
                    # ticks accumulate.
                    cur.execute("SELECT adr FROM adr_daily WHERE price_date = CURRENT_DATE ORDER BY price_date DESC LIMIT 1")
                    r = cur.fetchone()
                    adr = float(r[0]) if r and r[0] is not None else 1.0

            cur.execute("""
                SELECT close FROM intraday_prices
                WHERE symbol='NIFTY50' AND ts::date=CURRENT_DATE
                ORDER BY ts DESC LIMIT 1
            """)
            lv = cur.fetchone()
            # cc_task #72 bug_3: reference closes must track ACTUAL trading-day
            # positions (T-1/T-5/T-22), NOT "most recent raw_prices row" — which lags
            # when the EOD load is late and made Nifty Day compare the live index vs a
            # 3-day-old close (showed -0.03% when the real 1-day move was +0.89%).
            # Merge intraday last-bar closes (fills the recent stale tail, incl. T-1)
            # with raw_prices for depth (intraday retains only ~12 days). Never rely on
            # raw_prices alone for the live mood gate.
            cur.execute("""
                WITH days AS (
                    SELECT DISTINCT ON (ts::date) ts::date AS d, close::numeric AS c
                    FROM intraday_prices WHERE symbol='NIFTY50' AND ts::date < CURRENT_DATE
                    ORDER BY ts::date DESC, ts DESC
                ),
                eod AS (
                    SELECT price_date AS d, close::numeric AS c
                    FROM raw_prices WHERE symbol='NIFTY50' AND price_date < CURRENT_DATE
                ),
                merged AS (
                    SELECT d, c FROM days
                    UNION
                    SELECT d, c FROM eod WHERE d NOT IN (SELECT d FROM days)
                )
                SELECT c FROM merged ORDER BY d DESC LIMIT 30
            """)
            hist = [float(x[0]) for x in cur.fetchall()]
            if lv and lv[0] is not None and len(hist) >= 22:
                latest = float(lv[0])
                nday   = (latest / hist[0]  - 1) * 100   # T-1  (yesterday's last bar)
                nweek  = (latest / hist[4]  - 1) * 100   # T-5  (5 trading days back)
                nmonth = (latest / hist[21] - 1) * 100   # T-22 (22 trading days back)
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
    Minimum filter passes to qualify.

    SELL (sell_reversal=5, sell_momentum=6  --  n_filters <= 6):
      Strong Bullish (0 fails) + Bullish (1 fail)  -> n-1  (1 miss allowed)
      Neutral (2 fails) + Bearish (3+ fails)        -> n    (strict AND)
    sell_overbought / buy_s1_bounce use dedicated handlers -- never reach here.

    BUY (buy_reversal, buy_momentum -- 18-Jun-2026):
      Strong Bullish (0 fails) + Bullish (1 fail)  -> n    (strict AND -- only best setups)
      Neutral (2 fails) + Bearish (3+ fails)        -> n-1  (1 miss allowed -- genuine setups rarer)
    """
    if side == "SELL":
        if n_filters <= 6:
            if fails <= 1:
                return n_filters - 1  # 1 miss allowed in Strong Bullish / Bullish
            return n_filters           # strict AND in Neutral / Bearish
        return max(n_filters - 2 + min(fails, 2), 1)
    # BUY (buy_reversal, buy_momentum):
    if fails <= 1:
        return n_filters      # strict AND in Strong Bullish / Bullish -- only best setups
    return n_filters - 1      # 1 miss allowed in Neutral / Bearish -- genuine setups rarer


# -- Slot architecture --------------------------------------------------------

def _mood_slots(gate_fails: int) -> tuple:
    if gate_fails == 0: return 15, 5
    if gate_fails == 1: return 14, 6
    if gate_fails == 2: return 12, 8
    return 8, 13


def _so_slots(gate_fails: int) -> int:
    return 3 if gate_fails >= 3 else 4


def _s1b_slots(gate_fails: int) -> int:
    """Buy S1 Bounce dedicated slots -- ring-fenced. 3/3/3/2."""
    return 2 if gate_fails >= 3 else 3


# -- Dynamic buy_reversal Nifty-linked filters --------------------------------

def _get_nifty_1m_return(conn) -> float:
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH ranked AS (
                    SELECT close,
                           ROW_NUMBER() OVER (ORDER BY price_date DESC) AS rn
                    FROM raw_prices
                    WHERE symbol='NIFTY50' AND price_date < CURRENT_DATE
                    LIMIT 25
                )
                SELECT
                    (SELECT close FROM ranked WHERE rn=1)  AS latest,
                    (SELECT close FROM ranked WHERE rn=22) AS month_ago
            """)
            row = cur.fetchone()
            if row and row[0] and row[1] and float(row[1]) > 0:
                return (float(row[0]) / float(row[1]) - 1) * 100
    except Exception as e:
        log.warning(f"_get_nifty_1m_return: {e}")
    return 0.0


def _get_nifty_rsi(conn) -> Optional[float]:
    """Wilder RSI(14) on NIFTY50 daily closes -- market health gate for buy_s1_bounce."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT close FROM raw_prices
                WHERE symbol='NIFTY50' AND price_date < CURRENT_DATE
                ORDER BY price_date DESC LIMIT 30
            """)
            closes = [float(r[0]) for r in cur.fetchall()]
        if len(closes) < 15:
            return None
        closes.reverse()
        return _wilder_rsi(pd.Series(closes), 14)
    except Exception as e:
        log.warning(f"_get_nifty_rsi: {e}")
        return None


def _get_dynamic_buy_reversal_overrides(nifty_1m: float) -> dict:
    if nifty_1m > 2.0:
        return {"week_return": (0.0, 3.0), "rsi_month": (52.0, 67.0),
                "sector_week": (1.0, 4.0), "_regime": "BULL"}
    elif nifty_1m >= 0.0:
        return {"week_return": (0.0, 2.0), "rsi_month": (52.0, 62.0),
                "sector_week": (1.0, 3.0), "_regime": "NEUTRAL"}
    else:
        return {"week_return": (0.0, 1.0), "rsi_month": (52.0, 58.0),
                "sector_week": (1.0, 2.0), "_regime": "BEAR"}


# -- Auto paper entry (standard baskets) --------------------------------------

_PAPER_SIDE_MAP = {"BUY": "LONG", "SELL": "SHORT"}

def _auto_paper_entry(conn, sym: str, basket: str, side: str, cmp: Optional[float],
                       pv: Optional[dict], d: date, gate_fails: int):
    if not cmp or not pv:
        return

    now_ist  = datetime.now(IST)
    mkt_open = now_ist.replace(hour=9,  minute=15, second=0, microsecond=0)
    mkt_cut  = now_ist.replace(hour=15, minute=20, second=0, microsecond=0)
    if not (mkt_open <= now_ist <= mkt_cut):
        log.debug(f"auto_paper {sym}: skipped -- outside market hours {now_ist.strftime('%H:%M')} IST")
        return

    paper_side = _PAPER_SIDE_MAP.get(side, "LONG")
    pp, r1, s1 = pv["pp"], pv["r1"], pv["s1"]

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM earnings_calendar
                WHERE UPPER(ticker)=%s
                  AND ex_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day')
                LIMIT 1
            """, (sym.upper(),))
            if cur.fetchone():
                log.debug(f"auto_paper {sym}: skipped -- blackout")
                return
    except Exception as e:
        log.warning(f"auto_paper blackout check {sym}: {e}"); return

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM v8_paper_positions WHERE symbol=%s AND side=%s AND status='OPEN'",
                        (sym, paper_side))
            if cur.fetchone():
                return
            cur.execute("SELECT 1 FROM v8_paper_trades WHERE symbol=%s AND side=%s AND entry_ts::date=%s LIMIT 1",
                        (sym, paper_side, d))
            if cur.fetchone():
                return
            cur.execute("SELECT 1 FROM v8_paper_positions WHERE symbol=%s AND side=%s AND entry_ts::date=%s LIMIT 1",
                        (sym, paper_side, d))
            if cur.fetchone():
                return
    except Exception as e:
        log.warning(f"auto_paper guard check {sym}: {e}"); return

    try:
        buy_slots, sell_slots = _mood_slots(gate_fails)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT side, COUNT(*) FROM v8_paper_positions
                WHERE status='OPEN' AND basket != 'sell_overbought'
                GROUP BY side
            """)
            counts = {r[0]: int(r[1]) for r in cur.fetchall()}
        long_open  = counts.get("LONG",  0)
        short_open = counts.get("SHORT", 0)
        if paper_side == "LONG"  and long_open  >= buy_slots:
            log.info(f"auto_paper {sym}: slot_full LONG ({long_open}/{buy_slots})"); return
        if paper_side == "SHORT" and short_open >= sell_slots:
            log.info(f"auto_paper {sym}: slot_full SHORT ({short_open}/{sell_slots})"); return
    except Exception as e:
        log.warning(f"auto_paper slot check {sym}: {e}"); return

    entry = round(cmp, 2)
    if paper_side == "LONG":
        target = round(r1, 2)
        stop   = round(entry - (r1 - entry), 2)
    else:
        target = round(s1, 2)
        stop   = round(entry + (entry - s1), 2)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT lot_size FROM futures_universe WHERE symbol=%s", (sym,))
            r = cur.fetchone()
            qty = int(r[0]) if r and r[0] else 1
    except Exception:
        qty = 1

    entry_ts_ist = _now_ist()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO v8_paper_positions
                (symbol, side, basket, entry_price, entry_ts, qty, target, stop_loss, pp, pivot_date, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN')
                ON CONFLICT (symbol, side, status) DO NOTHING
            """, (sym, paper_side, basket, entry, entry_ts_ist, qty, target, stop, pp, d))
            inserted = cur.rowcount
        conn.commit()
        if inserted:
            log.info(f"auto_paper entry: {sym} {paper_side} @ {entry} "
                     f"entry_ts={entry_ts_ist.strftime('%H:%M')} IST "
                     f"target={target} sl={stop} basket={basket}")
    except Exception as e:
        log.warning(f"auto_paper insert {sym} {paper_side}: {e}")


# -- Auto paper entry (sell_overbought) ----------------------------------------

def _auto_paper_entry_so(conn, sym: str, cmp: Optional[float],
                          pv: Optional[dict], d: date, gate_fails: int):
    if not cmp or not pv:
        return

    now_ist  = datetime.now(IST)
    mkt_open = now_ist.replace(hour=9,  minute=15, second=0, microsecond=0)
    mkt_cut  = now_ist.replace(hour=15, minute=20, second=0, microsecond=0)
    if not (mkt_open <= now_ist <= mkt_cut):
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM earnings_calendar
                WHERE UPPER(ticker)=%s
                  AND ex_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day')
                LIMIT 1
            """, (sym.upper(),))
            if cur.fetchone():
                return
    except Exception as e:
        log.warning(f"auto_paper_so blackout {sym}: {e}"); return

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM v8_paper_positions WHERE symbol=%s AND side='SHORT' AND status='OPEN'",
                        (sym,))
            if cur.fetchone():
                return
            cur.execute("SELECT 1 FROM v8_paper_positions WHERE symbol=%s AND side='SHORT' AND basket='sell_overbought' AND entry_ts::date=%s LIMIT 1",
                        (sym, d))
            if cur.fetchone():
                return
    except Exception as e:
        log.warning(f"auto_paper_so guard {sym}: {e}"); return

    so_cap = _so_slots(gate_fails)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM v8_paper_positions
                WHERE status='OPEN' AND basket='sell_overbought' AND side='SHORT'
            """)
            so_open = int(cur.fetchone()[0])
        if so_open >= so_cap:
            log.info(f"auto_paper_so {sym}: SO slot_full ({so_open}/{so_cap})")
            return
    except Exception as e:
        log.warning(f"auto_paper_so slot check {sym}: {e}"); return

    pp, r1, s1 = pv["pp"], pv["r1"], pv["s1"]
    r2 = r1 + (r1 - pp)
    target = round(s1, 2)
    stop   = round(r2, 2)
    entry  = round(cmp, 2)

    if target >= entry:
        log.debug(f"auto_paper_so {sym}: S1 ({target}) >= entry ({entry}), skip")
        return

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT lot_size FROM futures_universe WHERE symbol=%s", (sym,))
            r = cur.fetchone()
            qty = int(r[0]) if r and r[0] else 1
    except Exception:
        qty = 1

    entry_ts_ist = _now_ist()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO v8_paper_positions
                (symbol, side, basket, entry_price, entry_ts, qty, target, stop_loss, pp, pivot_date, status)
                VALUES (%s, 'SHORT', 'sell_overbought', %s, %s, %s, %s, %s, %s, %s, 'OPEN')
                ON CONFLICT (symbol, side, status) DO NOTHING
            """, (sym, entry, entry_ts_ist, qty, target, stop, pp, d))
            inserted = cur.rowcount
        conn.commit()
        if inserted:
            log.info(f"auto_paper_so entry: {sym} SHORT @ {entry} "
                     f"entry_ts={entry_ts_ist.strftime('%H:%M')} IST "
                     f"target(S1)={target} sl(R2)={stop} so_slots={so_open+1}/{so_cap}")
    except Exception as e:
        log.warning(f"auto_paper_so insert {sym}: {e}")


# -- Auto paper entry (buy_s1_bounce) -----------------------------------------

def _auto_paper_entry_s1b(conn, sym: str, cmp: Optional[float], d: date, gate_fails: int):
    """Fixed +1.5% target / -1.5% stop. Dedicated ring-fenced slots 3/3/3/2."""
    if not cmp:
        return

    now_ist  = datetime.now(IST)
    mkt_open = now_ist.replace(hour=9,  minute=15, second=0, microsecond=0)
    mkt_cut  = now_ist.replace(hour=15, minute=20, second=0, microsecond=0)
    if not (mkt_open <= now_ist <= mkt_cut):
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM earnings_calendar
                WHERE UPPER(ticker)=%s
                  AND ex_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day')
                LIMIT 1
            """, (sym.upper(),))
            if cur.fetchone():
                return
    except Exception as e:
        log.warning(f"auto_paper_s1b blackout {sym}: {e}"); return

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM v8_paper_positions WHERE symbol=%s AND side='LONG' AND status='OPEN'", (sym,))
            if cur.fetchone():
                return
            cur.execute("""SELECT 1 FROM v8_paper_positions
                WHERE symbol=%s AND side='LONG' AND basket='buy_s1_bounce'
                AND entry_ts::date=%s LIMIT 1""", (sym, d))
            if cur.fetchone():
                return
    except Exception as e:
        log.warning(f"auto_paper_s1b guard {sym}: {e}"); return

    s1b_cap = _s1b_slots(gate_fails)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM v8_paper_positions
                WHERE status='OPEN' AND basket='buy_s1_bounce' AND side='LONG'
            """)
            s1b_open = int(cur.fetchone()[0])
        if s1b_open >= s1b_cap:
            log.info(f"auto_paper_s1b {sym}: slot_full ({s1b_open}/{s1b_cap})")
            return
    except Exception as e:
        log.warning(f"auto_paper_s1b slot check {sym}: {e}"); return

    entry  = round(cmp, 2)
    target = round(entry * 1.015, 2)
    stop   = round(entry * 0.985, 2)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT lot_size FROM futures_universe WHERE symbol=%s", (sym,))
            r = cur.fetchone()
            qty = int(r[0]) if r and r[0] else 1
    except Exception:
        qty = 1

    entry_ts_ist = _now_ist()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO v8_paper_positions
                (symbol, side, basket, entry_price, entry_ts, qty,
                 target, stop_loss, pp, pivot_date, status)
                VALUES (%s,'LONG','buy_s1_bounce',%s,%s,%s,%s,%s,%s,%s,'OPEN')
                ON CONFLICT (symbol, side, status) DO NOTHING
            """, (sym, entry, entry_ts_ist, qty, target, stop, entry, d))
            inserted = cur.rowcount
        conn.commit()
        if inserted:
            log.info(f"auto_paper_s1b: {sym} LONG @ {entry} "
                     f"tgt={target}(+1.5%) sl={stop}(-1.5%) "
                     f"slots={s1b_open+1}/{s1b_cap} ts={entry_ts_ist.strftime('%H:%M')} IST")
    except Exception as e:
        log.warning(f"auto_paper_s1b insert {sym}: {e}")


# -- Step 8: Write v8_qualified + funnel --------------------------------------

def _write_qualified(conn, all_metrics: List[dict], target_date: date):
    from v8_endpoints import FILTER_CONFIG, v21_hard_gate_pass

    gate_fails = _market_gate_fails(conn)
    pivots     = _load_pivots(conn)
    enabled_v21 = _load_filter_state(conn)   # cc#158: per-basket V2.1 enable state

    nifty_1m   = _get_nifty_1m_return(conn)
    dyn_br     = _get_dynamic_buy_reversal_overrides(nifty_1m)
    log.info(f"Nifty 1M={nifty_1m:+.2f}% -> buy_reversal regime={dyn_br['_regime']}")

    signal_ts_ist = _now_ist()

    for basket, filters in FILTER_CONFIG.items():
        if basket in ("sell_overbought", "buy_s1_bounce"):
            continue

        if basket == "buy_reversal":
            active_filters = dict(filters)
            active_filters["week_return"] = list(dyn_br["week_return"])
            active_filters["rsi_month"]   = list(dyn_br["rsi_month"])
            active_filters["sector_week"] = list(dyn_br["sector_week"])
        else:
            active_filters = filters

        # cc#158: sell_momentum V2.1 w52 is a MODIFY of a locked score-gate
        # filter (<=20 -> <=30), not a hard-gate add. Swap the bound in-place
        # only when enabled; n_filters (and thus the gate threshold) is
        # unchanged. Old <=20 path stays until enable (spec id=1267).
        if basket == "sell_momentum" and enabled_v21.get("sell_momentum"):
            active_filters = dict(active_filters)
            active_filters["week_index_52"] = [None, 30.0]

        n_filters = len(active_filters)
        side      = BASKET_SIDE.get(basket, "BUY")
        need      = _gate_threshold(gate_fails, n_filters, side)

        universe = []
        for s in all_metrics:
            score = sum(
                1 for metric, bounds in active_filters.items()
                if _passes(s.get(metric),
                           *(bounds if isinstance(bounds, list) else (bounds[0], bounds[1])))
            )
            s["_filter_score"] = score
            if score >= need:
                universe.append(s)

        funnel    = {}
        survivors = all_metrics[:]
        for metric, bounds in active_filters.items():
            mn, mx    = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            survivors = [s for s in survivors if _passes(s.get(metric), mn, mx)]
            funnel[metric] = len(survivors)
        funnel["_score_threshold"] = need
        funnel["_score_qualified"] = len(universe)
        if basket == "buy_reversal":
            funnel["_regime"] = dyn_br["_regime"]
            funnel["_nifty_1m"] = round(nifty_1m, 2)

        log.info(f"{basket}: score-gate need={need}/{n_filters} "
                 f"gate_fails={gate_fails} -> {len(universe)} score-qualified")

        # cc#158: V2.1 hard-gate layer — applied AFTER the score-gate, never
        # counted into the threshold. Disabled basket -> no-op (locked behavior).
        v21_on = enabled_v21.get(basket, False)
        if v21_on:
            before = len(universe)
            universe = [s for s in universe if v21_hard_gate_pass(basket, s, True)]
            log.info(f"{basket}: V2.1 hard-gate ON -> {len(universe)}/{before} pass")

        universe = [
            s for s in universe
            if (pv := pivots.get(s["symbol"])) and _pivot_room_ok(
                side, s.get("_cmp"), pv["pp"], pv["r1"], pv["s1"]
            )
        ]
        log.info(f"{basket}: pivot-room gate ({side}) -> {len(universe)} with room")

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

        for s in universe:
            sym  = s["symbol"]
            snap = {k: s.get(k) for k in [
                "gvm_score", "dma_50", "dma_200", "dma_20",
                "rsi_month", "rsi_weekly", "daily_rsi",
                "month_return", "week_return", "year_return", "mom_2d",
                "week_index_52", "range_3d", "ma9_vs_ma21", "vol_ratio",
                "sector_week", "sector_month", "sector_day",
            ]}
            snap["vol_ratio_legacy"]          = s.get("vol_ratio_legacy")
            snap["vol_ratio_time_normalized"] = s.get("vol_ratio_time_normalized")
            snap["vol_ratio_days_available"]  = s.get("vol_ratio_days_available")
            snap["vol_ratio_fallback"]        = s.get("vol_ratio_fallback")     # cc#170
            snap["filter_score"] = s.get("_filter_score")
            snap["filter_total"] = n_filters
            snap["hourly_pct"]          = s.get("hourly_pct")           # cc#158
            snap["fall_from_day_high"]  = s.get("fall_from_day_high")   # cc#158
            snap["v21_enabled"]         = v21_on                        # cc#158
            if basket == "buy_reversal":
                snap["regime"] = dyn_br["_regime"]
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO v8_qualified
                        (symbol, basket, signal_date, signal_ts, gvm_score, cmp,
                         mom_2d, week_return, month_return, dma_200, dma_50,
                         rsi_month, rsi_weekly, sector_week, sector_day,
                         month_index, week_index_52, daily_rsi, range_3d,
                         metrics, source)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (symbol, basket, signal_date) DO NOTHING
                    """, (
                        sym, basket, target_date,
                        signal_ts_ist,
                        s.get("gvm_score"), s.get("_cmp"),
                        s.get("mom_2d"), s.get("week_return"), s.get("month_return"),
                        s.get("dma_200"), s.get("dma_50"),
                        s.get("rsi_month"), s.get("rsi_weekly"),
                        s.get("sector_week"), s.get("sector_day"),
                        s.get("month_index"), s.get("week_index_52"),
                        s.get("daily_rsi"), s.get("range_3d"),
                        json.dumps(snap), "live_5min",
                    ))
                    first_qualification = cur.rowcount > 0
                conn.commit()
                if first_qualification:
                    _auto_paper_entry(conn, sym, basket, side,
                                      s.get("_cmp"), pivots.get(sym),
                                      target_date, gate_fails)
            except Exception as e:
                log.warning(f"qualified insert {basket} {sym}: {e}")

    _write_buy_s1_bounce_qualified(conn, all_metrics, target_date,
                                    gate_fails, pivots, signal_ts_ist, enabled_v21)
    _write_sell_overbought_qualified(conn, all_metrics, target_date,
                                      gate_fails, pivots, signal_ts_ist, enabled_v21)


def _write_buy_s1_bounce_qualified(conn, all_metrics: List[dict], target_date: date,
                                    gate_fails: int, pivots: dict, signal_ts_ist,
                                    enabled_v21: Optional[dict] = None):
    """
    Buy S1 Bounce V1 (17-Jun-2026). 7 strict filters (1 gate + 6 stages).
    Dedicated ring-fenced slots 3/3/3/2. Backtest: 88 sigs/yr, 73.9% WR.
    cc#158: V2.1 hard gate (hourly_pct >0..1.0, week_index_52 50..90) layered
    as extra strict-AND conditions when enabled (spec id=1265).
    """
    from v8_endpoints import v21_hard_gate_pass
    enabled_v21 = enabled_v21 or {}
    s1b_on = enabled_v21.get("buy_s1_bounce", False)
    nifty_rsi = _get_nifty_rsi(conn)
    if nifty_rsi is None or nifty_rsi < 55.0:
        log.debug(f"buy_s1_bounce: Nifty RSI={nifty_rsi} < 55 -- gated OFF")
        return

    s1b_cap = _s1b_slots(gate_fails)
    log.info(f"buy_s1_bounce: Nifty RSI={nifty_rsi:.1f} gated ON -- slots={s1b_cap}")

    candidates = [
        s for s in all_metrics
        if _passes(s.get("week_return"),  0.0, 3.0)
        and _passes(s.get("dma_50"),      0.0, None)
        and _passes(s.get("vol_ratio"),   1.5, None)
        and _passes(s.get("recovery_2d"), 2.0, 8.0)
        and _passes(s.get("day_ret"),     0.5, None)
    ]
    log.info(f"buy_s1_bounce: {len(candidates)} after metric pre-filter")
    if not candidates:
        return

    qualified = []
    for s in candidates:
        sym = s["symbol"]
        pv  = pivots.get(sym)
        if not pv:
            continue
        week_low = s.get("week_low")
        if week_low is None or float(week_low) > float(pv["s1"]):
            continue
        if not s.get("_cmp"):
            continue
        qualified.append(s)

    log.info(f"buy_s1_bounce: {len(qualified)} qualified after week_low<=S1")

    # cc#158: V2.1 hard-gate layer (strict-AND) — hourly_pct + week_index_52.
    if s1b_on:
        before = len(qualified)
        qualified = [s for s in qualified if v21_hard_gate_pass("buy_s1_bounce", s, True)]
        log.info(f"buy_s1_bounce: V2.1 hard-gate ON -> {len(qualified)}/{before} pass")

    for s in qualified:
        sym  = s["symbol"]
        snap = {
            "week_return": s.get("week_return"), "dma_50": s.get("dma_50"),
            "vol_ratio": s.get("vol_ratio"), "recovery_2d": s.get("recovery_2d"),
            "day_ret": s.get("day_ret"), "week_low": s.get("week_low"),
            "nifty_rsi": round(nifty_rsi, 1), "filter_score": 7, "filter_total": 7,
            "vol_ratio_legacy": s.get("vol_ratio_legacy"),
            "vol_ratio_time_normalized": s.get("vol_ratio_time_normalized"),
            "vol_ratio_days_available": s.get("vol_ratio_days_available"),
            "vol_ratio_fallback": s.get("vol_ratio_fallback"),   # cc#170
            "hourly_pct": s.get("hourly_pct"),            # cc#158
            "week_index_52": s.get("week_index_52"),      # cc#158
            "v21_enabled": s1b_on,                        # cc#158
        }
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO v8_qualified
                    (symbol, basket, signal_date, signal_ts, gvm_score, cmp,
                     mom_2d, week_return, month_return, dma_200, dma_50,
                     rsi_month, rsi_weekly, sector_week, sector_day,
                     month_index, week_index_52, daily_rsi, range_3d,
                     metrics, source)
                    VALUES
                    (%s,'buy_s1_bounce',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, basket, signal_date) DO NOTHING
                """, (
                    sym, target_date, signal_ts_ist,
                    s.get("gvm_score"), s.get("_cmp"),
                    s.get("mom_2d"), s.get("week_return"), s.get("month_return"),
                    s.get("dma_200"), s.get("dma_50"),
                    s.get("rsi_month"), s.get("rsi_weekly"),
                    s.get("sector_week"), s.get("sector_day"),
                    s.get("month_index"), s.get("week_index_52"),
                    s.get("daily_rsi"), s.get("range_3d"),
                    json.dumps(snap), "live_5min",
                ))
                first_qual = cur.rowcount > 0
            conn.commit()
            if first_qual:
                _auto_paper_entry_s1b(conn, sym, s.get("_cmp"), target_date, gate_fails)
        except Exception as e:
            log.warning(f"buy_s1_bounce insert {sym}: {e}")


def _write_sell_overbought_qualified(conn, all_metrics: List[dict], target_date: date,
                                      gate_fails: int, pivots: dict, signal_ts_ist,
                                      enabled_v21: Optional[dict] = None):
    from v8_endpoints import v21_hard_gate_pass
    enabled_v21 = enabled_v21 or {}
    so_v21_on = enabled_v21.get("sell_overbought", False)
    so_cap = _so_slots(gate_fails)
    log.info(f"sell_overbought: SO slots={so_cap} gate_fails={gate_fails}")

    candidates = [
        s for s in all_metrics
        if (s.get("rsi_weekly") or 0) >= 80
        and (s.get("rsi_month") or 0) >= 70
        and (s.get("sector_week") or 0) < 0
    ]
    log.info(f"sell_overbought: {len(candidates)} candidates after RSI/sector pre-filter")

    if not candidates:
        return

    syms = [s["symbol"] for s in candidates]
    today = target_date

    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH pivots AS (
                    SELECT symbol, price_date,
                        AVG((high+low+close)/3.0) OVER w AS pp,
                        MAX(high) OVER w AS h5,
                        MIN(low)  OVER w AS l5
                    FROM raw_prices
                    WHERE symbol = ANY(%s)
                      AND price_date >= %s - INTERVAL '14 days'
                    WINDOW w AS (PARTITION BY symbol ORDER BY price_date
                                 ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)
                ),
                latest_pivot AS (
                    SELECT DISTINCT ON (symbol) symbol, pp, h5, l5,
                        2*pp - h5           AS s1,
                        2*pp - l5           AS r1,
                        pp + (h5 - l5)      AS r2
                    FROM pivots ORDER BY symbol, price_date DESC
                ),
                hi5d AS (
                    SELECT symbol, MAX(high) AS max_h5d
                    FROM raw_prices
                    WHERE symbol = ANY(%s)
                      AND price_date >= %s - INTERVAL '7 days'
                      AND price_date <= %s
                    GROUP BY symbol
                ),
                hi3d AS (
                    SELECT symbol, MAX(high) AS max_h3d
                    FROM raw_prices
                    WHERE symbol = ANY(%s)
                      AND price_date >= %s - INTERVAL '4 days'
                      AND price_date <= %s
                    GROUP BY symbol
                ),
                latest_close AS (
                    SELECT DISTINCT ON (symbol) symbol, close
                    FROM raw_prices
                    WHERE symbol = ANY(%s) AND price_date <= %s
                    ORDER BY symbol, price_date DESC
                )
                SELECT lc.symbol,
                    lc.close AS entry,
                    lp.s1, lp.r1, lp.r2, lp.pp,
                    h5d.max_h5d,
                    h3d.max_h3d,
                    (lc.close - h3d.max_h3d) / NULLIF(h3d.max_h3d, 0) * 100 AS fall_3d_pct,
                    CASE WHEN h5d.max_h5d > 0.9 * lp.r1
                              OR h5d.max_h5d > 0.9 * lp.r2
                         THEN TRUE ELSE FALSE END AS near_resistance
                FROM latest_close lc
                JOIN latest_pivot lp ON lp.symbol = lc.symbol
                JOIN hi5d         h5d ON h5d.symbol = lc.symbol
                JOIN hi3d         h3d ON h3d.symbol = lc.symbol
                WHERE lp.s1 < lc.close
            """, (syms, today, syms, today, today, syms, today, today, syms, today))
            pivot_rows = {r[0]: dict(zip(
                ["symbol","entry","s1","r1","r2","pp","max_h5d","max_h3d","fall_3d_pct","near_resistance"],
                r)) for r in cur.fetchall()}
    except Exception as e:
        log.warning(f"sell_overbought pivot query: {e}")
        return

    qualified_so = []
    for s in candidates:
        sym = s["symbol"]
        pr  = pivot_rows.get(sym)
        if not pr:
            continue
        if not pr["near_resistance"]:
            continue
        if (pr["fall_3d_pct"] or 0) >= -3.0:
            continue
        # cc#158: V2.1 hard gate (strict-AND) — fall_from_day_high <= -1.5.
        # NULL-passes if no intraday high yet (spec id=1268).
        if so_v21_on and not v21_hard_gate_pass("sell_overbought", s, True):
            continue
        qualified_so.append((s, pr))

    log.info(f"sell_overbought: {len(qualified_so)} qualified after pivot filters"
             f"{' (V2.1 hard-gate ON)' if so_v21_on else ''}")

    for s, pr in qualified_so:
        sym = s["symbol"]
        snap = {
            "rsi_weekly": s.get("rsi_weekly"), "rsi_month": s.get("rsi_month"),
            "sector_week": s.get("sector_week"), "fall_3d_pct": pr["fall_3d_pct"],
            "near_r1": float(pr["max_h5d"] or 0) > 0.9 * float(pr["r1"] or 0),
            "near_r2": float(pr["max_h5d"] or 0) > 0.9 * float(pr["r2"] or 0),
            "filter_score": 5, "filter_total": 5, "so_slots": so_cap,
            "fall_from_day_high": s.get("fall_from_day_high"),   # cc#158
            "v21_enabled": so_v21_on,                            # cc#158
        }
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO v8_qualified
                    (symbol, basket, signal_date, signal_ts, gvm_score, cmp,
                     mom_2d, week_return, month_return, dma_200, dma_50,
                     rsi_month, rsi_weekly, sector_week, sector_day,
                     month_index, week_index_52, daily_rsi, range_3d,
                     metrics, source)
                    VALUES (%s,'sell_overbought',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, basket, signal_date) DO NOTHING
                """, (
                    sym, target_date, signal_ts_ist,
                    s.get("gvm_score"), s.get("_cmp"),
                    s.get("mom_2d"), s.get("week_return"), s.get("month_return"),
                    s.get("dma_200"), s.get("dma_50"),
                    s.get("rsi_month"), s.get("rsi_weekly"),
                    s.get("sector_week"), s.get("sector_day"),
                    s.get("month_index"), s.get("week_index_52"),
                    s.get("daily_rsi"), s.get("range_3d"),
                    json.dumps(snap), "live_5min",
                ))
                first_qualification = cur.rowcount > 0
            conn.commit()

            if first_qualification:
                pv = pivots.get(sym)
                _auto_paper_entry_so(conn, sym, s.get("_cmp"), pv,
                                     target_date, gate_fails)
        except Exception as e:
            log.warning(f"sell_overbought qualified insert {sym}: {e}")


# -- Main entry point ---------------------------------------------------------

def _write_heartbeat(conn):
    """Stamp app_config.sched_writer_hb on a successful tick so run_diagnosis sees
    the writer is alive. Uses the already-open conn (a 2nd psycopg3 connection in the
    scheduler thread fails silently — task #18). Covers scheduler + MCP + API paths."""
    try:
        with conn.cursor() as _hb:
            _hb.execute(
                "INSERT INTO app_config(key,value) VALUES('sched_writer_hb',%s) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                (_now_ist().isoformat(),))
        conn.commit()
    except Exception as _hbe:
        log.warning(f"sched_writer_hb write failed: {_hbe}")


def run_live_signal_writer(conn) -> dict:
    today = datetime.now(IST).date()

    # cc#211: HARD trading-day gate — the SINGLE write-layer choke point. Every caller
    # funnels through here (5-min tick, watchdog/forced-restart recovery tick, 09:10
    # stall-check recovery, MCP, API), and the restart/recovery paths BYPASS the
    # scheduler's market-hours wrapper — that is exactly how Sat 04-Jul wrote 212 junk
    # v8_metrics rows. Gating LINE 1 stops non-trading-day writes for ALL present and
    # future callers, without patching each one. No EOD fallback on weekends either.
    if not nse_holidays.is_trading_day(today):
        log.info(f"signal_writer: {today} is not a trading day — skipping (no v8_metrics write)")
        _ops_log(conn, "info", "signal_writer_skip_nontrading",
                 {"message": f"signal writer invoked on non-trading day {today} — skipped",
                  "date": str(today)})
        return {"skipped": "nontrading_day", "date": str(today)}

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
    vol_cutoff  = _round_down_5min(datetime.now(IST).replace(tzinfo=None))
    vol_tn_map  = _load_vol_ratio_time_normalized(conn, symbols, vol_cutoff)
    hourly_map  = _load_hourly_fut(conn, symbols)   # cc#158: fyers_fut 5m hourly

    if not intraday:
        log.warning("signal_writer: no intraday bars -- fyers_feed not running, using EOD fallback")
        all_metrics = []
        for sym in symbols:
            eod  = eod_metrics.get(sym, {})
            cmp  = cmp_map.get(sym)
            c2d  = eod_history.get(sym, {}).get("close_2d_ago")
            row  = dict(eod)
            row["symbol"]  = sym
            row["mom_2d"]  = (cmp / c2d - 1) * 100 if (cmp and c2d and c2d > 0) else eod.get("eod_mom_2d")
            row["_cmp"]    = cmp
            row["hourly_pct"] = hourly_map.get(sym)   # cc#158 (NULL in EOD fallback)
            all_metrics.append(row)
        _write_qualified(conn, all_metrics, today)
        _write_heartbeat(conn)
        _assert_no_nontrading_metrics(conn)   # cc#211
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
        m   = _compute_live_metrics(hist, bar, cmp, eod, vol_tn_map.get(sym))
        m["symbol"] = sym
        m["_cmp"]   = cmp if cmp else bar["close"]
        m["hourly_pct"] = hourly_map.get(sym)   # cc#158: fyers_fut 5m hourly
        computed[sym] = m

    _add_sector_aggregates(computed, eod_metrics)

    all_metrics = []
    for sym, m in computed.items():
        try:
            _upsert_metrics(conn, sym, m, today)
        except Exception as e:
            log.warning(f"upsert_metrics {sym}: {e}")
        all_metrics.append(m)

    # cc#140 (01-Jul-2026): vol_ratio side-by-side visibility -- old (legacy, full-day
    # avg) vs new (time-normalized, fyers_eq-only) formula, and NULL-state diagnosis
    # per data_gap_resolution_01Jul2026 (no silent NULLs -- must be visible in logs).
    insufficient_hist = sum(1 for m in all_metrics if (m.get("vol_ratio_days_available") or 0) < _VOL_MIN_CLEAN_DAYS)
    fallbacks = sum(1 for m in all_metrics if m.get("vol_ratio_fallback"))
    old_pass = sum(1 for m in all_metrics if (m.get("vol_ratio_legacy") or 0) >= 1.5)
    new_pass = sum(1 for m in all_metrics if (m.get("vol_ratio_time_normalized") or 0) >= 1.5)
    log.warning(
        f"vol_ratio[cc#170 v2]: cutoff={vol_cutoff} symbols={len(all_metrics)} "
        f"insufficient_history(<{_VOL_MIN_CLEAN_DAYS}d)={insufficient_hist} fallback_to_v1={fallbacks} "
        f"gate>=1.5: legacy={old_pass} time_matched_v2={new_pass}"
    )

    _write_qualified(conn, all_metrics, today)

    _write_adr_intraday(conn)
    _update_sector_aggregates_sql(conn, today)

    log.info(f"signal_writer: {len(computed)} updated, {no_bar} no_bar, source=live_5min")
    _write_heartbeat(conn)
    _assert_no_nontrading_metrics(conn)   # cc#211: loud on any bypassed non-trading write
    return {
        "date":    str(today),
        "updated": len(computed),
        "no_bar":  no_bar,
        "total":   len(symbols),
        "source":  "live_5min",
    }
