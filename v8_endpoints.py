"""
V8 endpoints — Quant Long-Short Basket Strategy

FILTER_CONFIG reorder (06-Jun-2026):
  Filters ordered by descending kill rate (biggest drop first) for clean waterfall.
  Kill rates computed from 06-Jun-2026 actual funnel counts (210 universe).

  buy_reversal (11 filters):
    gvm(164) → year_ret(2) → dma_200(20) → dma_50(14) → month_ret(5) →
    week_ret(3) → rsi_month(1) → rsi_weekly(0) → mom_2d(1) →
    sector_week → sector_month
  buy_momentum (11 filters):
    gvm(164) → year_ret(2) → dma_50(14) → dma_200(10) → rsi_month(9) →
    rsi_weekly(4) → month_ret(4) → week_ret(2) → mom_2d(0) →
    sector_week → sector_month
  sell_reversal (9 filters):
    dma_200(86) → dma_50(24) → rsi_weekly(23) → rsi_month(2) →
    week_ret(6) → month_ret(2) → mom_2d → sector_week → sector_month
  sell_momentum (12 filters):
    dma_200(97) → dma_50(29) → dma_20(30) → rsi_month(12) → daily_rsi(14) →
    rsi_weekly(0) → week_ret(3) → mom_2d(4) → month_ret(1) →
    week_index_52(7) → sector_week → sector_month

  sector_week/month NULL until Monday 15:45 GVM engine run.
  NULL filter = all stocks pass (no kill).

GVM gate (08-Jun-2026): buy baskets relaxed gvm_score min 7.0 -> 6.0
  (lets in 'Watch' band 6-7). Widens buy universe ~46 -> ~123. Permanent spec change.

mom_2d formula: (cmp / close_2_days_ago - 1) * 100 — 2-day momentum (T vs T-2).
  Renamed from 'day_change' 10-Jun-2026 to remove naming confusion (it was never 1-day).
day_1d (live) = price vs T-1 close. eod_chg (frozen) = T-1 vs T-2. DISPLAY ONLY.

/scan (11-Jun-2026): one-call payload for dashboard Filter Scan + Master Top Sectors.
  gainers/losers ranked by mom_2d with gate_pass; sectors show avg mom_2d + avg day_1d.
"""

from fastapi import APIRouter, HTTPException
from datetime import date, datetime, timedelta
from typing import Optional
import psycopg
import os

router = APIRouter(prefix="/api/v8", tags=["v8"])

def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))

def _ist_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


# ── Filter configs ────────────────────────────────────────────────────────────
# Ordered by descending kill rate for optimal funnel waterfall display.

FILTER_CONFIG = {
    "buy_reversal": {
        "gvm_score":    [6.0,  10.0],
        "year_return":  [-1.5, None],
        "dma_200":      [1.5,  20.0],
        "dma_50":       [1.5,  8.0],
        "month_return": [0.0,  7.2],
        "week_return":  [1.5,  3.0],
        "rsi_month":    [58.5, 75.0],
        "rsi_weekly":   [50.0, 67.5],
        "mom_2d":       [0.0,  2.4],
        "sector_week":  [0.0,  4.0],
        "sector_month": [0.0,  6.0],
    },
    "buy_momentum": {
        "gvm_score":    [6.0,  10.0],
        "year_return":  [0.0,  None],
        "dma_50":       [6.5,  25.0],
        "dma_200":      [7.0,  50.0],
        "rsi_month":    [71.5, 80.0],
        "rsi_weekly":   [71.5, 80.0],
        "month_return": [3.0,  14.0],
        "week_return":  [1.0,  7.0],
        "mom_2d":       [0.0,  3.5],
        "sector_week":  [0.0,  4.0],
        "sector_month": [0.0,  6.0],
    },
    "sell_reversal": {
        "dma_200":      [-30.0, 2.0],
        "dma_50":       [-20.0, 2.0],
        "rsi_weekly":   [10.0,  45.0],
        "rsi_month":    [20.0,  60.0],
        "week_return":  [-6.0,  3.0],
        "month_return": [-20.0, 2.0],
        "mom_2d":       [-6.0,  0.0],
        "sector_week":  [-4.0,  0.0],
        "sector_month": [-6.0,  0.0],
    },
    "sell_momentum": {
        "dma_200":      [-50.0, 0.0],
        "dma_50":       [-30.0, 0.0],
        "dma_20":       [None,  -2.0],
        "rsi_month":    [10.0,  45.0],
        "daily_rsi":    [None,  40.0],
        "rsi_weekly":   [5.0,   60.0],
        "week_return":  [-8.0,  0.0],
        "mom_2d":       [-3.0,  0.0],
        "month_return": [-30.0, 0.0],
        "week_index_52":[None,  20.0],
        "sector_week":  [-4.0,  0.0],
        "sector_month": [-6.0,  0.0],
    },
    "sell_overbought": {
        "dma_200":      [10.0, None],
        "week_index_52":[80.0, None],
        "rsi_month":    [60.0, None],
        "mom_2d":       [None, 0.0],
    },
}

BASKET_META = {
    "buy_reversal":    {"side": "BUY",  "target": "S1", "win_pct": "~65%", "signals_per_day": "~2"},
    "buy_momentum":    {"side": "BUY",  "target": "S1", "win_pct": "~65%", "signals_per_day": "~2"},
    "sell_reversal":   {"side": "SELL", "target": "S2", "win_pct": "57%",  "signals_per_day": "~2/week"},
    "sell_momentum":   {"side": "SELL", "target": "S2", "win_pct": "83%",  "signals_per_day": "~1.5"},
    "sell_overbought": {"side": "SELL", "target": "S1", "win_pct": "71%",  "signals_per_day": "~3"},
}

INDEX_SYMBOLS = {"NIFTY50", "BANKNIFTY"}

def _seg_override(symbol: str, segment):
    if segment:
        return segment
    if symbol in INDEX_SYMBOLS:
        return "Index"
    if symbol.endswith("BEES"):
        return "ETF"
    return None

_BLACKOUT_SQL = """
    symbol NOT IN (
        SELECT UPPER(ticker) FROM earnings_calendar
        WHERE ex_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day')
    )
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _passes_filter(value, mn, mx) -> bool:
    if value is None: return False
    v = float(value)
    if mn is not None and v < mn: return False
    if mx is not None and v > mx: return False
    return True


def _strict_pass(stock: dict, basket: str) -> bool:
    """True if stock passes ALL filters of the basket (strict all-pass)."""
    for metric, bounds in FILTER_CONFIG[basket].items():
        mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
        if not _passes_filter(stock.get(metric), mn, mx):
            return False
    return True


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
               mom_2d, week_index_52, vol_ratio,
               sector_week, sector_month
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


# ── Market breadth ────────────────────────────────────────────────────────────

def _live_breadth(cur):
    cur.execute("""
        WITH latest_intraday AS (
            SELECT DISTINCT ON (symbol) symbol, close AS cmp
            FROM intraday_prices
            WHERE ts::date = CURRENT_DATE
            ORDER BY symbol, ts DESC
        ),
        prev_close AS (
            SELECT DISTINCT ON (symbol) symbol, close AS pclose
            FROM raw_prices
            WHERE price_date < CURRENT_DATE
            ORDER BY symbol, price_date DESC
        )
        SELECT
            COUNT(*) FILTER (WHERE li.cmp > pc.pclose) AS advances,
            COUNT(*) FILTER (WHERE li.cmp < pc.pclose) AS declines,
            COUNT(*) FILTER (WHERE li.cmp = pc.pclose) AS unchanged,
            COUNT(*) AS total_matched
        FROM latest_intraday li
        JOIN prev_close pc ON pc.symbol = li.symbol
    """)
    r = cur.fetchone()
    if not r or (r[3] or 0) < 50:
        return None
    advances, declines, unchanged = r[0] or 0, r[1] or 0, r[2] or 0
    adr = round(advances / declines, 3) if declines else float(advances)
    return advances, declines, unchanged, adr, "live_intraday", str(date.today())


def _live_nifty_dwm(cur, symbol="NIFTY50"):
    cur.execute("""
        SELECT close FROM intraday_prices
        WHERE symbol = %s AND ts::date = CURRENT_DATE
        ORDER BY ts DESC LIMIT 1
    """, (symbol,))
    live = cur.fetchone()
    if not live or live[0] is None:
        return None
    latest = float(live[0])
    cur.execute("""
        SELECT close FROM raw_prices
        WHERE symbol = %s AND price_date < CURRENT_DATE
        ORDER BY price_date DESC LIMIT 30
    """, (symbol,))
    hist = cur.fetchall()
    if len(hist) < 22:
        return None
    prev  = float(hist[0][0])
    week  = float(hist[4][0]) if len(hist) > 4 else float(hist[-1][0])
    month = float(hist[20][0]) if len(hist) > 20 else float(hist[-1][0])
    return (
        round((latest / prev - 1) * 100, 2),
        round((latest / week - 1) * 100, 2),
        round((latest / month - 1) * 100, 2),
        latest,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/market_mood")
def market_mood():
    try:
        with _conn() as conn, conn.cursor() as cur:
            live = _live_breadth(cur)
            if live:
                advances, declines, unchanged, adr, breadth_source, adr_date = live
            else:
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
                breadth_source = "eod_fallback"

            adr_pass = adr >= 1.0

            live_nifty = _live_nifty_dwm(cur, "NIFTY50") if live else None
            if live_nifty:
                nifty_day, nifty_week, nifty_month, _ = live_nifty
                nifty_source = "live_intraday"
            else:
                cur.execute("""
                    SELECT price_date, close FROM raw_prices
                    WHERE symbol = 'NIFTY50' ORDER BY price_date DESC LIMIT 30
                """)
                nifty = cur.fetchall()
                if len(nifty) < 22:
                    nifty_day = nifty_week = nifty_month = None
                else:
                    latest = float(nifty[0][1])
                    prev   = float(nifty[1][1])
                    week   = float(nifty[5][1]) if len(nifty) > 5 else float(nifty[-1][1])
                    month  = float(nifty[21][1]) if len(nifty) > 21 else float(nifty[-1][1])
                    nifty_day   = round((latest / prev - 1) * 100, 2)
                    nifty_week  = round((latest / week - 1) * 100, 2)
                    nifty_month = round((latest / month - 1) * 100, 2)
                nifty_source = "eod_fallback"

            nifty_day_pass   = nifty_day   is not None and nifty_day   >= 0
            nifty_week_pass  = nifty_week  is not None and nifty_week  >= 0
            nifty_month_pass = nifty_month is not None and nifty_month >= 0

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
                "breadth_source": breadth_source, "nifty_source": nifty_source,
                "adr_detail": {"advances": advances, "declines": declines,
                               "unchanged": unchanged, "adr_date": adr_date,
                               "source": breadth_source},
            }
    except Exception as e:
        raise HTTPException(500, f"market_mood failed: {e}")


@router.get("/scan")
def scan(limit: int = 25):
    """
    One-call payload for dashboard Filter Scan + Master Top Sectors.
      gainers/losers: top N by mom_2d with full column set + gate_pass
        (gainers -> buy_reversal strict all-pass; losers -> sell_reversal).
      sectors: per-segment avg mom_2d + avg day_1d (both, per spec) + avg week.
    """
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT m.symbol, m.gvm_score, m.mom_2d, m.day_1d, m.eod_chg,
                       m.week_return, m.month_return, m.year_return,
                       m.dma_20, m.dma_50, m.dma_200,
                       m.rsi_weekly, m.rsi_month, m.daily_rsi,
                       m.vol_ratio, m.week_index_52,
                       m.sector_week, m.sector_month,
                       m.score_date, g.segment
                FROM v8_metrics m
                LEFT JOIN gvm_scores g ON g.symbol = m.symbol
                WHERE m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        score_date = str(rows[0]["score_date"]) if rows else None
        for s in rows:
            s["segment"] = _seg_override(s["symbol"], s.get("segment"))
            s.pop("score_date", None)
            # floats for JSON cleanliness
            for k, v in list(s.items()):
                if k not in ("symbol", "segment") and v is not None:
                    try: s[k] = float(v)
                    except (TypeError, ValueError): pass

        movers = [s for s in rows if s.get("mom_2d") is not None]
        movers.sort(key=lambda s: s["mom_2d"], reverse=True)
        n = min(max(limit, 1), 100)
        gainers = movers[:n]
        losers  = list(reversed(movers[-n:]))

        for s in gainers:
            s["gate_pass"] = _strict_pass(s, "buy_reversal")
        for s in losers:
            s["gate_pass"] = _strict_pass(s, "sell_reversal")

        # Sector aggregation — avg mom_2d AND avg day_1d (both shown per spec)
        from collections import defaultdict
        seg_groups = defaultdict(list)
        for s in rows:
            if s.get("segment"):
                seg_groups[s["segment"]].append(s)

        sectors = []
        for seg, members in seg_groups.items():
            m2 = [x["mom_2d"]  for x in members if x.get("mom_2d")  is not None]
            d1 = [x["day_1d"]  for x in members if x.get("day_1d")  is not None]
            wk = [x["week_return"] for x in members if x.get("week_return") is not None]
            if not m2:
                continue
            top = max((x for x in members if x.get("mom_2d") is not None),
                      key=lambda x: x["mom_2d"])
            sectors.append({
                "segment":    seg,
                "stocks":     len(members),
                "avg_mom_2d": round(sum(m2) / len(m2), 2),
                "avg_day_1d": round(sum(d1) / len(d1), 2) if d1 else None,
                "avg_week":   round(sum(wk) / len(wk), 2) if wk else None,
                "top_stock":  top["symbol"],
            })
        sectors.sort(key=lambda s: s["avg_mom_2d"], reverse=True)

        return {"score_date": score_date, "universe": len(rows),
                "gainers": gainers, "losers": losers, "sectors": sectors}
    except Exception as e:
        raise HTTPException(500, f"scan failed: {e}")


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
                    mom_2d, week_index_52,
                    (metrics->>'vol_ratio')::numeric AS vol_ratio,
                    sector_week, sector_month,
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
        return {"basket": basket, "count": len(rows), "stocks": rows,
                "source": source_note, **meta}
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

        # Live fallback
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, gvm_score, dma_50, dma_200, dma_20,
                       rsi_month, rsi_weekly, daily_rsi,
                       month_return, week_return, year_return, mom_2d,
                       week_index_52, ma9_vs_ma21, vol_ratio,
                       sector_week, sector_month
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


# ── Filter funnel detail + per-stock pass count ───────────────────────────────
# Two views for all 5 baskets:
#   /funnel_detail/{basket}  → sequential funnel, filters ordered by kill (low→high survivors)
#   /stock_passcount/{basket} → per-stock count of filters passed (0..N), ranked high→low

def _basket_universe(cur):
    cur.execute("""
        SELECT symbol, gvm_score, dma_20, dma_50, dma_200,
               rsi_month, rsi_weekly, daily_rsi,
               month_return, week_return, year_return, mom_2d,
               week_index_52, ma9_vs_ma21, vol_ratio,
               sector_week, sector_month
        FROM v8_metrics
        WHERE score_date = (SELECT MAX(score_date) FROM v8_metrics)
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@router.get("/funnel_detail/{basket}")
def funnel_detail(basket: str):
    basket = basket.lower()
    if basket not in FILTER_CONFIG:
        raise HTTPException(404, f"Unknown basket: {basket}. Valid: {list(FILTER_CONFIG.keys())}")
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
        total = len(all_rows)
        filters = FILTER_CONFIG[basket]
        stages = []
        survivors = all_rows[:]
        prev = total
        for metric, bounds in filters.items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            survivors = [s for s in survivors if _passes_filter(s.get(metric), mn, mx)]
            passed = len(survivors)
            killed = prev - passed
            stages.append({
                "metric": metric,
                "min": mn, "max": mx,
                "survivors": passed,
                "killed": killed,
                "kill_pct": round(killed / prev * 100, 1) if prev else 0.0,
            })
            prev = passed
        meta = BASKET_META.get(basket, {})
        return {
            "basket": basket, "score_date": str(date.today()),
            "universe": total, "final": prev,
            "filter_count": len(filters),
            "stages": stages, **meta,
        }
    except Exception as e:
        raise HTTPException(500, f"funnel_detail failed: {e}")


@router.get("/stock_passcount/{basket}")
def stock_passcount(basket: str):
    basket = basket.lower()
    if basket not in FILTER_CONFIG:
        raise HTTPException(404, f"Unknown basket: {basket}. Valid: {list(FILTER_CONFIG.keys())}")
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
        filters = FILTER_CONFIG[basket]
        n_filters = len(filters)
        out = []
        for s in all_rows:
            passed_list, failed_list = [], []
            for metric, bounds in filters.items():
                mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
                if _passes_filter(s.get(metric), mn, mx):
                    passed_list.append(metric)
                else:
                    failed_list.append(metric)
            out.append({
                "symbol": s["symbol"],
                "passed": len(passed_list),
                "total": n_filters,
                "passed_filters": passed_list,
                "failed_filters": failed_list,
                "gvm_score": s.get("gvm_score"),
                "mom_2d": s.get("mom_2d"),
            })
        out.sort(key=lambda x: (x["passed"], x["gvm_score"] if x["gvm_score"] is not None else -1), reverse=True)
        meta = BASKET_META.get(basket, {})
        return {
            "basket": basket, "score_date": str(date.today()),
            "universe": len(out), "filter_count": n_filters,
            "stocks": out, **meta,
        }
    except Exception as e:
        raise HTTPException(500, f"stock_passcount failed: {e}")


@router.get("/raw")
def raw_metrics(limit: int = 250):
    sql = """
        SELECT m.symbol, m.score_date, m.gvm_score,
               m.dma_20, m.dma_50, m.dma_200,
               m.rsi_month, m.rsi_weekly, m.daily_rsi,
               m.month_return, m.week_return, m.year_return,
               m.month_index, m.week_index_52,
               m.mom_2d, m.day_1d, m.eod_chg,
               m.sector_week, m.sector_month,
               p.pp, p.r1, p.r2, p.s1, p.s2
        FROM v8_metrics m
        JOIN futures_universe f ON f.symbol = m.symbol AND f.is_active = TRUE
        LEFT JOIN v8_paper_pivots p ON p.symbol = m.symbol
            AND p.pivot_date = (SELECT MAX(pivot_date) FROM v8_paper_pivots)
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
                           pw.close AS entry,
                           pw.ma9, pw.ma21, pw.vol_avg10, pw.volume,
                           pw.prev_high, pw.prev_low, pw.prev_close,
                           ROUND(((pw.ma9 - pw.ma21) / NULLIF(pw.ma21, 0) * 100)::numeric, 2) AS ma9_vs_ma21,
                           ROUND((pw.volume / NULLIF(pw.vol_avg10, 0))::numeric, 2)            AS vol_ratio,
                           ROUND((((pw.prev_high + pw.prev_low + pw.prev_close) / 3)
                               - (pw.prev_high - (pw.prev_high + pw.prev_low + pw.prev_close) / 3))::numeric, 2) AS s1,
                           ROUND((((pw.prev_high + pw.prev_low + pw.prev_close) / 3)
                               - 2 * (pw.prev_high - (pw.prev_high + pw.prev_low + pw.prev_close) / 3))::numeric, 2) AS s2
                    FROM price_window pw
                    WHERE pw.rn = 1 AND pw.ma21 IS NOT NULL AND pw.volume > 0
                ),
                filtered AS (
                    SELECT l.*, vm.dma_200, vm.week_index_52, vm.rsi_month,
                           vm.daily_rsi, vm.mom_2d, vm.gvm_score
                    FROM latest l
                    JOIN v8_metrics vm ON vm.symbol = l.symbol
                     AND vm.score_date = (SELECT MAX(score_date) FROM v8_metrics)
                    WHERE vm.dma_200        >= 10
                      AND vm.week_index_52  >= 80
                      AND l.ma9_vs_ma21     >= 3
                      AND l.vol_ratio       <= 0.8
                      AND vm.mom_2d          < 0
                      AND vm.rsi_month      >= 60
                      AND l.s1              <  l.entry
                      AND l.symbol NOT IN (
                          SELECT UPPER(ticker) FROM earnings_calendar
                          WHERE ex_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day')
                      )
                )
                SELECT symbol,
                    ROUND(entry::numeric, 2)                                                    AS entry,
                    s1                                                                          AS target,
                    ROUND((entry + (entry - s1))::numeric, 2)                                  AS stop,
                    ROUND(((entry - s1) / NULLIF(entry, 0) * 100)::numeric, 2)                 AS tgt_pct,
                    dma_200, week_index_52, ma9_vs_ma21, vol_ratio,
                    mom_2d,
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
            live = _live_breadth(cur)
            if live:
                advances, declines, unchanged, adr, source, bdate = live
                return {"price_date": bdate, "adr": adr, "advances": advances,
                        "declines": declines, "unchanged": unchanged,
                        "pass": adr >= 1.0, "source": source}
            cur.execute("""
                SELECT price_date, advances, declines, unchanged, adr
                FROM adr_daily ORDER BY price_date DESC LIMIT 1
            """)
            r = cur.fetchone()
        if not r:
            return {"adr": 0.0, "pass": False, "note": "adr_daily empty",
                    "source": "eod_fallback"}
        price_date, adv, dec, unc, adr_val = r
        adr = float(adr_val) if adr_val else 0.0
        return {"price_date": str(price_date), "adr": adr,
                "advances": adv, "declines": dec, "unchanged": unc,
                "pass": adr >= 1.0, "source": "eod_fallback"}
    except Exception as e:
        raise HTTPException(500, f"adr failed: {e}")


@router.get("/domestic_live")
def domestic_live():
    out = {}
    try:
        with _conn() as conn, conn.cursor() as cur:
            for sym in ("NIFTY50", "BANKNIFTY"):
                cur.execute("""
                    SELECT close FROM raw_prices
                    WHERE symbol = %s AND price_date < CURRENT_DATE
                    ORDER BY price_date DESC LIMIT 1
                """, (sym,))
                pc = cur.fetchone()
                prev_close = float(pc[0]) if pc and pc[0] is not None else None
                cur.execute("""
                    SELECT
                        (SELECT open  FROM intraday_prices WHERE symbol=%s AND ts::date=CURRENT_DATE ORDER BY ts ASC  LIMIT 1) AS o,
                        MAX(high) AS h, MIN(low) AS l,
                        (SELECT close FROM intraday_prices WHERE symbol=%s AND ts::date=CURRENT_DATE ORDER BY ts DESC LIMIT 1) AS c
                    FROM intraday_prices
                    WHERE symbol=%s AND ts::date=CURRENT_DATE
                """, (sym, sym, sym))
                r = cur.fetchone()
                if r and r[3] is not None and prev_close:
                    o, h, l, c = r[0], r[1], r[2], r[3]
                    chg = round((float(c) / prev_close - 1) * 100, 2)
                    out[sym] = {"price_date": str(date.today()),
                                "open": round(float(o), 2) if o else None,
                                "high": round(float(h), 2) if h else None,
                                "low":  round(float(l), 2) if l else None,
                                "close": round(float(c), 2),
                                "prev_close": round(prev_close, 2),
                                "chg_pct": chg, "source": "live_intraday"}
                else:
                    cur.execute("""
                        WITH d AS (SELECT price_date, open, high, low, close,
                                          ROW_NUMBER() OVER (ORDER BY price_date DESC) rn
                                   FROM raw_prices WHERE symbol = %s)
                        SELECT a.price_date::text, a.open, a.high, a.low, a.close,
                               ROUND(((a.close-b.close)/NULLIF(b.close,0)*100)::numeric,2)
                        FROM d a JOIN d b ON b.rn=2 WHERE a.rn=1
                    """, (sym,))
                    e = cur.fetchone()
                    if e:
                        out[sym] = {"price_date": e[0],
                                    "open": round(float(e[1]), 2) if e[1] else None,
                                    "high": round(float(e[2]), 2) if e[2] else None,
                                    "low":  round(float(e[3]), 2) if e[3] else None,
                                    "close": round(float(e[4]), 2) if e[4] else None,
                                    "chg_pct": round(float(e[5]), 2) if e[5] else None,
                                    "source": "eod_fallback"}
        return {"as_of": _ist_now().strftime("%Y-%m-%d %H:%M:%S IST"), "indices": out}
    except Exception as e:
        raise HTTPException(500, f"domestic_live failed: {e}")


@router.get("/positions")
def v8_positions(limit: int = 100):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT pj.id, pj.trade_date, pj.entry_time, pj.symbol, pj.direction,
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
                SELECT pj.id, pj.trade_date, pj.entry_time, pj.exit_time,
                       pj.symbol, pj.direction,
                       pj.entry_price AS entry, pj.exit_price AS exit,
                       pj.qty, pj.sl, pj.target,
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
