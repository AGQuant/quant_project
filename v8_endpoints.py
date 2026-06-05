"""
V8 endpoints — Quant Long-Short Basket Strategy

Endpoints:
  GET /api/v8/market_mood            — ADR + Nifty D/W/M + auto Buy/Sell slot allocation
  GET /api/v8/qualified/{basket}     — Pure read from v8_qualified (compute-on-write)
  GET /api/v8/filter_config/{basket} — Min/Max thresholds per basket
  GET /api/v8/funnel/{basket}        — Waterfall step counts from v8_funnel_counts
  GET /api/v8/adr                    — Quick ADR-only read from adr_daily
  GET /api/v8/raw                    — All active futures x 21 metrics (raw data tab)
  GET /api/v8/sell_overbought        — Failed breakout / exhaustion reversal (live compute)
  GET /api/v8/positions              — Open trades from personal_journal (V8 native)
  GET /api/v8/trades                 — Closed trades from personal_journal (V8 native)

Architecture: compute-on-write.
  - v8_signal_writer.py writes v8_qualified every 5-min during market hours
  - v8_engine.py writes v8_qualified + v8_signal_history + v8_funnel_counts at EOD
  - This file = pure reads only. Zero compute on the read path.

Filters removed:
  sector_day + sector_week — 05-Jun-2026: not meaningful for 208-stock universe
  month_index              — 05-Jun-2026: RSI month already covers this signal

day_change formula (05-Jun-2026):
  EOD:  (latest_close / close_2_days_ago - 1) * 100
  Live: (cmp / close_2_days_ago - 1) * 100
  2-day momentum. Example: CGPOWER 932 / 906.65 = +2.79%
"""

from fastapi import APIRouter, HTTPException
from datetime import date, timedelta
from typing import Optional
import psycopg
import os

router = APIRouter(prefix="/api/v8", tags=["v8"])

def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


# ── Filter configs ────────────────────────────────────────────────────────────
# Canonical source of truth. Used by v8_engine + v8_signal_writer to write signals.

FILTER_CONFIG = {
    "buy_reversal": {
        "gvm_score":   [7.0,  10.0],
        "year_return": [-1.5, None],
        "dma_200":     [1.5,  20.0],
        "dma_50":      [1.5,  8.0],
        "rsi_month":   [58.5, 75.0],
        "rsi_weekly":  [50.0, 67.5],
        "month_return":[0.0,  7.2],
        "week_return": [1.5,  3.0],
        "day_change":  [0.0,  2.4],
    },
    "buy_momentum": {
        "gvm_score":   [7.0,  10.0],
        "year_return": [0.0,  None],
        "dma_200":     [7.0,  50.0],
        "dma_50":      [6.5,  25.0],
        "rsi_month":   [71.5, 80.0],
        "rsi_weekly":  [71.5, 80.0],
        "month_return":[3.0,  14.0],
        "week_return": [1.0,  7.0],
        "day_change":  [0.0,  3.5],
    },
    "sell_reversal": {
        "dma_200":     [-30.0, 2.0],
        "dma_50":      [-20.0, 2.0],
        "rsi_month":   [20.0,  60.0],
        "rsi_weekly":  [10.0,  45.0],
        "month_return":[-20.0, 2.0],
        "week_return": [-6.0,  3.0],
        "range_3d":    [None,  -1.0],
    },
    "sell_momentum": {
        "dma_200":     [-50.0, 0.0],
        "dma_50":      [-30.0, 0.0],
        "dma_20":      [None,  -2.0],
        "rsi_month":   [10.0,  45.0],
        "rsi_weekly":  [5.0,   60.0],
        "daily_rsi":   [None,  40.0],
        "month_return":[-30.0, 0.0],
        "week_return": [-8.0,  0.0],
        "day_change":  [-3.0,  0.0],
        "range_3d":    [-10.0, -1.0],
        "week_index_52":[None, 20.0],
    },
    "sell_overbought": {
        "dma_200":     [10.0, None],
        "week_index_52":[80.0, None],
        "rsi_month":   [60.0, None],
        "day_change":  [None, 0.0],
    },
}

BASKET_META = {
    "buy_reversal":    {"side": "BUY",  "target": "S1", "win_pct": "~65%", "signals_per_day": "~2"},
    "buy_momentum":    {"side": "BUY",  "target": "S1", "win_pct": "~65%", "signals_per_day": "~2"},
    "sell_reversal":   {"side": "SELL", "target": "S2", "win_pct": "57%",  "signals_per_day": "~2/week"},
    "sell_momentum":   {"side": "SELL", "target": "S2", "win_pct": "83%",  "signals_per_day": "~1.5"},
    "sell_overbought": {"side": "SELL", "target": "S1", "win_pct": "71%",  "signals_per_day": "~3"},
}

_BLACKOUT_SQL = """
    symbol NOT IN (
        SELECT UPPER(ticker) FROM earnings_calendar
        WHERE ex_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day')
    )
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pivot_s1_s2(prev_high, prev_low, prev_close):
    pp = (prev_high + prev_low + prev_close) / 3
    s1 = pp - (prev_high - pp)
    s2 = pp - (prev_high - prev_low)
    return pp, s1, s2


def _normalize_basket_to_strategy(basket: Optional[str]) -> str:
    if not basket:
        return ''
    mapping = {
        'buy_reversal':    'Buy Reversal',
        'buy_momentum':    'Buy Momentum',
        'sell_reversal':   'Sell Reversal',
        'sell_momentum':   'Sell Momentum',
        'sell_overbought': 'Sell Overbought',
    }
    return mapping.get(basket.lower(), basket)


def _live_qualified_fallback(basket: str, limit: int):
    config = FILTER_CONFIG[basket]
    where_clauses = [
        "score_date = (SELECT MAX(score_date) FROM v8_metrics)",
        _BLACKOUT_SQL,
    ]
    params = []
    for metric, bounds in config.items():
        mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
        if mn is not None:
            where_clauses.append(f"{metric} >= %s")
            params.append(mn)
        if mx is not None:
            where_clauses.append(f"{metric} <= %s")
            params.append(mx)
    where_sql = " AND ".join(where_clauses)
    sql = f"""
        SELECT symbol, gvm_score,
               dma_50, dma_200, rsi_month, rsi_weekly, daily_rsi,
               week_return, month_return, year_return,
               day_change, range_3d, week_index_52
        FROM v8_metrics
        WHERE {where_sql}
        ORDER BY gvm_score DESC NULLS LAST
        LIMIT %s
    """
    params.append(min(max(limit, 1), 200))
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _passes_filter(value, mn, mx) -> bool:
    if value is None: return False
    v = float(value)
    if mn is not None and v < mn: return False
    if mx is not None and v > mx: return False
    return True


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/market_mood")
def market_mood():
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT advances, declines, unchanged, adr, price_date
                FROM adr_daily ORDER BY price_date DESC LIMIT 1
            """)
            r = cur.fetchone()
            if r:
                advances, declines, unchanged = r[0] or 0, r[1] or 0, r[2] or 0
                adr      = round(float(r[3]), 3) if r[3] is not None else 1.0
                adr_date = str(r[4])
            else:
                advances, declines, unchanged = 0, 0, 0
                adr = 1.0; adr_date = "no_data"

            adr_pass = adr >= 1.0

            cur.execute("""
                SELECT price_date, close FROM raw_prices
                WHERE symbol = 'NIFTY50' ORDER BY price_date DESC LIMIT 30
            """)
            nifty = cur.fetchall()
            if len(nifty) < 22:
                nifty_day = nifty_week = nifty_month = None
                nifty_day_pass = nifty_week_pass = nifty_month_pass = False
            else:
                latest = float(nifty[0][1])
                prev   = float(nifty[1][1])
                week   = float(nifty[5][1]) if len(nifty) > 5 else float(nifty[-1][1])
                month  = float(nifty[21][1]) if len(nifty) > 21 else float(nifty[-1][1])
                nifty_day   = round((latest / prev - 1) * 100, 2)
                nifty_week  = round((latest / week - 1) * 100, 2)
                nifty_month = round((latest / month - 1) * 100, 2)
                nifty_day_pass   = nifty_day >= 0
                nifty_week_pass  = nifty_week >= 0
                nifty_month_pass = nifty_month >= 0

            checks = [
                {"filter": "ADR",         "value": adr,         "required": ">= 1", "pass": adr_pass},
                {"filter": "Nifty Day",   "value": nifty_day,   "required": ">= 0", "pass": nifty_day_pass},
                {"filter": "Nifty Week",  "value": nifty_week,  "required": ">= 0", "pass": nifty_week_pass},
                {"filter": "Nifty Month", "value": nifty_month, "required": ">= 0", "pass": nifty_month_pass},
            ]
            fails = sum(1 for c in checks if not c["pass"])

            if fails == 0:   buy_slots, sell_slots, mood = 10, 5,  "Strong Bullish"
            elif fails == 1: buy_slots, sell_slots, mood = 8,  7,  "Bullish"
            elif fails == 2: buy_slots, sell_slots, mood = 7,  8,  "Neutral"
            else:            buy_slots, sell_slots, mood = 5,  10, "Bearish"

            return {
                "checked_at": str(date.today()), "checks": checks,
                "fails": fails, "mood": mood,
                "buy_slots": buy_slots, "sell_slots": sell_slots, "total_slots": 15,
                "adr_detail": {"advances": advances, "declines": declines,
                               "unchanged": unchanged, "adr_date": adr_date, "source": "adr_daily"},
            }
    except Exception as e:
        raise HTTPException(500, f"market_mood failed: {e}")


@router.get("/filter_config/{basket}")
def filter_config(basket: str):
    basket = basket.lower()
    if basket not in FILTER_CONFIG:
        raise HTTPException(404, f"Unknown basket: {basket}. Valid: {list(FILTER_CONFIG.keys())}")
    config = FILTER_CONFIG[basket]
    rows = []
    for metric, bounds in config.items():
        mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
        rows.append({
            "metric":      metric,
            "min":         mn,
            "max":         mx,
            "min_display": "" if mn is None else mn,
            "max_display": "" if mx is None else mx,
        })
    meta = BASKET_META.get(basket, {})
    return {"basket": basket, "filters": rows, "count": len(rows), **meta}


@router.get("/qualified/{basket}")
def qualified(basket: str, limit: int = 50):
    basket = basket.lower()
    if basket == "sell_overbought":
        return sell_overbought(limit=limit)
    if basket not in FILTER_CONFIG:
        raise HTTPException(404, f"Unknown basket: {basket}")

    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                    symbol, gvm_score, cmp,
                    dma_50, dma_200, rsi_month, rsi_weekly, daily_rsi,
                    week_return, month_return,
                    day_change, range_3d, week_index_52,
                    source, signal_ts
                FROM v8_qualified
                WHERE basket = %s
                  AND signal_date = CURRENT_DATE
                  AND symbol NOT IN (
                      SELECT UPPER(ticker) FROM earnings_calendar
                      WHERE ex_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day')
                  )
                ORDER BY gvm_score DESC NULLS LAST
                LIMIT %s
            """, (basket, min(max(limit, 1), 200)))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        if not rows:
            rows = _live_qualified_fallback(basket, limit)
            source_note = 'live_fallback'
        else:
            source_note = rows[0].get('source', 'precomputed') if rows else 'precomputed'

        meta = BASKET_META.get(basket, {})
        return {"basket": basket, "count": len(rows), "stocks": rows, "source": source_note, **meta}
    except Exception as e:
        raise HTTPException(500, f"qualified failed: {e}")


@router.get("/funnel/{basket}")
def funnel_counts(basket: str):
    basket = basket.lower()
    if basket not in FILTER_CONFIG:
        raise HTTPException(404, f"Unknown basket: {basket}")

    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT counts FROM v8_funnel_counts
                WHERE basket = %s AND score_date = CURRENT_DATE
                ORDER BY computed_at DESC LIMIT 1
            """, (basket,))
            row = cur.fetchone()

        if row:
            counts = row[0] if isinstance(row[0], dict) else {}
            return {"basket": basket, "score_date": str(date.today()),
                    "counts": counts, "source": "precomputed"}

        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, gvm_score, dma_50, dma_200, dma_20,
                       rsi_month, rsi_weekly, daily_rsi,
                       month_return, week_return, year_return, day_change,
                       week_index_52, range_3d, ma9_vs_ma21, vol_ratio
                FROM v8_metrics
                WHERE score_date = (SELECT MAX(score_date) FROM v8_metrics)
            """)
            cols = [d[0] for d in cur.description]
            all_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        filters  = FILTER_CONFIG[basket]
        universe = all_rows[:]
        counts   = {}
        for metric, bounds in filters.items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            universe = [s for s in universe if _passes_filter(s.get(metric), mn, mx)]
            counts[metric] = len(universe)

        return {"basket": basket, "score_date": str(date.today()),
                "counts": counts, "source": "live_fallback"}
    except Exception as e:
        raise HTTPException(500, f"funnel failed: {e}")


@router.get("/raw")
def raw_metrics(limit: int = 250):
    sql = """
        SELECT m.symbol, m.score_date, m.gvm_score,
               m.dma_20, m.dma_50, m.dma_200,
               m.rsi_month, m.rsi_weekly, m.daily_rsi,
               m.month_return, m.week_return, m.year_return,
               m.month_index, m.week_index_52,
               m.day_change, m.range_3d,
               m.upper_bb, m.lower_bb,
               m.ma9_vs_ma21, m.vol_ratio
        FROM v8_metrics m
        JOIN futures_universe f ON f.symbol = m.symbol AND f.is_active = TRUE
        WHERE m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
        ORDER BY m.gvm_score DESC NULLS LAST
        LIMIT %s
    """
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (min(max(limit, 1), 300),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        score_date = rows[0]["score_date"] if rows else None
        return {"count": len(rows), "score_date": str(score_date) if score_date else None,
                "columns": cols, "stocks": rows}
    except Exception as e:
        raise HTTPException(500, f"raw_metrics failed: {e}")


@router.get("/sell_overbought")
def sell_overbought(limit: int = 50):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH price_window AS (
                    SELECT r.symbol, r.price_date, r.close, r.high, r.low, r.volume,
                           AVG(r.close)   OVER w9   AS ma9,
                           AVG(r.close)   OVER w21  AS ma21,
                           AVG(r.volume)  OVER w10  AS vol_avg10,
                           LAG(r.high,1)  OVER ws    AS prev_high,
                           LAG(r.low,1)   OVER ws    AS prev_low,
                           LAG(r.close,1) OVER ws    AS prev_close,
                           ROW_NUMBER()   OVER (PARTITION BY r.symbol ORDER BY r.price_date DESC) AS rn
                    FROM raw_prices r
                    JOIN futures_universe fu ON fu.symbol = r.symbol AND fu.is_active = TRUE
                    WHERE r.price_date >= CURRENT_DATE - INTERVAL '60 days'
                    WINDOW
                        w9  AS (PARTITION BY r.symbol ORDER BY r.price_date ROWS 8  PRECEDING),
                        w21 AS (PARTITION BY r.symbol ORDER BY r.price_date ROWS 20 PRECEDING),
                        w10 AS (PARTITION BY r.symbol ORDER BY r.price_date ROWS 9  PRECEDING),
                        ws  AS (PARTITION BY r.symbol ORDER BY r.price_date)
                ),
                latest AS (
                    SELECT pw.symbol,
                           pw.close   AS entry,
                           pw.ma9, pw.ma21, pw.vol_avg10, pw.volume,
                           pw.prev_high, pw.prev_low, pw.prev_close,
                           ROUND(((pw.ma9 - pw.ma21) / NULLIF(pw.ma21, 0) * 100)::numeric, 2)  AS ma9_vs_ma21,
                           ROUND((pw.volume / NULLIF(pw.vol_avg10, 0))::numeric, 2)             AS vol_ratio,
                           ROUND((((pw.prev_high + pw.prev_low + pw.prev_close) / 3)
                               - (pw.prev_high - (pw.prev_high + pw.prev_low + pw.prev_close) / 3))::numeric, 2) AS s1,
                           ROUND((((pw.prev_high + pw.prev_low + pw.prev_close) / 3)
                               - 2 * (pw.prev_high - (pw.prev_high + pw.prev_low + pw.prev_close) / 3))::numeric, 2) AS s2
                    FROM price_window pw
                    WHERE pw.rn = 1 AND pw.ma21 IS NOT NULL AND pw.volume > 0
                ),
                filtered AS (
                    SELECT l.*,
                           vm.dma_200, vm.week_index_52, vm.rsi_month, vm.daily_rsi,
                           vm.day_change, vm.gvm_score
                    FROM latest l
                    JOIN v8_metrics vm ON vm.symbol = l.symbol
                     AND vm.score_date = (SELECT MAX(score_date) FROM v8_metrics)
                    WHERE vm.dma_200        >= 10
                      AND vm.week_index_52  >= 80
                      AND l.ma9_vs_ma21     >= 3
                      AND l.vol_ratio       <= 0.8
                      AND vm.day_change      < 0
                      AND vm.rsi_month      >= 60
                      AND l.s1              <  l.entry
                      AND l.symbol NOT IN (
                          SELECT UPPER(ticker) FROM earnings_calendar
                          WHERE ex_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day')
                      )
                )
                SELECT symbol,
                    ROUND(entry::numeric, 2)                          AS entry,
                    s1                                                AS target,
                    ROUND((entry + (entry - s1))::numeric, 2)         AS stop,
                    ROUND(((entry - s1) / NULLIF(entry, 0) * 100)::numeric, 2) AS tgt_pct,
                    dma_200, week_index_52, ma9_vs_ma21, vol_ratio,
                    day_change,
                    ROUND(rsi_month::numeric, 1)  AS rsi_month,
                    ROUND(daily_rsi::numeric, 1)  AS daily_rsi,
                    ROUND(gvm_score::numeric, 2)  AS gvm_score
                FROM filtered
                ORDER BY dma_200 DESC NULLS LAST
                LIMIT %s
            """, (min(max(limit, 1), 200),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {
            "basket": "sell_overbought", "count": len(rows),
            "target": "S1", "sl": "entry + (entry - S1) — 1:1",
            "win_pct_may2026": "71.4%",
            "note": "Market gate required — fails in recovery/bull markets",
            "stocks": rows,
        }
    except Exception as e:
        raise HTTPException(500, f"sell_overbought failed: {e}")


@router.get("/adr")
def adr_only():
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT price_date, advances, declines, unchanged, adr
                FROM adr_daily ORDER BY price_date DESC LIMIT 1
            """)
            r = cur.fetchone()
        if not r:
            return {"adr": 0.0, "advances": 0, "declines": 0, "unchanged": 0,
                    "pass": False, "note": "adr_daily empty"}
        price_date, adv, dec, unc, adr_val = r
        adr = float(adr_val) if adr_val else 0.0
        return {"price_date": str(price_date), "adr": adr,
                "advances": adv, "declines": dec, "unchanged": unc, "pass": adr >= 1.0}
    except Exception as e:
        raise HTTPException(500, f"adr failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  PERSONAL JOURNAL — V8 native open + closed trades
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/positions")
def v8_positions(limit: int = 100):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                    pj.id, pj.trade_date, pj.entry_time, pj.symbol, pj.direction,
                    pj.entry_price, pj.qty, pj.sl, pj.target,
                    pj.v8_basket, pj.v8_signal_match, pj.setup_quality,
                    pj.rule_score_total, pj.notes,
                    COALESCE(cp.cmp, pj.entry_price) AS cmp,
                    CASE
                        WHEN UPPER(pj.direction) = 'LONG'  THEN ROUND(((COALESCE(cp.cmp, pj.entry_price) - pj.entry_price) * pj.qty)::numeric, 2)
                        WHEN UPPER(pj.direction) = 'SHORT' THEN ROUND(((pj.entry_price - COALESCE(cp.cmp, pj.entry_price)) * pj.qty)::numeric, 2)
                        ELSE 0
                    END AS unrealised_pnl
                FROM personal_journal pj
                LEFT JOIN cmp_prices cp ON cp.symbol = pj.symbol
                WHERE pj.exit_time IS NULL
                ORDER BY pj.entry_time DESC NULLS LAST
                LIMIT %s
            """, (min(max(limit, 1), 500),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            r['strategy'] = _normalize_basket_to_strategy(r.get('v8_basket'))
        return rows
    except Exception as e:
        raise HTTPException(500, f"v8_positions failed: {e}")


@router.get("/trades")
def v8_trades(limit: int = 200):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                    pj.id, pj.trade_date, pj.entry_time, pj.exit_time, pj.symbol, pj.direction,
                    pj.entry_price AS entry, pj.exit_price AS exit, pj.qty, pj.sl, pj.target,
                    pj.pnl, pj.result, pj.holding_days,
                    pj.v8_basket, pj.v8_signal_match, pj.setup_quality,
                    pj.rule_score_total, pj.rule_violations, pj.lesson, pj.notes
                FROM personal_journal pj
                WHERE pj.exit_time IS NOT NULL
                ORDER BY pj.exit_time DESC
                LIMIT %s
            """, (min(max(limit, 1), 1000),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            r['strategy'] = _normalize_basket_to_strategy(r.get('v8_basket'))
        return rows
    except Exception as e:
        raise HTTPException(500, f"v8_trades failed: {e}")
