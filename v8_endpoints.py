"""
V8 endpoints — Quant Long-Short Basket Strategy

ADR (14-Jun-2026): _read_adr gates the live tiers (adr_intraday 5-min, then
  live compute from intraday_prices) behind _market_open(). When NSE is CLOSED
  (weekend/holiday/outside 09:15-15:30 IST) it returns the last EOD adr_daily
  row directly, so a stale/partial intraday breadth (e.g. 0.26) is never shown.
ADR (11-Jun-2026): market_mood reads adr_intraday (live 5-min) primary,
  falls back to adr_daily (EOD). Per spec id=165.
qualified JOIN fix (11-Jun-2026): replaced window-function JOIN causing
  duplicate rows (FORTIS appeared twice) with subquery for first_seen.
Filter tuning (12-Jun-2026): sell_overbought rsi_month>=68 + day_1d<0 +
  sector_week<=2 + gvm_score<=8 (FILTER_CONFIG + live SQL, consistent);
  sell_reversal week_return max 3.0->1.0; buy_reversal week_return 1.0-4.0,
  year_return dead filter removed (adaptive gate auto-adjusts 11->10).
Sector cap relax (12-Jun-2026): buy_reversal + buy_momentum sector_week
  upper cap 4.0->6.0.
Pivot-room gate (15-Jun-2026): Added _pivot_room_ok() + _basket_cmp().
Score-based fallback (15-Jun-2026): _live_qualified_fallback score-based.
rsi_month widened (15-Jun-2026): buy_reversal [58.5, 75.0] → [45.0, 80.0].
funnel_detail individual (15-Jun-2026): per-filter individual pass counts.
  Each stage: passes, fails, pass_pct, condition_min, condition_max,
  survivors (compat), killed (compat). Top-level: filter_count, final (compat).
"""

from fastapi import APIRouter, HTTPException
from datetime import date, datetime, timedelta
from typing import Optional
import psycopg
import os

from nse_holidays import is_trading_day

router = APIRouter(prefix="/api/v8", tags=["v8"])

def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))

def _ist_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _market_open() -> bool:
    """True only during live NSE hours (Mon–Fri, non-holiday, 09:15–15:30 IST)."""
    now = _ist_now()
    if not is_trading_day(now.date()):
        return False
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


FILTER_CONFIG = {
    "buy_reversal": {
        "gvm_score":    [6.0,  10.0],
        "dma_200":      [1.5,  20.0],
        "dma_50":       [1.5,  8.0],
        "month_return": [0.0,  7.2],
        "week_return":  [1.0,  4.0],
        "rsi_month":    [45.0, 80.0],  # widened from [58.5, 75.0]
        "rsi_weekly":   [50.0, 67.5],
        "mom_2d":       [0.0,  2.4],
        "sector_week":  [0.0,  6.0],
        "sector_month": [0.0,  6.0],
    },
    "buy_momentum": {
        "gvm_score":    [6.0,  10.0],
        "year_return":  [0.0,  None],
        "dma_50":       [6.5,  25.0],
        "dma_200":      [7.0,  50.0],
        "rsi_month":    [60.0, 80.0],
        "rsi_weekly":   [60.0, 80.0],
        "month_return": [3.0,  14.0],
        "week_return":  [1.0,  7.0],
        "day_1d":       [0.0,  3.0],
        "sector_week":  [0.0,  6.0],
        "sector_month": [0.0,  6.0],
    },
    "sell_reversal": {
        "dma_200":      [-30.0, 2.0],
        "dma_50":       [-20.0, 2.0],
        "rsi_weekly":   [10.0,  35.0],
        "rsi_month":    [20.0,  60.0],
        "week_return":  [-3.0,  1.0],
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
        "daily_rsi":    [None,  30.0],
        "rsi_weekly":   [5.0,   60.0],
        "week_return":  [-8.0,  0.0],
        "mom_2d":       [-2.0,  0.0],
        "month_return": [-30.0, 0.0],
        "week_index_52":[None,  20.0],
        "sector_week":  [-4.0,  0.0],
        "sector_month": [-6.0,  0.0],
    },
    "sell_overbought": {
        "dma_200":      [10.0, None],
        "week_index_52":[80.0, None],
        "rsi_month":    [68.0, None],
        "mom_2d":       [None, 0.0],
        "day_1d":       [None, 0.0],
        "sector_week":  [None, 2.0],
        "gvm_score":    [None, 8.0],
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
    if segment: return segment
    if symbol in INDEX_SYMBOLS: return "Index"
    if symbol.endswith("BEES"): return "ETF"
    return None

_BLACKOUT_SQL = """
    symbol NOT IN (
        SELECT UPPER(ticker) FROM earnings_calendar
        WHERE ex_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day')
    )
"""


def _passes_filter(value, mn, mx) -> bool:
    if value is None: return False
    v = float(value)
    if mn is not None and v < mn: return False
    if mx is not None and v > mx: return False
    return True

def _pivot_room_ok(side: str, cmp, pp, r1, s1) -> bool:
    """
    Paper-engine pivot-room gate.
    BUY:  pp < cmp <= r1  AND  (r1 - cmp) >= 0.5 * (r1 - pp)
    SELL: s1 <= cmp < pp  AND  (cmp - s1) >= 0.5 * (pp - s1)
    """
    try:
        cmp = float(cmp); pp = float(pp)
    except (TypeError, ValueError):
        return False
    if side == "BUY":
        try: r1 = float(r1)
        except (TypeError, ValueError): return False
        band = r1 - pp
        return band > 0 and pp < cmp <= r1 and (r1 - cmp) >= 0.5 * band
    else:
        try: s1 = float(s1)
        except (TypeError, ValueError): return False
        band = pp - s1
        return band > 0 and s1 <= cmp < pp and (cmp - s1) >= 0.5 * band


def _gate_score(stock: dict, basket: str) -> int:
    count = 0
    for metric, bounds in FILTER_CONFIG[basket].items():
        mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
        if _passes_filter(stock.get(metric), mn, mx):
            count += 1
    return count

def _normalize_basket_to_strategy(basket: Optional[str]) -> str:
    if not basket: return ''
    return {'buy_reversal':'Buy Reversal','buy_momentum':'Buy Momentum',
            'sell_reversal':'Sell Reversal','sell_momentum':'Sell Momentum',
            'sell_overbought':'Sell Overbought'}.get(basket.lower(), basket)

def _live_qualified_fallback(basket: str, limit: int):
    """Score-based fallback — n-3 threshold (most lenient, pre-market default)."""
    config    = FILTER_CONFIG[basket]
    n_filters = len(config)
    need      = max(n_filters - 3, 1)

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT symbol, gvm_score, dma_50, dma_200, rsi_month, rsi_weekly, daily_rsi,
                   week_return, month_return, year_return, mom_2d, day_1d,
                   week_index_52, vol_ratio, sector_week, sector_month
            FROM v8_metrics
            WHERE score_date = (SELECT MAX(score_date) FROM v8_metrics)
              AND {_BLACKOUT_SQL}
        """)
        cols     = [d[0] for d in cur.description]
        all_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        pivots   = _basket_pivots(cur)
        cmp_map  = _basket_cmp(cur)

    rows = []
    for r in all_rows:
        score = sum(
            1 for metric, bounds in config.items()
            if _passes_filter(r.get(metric),
                              *(bounds if isinstance(bounds, list) else (bounds[0], bounds[1])))
        )
        r["filter_score"] = score
        r["filter_total"] = n_filters
        if score >= need:
            rows.append(r)

    side = "BUY" if basket.startswith("buy") else "SELL"
    out = []
    for r in rows:
        pv  = pivots.get(r["symbol"])
        cmp = cmp_map.get(r["symbol"])
        if cmp is None or pv is None:
            out.append(r)
            continue
        if _pivot_room_ok(side, cmp, pv.get("pp"), pv.get("r1"), pv.get("s1")):
            out.append(r)

    out.sort(key=lambda x: (x.get("filter_score", 0), x.get("gvm_score") or 0), reverse=True)
    return out[:min(max(limit, 1), 200)]


# ── ADR helpers ───────────────────────────────────────────────────────────────

def _read_adr(cur):
    if _market_open():
        cur.execute("""
            SELECT advances, declines, unchanged, adr, universe_count, ts
            FROM adr_intraday
            WHERE ts::date = CURRENT_DATE
            ORDER BY ts DESC LIMIT 1
        """)
        row = cur.fetchone()
        if row and (row[4] or 0) >= 50:
            adv, dec, unc, adr = row[0] or 0, row[1] or 0, row[2] or 0, float(row[3])
            return adv, dec, unc, adr, "adr_intraday", str(date.today())

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
                   COUNT(*) FILTER (WHERE li.cmp = pc.pclose),
                   COUNT(*)
            FROM li JOIN pc ON pc.symbol = li.symbol
        """)
        r = cur.fetchone()
        if r and (r[3] or 0) >= 50:
            adv, dec, unc = r[0] or 0, r[1] or 0, r[2] or 0
            adr = round(adv / dec, 3) if dec else float(adv)
            return adv, dec, unc, adr, "live_intraday", str(date.today())

    cur.execute("SELECT advances, declines, unchanged, adr, price_date FROM adr_daily ORDER BY price_date DESC LIMIT 1")
    r = cur.fetchone()
    if r:
        adv, dec, unc = r[0] or 0, r[1] or 0, r[2] or 0
        adr = round(float(r[3]), 3) if r[3] is not None else 1.0
        return adv, dec, unc, adr, "adr_daily", str(r[4])

    return 0, 0, 0, 1.0, "no_data", str(date.today())


def _live_nifty_dwm(cur, symbol="NIFTY50"):
    cur.execute("""
        SELECT close FROM intraday_prices
        WHERE symbol = %s AND ts::date = CURRENT_DATE
        ORDER BY ts DESC LIMIT 1
    """, (symbol,))
    live = cur.fetchone()
    if not live or live[0] is None: return None
    latest = float(live[0])
    cur.execute("""
        SELECT close FROM raw_prices
        WHERE symbol = %s AND price_date < CURRENT_DATE
        ORDER BY price_date DESC LIMIT 30
    """, (symbol,))
    hist = cur.fetchall()
    if len(hist) < 22: return None
    prev  = float(hist[0][0])
    week  = float(hist[4][0]) if len(hist) > 4 else float(hist[-1][0])
    month = float(hist[20][0]) if len(hist) > 20 else float(hist[-1][0])
    return (round((latest/prev-1)*100,2), round((latest/week-1)*100,2),
            round((latest/month-1)*100,2), latest)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/market_mood")
def market_mood():
    try:
        with _conn() as conn, conn.cursor() as cur:
            advances, declines, unchanged, adr, breadth_source, adr_date = _read_adr(cur)
            adr_pass = adr >= 1.0

            live_nifty = _live_nifty_dwm(cur, "NIFTY50")
            if live_nifty:
                nifty_day, nifty_week, nifty_month, _ = live_nifty
                nifty_source = "live_intraday"
            else:
                cur.execute("SELECT price_date, close FROM raw_prices WHERE symbol='NIFTY50' ORDER BY price_date DESC LIMIT 30")
                nifty = cur.fetchall()
                if len(nifty) < 22:
                    nifty_day = nifty_week = nifty_month = None
                else:
                    latest = float(nifty[0][1]); prev = float(nifty[1][1])
                    week   = float(nifty[5][1]) if len(nifty) > 5 else float(nifty[-1][1])
                    month  = float(nifty[21][1]) if len(nifty) > 21 else float(nifty[-1][1])
                    nifty_day   = round((latest/prev-1)*100,2)
                    nifty_week  = round((latest/week-1)*100,2)
                    nifty_month = round((latest/month-1)*100,2)
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
            for k, v in list(s.items()):
                if k not in ("symbol", "segment") and v is not None:
                    try: s[k] = float(v)
                    except (TypeError, ValueError): pass

        movers = [s for s in rows if s.get("mom_2d") is not None]
        movers.sort(key=lambda s: s["mom_2d"], reverse=True)
        n = min(max(limit, 1), 100)
        gainers = movers[:n]; losers = list(reversed(movers[-n:]))
        n_buy = len(FILTER_CONFIG["buy_reversal"]); n_sell = len(FILTER_CONFIG["sell_reversal"])
        for s in gainers: s["gate_score"] = _gate_score(s, "buy_reversal"); s["gate_total"] = n_buy
        for s in losers:  s["gate_score"] = _gate_score(s, "sell_reversal"); s["gate_total"] = n_sell

        from collections import defaultdict
        seg_groups = defaultdict(list)
        for s in rows:
            if s.get("segment"): seg_groups[s["segment"]].append(s)
        sectors = []
        for seg, members in seg_groups.items():
            m2 = [x["mom_2d"]      for x in members if x.get("mom_2d")      is not None]
            d1 = [x["day_1d"]      for x in members if x.get("day_1d")      is not None]
            wk = [x["week_return"] for x in members if x.get("week_return") is not None]
            if not m2: continue
            top = max((x for x in members if x.get("mom_2d") is not None), key=lambda x: x["mom_2d"])
            sectors.append({"segment": seg, "stocks": len(members),
                            "avg_mom_2d": round(sum(m2)/len(m2),2),
                            "avg_day_1d": round(sum(d1)/len(d1),2) if d1 else None,
                            "avg_week":   round(sum(wk)/len(wk),2) if wk else None,
                            "top_stock":  top["symbol"]})
        sectors.sort(key=lambda s: s["avg_mom_2d"], reverse=True)
        return {"score_date": score_date, "universe": len(rows),
                "gainers": gainers, "losers": losers, "sectors": sectors}
    except Exception as e:
        raise HTTPException(500, f"scan failed: {e}")


@router.get("/filter_config/{basket}")
def filter_config(basket: str):
    basket = basket.lower()
    if basket not in FILTER_CONFIG:
        raise HTTPException(404, f"Unknown basket: {basket}")
    rows = []
    for metric, bounds in FILTER_CONFIG[basket].items():
        mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
        rows.append({"metric": metric, "min": mn, "max": mx,
                     "min_display": "" if mn is None else mn,
                     "max_display": "" if mx is None else mx})
    return {"basket": basket, "filters": rows, "count": len(rows), **BASKET_META.get(basket, {})}


@router.get("/qualified/{basket}")
def qualified(basket: str, limit: int = 50):
    basket = basket.lower()
    if basket == "sell_overbought": return sell_overbought(limit=limit)
    if basket not in FILTER_CONFIG: raise HTTPException(404, f"Unknown basket: {basket}")
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                    q.symbol, q.gvm_score, q.cmp,
                    q.dma_50, q.dma_200, q.rsi_month, q.rsi_weekly,
                    q.week_return, q.month_return,
                    q.mom_2d, q.week_index_52,
                    (q.metrics->>'vol_ratio')::numeric AS vol_ratio,
                    q.sector_week, q.sector_month,
                    q.source, q.signal_ts,
                    m.day_1d,
                    g.segment,
                    p.pp, p.r1, p.s1,
                    fs.first_seen,
                    (q.metrics->>'filter_score')::numeric AS filter_score,
                    (q.metrics->>'filter_total')::numeric AS filter_total
                FROM v8_qualified q
                LEFT JOIN v8_metrics m ON m.symbol = q.symbol
                    AND m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
                LEFT JOIN gvm_scores g ON g.symbol = q.symbol
                LEFT JOIN v8_paper_pivots p ON p.symbol = q.symbol
                    AND p.pivot_date = (SELECT MAX(pivot_date) FROM v8_paper_pivots)
                LEFT JOIN (
                    SELECT symbol, basket, MIN(signal_ts) AS first_seen
                    FROM v8_qualified GROUP BY symbol, basket
                ) fs ON fs.symbol = q.symbol AND fs.basket = q.basket
                WHERE q.basket = %s
                  AND q.signal_date = CURRENT_DATE
                  AND q.symbol NOT IN (
                      SELECT UPPER(ticker) FROM earnings_calendar
                      WHERE ex_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day')
                  )
                ORDER BY q.gvm_score DESC NULLS LAST
                LIMIT %s
            """, (basket, min(max(limit, 1), 200)))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        if not rows:
            rows = _live_qualified_fallback(basket, limit)
            source_note = 'live_fallback'
        else:
            source_note = rows[0].get('source', 'precomputed') if rows else 'precomputed'

        for r in rows:
            r['segment'] = _seg_override(r['symbol'], r.get('segment'))

        return {"basket": basket, "count": len(rows), "stocks": rows,
                "source": source_note, **BASKET_META.get(basket, {})}
    except Exception as e:
        raise HTTPException(500, f"qualified failed: {e}")


def _basket_cmp(cur):
    """Load latest CMP for all symbols from cmp_prices."""
    cur.execute("SELECT symbol, cmp FROM cmp_prices WHERE cmp IS NOT NULL")
    return {r[0]: float(r[1]) for r in cur.fetchall()}


@router.get("/funnel/{basket}")
def funnel_counts(basket: str):
    basket = basket.lower()
    if basket not in FILTER_CONFIG: raise HTTPException(404, f"Unknown basket: {basket}")
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT counts FROM v8_funnel_counts WHERE basket=%s AND score_date=CURRENT_DATE ORDER BY computed_at DESC LIMIT 1", (basket,))
            row = cur.fetchone()
        if row:
            return {"basket": basket, "score_date": str(date.today()), "counts": row[0] if isinstance(row[0], dict) else {}, "source": "precomputed"}
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT symbol, gvm_score, dma_50, dma_200, dma_20,
                       rsi_month, rsi_weekly, daily_rsi, month_return, week_return,
                       year_return, mom_2d, week_index_52, ma9_vs_ma21, vol_ratio,
                       sector_week, sector_month FROM v8_metrics
                       WHERE score_date=(SELECT MAX(score_date) FROM v8_metrics)""")
            cols = [d[0] for d in cur.description]
            all_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        filters = FILTER_CONFIG[basket]; universe = all_rows[:]; counts = {}
        for metric, bounds in filters.items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            universe = [s for s in universe if _passes_filter(s.get(metric), mn, mx)]
            counts[metric] = len(universe)
        return {"basket": basket, "score_date": str(date.today()), "counts": counts, "source": "live_fallback"}
    except Exception as e:
        raise HTTPException(500, f"funnel failed: {e}")


def _basket_universe(cur):
    cur.execute("""
        SELECT symbol, gvm_score, dma_20, dma_50, dma_200,
               rsi_month, rsi_weekly, daily_rsi,
               month_return, week_return, year_return, mom_2d, day_1d,
               week_index_52, ma9_vs_ma21, vol_ratio, sector_week, sector_month
        FROM v8_metrics WHERE score_date=(SELECT MAX(score_date) FROM v8_metrics)
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

def _basket_pivots(cur):
    cur.execute("""SELECT symbol, pp, r1, s1 FROM v8_paper_pivots
        WHERE pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)
          AND pp IS NOT NULL AND r1 IS NOT NULL""")
    return {r[0]: {"pp": float(r[1]), "r1": float(r[2]), "s1": float(r[3])} for r in cur.fetchall()}


@router.get("/funnel_detail/{basket}")
def funnel_detail(basket: str):
    basket = basket.lower()
    if basket not in FILTER_CONFIG: raise HTTPException(404, f"Unknown basket: {basket}")
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
            pivots   = _basket_pivots(cur)
            cmp_map  = _basket_cmp(cur)
            cur.execute("""
                SELECT COUNT(*) FROM v8_qualified
                WHERE basket=%s AND signal_date=CURRENT_DATE
            """, (basket,))
            score_qualified = int(cur.fetchone()[0])
            cur.execute("""
                SELECT counts->>'_score_threshold'
                FROM v8_funnel_counts WHERE basket=%s AND score_date=CURRENT_DATE
                ORDER BY computed_at DESC LIMIT 1
            """, (basket,))
            fc = cur.fetchone()
            score_threshold = int(fc[0]) if fc and fc[0] else None

        total   = len(all_rows)
        filters = FILTER_CONFIG[basket]
        n       = len(filters)
        side    = "BUY" if basket.startswith("buy") else "SELL"

        # Individual pass counts — each filter scored independently vs full universe
        stages = []
        for metric, bounds in filters.items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            passes = sum(1 for s in all_rows if _passes_filter(s.get(metric), mn, mx))
            stages.append({
                "metric":   metric,
                "min":      mn,
                "max":      mx,
                "passes":   passes,
                "fails":    total - passes,
                "pass_pct": round(passes / total * 100, 1) if total else 0,
            })

        # Pivot-room: how many score-qualified stocks also have room to target
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT q.symbol, p.pp, p.r1, p.s1, c.cmp
                FROM v8_qualified q
                LEFT JOIN v8_paper_pivots p ON p.symbol = q.symbol
                    AND p.pivot_date = (SELECT MAX(pivot_date) FROM v8_paper_pivots)
                LEFT JOIN cmp_prices c ON c.symbol = q.symbol
                WHERE q.basket = %s AND q.signal_date = CURRENT_DATE
            """, (basket,))
            sq_rows = cur.fetchall()
        pivot_pass = sum(
            1 for _, pp, r1, s1, cmp in sq_rows
            if pp and _pivot_room_ok(side, cmp, pp, r1, s1)
        )

        # Enrich stages with aliases + formatted condition columns
        for st in stages:
            mn, mx = st.get("min"), st.get("max")
            st["survivors"]     = st["passes"]           # frontend compat
            st["killed"]        = st["fails"]             # frontend compat
            st["kill_pct"]      = st["pass_pct"]          # frontend compat
            st["condition_min"] = f">= {mn}" if mn is not None else "—"
            st["condition_max"] = f"<= {mx}" if mx is not None else "—"

        return {
            "basket":          basket,
            "score_date":      str(date.today()),
            "universe":        total,
            "n_filters":       n,
            "filter_count":    n,                    # frontend compat
            "score_threshold": score_threshold,
            "score_qualified": score_qualified,
            "final":           score_qualified,      # frontend compat
            "pivot_pass":      pivot_pass,
            "stages":          stages,
            **BASKET_META.get(basket, {}),
        }
    except Exception as e:
        raise HTTPException(500, f"funnel_detail failed: {e}")


@router.get("/stock_passcount/{basket}")
def stock_passcount(basket: str):
    basket = basket.lower()
    if basket not in FILTER_CONFIG: raise HTTPException(404, f"Unknown basket: {basket}")
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
        filters = FILTER_CONFIG[basket]; n_filters = len(filters); out = []
        for s in all_rows:
            passed_list, failed_list = [], []
            for metric, bounds in filters.items():
                mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
                if _passes_filter(s.get(metric), mn, mx): passed_list.append(metric)
                else: failed_list.append(metric)
            out.append({"symbol": s["symbol"], "passed": len(passed_list), "total": n_filters,
                        "passed_filters": passed_list, "failed_filters": failed_list,
                        "gvm_score": s.get("gvm_score"), "mom_2d": s.get("mom_2d")})
        out.sort(key=lambda x: (x["passed"], x["gvm_score"] if x["gvm_score"] is not None else -1), reverse=True)
        return {"basket": basket, "score_date": str(date.today()),
                "universe": len(out), "filter_count": n_filters, "stocks": out,
                **BASKET_META.get(basket, {})}
    except Exception as e:
        raise HTTPException(500, f"stock_passcount failed: {e}")


@router.get("/raw")
def raw_metrics(limit: int = 250):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
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
                ORDER BY m.gvm_score DESC NULLS LAST LIMIT %s
            """, (min(max(limit, 1), 300),))
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
                           AVG(r.close) OVER w9 AS ma9, AVG(r.close) OVER w21 AS ma21,
                           AVG(r.volume) OVER w10 AS vol_avg10,
                           LAG(r.high,1) OVER ws AS prev_high, LAG(r.low,1) OVER ws AS prev_low,
                           LAG(r.close,1) OVER ws AS prev_close,
                           ROW_NUMBER() OVER (PARTITION BY r.symbol ORDER BY r.price_date DESC) AS rn
                    FROM raw_prices r
                    JOIN futures_universe fu ON fu.symbol = r.symbol AND fu.is_active = TRUE
                    WHERE r.price_date >= CURRENT_DATE - INTERVAL '60 days'
                    WINDOW w9 AS (PARTITION BY r.symbol ORDER BY r.price_date ROWS 8 PRECEDING),
                           w21 AS (PARTITION BY r.symbol ORDER BY r.price_date ROWS 20 PRECEDING),
                           w10 AS (PARTITION BY r.symbol ORDER BY r.price_date ROWS 9 PRECEDING),
                           ws  AS (PARTITION BY r.symbol ORDER BY r.price_date)
                ),
                latest AS (
                    SELECT pw.symbol, pw.close AS entry, pw.ma9, pw.ma21,
                           ROUND(((pw.ma9-pw.ma21)/NULLIF(pw.ma21,0)*100)::numeric,2) AS ma9_vs_ma21,
                           ROUND((pw.volume/NULLIF(pw.vol_avg10,0))::numeric,2) AS vol_ratio,
                           ROUND((((pw.prev_high+pw.prev_low+pw.prev_close)/3)-(pw.prev_high-(pw.prev_high+pw.prev_low+pw.prev_close)/3))::numeric,2) AS s1
                    FROM price_window pw WHERE pw.rn=1 AND pw.ma21 IS NOT NULL AND pw.volume>0
                ),
                filtered AS (
                    SELECT l.*, vm.dma_200, vm.week_index_52, vm.rsi_month,
                           vm.daily_rsi, vm.mom_2d, vm.day_1d, vm.sector_week, vm.gvm_score
                    FROM latest l
                    JOIN v8_metrics vm ON vm.symbol=l.symbol
                      AND vm.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                    WHERE vm.dma_200>=10 AND vm.week_index_52>=80 AND l.ma9_vs_ma21>=3
                      AND l.vol_ratio<=0.8 AND vm.mom_2d<0 AND vm.rsi_month>=68
                      AND COALESCE(vm.day_1d,0)<0
                      AND COALESCE(vm.sector_week,0)<=2
                      AND COALESCE(vm.gvm_score,0)<=8
                      AND l.s1<l.entry
                      AND l.symbol NOT IN (SELECT UPPER(ticker) FROM earnings_calendar
                          WHERE ex_date IN (CURRENT_DATE, CURRENT_DATE+INTERVAL '1 day'))
                )
                SELECT symbol, ROUND(entry::numeric,2) AS entry, s1 AS target,
                    ROUND((entry+(entry-s1))::numeric,2) AS stop,
                    ROUND(((entry-s1)/NULLIF(entry,0)*100)::numeric,2) AS tgt_pct,
                    dma_200, week_index_52, ma9_vs_ma21, vol_ratio, mom_2d,
                    ROUND(rsi_month::numeric,1) AS rsi_month,
                    ROUND(daily_rsi::numeric,1) AS daily_rsi,
                    ROUND(gvm_score::numeric,2) AS gvm_score
                FROM filtered ORDER BY dma_200 DESC NULLS LAST LIMIT %s
            """, (min(max(limit, 1), 200),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {"basket": "sell_overbought", "count": len(rows),
                "target": "S1", "sl": "entry + (entry - S1) — 1:1",
                "win_pct_may2026": "71.4%", "stocks": rows}
    except Exception as e:
        raise HTTPException(500, f"sell_overbought failed: {e}")


@router.get("/adr")
def adr_only():
    try:
        with _conn() as conn, conn.cursor() as cur:
            adv, dec, unc, adr, source, adr_date = _read_adr(cur)
        return {"price_date": adr_date, "adr": adr, "advances": adv,
                "declines": dec, "unchanged": unc,
                "pass": adr >= 1.0, "source": source}
    except Exception as e:
        raise HTTPException(500, f"adr failed: {e}")


@router.get("/domestic_live")
def domestic_live():
    out = {}
    try:
        with _conn() as conn, conn.cursor() as cur:
            for sym in ("NIFTY50", "BANKNIFTY"):
                cur.execute("SELECT close FROM raw_prices WHERE symbol=%s AND price_date<CURRENT_DATE ORDER BY price_date DESC LIMIT 1", (sym,))
                pc = cur.fetchone(); prev_close = float(pc[0]) if pc and pc[0] else None
                cur.execute("""
                    SELECT (SELECT open FROM intraday_prices WHERE symbol=%s AND ts::date=CURRENT_DATE ORDER BY ts ASC LIMIT 1),
                           MAX(high), MIN(low),
                           (SELECT close FROM intraday_prices WHERE symbol=%s AND ts::date=CURRENT_DATE ORDER BY ts DESC LIMIT 1)
                    FROM intraday_prices WHERE symbol=%s AND ts::date=CURRENT_DATE
                """, (sym, sym, sym))
                r = cur.fetchone()
                if r and r[3] is not None and prev_close:
                    o,h,l,c = r[0],r[1],r[2],r[3]
                    out[sym] = {"price_date": str(date.today()),
                                "open": round(float(o),2) if o else None,
                                "high": round(float(h),2) if h else None,
                                "low":  round(float(l),2) if l else None,
                                "close": round(float(c),2),
                                "prev_close": round(prev_close,2),
                                "chg_pct": round((float(c)/prev_close-1)*100,2),
                                "source": "live_intraday"}
                else:
                    cur.execute("""WITH d AS (SELECT price_date,open,high,low,close,
                        ROW_NUMBER() OVER (ORDER BY price_date DESC) rn FROM raw_prices WHERE symbol=%s)
                        SELECT a.price_date::text,a.open,a.high,a.low,a.close,
                        ROUND(((a.close-b.close)/NULLIF(b.close,0)*100)::numeric,2)
                        FROM d a JOIN d b ON b.rn=2 WHERE a.rn=1""", (sym,))
                    e = cur.fetchone()
                    if e:
                        out[sym] = {"price_date": e[0],
                                    "open": round(float(e[1]),2) if e[1] else None,
                                    "high": round(float(e[2]),2) if e[2] else None,
                                    "low":  round(float(e[3]),2) if e[3] else None,
                                    "close": round(float(e[4]),2) if e[4] else None,
                                    "chg_pct": round(float(e[5]),2) if e[5] else None,
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
                       CASE WHEN UPPER(pj.direction)='LONG'
                            THEN ROUND(((COALESCE(cp.cmp,pj.entry_price)-pj.entry_price)*pj.qty)::numeric,2)
                            WHEN UPPER(pj.direction)='SHORT'
                            THEN ROUND(((pj.entry_price-COALESCE(cp.cmp,pj.entry_price))*pj.qty)::numeric,2)
                            ELSE 0 END AS unrealised_pnl
                FROM personal_journal pj
                LEFT JOIN cmp_prices cp ON cp.symbol=pj.symbol
                WHERE pj.exit_time IS NULL
                ORDER BY pj.entry_time DESC NULLS LAST LIMIT %s
            """, (min(max(limit, 1), 500),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows: r['strategy'] = _normalize_basket_to_strategy(r.get('v8_basket'))
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
                       pj.qty, pj.sl, pj.target, pj.pnl, pj.result, pj.holding_days,
                       pj.v8_basket, pj.v8_signal_match, pj.setup_quality,
                       pj.rule_score_total, pj.rule_violations, pj.lesson, pj.notes
                FROM personal_journal pj WHERE pj.exit_time IS NOT NULL
                ORDER BY pj.exit_time DESC LIMIT %s
            """, (min(max(limit, 1), 1000),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows: r['strategy'] = _normalize_basket_to_strategy(r.get('v8_basket'))
        return rows
    except Exception as e:
        raise HTTPException(500, f"v8_trades failed: {e}")
