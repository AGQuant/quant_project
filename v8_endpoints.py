"""
V8 endpoints -- Quant Long-Short Basket Strategy

ADR (14-Jun-2026): _read_adr gates the live tiers behind _market_open().
ADR (11-Jun-2026): market_mood reads adr_intraday primary, falls back to adr_daily.
buy_reversal V2 LOCKED (16-Jun-2026): RSI caps widened, dma_50 widened, mom_2d widened.
  rsiM[52-82] rsiW[57-73] dma50[2-12] mom2d[0-3] | 83 sigs/yr, 85.9% WR, EV +0.496%
  Root cause V1: rsi caps too tight -- bull market stocks have high RSI by nature.
buy_momentum optimisation v1 (16-Jun-2026): Target=R2(BULL)/R1. 77.4% WR.
sell_reversal V4 LOCKED (16-Jun-2026): mom_2d tightened -2 -> -3.
  5 filters: rsi_weekly<=45, mom_2d<=-3, sector_week<=-1.5, dma_200<=2, week_return[-10,-0.5]
  Target=S2 | Stop=PP+0.5*(R1-PP) | 156 sigs/yr, 79.3% WR, EV +0.752%/trade.
sell_momentum V2 LOCKED (16-Jun-2026): 6 filters, Target=S2, Stop=PP+0.5*(R1-PP), 71.9% WR.
sell_overbought V2 LOCKED (16-Jun-2026): Pivot-based mean reversion.
  5 filters: week_high>0.9*R1orR2, fall_3d<-3%, rsiW>=80, rsiM>=70, sector_week<0.
  Target=S1 | Stop=R2 | 81.5% WR | EV +1.56%/trade.
  Dedicated ring-fenced slots: 4 (Bull/Neutral) / 3 (Bearish). Total always 24.
buy_s1_bounce V1 LOCKED (17-Jun-2026): S1 support bounce -- BUY_S1_BOUNCE_SPEC_V1 id=378.
  8 strict-AND filters. 73.9% WR | EV +0.716%/trade | 88 sigs/yr.
  Dedicated ring-fenced slots: 3 (Strong Bull/Bull/Neutral) / 2 (Bearish).
  Funnel: dedicated s1b_funnel_detail()/s1b_funnel_counts() compute the TRUE
  8-filter cumulative drop-off (incl 5 live metrics not in FILTER_CONFIG).
  The static FILTER_CONFIG entry below (3 cols) is for endpoint display only.
Slot architecture (17-Jun-2026) SLOT_ARCHITECTURE_V2.4.0 id=379:
  Standard pool (4 baskets):
    Strong Bullish: 15B/5S | Bullish: 14B/6S | Neutral: 12B/8S | Bearish: 8B/13S
  SO dedicated: 4 (Bull/Neutral) / 3 (Bearish).
  S1B dedicated: 3 (Strong Bull/Bull/Neutral) / 2 (Bearish).
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
    now = _ist_now()
    if not is_trading_day(now.date()):
        return False
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


# -- Static base FILTER_CONFIG --
FILTER_CONFIG = {
    "buy_reversal": {
        # V2 LOCKED 16-Jun-2026 -- BUY_REVERSAL_SPEC_V2
        # Root cause V1: rsi_month cap 67 + rsi_weekly cap 62 too tight.
        # Bull stocks have high RSI; rsi_month 70-80+ showed 100% WR. dma_50 1.5-8 too narrow.
        # 83 sigs/yr, 85.9% WR, EV +0.496%/trade
        "gvm_score":    [6.5,  10.0],
        "dma_200":      [1.5,  20.0],
        "dma_50":       [2.0,  12.0],
        "month_return": [-2.0,  7.2],
        "week_return":  [0.0,   4.0],
        "rsi_month":    [52.0, 82.0],
        "rsi_weekly":   [57.0, 73.0],
        "mom_2d":       [0.0,   3.0],
        "sector_week":  [1.0,   6.0],
        "sector_month": [0.0,   6.0],
    },
    "buy_momentum": {
        "gvm_score":    [7.0,  10.0],
        "dma_50":       [8.0,  25.0],
        "dma_200":      [8.0,  40.0],
        "rsi_month":    [70.0, 100.0],
        "rsi_weekly":   [60.0, 85.0],
        "month_return": [2.0,  30.0],
        "week_return":  [0.5,  12.0],
        "mom_2d":       [0.0,   6.0],
        "sector_week":  [0.0,   6.0],
        "sector_month": [0.0,   6.0],
    },
    "sell_reversal": {
        # V4 LOCKED 16-Jun-2026 -- SELL_REVERSAL_SPEC_V4
        # Target=S2 | Stop=PP+0.5*(R1-PP) | 5 strict AND
        # 156 sigs/yr, 79.3% WR, EV +0.752%/trade
        "rsi_weekly":   [None, 45.0],
        "mom_2d":       [None, -3.0],
        "sector_week":  [None, -1.5],
        "dma_200":      [None,  2.0],
        "week_return":  [-10.0, -0.5],
    },
    "sell_momentum": {
        # V2 LOCKED 16-Jun-2026 -- SELL_MOMENTUM_SPEC_V2
        # Target=S2 | Stop=PP+0.5*(R1-PP) | 6 strict AND
        # 97 sigs/yr, 71.9% WR, EV +0.55%/trade
        "dma_200":       [None,  -2.0],
        "rsi_month":     [None,  38.0],
        "rsi_weekly":    [None,  38.0],
        "week_index_52": [None,  20.0],
        "sector_week":   [None,  -2.0],
        "mom_2d":        [None,  -1.5],
    },
    # sell_overbought: pivot-based filters computed live -- rsi/sector here for reference only
    "sell_overbought": {
        "rsi_weekly":   [80.0, None],
        "rsi_month":    [70.0, None],
        "sector_week":  [None,  0.0],
    },
    # buy_s1_bounce -- BUY_S1_BOUNCE_SPEC_V1 LOCKED 17-Jun-2026 id=378
    # 8 strict-AND: week_low_vs_s1, recovery_2d[2-8%], week_return[0-3%],
    #   close_vs_open, day_ret[>0.5%], vol_ratio[>=1.5x], nifty_rsi[>=55], dma50_pct[>0%]
    # S1B-specific metrics computed live by v8_signal_writer v2.4.0
    # Only standard v8_metrics cols listed here (display only -- funnel uses
    # dedicated s1b_funnel_detail() which computes the full 8-filter drop-off)
    "buy_s1_bounce": {
        "week_return":  [0.0,  3.0],
        "vol_ratio":    [1.5, None],
        "dma_50":       [0.0, None],
    },
}

SELL_REVERSAL_SL_MULT  = 0.5
SELL_MOMENTUM_SL_MULT  = 0.5

BASKET_META = {
    "buy_reversal":    {"side": "BUY",  "target": "R1",                        "win_pct": "85.9%", "signals_per_day": "~0.3/day"},
    "buy_momentum":    {"side": "BUY",  "target": "R2(BULL)/R1(NEUTRAL+BEAR)", "win_pct": "77.4%", "signals_per_day": "~2/day"},
    "sell_reversal":   {"side": "SELL", "target": "S2",                        "win_pct": "79.3%", "signals_per_day": "~0.6/day"},
    "sell_momentum":   {"side": "SELL", "target": "S2",                        "win_pct": "71.9%", "signals_per_day": "~0.4/day"},
    "sell_overbought": {"side": "SELL", "target": "S1",                        "win_pct": "81.5%", "signals_per_day": "~0.4/day"},
    "buy_s1_bounce":   {"side": "BUY",  "target": "+1.5% fixed",               "win_pct": "73.9%", "signals_per_day": "~0.3/day"},
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


def _get_nifty_1m_return() -> float:
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH ranked AS (
                    SELECT close, ROW_NUMBER() OVER (ORDER BY price_date DESC) AS rn
                    FROM raw_prices WHERE symbol='NIFTY50' AND price_date < CURRENT_DATE LIMIT 25
                )
                SELECT (SELECT close FROM ranked WHERE rn=1),
                       (SELECT close FROM ranked WHERE rn=22)
            """)
            row = cur.fetchone()
            if row and row[0] and row[1] and float(row[1]) > 0:
                return (float(row[0]) / float(row[1]) - 1) * 100
    except Exception:
        pass
    return 0.0


def _get_nifty_regime() -> tuple:
    nifty_1m = _get_nifty_1m_return()
    if nifty_1m > 2.0:    regime = "BULL"
    elif nifty_1m >= -2.0: regime = "NEUTRAL"
    else:                  regime = "BEAR"
    return regime, nifty_1m


def _get_buy_reversal_live_filters() -> dict:
    regime, nifty_1m = _get_nifty_regime()
    if regime == "BULL":      wk_max, rsi_max, sec_max = 3.0, 82.0, 4.0
    elif regime == "NEUTRAL": wk_max, rsi_max, sec_max = 2.0, 75.0, 3.0
    else:                     wk_max, rsi_max, sec_max = 1.0, 65.0, 2.0
    live = dict(FILTER_CONFIG["buy_reversal"])
    live["week_return"] = [0.0, wk_max]
    live["rsi_month"]   = [52.0, rsi_max]
    live["sector_week"] = [1.0, sec_max]
    return live, regime, nifty_1m


def _get_buy_momentum_target(regime: str) -> str:
    return "R2" if regime == "BULL" else "R1"


def _pivot_room_ok(side: str, cmp, pp, r1, s1) -> bool:
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
    config = _get_buy_reversal_live_filters()[0] if basket == "buy_reversal" else FILTER_CONFIG[basket]
    return sum(1 for metric, bounds in config.items()
               if _passes_filter(stock.get(metric),
                                 *(bounds if isinstance(bounds, list) else (bounds[0], bounds[1]))))

def _normalize_basket_to_strategy(basket: Optional[str]) -> str:
    if not basket: return ''
    return {'buy_reversal':'Buy Reversal','buy_momentum':'Buy Momentum',
            'sell_reversal':'Sell Reversal','sell_momentum':'Sell Momentum',
            'sell_overbought':'Sell Overbought','buy_s1_bounce':'Buy S1 Bounce'}.get(basket.lower(), basket)

def _live_qualified_fallback(basket: str, limit: int):
    if basket == "buy_reversal":
        config, _, _ = _get_buy_reversal_live_filters()
    else:
        config = FILTER_CONFIG[basket]
    n_filters = len(config)
    need      = max(n_filters - 1, 1)

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
        score = sum(1 for metric, bounds in config.items()
                    if _passes_filter(r.get(metric),
                                      *(bounds if isinstance(bounds, list) else (bounds[0], bounds[1]))))
        r["filter_score"] = score
        r["filter_total"] = n_filters
        r["status"] = "NEAR_MISS"
        if score >= need:
            rows.append(r)

    side = "BUY" if basket.startswith("buy") else "SELL"
    out = []
    for r in rows:
        pv  = pivots.get(r["symbol"])
        cmp = cmp_map.get(r["symbol"])
        r["cmp"] = cmp
        if pv:
            r["pp"] = pv.get("pp"); r["r1"] = pv.get("r1"); r["s1"] = pv.get("s1")
        if cmp is None or pv is None:
            out.append(r); continue
        if _pivot_room_ok(side, cmp, pv.get("pp"), pv.get("r1"), pv.get("s1")):
            out.append(r)

    out.sort(key=lambda x: (x.get("filter_score", 0), x.get("gvm_score") or 0), reverse=True)
    return out[:min(max(limit, 1), 200)]


def _load_open_positions(basket: str) -> dict:
    side = "LONG" if basket.startswith("buy") else "SHORT"
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT p.symbol, p.entry_price, p.target, p.stop_loss, p.qty,
                       COALESCE(c.cmp, p.entry_price) AS cmp,
                       CASE WHEN p.side='LONG'
                            THEN ROUND(((COALESCE(c.cmp,p.entry_price)-p.entry_price)/p.entry_price*100)::numeric,2)
                            ELSE ROUND(((p.entry_price-COALESCE(c.cmp,p.entry_price))/p.entry_price*100)::numeric,2)
                       END AS pnl_pct
                FROM v8_paper_positions p
                LEFT JOIN cmp_prices c ON c.symbol=p.symbol
                WHERE p.basket=%s AND p.status='OPEN' AND p.side=%s
            """, (basket, side))
            cols = [d[0] for d in cur.description]
            return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
    except Exception:
        return {}


def _load_slot_full(basket: str) -> set:
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT symbol FROM v8_qualified
                WHERE basket=%s AND signal_date=CURRENT_DATE
                  AND metrics->>'status'='slot_full'
            """, (basket,))
            return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


def _enrich_with_status(stocks: list, basket: str, open_pos: dict, slot_full: set) -> list:
    for s in stocks:
        sym = s.get("symbol", "")
        pos = open_pos.get(sym)
        if pos:
            s["status"]       = "OPEN"
            s["entry_price"]  = float(pos["entry_price"]) if pos.get("entry_price") else None
            s["open_pnl_pct"] = float(pos["pnl_pct"])    if pos.get("pnl_pct")     else None
            s["open_target"]  = float(pos["target"])      if pos.get("target")      else None
            s["open_stop"]    = float(pos["stop_loss"])   if pos.get("stop_loss")   else None
        elif sym in slot_full:
            s["status"] = "SLOT_FULL"
        elif s.get("status") != "NEAR_MISS":
            s["status"] = "QUALIFIED"
    return stocks


def _read_adr(cur):
    if _market_open():
        cur.execute("""
            SELECT advances, declines, unchanged, adr, universe_count, ts
            FROM adr_intraday WHERE ts::date = CURRENT_DATE ORDER BY ts DESC LIMIT 1
        """)
        row = cur.fetchone()
        if row and (row[4] or 0) >= 50:
            adv, dec, unc, adr = row[0] or 0, row[1] or 0, row[2] or 0, float(row[3])
            return adv, dec, unc, adr, "adr_intraday", str(date.today())
        cur.execute("""
            WITH li AS (
                SELECT DISTINCT ON (symbol) symbol, close AS cmp
                FROM intraday_prices WHERE ts::date = CURRENT_DATE ORDER BY symbol, ts DESC
            ),
            pc AS (
                SELECT DISTINCT ON (symbol) symbol, close AS pclose
                FROM raw_prices WHERE price_date < CURRENT_DATE ORDER BY symbol, price_date DESC
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
        WHERE symbol = %s AND ts::date = CURRENT_DATE ORDER BY ts DESC LIMIT 1
    """, (symbol,))
    live = cur.fetchone()
    if not live or live[0] is None: return None
    latest = float(live[0])
    cur.execute("""
        SELECT close FROM raw_prices
        WHERE symbol = %s AND price_date < CURRENT_DATE ORDER BY price_date DESC LIMIT 30
    """, (symbol,))
    hist = cur.fetchall()
    if len(hist) < 22: return None
    prev  = float(hist[0][0])
    week  = float(hist[4][0]) if len(hist) > 4 else float(hist[-1][0])
    month = float(hist[20][0]) if len(hist) > 20 else float(hist[-1][0])
    return (round((latest/prev-1)*100,2), round((latest/week-1)*100,2),
            round((latest/month-1)*100,2), latest)


# -- Endpoints -----------------------------------------------------------------

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

            if fails == 0:   buy_slots, sell_slots, mood = 15, 5,  "Strong Bullish"
            elif fails == 1: buy_slots, sell_slots, mood = 14, 6,  "Bullish"
            elif fails == 2: buy_slots, sell_slots, mood = 12, 8,  "Neutral"
            else:            buy_slots, sell_slots, mood = 8,  13, "Bearish"

            so_slots  = 3 if fails >= 3 else 4
            s1b_slots = 2 if fails >= 3 else 3
            total_slots = buy_slots + sell_slots + so_slots + s1b_slots

            return {
                "checked_at": str(date.today()), "checks": checks,
                "fails": fails, "mood": mood,
                "buy_slots": buy_slots, "sell_slots": sell_slots,
                "so_slots": so_slots, "s1b_slots": s1b_slots, "total_slots": total_slots,
                "slot_note": "so_slots ring-fenced for sell_overbought; s1b_slots ring-fenced for buy_s1_bounce -- never compete with standard pools",
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

    regime, nifty_1m = _get_nifty_regime()

    if basket == "buy_reversal":
        live_config, regime, nifty_1m = _get_buy_reversal_live_filters()
        rows = []
        for metric, bounds in live_config.items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            rows.append({"metric": metric, "min": mn, "max": mx,
                         "min_display": "" if mn is None else mn,
                         "max_display": "" if mx is None else mx,
                         "dynamic": metric in ("week_return", "rsi_month", "sector_week")})
        return {
            "basket": basket, "filters": rows, "count": len(rows),
            "regime": regime, "nifty_1m_return": round(nifty_1m, 2),
            "regime_rules": {
                "BULL":    {"condition": "Nifty 1M > +2%",  "week_return_max": 3.0, "rsi_month_max": 82.0, "sector_week_max": 4.0},
                "NEUTRAL": {"condition": "Nifty 1M 0-2%",   "week_return_max": 2.0, "rsi_month_max": 75.0, "sector_week_max": 3.0},
                "BEAR":    {"condition": "Nifty 1M < 0%",   "week_return_max": 1.0, "rsi_month_max": 65.0, "sector_week_max": 2.0},
            },
            "backtest": {"signals": 83, "wr_pct": 85.9, "ev_per_trade": 0.496},
            **BASKET_META.get(basket, {})
        }

    if basket == "buy_momentum":
        target = _get_buy_momentum_target(regime)
        rows = []
        for metric, bounds in FILTER_CONFIG["buy_momentum"].items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            rows.append({"metric": metric, "min": mn, "max": mx,
                         "min_display": "" if mn is None else mn,
                         "max_display": "" if mx is None else mx,
                         "dynamic": metric == "rsi_month"})
        return {
            "basket": basket, "filters": rows, "count": len(rows),
            "regime": regime, "nifty_1m_return": round(nifty_1m, 2),
            "target": target, "target_rule": "R2 in BULL (Nifty 1M > +2%), R1 in NEUTRAL/BEAR",
            "stop": "S1",
            "regime_rules": {
                "BULL":    {"condition": "Nifty 1M > +2%", "target": "R2", "slots": 15},
                "NEUTRAL": {"condition": "-2% to +2%",     "target": "R1", "slots": 12},
                "BEAR":    {"condition": "Nifty 1M < -2%", "target": "R1", "slots": 8},
            },
            "backtest": {"signals": 243, "wr_pct": 77.4, "avg_win": 1.79, "total_pnl": 120.27},
            **BASKET_META.get(basket, {})
        }

    if basket == "sell_reversal":
        rows = []
        for metric, bounds in FILTER_CONFIG["sell_reversal"].items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            rows.append({"metric": metric, "min": mn, "max": mx,
                         "min_display": "" if mn is None else mn,
                         "max_display": "" if mx is None else mx})
        return {
            "basket": basket, "filters": rows, "count": len(rows),
            "target": "S2", "target_formula": "S2 = PP - (H5 - L5)  [rolling-5-day pivot]",
            "stop": f"PP + {SELL_REVERSAL_SL_MULT}*(R1-PP)", "sl_mult": SELL_REVERSAL_SL_MULT,
            "backtest": {"signals": 156, "wr_pct": 79.3, "expected_value": 0.752},
            **BASKET_META.get(basket, {})
        }

    if basket == "sell_momentum":
        rows = []
        for metric, bounds in FILTER_CONFIG["sell_momentum"].items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            rows.append({"metric": metric, "min": mn, "max": mx,
                         "min_display": "" if mn is None else mn,
                         "max_display": "" if mx is None else mx})
        return {
            "basket": basket, "filters": rows, "count": len(rows),
            "target": "S2", "target_formula": "S2 = PP - (H5 - L5)  [rolling-5-day pivot]",
            "stop": f"PP + {SELL_MOMENTUM_SL_MULT}*(R1-PP)", "sl_mult": SELL_MOMENTUM_SL_MULT,
            "backtest": {"signals": 97, "wr_pct": 71.9, "expected_value": 0.55},
            **BASKET_META.get(basket, {})
        }

    if basket == "sell_overbought":
        return {
            "basket": basket,
            "principle": "Mean reversion from overbought resistance",
            "filters": [
                {"metric": "week_high_vs_pivot", "condition": "hi5d > 0.9*R1 OR hi5d > 0.9*R2"},
                {"metric": "fall_3d",            "condition": "< -3.0%"},
                {"metric": "rsi_weekly",         "condition": ">= 80"},
                {"metric": "rsi_month",          "condition": ">= 70"},
                {"metric": "sector_week",        "condition": "< 0"},
            ],
            "count": 5,
            "target": "S1", "target_formula": "S1 = 2*PP - H5",
            "stop": "R2",   "stop_formula":   "R2 = PP + (H5 - L5)",
            "slot_architecture": {"strong_bullish": 4, "bullish": 4, "neutral": 4, "bearish": 3,
                                  "note": "Ring-fenced -- never competes with standard sell pool"},
            "backtest": {"signals": 112, "wr_pct": 81.5, "expected_value": 1.56},
            **BASKET_META.get(basket, {})
        }

    if basket == "buy_s1_bounce":
        return {
            "basket": basket,
            "principle": "Bounce from pivot S1 support -- 8 strict-AND filters",
            "filters": [
                {"metric": "nifty_rsi (market gate)", "condition": ">= 55"},
                {"metric": "week_low_vs_s1",          "condition": "week_low <= pivot S1"},
                {"metric": "recovery_2d",             "condition": "2% to 8%"},
                {"metric": "week_return",             "condition": "0% to 3%"},
                {"metric": "close_vs_open",           "condition": "close > open"},
                {"metric": "day_ret",                 "condition": "> 0.5%"},
                {"metric": "vol_ratio",               "condition": ">= 1.5x"},
                {"metric": "dma_50",                  "condition": "> 0%"},
            ],
            "count": 8,
            "target": "+1.5% fixed from entry", "stop": "-1.5% fixed from entry",
            "slot_architecture": {"strong_bullish": 3, "bullish": 3, "neutral": 3, "bearish": 2,
                                  "note": "Ring-fenced -- never competes with standard buy pool"},
            "backtest": {"signals": 88, "wr_pct": 73.9, "expected_value": 0.716},
            "note": "5 of 8 filters (nifty_rsi, recovery_2d, day_ret, close_vs_open, "
                    "week_low) are computed live by v8_signal_writer -- not in static "
                    "FILTER_CONFIG. Use /funnel_detail/buy_s1_bounce for true drop-off.",
            **BASKET_META.get(basket, {})
        }

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
    if basket == "buy_s1_bounce":   return buy_s1_bounce_qualified(limit=limit)
    if basket not in FILTER_CONFIG: raise HTTPException(404, f"Unknown basket: {basket}")
    try:
        open_pos  = _load_open_positions(basket)
        slot_full = _load_slot_full(basket)

        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT q.symbol, q.gvm_score, q.cmp,
                    q.dma_50, q.dma_200, q.rsi_month, q.rsi_weekly,
                    q.week_return, q.month_return,
                    q.mom_2d, q.week_index_52,
                    (q.metrics->>'vol_ratio')::numeric AS vol_ratio,
                    q.sector_week, q.sector_month,
                    q.source, q.signal_ts,
                    m.day_1d, g.segment,
                    p.pp, p.r1, p.s1,
                    fs.first_seen,
                    (q.metrics->>'filter_score')::numeric AS filter_score,
                    (q.metrics->>'filter_total')::numeric AS filter_total,
                    q.metrics->>'regime' AS regime,
                    q.metrics->>'status' AS stored_status
                FROM v8_qualified q
                LEFT JOIN v8_metrics m ON m.symbol=q.symbol
                    AND m.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                LEFT JOIN gvm_scores g ON g.symbol=q.symbol
                LEFT JOIN v8_paper_pivots p ON p.symbol=q.symbol
                    AND p.pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)
                LEFT JOIN (SELECT symbol, basket, MIN(signal_ts) AS first_seen
                           FROM v8_qualified GROUP BY symbol, basket) fs
                    ON fs.symbol=q.symbol AND fs.basket=q.basket
                WHERE q.basket=%s AND q.signal_date=CURRENT_DATE
                  AND q.symbol NOT IN (
                      SELECT UPPER(ticker) FROM earnings_calendar
                      WHERE ex_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day'))
                ORDER BY q.gvm_score DESC NULLS LAST LIMIT %s
            """, (basket, min(max(limit, 1), 200)))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        if not rows:
            rows = _live_qualified_fallback(basket, limit)
            source_note = 'live_fallback'
        else:
            source_note = rows[0].get('source', 'precomputed') if rows else 'precomputed'
            with _conn() as conn, conn.cursor() as cur:
                pivots  = _basket_pivots(cur)
                cmp_map = _basket_cmp(cur)
            for r in rows:
                sym = r["symbol"]
                if not r.get("pp") and sym in pivots:
                    pv = pivots[sym]
                    r["pp"] = pv.get("pp"); r["r1"] = pv.get("r1"); r["s1"] = pv.get("s1")
                if not r.get("cmp") and sym in cmp_map:
                    r["cmp"] = cmp_map[sym]
                r["status"] = r.pop("stored_status", None) or "QUALIFIED"

        for r in rows:
            r['segment'] = _seg_override(r['symbol'], r.get('segment'))

        rows = _enrich_with_status(rows, basket, open_pos, slot_full)

        extra = {}
        if basket == "buy_momentum":
            regime, nifty_1m = _get_nifty_regime()
            extra = {"regime": regime, "nifty_1m": round(nifty_1m, 2),
                     "target": _get_buy_momentum_target(regime)}
        elif basket == "sell_reversal":
            extra = {"target": "S2", "target_formula": "S2 = PP - (H5 - L5)",
                     "stop_formula": f"PP + {SELL_REVERSAL_SL_MULT}*(R1-PP)", "sl_mult": SELL_REVERSAL_SL_MULT}
        elif basket == "sell_momentum":
            extra = {"target": "S2", "target_formula": "S2 = PP - (H5 - L5)",
                     "stop_formula": f"PP + {SELL_MOMENTUM_SL_MULT}*(R1-PP)", "sl_mult": SELL_MOMENTUM_SL_MULT}

        return {"basket": basket, "count": len(rows), "stocks": rows,
                "source": source_note, **BASKET_META.get(basket, {}), **extra}
    except Exception as e:
        raise HTTPException(500, f"qualified failed: {e}")


def _basket_cmp(cur):
    cur.execute("SELECT symbol, cmp FROM cmp_prices WHERE cmp IS NOT NULL")
    return {r[0]: float(r[1]) for r in cur.fetchall()}


@router.get("/funnel/{basket}")
def funnel_counts(basket: str):
    basket = basket.lower()
    if basket == "buy_s1_bounce": return s1b_funnel_counts()
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
    if basket == "buy_s1_bounce": return s1b_funnel_detail()
    if basket not in FILTER_CONFIG: raise HTTPException(404, f"Unknown basket: {basket}")
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
            pivots   = _basket_pivots(cur)
            cmp_map  = _basket_cmp(cur)
            cur.execute("SELECT COUNT(*) FROM v8_qualified WHERE basket=%s AND signal_date=CURRENT_DATE", (basket,))
            score_qualified = int(cur.fetchone()[0])
            cur.execute("""SELECT counts->>'_score_threshold' FROM v8_funnel_counts
                WHERE basket=%s AND score_date=CURRENT_DATE ORDER BY computed_at DESC LIMIT 1""", (basket,))
            fc = cur.fetchone()
            score_threshold = int(fc[0]) if fc and fc[0] else None

        total   = len(all_rows)
        filters = _get_buy_reversal_live_filters()[0] if basket == "buy_reversal" else FILTER_CONFIG[basket]
        n       = len(filters)
        side    = "BUY" if basket.startswith("buy") else "SELL"

        stages = []
        for metric, bounds in filters.items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            passes = sum(1 for s in all_rows if _passes_filter(s.get(metric), mn, mx))
            stages.append({"metric": metric, "min": mn, "max": mx,
                           "passes": passes, "fails": total - passes,
                           "pass_pct": round(passes / total * 100, 1) if total else 0,
                           "dynamic": basket == "buy_reversal" and metric in ("week_return", "rsi_month", "sector_week")})

        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT q.symbol, p.pp, p.r1, p.s1, c.cmp FROM v8_qualified q
                LEFT JOIN v8_paper_pivots p ON p.symbol=q.symbol
                    AND p.pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)
                LEFT JOIN cmp_prices c ON c.symbol=q.symbol
                WHERE q.basket=%s AND q.signal_date=CURRENT_DATE
            """, (basket,))
            sq_rows = cur.fetchall()
        pivot_pass = sum(1 for _, pp, r1, s1, cmp in sq_rows if pp and _pivot_room_ok(side, cmp, pp, r1, s1))

        for st in stages:
            mn, mx = st.get("min"), st.get("max")
            st["condition_min"] = f">= {mn}" if mn is not None else "-"
            st["condition_max"] = f"<= {mx}" if mx is not None else "-"

        return {"basket": basket, "score_date": str(date.today()),
                "universe": total, "n_filters": n,
                "score_threshold": score_threshold, "score_qualified": score_qualified,
                "pivot_pass": pivot_pass, "stages": stages,
                **BASKET_META.get(basket, {})}
    except Exception as e:
        raise HTTPException(500, f"funnel_detail failed: {e}")


@router.get("/stock_passcount/{basket}")
def stock_passcount(basket: str):
    basket = basket.lower()
    if basket not in FILTER_CONFIG: raise HTTPException(404, f"Unknown basket: {basket}")
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
        filters = _get_buy_reversal_live_filters()[0] if basket == "buy_reversal" else FILTER_CONFIG[basket]
        n_filters = len(filters); out = []
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
                JOIN futures_universe f ON f.symbol=m.symbol AND f.is_active=TRUE
                LEFT JOIN v8_paper_pivots p ON p.symbol=m.symbol
                    AND p.pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)
                WHERE m.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                ORDER BY m.gvm_score DESC NULLS LAST LIMIT %s
            """, (min(max(limit, 1), 300),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        score_date = rows[0]["score_date"] if rows else None
        return {"count": len(rows), "score_date": str(score_date) if score_date else None,
                "columns": cols, "stocks": rows}
    except Exception as e:
        raise HTTPException(500, f"raw_metrics failed: {e}")



def _s1b_funnel_stages():
    """
    Compute the true 8-filter Buy S1 Bounce funnel using live intraday data.
    Returns (stages, nifty_rsi, gate_open). Each stage shows cumulative survivors.
    Filter order matches signal-writer _write_buy_s1_bounce_qualified():
      F0 Market gate: Nifty RSI(14) >= 55  (global on/off)
      F1 week_return 0-3%       (v8_metrics)
      F2 dma_50 > 0%            (v8_metrics)
      F3 vol_ratio >= 1.5x      (v8_metrics, live)
      F4 recovery_2d 2-8%       (live: (cmp-lo_2d)/lo_2d*100)
      F5 day_ret > 0.5%         (live: (cmp-day_open)/day_open*100)
      F6 week_low <= S1         (min(lo_5d, today_low) <= pivot S1)
    """
    with _conn() as conn, conn.cursor() as cur:
        # Nifty RSI(14) market gate
        cur.execute("""
            SELECT close FROM raw_prices
            WHERE symbol='NIFTY50' AND price_date < CURRENT_DATE
            ORDER BY price_date DESC LIMIT 30
        """)
        nifty_closes = [float(r[0]) for r in cur.fetchall()]
        nifty_rsi = None
        if len(nifty_closes) >= 15:
            nifty_closes.reverse()
            import pandas as _pd
            s = _pd.Series(nifty_closes)
            delta = s.diff()
            gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / loss.replace(0, float("nan"))
            rsi_series = 100 - (100 / (1 + rs))
            v = rsi_series.iloc[-1]
            nifty_rsi = float(v) if v == v else None

        # Per-symbol metrics + live intraday + pivots
        cur.execute("""
            WITH td AS (
                SELECT symbol,
                    (SELECT open  FROM intraday_prices i2
                     WHERE i2.symbol=ip.symbol AND i2.ts::date=CURRENT_DATE
                     ORDER BY ts ASC LIMIT 1)  AS day_open,
                    (SELECT close FROM intraday_prices i3
                     WHERE i3.symbol=ip.symbol AND i3.ts::date=CURRENT_DATE
                     ORDER BY ts DESC LIMIT 1) AS live_close,
                    MIN(low) FILTER (WHERE ts::date=CURRENT_DATE) AS today_low
                FROM intraday_prices ip
                WHERE ts::date=CURRENT_DATE
                GROUP BY symbol
            ),
            hist AS (
                SELECT symbol,
                    MIN(low) FILTER (WHERE rn<=2) AS lo_2d,
                    MIN(low) FILTER (WHERE rn<=5) AS lo_5d
                FROM (
                    SELECT symbol, low,
                        ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
                    FROM raw_prices WHERE price_date < CURRENT_DATE
                ) x WHERE rn<=5
                GROUP BY symbol
            )
            SELECT m.symbol, m.week_return, m.dma_50, m.vol_ratio,
                   td.day_open, td.live_close, td.today_low,
                   h.lo_2d, h.lo_5d, p.s1
            FROM v8_metrics m
            JOIN futures_universe f ON f.symbol=m.symbol AND f.is_active=TRUE
            LEFT JOIN td   ON td.symbol=m.symbol
            LEFT JOIN hist h ON h.symbol=m.symbol
            LEFT JOIN v8_paper_pivots p ON p.symbol=m.symbol
                AND p.pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)
            WHERE m.score_date=(SELECT MAX(score_date) FROM v8_metrics)
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    total = len(rows)
    gate_open = nifty_rsi is not None and nifty_rsi >= 55.0

    def _f(v):
        try: return float(v) if v is not None else None
        except (TypeError, ValueError): return None

    # compute derived live metrics per row
    for r in rows:
        cmp = _f(r.get("live_close"))
        op  = _f(r.get("day_open"))
        lo2 = _f(r.get("lo_2d"))
        lo5 = _f(r.get("lo_5d"))
        tlow = _f(r.get("today_low"))
        r["recovery_2d"] = ((cmp - lo2) / lo2 * 100) if (cmp and lo2 and lo2 > 0) else None
        r["day_ret"]     = ((cmp - op) / op * 100) if (cmp and op and op > 0) else None
        wl_candidates = [x for x in (lo5, tlow) if x is not None]
        r["week_low"] = min(wl_candidates) if wl_candidates else None

    # cumulative funnel
    stages = []
    survivors = rows[:]

    def _stage(label, cond, condition_text):
        nonlocal survivors
        survivors = [s for s in survivors if cond(s)]
        stages.append({"metric": label, "condition": condition_text,
                       "passes": len(survivors), "fails_remaining": 0})

    # F0 market gate -- if gate closed, all fail downstream (show 0 survivors)
    if not gate_open:
        # gate is global; entire basket OFF
        stages.append({"metric": "nifty_rsi (market gate)",
                       "condition": ">= 55  (GATE CLOSED -- basket OFF)",
                       "passes": 0, "fails_remaining": total})
        return stages, nifty_rsi, gate_open, total

    stages.append({"metric": "nifty_rsi (market gate)",
                   "condition": f">= 55  (OPEN, Nifty RSI={nifty_rsi:.1f})",
                   "passes": total, "fails_remaining": 0})

    _stage("week_return", lambda s: _passes_filter(s.get("week_return"), 0.0, 3.0),
           "0% to 3%")
    _stage("dma_50", lambda s: _passes_filter(s.get("dma_50"), 0.0, None),
           "> 0%")
    _stage("vol_ratio", lambda s: _passes_filter(s.get("vol_ratio"), 1.5, None),
           ">= 1.5x")
    _stage("recovery_2d", lambda s: _passes_filter(s.get("recovery_2d"), 2.0, 8.0),
           "2% to 8%")
    _stage("day_ret", lambda s: _passes_filter(s.get("day_ret"), 0.5, None),
           "> 0.5% (close > open)")
    _stage("week_low_vs_s1",
           lambda s: s.get("week_low") is not None and s.get("s1") is not None
                     and float(s["week_low"]) <= float(s["s1"]),
           "week_low <= pivot S1")

    return stages, nifty_rsi, gate_open, total


def s1b_funnel_detail():
    """Dedicated Buy S1 Bounce funnel -- all 8 strict-AND filters, live data."""
    try:
        stages, nifty_rsi, gate_open, total = _s1b_funnel_stages()
        final_qualified = stages[-1]["passes"] if stages else 0
        return {
            "basket": "buy_s1_bounce",
            "score_date": str(date.today()),
            "universe": total,
            "n_filters": 8,
            "gate_type": "strict AND (all must pass)",
            "market_gate": {"metric": "nifty_rsi", "threshold": 55.0,
                            "value": round(nifty_rsi, 1) if nifty_rsi is not None else None,
                            "open": gate_open},
            "score_qualified": final_qualified,
            "pivot_pass": final_qualified,
            "stages": stages,
            "note": "S1B uses 8 strict-AND filters incl 5 live intraday metrics "
                    "(recovery_2d, day_ret, week_low) not in static FILTER_CONFIG. "
                    "This funnel computes the TRUE drop-off.",
            **BASKET_META.get("buy_s1_bounce", {})
        }
    except Exception as e:
        raise HTTPException(500, f"s1b_funnel_detail failed: {e}")


def s1b_funnel_counts():
    """Compact cumulative counts version for the /funnel/{basket} shape."""
    try:
        stages, nifty_rsi, gate_open, total = _s1b_funnel_stages()
        counts = {st["metric"]: st["passes"] for st in stages}
        counts["_market_gate_open"] = gate_open
        counts["_nifty_rsi"] = round(nifty_rsi, 1) if nifty_rsi is not None else None
        counts["_final_qualified"] = stages[-1]["passes"] if stages else 0
        return {"basket": "buy_s1_bounce", "score_date": str(date.today()),
                "counts": counts, "source": "live_8filter"}
    except Exception as e:
        raise HTTPException(500, f"s1b_funnel_counts failed: {e}")


@router.get("/buy_s1_bounce")
def buy_s1_bounce_qualified(limit: int = 50):
    """BUY_S1_BOUNCE_SPEC_V1 -- 73.9% WR -- 88 sigs/yr -- LOCKED 17-Jun-2026 id=378"""
    try:
        open_pos  = _load_open_positions("buy_s1_bounce")
        slot_full = _load_slot_full("buy_s1_bounce")

        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT q.symbol, q.gvm_score, q.cmp,
                    q.dma_50, q.week_return, q.mom_2d, q.rsi_month,
                    (q.metrics->>'recovery_2d')::numeric AS recovery_2d,
                    (q.metrics->>'week_low')::numeric    AS week_low,
                    (q.metrics->>'day_ret')::numeric     AS day_ret,
                    (q.metrics->>'nifty_rsi')::numeric   AS nifty_rsi,
                    (q.metrics->>'vol_ratio')::numeric   AS vol_ratio,
                    (q.metrics->>'target')::numeric      AS target_price,
                    (q.metrics->>'stop_loss')::numeric   AS stop_price,
                    q.source, q.signal_ts,
                    m.day_1d, g.segment,
                    p.pp, p.r1, p.s1,
                    q.metrics->>'status' AS stored_status
                FROM v8_qualified q
                LEFT JOIN v8_metrics m ON m.symbol=q.symbol
                    AND m.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                LEFT JOIN gvm_scores g ON g.symbol=q.symbol
                LEFT JOIN v8_paper_pivots p ON p.symbol=q.symbol
                    AND p.pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)
                WHERE q.basket='buy_s1_bounce' AND q.signal_date=CURRENT_DATE
                  AND q.symbol NOT IN (
                      SELECT UPPER(ticker) FROM earnings_calendar
                      WHERE ex_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day'))
                ORDER BY q.gvm_score DESC NULLS LAST LIMIT %s
            """, (min(max(limit, 1), 200),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        for r in rows:
            r['segment'] = _seg_override(r['symbol'], r.get('segment'))
            r['status']  = r.pop('stored_status', None) or 'QUALIFIED'

        rows = _enrich_with_status(rows, "buy_s1_bounce", open_pos, slot_full)

        return {
            "basket":            "buy_s1_bounce",
            "count":             len(rows),
            "target":            "+1.5% fixed from entry",
            "stop":              "-1.5% fixed from entry",
            "slot_architecture": "Dedicated ring-fenced: 3 (Strong Bull/Bull/Neutral) / 2 (Bearish)",
            "win_pct":           "73.9%",
            "ev_per_trade":      "+0.716%",
            "stocks":            rows,
        }
    except Exception as e:
        raise HTTPException(500, f"buy_s1_bounce_qualified failed: {e}")


@router.get("/sell_overbought")
def sell_overbought(limit: int = 50):
    try:
        open_pos  = _load_open_positions("sell_overbought")
        slot_full = _load_slot_full("sell_overbought")

        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH pivots AS (
                    SELECT symbol, price_date,
                        AVG((high+low+close)/3.0) OVER w AS pp,
                        MAX(high) OVER w AS h5, MIN(low) OVER w AS l5
                    FROM raw_prices WHERE price_date >= CURRENT_DATE - INTERVAL '14 days'
                    WINDOW w AS (PARTITION BY symbol ORDER BY price_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)
                ),
                latest_pivot AS (
                    SELECT symbol, pp, 2*pp-h5 AS s1, 2*pp-l5 AS r1, pp+(h5-l5) AS r2
                    FROM pivots WHERE price_date=(SELECT MAX(price_date) FROM pivots p2 WHERE p2.symbol=pivots.symbol)
                ),
                hi3d AS (SELECT symbol, MAX(high) AS max_high_3d FROM raw_prices
                    WHERE price_date>=CURRENT_DATE-INTERVAL '4 days' AND price_date<=CURRENT_DATE GROUP BY symbol),
                hi5d AS (SELECT symbol, MAX(high) AS max_high_5d FROM raw_prices
                    WHERE price_date>=CURRENT_DATE-INTERVAL '7 days' AND price_date<=CURRENT_DATE GROUP BY symbol),
                latest_close AS (
                    SELECT DISTINCT ON (symbol) symbol, close, price_date
                    FROM raw_prices WHERE price_date<=CURRENT_DATE ORDER BY symbol, price_date DESC
                )
                SELECT lc.symbol,
                    ROUND(lc.close::numeric,2) AS entry,
                    ROUND(lp.s1::numeric,2)    AS target,
                    ROUND(lp.r2::numeric,2)    AS stop,
                    ROUND(((lp.s1-lc.close)/NULLIF(lc.close,0)*100)::numeric,2) AS tgt_pct,
                    ROUND(((lp.r2-lc.close)/NULLIF(lc.close,0)*100)::numeric,2) AS sl_pct,
                    ROUND(h5d.max_high_5d::numeric,2) AS week_high,
                    ROUND(h3d.max_high_3d::numeric,2) AS high_3d,
                    ROUND(((lc.close-h3d.max_high_3d)/NULLIF(h3d.max_high_3d,0)*100)::numeric,2) AS fall_3d_pct,
                    ROUND(lp.r1::numeric,2) AS r1, ROUND(lp.r2::numeric,2) AS r2, ROUND(lp.pp::numeric,2) AS pp,
                    ROUND(vm.rsi_weekly::numeric,1) AS rsi_weekly,
                    ROUND(vm.rsi_month::numeric,1)  AS rsi_month,
                    ROUND(vm.sector_week::numeric,2) AS sector_week,
                    ROUND(vm.gvm_score::numeric,2)   AS gvm_score
                FROM latest_close lc
                JOIN futures_universe fu ON fu.symbol=lc.symbol AND fu.is_active=TRUE
                JOIN latest_pivot lp ON lp.symbol=lc.symbol
                JOIN hi3d h3d ON h3d.symbol=lc.symbol
                JOIN hi5d h5d ON h5d.symbol=lc.symbol
                JOIN v8_metrics vm ON vm.symbol=lc.symbol
                    AND vm.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                WHERE (h5d.max_high_5d>0.9*lp.r1 OR h5d.max_high_5d>0.9*lp.r2)
                  AND (lc.close-h3d.max_high_3d)/NULLIF(h3d.max_high_3d,0)*100 < -3.0
                  AND vm.rsi_weekly>=80 AND vm.rsi_month>=70 AND vm.sector_week<0
                  AND lp.s1<lc.close
                  AND lc.symbol NOT IN (
                      SELECT UPPER(ticker) FROM earnings_calendar
                      WHERE ex_date IN (CURRENT_DATE, CURRENT_DATE+INTERVAL '1 day'))
                ORDER BY vm.rsi_weekly DESC NULLS LAST LIMIT %s
            """, (min(max(limit, 1), 200),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        for r in rows: r["status"] = "QUALIFIED"
        rows = _enrich_with_status(rows, "sell_overbought", open_pos, slot_full)
        return {"basket": "sell_overbought", "count": len(rows),
                "target": "S1", "stop": "R2",
                "slot_architecture": "Dedicated ring-fenced: 4 (Bull/Neutral) / 3 (Bearish)",
                "win_pct": "81.5%", "ev_per_trade": "+1.56%", "stocks": rows}
    except Exception as e:
        raise HTTPException(500, f"sell_overbought failed: {e}")


@router.get("/adr")
def adr_only():
    try:
        with _conn() as conn, conn.cursor() as cur:
            adv, dec, unc, adr, source, adr_date = _read_adr(cur)
        return {"price_date": adr_date, "adr": adr, "advances": adv,
                "declines": dec, "unchanged": unc, "pass": adr >= 1.0, "source": source}
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
                                "close": round(float(c),2), "prev_close": round(prev_close,2),
                                "chg_pct": round((float(c)/prev_close-1)*100,2), "source": "live_intraday"}
                else:
                    cur.execute("""WITH d AS (SELECT price_date,open,high,low,close,
                        ROW_NUMBER() OVER (ORDER BY price_date DESC) rn FROM raw_prices WHERE symbol=%s)
                        SELECT a.price_date::text,a.open,a.high,a.low,a.close,
                        ROUND(((a.close-b.close)/NULLIF(b.close,0)*100)::numeric,2)
                        FROM d a JOIN d b ON b.rn=2 WHERE a.rn=1""", (sym,))
                    e = cur.fetchone()
                    if e:
                        out[sym] = {"price_date": e[0], "open": round(float(e[1]),2) if e[1] else None,
                                    "high": round(float(e[2]),2) if e[2] else None,
                                    "low":  round(float(e[3]),2) if e[3] else None,
                                    "close": round(float(e[4]),2) if e[4] else None,
                                    "chg_pct": round(float(e[5]),2) if e[5] else None, "source": "eod_fallback"}
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
