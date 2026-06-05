"""
V8 Signal Writer — 5-min live signal engine
============================================
Lightweight job that runs every 5-min during market hours.

What it does:
  1. Reads v8_metrics (latest EOD row per symbol — slow metrics: RSI, DMA etc)
  2. Reads cmp_prices (live Fyers CMP) + raw_prices (close_2_days_ago)
  3. Computes live day_change = (cmp / close_2_days_ago - 1) * 100
     2-day momentum: current price vs close 2 trading days ago (05-Jun-2026)
  4. Overrides day_change in the metric row with the live value
  5. Applies FILTER_CONFIG to all symbols
  6. Writes results to v8_qualified (today only — clears + re-inserts per basket)

What it does NOT do:
  - Does NOT recompute RSI, DMA, Bollinger, or any slow metric
  - Does NOT touch v8_signal_history (EOD engine writes history, not live ticks)
  - Does NOT touch v8_funnel_counts (EOD engine writes funnel counts)
  - Does NOT make any external API calls (pure DB join)

Result: sub-second signal refresh every 5-min with zero external dependency.
Called from main.py _live_loop alongside run_live_tick.
"""

import logging
import json
from datetime import datetime, date, timezone, timedelta
from typing import Dict, List, Optional
import psycopg
import os

log = logging.getLogger("scorr.signal_writer")

IST = timezone(timedelta(hours=5, minutes=30))


def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _passes(value, mn, mx) -> bool:
    if value is None:
        return False
    v = float(value)
    if mn is not None and v < mn:
        return False
    if mx is not None and v > mx:
        return False
    return True


def run_live_signal_writer(conn) -> Dict:
    """
    Main entry point — called every 5-min from main.py _live_loop.
    Returns dict with counts per basket.
    """
    from v8_endpoints import FILTER_CONFIG

    today = datetime.now(IST).date()

    # ── Step 1: Load latest EOD metrics for all symbols ──
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    m.symbol, m.score_date,
                    m.gvm_score, m.dma_50, m.dma_200, m.dma_20,
                    m.rsi_month, m.rsi_weekly, m.daily_rsi,
                    m.month_return, m.week_return, m.year_return,
                    m.sector_day, m.sector_week, m.month_index, m.week_index_52,
                    m.range_3d, m.ma9_vs_ma21, m.vol_ratio,
                    m.day_change AS eod_day_change
                FROM v8_metrics m
                WHERE m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
            """)
            cols = [d[0] for d in cur.description]
            metrics_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"signal_writer: failed to load v8_metrics: {e}")
        return {"error": str(e)}

    if not metrics_rows:
        log.warning("signal_writer: no v8_metrics rows found")
        return {"qualified": {}}

    # ── Step 2: Load CMP + close_2_days_ago → compute live day_change ──
    # day_change = (cmp / close_2_days_ago - 1) * 100
    # close_2_days_ago = 2nd most recent close in raw_prices (OFFSET 1)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    c.symbol,
                    c.cmp,
                    r.close AS close_2d_ago,
                    ROUND(((c.cmp / NULLIF(r.close, 0) - 1) * 100)::numeric, 4) AS live_day_change
                FROM cmp_prices c
                JOIN LATERAL (
                    SELECT close FROM raw_prices
                    WHERE symbol = c.symbol
                    ORDER BY price_date DESC
                    OFFSET 1 LIMIT 1
                ) r ON true
            """)
            cmp_rows = cur.fetchall()
            live_cmp = {
                r[0]: {
                    'cmp': _safe_float(r[1]),
                    'close_2d_ago': _safe_float(r[2]),
                    'live_day_change': _safe_float(r[3])
                } for r in cmp_rows
            }
    except Exception as e:
        log.error(f"signal_writer: failed to load CMP: {e}")
        live_cmp = {}

    # ── Step 3: Merge live day_change into metrics rows ──
    all_metrics = []
    for m in metrics_rows:
        sym = m['symbol']
        live = live_cmp.get(sym, {})
        m['day_change'] = live.get('live_day_change') if live.get('live_day_change') is not None else m.get('eod_day_change')
        m['cmp'] = live.get('cmp')
        all_metrics.append(m)

    # ── Step 4: Apply filters + write to v8_qualified ──
    results = {'qualified': {}, 'total': 0, 'source': 'live_5min'}
    for basket, filters in FILTER_CONFIG.items():
        if basket == 'sell_overbought':
            continue

        qualified = [s for s in all_metrics if all(
            _passes(s.get(metric), bounds[0] if isinstance(bounds, list) else bounds[0],
                    bounds[1] if isinstance(bounds, list) else bounds[1])
            for metric, bounds in filters.items()
        )]

        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM v8_qualified WHERE basket=%s AND signal_date=%s",
                            (basket, today))
            conn.commit()
        except Exception as e:
            log.warning(f"signal_writer: clear {basket}: {e}")

        for s in qualified:
            sym = s['symbol']
            metrics_snap = {k: s.get(k) for k in [
                'gvm_score','dma_50','dma_200','rsi_month','rsi_weekly',
                'month_return','week_return','day_change'
            ]}
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO v8_qualified
                        (symbol, basket, signal_date, signal_ts, gvm_score, cmp,
                         day_change, week_return, month_return, dma_200, dma_50,
                         rsi_month, rsi_weekly, sector_week, sector_day, month_index,
                         week_index_52, daily_rsi, range_3d, metrics, source)
                        VALUES (%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (symbol, basket, signal_date) DO UPDATE SET
                            signal_ts=NOW(), cmp=EXCLUDED.cmp,
                            day_change=EXCLUDED.day_change,
                            metrics=EXCLUDED.metrics, source=EXCLUDED.source
                    """, (
                        sym, basket, today,
                        s.get('gvm_score'), s.get('cmp'),
                        s.get('day_change'), s.get('week_return'), s.get('month_return'),
                        s.get('dma_200'), s.get('dma_50'),
                        s.get('rsi_month'), s.get('rsi_weekly'),
                        s.get('sector_week'), s.get('sector_day'),
                        s.get('month_index'), s.get('week_index_52'),
                        s.get('daily_rsi'), s.get('range_3d'),
                        json.dumps(metrics_snap), 'live_5min'
                    ))
                conn.commit()
            except Exception as e:
                log.warning(f"signal_writer: insert {basket} {sym}: {e}")

        results['qualified'][basket] = len(qualified)
        results['total'] += len(qualified)

    log.info(f"signal_writer: {results['qualified']} signals written (live_5min)")
    return results
