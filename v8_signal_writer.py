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
  BUY threshold (buy_momentum -- 18-Jun-2026):
    Strong Bullish (0 fails) + Bullish (1 fail): n   (strict AND -- fewer, higher quality)
    Neutral (2 fails) + Bearish (3+ fails):       n-1 (1 miss allowed -- genuine setups rarer)
  SELL threshold (sell_reversal=5, sell_momentum=6):
    Strong Bullish (0 fails) + Bullish (1 fail): n-1 (1 miss allowed -- 18-Jun-2026)
    Neutral (2 fails) + Bearish (3+ fails):       n   (strict AND)
    Rationale: in bull markets genuine weakness is rarer; n-1 still a
    meaningful signal. In bear markets signals fire freely -- keep strict.

buy_reversal V3 (10-Jul-2026, spec id=2818 -- supersedes V2 dynamic-regime, id=355 archived):
  TRUE REVERSAL inverse-sandwich dip-buy. NOT in the score-gate loop; dedicated strict-AND of 8
  fixed conditions via _write_buy_reversal_v3_qualified (NO Nifty regime, NO R2):
    daily_rsi<=40, true_weekly_rsi>=60 (TRUE calendar weekly, basket-local), dma_200>=0,
    gvm_score>=6.5, mom_2d 0-3, hourly_pct 0.1-1.0 (NULL first hr), CMP>PP, room-to-R1>2%.
  Entry live CMP, target R1 only, stop 1:1 mirror, max hold 15 trading days. Standard slot pool.

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
import guards          # cc#217 P2: canonical trading-day gate + entry-gate + guard primitives (sim-aware)
from time import perf_counter   # cc#217 P3: tick wall-time (distinct from datetime.time)
from sim_clock import _now, _today   # cc#218: injectable clock (sim_ts=None => live)

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


# cc#256: per-tick slot_full accumulator. signal_ts_ist is one shared value per
# _write_qualified() tick, so that call IS the natural tick boundary — reset here at its
# start, the 4 auto-entry slot_full branches append, and _flush_slot_blocks emits a single
# ops_log alert at tick end when any pool's blocked count exceeds the app_config threshold.
# (07-Jul recovery tick silently slot_full-blocked 10 SHORT candidates with zero visibility.)
_slot_full_blocks: Dict[str, list] = {}


def _reset_slot_blocks() -> None:
    global _slot_full_blocks
    _slot_full_blocks = {"LONG": [], "SHORT": [], "SO": [], "S1B": []}


def _record_slot_block(pool: str, sym: str, open_cnt: int, cap: int) -> None:
    try:
        _slot_full_blocks.setdefault(pool, []).append(
            {"symbol": sym, "open": open_cnt, "cap": cap})
    except Exception:
        pass


def _slot_full_threshold(conn) -> int:
    """app_config-driven burst threshold (default 3), tunable live without redeploy. Alert
    fires when a pool's same-tick blocked count EXCEEDS this — routine 1-2 slot-full touches
    are normal operation and never alert."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='slot_full_burst_threshold'")
            r = cur.fetchone()
        if r and r[0] is not None:
            return int(str(r[0]).strip())
    except Exception as e:
        log.warning(f"slot_full_threshold read: {e}")
    return 3


def _flush_slot_blocks(conn, signal_ts_ist) -> None:
    """cc#256: at tick end, alert loudly if a burst of same-tick slot_full blocks piled up on
    any pool (LONG/SHORT standard, SO, S1B) — so a recovery-tick or volatile-market pileup is
    logged immediately, not discovered later by screenshot + manual SQL."""
    threshold = _slot_full_threshold(conn)
    for pool, blocks in _slot_full_blocks.items():
        if len(blocks) <= threshold:
            continue
        syms = [b["symbol"] for b in blocks]
        cap = blocks[0]["cap"]
        open_cnt = max((b["open"] for b in blocks), default=None)
        _ops_log(conn, "alert", "slot_full_burst", {
            "pool": pool, "cap": cap, "current_open_count": open_cnt,
            "blocked_count": len(blocks), "blocked_symbols": syms,
            "threshold": threshold, "signal_ts": str(signal_ts_ist),
        })
        log.warning(f"slot_full_burst {pool}: {len(blocks)} blocked (cap={cap}) {syms}")


def _assert_no_nontrading_metrics(conn) -> None:
    """cc#211 self-defense: if the latest v8_metrics row is dated a non-trading day,
    something bypassed the write gate — make the silent corruption LOUD."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(score_date) FROM v8_metrics")
            row = cur.fetchone()
        latest = row[0] if row else None
        if latest and not guards.is_trading_day(latest):
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

def _now_ist(sim_ts=None) -> datetime:
    """Current datetime in IST as a naive datetime (for DB storage). cc#218: routes
    through the injectable clock — sim_ts=None is exactly datetime.now(IST) (live)."""
    return _now(sim_ts)


def _bar_cutoff(sim_ts=None) -> datetime:
    """As-of cutoff for reading intraday BARS (cc#218 D6 fix). A 5-min bar stamped T only
    finishes and lands in the DB at T+5min, so at sim tick T the sim may see ONLY bars that
    had already closed by then — ts <= T-5min. Reading ts <= T would give a one-bar (5-min)
    LOOKAHEAD and diverge from the live signals. LIVE (sim_ts=None) is untouched: unfinished
    bars never exist in the DB, so `ts <= now` is already correct — no shift applied."""
    n = _now(sim_ts)
    return n - timedelta(minutes=5) if sim_ts is not None else n


# -- Pivot-room gate ----------------------------------------------------------

BASKET_SIDE = {
    "buy_reversal":  "BUY",
    "buy_momentum":  "BUY",
    "sell_reversal": "SELL",
    "sell_momentum": "SELL",
}


def _load_pivots(conn, sim_ts=None) -> Dict[str, dict]:
    # cc#218: live uses the latest pivot set (today's, computed pre-open). In sim, pin to
    # the sim day's pivots (pivot_date = sim_date) — a belt in case the harness schema does
    # not materialize v8_paper_pivots and the read falls through to public (latest != sim).
    with conn.cursor() as cur:
        if sim_ts is None:
            cur.execute("""
                SELECT symbol, pp, r1, s1, s2
                FROM v8_paper_pivots
                WHERE pivot_date = (SELECT MAX(pivot_date) FROM v8_paper_pivots)
                  AND pp IS NOT NULL AND r1 IS NOT NULL AND s1 IS NOT NULL
            """)
        else:
            cur.execute("""
                SELECT symbol, pp, r1, s1, s2
                FROM v8_paper_pivots
                WHERE pivot_date = %s
                  AND pp IS NOT NULL AND r1 IS NOT NULL AND s1 IS NOT NULL
            """, (_today(sim_ts),))
        # cc#378: s2 added (NOT in the WHERE, so it never drops a symbol) — sell_reversal V5-D
        # picks S1 or S2 as its dynamic target. s2 may be None for a symbol without it.
        return {r[0]: {"pp": float(r[1]), "r1": float(r[2]), "s1": float(r[3]),
                       "s2": float(r[4]) if r[4] is not None else None}
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

def _write_adr_intraday(conn, sim_ts=None):
    # cc#218: point-in-time A/D — intraday cmp is the last CLOSED fyers-any bar (<= sim_ts-5min
    # in sim, D6 fix) on the (sim or live) date; prior close from raw_prices < that date.
    # sim_ts=None is live (no shift — unfinished bars aren't in the DB).
    _d = _today(sim_ts)
    _cut = _bar_cutoff(sim_ts)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH li AS (
                    SELECT DISTINCT ON (symbol) symbol, close AS cmp
                    FROM intraday_prices WHERE ts::date = %s AND ts <= %s
                    ORDER BY symbol, ts DESC
                ),
                pc AS (
                    SELECT DISTINCT ON (symbol) symbol, close AS pclose
                    FROM raw_prices WHERE price_date < %s
                    ORDER BY symbol, price_date DESC
                )
                SELECT
                    COUNT(*) FILTER (WHERE li.cmp > pc.pclose) AS advances,
                    COUNT(*) FILTER (WHERE li.cmp < pc.pclose) AS declines,
                    COUNT(*) FILTER (WHERE li.cmp = pc.pclose) AS unchanged,
                    COUNT(*) AS total
                FROM li JOIN pc ON pc.symbol = li.symbol
            """, (_d, _cut, _d))
            row = cur.fetchone()
            if not row or (row[3] or 0) < 50:
                return
            adv, dec, unc, tot = row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0
            adr = round(adv / dec, 3) if dec else float(adv)
            now_ist = _now_ist(sim_ts)
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
        # cc#218 hotfix: a failed INSERT aborts the transaction; without a rollback the
        # aborted state persists and silently kills _update_sector_aggregates / heartbeat
        # that run after this. Prior work is already committed, so rollback discards only
        # this failed statement. (live + sim — same code both modes.)
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning(f"_write_adr_intraday: {e}")


# -- Step 1: Load EOD metrics snapshot ----------------------------------------

def _load_eod_metrics(conn, sim_ts=None) -> Dict[str, dict]:
    # cc#218: the EOD baseline a live tick sees at start-of-day is the latest EOD row.
    # In sim (replaying a past day) that must be the latest EOD row DATED BEFORE the sim
    # day — else we'd leak future EOD values. sim_ts=None keeps the exact live queries.
    _asof = _today(sim_ts) if sim_ts is not None else None
    gvm_map: Dict[str, dict] = {}
    with conn.cursor() as cur:
        if sim_ts is None:
            cur.execute("""
                SELECT symbol, gvm_score, segment
                FROM gvm_scores
                WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)
            """)
        else:
            cur.execute("""
                SELECT symbol, gvm_score, segment
                FROM gvm_scores
                WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores WHERE score_date < %s)
            """, (_asof,))
        for sym, gvm, seg in cur.fetchall():
            gvm_map[sym] = {"gvm_score": _safe_float(gvm), "segment": seg}

    frozen_map: Dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (symbol)
                symbol, rsi_month, rsi_weekly, sector_week, sector_month,
                mom_2d AS eod_mom_2d
            FROM v8_metrics
            WHERE (rsi_month   IS NOT NULL
               OR rsi_weekly  IS NOT NULL
               OR sector_week IS NOT NULL
               OR sector_month IS NOT NULL)
              AND (%s::date IS NULL OR score_date < %s::date)
            ORDER BY symbol, score_date DESC
        """, (_asof, _asof))
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

def _load_eod_history(conn, symbols: List[str], sim_ts=None) -> Dict[str, dict]:
    today = _today(sim_ts)   # cc#218: history strictly before the (sim or live) day
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

def _load_intraday_bars(conn, symbols: List[str], sim_ts=None) -> Dict[str, dict]:
    """source='fyers_eq' pinned throughout (cc#140, 01-Jul-2026): intraday_prices
    carries both fyers_eq (equity) and fyers_fut (futures contract) rows per
    symbol/day. Without a source filter, MAX(volume) etc. silently pick
    whichever series is numerically larger, mixing equity-scale price/volume
    with futures-scale price/volume for the same symbol.

    cc#218: point-in-time — `AND ts <= cut` where cut=_now(sim_ts). In live cut is the
    real now, so `ts <= now` is a no-op (bars only exist up to now); in sim it is the
    as-of cutoff so the tick only sees bars up to the frozen clock. `today`=_today(sim_ts)."""
    today = _today(sim_ts)
    cut = _bar_cutoff(sim_ts)   # cc#218 D6: bars close at ts+5min -> sim sees ts<=sim_ts-5min
    with conn.cursor() as cur:
        # cc#217 P3: single GROUP BY pass (was 2 correlated per-symbol subqueries + 3 FILTER
        # aggregates). The outer WHERE already restricts to today/fyers_eq/ts<=cut, so the old
        # FILTERs were redundant; live_close/day_open come from array_agg ordered by ts (last
        # close / first open) — byte-identical to the old ORDER BY ts DESC/ASC LIMIT 1.
        cur.execute("""
            SELECT
                symbol,
                (array_agg(close ORDER BY ts DESC))[1]  AS live_close,
                (array_agg(open  ORDER BY ts ASC ))[1]  AS day_open,
                MAX(high)   AS day_high,
                MIN(low)    AS day_low,
                MAX(volume) AS day_vol,
                MAX(ts)     AS bar_ts        -- cc#259: latest bar timestamp for the freshness gate
            FROM intraday_prices
            WHERE symbol = ANY(%s) AND ts::date = %s AND source = 'fyers_eq' AND ts <= %s
            GROUP BY symbol
        """, (symbols, today, cut))
        bars = {}
        for sym, lc, op, hi, lo, vol, bts in cur.fetchall():
            if lc is None:
                continue
            bars[sym] = {
                "close":  _safe_float(lc),
                "open":   _safe_float(op),
                "high":   _safe_float(hi),
                "low":    _safe_float(lo),
                "volume": _safe_float(vol),
                "bar_ts": bts,               # cc#259: naive IST bar timestamp (freshness check)
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


def _build_vol_baseline(conn, symbols: List[str], sim_ts=None) -> None:
    """cc#170: build the 7-trading-day same-time cumulative-volume baseline curve
    for every symbol, once per day. For each symbol/day the best clean source is
    used (fyers_eq WS > fyers REST > yahoo) with per-day SEMANTICS AUTO-DETECT
    (cc#150 pattern): a monotonic non-decreasing volume series is a cumulative
    day counter -> cum at t = latest value <= t; otherwise volumes are per-bar
    -> cum at t = SUM(bars <= t). Never mixes the two interpretations.
    cc#218: baseline reconstructs from days STRICTLY BEFORE the (sim or live) day
    (RULING_B: score/vol baseline is prior-day data the live tick sees at start-of-day)."""
    today = _today(sim_ts)
    _VOL_BASELINE["date"] = today
    _VOL_BASELINE["curve"] = {}
    _VOL_BASELINE["days"] = {}
    _VOL_BASELINE["full_day"] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, source, ts::date, ts::time, volume
            FROM intraday_prices
            WHERE symbol = ANY(%s) AND ts::date < %s
              AND ts::date >= %s - INTERVAL '11 days'
              AND source IN ('fyers_eq', 'fyers', 'yahoo')
              AND volume IS NOT NULL
              AND ts::time BETWEEN '09:15' AND '15:30'
            ORDER BY symbol, source, ts
        """, (symbols, today, today))
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


def _load_vol_ratio_time_normalized(conn, symbols: List[str], cutoff: time, sim_ts=None) -> Dict[str, dict]:
    """VOL X v2 (cc#170, supersedes cc#140 v1.5): today's cumulative volume at
    time x vs AVG cumulative volume at the same time x over the last 7 clean
    trading days (precomputed curve, source-semantics safe -- see
    _build_vol_baseline). After close the comparison is full-day vs 7-day avg
    full-day (cutoff clamps to the last bucket), so v2 stays consistent EOD.
    <4 clean baseline days -> ratio None here; _compute_live_metrics falls back
    to the v1 formula (cum / 10d full-day avg) and flags vol_ratio_fallback."""
    if _VOL_BASELINE["date"] != _today(sim_ts):   # cc#218
        try:
            _build_vol_baseline(conn, symbols, sim_ts=sim_ts)
        except Exception as e:
            # cc#218 hotfix: a failed baseline SELECT aborts the transaction; clear it so
            # the reads/writes that follow this tick don't silently die on aborted state.
            try:
                conn.rollback()
            except Exception:
                pass
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
              AND ts::date = %s AND ts::time <= %s
            GROUP BY symbol
        """, (symbols, _today(sim_ts), cutoff))   # cc#218: CURRENT_DATE -> sim/live date
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

def _load_cmp(conn, sim_ts=None) -> Dict[str, float]:
    # cc#218: LIVE reads the cmp_prices snapshot (latest LTP per symbol). That table is a
    # single-row-per-symbol live snapshot and CANNOT be rewound, so in SIM we reconstruct
    # CMP as the latest fyers_eq bar close <= sim_ts (same series the writer's _cmp
    # fallback uses). sim_ts=None keeps the exact live query.
    if sim_ts is None:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, cmp FROM cmp_prices")
            return {r[0]: _safe_float(r[1]) for r in cur.fetchall()}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (symbol) symbol, close
            FROM intraday_prices
            WHERE source = 'fyers_eq' AND ts::date = %s AND ts <= %s
            ORDER BY symbol, ts DESC
        """, (_today(sim_ts), _bar_cutoff(sim_ts)))   # cc#218 D6: no bar lookahead
        return {r[0]: _safe_float(r[1]) for r in cur.fetchall()}


def _load_hourly_fut(conn, symbols: List[str], sim_ts=None) -> Dict[str, Optional[float]]:
    """cc#158: hourly momentum on the FUTURES series (spec id 1263-1267).
    (last 5m close - close 12 bars ago)/close_12_ago * 100, from
    intraday_prices source='fyers_fut' timeframe='5m', single tick at
    qualification. 12 bars * 5min = 60min = "hourly". NULL when the 12-bars-ago
    bar does not exist yet (first ~hour of the session) so the hard gate
    NULL-passes rather than blocking early signals.
    cc#218: point-in-time `AND ts <= cut` (no-op in live) so the ROW_NUMBER window ranks
    only bars up to the frozen clock; today=_today(sim_ts)."""
    today = _today(sim_ts)
    cut = _bar_cutoff(sim_ts)   # cc#218 D6: bars close at ts+5min -> sim sees ts<=sim_ts-5min
    out: Dict[str, Optional[float]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH ranked AS (
                    SELECT symbol, close,
                           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
                    FROM intraday_prices
                    WHERE source = 'fyers_fut' AND timeframe = '5m'
                      AND ts::date = %s AND symbol = ANY(%s) AND ts <= %s
                )
                SELECT symbol,
                       MAX(close) FILTER (WHERE rn = 1)  AS last_close,
                       MAX(close) FILTER (WHERE rn = 13) AS close_12_ago
                FROM ranked
                WHERE rn IN (1, 13)
                GROUP BY symbol
            """, (today, symbols, cut))
            for sym, last_close, close_12_ago in cur.fetchall():
                if last_close is not None and close_12_ago and float(close_12_ago) > 0:
                    out[sym] = (float(last_close) / float(close_12_ago) - 1) * 100
                else:
                    out[sym] = None
    except Exception as e:
        # cc#218 hotfix: failed SELECT aborts the transaction — clear it so the compute +
        # write phase after this load doesn't die silently on aborted state.
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning(f"_load_hourly_fut: {e} -- hourly NULL-passes this tick")
    return out


def _load_hourly_fut_v3(conn, symbols: List[str], sim_ts=None) -> Dict[str, tuple]:
    """cc#355: buy_reversal_v3 hourly with NO null-pass window. Returns {sym: (hourly_pct, n_bars)}:
      - n_bars = settled fyers_fut 5m bars so far today (the first settles at 09:20).
      - hourly_pct: >=13 bars (~10:15+) -> standard 12-bar (60-min) rolling change (last vs 12-ago);
                    1..12 bars (09:20-~10:15) -> PARTIAL window = (last close / earliest settled bar
                    of the day - 1)*100; 0 bars (09:15-09:20) -> None (the only exempt window).
    The handler enforces 0.1-1.0 whenever n_bars>=1 (i.e. from 09:20 on). In sim/replay with no
    recent fut bars n_bars=0 -> exempt (hourly stays a live-only trigger, per spec id 1263-1267)."""
    today = _today(sim_ts)
    cut   = _bar_cutoff(sim_ts)
    out: Dict[str, tuple] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH ranked AS (
                    SELECT symbol, close,
                           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn,
                           COUNT(*)     OVER (PARTITION BY symbol)                  AS n
                    FROM intraday_prices
                    WHERE source='fyers_fut' AND timeframe='5m'
                      AND ts::date = %s AND symbol = ANY(%s) AND ts <= %s
                )
                SELECT symbol,
                       MAX(n)                                    AS n_bars,
                       MAX(close) FILTER (WHERE rn = 1)          AS last_close,
                       MAX(close) FILTER (WHERE rn = 13)         AS close_12_ago,
                       MAX(close) FILTER (WHERE rn = n)          AS first_close
                FROM ranked GROUP BY symbol
            """, (today, symbols, cut))
            for sym, n_bars, last_close, close_12_ago, first_close in cur.fetchall():
                n_bars = int(n_bars or 0)
                hourly = None
                if last_close is not None:
                    if n_bars >= 13 and close_12_ago and float(close_12_ago) > 0:
                        hourly = (float(last_close) / float(close_12_ago) - 1) * 100
                    elif n_bars >= 1 and first_close and float(first_close) > 0:
                        hourly = (float(last_close) / float(first_close) - 1) * 100
                out[sym] = (hourly, n_bars)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning(f"_load_hourly_fut_v3: {e} -- hourly exempt this tick")
    return out


def _load_filter_state(conn, sim_ts=None) -> Dict[str, bool]:
    """cc#158: per-basket V2.1 enable state. LIVE (sim_ts=None) reads v8_filter_state so a
    kill-switch disable takes effect on the next signal pass — byte-identical to before.
    cc#324: SIM/BT7 (sim_ts set) is POINT-IN-TIME — the latest v8_filter_state_log row per
    basket with changed_at <= the replayed day START (IST midnight); a basket with no predating
    log row resolves DISABLED (matches the locked fail-safe). This closes the parity hole where
    a replay of a past day applied TODAY'S enable state. FAIL-SAFE: on any error, return {} ->
    every basket's hard gate treats itself as DISABLED (exact locked behavior), never on."""
    try:
        with conn.cursor() as cur:
            if sim_ts is None:
                cur.execute("SELECT basket, enabled FROM v8_filter_state")
                return {b: bool(e) for b, e in cur.fetchall()}
            cur.execute("""
                SELECT DISTINCT ON (basket) basket, enabled
                FROM v8_filter_state_log
                WHERE changed_at <= (%s::timestamp AT TIME ZONE 'Asia/Kolkata')
                ORDER BY basket, changed_at DESC
            """, (_today(sim_ts),))
            return {b: bool(e) for b, e in cur.fetchall()}
    except Exception as e:
        # cc#218 hotfix: failed SELECT aborts the transaction — clear it so the per-basket
        # qualified inserts that follow in _write_qualified don't die silently.
        try:
            conn.rollback()
        except Exception:
            pass
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
        "sector_day": None,
    }

    if len(c) >= 20:  out["dma_20"]  = _safe_pct(live, float(np.mean(c[-20:])))
    if len(c) >= 50:  out["dma_50"]  = _safe_pct(live, float(np.mean(c[-50:])))
    if len(c) >= 200: out["dma_200"] = _safe_pct(live, float(np.mean(c[-200:])))

    if len(c) >= 253: out["year_return"]  = _safe_pct(live, c[-253])
    if len(c) >= 22:  out["month_return"] = _safe_pct(live, c[-22])
    if len(c) >= 6:   out["week_return"]  = _safe_pct(live, c[-6])

    # cc#367: day_1d & mom_2d are pinned to `live` (the clean equity 5-min bar close), NOT the
    # cmp_prices LTP. cmp_prices can be polluted by 3-4% (a futures tick leaking onto the spot
    # key, or a corrupt post-close tick), and mom_2d is a LIVE GATE INPUT (buy_reversal 0-3,
    # buy_momentum 0.5-6, sell_momentum <=-1.5, sell_reversal <=-3.0) — a polluted snapshot could
    # flip qualifications. Every other ratio in this function already uses `live`; this makes the
    # two momentum ratios consistent with them and immune to cmp_prices corruption. (day_1d is
    # display-only — confirmed not present in any FILTER_CONFIG gate.)
    price = live

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

    # cc#232: 4 dead range/BB metrics removed (0 readers, gated nothing, display-only).
    # daily_rsi + ma9_vs_ma21 KEPT (active external readers — trade-check, GVM, paper).

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
    op = bar.get("open")
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

# cc#217 P3: single source for the v8_metrics upsert SQL + row builder, shared by the batch
# path (one executemany + one commit per tick) and the per-symbol fallback. Byte-identical to
# the pre-P3 per-symbol INSERT (cc#218: score_date=target_date; computed_at=NOW() is
# write-metadata, never parity-compared).
_UPSERT_METRICS_SQL = """
    INSERT INTO v8_metrics
    (symbol, score_date, gvm_score,
     dma_20, dma_50, dma_200, daily_rsi,
     rsi_month, rsi_weekly,
     month_return, week_return, year_return, mom_2d,
     day_1d, eod_chg,
     sector_day, sector_week, sector_month,
     month_index, week_index_52,
     ma9_vs_ma21, vol_ratio)
    VALUES (%s,%s,%s, %s,%s,%s,%s, %s,%s, %s,%s,%s,%s,
            %s,%s, %s,%s,%s, %s,%s, %s,%s)
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
        ma9_vs_ma21   = EXCLUDED.ma9_vs_ma21,
        vol_ratio     = EXCLUDED.vol_ratio,
        computed_at   = NOW()
"""


def _metrics_row(sym: str, m: dict, target_date: date) -> tuple:
    """The v8_metrics upsert param tuple for one symbol."""
    return (
        sym, target_date, m.get("gvm_score"),
        m.get("dma_20"), m.get("dma_50"), m.get("dma_200"), m.get("daily_rsi"),
        m.get("rsi_month"), m.get("rsi_weekly"),
        m.get("month_return"), m.get("week_return"), m.get("year_return"), m.get("mom_2d"),
        m.get("day_1d"), m.get("eod_chg"),
        m.get("sector_day"), m.get("sector_week"), m.get("sector_month"),
        m.get("month_index"), m.get("week_index_52"),
        m.get("ma9_vs_ma21"), m.get("vol_ratio"),
    )


def _upsert_metrics(conn, sym: str, m: dict, target_date: date, sim_ts=None):
    """Single-symbol upsert + commit (retained for the batch's per-symbol fallback path)."""
    with conn.cursor() as cur:
        cur.execute(_UPSERT_METRICS_SQL, _metrics_row(sym, m, target_date))
    conn.commit()


def _upsert_metrics_batch(conn, computed: dict, target_date: date, sim_ts=None) -> int:
    """cc#217 P3: upsert the whole tick's metrics in ONE executemany + ONE commit — was 212
    sequential INSERT+COMMIT, the biggest tick-latency cost.

    Preserves the cc#218 SAVEPOINT intent ('one bad symbol must not kill the rest'). psycopg
    executemany is all-or-nothing — one bad row aborts the batch — so on batch failure we
    ROLLBACK TO SAVEPOINT and FALL BACK to per-symbol upserts (each in its own savepoint), so
    exactly the offending symbol is skipped and every good row still lands. Returns #written."""
    syms = list(computed.keys())
    if not syms:
        return 0
    rows = [_metrics_row(sym, computed[sym], target_date) for sym in syms]
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT sp_batch")
            cur.executemany(_UPSERT_METRICS_SQL, rows)
        conn.commit()
        return len(rows)
    except Exception as e:
        try:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT sp_batch")
        except Exception:
            pass
        log.warning(f"upsert_metrics batch failed ({e}) — per-symbol fallback")
    ok = 0
    for sym in syms:
        try:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT sp_upsert")
                cur.execute(_UPSERT_METRICS_SQL, _metrics_row(sym, computed[sym], target_date))
            conn.commit()
            ok += 1
        except Exception as e2:
            try:
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_upsert")
            except Exception:
                pass
            log.warning(f"upsert_metrics {sym}: {e2}")
    return ok


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

def _market_gate_fails(conn, sim_ts=None) -> int:
    # cc#218: point-in-time mood gate. _d = the (sim or live) date == CURRENT_DATE in live
    # (UTC session, market hours); _cut = _now(sim_ts) so intraday reads stop at the frozen
    # clock (no-op in live). Every CURRENT_DATE below routes through these.
    _d = _today(sim_ts)
    _cut = _bar_cutoff(sim_ts)   # cc#218 D6: intraday reads stop at last CLOSED bar (sim_ts-5min)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT advances, declines, universe_count
                FROM adr_intraday
                WHERE ts::date = %s AND ts <= %s
                ORDER BY ts DESC LIMIT 1
            """, (_d, _cut))
            row = cur.fetchone()
            if row and (row[2] or 0) >= 50:
                adv, dec = row[0] or 0, row[1] or 0
                adr = (adv / dec) if dec else float(adv)
            else:
                cur.execute("""
                    WITH li AS (
                        SELECT DISTINCT ON (symbol) symbol, close AS cmp
                        FROM intraday_prices WHERE ts::date = %s AND ts <= %s
                        ORDER BY symbol, ts DESC
                    ),
                    pc AS (
                        SELECT DISTINCT ON (symbol) symbol, close AS pclose
                        FROM raw_prices WHERE price_date < %s
                        ORDER BY symbol, price_date DESC
                    )
                    SELECT COUNT(*) FILTER (WHERE li.cmp > pc.pclose),
                           COUNT(*) FILTER (WHERE li.cmp < pc.pclose),
                           COUNT(*)
                    FROM li JOIN pc ON pc.symbol = li.symbol
                """, (_d, _cut, _d))
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
                    cur.execute("SELECT adr FROM adr_daily WHERE price_date = %s ORDER BY price_date DESC LIMIT 1", (_d,))
                    r = cur.fetchone()
                    adr = float(r[0]) if r and r[0] is not None else 1.0

            cur.execute("""
                SELECT close FROM intraday_prices
                WHERE symbol='NIFTY50' AND ts::date=%s AND ts <= %s
                ORDER BY ts DESC LIMIT 1
            """, (_d, _cut))
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
                    FROM intraday_prices WHERE symbol='NIFTY50' AND ts::date < %s
                    ORDER BY ts::date DESC, ts DESC
                ),
                eod AS (
                    SELECT price_date AS d, close::numeric AS c
                    FROM raw_prices WHERE symbol='NIFTY50' AND price_date < %s
                ),
                merged AS (
                    SELECT d, c FROM days
                    UNION
                    SELECT d, c FROM eod WHERE d NOT IN (SELECT d FROM days)
                )
                SELECT c FROM merged ORDER BY d DESC LIMIT 30
            """, (_d, _d))
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

            # cc#323: REVERTED cc#265 — India VIX removed from the market gate (never founder-
            # approved as a check). Back to the locked 4-check design: ADR + Nifty day/week/month.
            # (The VIX 5-min feed, /indiavix_intraday endpoint and dashboard popout stay — display
            # only, not a gate vote.)
            checks = [adr >= 1.0, nday >= 0, nweek >= 0, nmonth >= 0]
            return sum(1 for c in checks if not c)
    except Exception as e:
        # cc#216: fail CONSERVATIVE, never aggressive. Returning 0 fails = Strong Bullish
        # = max buy aggression (15B/5S) on an ERROR — exactly backwards. Return 2 (Neutral,
        # 12B/8S) and make the degraded mood loud in ops_log.
        log.warning(f"_market_gate_fails: {e} — defaulting to Neutral (2 fails), not Strong Bullish")
        try:
            _ops_log(conn, "alert", "market_gate_fails_error",
                     {"message": f"market-mood gate errored ({e}) — defaulted to Neutral (2 fails) "
                                 f"to avoid max-aggression buying on a compute failure"})
        except Exception:
            pass
        return 2


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

def _get_nifty_1m_return(conn, sim_ts=None) -> float:
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH ranked AS (
                    SELECT close,
                           ROW_NUMBER() OVER (ORDER BY price_date DESC) AS rn
                    FROM raw_prices
                    WHERE symbol='NIFTY50' AND price_date < %s
                    LIMIT 25
                )
                SELECT
                    (SELECT close FROM ranked WHERE rn=1)  AS latest,
                    (SELECT close FROM ranked WHERE rn=22) AS month_ago
            """, (_today(sim_ts),))
            row = cur.fetchone()
            if row and row[0] and row[1] and float(row[1]) > 0:
                return (float(row[0]) / float(row[1]) - 1) * 100
    except Exception as e:
        log.warning(f"_get_nifty_1m_return: {e}")
    return 0.0


def _get_nifty_rsi(conn, sim_ts=None) -> Optional[float]:
    """Wilder RSI(14) on NIFTY50 daily closes -- market health gate for buy_s1_bounce."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT close FROM raw_prices
                WHERE symbol='NIFTY50' AND price_date < %s
                ORDER BY price_date DESC LIMIT 30
            """, (_today(sim_ts),))
            closes = [float(r[0]) for r in cur.fetchall()]
        if len(closes) < 15:
            return None
        closes.reverse()
        return _wilder_rsi(pd.Series(closes), 14)
    except Exception as e:
        log.warning(f"_get_nifty_rsi: {e}")
        return None


# cc#354: _get_dynamic_buy_reversal_overrides (V2 Nifty-regime bounds) removed — buy_reversal V3
# uses fixed absolute conditions (spec id=2818), no regime overrides.


# -- Auto paper entry (standard baskets) --------------------------------------

_PAPER_SIDE_MAP = {"BUY": "LONG", "SELL": "SHORT"}

def _conflict_ok(conn, sym: str, paper_side: str, basket: str, d: date, cmp: float, sim_ts=None) -> bool:
    """cc#214: enforce the founder-locked conflict policy (12-Jun) on the LIVE entry path.

    The live auto-entry functions only checked the SAME side open — so a next-day OPPOSITE
    qualification would open the opposite side = a simultaneous LONG+SHORT hedge (policy
    violation). This reuses v8_paper._resolve_conflict (the SAME helper paper_tick uses —
    never duplicate the policy):
      • same-day opposite open  -> BLOCK new entry, log missed 'opposite_open' (existing holds)
      • next-day opposite open   -> CONFLICT_EXIT flatten existing @ the live equity CMP, log
                                    missed 'conflict_exit_blocked', do NOT open new (never
                                    reverse, never hedge; may re-enter opposite a later day)
    Returns True to PROCEED, False to SKIP. Fail-closed on error (never risk a hedge).
    exit price = the live equity CMP (cc#215: cmp is equity-priced), exit_ts = now IST."""
    try:
        import v8_paper
        return v8_paper._resolve_conflict(conn, sym, paper_side, basket, d, round(cmp, 2), _now_ist(sim_ts))
    except Exception as e:
        log.warning(f"conflict check {sym} {paper_side}: {e} — skipping entry (fail-closed, never hedge)")
        return False


def _entry_guards(conn, sym: str, paper_side: str, basket: str, d: date, cmp: float,
                  sim_ts=None, basket_scoped: bool = False) -> bool:
    """cc#217 P2: shared pre-entry gate for all three auto-entry fns — the ~70%-duplicated
    guard block. In order (identical to the old inline sequence): earnings blackout ->
    same-side OPEN -> traded-today (basket-scoped for the SO/S1B dedicated pools, generic
    trades+positions for standard baskets) -> founder-locked opposite-side conflict policy.
    Returns True to PROCEED, False to SKIP. Fail-closed (any guard-query error => SKIP)."""
    try:
        if guards.blackout(conn, sym, _today(sim_ts)):
            log.debug(f"auto_paper {sym}: skipped -- blackout")
            return False
        if guards.has_open(conn, sym, paper_side):
            return False
        if guards.traded_today(conn, sym, paper_side, d, basket=(basket if basket_scoped else None)):
            return False
    except Exception as e:
        log.warning(f"entry guards {sym} {paper_side}: {e} — skipping entry (fail-closed)")
        return False
    # opposite-side conflict policy (block same-day / CONFLICT_EXIT next-day); own try inside
    return _conflict_ok(conn, sym, paper_side, basket, d, cmp, sim_ts=sim_ts)


def _auto_paper_entry(conn, sym: str, basket: str, side: str, cmp: Optional[float],
                       pv: Optional[dict], d: date, gate_fails: int, sim_ts=None,
                       target: Optional[float] = None, stop: Optional[float] = None):
    if not cmp or not pv:
        return

    now_ist = _now(sim_ts)   # cc#218: sim_ts=None => naive datetime.now(IST); gate logic identical
    if not guards.in_entry_window(now_ist):   # cc#217 P2: was inline 09:15-15:20 block
        log.debug(f"auto_paper {sym}: skipped -- outside market hours {now_ist.strftime('%H:%M')} IST")
        return

    paper_side = _PAPER_SIDE_MAP.get(side, "LONG")
    pp, r1, s1 = pv["pp"], pv["r1"], pv["s1"]

    # cc#217 P2: shared blackout + same-side-open + traded-today (generic) + conflict policy
    if not _entry_guards(conn, sym, paper_side, basket, d, cmp, sim_ts=sim_ts):
        return

    try:
        buy_slots, sell_slots = _mood_slots(gate_fails)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT side, COUNT(*) FROM v8_paper_positions
                WHERE status='OPEN' AND basket NOT IN ('sell_overbought','buy_s1_bounce')
                GROUP BY side
            """)   # cc#216: both dedicated pools (SO + S1B) are ring-fenced — was excluding
                   # only sell_overbought, so buy_s1_bounce positions inflated standard slots
            counts = {r[0]: int(r[1]) for r in cur.fetchall()}
        long_open  = counts.get("LONG",  0)
        short_open = counts.get("SHORT", 0)
        if paper_side == "LONG"  and long_open  >= buy_slots:
            log.info(f"auto_paper {sym}: slot_full LONG ({long_open}/{buy_slots})")
            _record_slot_block("LONG", sym, long_open, buy_slots); return   # cc#256
        if paper_side == "SHORT" and short_open >= sell_slots:
            log.info(f"auto_paper {sym}: slot_full SHORT ({short_open}/{sell_slots})")
            _record_slot_block("SHORT", sym, short_open, sell_slots); return   # cc#256
    except Exception as e:
        log.warning(f"auto_paper slot check {sym}: {e}"); return

    entry = round(cmp, 2)
    if target is not None and stop is not None:
        # cc#378: caller-supplied FROZEN levels (sell_reversal V5-D: S1/S2-dynamic target + 1:1
        # mirror stop, computed in the dedicated handler) — used verbatim, no basket recompute.
        target = round(target, 2)
        stop   = round(stop, 2)
    elif basket == "buy_momentum":
        # cc#359 V2 (spec id=2834): fixed +/-3.0% 1:1, frozen at entry (replaces R1/mirror).
        target = round(entry * 1.03, 2)
        stop   = round(entry * 0.97, 2)
    elif basket == "sell_momentum":
        # cc#380 V3 (spec id=2901): fixed -/+3.0% 1:1 SHORT (target below, stop above); replaces V2 pivot.
        target = round(entry * 0.97, 2)
        stop   = round(entry * 1.03, 2)
    elif paper_side == "LONG":
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

    entry_ts_ist = _now_ist(sim_ts)   # cc#218

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
                          pv: Optional[dict], d: date, gate_fails: int, sim_ts=None):
    if not cmp or not pv:
        return

    now_ist = _now(sim_ts)   # cc#218
    if not guards.in_entry_window(now_ist):   # cc#217 P2
        return

    # cc#217 P2: shared blackout + same-side-open + traded-today (SO pool) + conflict policy
    if not _entry_guards(conn, sym, "SHORT", "sell_overbought", d, cmp, sim_ts=sim_ts, basket_scoped=True):
        return

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
            _record_slot_block("SO", sym, so_open, so_cap)   # cc#256
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

    entry_ts_ist = _now_ist(sim_ts)   # cc#218
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

def _auto_paper_entry_s1b(conn, sym: str, cmp: Optional[float], d: date, gate_fails: int, sim_ts=None):
    """Fixed +1.5% target / -1.5% stop. Dedicated ring-fenced slots 3/3/3/2."""
    if not cmp:
        return

    now_ist = _now(sim_ts)   # cc#218
    if not guards.in_entry_window(now_ist):   # cc#217 P2
        return

    # cc#217 P2: shared blackout + same-side-open + traded-today (S1B pool) + conflict policy
    if not _entry_guards(conn, sym, "LONG", "buy_s1_bounce", d, cmp, sim_ts=sim_ts, basket_scoped=True):
        return

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
            _record_slot_block("S1B", sym, s1b_open, s1b_cap)   # cc#256
            return
    except Exception as e:
        log.warning(f"auto_paper_s1b slot check {sym}: {e}"); return

    entry  = round(cmp, 2)
    target = round(entry * 1.020, 2)   # cc#358 V2: +2.0% (was +1.5%)
    stop   = round(entry * 0.980, 2)   # cc#358 V2: -2.0% (was -1.5%)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT lot_size FROM futures_universe WHERE symbol=%s", (sym,))
            r = cur.fetchone()
            qty = int(r[0]) if r and r[0] else 1
    except Exception:
        qty = 1

    entry_ts_ist = _now_ist(sim_ts)   # cc#218
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
                     f"tgt={target}(+2.0%) sl={stop}(-2.0%) "
                     f"slots={s1b_open+1}/{s1b_cap} ts={entry_ts_ist.strftime('%H:%M')} IST")
    except Exception as e:
        log.warning(f"auto_paper_s1b insert {sym}: {e}")


# -- Step 8: Write v8_qualified + funnel --------------------------------------

# -- BUY_REVERSAL_V3 (inverse-sandwich dip-buy, canonical spec id=2818) --------

def _true_weekly_rsi(conn, symbol: str, live_cmp: Optional[float], sim_ts=None) -> Optional[float]:
    """cc#354: TRUE calendar-weekly Wilder RSI-14 for the buy_reversal V3 basket ONLY.
    Resamples raw_prices daily closes to week-end (W, Mon-Sun) last-close, then sets the
    CURRENT (partial) week's running close to the live CMP — filters define the dip, the live
    tick catches the turn. Computed BASKET-LOCALLY; it must NEVER read/write the shared synthetic
    rsi_weekly column (cc#353 audit: that column is a 5-day-stride approximation, ~16pt off)."""
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT price_date, close FROM raw_prices
                           WHERE symbol=%s AND price_date < %s
                             AND price_date >= (%s::date - INTERVAL '800 days')
                           ORDER BY price_date""",
                        (symbol, _today(sim_ts), _today(sim_ts)))
            rows = cur.fetchall()
        if len(rows) < 90:
            return None
        s  = pd.Series([float(c) for _, c in rows],
                       index=pd.to_datetime([d for d, _ in rows]))
        wk = s.resample("W").last().dropna()   # W = week ending Sunday (Mon-Sun buckets)
        if len(wk) < 15:
            return None
        if live_cmp:
            # set THIS week's running close to the live tick. Overwrites the current-week bucket
            # if history already has partial-week closes, else starts it (e.g. a Monday tick) —
            # never clobbers last week's completed bar (the naive iloc[-1] overwrite would).
            today_ts   = pd.Timestamp(_today(sim_ts)).normalize()
            cur_sunday = today_ts + pd.Timedelta(days=(6 - today_ts.weekday()) % 7)
            wk.loc[cur_sunday] = float(live_cmp)
            wk = wk.sort_index()
        return _wilder_rsi(wk, 14)
    except Exception as e:
        log.warning(f"_true_weekly_rsi {symbol}: {e}")
        return None


def _write_buy_reversal_v3_qualified(conn, all_metrics: List[dict], target_date: date,
                                     gate_fails: int, pivots: dict, signal_ts_ist, sim_ts=None):
    """cc#354 BUY_REVERSAL_V3 — TRUE REVERSAL inverse-sandwich dip-buy (spec id=2818, supersedes
    the archived V2 id=355). buy_reversal is REMOVED from the standard score-gate + dynamic-regime
    loop; this is a strict-AND of 8 conditions with no regime overrides and no R2 anywhere:
      (a) daily_rsi <= 40           (cold short-term dip)
      (b) true_weekly_rsi >= 60     (strong larger frame — TRUE calendar weekly, cc#353)
      (c) dma_200 >= 0              (long-term uptrend)
      (d) gvm_score >= 6.5          (quality)
      (e) mom_2d in [0, 3]          (2-day momentum turned up — dip has started recovering)
      (f) hourly_pct in [0.1, 1.0]  (fyers_fut hourly turning up, not spiking; cc#355: partial-window
                                     from 09:20, ENFORCED — only the 09:15-09:20 single-bar window is exempt)
      (g) CMP > PP                  (holding above pivot)
      (h) room-to-R1 > 2%          (QUALIFICATION gate — no room, no signal; never fall back to R2)
    Entry = standard _auto_paper_entry (live CMP, target R1, 1:1 mirror stop, standard slot pool)."""
    basket, side = "buy_reversal", "BUY"
    # cc#355: v3 hourly — enforced 0.1-1.0 from the first settled fut bar (09:20); partial window
    # (last/earliest-of-day) until the 12-bar rolling value exists (~10:15). Only the 09:15-09:20
    # window (0 settled bars) is exempt. Batched once for the whole universe.
    hourly_v3 = _load_hourly_fut_v3(conn, [s["symbol"] for s in all_metrics], sim_ts=sim_ts)
    # cc#357: base universe = symbols with a live CMP + valid pivots; annotate room + v3 hourly once.
    base = []
    for s in all_metrics:
        cmp = s.get("_cmp")
        pv  = pivots.get(s["symbol"])
        if not cmp or not pv:
            continue
        pp, r1 = pv.get("pp"), pv.get("r1")
        if pp is None or r1 is None:
            continue
        s["_pp"] = pp
        s["_room_r1_pct"] = round((r1 - cmp) / cmp * 100.0, 3)
        hourly, n_bars = hourly_v3.get(s["symbol"], (None, 0))
        s["_hourly_v3"] = round(hourly, 3) if hourly is not None else None
        # (f) exempt ONLY in the 09:15-09:20 single-bar window (n_bars==0); else ENFORCE 0.1-1.0
        s["_hourly_ok"] = (n_bars == 0) or (hourly is not None and 0.1 <= hourly <= 1.0)
        base.append(s)

    # cc#364: INDEPENDENT per-filter pass counts across `base` (buy_momentum convention) — each of
    # the 7 cheap gates counted ALONE over the whole base, NOT cumulative survivors. true_weekly_rsi
    # (stage 8, DB-heavy) is counted only over the strict-intersection stage-7 survivors below.
    # Final = strict-AND of all 8 (unchanged). Counts feed v8_funnel_counts / br_funnel_detail.
    funnel = {"_universe": len(base)}
    funnel["daily_rsi"]  = sum(1 for s in base if _passes(s.get("daily_rsi"), None, 40.0))   # (a)
    funnel["dma_200"]    = sum(1 for s in base if _passes(s.get("dma_200"), 0.0, None))       # (c)
    funnel["gvm_score"]  = sum(1 for s in base if _passes(s.get("gvm_score"), 6.5, None))     # (d)
    funnel["mom_2d"]     = sum(1 for s in base if _passes(s.get("mom_2d"), 0.0, 3.0))         # (e)
    funnel["hourly_pct"] = sum(1 for s in base if s["_hourly_ok"])                            # (f)
    funnel["cmp_gt_pp"]  = sum(1 for s in base if s["_cmp"] > s["_pp"])                       # (g)
    funnel["room_r1"]    = sum(1 for s in base if s["_room_r1_pct"] > 2.0)                     # (h)
    # strict intersection of the 7 cheap gates — only these reach the heavy stage-8 wRSI read
    # (identical set to the old cumulative pre-filter, so qualification is byte-for-byte unchanged).
    surv = [s for s in base
            if _passes(s.get("daily_rsi"), None, 40.0)
            and _passes(s.get("dma_200"), 0.0, None)
            and _passes(s.get("gvm_score"), 6.5, None)
            and _passes(s.get("mom_2d"), 0.0, 3.0)
            and s["_hourly_ok"]
            and s["_cmp"] > s["_pp"]
            and s["_room_r1_pct"] > 2.0]
    funnel["_stage7_survivors"] = len(surv)   # denominator for the stage-8 true_weekly_rsi row
    log.info(f"buy_reversal_v3: {len(surv)} after 7-condition cheap pre-filter")

    qualified = []
    for s in surv:
        twr = _true_weekly_rsi(conn, s["symbol"], s.get("_cmp"), sim_ts=sim_ts)
        s["_true_weekly_rsi"] = round(twr, 2) if twr is not None else None
        if twr is not None and twr >= 60.0:               # 8 (b)
            qualified.append(s)
    funnel["true_weekly_rsi"] = len(qualified)
    funnel["_score_qualified"] = len(qualified)
    log.info(f"buy_reversal_v3: {len(qualified)} qualified (true_weekly_rsi>=60) [spec id=2818]")

    # cc#357: persist the 8-stage funnel so /funnel_detail/buy_reversal (br_funnel_detail) can render it.
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
        log.warning(f"buy_reversal_v3 funnel: {e}")

    for s in qualified:
        sym  = s["symbol"]
        snap = {
            "true_weekly_rsi": s.get("_true_weekly_rsi"),   # step_5 audit fields
            "daily_rsi":       s.get("daily_rsi"),
            "mom_2d":          s.get("mom_2d"),
            "hourly_pct":      s.get("_hourly_v3"),   # cc#355: enforced v3 hourly (partial from 09:20)
            "room_r1_pct":     s.get("_room_r1_pct"),
            "gvm_score":       s.get("gvm_score"),
            "dma_200":         s.get("dma_200"),
            "filter_score": 8, "filter_total": 8,
            "spec": "BUY_REVERSAL_V3_INVERSE_SANDWICH id=2818",
        }
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO v8_qualified
                    (symbol, basket, signal_date, signal_ts, gvm_score, cmp,
                     mom_2d, week_return, month_return, dma_200, dma_50,
                     rsi_month, rsi_weekly, sector_week, sector_day,
                     month_index, week_index_52, daily_rsi,
                     metrics, source)
                    VALUES (%s,'buy_reversal',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'live_5min')
                    ON CONFLICT (symbol, basket, signal_date) DO NOTHING
                """, (
                    sym, target_date, signal_ts_ist,
                    s.get("gvm_score"), s.get("_cmp"),
                    s.get("mom_2d"), s.get("week_return"), s.get("month_return"),
                    s.get("dma_200"), s.get("dma_50"),
                    s.get("rsi_month"), s.get("rsi_weekly"),
                    s.get("sector_week"), s.get("sector_day"),
                    s.get("month_index"), s.get("week_index_52"),
                    s.get("daily_rsi"), json.dumps(snap),
                ))
            conn.commit()
            _auto_paper_entry(conn, sym, basket, side, s.get("_cmp"), pivots.get(sym),
                              target_date, gate_fails, sim_ts=sim_ts)
        except Exception as e:
            log.warning(f"buy_reversal_v3 insert {sym}: {e}")


def _write_sell_reversal_v5d_qualified(conn, all_metrics: List[dict], target_date: date,
                                       gate_fails: int, pivots: dict, signal_ts_ist, sim_ts=None):
    """cc#378 SELL_REVERSAL_V5D (spec id=2894, supersedes archived V4 id=357). The SELL mirror of
    BUY_REVERSAL_V3: sell_reversal is REMOVED from the standard score-gate loop; strict-AND of 6
    conditions, RAW — NO market gate, NO auto kill-switch (founder-locked 11-Jul):
      (a) daily_rsi   >= 60         (hot bounce)
      (b) true_weekly_rsi <= 40     (weak larger frame — TRUE calendar weekly, basket-local; cc#353)
      (c) rsi_month   < 50          (weak monthly)
      (d) dma_200     <= 0          (below the 200-DMA)
      (e) CMP < PP                  (below the rolling-5d pivot)
      (f) room gate: target (S1 if (cmp-S1)/cmp >= 2% else S2, never beyond S2) >= 2% away, else NO signal.
    Target = that S1-or-S2 level; Stop = 1:1 mirror = entry + (entry - target). Entry = _auto_paper_entry
    with the handler-computed FROZEN levels, standard SELL slot pool + all standard guards."""
    basket, side = "sell_reversal", "SELL"
    # cc#378: base universe = symbols with a live CMP + valid pivots; annotate dynamic target + room once.
    base = []
    for s in all_metrics:
        cmp = s.get("_cmp")
        pv  = pivots.get(s["symbol"])
        if not cmp or not pv:
            continue
        pp, s1, s2 = pv.get("pp"), pv.get("s1"), pv.get("s2")
        if pp is None or s1 is None:
            continue
        # (f) dynamic target: S1 if >=2% below entry, else S2 (needs s2); chosen target must be >=2%.
        room_s1 = round((cmp - s1) / cmp * 100.0, 3)
        room_s2 = round((cmp - s2) / cmp * 100.0, 3) if s2 is not None else None
        if room_s1 >= 2.0:
            tgt, room = s1, room_s1
        elif room_s2 is not None and room_s2 >= 2.0:
            tgt, room = s2, room_s2
        else:
            tgt, room = None, None      # room gate fails -> no signal
        s["_pp"] = pp
        s["_sr_target"] = tgt
        s["_sr_room_pct"] = room
        s["_sr_room_ok"] = tgt is not None
        base.append(s)

    # cc#378: INDEPENDENT per-filter pass counts across `base` (buy_momentum convention, cc#364 style).
    # true_weekly_rsi (heavy) is counted only over the strict-intersection of the 5 cheap gates.
    def _rm_lt50(s):   # rsi_month < 50 (STRICT — _passes is inclusive, so handle it directly)
        v = s.get("rsi_month")
        return v is not None and float(v) < 50.0
    funnel = {"_universe": len(base)}
    funnel["daily_rsi"] = sum(1 for s in base if _passes(s.get("daily_rsi"), 60.0, None))   # (a)
    funnel["rsi_month"] = sum(1 for s in base if _rm_lt50(s))                                # (c)
    funnel["dma_200"]   = sum(1 for s in base if _passes(s.get("dma_200"), None, 0.0))       # (d)
    funnel["cmp_lt_pp"] = sum(1 for s in base if s["_cmp"] < s["_pp"])                        # (e)
    funnel["room"]      = sum(1 for s in base if s["_sr_room_ok"])                            # (f)
    surv = [s for s in base
            if _passes(s.get("daily_rsi"), 60.0, None)
            and _rm_lt50(s)
            and _passes(s.get("dma_200"), None, 0.0)
            and s["_cmp"] < s["_pp"]
            and s["_sr_room_ok"]]
    funnel["_stage5_survivors"] = len(surv)   # denominator for the stage-6 true_weekly_rsi row
    log.info(f"sell_reversal_v5d: {len(surv)} after 5-condition cheap pre-filter")

    qualified = []
    for s in surv:
        twr = _true_weekly_rsi(conn, s["symbol"], s.get("_cmp"), sim_ts=sim_ts)
        s["_true_weekly_rsi"] = round(twr, 2) if twr is not None else None
        if twr is not None and twr <= 40.0:               # (b)
            qualified.append(s)
    funnel["true_weekly_rsi"] = len(qualified)
    funnel["_score_qualified"] = len(qualified)
    log.info(f"sell_reversal_v5d: {len(qualified)} qualified (true_weekly_rsi<=40) [spec id=2894]")

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
        log.warning(f"sell_reversal_v5d funnel: {e}")

    for s in qualified:
        sym   = s["symbol"]
        cmp   = s.get("_cmp")
        entry = round(cmp, 2)
        tgt   = round(s["_sr_target"], 2)
        stop  = round(entry + (entry - tgt), 2)   # 1:1 mirror ABOVE entry (SELL)
        snap = {
            "true_weekly_rsi": s.get("_true_weekly_rsi"),
            "daily_rsi":       s.get("daily_rsi"),
            "rsi_month":       s.get("rsi_month"),
            "dma_200":         s.get("dma_200"),
            "room_pct":        s.get("_sr_room_pct"),
            "target":          tgt,
            "stop":            stop,
            "filter_score": 6, "filter_total": 6,
            "spec": "SELL_REVERSAL_V5D id=2894",
        }
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO v8_qualified
                    (symbol, basket, signal_date, signal_ts, gvm_score, cmp,
                     mom_2d, week_return, month_return, dma_200, dma_50,
                     rsi_month, rsi_weekly, sector_week, sector_day,
                     month_index, week_index_52, daily_rsi,
                     metrics, source)
                    VALUES (%s,'sell_reversal',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'live_5min')
                    ON CONFLICT (symbol, basket, signal_date) DO NOTHING
                """, (
                    sym, target_date, signal_ts_ist,
                    s.get("gvm_score"), cmp,
                    s.get("mom_2d"), s.get("week_return"), s.get("month_return"),
                    s.get("dma_200"), s.get("dma_50"),
                    s.get("rsi_month"), s.get("rsi_weekly"),
                    s.get("sector_week"), s.get("sector_day"),
                    s.get("month_index"), s.get("week_index_52"),
                    s.get("daily_rsi"), json.dumps(snap),
                ))
            conn.commit()
            # cc#378: entry with the handler-computed FROZEN S1/S2 target + 1:1 mirror stop.
            _auto_paper_entry(conn, sym, basket, side, cmp, pivots.get(sym),
                              target_date, gate_fails, sim_ts=sim_ts, target=tgt, stop=stop)
        except Exception as e:
            log.warning(f"sell_reversal_v5d insert {sym}: {e}")


def _write_sell_momentum_v3_qualified(conn, all_metrics: List[dict], target_date: date,
                                      gate_fails: int, pivots: dict, signal_ts_ist, sim_ts=None):
    """cc#380 SELL_MOMENTUM_V3 (N5, spec id=2901, supersedes V2). Dedicated strict-AND handler —
    sell_momentum is REMOVED from the standard score-gate loop. Strict AND of 9:
      (1) true_weekly_rsi <= 45     (TRUE calendar weekly — basket-local, never shared rsi_weekly)
      (2) rsi_month       < 40      (deeply weak monthly)
      (3) mom_2d in [-4, -1]        (recent down-momentum, not a crash)
      (4) dma_200         <= +2     (below / near the 200-DMA)
      (5) week_return in [-10, -0.5](weak week, not capitulation)
      (6) sector_week     < 0       (weak sector)
      (7) CMP < PP                  (below the rolling-5d pivot)
      (8) week_index_52 in [20, 60] (mid 52-week band)
      (9) S2-clearance (CMP-S2)/CMP >= 3%  (support sits below the 3% target so it can't block the fall)
    Exits FIXED +/-3.0% (true 1:1) via _auto_paper_entry's sell_momentum branch; max hold 15 trading
    days; standard SELL slot pool + all guards. Independent per-filter funnel counts (cc#364 style)."""
    basket, side = "sell_momentum", "SELL"
    base = []
    for s in all_metrics:
        cmp = s.get("_cmp")
        pv  = pivots.get(s["symbol"])
        if not cmp or not pv:
            continue
        pp, s2 = pv.get("pp"), pv.get("s2")
        if pp is None:
            continue
        s["_pp"] = pp
        s["_s2c_pct"] = round((cmp - s2) / cmp * 100.0, 3) if (s2 is not None and cmp) else None
        base.append(s)

    def _rm_lt40(s):       # rsi_month < 40 (STRICT)
        v = s.get("rsi_month")
        return v is not None and float(v) < 40.0
    def _sw_lt0(s):        # sector_week < 0 (STRICT)
        v = s.get("sector_week")
        return v is not None and float(v) < 0.0
    def _s2c_ok(s):        # (CMP-S2)/CMP >= 3%  (fails if no s2)
        return s["_s2c_pct"] is not None and s["_s2c_pct"] >= 3.0

    # cc#380: INDEPENDENT per-filter pass counts across `base` (cc#364 convention). true_weekly_rsi
    # (heavy) is counted only over the strict-intersection of the 8 cheap gates.
    funnel = {"_universe": len(base)}
    funnel["rsi_month"]     = sum(1 for s in base if _rm_lt40(s))                              # (2)
    funnel["mom_2d"]        = sum(1 for s in base if _passes(s.get("mom_2d"), -4.0, -1.0))     # (3)
    funnel["dma_200"]       = sum(1 for s in base if _passes(s.get("dma_200"), None, 2.0))     # (4)
    funnel["week_return"]   = sum(1 for s in base if _passes(s.get("week_return"), -10.0, -0.5))  # (5)
    funnel["sector_week"]   = sum(1 for s in base if _sw_lt0(s))                               # (6)
    funnel["week_index_52"] = sum(1 for s in base if _passes(s.get("week_index_52"), 20.0, 60.0))  # (8)
    funnel["cmp_lt_pp"]     = sum(1 for s in base if s["_cmp"] < s["_pp"])                     # (7)
    funnel["s2_clearance"]  = sum(1 for s in base if _s2c_ok(s))                               # (9)
    surv = [s for s in base
            if _rm_lt40(s)
            and _passes(s.get("mom_2d"), -4.0, -1.0)
            and _passes(s.get("dma_200"), None, 2.0)
            and _passes(s.get("week_return"), -10.0, -0.5)
            and _sw_lt0(s)
            and _passes(s.get("week_index_52"), 20.0, 60.0)
            and s["_cmp"] < s["_pp"]
            and _s2c_ok(s)]
    funnel["_stage8_survivors"] = len(surv)   # denominator for the stage-9 true_weekly_rsi row
    log.info(f"sell_momentum_v3: {len(surv)} after 8-condition cheap pre-filter")

    qualified = []
    for s in surv:
        twr = _true_weekly_rsi(conn, s["symbol"], s.get("_cmp"), sim_ts=sim_ts)
        s["_true_weekly_rsi"] = round(twr, 2) if twr is not None else None
        if twr is not None and twr <= 45.0:               # (1)
            qualified.append(s)
    funnel["true_weekly_rsi"] = len(qualified)
    funnel["_score_qualified"] = len(qualified)
    log.info(f"sell_momentum_v3: {len(qualified)} qualified (true_weekly_rsi<=45) [spec id=2901]")

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
        log.warning(f"sell_momentum_v3 funnel: {e}")

    for s in qualified:
        sym = s["symbol"]
        cmp = s.get("_cmp")
        snap = {
            "true_weekly_rsi": s.get("_true_weekly_rsi"),
            "rsi_month":       s.get("rsi_month"),
            "mom_2d":          s.get("mom_2d"),
            "dma_200":         s.get("dma_200"),
            "week_return":     s.get("week_return"),
            "sector_week":     s.get("sector_week"),
            "week_index_52":   s.get("week_index_52"),
            "s2_clearance_pct": s.get("_s2c_pct"),
            "target": round(round(cmp, 2) * 0.97, 2), "stop": round(round(cmp, 2) * 1.03, 2),
            "filter_score": 9, "filter_total": 9,
            "spec": "SELL_MOMENTUM_V3_N5 id=2901",
        }
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO v8_qualified
                    (symbol, basket, signal_date, signal_ts, gvm_score, cmp,
                     mom_2d, week_return, month_return, dma_200, dma_50,
                     rsi_month, rsi_weekly, sector_week, sector_day,
                     month_index, week_index_52, daily_rsi,
                     metrics, source)
                    VALUES (%s,'sell_momentum',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'live_5min')
                    ON CONFLICT (symbol, basket, signal_date) DO NOTHING
                """, (
                    sym, target_date, signal_ts_ist,
                    s.get("gvm_score"), cmp,
                    s.get("mom_2d"), s.get("week_return"), s.get("month_return"),
                    s.get("dma_200"), s.get("dma_50"),
                    s.get("rsi_month"), s.get("rsi_weekly"),
                    s.get("sector_week"), s.get("sector_day"),
                    s.get("month_index"), s.get("week_index_52"),
                    s.get("daily_rsi"), json.dumps(snap),
                ))
            conn.commit()
            # cc#380: entry uses _auto_paper_entry's sell_momentum branch (fixed -/+3.0%).
            _auto_paper_entry(conn, sym, basket, side, cmp, pivots.get(sym),
                              target_date, gate_fails, sim_ts=sim_ts)
        except Exception as e:
            log.warning(f"sell_momentum_v3 insert {sym}: {e}")


def _write_qualified(conn, all_metrics: List[dict], target_date: date, sim_ts=None, v21_backtest=False):
    from v8_endpoints import FILTER_CONFIG, v21_hard_gate_pass

    gate_fails = _market_gate_fails(conn, sim_ts=sim_ts)
    pivots     = _load_pivots(conn, sim_ts=sim_ts)
    # cc#324: point-in-time V2.1 enable state in sim (was reading TODAY's state during replays).
    enabled_v21 = _load_filter_state(conn, sim_ts=sim_ts)

    signal_ts_ist = _now_ist(sim_ts)   # cc#218
    _reset_slot_blocks()   # cc#256: fresh per-tick slot_full accumulator

    for basket, filters in FILTER_CONFIG.items():
        # cc#354: buy_reversal left the standard score-gate + dynamic-regime loop entirely —
        # it now runs as BUY_REVERSAL_V3 via its own dedicated strict-AND handler (spec id=2818).
        # cc#378: sell_reversal likewise left the loop — SELL_REVERSAL_V5D dedicated handler (id=2894).
        # cc#380: sell_momentum left the loop too — SELL_MOMENTUM_V3 dedicated handler (id=2901).
        if basket in ("sell_overbought", "buy_s1_bounce", "buy_reversal", "sell_reversal", "sell_momentum"):
            continue

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

        log.info(f"{basket}: score-gate need={need}/{n_filters} "
                 f"gate_fails={gate_fails} -> {len(universe)} score-qualified")

        # cc#158: V2.1 hard-gate layer — applied AFTER the score-gate, never
        # counted into the threshold. Disabled basket -> no-op (locked behavior).
        v21_on = enabled_v21.get(basket, False)
        if v21_on:
            before = len(universe)
            universe = [s for s in universe if v21_hard_gate_pass(basket, s, True, backtest=v21_backtest)]
            log.info(f"{basket}: V2.1 hard-gate ON ({'backtest' if v21_backtest else 'live/parity'})"
                     f" -> {len(universe)}/{before} pass")

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
                "week_index_52", "ma9_vs_ma21", "vol_ratio",
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
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO v8_qualified
                        (symbol, basket, signal_date, signal_ts, gvm_score, cmp,
                         mom_2d, week_return, month_return, dma_200, dma_50,
                         rsi_month, rsi_weekly, sector_week, sector_day,
                         month_index, week_index_52, daily_rsi,
                         metrics, source)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                        s.get("daily_rsi"),
                        json.dumps(snap), "live_5min",
                    ))
                    first_qualification = cur.rowcount > 0   # v8_qualified daily latch (display)
                conn.commit()
                # cc#254: attempt entry on EVERY tick the symbol is in `universe` (i.e. it passes
                # the live score-gate + V2.1 + pivot-room right NOW), not only its FIRST
                # qualification of the day — so a symbol blocked purely by slot_full earlier enters
                # when a slot frees later the same day. _auto_paper_entry re-checks entry window,
                # blackout, same-side-open, traded_today and slot live, so re-calling is idempotent
                # (already-open / already-traded-today symbols are skipped).
                _auto_paper_entry(conn, sym, basket, side,
                                  s.get("_cmp"), pivots.get(sym),
                                  target_date, gate_fails, sim_ts=sim_ts)
            except Exception as e:
                log.warning(f"qualified insert {basket} {sym}: {e}")

    _write_buy_reversal_v3_qualified(conn, all_metrics, target_date,
                                     gate_fails, pivots, signal_ts_ist, sim_ts=sim_ts)
    _write_sell_reversal_v5d_qualified(conn, all_metrics, target_date,
                                       gate_fails, pivots, signal_ts_ist, sim_ts=sim_ts)
    _write_sell_momentum_v3_qualified(conn, all_metrics, target_date,
                                      gate_fails, pivots, signal_ts_ist, sim_ts=sim_ts)
    _write_buy_s1_bounce_qualified(conn, all_metrics, target_date,
                                    gate_fails, pivots, signal_ts_ist, enabled_v21,
                                    sim_ts=sim_ts, v21_backtest=v21_backtest)
    _write_sell_overbought_qualified(conn, all_metrics, target_date,
                                      gate_fails, pivots, signal_ts_ist, enabled_v21,
                                      sim_ts=sim_ts, v21_backtest=v21_backtest)

    # cc#256: tick complete — flush a slot_full_burst alert if any pool piled up. Live only
    # (sim_ts is None); replay/bt7 ticks accumulate harmlessly but never emit alerts.
    if sim_ts is None:
        _flush_slot_blocks(conn, signal_ts_ist)


def _write_buy_s1_bounce_qualified(conn, all_metrics: List[dict], target_date: date,
                                    gate_fails: int, pivots: dict, signal_ts_ist,
                                    enabled_v21: Optional[dict] = None, sim_ts=None,
                                    v21_backtest=False):
    """
    Buy S1 Bounce V1 (17-Jun-2026). 8 strict filters (nifty_rsi gate + gvm>=7 + 6 stages).
    Dedicated ring-fenced slots 3/3/3/2. Backtest: 88 sigs/yr, 73.9% WR (predates gvm>=7
    gate — cc#234; live-with-gvm>=7 is tighter, Claude web re-backtests to confirm WR).
    cc#158: V2.1 hard gate (hourly_pct >0..1.0, week_index_52 50..90) layered
    as extra strict-AND conditions when enabled (spec id=1265).
    """
    from v8_endpoints import v21_hard_gate_pass
    enabled_v21 = enabled_v21 or {}
    s1b_on = enabled_v21.get("buy_s1_bounce", False)
    nifty_rsi = _get_nifty_rsi(conn, sim_ts=sim_ts)
    if nifty_rsi is None or nifty_rsi < 55.0:
        log.debug(f"buy_s1_bounce: Nifty RSI={nifty_rsi} < 55 -- gated OFF")
        return

    s1b_cap = _s1b_slots(gate_fails)
    log.info(f"buy_s1_bounce: Nifty RSI={nifty_rsi:.1f} gated ON -- slots={s1b_cap}")

    candidates = [
        s for s in all_metrics
        # cc#234: gvm>=7.0 hard gate (filter #0). cc#76 added this to the /buy_s1_bounce
        # endpoint + funnel but NOT the writer, so the writer auto-entered paper for
        # gvm<7 stocks the qualified list then hid (ghost entries). Now writer==funnel==endpoint.
        if _passes(s.get("gvm_score"),    7.0, None)
        and _passes(s.get("week_return"),  0.0, 2.5)   # cc#358 V2: cap 2.5 (was 3.0)
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
        qualified = [s for s in qualified if v21_hard_gate_pass("buy_s1_bounce", s, True, backtest=v21_backtest)]
        log.info(f"buy_s1_bounce: V2.1 hard-gate ON -> {len(qualified)}/{before} pass")

    for s in qualified:
        sym  = s["symbol"]
        snap = {
            "week_return": s.get("week_return"), "dma_50": s.get("dma_50"),
            "vol_ratio": s.get("vol_ratio"), "recovery_2d": s.get("recovery_2d"),
            "day_ret": s.get("day_ret"), "week_low": s.get("week_low"),
            "nifty_rsi": round(nifty_rsi, 1), "filter_score": 8, "filter_total": 8,
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
                     month_index, week_index_52, daily_rsi,
                     metrics, source)
                    VALUES
                    (%s,'buy_s1_bounce',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, basket, signal_date) DO NOTHING
                """, (
                    sym, target_date, signal_ts_ist,
                    s.get("gvm_score"), s.get("_cmp"),
                    s.get("mom_2d"), s.get("week_return"), s.get("month_return"),
                    s.get("dma_200"), s.get("dma_50"),
                    s.get("rsi_month"), s.get("rsi_weekly"),
                    s.get("sector_week"), s.get("sector_day"),
                    s.get("month_index"), s.get("week_index_52"),
                    s.get("daily_rsi"),
                    json.dumps(snap), "live_5min",
                ))
                first_qual = cur.rowcount > 0   # daily latch (display)
            conn.commit()
            # cc#254: retry entry every tick it still passes the s1b strict gate, not just first
            # qualification — _auto_paper_entry_s1b re-checks window/guards/slot live (idempotent).
            _auto_paper_entry_s1b(conn, sym, s.get("_cmp"), target_date, gate_fails, sim_ts=sim_ts)
        except Exception as e:
            log.warning(f"buy_s1_bounce insert {sym}: {e}")


def _write_sell_overbought_qualified(conn, all_metrics: List[dict], target_date: date,
                                      gate_fails: int, pivots: dict, signal_ts_ist,
                                      enabled_v21: Optional[dict] = None, sim_ts=None,
                                      v21_backtest=False):
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
        if so_v21_on and not v21_hard_gate_pass("sell_overbought", s, True, backtest=v21_backtest):
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
                     month_index, week_index_52, daily_rsi,
                     metrics, source)
                    VALUES (%s,'sell_overbought',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, basket, signal_date) DO NOTHING
                """, (
                    sym, target_date, signal_ts_ist,
                    s.get("gvm_score"), s.get("_cmp"),
                    s.get("mom_2d"), s.get("week_return"), s.get("month_return"),
                    s.get("dma_200"), s.get("dma_50"),
                    s.get("rsi_month"), s.get("rsi_weekly"),
                    s.get("sector_week"), s.get("sector_day"),
                    s.get("month_index"), s.get("week_index_52"),
                    s.get("daily_rsi"),
                    json.dumps(snap), "live_5min",
                ))
                first_qualification = cur.rowcount > 0   # daily latch (display)
            conn.commit()

            # cc#254: retry entry every tick it still passes the SO gate, not just first
            # qualification — _auto_paper_entry_so re-checks window/guards/slot live (idempotent).
            pv = pivots.get(sym)
            _auto_paper_entry_so(conn, sym, s.get("_cmp"), pv,
                                 target_date, gate_fails, sim_ts=sim_ts)
        except Exception as e:
            log.warning(f"sell_overbought qualified insert {sym}: {e}")


# -- Main entry point ---------------------------------------------------------

def _write_heartbeat(conn, sim_ts=None):
    """Stamp app_config.sched_writer_hb on a successful tick so run_diagnosis sees
    the writer is alive. Uses the already-open conn (a 2nd psycopg3 connection in the
    scheduler thread fails silently — task #18). Covers scheduler + MCP + API paths.
    cc#218: heartbeat value routes through _now_ist(sim_ts) — sim_ts=None is live."""
    try:
        with conn.cursor() as _hb:
            _hb.execute(
                "INSERT INTO app_config(key,value) VALUES('sched_writer_hb',%s) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                (_now_ist(sim_ts).isoformat(),))
        conn.commit()
    except Exception as _hbe:
        log.warning(f"sched_writer_hb write failed: {_hbe}")


def run_live_signal_writer(conn, sim_ts=None, v21_backtest=False) -> dict:
    # cc#218: sim_ts=None => live (datetime.now(IST)); sim_ts set => frozen clock for the
    # BT7 harness. Every time read below routes through _now/_today(sim_ts) or is threaded
    # into the callee, so this whole tick is point-in-time when replaying a golden day.
    # cc#324: v21_backtest (BT7 BACKTEST mode) applies V2.1 as week_index_52 ONLY — the live-only
    # intraday refinements (hourly_pct, fall_from_day_high) are policy-skipped. Default False =
    # live + PARITY replays apply V2.1 exactly as live did (hourly included). Live never sets it.
    today = _today(sim_ts)

    # cc#211: HARD trading-day gate — the SINGLE write-layer choke point. Every caller
    # funnels through here (5-min tick, watchdog/forced-restart recovery tick, 09:10
    # stall-check recovery, MCP, API), and the restart/recovery paths BYPASS the
    # scheduler's market-hours wrapper — that is exactly how Sat 04-Jul wrote 212 junk
    # v8_metrics rows. Gating LINE 1 stops non-trading-day writes for ALL present and
    # future callers, without patching each one. No EOD fallback on weekends either.
    if not guards.is_trading_day(today):
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

    _t_tick = perf_counter()   # cc#217 P3: tick wall-time instrumentation
    eod_metrics = _load_eod_metrics(conn, sim_ts=sim_ts)
    eod_history = _load_eod_history(conn, symbols, sim_ts=sim_ts)
    _t_intr = perf_counter()
    intraday    = _load_intraday_bars(conn, symbols, sim_ts=sim_ts)
    _intr_ms = (perf_counter() - _t_intr) * 1000.0   # cc#217 P3: single-query load time
    cmp_map     = _load_cmp(conn, sim_ts=sim_ts)
    vol_cutoff  = _round_down_5min(_bar_cutoff(sim_ts))   # cc#218 D6: today's cum-vol only up to last CLOSED bar
    vol_tn_map  = _load_vol_ratio_time_normalized(conn, symbols, vol_cutoff, sim_ts=sim_ts)
    hourly_map  = _load_hourly_fut(conn, symbols, sim_ts=sim_ts)   # cc#158: fyers_fut 5m hourly

    # cc#259: bar-FRESHNESS gate. _load_intraday_bars returns MAX(ts) rows but had NO check that
    # the bar is RECENT — only that one EXISTS. When the fyers_feed bar writer froze at 14:00
    # (07-Jul), every later tick re-fetched the same 14:00 bar and recomputed plausible-looking
    # quals/entries off dead prices with healthy-looking tick_perf and zero alerts — worse than a
    # clean outage. A frozen-but-present bar is now treated the SAME as a missing bar: drop it,
    # and if a large fraction of symbols are stale, fail loud like cc#212 instead of computing.
    _now_tick = _now_ist(sim_ts)
    _STALE_BAR_MIN = 12                      # ~2 tick intervals + buffer (live tick = 5 min)
    _stale = {s: b for s, b in intraday.items()
              if b.get("bar_ts") is None
              or (_now_tick - b["bar_ts"]).total_seconds() > _STALE_BAR_MIN * 60}
    _n_had = len(intraday)
    for s in _stale:
        intraday.pop(s, None)                # frozen bar == missing bar (cc#259)
    if _stale and _n_had and len(_stale) >= 0.5 * _n_had:
        _newest = max((b["bar_ts"] for b in _stale.values() if b.get("bar_ts")), default=None)
        _age = round((_now_tick - _newest).total_seconds() / 60, 1) if _newest else None
        log.error(f"signal_writer: {len(_stale)}/{_n_had} intraday bars STALE (newest {_newest}, "
                  f"~{_age} min old) at tick {_now_tick:%H:%M} — fyers_feed bar writer frozen; "
                  f"SKIPPING (no quals, no paper entries)")
        _ops_log(conn, "alert", "writer_stale_intraday_bars",
                 {"message": "signal writer found a large fraction of intraday bars STALE "
                             "(frozen-not-missing) — fyers_feed bar writer likely frozen; signal "
                             "generation skipped to avoid recomputing quals/entries off dead prices",
                  "stale": len(_stale), "total": _n_had, "newest_bar_ts": str(_newest),
                  "stale_age_min": _age, "date": str(today)})
        _write_heartbeat(conn, sim_ts=sim_ts)
        return {"skipped": "stale_intraday_bars", "stale": len(_stale), "total": _n_had,
                "newest_bar_ts": str(_newest), "date": str(today)}

    if not intraday:
        # cc#212: FAIL LOUD — the old eod_fallback branch synthesized signals from frozen
        # EOD metrics + cmp_prices and ran _write_qualified (incl auto paper entries). But
        # when the feed is down cmp_prices is ALSO stale, so entries could fire at
        # yesterday's prices and poison the paper track record. Founder decision (05-Jul):
        # stop + alert on missing live data, never silently degrade to stale. cc#211's
        # line-1 gate guarantees we only reach here on a genuine trading day, so this alert
        # is a clean feed-outage signal. Recovery = the feed watchdogs + 09:10 stall-check.
        # Heartbeat is still written so the watchdog can see the writer itself is alive.
        log.error("signal_writer: no intraday bars on a trading day — feed down; SKIPPING "
                  "(no metrics upsert, no quals, no paper entries)")
        _ops_log(conn, "alert", "writer_no_intraday_bars",
                 {"message": "signal writer found zero intraday bars on a trading day — "
                             "fyers_feed likely down; signal generation skipped to avoid "
                             "stale-price paper entries",
                  "date": str(today)})
        _write_heartbeat(conn, sim_ts=sim_ts)
        return {"skipped": "no_intraday_bars", "date": str(today)}

    computed: Dict[str, dict] = {}
    no_bar = 0
    compute_err = 0
    err_sample = None
    for sym in symbols:
        bar  = intraday.get(sym)
        hist = eod_history.get(sym)
        if not bar or not hist or len(hist["closes"]) < 5:
            no_bar += 1
            continue
        # cc#230: per-symbol guard — one bad symbol must NOT crash the whole writer (the
        # 03-Jul outage: an unhandled exception here left v8_metrics dead for 3 days).
        try:
            eod = eod_metrics.get(sym, {})
            cmp = cmp_map.get(sym)
            m   = _compute_live_metrics(hist, bar, cmp, eod, vol_tn_map.get(sym))
            m["symbol"] = sym
            m["_cmp"]   = cmp if cmp else bar["close"]
            m["hourly_pct"] = hourly_map.get(sym)   # cc#158: fyers_fut 5m hourly
            computed[sym] = m
        except Exception as _ce:
            compute_err += 1
            if err_sample is None:
                err_sample = f"{sym}: {_ce!r}"
            if compute_err <= 3:
                log.error(f"compute_live_metrics {sym}: {_ce}", exc_info=True)
    if compute_err:
        log.error(f"signal_writer: {compute_err} symbols failed compute (sample {err_sample})")
        try:
            _ops_log(conn, "alert", "signal_writer_compute_errors",
                     {"count": compute_err, "sample": err_sample, "date": str(today)})
        except Exception:
            pass
        # cc#246: 100%-failure escalation — every attempted symbol failing means a
        # systemic bug (e.g. a NameError in _compute_live_metrics), not one bad symbol.
        # The per-symbol guard swallows these into warnings, so the writer looks alive
        # (heartbeat fine) while writing nothing. Page loudly instead of running dark.
        attempted = len(symbols) - no_bar
        if attempted > 0 and compute_err >= attempted:
            log.critical(
                f"signal_writer: TOTAL COMPUTE FAILURE — {compute_err}/{attempted} symbols "
                f"failed, 0 computed (systemic bug). sample={err_sample}")
            try:
                _ops_log(conn, "critical", "signal_writer_total_failure",
                         {"count": compute_err, "attempted": attempted,
                          "sample": err_sample, "date": str(today)})
            except Exception:
                pass

    _add_sector_aggregates(computed, eod_metrics)

    # cc#217 P3: one batched upsert + one commit for the whole tick (was 212 sequential
    # INSERT+COMMIT). _upsert_metrics_batch preserves the cc#218 skip-bad-symbol guarantee via
    # a batch savepoint + per-symbol fallback on batch failure.
    _t_upsert = perf_counter()
    _upsert_metrics_batch(conn, computed, today, sim_ts=sim_ts)
    _upsert_ms = (perf_counter() - _t_upsert) * 1000.0
    all_metrics = list(computed.values())

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

    _write_qualified(conn, all_metrics, today, sim_ts=sim_ts, v21_backtest=v21_backtest)

    _write_adr_intraday(conn, sim_ts=sim_ts)
    _update_sector_aggregates_sql(conn, today)

    log.info(f"signal_writer: {len(computed)} updated, {no_bar} no_bar, source=live_5min")
    _write_heartbeat(conn, sim_ts=sim_ts)
    _assert_no_nontrading_metrics(conn)   # cc#211: loud on any bypassed non-trading write

    # cc#217 P3: tick wall-time to ops_log — before/after numbers for the batch-upsert +
    # single-query-load win (biggest levers: upsert_ms was ~212 sequential commits).
    _tick_ms = (perf_counter() - _t_tick) * 1000.0
    _ops_log(conn, "info", "tick_perf",
             {"tick_ms": round(_tick_ms, 1), "upsert_ms": round(_upsert_ms, 1),
              "load_intraday_ms": round(_intr_ms, 1), "symbols": len(computed),
              "date": str(today)})
    log.info(f"signal_writer perf: tick={_tick_ms:.0f}ms upsert={_upsert_ms:.0f}ms "
             f"load_intraday={_intr_ms:.0f}ms symbols={len(computed)}")
    return {
        "date":    str(today),
        "updated": len(computed),
        "no_bar":  no_bar,
        "total":   len(symbols),
        "source":  "live_5min",
    }
