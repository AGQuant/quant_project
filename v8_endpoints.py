"""
V8 endpoints — Quant Long-Short Basket Strategy
Display source for V8 Final CLS V Google Sheet.

5 endpoints:
  GET /api/v8/market_mood            — ADR + Nifty D/W/M + auto Buy/Sell slot allocation
  GET /api/v8/qualified/{basket}     — Stocks passing filters for a basket
  GET /api/v8/filter_config/{basket} — Min/Max thresholds per basket
  GET /api/v8/adr                    — Quick ADR-only refresh
  GET /api/v8/sell_overbought        — Failed breakout / exhaustion reversal signals

5 baskets: buy_reversal, buy_momentum, sell_reversal, sell_momentum, sell_overbought
Sell_Overbought: dma200≥10, wi52≥80, ma9_vs_ma21≥3%, vol_ratio≤0.8, r1d<0, rsi_month≥60
Target = S1 (pivot support 1), SL = entry + (entry - S1) = 1:1
Backtest May 2026: 71.4% win rate (15 signals, 10 wins, 4 losses)
Market gate handles April (recovery market — all sell strategies fail correctly).
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
# Format: metric -> [min, max]  (None = no bound)
# Sell_Overbought uses v5_metrics columns PLUS computed ma9_vs_ma21 & vol_ratio
# which are NOT in v5_metrics — handled separately in the /sell_overbought endpoint.

FILTER_CONFIG = {
    "buy_reversal": {
        "gvm_score":    [7.0,  10.0],
        "year_return":  [-1.5, None],
        "dma_200":      [1.5,  20.0],
        "dma_50":       [1.5,  8.0],
        "rsi_month":    [58.5, 75.0],
        "rsi_weekly":   [50.0, 67.5],
        "month_return": [0.0,  7.2],
        "week_return":  [1.5,  3.0],
        "sector_week":  [1.5,  5.0],
        "sector_day":   [0.0,  3.0],
        "month_index":  [50.0, 100.0],
        "range_1d":     [0.5,  2.4],
    },
    "buy_momentum": {
        "gvm_score":    [7.0,  10.0],
        "year_return":  [0.0,  None],
        "dma_200":      [7.0,  50.0],
        "dma_50":       [6.5,  25.0],
        "rsi_month":    [71.5, 80.0],
        "rsi_weekly":   [71.5, 80.0],
        "month_return": [3.0,  12.0],
        "week_return":  [1.0,  7.0],
        "sector_week":  [1.5,  5.0],
        "sector_day":   [0.0,  3.0],
        "month_index":  [50.0, 100.0],
        "range_1d":     [1.0,  3.5],
    },
    "sell_reversal": {
        "dma_200":      [-30.0, 2.0],
        "dma_50":       [-20.0, 2.0],
        "rsi_month":    [20.0,  60.0],
        "rsi_weekly":   [10.0,  45.0],
        "month_return": [-20.0, 2.0],
        "week_return":  [-6.0,  3.0],
        "sector_week":  [-12.0, -1.5],
        "sector_day":   [-3.0,  1.0],
        "month_index":  [0.0,   50.0],
        "range_3d":     [None,  -1.0],   # ≤ -1 locked Config B
    },
    "sell_momentum": {
        "dma_200":      [-50.0, 0.0],
        "dma_50":       [-30.0, 0.0],
        "dma_20":       [None,  -2.0],
        "rsi_month":    [10.0,  45.0],
        "rsi_weekly":   [5.0,   60.0],
        "daily_rsi":    [None,  40.0],
        "month_return": [-30.0, 0.0],
        "week_return":  [-8.0,  0.0],
        "sector_week":  [-10.0, -1.6],
        "sector_day":   [-3.0,  1.0],
        "month_index":  [0.0,   35.0],
        "range_1d":     [-3.0,  0.0],
        "range_3d":     [-10.0, -1.0],
        "week_index_52":[None,  20.0],
    },
    # Sell Overbought — computed live from raw_prices + v5_metrics
    # Config stored in v5_filters table; endpoint runs its own SQL
    "sell_overbought": {
        "dma_200":      [10.0, None],
        "week_index_52":[80.0, None],
        "rsi_month":    [60.0, None],
        "range_1d":     [None, 0.0],
        # ma9_vs_ma21 >= 3% and vol_ratio <= 0.8 computed in endpoint
    },
}

# Human-readable descriptions for display
BASKET_META = {
    "buy_reversal":    {"side": "BUY",  "target": "S1", "win_pct": "~65%", "signals_per_day": "~2"},
    "buy_momentum":    {"side": "BUY",  "target": "S1", "win_pct": "~65%", "signals_per_day": "~2"},
    "sell_reversal":   {"side": "SELL", "target": "S2", "win_pct": "57%",  "signals_per_day": "~2/week"},
    "sell_momentum":   {"side": "SELL", "target": "S2", "win_pct": "83%",  "signals_per_day": "~1.5"},
    "sell_overbought": {"side": "SELL", "target": "S1", "win_pct": "71%",  "signals_per_day": "~3"},
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pivot_s1_s2(prev_high, prev_low, prev_close):
    pp = (prev_high + prev_low + prev_close) / 3
    s1 = pp - (prev_high - pp)
    s2 = pp - (prev_high - prev_low)
    return pp, s1, s2


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/market_mood")
def market_mood():
    """
    Market Mood gate — 4 conditions:
      ADR (futures advance/decline ratio) >= 1
      Nifty Day return >= 0
      Nifty Week return >= 0
      Nifty Month return >= 0

    Slot allocation by # of fails (out of 4):
      0 fails -> Buy 10 / Sell 5
      1 fail  -> Buy 8  / Sell 7
      2 fails -> Buy 7  / Sell 8
      3+ fails -> Buy 5 / Sell 10
    Total = 15 always.
    """
    try:
        with _conn() as conn, conn.cursor() as cur:
            # ADR from intraday (today) or v5_metrics fallback
            cur.execute("""
                WITH today_change AS (
                    SELECT i.symbol,
                           (lc.close - fc.open) / NULLIF(fc.open, 0) * 100 AS day_pct
                    FROM (SELECT DISTINCT symbol FROM v5_signals) i
                    JOIN LATERAL (
                        SELECT close FROM intraday_prices
                        WHERE symbol = i.symbol AND ts::date = CURRENT_DATE
                        ORDER BY ts DESC LIMIT 1
                    ) lc ON true
                    JOIN LATERAL (
                        SELECT open FROM intraday_prices
                        WHERE symbol = i.symbol AND ts::date = CURRENT_DATE
                        ORDER BY ts ASC LIMIT 1
                    ) fc ON true
                )
                SELECT
                    COUNT(*) FILTER (WHERE day_pct > 0) AS advances,
                    COUNT(*) FILTER (WHERE day_pct < 0) AS declines,
                    COUNT(*) FILTER (WHERE day_pct = 0) AS unchanged
                FROM today_change
            """)
            r = cur.fetchone()
            advances, declines, unchanged = r[0] or 0, r[1] or 0, r[2] or 0

            # Fallback to v5_metrics prev_day_change if no intraday
            if (advances + declines) == 0:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE prev_day_change > 0) AS adv,
                        COUNT(*) FILTER (WHERE prev_day_change < 0) AS dec,
                        COUNT(*) FILTER (WHERE prev_day_change = 0) AS unc
                    FROM v5_metrics
                    WHERE score_date = (SELECT MAX(score_date) FROM v5_metrics)
                """)
                r = cur.fetchone()
                advances, declines, unchanged = r[0] or 0, r[1] or 0, r[2] or 0

            adr = round(advances / declines, 3) if declines > 0 else (999.0 if advances > 0 else 0.0)
            adr_pass = adr >= 1.0

            # Nifty returns from raw_prices
            cur.execute("""
                SELECT price_date, close
                FROM raw_prices
                WHERE symbol = 'NIFTY50'
                ORDER BY price_date DESC
                LIMIT 30
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
                "checked_at":  str(date.today()),
                "checks":      checks,
                "fails":       fails,
                "mood":        mood,
                "buy_slots":   buy_slots,
                "sell_slots":  sell_slots,
                "total_slots": 15,
                "adr_detail":  {"advances": advances, "declines": declines, "unchanged": unchanged},
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

    config = FILTER_CONFIG[basket]
    where_clauses = ["score_date = (SELECT MAX(score_date) FROM v5_metrics)"]
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
        SELECT
            symbol, gvm_score,
            dma_50, dma_200, rsi_month, rsi_weekly, daily_rsi,
            week_return, month_return, year_return,
            sector_day, sector_week, month_index,
            range_1d, range_3d, week_index_52
        FROM v5_metrics
        WHERE {where_sql}
        ORDER BY gvm_score DESC NULLS LAST
        LIMIT %s
    """
    params.append(min(max(limit, 1), 200))

    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        meta = BASKET_META.get(basket, {})
        return {"basket": basket, "count": len(rows), "stocks": rows, **meta}
    except Exception as e:
        raise HTTPException(500, f"qualified failed: {e}")


@router.get("/sell_overbought")
def sell_overbought(limit: int = 50):
    """
    Sell Overbought — Failed breakout / exhaustion reversal.
    Computes ma9_vs_ma21 and vol_ratio live from raw_prices.
    Target = S1 (pivot support 1), SL = entry + (entry - S1).
    Backtest May-2026: 71.4% win rate.

    Filters:
      dma_200       >= 10%       (extended above 200DMA)
      week_index_52 >= 80        (near 52-week high)
      ma9_vs_ma21   >= 3%        (short-term momentum stretched)
      vol_ratio     <= 0.8       (volume drying — exhaustion)
      range_1d      < 0          (today red — reversal starting)
      rsi_month     >= 60        (RSI elevated)
    """
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
                           -- Pivot S1
                           ROUND((((pw.prev_high + pw.prev_low + pw.prev_close) / 3)
                               - (pw.prev_high - (pw.prev_high + pw.prev_low + pw.prev_close) / 3))::numeric, 2) AS s1,
                           -- Pivot S2
                           ROUND((((pw.prev_high + pw.prev_low + pw.prev_close) / 3)
                               - 2 * (pw.prev_high - (pw.prev_high + pw.prev_low + pw.prev_close) / 3))::numeric, 2) AS s2
                    FROM price_window pw
                    WHERE pw.rn = 1
                      AND pw.ma21 IS NOT NULL
                      AND pw.volume > 0
                ),
                filtered AS (
                    SELECT l.*,
                           vm.dma_200, vm.week_index_52, vm.rsi_month, vm.daily_rsi,
                           vm.range_1d, vm.gvm_score, vm.sector_week
                    FROM latest l
                    JOIN v5_metrics vm
                      ON vm.symbol = l.symbol
                     AND vm.score_date = (SELECT MAX(score_date) FROM v5_metrics)
                    WHERE vm.dma_200      >= 10
                      AND vm.week_index_52 >= 80
                      AND l.ma9_vs_ma21   >= 3
                      AND l.vol_ratio     <= 0.8
                      AND vm.range_1d     <  0
                      AND vm.rsi_month    >= 60
                      AND l.s1            <  l.entry
                )
                SELECT
                    symbol,
                    ROUND(entry::numeric, 2)         AS entry,
                    s1                               AS target,
                    ROUND((entry + (entry - s1))::numeric, 2) AS stop,
                    ROUND(((entry - s1) / NULLIF(entry, 0) * 100)::numeric, 2) AS tgt_pct,
                    dma_200, week_index_52,
                    ma9_vs_ma21, vol_ratio,
                    range_1d,
                    ROUND(rsi_month::numeric, 1)    AS rsi_month,
                    ROUND(daily_rsi::numeric, 1)    AS daily_rsi,
                    ROUND(gvm_score::numeric, 2)    AS gvm_score,
                    ROUND(sector_week::numeric, 2)  AS sector_week
                FROM filtered
                ORDER BY dma_200 DESC NULLS LAST
                LIMIT %s
            """, (min(max(limit, 1), 200),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {
            "basket":     "sell_overbought",
            "count":      len(rows),
            "target":     "S1",
            "sl":         "entry + (entry - S1) — 1:1",
            "win_pct_may2026": "71.4%",
            "note":       "Market gate required — fails in recovery/bull markets",
            "stocks":     rows,
        }
    except Exception as e:
        raise HTTPException(500, f"sell_overbought failed: {e}")


@router.get("/adr")
def adr_only():
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE prev_day_change > 0) AS adv,
                    COUNT(*) FILTER (WHERE prev_day_change < 0) AS dec,
                    COUNT(*) FILTER (WHERE prev_day_change = 0) AS unc
                FROM v5_metrics
                WHERE score_date = (SELECT MAX(score_date) FROM v5_metrics)
            """)
            r = cur.fetchone()
            adv, dec, unc = r[0] or 0, r[1] or 0, r[2] or 0
            adr = round(adv / dec, 3) if dec > 0 else (999.0 if adv > 0 else 0.0)
            return {"adr": adr, "advances": adv, "declines": dec, "unchanged": unc, "pass": adr >= 1.0}
    except Exception as e:
        raise HTTPException(500, f"adr failed: {e}")
