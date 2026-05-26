"""
V8 endpoints — Quant Long-Short Basket Strategy
Display source for V8 Final CLS V Google Sheet.

4 endpoints:
  GET /api/v8/market_mood          — ADR + Nifty D/W/M + auto Buy/Sell slot allocation
  GET /api/v8/qualified/{basket}   — Stocks passing filters for a basket
  GET /api/v8/filter_config/{basket} — Min/Max thresholds per basket
  GET /api/v8/adr                  — Quick ADR-only refresh

4 baskets: buy_reversal, buy_momentum, sell_reversal, sell_momentum
Filter thresholds from V6 FINAL_OPTIMIZED (backtest run_20260525_000300)
GVM >= 7 added as Buy quality gate. Universe: 290 F&O stocks.
"""

from fastapi import APIRouter, HTTPException
from datetime import date, timedelta
from typing import Optional
import psycopg
import os

router = APIRouter(prefix="/api/v8", tags=["v8"])

def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))

# V6 FINAL_OPTIMIZED filter config — locked from backtest 25-May-26
FILTER_CONFIG = {
    "buy_reversal": {
        "gvm_score":   [7.0, 10.0],
        "year_return": [-1.5, None],
        "dma_200":     [1.5, 20.0],
        "dma_50":      [1.5, 8.0],
        "rsi_month":   [58.5, 75.0],
        "rsi_weekly":  [50.0, 67.5],
        "month_return":[0.0, 7.2],
        "week_return": [1.5, 3.0],
        "sector_week": [1.5, 5.0],
        "sector_day":  [0.0, 3.0],
        "month_index": [50.0, 100.0],
        "range_1d":    [0.5, 2.4],
    },
    "buy_momentum": {
        "gvm_score":   [7.0, 10.0],
        "year_return": [0.0, None],
        "dma_200":     [7.0, 50.0],
        "dma_50":      [6.5, 25.0],
        "rsi_month":   [71.5, 80.0],
        "rsi_weekly":  [71.5, 80.0],
        "month_return":[3.0, 12.0],
        "week_return": [1.0, 7.0],
        "sector_week": [1.5, 5.0],
        "sector_day":  [0.0, 3.0],
        "month_index": [50.0, 100.0],
        "range_1d":    [1.0, 3.5],
    },
    "sell_reversal": {
        "year_return": [None, None],
        "dma_200":     [-25.0, 0.0],
        "dma_50":      [-15.0, 0.5],
        "rsi_month":   [24.0, 55.0],
        "rsi_weekly":  [15.0, 35.0],
        "month_return":[-15.0, 0.0],
        "week_return": [-4.0, 1.0],
        "sector_week": [-10.0, -1.5],
        "sector_day":  [-1.4, 0.0],
        "month_index": [0.0, 35.0],
        "range_1d":    [-2.0, -1.5],
    },
    "sell_momentum": {
        "year_return": [None, None],
        "dma_200":     [-40.0, -2.6],
        "dma_50":      [-25.0, -1.0],
        "rsi_month":   [15.0, 35.0],
        "rsi_weekly":  [10.0, 55.0],
        "month_return":[-25.0, -2.0],
        "week_return": [-5.6, -1.0],
        "sector_week": [-8.0, -1.2],
        "sector_day":  [-1.6, 0.0],
        "month_index": [0.0, 24.5],
        "range_1d":    [-2.4, -0.5],
    },
}

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

            if fails == 0:
                buy_slots, sell_slots, mood = 10, 5, "Strong Bullish"
            elif fails == 1:
                buy_slots, sell_slots, mood = 8, 7, "Bullish"
            elif fails == 2:
                buy_slots, sell_slots, mood = 7, 8, "Neutral"
            else:
                buy_slots, sell_slots, mood = 5, 10, "Bearish"

            return {
                "checked_at": str(date.today()),
                "checks": checks,
                "fails": fails,
                "mood": mood,
                "buy_slots": buy_slots,
                "sell_slots": sell_slots,
                "total_slots": 15,
                "adr_detail": {"advances": advances, "declines": declines, "unchanged": unchanged},
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
    for metric, (mn, mx) in config.items():
        rows.append({
            "metric": metric,
            "min": mn,
            "max": mx,
            "min_display": "" if mn is None else mn,
            "max_display": "" if mx is None else mx,
        })
    return {"basket": basket, "filters": rows, "count": len(rows)}

@router.get("/qualified/{basket}")
def qualified(basket: str, limit: int = 50):
    basket = basket.lower()
    if basket not in FILTER_CONFIG:
        raise HTTPException(404, f"Unknown basket: {basket}")

    config = FILTER_CONFIG[basket]
    where_clauses = ["score_date = (SELECT MAX(score_date) FROM v5_metrics)"]
    params = []

    for metric, (mn, mx) in config.items():
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
            dma_50, dma_200, rsi_month, rsi_weekly,
            week_return, month_return, year_return,
            sector_day, sector_week, month_index, range_1d
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
        return {"basket": basket, "count": len(rows), "stocks": rows}
    except Exception as e:
        raise HTTPException(500, f"qualified failed: {e}")

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
