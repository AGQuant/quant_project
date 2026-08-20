"""
V8 Signal Writer -- Single Live Engine (v2.5.0, cc#502 V8 SUITE REBUILD 18-Jul-2026)
=====================================================================================
Unified 5-min engine. Replaces v8_live.py + old v8_signal_writer.py.

What it does every 5-min during market hours:
  1. Loads latest EOD v8_metrics row per symbol (slow metrics: GVM, RSI M/W, sector_week, sector_month)
  2. Reads intraday_prices (today's bars) per symbol
  3. Recomputes all live-moving metrics from intraday close spliced onto EOD history
  4. Only EOD-frozen metric: gvm_score (22:00 nightly). All others now live.
  5. Upserts v8_metrics (today's row) with live values
  6. Runs the four dedicated basket handlers -> writes v8_qualified (latch semantics)
  7. On first qualification: auto-logs paper trade in v8_paper_positions
  8. Writes v8_funnel_counts (independent per-filter pass counts, cc#364 convention)
  9. Writes adr_intraday (live ADR every 5-min tick)

cc#502 V8 SUITE REBUILD (18-Jul-2026, CLEAN CONSOLIDATED SPEC v2): Sell Overbought and Buy S1
Bounce are REMOVED entirely (handlers, auto-entry fns, ring-fenced slot pools, funnel rows).
All four remaining baskets are dedicated strict-AND handlers -- the generic FILTER_CONFIG
score-gate loop (_write_qualified's old per-basket scoring body, and _gate_threshold) is
retired; FILTER_CONFIG (v8_endpoints.py) now exists for display/endpoint parity only. Metric
convention: wRSI everywhere = _true_weekly_rsi() (calendar-weekly Wilder-14, current week
bucket = live CMP) -- NEVER the synthetic v8_metrics.rsi_weekly (5-day-stride approximation,
cc#353). Each handler runs its cheap gates first (independent per-filter funnel counts across
the universe, not cumulative survivors), then the heavy true_weekly_rsi stage only on the
strict-intersection survivors.

  BUY_MOMENTUM V4 FINAL (_write_buy_momentum_v3_qualified, spec 22386 on V3's 5650): HARD
    gates (dma_50 5-12, dma_20>0, week_index_52>=75, gvm_score>=7, day_1d>0, mom_2d 0-4 (the ONE
    V4 addition), hourly_pct>0 AND NOT NULL, FINAL heavy true_weekly_rsi 70-85) AND
    SCORE>=7-of-10 V2 bands (fixed threshold, no mood-dependent n/n-1). mom_2d was score-band-only
    in V3 and KEEPS its score band; the sector_week hard gate drafted in 22375 was DROPPED by the
    founder, so sector_week is a score band only. Exits fixed +/-3.0%, standard slot pool,
    entry window 09:30-15:15 (cc#1138 rule 1 moved the open from 09:15; cc#855 moved the cut
    from 15:20. The 14:00 cutoff was proposed and founder-REJECTED).
  BUY_REVERSAL V6.1 (_write_buy_reversal_v6_qualified, cc#606 -> cc#754, supersedes V5): 9
    CHEAP conditions -- S1-touch (prior-4-day low OR today's live day_low <= S1), mom_2d [-0.5, 2.5]
    (cc#754 upper cap), week_return>=-2, rsi_month 60-90, sector_week>0 strict, month_return<5,
    day_1d>0 strict, gvm_score>=6.5 strict (cc#754 quality gate).
    V5's heavy true_weekly_rsi>=70 stage REMOVED from this basket only. Entry all-day live CMP,
    no CMP>PP/room/hourly gate. Exits fixed +3%/-3% frozen, max hold 15 trading days, standard pool.
  SELL_REVERSAL V6.1 (_write_sell_reversal_v61_qualified, replaces V5-D): 10 conditions --
    R1-touch last 3 days (per-day pair vs that day's pivot), day_1d [-2,0], dma_20/50/200<0,
    week_index_52<50, sector_week<0 strict, mom_2d [-4,-1], month_return>=-10, FINAL heavy
    true_weekly_rsi<=45. Target S1-or-S2 dynamic (room>=2%), stop 1:1 mirror, RAW (no market
    gate, no kill-switch), standard SELL slot pool.
  SELL_MOMENTUM V4_N5I (_write_sell_momentum_v4_qualified): 8 conditions, ALL CHEAP (cc#854,
    spec 15366) -- mRSI<40 strict, mom_2d [-4,-1], dma_200<=+2, week_return [-10,-0.5],
    sector_week<0 strict, CMP<PP, week_index_52 [20,60], S2-clearance >=3%. The true weekly RSI
    FINAL stage was REMOVED 04-Aug (it passed 0 of 208 and took the funnel to zero); mom_2d
    restored from the drifted [-4,-2] to the spec [-4,-1]. Exits fixed +/-3.0%, standard SELL pool.

Slot architecture (cc#502): SO/S1B ring-fenced pools removed -- ONE standard pool, 20 slots
total (was 24; the 4 freed slots are NOT redistributed):
    Strong Bullish: 15B / 5S  | Bullish:  14B / 6S
    Neutral:        12B / 8S  | Bearish:   8B / 13S

Sector aggregates (REDEFINED by cc#1102, founder ruling 19-Aug-2026):
  sector_day / sector_week / sector_month are the EQUAL-WEIGHT average across the ACTIVE FUTURES
  members of the symbol's futures_universe.theme. Computed ONCE, in theme_change.py, which the
  live writer, the EOD engine and every display surface all call — one grouping, three data
  sources, never three rules.
  Was, until cc#1102: the MCAP-WEIGHTED change across all ~1,795 members of the gvm_scores.segment
  (cc#810/cc#1003). That described a universe V8 cannot trade and let the largest name speak for a
  book that buys ONE LOT PER NAME. Before cc#810 it was an unweighted AVG over futures peers, which
  made a 16-member segment report the move of the 2 F&O names in it.
  A theme with fewer than 3 priced members yields NULL on all three, and NULL FAILS every sector
  gate closed — the gates read `v is not None and <comparison>`.
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
from price_sources import NOT_FUT_SQL   # cc#1056: the ONE futures-source list (cc#1053 registry)
import v8_timing_rules   # cc#1138: V8_TIMING_RULES_V1, session_log 27321

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
# start, the standard LONG/SHORT auto-entry slot_full branch appends, and _flush_slot_blocks
# emits a single ops_log alert at tick end when the pool's blocked count exceeds the
# app_config threshold. (07-Jul recovery tick silently slot_full-blocked 10 SHORT candidates
# with zero visibility.) cc#502: SO/S1B ring-fenced pools removed with those two baskets.
_slot_full_blocks: Dict[str, list] = {}


def _reset_slot_blocks() -> None:
    global _slot_full_blocks
    _slot_full_blocks = {"LONG": [], "SHORT": []}


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
                      -- cc#855: never let the auction print be "the latest bar"
                      AND COALESCE(source,'') NOT IN ('fyers_eq_auction','auction')
                      -- cc#1056: and never let a FUTURES bar be it either. `pc` below is a
                      -- raw_prices CASH close, so an unfiltered `li` compared a futures print
                      -- against a cash baseline for 208 of 209 F&O symbols — the futures leg is
                      -- the latest bar of the day for almost the whole universe. Futures carry a
                      -- basis ABOVE cash, so the error is one-directional and inflates advances.
                      -- MEASURED over the five days that have futures data:
                      --   10-Aug 1.0000 -> 0.8997   11-Aug 0.6859 -> 0.6580
                      --   12-Aug 0.6851 -> 0.6064   13-Aug 1.0525 -> 0.9499
                      --   14-Aug 0.6426 -> 0.6376
                      -- The mood gate's adr >= 1.0 condition PASSED on 10-Aug and 13-Aug purely on
                      -- that artefact and fails on cash. Two of five days, not a rounding error.
                      -- Kept as its own clause rather than folded into the cc#855 list: two
                      -- exclusions, two different reasons, each traceable to its own card.
                      AND COALESCE(source,'') NOT IN ('fyers_fut', 'fyers_fut_rest')
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
                    computed_at    = NOW() AT TIME ZONE 'Asia/Kolkata'
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


# ── cc#855 CLOSING-AUCTION SOURCES — NOT continuous price action ──────────────────────────────
# SEBI CAS went live 03-Aug-2026 for Category I (F&O-eligible) cash stocks. worker/fyers_feed.py
# now tags those bars: 'fyers_eq_auction' for the 15:15-15:30 order-collection window and 'auction'
# for the 15:30 matched close print. An auction fill is a single matched price on ~50x normal
# volume (04-Aug: APOLLOHOSP 8884 -> 9050, +1.87% on 2.59L shares; 11 of the top 12 moves that day
# were UP — a market-wide imbalance, not news). It must never create or kill a signal.
#
# Most reads in this file already pin source='fyers_eq' and therefore exclude these tags BY
# CONSTRUCTION. The exclusion below is for the handful that deliberately read ACROSS sources (the
# ADR breadth CTEs and the NIFTY50 reference closes), which would otherwise pick the auction print
# as "the latest bar" via DISTINCT ON ... ORDER BY ts DESC.
AUCTION_SOURCES = ('fyers_eq_auction', 'auction')

# ── cc#855 item 8: TIMEZONE CANON — IST, EVERYWHERE ───────────────────────────────────────────
# Defect found during this audit: v8_metrics.computed_at was stored NAIVE UTC while
# v8_qualified.signal_ts was stored NAIVE IST — same engine, same tick, two conventions. Evidence
# (04-Aug 19:26 IST): MAX(signal_ts) = 15:30:16 reads -93.8 min old against NOW() (i.e. in the
# FUTURE, so it cannot be UTC) and +236 min against IST now, which is correct. MAX(computed_at) =
# 10:15:18 is 15:45 IST — exactly when the EOD engine runs — so it was UTC.
#
# CANONICAL: IST (Asia/Kolkata). This matches signal_ts, adr_intraday.ts, v8_paper_* and the
# platform rule that all times are IST. Bare NOW() returns timestamptz and silently casts to the
# SESSION timezone (UTC on Railway) when assigned to a naive `timestamp` column — that cast is the
# whole bug, and it is the same class as cc#844's 330-minute phantom staleness. Every write here
# is now NOW() AT TIME ZONE 'Asia/Kolkata', which is explicit and session-independent.
#
# Existing rows are migrated by a one-shot +5:30 UPDATE run immediately after this deploys, in the
# overnight window when no writer tick can interleave.

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
        # cc#1011: every column explicitly qualified to the intraday_prices alias `ip`, so `ts` can
        # never resolve ambiguously if this query later grows a JOIN — the fail-closed contract for
        # the vol baseline still holds (its except below rolls back and blanks the curve), but a bare
        # `ts` should never be the thing that trips it.
        cur.execute("""
            SELECT ip.symbol, ip.source, ip.ts::date, ip.ts::time, ip.volume
            FROM intraday_prices ip
            WHERE ip.symbol = ANY(%s) AND ip.ts::date < %s
              AND ip.ts::date >= %s - INTERVAL '11 days'
              AND ip.source IN ('fyers_eq', 'fyers', 'yahoo')
              AND ip.volume IS NOT NULL
              AND ip.ts::time BETWEEN '09:15' AND '15:30'
            ORDER BY ip.symbol, ip.source, ip.ts
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


# cc#502: _load_hourly_fut_v3 removed -- its only caller was the retired buy_reversal V3
# inverse-sandwich handler; BUY_REVERSAL_V5 has no hourly gate at all, and BUY_MOMENTUM_V3
# explicitly uses the standard _load_hourly_fut (NULL-blocking) instead.


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

    # cc#502: today's live session low, straight from the bar -- BUY_REVERSAL_V5's S1-touch
    # filter needs it (today's day_low <= S1 is a legitimate live leg alongside the prior-4-day
    # check; entry can only happen AFTER the pierce by construction, never a lookahead).
    out["day_low"] = today_bar_low

    out["_live"] = live
    return out


# -- Step 6: Sector aggregates (live) -----------------------------------------

def _add_sector_aggregates(computed: Dict[str, dict], eod_metrics: Dict[str, dict], conn):
    """Set sector_day / sector_week / sector_month on every symbol in `computed`, from the FUTURES
    THEME the symbol belongs to — equal-weight across that theme's ACTIVE futures members.

    cc#1102 (founder ruling 19-Aug-2026). THIS REPLACES THE cc#1003 GVM-SEGMENT BASIS. The old value
    was the mcap-weighted change across ALL ~1,795 gvm_scores members of a GVM segment. Two things
    were wrong with it for THIS book. The universe was wrong: it included microcaps V8 can never
    trade, so the sector described a market the basket does not operate in. And the weighting was
    wrong: V8 trades ONE LOT PER NAME, so a mcap weight lets the biggest name in the segment speak
    for the basket's experience. Equal weight over the futures members is what a one-lot-per-name
    book actually feels.

    THE COMPUTATION IS NOT HERE. It is theme_change.aggregate, the one function the display surfaces
    also go through — cc#1042 exists because two surfaces grouped the same data differently and
    disagreed in public, and a gate disagreeing with its own table would be that failure with money
    attached. This function only chooses the DATA SOURCE: `computed`, the live values this tick is
    about to upsert. That is deliberate — the table and the gate then hold the same number by
    construction, which was the point of cc#1003 and is preserved here.

    WHAT cc#1003 AND cc#1011 ESTABLISHED AND THIS KEEPS. The theme read runs on its OWN short-lived
    connection, never the tick's: on 11-Aug a sector read aborted the tick transaction in place and
    the metrics upsert then wrote 0 of 208 while the heartbeat still stamped healthy. On a separate
    connection a failure here can ONLY produce NULL sector values. And NULL FAILS every sector gate
    closed — each is written `v is not None and <comparison>` — so a missing sector never admits a
    trade on a number nobody measured.

    THIN THEMES: fewer than theme_change.THEME_MIN_MEMBERS priced members yields None on all three
    fields. Honest absence over a fabricated average. `eod_metrics` is no longer read here — the
    theme average is taken over the same live values being written, so a stale EOD fallback would
    mix two sessions inside one average.
    """
    theme_map: Dict[str, str] = {}
    themes: Dict[str, dict] = {}
    try:
        import theme_change
        with psycopg.connect(os.environ.get("DATABASE_URL")) as _sc, _sc.cursor() as cur:
            theme_map = theme_change.theme_of(cur)
        themes = theme_change.aggregate(theme_map, computed)
    except Exception as e:
        log.warning(f"_add_sector_aggregates(cc#1102 theme): isolated theme fetch failed ({e}); "
                    f"sector_* left None this tick — gates fail closed, never a stale value and "
                    f"never a poisoned tick connection")
        theme_map, themes = {}, {}

    n_null = 0
    for sym, m in computed.items():
        info = themes.get(theme_map.get(sym)) if theme_map.get(sym) else None
        if info and not info.get("thin"):
            m["sector_day"]   = info.get("day")
            m["sector_week"]  = info.get("week")
            m["sector_month"] = info.get("month")
        else:
            m["sector_day"] = m["sector_week"] = m["sector_month"] = None
            n_null += 1
    log.info(f"_add_sector_aggregates: {len(themes)} futures themes, equal-weight; "
             f"{n_null} of {len(computed)} symbols carry NULL sector values [cc#1102]")


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
        computed_at   = NOW() AT TIME ZONE 'Asia/Kolkata'
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


# cc#580 fault_1: the per-symbol upsert fallback exists to skip ONE bad DATA row -- NOT to survive a
# lock/timeout. On 20-Jul a lock on v8_metrics timed out the batch, then EACH of ~210 per-symbol
# upserts blocked the full 90s statement_timeout in turn -- one tick ground on for ~5h, pinning its
# worker thread (and, as ticks piled up, saturating the pool so the watchdog restart had no worker).
# These bounds make the fallback bail fast when failures are systemic, so the tick always RETURNS.
_FALLBACK_BUDGET_SEC      = 30    # hard wall-budget for the whole per-symbol fallback loop
_FALLBACK_MAX_CONSEC_FAIL = 5     # abort after N consecutive fails (systemic lock, not bad rows)


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
    consec_fail = 0
    deadline = perf_counter() + _FALLBACK_BUDGET_SEC
    for sym in syms:
        # cc#580 fault_1: bail fast on a systemic lock/timeout instead of grinding all ~210 symbols
        # at 90s each (root cause of the 20-Jul ~5h freeze). One bad DATA row still gets skipped.
        if perf_counter() > deadline:
            log.error(f"upsert_metrics fallback: {_FALLBACK_BUDGET_SEC}s wall-budget spent after "
                      f"{ok}/{len(syms)} — aborting (lock/timeout, not bad rows); tick will finish")
            break
        if consec_fail >= _FALLBACK_MAX_CONSEC_FAIL:
            log.error(f"upsert_metrics fallback: {consec_fail} consecutive failures — systemic "
                      f"(lock/timeout); aborting after {ok}/{len(syms)} to free the worker")
            break
        try:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT sp_upsert")
                cur.execute(_UPSERT_METRICS_SQL, _metrics_row(sym, computed[sym], target_date))
            conn.commit()
            ok += 1
            consec_fail = 0
        except Exception as e2:
            consec_fail += 1
            try:
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_upsert")
            except Exception:
                pass
            log.warning(f"upsert_metrics {sym}: {e2}")
    return ok
# -- Step 7b: RETIRED ---------------------------------------------------------
# cc#1011 retired the separate _update_sector_aggregates_sql pass (its work moved into
# _add_sector_aggregates, on an isolated connection). cc#1102 DELETES the dead body rather than
# leaving it parked: it still grouped by gvm_scores.segment, so anything that ever called it again
# would have silently written the OLD taxonomy over the new one. A retired function that still
# knows how to do the wrong thing is a landmine, not documentation.



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
                WHERE ts <= %s
                ORDER BY ts DESC LIMIT 1
            """, (_cut,))   # cc#719: FREEZE at the last adr_intraday tick at/before the cutoff (any
                            # session) — post-15:30/overnight holds the session close, never adr_daily.
            row = cur.fetchone()
            if row and (row[2] or 0) >= 50:
                adv, dec = row[0] or 0, row[1] or 0
                adr = (adv / dec) if dec else float(adv)
            else:
                cur.execute("""
                    WITH li AS (
                        SELECT DISTINCT ON (symbol) symbol, close AS cmp
                        FROM intraday_prices WHERE ts::date = %s AND ts <= %s
                          -- cc#855: never let the auction print be "the latest bar"
                          AND COALESCE(source,'') NOT IN ('fyers_eq_auction','auction')
                          -- cc#1056: and never let a FUTURES bar be it either. `pc` below is a
                          -- raw_prices CASH close, so an unfiltered `li` compared a futures print
                          -- against a cash baseline for 208 of 209 F&O symbols — the futures leg is
                          -- the latest bar of the day for almost the whole universe. Futures carry a
                          -- basis ABOVE cash, so the error is one-directional and inflates advances.
                          -- MEASURED over the five days that have futures data:
                          --   10-Aug 1.0000 -> 0.8997   11-Aug 0.6859 -> 0.6580
                          --   12-Aug 0.6851 -> 0.6064   13-Aug 1.0525 -> 0.9499
                          --   14-Aug 0.6426 -> 0.6376
                          -- The mood gate's adr >= 1.0 condition PASSED on 10-Aug and 13-Aug purely on
                          -- that artefact and fails on cash. Two of five days, not a rounding error.
                          -- Kept as its own clause rather than folded into the cc#855 list: two
                          -- exclusions, two different reasons, each traceable to its own card.
                          AND COALESCE(source,'') NOT IN ('fyers_fut', 'fyers_fut_rest')
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
                    # cc#719: NO adr_intraday tick and no live breadth (only at the very first tick,
                    # 09:15-09:16, before the writer has stamped a row). The adr_daily fallback is
                    # REMOVED entirely (founder rule: never read adr_daily in the gate — it is
                    # EOD-derived and stale). INDETERMINATE -> neutral 1.0 (adr>=1.0, adds no fail);
                    # mood self-corrects as today's ticks accumulate.
                    adr = 1.0

            cur.execute("""
                SELECT close FROM intraday_prices
                WHERE symbol='NIFTY50' AND ts::date=%s AND ts <= %s
                  AND COALESCE(source,'') NOT IN ('fyers_eq_auction','auction')   -- cc#855
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
                      -- cc#855: reference closes stay on the CONTINUOUS basis. Mixing a 31-Jul
                      -- continuous close against a 04-Aug auction close would compare two
                      -- different price definitions across the 03-Aug methodology change.
                      AND COALESCE(source,'') NOT IN ('fyers_eq_auction','auction')
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


# cc#502: _gate_threshold removed -- its only caller was the generic score-gate loop body,
# retired now that all four baskets are dedicated strict-AND handlers.

# -- Slot architecture --------------------------------------------------------

def _mood_slots(gate_fails: int) -> tuple:
    """Daily buy/sell slot caps from the mood check.

    cc#1138 rule 3 (session_log 27321): SELL SLOTS +1 when the mood check fails 2, 3 or 4 of 4.
    Fail 0 or 1 keeps base. BUY SLOTS ARE UNCHANGED - the bonus is added to the sell leg only.
    So: fails 2 -> 8/9, fails 3 or 4 -> 8/14, fails 0 -> 15/5, fails 1 -> 14/6.

    The base ladder above is untouched. The bonus is a separate term added after it, so the two
    can be read and reverted independently, and the ladder still says what the mood alone buys.

    NOTE the same ladder exists a second time in v8_endpoints.market_mood(), which is what the
    surfaces and the admin paper_tick read. Both carry the bonus - if only one did, the engine
    and the screen would disagree about how many shorts the day allows.
    """
    if gate_fails == 0: buy, sell = 15, 5
    elif gate_fails == 1: buy, sell = 14, 6
    elif gate_fails == 2: buy, sell = 12, 8
    else: buy, sell = 8, 13
    return buy, sell + v8_timing_rules.sell_slot_bonus(gate_fails)


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

    # cc#517: F&O ban list gate -- exchange forbids fresh positions in a banned symbol; paper must
    # not take entries a real account couldn't. Exits are untouched (only this NEW-entry path is
    # gated). Fails OPEN (doesn't block) on any lookup error / before fo_ban has its first nightly
    # row -- an infra hiccup here must never become a new source of missed entries.
    try:
        from nse_eod_ingest import is_banned_today
        with conn.cursor() as cur:
            if is_banned_today(cur, sym):
                log.info(f"auto_paper {sym}: skipped -- F&O ban list (cc#517)")
                return
    except Exception as e:
        log.debug(f"auto_paper {sym}: fo_ban check skipped ({e})")

    now_ist = _now(sim_ts)   # cc#218: sim_ts=None => naive datetime.now(IST); gate logic identical
    if not guards.in_entry_window(now_ist):   # cc#217 P2: was inline 09:15-15:20 block
        log.debug(f"auto_paper {sym}: skipped -- outside market hours {now_ist.strftime('%H:%M')} IST")
        return

    paper_side = _PAPER_SIDE_MAP.get(side, "LONG")
    pp, r1, s1 = pv["pp"], pv["r1"], pv["s1"]

    # cc#217 P2: shared blackout + same-side-open + traded-today (generic) + conflict policy
    if not _entry_guards(conn, sym, paper_side, basket, d, cmp, sim_ts=sim_ts):
        return

    # cc#714: the s1_reclaim_obs observation basket is RING-FENCED — its dedicated 2-concurrent cap
    # is enforced by the handler, and it is EXCLUDED from the standard slot pools both ways (never
    # consumes a standard slot; standard baskets never count it toward theirs).
    # cc#1100: the exemption stayed a SET after the sell_reversal_v7b shadow was withdrawn (founder
    # ruled V7-B LIVE on the sell_reversal basket, so there is no shadow tag to fence off any more).
    # The set is kept rather than collapsed back to one name because the two-way exclusion is the
    # part that is easy to get wrong, and a set makes the next ring-fenced basket a one-line change
    # instead of a re-derivation of this branch. All four live baskets still enter it.
    if basket not in _SLOT_EXEMPT_BASKETS:
      try:
        buy_slots, sell_slots = _mood_slots(gate_fails)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT side, COUNT(*) FROM v8_paper_positions
                WHERE status='OPEN' AND basket <> ALL(%s)
                GROUP BY side
            """, (list(_SLOT_EXEMPT_BASKETS),))   # cc#502: single standard pool; cc#714 + cc#1100: exempt excluded.
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
    elif basket == "buy_reversal":
        # cc#502 BUY_REVERSAL_V5: fixed +3.0%/-3.0% 1:1, frozen at entry (replaces R1-target/mirror).
        target = round(entry * 1.03, 2)
        stop   = round(entry * 0.97, 2)
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

    # cc#1019 FUT_BOOK_CUTOVER_V1 (session_log 21766): the RECORDED fill is the futures price at
    # this moment; the DECISION was and stays cash-based. Everything above this line is untouched —
    # `entry` is the equity CMP and target/stop are still derived from it, so qualification and the
    # risk levels the engine trades on are byte-identical to yesterday. Only the number written to
    # v8_paper_positions.entry_price changes instrument. A missing fut bar records the equity price
    # and says so loudly (see fut_fill_price) rather than skipping the entry.
    try:
        from fut_fill_price import fill_price as _fut_fill
        fill_px, fill_basis = _fut_fill(conn, sym, entry_ts_ist, entry, what="entry")
    except Exception as e:
        log.warning(f"auto_paper {sym}: fut fill pricing unavailable ({e}) — recording EQ entry")
        fill_px, fill_basis = entry, "eq"

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO v8_paper_positions
                (symbol, side, basket, entry_price, entry_ts, qty, target, stop_loss, pp, pivot_date, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN')
                ON CONFLICT (symbol, side, status) DO NOTHING
            """, (sym, paper_side, basket, fill_px, entry_ts_ist, qty, target, stop, pp, d))
            inserted = cur.rowcount
        conn.commit()
        if inserted:
            log.info(f"auto_paper entry: {sym} {paper_side} @ {fill_px} [{fill_basis}] "
                     f"(cash {entry}) entry_ts={entry_ts_ist.strftime('%H:%M')} IST "
                     f"target={target} sl={stop} basket={basket}")
    except Exception as e:
        log.warning(f"auto_paper insert {sym} {paper_side}: {e}")


# -- Step 8: Write v8_qualified + funnel --------------------------------------

# -- V8 SUITE (cc#502, 18-Jul-2026): 4 dedicated strict-AND handlers -----------

def _true_weekly_rsi(conn, symbol: str, live_cmp: Optional[float], sim_ts=None) -> Optional[float]:
    """cc#354, widened cc#502: TRUE calendar-weekly Wilder RSI-14, the shared FINAL heavy stage
    for ALL FOUR dedicated basket handlers (never the shared synthetic v8_metrics.rsi_weekly).
    Resamples raw_prices daily closes to week-end (W, Mon-Sun) last-close, then sets the
    CURRENT (partial) week's running close to the live CMP — filters define the setup, the live
    tick catches the turn. Computed BASKET-CALL-LOCALLY (each handler calls it per-symbol on its
    own survivor set); it must NEVER read/write the shared synthetic rsi_weekly column (cc#353
    audit: that column is a 5-day-stride approximation, ~16pt off)."""
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


# ── cc#1101 · THE FUNNEL ROW IS A SESSION, NOT A TICK ────────────────────────────────────────
# Every handler used to run its own copy of the same upsert: one row per (basket, score_date),
# ON CONFLICT DO UPDATE, on EVERY 5-min tick. That makes the whole row point-in-time — it reports
# whatever was true at the LAST tick of the day. But v8_qualified is a CUMULATIVE day record, so
# the two could never agree except by luck, and on 7 of 8 basket-days measured (17 + 18-Aug) the
# funnel under-reported the table. buy_momentum was the worst case because V5 also gates entries
# to 10:15-13:00: after 13:00 its survivor set is empty BY CONSTRUCTION, so every remaining tick
# rewrote the aggregates to 0 and the 15:15 tick was the one the page served. The funnel then said
# the basket produced nothing on a day it signalled OFSS at 10:25.
#
# The per-gate counts are LEFT point-in-time on purpose — "how many stocks clear dma_50 right now"
# is a reading of the market and is honest at any tick. It is only the three AGGREGATE keys, the
# ones that answer "what did this basket produce TODAY", that have to be day-scoped:
#
#   _score_qualified / _hard_qualified / _stage6_survivors / _stage8_survivors / _stage9_survivors
#       -> day HIGH-WATERMARK. A later tick can raise them, never erase them.
#   _qualified_today
#       -> read straight from v8_qualified, the table the funnel's final row claims to describe.
#          One number, one source (the cc#1025 FUNNEL_TRUTH principle), so parity is true by
#          construction instead of by coincidence.
#
# HONEST LIMIT, stated rather than hidden: the funnel is written BEFORE this tick's qualifier rows
# are inserted, so _qualified_today lags by at most one 5-min tick during the session. It is exact
# from the next tick onward, and the last tick of the day always runs after every qualifier, so the
# stored end-of-day value is exact — which is the value every off-market reader sees.
_FUNNEL_DAY_PEAK_KEYS = ("_stage6_survivors", "_stage8_survivors", "_stage9_survivors",
                         "_hard_qualified", "_score_qualified")


def _merge_day_peaks(funnel: dict, prior: dict) -> dict:
    """Raise this tick's aggregates to the day's high-watermark. Pure, so it can be tested.

    Only keys the caller actually produced are touched — a basket with no heavy stage never gains a
    `_stage8_survivors` of 0 it did not compute. A stored value that is not a number is left exactly
    as it is rather than being coerced to 0, because a legacy row is evidence of what the old writer
    did and overwriting it would destroy that.
    """
    for k in _FUNNEL_DAY_PEAK_KEYS:
        if k in funnel:
            try:
                funnel[k] = max(int(funnel[k] or 0), int((prior or {}).get(k) or 0))
            except (TypeError, ValueError):
                pass
    return funnel


def _upsert_funnel_counts(conn, basket: str, target_date: date, funnel: dict) -> None:
    """The ONE place a funnel row is written. Mutates `funnel` in place, then upserts it.

    Failure is logged and swallowed, exactly as the five inline copies did: a funnel row is a
    display artifact and must never be able to stop a tick from placing trades.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT counts FROM v8_funnel_counts WHERE basket=%s AND score_date=%s",
                        (basket, target_date))
            row = cur.fetchone()
            _merge_day_peaks(funnel, row[0] if row and isinstance(row[0], dict) else {})
            cur.execute("SELECT COUNT(DISTINCT symbol) FROM v8_qualified "
                        "WHERE basket=%s AND signal_date=%s", (basket, target_date))
            funnel["_qualified_today"] = int(cur.fetchone()[0] or 0)
            cur.execute("""
                INSERT INTO v8_funnel_counts (basket, score_date, counts)
                VALUES (%s, %s, %s)
                ON CONFLICT (basket, score_date) DO UPDATE SET
                    counts = EXCLUDED.counts, computed_at = NOW() AT TIME ZONE 'Asia/Kolkata'
            """, (basket, target_date, json.dumps(funnel)))
        conn.commit()
    except Exception as e:
        log.warning(f"{basket} funnel: {e}")


def _write_buy_reversal_v6_qualified(conn, all_metrics: List[dict], target_date: date,
                                     gate_fails: int, pivots: dict, signal_ts_ist, sim_ts=None):
    """cc#606 BUY_REVERSAL_V6 -> cc#754 V6.1 (session_log 7828 + 29-Jul directive). Dedicated
    strict-AND of 9 conditions — ALL CHEAP now (the heavy per-symbol true_weekly_rsi stage is
    removed FROM THIS BASKET ONLY; buy_momentum/sell_momentum/sell_reversal twr stages untouched).
    cc#754 V6.1 adds two tightening gates to the V6 foundation (7 gates unchanged):
      (8) gvm_score >= 6.5 (NULL fails) — restores the quality gate (V6 sprayed junk, e.g. GVM 5.02).
      (9) mom_2d <= 2.5 upper cap (with the existing >= -0.5 -> band [-0.5, 2.5]) — a dip-buy must
          not chase a 2-day rally (all 3 V6 losses in the 29-Jul sim were hot-bounce chases +3.4..+3.8).
      (1) S1-touch: MIN(prior-4-trading-day raw_prices low) <= today's S1, OR today's live
          session day_low <= today's S1. [Entry can only happen AFTER the pierce — never lookahead.]
      (2) mom_2d >= -0.5
      (3) week_return >= -2
      (4) rsi_month in [60, 90]
      (5) sector_week > 0 strict
      (6) month_return < 5
      (7) day_1d > 0 STRICT (NULL fails) — replaces V5's true_weekly_rsi>=70, which was structurally
          empty with the S1-dip in this regime (V5 live 4 sessions -> 1 qual) and unvalidated
          (V5 356tr/62.9% may have scored on synthetic rsi_weekly, cc#353).
    Entry live CMP, all-day. Exits fixed +3%/-3% frozen at entry (_auto_paper_entry's buy_reversal
    branch, unchanged), max hold 15 trading days (existing basket-keyed exit logic, unchanged)."""
    basket, side = "buy_reversal", "BUY"

    # (1) S1-touch leg 1: prior-4-trading-day low, batched once for the whole universe via a
    # single ranked query (no correlated per-symbol subqueries).
    syms = [s["symbol"] for s in all_metrics]
    prior4_low = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH ranked AS (
                    SELECT symbol, low,
                           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
                    FROM raw_prices
                    WHERE symbol = ANY(%s) AND price_date < %s
                )
                SELECT symbol, MIN(low) FROM ranked WHERE rn <= 4 GROUP BY symbol
            """, (syms, target_date))
            for sym, lo in cur.fetchall():
                if lo is not None:
                    prior4_low[sym] = float(lo)
    except Exception as e:
        log.warning(f"buy_reversal_v6 prior4_low: {e}")

    base = []
    for s in all_metrics:
        cmp = s.get("_cmp")
        pv  = pivots.get(s["symbol"])
        if not cmp or not pv:
            continue
        s1 = pv.get("s1")
        if s1 is None:
            continue
        p4lo     = prior4_low.get(s["symbol"])
        today_lo = s.get("day_low")   # leg 2: today's live session low (cc#502)
        s["_s1_touch"] = (p4lo is not None and p4lo <= s1) or (today_lo is not None and today_lo <= s1)
        base.append(s)

    def _sw_gt0(s):     # sector_week > 0 (STRICT)
        v = s.get("sector_week")
        return v is not None and float(v) > 0.0

    def _d1_gt0(s):     # cc#606: day_1d > 0 (STRICT, NULL fails — same strictness as sector_week)
        v = s.get("day_1d")
        return v is not None and float(v) > 0.0

    def _gvm_ok(s):     # cc#754 (8): gvm_score >= 6.5 (STRICT, NULL fails)
        v = s.get("gvm_score")
        return v is not None and float(v) >= 6.5

    # cc#606/#754 BUY_REVERSAL_V6.1: all 9 conditions are CHEAP (no heavy true_weekly_rsi stage).
    # cc#364-style INDEPENDENT per-filter pass counts across `base` — each gate counted ALONE over
    # the whole base, NOT cumulative survivors. _score_qualified = strict 9-way intersection.
    funnel = {"_universe": len(base)}
    funnel["s1_touch"]     = sum(1 for s in base if s["_s1_touch"])                             # (1)
    funnel["mom_2d"]       = sum(1 for s in base if _passes(s.get("mom_2d"), -0.5, 2.5))        # (2)+(9) cc#754 band [-0.5,2.5]
    funnel["week_return"]  = sum(1 for s in base if _passes(s.get("week_return"), -2.0, None))  # (3)
    funnel["rsi_month"]    = sum(1 for s in base if _passes(s.get("rsi_month"), 60.0, 90.0))    # (4)
    funnel["sector_week"]  = sum(1 for s in base if _sw_gt0(s))                                 # (5)
    funnel["month_return"] = sum(1 for s in base if _passes(s.get("month_return"), None, 5.0))  # (6)
    funnel["day_1d"]       = sum(1 for s in base if _d1_gt0(s))                                 # (7) cc#606
    funnel["gvm_score"]    = sum(1 for s in base if _gvm_ok(s))                                 # (8) cc#754
    # _stage6_survivors kept: the 6 pre-day_1d cheap gates intersected (mom_2d now capped at 2.5, cc#754).
    surv = [s for s in base
            if s["_s1_touch"]
            and _passes(s.get("mom_2d"), -0.5, 2.5)
            and _passes(s.get("week_return"), -2.0, None)
            and _passes(s.get("rsi_month"), 60.0, 90.0)
            and _sw_gt0(s)
            and _passes(s.get("month_return"), None, 5.0)]
    funnel["_stage6_survivors"] = len(surv)

    qualified = [s for s in surv if _d1_gt0(s) and _gvm_ok(s)]   # (7) day_1d>0 + (8) gvm>=6.5 — cheap, no per-symbol query
    funnel["_score_qualified"] = len(qualified)
    log.info(f"buy_reversal_v61: {len(surv)} after 6 cheap gates -> {len(qualified)} qualified (day_1d>0 + gvm>=6.5) [cc#754]")

    _upsert_funnel_counts(conn, basket, target_date, funnel)

    for s in qualified:
        sym  = s["symbol"]
        snap = {
            "day_1d":          s.get("day_1d"),
            "mom_2d":          s.get("mom_2d"),
            "week_return":     s.get("week_return"),
            "rsi_month":       s.get("rsi_month"),
            "sector_week":     s.get("sector_week"),
            "month_return":    s.get("month_return"),
            "gvm_score":       s.get("gvm_score"),   # cc#754 (8)
            "s1_touch":        s.get("_s1_touch"),
            "filter_score": 9, "filter_total": 9,
            "spec": "BUY_REVERSAL_V6.1 cc#754",
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
            log.warning(f"buy_reversal_v6 insert {sym}: {e}")


def _write_sell_reversal_v7b_qualified(conn, all_metrics: List[dict], target_date: date,
                                       gate_fails: int, pivots: dict, signal_ts_ist, sim_ts=None):
    """SELL_REVERSAL_V7-B — the LIVE sell_reversal spec. cc#1100, session_log 26363 filters_v7b.

    FOUNDER RULED LIVE, NOT SHADOW (19-Aug-2026, relayed in cc_task_logs 2793). V7-B REPLACES the
    V6.1 filter set on this basket. Same basket tag `sell_reversal`, same SELL slot pool, same book.
    The earlier shadow build (a separate `sell_reversal_v7b` tag behind the s1_reclaim_obs
    ring-fence) is WITHDRAWN and removed — a second tag would have split the book in two.

    WHAT THE BASKET IS, in one sentence, because the name has misled people: price rallied UP to R1
    resistance, was REJECTED there, and has already given back at least 3% by the time we enter. It
    is the failed bounce off R1. The stock's position inside its 52-week range is context, not the
    identity of the setup (founder, 19-Aug ~01:40).

    THE TWELVE GATES (verbatim from cc_task_logs 2791):
      (1)  R1-touch: day HIGH >= that day's R1 on any of the last 3 trading days
      (2)  day_1d in [-2, 0]        the BAND, not negative-only — see the note below
      (3)  dma_20 < 0
      (4)  dma_50 < 0
      (5)  dma_200 < 0
      (6)  week_index_52 < 30       TIGHTENED from V6.1's < 50
      (7)  rsi_month <= 30          NEW
      (8)  sector_week <= -0.5      V6.1 was < 0
      (9)  fall from R1 >= 3%       NEW, at ENTRY: (R1 - entry) / R1 * 100
      (10) mom_2d in [-4, -1]
      (11) month_return >= -10      ABSOLUTE, not Nifty-relative
      (12) room: target (S1 or S2) >= 2% from entry

    WHAT LEFT V6.1 AND WHY IT MATTERS. true_weekly_rsi <= 45 is GONE — and it was not merely
    inert, it was LEAKING: four live trades entered at stored wRSI 49.4, 57.0, 70.4 and 74.0
    against a <=45 gate. sector_month is deliberately NOT implemented; it behaved as a regime
    switch rather than a stock filter and produced zero qualifying rows in four months of twelve.

    THE DAY-CHANGE BAND IS -2..0, NOT negative-only. Removing the lower bound lets crash-day
    entries in, and that removal was an artifact of the simulation rather than a finding. This is
    the one place the sweep and the spec disagree, so it is stated rather than buried.

    FALL FROM R1 is evaluated on the ENTRY BAR price — the live 5-min CMP the entry is taken at,
    not the prior close and not the day open. R1 is the same rolling-5d pivot gate (1) uses.

    EVIDENCE AND ITS LIMIT, kept next to the code that trades it: a 12-month 5-min replay gave 144
    entries, 86 wins, 60.1%. Ex-March 2026 it is 102 entries at 55.4%, and 29% of all backtest
    trades fall in that one bear month. There has been no BT7 parity run and the replay gates on
    PRIOR-DAY EOD metrics while this writer evaluates every 5 minutes — a phase-2 entry criterion
    per STRATEGY_PHASE_MODEL_V1 (session_log 26386), not a reason to hold the ruling.

    Exits are UNCHANGED from V6.1: target = S1 if room >= 2% else S2 (never beyond S2), stop = 1:1
    mirror above entry, max hold 15 trading days. Entry via _auto_paper_entry with the FROZEN
    levels, standard SELL slot pool and all standard guards.
    """
    basket, side = "sell_reversal", "SELL"

    # (1) R1-touch last 3 trading days: per-day pair (that day's high vs that SAME day's r1),
    # batched once for the whole universe. Excludes today (backtest convention). Query unchanged
    # from V6.1 — this leg is the one thing V7-B did not alter.
    syms = [s["symbol"] for s in all_metrics]
    r1_touch = set()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH days AS (
                    SELECT DISTINCT price_date AS d FROM raw_prices
                    WHERE price_date < %s
                    ORDER BY d DESC LIMIT 3
                )
                SELECT DISTINCT rp.symbol
                FROM raw_prices rp
                JOIN v8_paper_pivots pv ON pv.symbol = rp.symbol AND pv.pivot_date = rp.price_date
                WHERE rp.price_date IN (SELECT d FROM days) AND rp.symbol = ANY(%s)
                  AND pv.r1 IS NOT NULL AND rp.high >= pv.r1
            """, (target_date, syms))
            r1_touch = {r[0] for r in cur.fetchall()}
    except Exception as e:
        log.warning(f"sell_reversal_v7b r1_touch: {e}")

    base = []
    for s in all_metrics:
        cmp = s.get("_cmp")
        pv  = pivots.get(s["symbol"])
        if not cmp or not pv:
            continue
        pp, s1, s2, r1 = pv.get("pp"), pv.get("s1"), pv.get("s2"), pv.get("r1")
        if s1 is None:
            continue
        # dynamic target: S1 if >=2% below entry, else S2 (needs s2); chosen target must be >=2%.
        room_s1 = round((cmp - s1) / cmp * 100.0, 3)
        room_s2 = round((cmp - s2) / cmp * 100.0, 3) if s2 is not None else None
        if room_s1 >= 2.0:
            tgt, room = s1, room_s1
        elif room_s2 is not None and room_s2 >= 2.0:
            tgt, room = s2, room_s2
        else:
            tgt, room = None, None      # room gate fails -> no signal
        # (9) fall from R1, on the entry-bar price. A missing or non-positive R1 FAILS the gate
        # rather than dividing — admitting a row on bad pivot data would put real book behind a
        # setup we never actually measured.
        fall = ((r1 - cmp) / r1 * 100.0) if (r1 and r1 > 0) else None
        s["_pp"] = pp
        s["_sr_target"] = tgt
        s["_sr_room_pct"] = room
        s["_sr_room_ok"] = tgt is not None
        s["_sr_r1"] = r1
        s["_sr_fall_r1"] = round(fall, 3) if fall is not None else None
        s["_r1_touch"] = s["symbol"] in r1_touch
        base.append(s)

    def _sw_le(s):        # sector_week <= -0.5
        v = s.get("sector_week")
        return v is not None and float(v) <= -0.5

    def _fall_ok(s):      # fall from R1 >= 3%
        v = s.get("_sr_fall_r1")
        return v is not None and v >= 3.0

    # cc#364-style INDEPENDENT per-filter pass counts across `base`. V7-B has NO heavy final stage
    # — every gate is cheap, so all 12 rows are counted over the same universe and there is no
    # survivor denominator to carry.
    funnel = {"_universe": len(base)}
    funnel["r1_touch"]      = sum(1 for s in base if s["_r1_touch"])                               # (1)
    funnel["day_1d"]        = sum(1 for s in base if _passes(s.get("day_1d"), -2.0, 0.0))          # (2)
    funnel["dma_20"]        = sum(1 for s in base if _passes(s.get("dma_20"), None, 0.0))          # (3)
    funnel["dma_50"]        = sum(1 for s in base if _passes(s.get("dma_50"), None, 0.0))          # (4)
    funnel["dma_200"]       = sum(1 for s in base if _passes(s.get("dma_200"), None, 0.0))         # (5)
    funnel["week_index_52"] = sum(1 for s in base if _passes(s.get("week_index_52"), None, 30.0))  # (6)
    funnel["rsi_month"]     = sum(1 for s in base if _passes(s.get("rsi_month"), None, 30.0))      # (7)
    funnel["sector_week"]   = sum(1 for s in base if _sw_le(s))                                    # (8)
    funnel["fall_from_r1"]  = sum(1 for s in base if _fall_ok(s))                                  # (9)
    funnel["mom_2d"]        = sum(1 for s in base if _passes(s.get("mom_2d"), -4.0, -1.0))         # (10)
    funnel["month_return"]  = sum(1 for s in base if _passes(s.get("month_return"), -10.0, None))  # (11)
    funnel["room"]          = sum(1 for s in base if s["_sr_room_ok"])                             # (12)
    qualified = [s for s in base
                 if s["_r1_touch"]
                 and _passes(s.get("day_1d"), -2.0, 0.0)
                 and _passes(s.get("dma_20"), None, 0.0)
                 and _passes(s.get("dma_50"), None, 0.0)
                 and _passes(s.get("dma_200"), None, 0.0)
                 and _passes(s.get("week_index_52"), None, 30.0)
                 and _passes(s.get("rsi_month"), None, 30.0)
                 and _sw_le(s)
                 and _fall_ok(s)
                 and _passes(s.get("mom_2d"), -4.0, -1.0)
                 and _passes(s.get("month_return"), -10.0, None)
                 and s["_sr_room_ok"]]
    funnel["_score_qualified"] = len(qualified)
    log.info(f"sell_reversal_v7b: {len(qualified)} qualified of {len(base)} (12-gate strict AND) [cc#1100]")

    _upsert_funnel_counts(conn, basket, target_date, funnel)

    for s in qualified:
        sym   = s["symbol"]
        cmp   = s.get("_cmp")
        entry = round(cmp, 2)
        tgt   = round(s["_sr_target"], 2)
        stop  = round(entry + (entry - tgt), 2)   # 1:1 mirror ABOVE entry (SELL)
        snap = {
            "r1_touch":        s.get("_r1_touch"),
            "day_1d":          s.get("day_1d"),
            "dma_20":          s.get("dma_20"),
            "dma_50":          s.get("dma_50"),
            "dma_200":         s.get("dma_200"),
            "week_index_52":   s.get("week_index_52"),
            "rsi_month":       s.get("rsi_month"),
            "sector_week":     s.get("sector_week"),
            "mom_2d":          s.get("mom_2d"),
            "month_return":    s.get("month_return"),
            "r1":              s.get("_sr_r1"),
            "fall_from_r1":    s.get("_sr_fall_r1"),
            "room_pct":        s.get("_sr_room_pct"),
            "target":          tgt,
            "stop":            stop,
            "filter_score": 12, "filter_total": 12,
            "spec": "SELL_REVERSAL_V7B cc#1100",
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
            # entry with the handler-computed FROZEN S1/S2 target + 1:1 mirror stop.
            _auto_paper_entry(conn, sym, basket, side, cmp, pivots.get(sym),
                              target_date, gate_fails, sim_ts=sim_ts, target=tgt, stop=stop)
        except Exception as e:
            log.warning(f"sell_reversal_v7b insert {sym}: {e}")


def _write_sell_momentum_v4_qualified(conn, all_metrics: List[dict], target_date: date,
                                      gate_fails: int, pivots: dict, signal_ts_ist, sim_ts=None):
    """cc#854 SELL_MOMENTUM_V4_N5I (was cc#502 V4). Dedicated strict-AND handler — sell_momentum
    is REMOVED from the standard score-gate loop. Strict AND of 8:
      (1) rsi_month       < 40      (deeply weak monthly — now the ONLY slow-timeframe filter)
      (2) mom_2d in [-4, -1]        (recent down-momentum, not a crash)
      (3) dma_200         <= +2     (below / near the 200-DMA)
      (4) week_return in [-10, -0.5](weak week, not capitulation)
      (5) sector_week     < 0       (weak sector)
      (6) CMP < PP                  (below the rolling-5d pivot)
      (7) week_index_52 in [20, 60] (mid 52-week band)
      (8) S2-clearance (CMP-S2)/CMP >= 3%  (support sits below the 3% target so it can't block the fall)

    cc#854 (founder-locked 04-Aug, spec session_log id=15366 — supersedes id=2901 for the FILTER
    SET ONLY). Two changes, both to undo drift that had made this basket unfireable:

      TRUE WEEKLY RSI IS REMOVED ENTIRELY. It was the terminal stage and on 04-Aug it passed 0 of
      208, taking the funnel 1 -> 0. Over the prior 14 sessions at <=45 it was near-inert (identical
      counts on 10 of 14 days); at <=40 it is a hard stop. Founder chose REMOVAL, explicitly not a
      relaxation back to <=45. The shared _true_weekly_rsi() helper stays — buy_momentum,
      buy_reversal and sell_reversal still use it; only this basket's call is gone, which also
      removes a per-symbol heavy query from every 5-min tick.

      mom_2d RESTORED to the spec band [-4, -1]. It had drifted to [-4, -2].

    RISK ACCEPTED BY FOUNDER (15366): without the weekly gate this is a 2-day momentum short with
    monthly RSI as the only slow-timeframe filter. Watch WR against the 64% 1-yr backtest baseline;
    below ~55% over 30+ live trades, the weekly gate is the first thing to reconsider — at 45, not 40.

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

    # cc#380: INDEPENDENT per-filter pass counts across `base` (cc#364 convention). cc#854: every
    # filter is cheap now, so all 8 rows are counted over the full universe — there is no heavy
    # stage left to count over a survivor subset.
    funnel = {"_universe": len(base)}
    funnel["rsi_month"]     = sum(1 for s in base if _rm_lt40(s))                              # (1)
    funnel["mom_2d"]        = sum(1 for s in base if _passes(s.get("mom_2d"), -4.0, -1.0))     # (2) cc#854: -2 -> -1 (spec 2901)
    funnel["dma_200"]       = sum(1 for s in base if _passes(s.get("dma_200"), None, 2.0))     # (3)
    funnel["week_return"]   = sum(1 for s in base if _passes(s.get("week_return"), -10.0, -0.5))  # (4)
    funnel["sector_week"]   = sum(1 for s in base if _sw_lt0(s))                               # (5)
    funnel["cmp_lt_pp"]     = sum(1 for s in base if s["_cmp"] < s["_pp"])                     # (6)
    funnel["week_index_52"] = sum(1 for s in base if _passes(s.get("week_index_52"), 20.0, 60.0))  # (7)
    funnel["s2_clearance"]  = sum(1 for s in base if _s2c_ok(s))                               # (8)
    surv = [s for s in base
            if _rm_lt40(s)
            and _passes(s.get("mom_2d"), -4.0, -1.0)
            and _passes(s.get("dma_200"), None, 2.0)
            and _passes(s.get("week_return"), -10.0, -0.5)
            and _sw_lt0(s)
            and _passes(s.get("week_index_52"), 20.0, 60.0)
            and s["_cmp"] < s["_pp"]
            and _s2c_ok(s)]
    # cc#854: the true_weekly_rsi FINAL stage is GONE. The 8 cheap gates ARE the whole basket now,
    # so the strict-AND survivors are the qualifiers — there is no second pass and no
    # `_stage8_survivors` denominator, because there is no stage 9 left to divide into.
    # `true_weekly_rsi` is deliberately absent from `funnel`, which is what drops its funnel row.
    qualified = surv
    funnel["_score_qualified"] = len(qualified)
    log.info(f"sell_momentum_v4: {len(qualified)} qualified (8 filters, cc#854 N5I) ")

    _upsert_funnel_counts(conn, basket, target_date, funnel)

    for s in qualified:
        sym = s["symbol"]
        cmp = s.get("_cmp")
        snap = {
            # cc#854: true_weekly_rsi dropped — the filter is removed, so the value is no longer
            # computed. Emitting a null here would read as "measured and empty" rather than
            # "no longer part of this basket".
            "rsi_month":       s.get("rsi_month"),
            "mom_2d":          s.get("mom_2d"),
            "dma_200":         s.get("dma_200"),
            "week_return":     s.get("week_return"),
            "sector_week":     s.get("sector_week"),
            "week_index_52":   s.get("week_index_52"),
            "s2_clearance_pct": s.get("_s2c_pct"),
            "target": round(round(cmp, 2) * 0.97, 2), "stop": round(round(cmp, 2) * 1.03, 2),
            "filter_score": 8, "filter_total": 8,
            "spec": "SELL_MOMENTUM_V4_N5I cc#854 (session_log 15366)",
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
            log.warning(f"sell_momentum_v4 insert {sym}: {e}")


def _write_buy_momentum_v3_qualified(conn, all_metrics: List[dict], target_date: date,
                                     gate_fails: int, pivots: dict, signal_ts_ist, sim_ts=None):
    """cc#502 BUY_MOMENTUM_V3 -> BUY_MOMENTUM_V4 FINAL (spec 22386 on 5650) — leaves the generic
    score-gate loop entirely; dedicated handler. Two independent layers, both must pass:
      HARD gates (all strict-AND): dma_50 in [5,12], dma_20 > 0, week_index_52 >= 75,
        gvm_score >= 7, day_1d > 0, mom_2d in [0,4] (V4, the ONE added gate), hourly_pct > 0 AND
        NOT NULL (existing 12-bar _load_hourly_fut -- NULL blocks, so no entries before ~10:15; the
        v3 partial-window loader is NOT used here), FINAL heavy stage: true_weekly_rsi in [70, 85].
      SCORE >= 7 of 10 V2 bands (FIXED threshold, NO mood-dependent n/n-1): gvm 7-10, dma50
        8-25, dma200 8-40, rsi_month 70-100, wRSI 60-85 (the SAME true_weekly_rsi value from the
        hard-gate heavy stage), week_return 0.5-12, month_return 2-30, mom_2d 0-6, sector_week
        0-6, sector_month 0-6.
    cc#1045: the mom_2d and sector_week SCORE bands above are deliberately LEFT AS THEY WERE. They
    still contribute their point exactly as V3's overridden dma_50/wRSI bands do — the hard gate is
    a layer on top, never a replacement, so the score arithmetic is unchanged and a V3 score is
    still comparable to a V4 score. sector_week is now a SCORE BAND ONLY again: 22386 dropped its
    hard gate, so it influences the score and never blocks an entry on its own.
    NO pivot-room gate (not in evidence; exits fixed). Exits FIXED +/-3.0% via _auto_paper_entry's
    buy_momentum branch; standard BUY slot pool + all standard guards. Entry window 09:30-15:15
    UNCHANGED — the proposed 14:00 cutoff was explicitly REJECTED by the founder (22386)."""
    basket, side = "buy_momentum", "BUY"

    def _hourly_ok(s):   # NULL-blocks (no entries before ~10:15); strict > 0
        v = s.get("hourly_pct")
        return v is not None and float(v) > 0.0
    def _day1d_gt0(s):    # day_1d > 0 (STRICT)
        v = s.get("day_1d")
        return v is not None and float(v) > 0.0
    def _dma20_gt0(s):    # dma_20 > 0 (STRICT)
        v = s.get("dma_20")
        return v is not None and float(v) > 0.0
    # ── cc#1051 BUY_MOMENTUM_V5 (spec 23186) ────────────────────────────────────────────────
    # The basket keeps its NAME and its slots; only the rule set changes (founder 16-Aug: upgrade
    # in place, do not fork a new basket). Two gates that were live this morning are GONE by
    # design, not by accident: the cc#1045 mom_2d [0,4] gate (23186 says mom_2d is band-only) and
    # the true_weekly_rsi [70,85] heavy stage that had gated this basket since cc#502 (23186 keeps
    # twr as a SCORE band). In their place come the three S1-pullback legs below.
    def _day1d_band(s):   # day_1d in (0, 2.0] — still strictly positive, now capped
        v = s.get("day_1d")
        return v is not None and 0.0 < float(v) <= 2.0

    # PIVOTS ARE NOT RECOMPUTED HERE. `pivots` is the same rolling-5-day classic set every other
    # basket is handed (PP=(H5+L5+C1)/3, S1=2*PP-H5), so the S1 and PP legs below read the one
    # shared derivation rather than forking the formula — spec 23186 names that convention
    # explicitly and the card forbids a private copy.
    def _pp_band(s):      # live price ABOVE PP by 0 to 1.5 pct
        cmp_, pv = s.get("_cmp"), pivots.get(s["symbol"])
        if not cmp_ or not pv or not pv.get("pp"):
            return False
        off = (float(cmp_) / float(pv["pp"]) - 1) * 100.0
        return 0.0 <= off <= 1.5

    # ENTRY WINDOW 10:15-13:00 IST, hard. The hourly gate already blocks before ~10:15; the 13:00
    # cutoff is new and is what makes this a PULLBACK basket rather than an all-day chase. Read off
    # the tick clock so a replay honours it exactly as the live path does.
    _tick = signal_ts_ist if not sim_ts else sim_ts
    try:
        _in_window = (_tick is not None and _tick.time() >= time(10, 15)
                      and _tick.time() <= time(13, 0))
    except Exception:
        _in_window = True

    # S1-TOUCH, 3 COMPLETED SESSIONS OR TODAY-SO-FAR. Same shape as buy_reversal's leg (batched
    # once for the whole universe, no per-symbol subqueries) but 3 sessions per 23186, not 4.
    _p3lo = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH ranked AS (
                    SELECT symbol, low,
                           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
                    FROM raw_prices
                    WHERE symbol = ANY(%s) AND price_date < %s
                )
                SELECT symbol, MIN(low) FROM ranked WHERE rn <= 3 GROUP BY symbol
            """, ([m["symbol"] for m in all_metrics], target_date))
            for _sym, _lo in cur.fetchall():
                if _lo is not None:
                    _p3lo[_sym] = float(_lo)
    except Exception as e:
        log.warning(f"buy_momentum_v5 prior3_low: {e}")

    def _s1_touch(s):
        pv = pivots.get(s["symbol"])
        if not pv or pv.get("s1") is None:
            return False
        s1 = float(pv["s1"])
        p3 = _p3lo.get(s["symbol"])
        today_lo = s.get("day_low")
        return (p3 is not None and p3 <= s1) or (today_lo is not None and float(today_lo) <= s1)

    base = list(all_metrics)
    for s in base:
        s["_s1_touch"] = _s1_touch(s)
    funnel = {"_universe": len(base)}
    funnel["dma_50"]        = sum(1 for s in base if _passes(s.get("dma_50"), 5.0, 12.0))
    funnel["dma_20"]        = sum(1 for s in base if _dma20_gt0(s))
    funnel["week_index_52"] = sum(1 for s in base if _passes(s.get("week_index_52"), 75.0, None))
    funnel["gvm_score"]     = sum(1 for s in base if _passes(s.get("gvm_score"), 7.0, None))
    funnel["day_1d"]        = sum(1 for s in base if _day1d_band(s))
    funnel["s1_touch"]      = sum(1 for s in base if s["_s1_touch"])
    funnel["pp_band"]       = sum(1 for s in base if _pp_band(s))
    funnel["hourly_pct"]    = sum(1 for s in base if _hourly_ok(s))
    funnel["_entry_window"] = 1 if _in_window else 0
    surv = [s for s in base
            if _passes(s.get("dma_50"), 5.0, 12.0)
            and _dma20_gt0(s)
            and _passes(s.get("week_index_52"), 75.0, None)
            and _passes(s.get("gvm_score"), 7.0, None)
            and _day1d_band(s)
            and s["_s1_touch"]
            and _pp_band(s)
            and _hourly_ok(s)] if _in_window else []
    # KEY NAME IS LEGACY, VALUE IS CURRENT — see cc#1045. `_stage6_survivors` now holds the
    # intersection of the EIGHT cheap gates. The name is kept because v8_endpoints reads this
    # literal key; renaming it would null the denominator on every stored row.
    funnel["_stage6_survivors"] = len(surv)
    log.info(f"buy_momentum_v5: {len(surv)} after 8-gate S1-pullback filter "
             f"(window={_in_window}) [cc#1051]")

    # twr is still COMPUTED — it is the 10th SCORE band — but it no longer gates anything.
    hard_qualified = surv
    for s in surv:
        twr = _true_weekly_rsi(conn, s["symbol"], s.get("_cmp"), sim_ts=sim_ts)
        s["_true_weekly_rsi"] = round(twr, 2) if twr is not None else None
    # cc#1051: twr is no longer a GATE, so it must not appear as a funnel gate key — the cc#599
    # registry-parity check compares these keys to BASKET_FILTERS and would flag a ghost row. The
    # count the score stage needs (stocks clearing every hard gate) moves to a PRIVATE key, and
    # v8_endpoints reads it with a fallback to the old name so rows stored before this deploy
    # still render their denominator.
    funnel["_hard_qualified"] = len(hard_qualified)

    # SCORE >= 7 of 10 V2 bands, FIXED threshold (no mood-dependent n/n-1). wRSI band reuses the
    # SAME true_weekly_rsi value just computed for the hard gate (spec: "use the SAME value").
    SCORE_BANDS = {
        "gvm_score":    (7.0, 10.0),
        "dma_50":       (8.0, 25.0),
        "dma_200":      (8.0, 40.0),
        "rsi_month":    (70.0, 100.0),
        "week_return":  (0.5, 12.0),
        "month_return": (2.0, 30.0),
        "mom_2d":       (0.0, 6.0),
        "sector_week":  (0.0, 6.0),
        "sector_month": (0.0, 6.0),
    }
    qualified = []
    for s in hard_qualified:
        score = sum(1 for metric, (mn, mx) in SCORE_BANDS.items() if _passes(s.get(metric), mn, mx))
        twr = s.get("_true_weekly_rsi")
        if twr is not None and 60.0 <= twr <= 85.0:
            score += 1
        s["_score"] = score
        if score >= 7:
            qualified.append(s)
    funnel["_score_qualified"] = len(qualified)
    log.info(f"buy_momentum_v4: {len(qualified)} qualified (score>=7/10) [cc#1038]")

    _upsert_funnel_counts(conn, basket, target_date, funnel)

    for s in qualified:
        sym  = s["symbol"]
        snap = {
            "true_weekly_rsi": s.get("_true_weekly_rsi"),
            "dma_50":  s.get("dma_50"), "dma_20": s.get("dma_20"), "dma_200": s.get("dma_200"),
            "week_index_52": s.get("week_index_52"), "gvm_score": s.get("gvm_score"),
            "day_1d": s.get("day_1d"), "hourly_pct": s.get("hourly_pct"),
            "rsi_month": s.get("rsi_month"), "week_return": s.get("week_return"),
            "month_return": s.get("month_return"), "mom_2d": s.get("mom_2d"),
            "sector_week": s.get("sector_week"), "sector_month": s.get("sector_month"),
            "score": s.get("_score"), "score_threshold": 7, "score_total": 10,
            "filter_score": s.get("_score"), "filter_total": 10,
            # cc#1038 V4 ERA MARKER. Same convention as BUY_REVERSAL_V6_FRESH_BOOK (session_log
            # 7842): the era lives on the qual snapshot, so V4 performance counts ONLY trades whose
            # snapshot carries this string. Rows already stamped BUY_MOMENTUM_V3 keep their stamp —
            # historical trades are never rewritten and never deleted. Open V3 positions are
            # GRANDFATHERED: this handler only gates ENTRY, and exits (+/-3% frozen at entry,
            # 15-day max hold, gap exits) are owned by v8_paper and are byte-untouched here, so a
            # V3 position opened before the deploy runs out its V3 exit exactly as before.
            # cc#1051 V5 FRESH BOOK (BUY_REVERSAL_V6_FRESH_BOOK precedent, session_log 7842):
            # V5 performance counts ONLY rows carrying this stamp. V4-stamped rows keep theirs and
            # are never rewritten, so the two eras stay separable in the book.
            "spec": "BUY_MOMENTUM_V5 cc#1051",
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
                    VALUES (%s,'buy_momentum',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'live_5min')
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
            log.warning(f"buy_momentum_v3 insert {sym}: {e}")


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# cc#607 PHASE A — BASKET_FILTER_REGISTRY (single source of truth for basket gates)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# The four dedicated handlers above ARE the trading-logic truth. This registry is DATA declared
# adjacent to them, enumerating each basket's live gates in funnel/display order — key, display
# label + condition strings, machine bounds (min/max/strict), and type. v8_endpoints.py imports
# BASKET_FILTERS so the funnel rows, per-stock pass-count breakdown, /api/v8/filters payload and the
# dashboard i-button ALL generate from this one list instead of hand-maintained copies (the source
# of the cc#607 ghost true-weekly-RSI row on buy_reversal). Drop a gate here (and in the handler)
# and it disappears from every display surface in one deploy; the cc#599 watchdog parity check
# alerts if a handler's live funnel keys ever drift from this registry.
#
# Per-filter fields:
#   key        v8_funnel_counts key (== the funnel key the handler writes) / v8_metrics column
#   label      display label (spaces preserved for verbatim dashboard render)
#   cond_min   left-hand condition string (e.g. ">= -0.5", "> 0", "");  cond_max  right-hand ("<= 90")
#   min/max    machine bounds for FILTER_CONFIG-shape derivation (None = open side)
#   type       "band"   = a v8_metrics column gate (feeds FILTER_CONFIG + pass-count band loop)
#              "custom" = a live/pivot/price-history leg the handler computes inline (s1_touch,
#                         r1_touch, room, cmp_lt_pp, s2_clearance) — NOT a v8_metrics column
#   heavy      True only for the per-symbol true_weekly_rsi FINAL stage (funnel denom = survivors)
#   denom_key  for a heavy stage: the v8_funnel_counts survivor-count key its denominator reads
# NOTE (Phase A): trading logic still lives in each handler; Phase B (future task) has the handlers
# consume min/max FROM this registry so bounds are literal-single-source too. Keys/order/bounds here
# MUST mirror the handler exactly — funnel counts are byte-identical pre/post (metadata-only change).
BASKET_FILTERS = {
    # BUY_REVERSAL_V6.1 (cc#606 -> cc#754) — 9 CHEAP gates (V6.1 adds gvm_score>=6.5 + mom_2d upper cap
    # 2.5), no heavy true_weekly_rsi stage (day_1d>0 replaced it).
    "buy_reversal": [
        {"key": "s1_touch",     "label": "S1 touch (prior-4d low or today's low)", "cond_min": "<= S1",   "cond_max": "",     "min": None,  "max": None,  "type": "custom"},
        {"key": "mom_2d",       "label": "mom 2d",       "cond_min": ">= -0.5", "cond_max": "<= 2.5","min": -0.5,  "max": 2.5,   "type": "band"},
        {"key": "week_return",  "label": "week return",  "cond_min": ">= -2",   "cond_max": "",     "min": -2.0,  "max": None,  "type": "band"},
        {"key": "rsi_month",    "label": "monthly RSI",  "cond_min": ">= 60",   "cond_max": "<= 90","min": 60.0,  "max": 90.0,  "type": "band"},
        {"key": "sector_week",  "label": "sector week",  "cond_min": "> 0",     "cond_max": "",     "min": 0.0,   "max": None,  "type": "band", "strict": True},
        {"key": "month_return", "label": "month return", "cond_min": "",        "cond_max": "< 5",  "min": None,  "max": 5.0,   "type": "band"},
        {"key": "day_1d",       "label": "day 1d",       "cond_min": "> 0",     "cond_max": "",     "min": 0.0,   "max": None,  "type": "band", "strict": True},
        {"key": "gvm_score",    "label": "gvm score",    "cond_min": ">= 6.5",  "cond_max": "",     "min": 6.5,   "max": None,  "type": "band", "strict": True},
    ],
    # SELL_REVERSAL_V7-B (cc#1100, spec session_log 26363 filters_v7b, founder ruled LIVE 19-Aug) —
    # 12 cheap gates, NO heavy final stage. This REPLACED the V6.1 set on the live basket. Two
    # things left V6.1 and both are deliberate: true_weekly_rsi <= 45 is gone (it was LEAKING —
    # four live trades entered at stored wRSI 49.4, 57.0, 70.4, 74.0 against that gate), and
    # sector_month was never added because it acted as a regime switch, dark for 4 months of 12.
    "sell_reversal": [
        {"key": "r1_touch",       "label": "R1 touch (last 3 days)", "cond_min": "",       "cond_max": "",       "min": None,   "max": None, "type": "custom"},
        {"key": "day_1d",         "label": "day change",   "cond_min": ">= -2",  "cond_max": "<= 0",   "min": -2.0,  "max": 0.0,  "type": "band"},
        {"key": "dma_20",         "label": "dma 20",       "cond_min": "",       "cond_max": "< 0",    "min": None,  "max": 0.0,  "type": "band", "strict": True},
        {"key": "dma_50",         "label": "dma 50",       "cond_min": "",       "cond_max": "< 0",    "min": None,  "max": 0.0,  "type": "band", "strict": True},
        {"key": "dma_200",        "label": "dma 200",      "cond_min": "",       "cond_max": "< 0",    "min": None,  "max": 0.0,  "type": "band", "strict": True},
        {"key": "week_index_52",  "label": "52w index",    "cond_min": "",       "cond_max": "< 30",   "min": None,  "max": 30.0, "type": "band", "strict": True},
        {"key": "rsi_month",      "label": "monthly RSI",  "cond_min": "",       "cond_max": "<= 30",  "min": None,  "max": 30.0, "type": "band"},
        {"key": "sector_week",    "label": "sector week",  "cond_min": "",       "cond_max": "<= -0.5","min": None,  "max": -0.5, "type": "band"},
        {"key": "fall_from_r1",   "label": "fall from R1", "cond_min": ">= 3%",  "cond_max": "",       "min": None,  "max": None, "type": "custom"},
        {"key": "mom_2d",         "label": "mom 2d",       "cond_min": ">= -4",  "cond_max": "<= -1",  "min": -4.0,  "max": -1.0, "type": "band"},
        {"key": "month_return",   "label": "month return", "cond_min": ">= -10", "cond_max": "",       "min": -10.0, "max": None, "type": "band"},
        {"key": "room",           "label": "room to S1/S2","cond_min": ">= 2%",  "cond_max": "",       "min": None,  "max": None, "type": "custom"},
    ],
    # SELL_MOMENTUM_V4_N5I (cc#854, spec 15366) — 8 cheap gates, NO heavy FINAL stage.
    "sell_momentum": [
        {"key": "rsi_month",      "label": "monthly RSI",  "cond_min": "",       "cond_max": "< 40",   "min": None,  "max": 40.0, "type": "band", "strict": True},
        {"key": "mom_2d",         "label": "mom 2d",       "cond_min": ">= -4",  "cond_max": "<= -1",  "min": -4.0,  "max": -1.0, "type": "band"},
        {"key": "dma_200",        "label": "dma 200",      "cond_min": "",       "cond_max": "<= 2",   "min": None,  "max": 2.0,  "type": "band"},
        {"key": "week_return",    "label": "week return",  "cond_min": ">= -10", "cond_max": "<= -0.5","min": -10.0, "max": -0.5, "type": "band"},
        {"key": "sector_week",    "label": "sector week",  "cond_min": "",       "cond_max": "< 0",    "min": None,  "max": 0.0,  "type": "band", "strict": True},
        {"key": "week_index_52",  "label": "52w index",    "cond_min": ">= 20",  "cond_max": "<= 60",  "min": 20.0,  "max": 60.0, "type": "band"},
        {"key": "cmp_lt_pp",      "label": "CMP < PP",     "cond_min": "",       "cond_max": "",       "min": None,  "max": None, "type": "custom"},
        {"key": "s2_clearance",   "label": "S2 clearance", "cond_min": ">= 3%",  "cond_max": "",       "min": None,  "max": None, "type": "custom"},
        # cc#854: the true weekly RSI row is REMOVED — 9 filters become 8. Nothing else in this
        # list changes, and the sell_reversal block above keeps ITS twr<=45 row untouched.
    ],
    # BUY_MOMENTUM_V5 (cc#1051, spec 23186) — S1-PULLBACK rule set, name and slots unchanged.
    # mom_2d and true_weekly_rsi are SCORE BANDS ONLY now; s1_touch and pp_band are the new legs.
    # Historical note kept so the lineage is readable — the previous shape was (cc#502 -> cc#1045,
    # spec 22386) 7 cheap HARD gates + heavy
    # true_weekly_rsi[70,85] FINAL stage. V4 adds mom_2d [0,4] as its ONE hard gate; it was
    # score-band-only in V3 and keeps its score band as well. The sector_week hard gate drafted in
    # 22375 was DROPPED by the founder before any trading day ran under it — sector_week stays a
    # SCORE band (0,6) and nothing more.
    # (A separate SCORE>=7-of-10 V2-band layer also applies; it is a second layer, not a funnel gate.)
    # Order mirrors the handler's evaluation order, which is what the funnel renders.
    "buy_momentum": [
        {"key": "dma_50",         "label": "dma 50",       "cond_min": ">= 5",  "cond_max": "<= 12", "min": 5.0,  "max": 12.0, "type": "band"},
        {"key": "dma_20",         "label": "dma 20",       "cond_min": "> 0",   "cond_max": "",      "min": 0.0,  "max": None, "type": "band", "strict": True},
        {"key": "week_index_52",  "label": "52w index",    "cond_min": ">= 75", "cond_max": "",      "min": 75.0, "max": None, "type": "band"},
        {"key": "gvm_score",      "label": "gvm score",    "cond_min": ">= 7",  "cond_max": "",      "min": 7.0,  "max": None, "type": "band"},
        {"key": "day_1d",         "label": "day change",   "cond_min": "> 0",   "cond_max": "<= 2",  "min": 0.0,  "max": 2.0,  "type": "band", "strict": True},
        {"key": "s1_touch",       "label": "S1 touch (last 3 sessions or today)", "cond_min": "<= S1", "cond_max": "", "min": None, "max": None, "type": "custom"},
        {"key": "pp_band",        "label": "above PP by",  "cond_min": ">= 0",  "cond_max": "<= 1.5%","min": 0.0,  "max": 1.5,  "type": "custom"},
        # cc#1076: the label said "from ~10:15", which read as an ANCHOR — a baseline fixed at
        # 10:15 that the price is compared against all day. It never was. _load_hourly_fut has
        # always computed close(rn=1) vs close(rn=13), a ROLLING 60 minutes, and the label was
        # describing the moment the value first EXISTS rather than what it measures. That reading
        # is what made a 0-pass funnel at 10:10 look like a broken gate instead of a window that
        # had not opened yet. Wording now states the measure, not the clock.
        {"key": "hourly_pct",     "label": "hourly % (rolling 60m)", "cond_min": "> 0", "cond_max": "NOT NULL", "min": 0.0, "max": None, "type": "custom"},
    ],
}

# Header-pill spec label per basket (dashboard reads this via /api/v8/filters).
BASKET_SPEC = {
    "buy_reversal":  {"version": "V6.1", "cc": "cc#754", "label": "Buy Reversal V6.1"},
    "sell_reversal": {"version": "V7-B", "cc": "cc#1100", "label": "Sell Reversal V7-B"},
    "sell_momentum": {"version": "V4",   "cc": "cc#502", "label": "Sell Momentum V4"},
    "buy_momentum":  {"version": "V5",   "cc": "cc#1051", "label": "Buy Momentum V5"},
}

# FILTER_CONFIG-shape derivation: the v8_metrics-column ("band") subset per basket, {key: [min,max]}.
# v8_endpoints.py builds its FILTER_CONFIG from this (replacing the hand-maintained basket dicts).
# Excludes "custom" legs (s1_touch/r1_touch/room/cmp_lt_pp/s2_clearance — not v8_metrics columns) AND
# the heavy true_weekly_rsi stage (computed per-symbol, never a stored column). NOTE: buy_momentum's
# FILTER_CONFIG is its SEPARATE SCORE-band layer (dma_50[8,25]…), NOT these hard-gate funnel bounds
# (dma_50[5,12]…) — v8_endpoints.py keeps that one literal; this helper is used for the other 3.
def basket_filter_config(basket: str) -> dict:
    return {f["key"]: [f["min"], f["max"]] for f in BASKET_FILTERS.get(basket, [])
            if f["type"] == "band" and not f.get("heavy")}

# Funnel/display stage tuples (key, label, cond_min, cond_max) — the endpoints' stage lists.
def basket_stage_rows(basket: str) -> list:
    return [(f["key"], f["label"], f.get("cond_min", ""), f.get("cond_max", "")) for f in BASKET_FILTERS.get(basket, [])]

# Live funnel keys the handler is expected to write for a basket (parity-check target, cc#599).
def basket_funnel_keys(basket: str) -> set:
    return {f["key"] for f in BASKET_FILTERS.get(basket, [])}


# -- cc#714: S1-RECLAIM OBSERVATION BASKET (ring-fenced, paper-only, 10-trading-day sunset) --------
# A 5-min S1-reclaim LONG with SWING exits (target=R1 frozen, stop=1:1 mirror frozen, 21-cal-day hard
# timeout — see v8_paper.run_paper_exits). RING-FENCED: own basket tag `s1_reclaim_obs`, dedicated
# 2-concurrent cap, EXCLUDED from the standard slot pools + the V8 book aggregate (v8_endpoints
# day-wise perf). Ships DISABLED — set app_config `s1_reclaim_obs_enabled`='1' to start the live
# observation window (founder-controlled; Claude-web reviews daily, decides extend/kill). Backtest
# verdict session_log id=9831; relocated out of V14 (whose 15:15 square-off would truncate the swing).
S1REC_BASKET = "s1_reclaim_obs"
S1REC_MAX_CONCURRENT = 2
S1REC_SUNSET_TRADING_DAYS = 10
S1REC_TIMEOUT_CAL_DAYS = 21   # enforced in v8_paper.run_paper_exits (basket-scoped)

# ── cc#1100 · THE V7-B SHADOW TAG IS WITHDRAWN ────────────────────────────────────────────────
# It existed for about eight hours. The card first asked for a record-only shadow behind the
# s1_reclaim_obs ring-fence; the founder then ruled 19-Aug that V7-B REPLACES the V6.1 filter set
# on the LIVE sell_reversal basket — same tag, same slots, same book (relayed in cc_task_logs
# 2793). So `sell_reversal_v7b` is gone as a basket name: keeping it would have split one book
# across two tags and every P&L surface would have had to learn about the second one.
#
# The caveat that motivated the shadow is NOT gone and is recorded where it belongs, in the live
# handler's docstring: the evidence is a replay gated on PRIOR-DAY EOD metrics while this writer
# evaluates every 5 minutes. Under STRATEGY_PHASE_MODEL_V1 (session_log 26386) that is a phase-2
# entry criterion — a BT7 parity run — not a reason to hold the founder's ruling.

# Baskets that are ring-fenced OUT of the standard slot pools, both ways. Read by
# _auto_paper_entry. A name here takes no standard slot and is never counted toward one.
_SLOT_EXEMPT_BASKETS = {S1REC_BASKET}


def _s1rec_enabled(conn) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='s1_reclaim_obs_enabled'")
            r = cur.fetchone()
        return bool(r and str(r[0]).strip().lower() in ("1", "true", "on", "yes"))
    except Exception:
        return False


def _s1rec_sunset_reached(conn, target_date, sim_ts=None) -> bool:
    """Stamp the first LIVE tick date once; block NEW entries once >= S1REC_SUNSET_TRADING_DAYS
    distinct raw_prices trading days have elapsed since. Fail-CLOSED (block) on error so the
    observation window can never silently over-run."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='s1_reclaim_obs_first_tick'")
            r = cur.fetchone()
            if not r or not r[0]:
                if sim_ts is None:   # live only stamps the anchor; sim/backtest never does
                    cur.execute("""INSERT INTO app_config (key, value) VALUES ('s1_reclaim_obs_first_tick', %s)
                                   ON CONFLICT (key) DO NOTHING""", (str(target_date),))
                    conn.commit()
                return False
            cur.execute("""SELECT COUNT(DISTINCT price_date) FROM raw_prices
                           WHERE price_date > %s AND price_date <= %s""", (r[0], target_date))
            return int(cur.fetchone()[0]) >= S1REC_SUNSET_TRADING_DAYS
    except Exception as e:
        log.warning(f"s1_reclaim_obs sunset check: {e}")
        try: conn.rollback()
        except Exception: pass
        return True


def _s1rec_prior5_low_touch(conn, sym, s1, today) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT MIN(low) FROM (SELECT low FROM raw_prices
                           WHERE symbol=%s AND price_date < %s ORDER BY price_date DESC LIMIT 5) x""",
                        (sym, today))
            r = cur.fetchone()
        return bool(r and r[0] is not None and float(r[0]) <= s1)
    except Exception:
        return False


def _s1rec_last2_closes(conn, sym, today, cut) -> list:
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT close FROM intraday_prices
                           WHERE symbol=%s AND ts::date=%s AND source='fyers_eq' AND ts <= %s
                           ORDER BY ts DESC LIMIT 2""", (sym, today, cut))
            return [float(r[0]) for r in cur.fetchall() if r[0] is not None]
    except Exception:
        return []


def _s1rec_recent_entry(conn, sym, today) -> bool:
    """One entry per symbol per day + 5-trading-session per-symbol cooldown: block if any
    s1_reclaim_obs paper entry (open OR since-closed) exists within the last 5 trading sessions
    (inclusive of today). Fail-CLOSED (block) on error — never double-enter."""
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT MIN(price_date) FROM (SELECT DISTINCT price_date FROM raw_prices
                           WHERE price_date <= %s ORDER BY price_date DESC LIMIT 5) d""", (today,))
            r = cur.fetchone()
            floor_d = r[0] if r and r[0] else today
            cur.execute("SELECT 1 FROM v8_paper_positions WHERE symbol=%s AND basket=%s AND entry_ts::date >= %s LIMIT 1",
                        (sym, S1REC_BASKET, floor_d))
            if cur.fetchone(): return True
            cur.execute("SELECT 1 FROM v8_paper_trades WHERE symbol=%s AND basket=%s AND entry_ts::date >= %s LIMIT 1",
                        (sym, S1REC_BASKET, floor_d))
            return cur.fetchone() is not None
    except Exception:
        return True


def _write_s1_reclaim_obs_qualified(conn, all_metrics: List[dict], target_date: date,
                                    gate_fails, pivots, signal_ts_ist, sim_ts=None):
    """cc#714 observation basket — see the block header above. Same handler signature/dispatch as
    the four live baskets, so it inherits _write_qualified's per-basket try/except isolation."""
    if not _s1rec_enabled(conn):
        return
    if gate_fails > 2:                      # precondition: V8 mood gate OPEN (not the most-defensive posture)
        return
    if _s1rec_sunset_reached(conn, target_date, sim_ts=sim_ts):
        log.info("s1_reclaim_obs: sunset reached (>=10 trading days) — no new entries; open positions ride")
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM v8_paper_positions WHERE status='OPEN' AND basket=%s", (S1REC_BASKET,))
            open_obs = int(cur.fetchone()[0])
    except Exception as e:
        log.warning(f"s1_reclaim_obs cap check: {e}")
        return
    if open_obs >= S1REC_MAX_CONCURRENT:
        return

    today = _today(sim_ts)
    cut = _bar_cutoff(sim_ts)
    fired = 0
    for s in all_metrics:
        if open_obs >= S1REC_MAX_CONCURRENT:
            break
        sym = s["symbol"]
        cmp = s.get("_cmp")
        pv = pivots.get(sym)
        if not cmp or not pv:
            continue
        s1, r1 = pv.get("s1"), pv.get("r1")
        if s1 is None or r1 is None:
            continue
        # (0915 EOD-frozen preconditions) rsi_month>70, gvm>=6.5, prev close>=200DMA (dma_200>=0)
        rm, gv, d200 = s.get("rsi_month"), s.get("gvm_score"), s.get("dma_200")
        if rm is None or float(rm) <= 70.0: continue
        if gv is None or float(gv) < 6.5: continue
        if d200 is None or float(d200) < 0.0: continue
        # room: (R1 - bar close)/close > 2%
        if (r1 - cmp) / cmp * 100.0 <= 2.0: continue
        # S1 touched: prior-5-session low <= S1 OR today's running low <= S1
        day_lo = s.get("day_low")
        touched = (day_lo is not None and float(day_lo) <= s1) or _s1rec_prior5_low_touch(conn, sym, s1, today)
        if not touched: continue
        # reclaim bar: current 5-min close > S1 AND previous 5-min close <= S1*1.001 (tolerance)
        closes = _s1rec_last2_closes(conn, sym, today, cut)
        if len(closes) < 2: continue
        cur_c, prev_c = closes[0], closes[1]
        if not (cur_c > s1 and prev_c <= s1 * 1.001): continue
        # one entry per symbol per day + 5-session cooldown
        if _s1rec_recent_entry(conn, sym, today): continue

        entry = round(cur_c, 2)
        tgt = round(r1, 2)
        stop = round(entry - (r1 - entry), 2)   # 1:1 mirror below entry (LONG), frozen
        snap = {"s1": s1, "r1": r1, "prev_close": prev_c, "rsi_month": rm, "gvm_score": gv,
                "dma_200": d200, "room_pct": round((r1 - cur_c) / cur_c * 100.0, 2),
                "target": tgt, "stop": stop, "observation": True, "spec": "S1RECLAIM_OBS cc#714"}
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO v8_qualified
                    (symbol, basket, signal_date, signal_ts, gvm_score, cmp, rsi_month, dma_200, metrics, source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'live_5min')
                    ON CONFLICT (symbol, basket, signal_date) DO NOTHING""",
                    (sym, S1REC_BASKET, target_date, signal_ts_ist, gv, cur_c, rm, d200, json.dumps(snap)))
            conn.commit()
        except Exception as e:
            log.warning(f"s1_reclaim_obs qualified {sym}: {e}")
            try: conn.rollback()
            except Exception: pass
        try:
            _auto_paper_entry(conn, sym, S1REC_BASKET, "BUY", cur_c, pv,
                              target_date, gate_fails, sim_ts=sim_ts, target=tgt, stop=stop)
            open_obs += 1; fired += 1
        except Exception as e:
            log.warning(f"s1_reclaim_obs entry {sym}: {e}")
    if fired:
        log.info(f"s1_reclaim_obs: {fired} observation entr{'y' if fired == 1 else 'ies'} this tick (cap {S1REC_MAX_CONCURRENT})")


def _write_qualified(conn, all_metrics: List[dict], target_date: date, sim_ts=None, v21_backtest=False):
    """cc#502: the generic FILTER_CONFIG score-gate loop is retired — all four baskets are now
    dedicated strict-AND handlers (zero baskets were left running through it). This is now just
    the shared per-tick setup (mood gate for slots, pivots, slot-block accumulator) followed by
    the four dedicated handler calls and the end-of-tick slot-burst flush. v21_backtest is kept
    on the signature for external callers (BT7/replay) even though nothing here reads it anymore
    -- the V2.1 hard-gate subsystem (v8_endpoints.V21_FILTERS / v21_hard_gate_pass) is untouched
    infrastructure, simply no longer called by any basket handler."""
    gate_fails = _market_gate_fails(conn, sim_ts=sim_ts)
    pivots     = _load_pivots(conn, sim_ts=sim_ts)

    signal_ts_ist = _now_ist(sim_ts)   # cc#218
    _reset_slot_blocks()   # cc#256: fresh per-tick slot_full accumulator

    # cc#580 fault_1: isolate each basket handler so ONE bad basket (or one bad symbol inside it)
    # cannot abort the other three or the rest of the tick. On a handler exception, rollback the
    # aborted txn so the next handler + the end-of-tick heartbeat can still commit ("tick advances
    # on partial failure"). The compute loop + metrics upsert are already per-symbol guarded.
    for _handler, _bname in (
        (_write_buy_reversal_v6_qualified,  "buy_reversal_v6"),
        (_write_sell_reversal_v7b_qualified, "sell_reversal"),
        (_write_sell_momentum_v4_qualified,  "sell_momentum_v4"),
        (_write_buy_momentum_v3_qualified,   "buy_momentum_v3"),
        (_write_s1_reclaim_obs_qualified,    "s1_reclaim_obs"),   # cc#714: ring-fenced observation (enable-gated)
    ):
        try:
            _handler(conn, all_metrics, target_date,
                     gate_fails, pivots, signal_ts_ist, sim_ts=sim_ts)
        except Exception as _he:
            log.error(f"_write_qualified: basket {_bname} failed — {_he}", exc_info=True)
            try:
                conn.rollback()   # clear the aborted txn so the next basket + heartbeat commit
            except Exception:
                pass

    # cc#256: tick complete — flush a slot_full_burst alert if any pool piled up. Live only
    # (sim_ts is None); replay/bt7 ticks accumulate harmlessly but never emit alerts.
    if sim_ts is None:
        _flush_slot_blocks(conn, signal_ts_ist)




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

    _add_sector_aggregates(computed, eod_metrics, conn)

    # cc#217 P3: one batched upsert + one commit for the whole tick (was 212 sequential
    # INSERT+COMMIT). _upsert_metrics_batch preserves the cc#218 skip-bad-symbol guarantee via
    # a batch savepoint + per-symbol fallback on batch failure.
    _t_upsert = perf_counter()
    written = _upsert_metrics_batch(conn, computed, today, sim_ts=sim_ts)   # cc#1011: capture the count
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

    # cc#580 fault_1: the metrics upsert already committed above. Make the downstream phases
    # (qualified baskets, ADR, sector aggregates) NON-FATAL so a failure in any of them can never
    # stop the tick from completing + stamping the heartbeat — a silent writer death is the exact
    # 20-Jul failure mode. Each phase rolls back its own aborted txn so the next one can commit.
    try:
        _write_qualified(conn, all_metrics, today, sim_ts=sim_ts, v21_backtest=v21_backtest)
    except Exception as _qe:
        log.error(f"signal_writer: _write_qualified failed — {_qe}", exc_info=True)
        try: conn.rollback()
        except Exception: pass

    try:
        _write_adr_intraday(conn, sim_ts=sim_ts)
        # cc#1011: the separate sector pass is RETIRED, and cc#1102 deleted its body.
        # _add_sector_aggregates sets the theme values on `computed` (on an isolated connection), so
        # the metrics UPSERT above already writes them to v8_metrics — the second pass was redundant
        # AND, by running on the tick's OWN connection, was a way to re-poison it after the write.
        # Table and gates now both come from the one isolated computation.
    except Exception as _ae:
        log.error(f"signal_writer: adr update failed — {_ae}", exc_info=True)
        try: conn.rollback()
        except Exception: pass

    # cc#1011 HEARTBEAT HONESTY: "ok" — the heartbeat, the scheduler status, and the MCP return —
    # MUST be gated on an ACTUAL write. On 11-Aug the tick computed 208 symbols but wrote 0 (a
    # poisoned connection failed the upsert), yet the heartbeat stamped alive and the scheduler logged
    # ok, so the writer ran dark for an hour with every watchdog green — the exact watchdog-blindness
    # class from the feed-incident manual. A tick that computed symbols but wrote NONE is a FAILURE:
    # do NOT stamp the heartbeat (so run_diagnosis/the watchdog sees it stale and restarts), page
    # loudly, and RAISE so the scheduler records last_status=error instead of ok.
    if computed and not written:
        try:
            conn.rollback()   # clear any aborted state so the ops_log write itself can land
        except Exception:
            pass
        log.critical(f"signal_writer: WROTE 0/{len(computed)} rows despite computing them — the "
                     f"metrics upsert failed (poisoned txn / lock). NOT stamping heartbeat.")
        try:
            _ops_log(conn, "critical", "signal_writer_zero_write",
                     {"computed": len(computed), "written": 0, "date": str(today)})
        except Exception:
            pass
        raise RuntimeError(f"signal_writer wrote 0/{len(computed)} rows — write failure, not a "
                           f"healthy tick (heartbeat withheld; see signal_writer_zero_write)")

    log.info(f"signal_writer: {written}/{len(computed)} written, {no_bar} no_bar, source=live_5min")
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
        "updated": written,            # cc#1011: rows actually WRITTEN, not merely computed
        "computed": len(computed),     # cc#1011: kept for visibility (written < computed => partial)
        "no_bar":  no_bar,
        "total":   len(symbols),
        "source":  "live_5min",
    }
