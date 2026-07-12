"""
V8 endpoints -- Quant Long-Short Basket Strategy

ADR (14-Jun-2026): _read_adr gates the live tiers behind _market_open().
ADR (11-Jun-2026): market_mood reads adr_intraday primary, falls back to adr_daily.
buy_reversal V3 (10-Jul-2026, spec id=2818): TRUE REVERSAL inverse-sandwich dip-buy. Dedicated
  strict-AND handler in v8_signal_writer (NOT the score-gate loop, NO regime). 8 conditions:
  daily_rsi<=40, true_weekly_rsi>=60, dma_200>=0, gvm>=6.5, mom_2d 0-3, hourly 0.1-1.0 (from 09:20),
  CMP>PP, room-to-R1>2%. Target R1 only, 1:1 mirror stop. ~35-55 sigs/yr, 62-64% WR, +1.0-1.2% EV.
  FILTER_CONFIG below carries only the v8_metrics-computable subset (the 4 daily-metric gates) for
  the funnel display; the 4 live/pivot gates are enforced live in the writer's dedicated handler.
buy_momentum GVM floor relaxed to 6.0 (23-Jun-2026) — was 7.0.
sell_reversal V4 LOCKED (16-Jun-2026): 5 strict AND | 79.3% WR | EV +0.752%/trade.
  Mood relaxation (18-Jun-2026): 4/5 in Strong Bullish + Bullish | 5/5 in Neutral/Bear.
sell_momentum V2 LOCKED (16-Jun-2026): 6 strict AND | 71.9% WR | EV +0.55%/trade.
  Mood relaxation (18-Jun-2026): 5/6 in Strong Bullish + Bullish | 6/6 in Neutral/Bear.
sell_overbought V2 LOCKED (16-Jun-2026): 5 strict AND | 81.5% WR | EV +1.56%/trade.
  Dedicated ring-fenced slots: 4 (Bull/Neutral) / 3 (Bearish). Total always 24.
  Funnel: dedicated so_funnel_detail() computes all 5 filters live from raw_prices.
buy_s1_bounce V2 LOCKED (10-Jul-2026): 7 filters (1 gate + 6 stages), week_return<=2.5, fixed +/-2.0% exits.
  Dedicated ring-fenced slots: 3 (Strong Bull/Bull/Neutral) / 2 (Bearish).
  Funnel: dedicated s1b_funnel_detail() computes all 7 filters live.
Generic funnel_detail stages emit survivors/killed (dashboard aliases for passes/fails).
Slot architecture (17-Jun-2026) SLOT_ARCHITECTURE_V2.4.0 id=379:
  Standard pool: Strong Bullish 15B/5S | Bullish 14B/6S | Neutral 12B/8S | Bearish 8B/13S
  SO dedicated: 4/4/4/3. S1B dedicated: 3/3/3/2.
"""

from fastapi import APIRouter, HTTPException, Response
from datetime import date, datetime, timedelta
from typing import Optional
import psycopg
import os
import time

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


FILTER_CONFIG = {
    # cc#354/355 V3: dedicated strict-AND handler in the writer. Only the 4 v8_metrics-computable
    # gates live here (for the funnel); true_weekly_rsi>=60, hourly 0.1-1.0, CMP>PP, room>2% are the
    # live/pivot gates enforced in _write_buy_reversal_v3_qualified. No regime overrides.
    "buy_reversal": {
        "daily_rsi":  [None, 40.0],   # cold short-term dip
        "dma_200":    [0.0,  None],   # long-term uptrend
        "gvm_score":  [6.5,  None],   # quality
        "mom_2d":     [0.0,   3.0],   # 2-day momentum turned up
    },
    "buy_momentum": {
        # V2 filter optimisation (cc_task #74, 24-Jun-2026): EOD backtest across 9
        # scenarios -> rsi_weekly 60-85->62-82, mom_2d 0->0.5 min, gvm revert 6.0->7.0.
        # WR 65.2->71.4%, EV +0.322->+0.517%, signals 288->~135/yr.
        "gvm_score":    [7.0,  10.0],
        "dma_50":       [8.0,  25.0],
        "dma_200":      [8.0,  40.0],
        "rsi_month":    [70.0, 100.0],
        "rsi_weekly":   [62.0, 82.0],
        "month_return": [2.0,  30.0],
        "week_return":  [0.5,  12.0],
        "mom_2d":       [0.5,   6.0],
        "sector_week":  [0.0,   6.0],
        "sector_month": [0.0,   6.0],
    },
    "sell_reversal": {
        # cc#378 SELL_REVERSAL_V5D (spec id=2894, supersedes V4 id=357). Dedicated strict-AND handler
        # (_write_sell_reversal_v5d_qualified) — OUT of the score-gate loop. These are the 3
        # v8_metrics-computable gates shown in the funnel; the live/pivot gates (TRUE weekly RSI<=40,
        # CMP<PP, room-to-S1/S2>=2%) are enforced in the handler and rendered via sr_funnel_detail.
        "daily_rsi":  [60.0, None],   # hot bounce
        "rsi_month":  [None, 50.0],   # weak monthly (engine enforces STRICT <50)
        "dma_200":    [None, 0.0],    # below the 200-DMA
    },
    "sell_momentum": {
        # cc#380 SELL_MOMENTUM_V3 (N5, spec id=2901, supersedes V2). Dedicated strict-AND handler
        # (_write_sell_momentum_v3_qualified) — OUT of the score-gate loop. These are the 6
        # v8_metrics-computable gates shown in the funnel; the live/pivot gates (TRUE weekly RSI<=45,
        # CMP<PP, S2-clearance>=3%) are enforced in the handler and rendered via sm_funnel_detail.
        "rsi_month":     [None, 40.0],    # weak monthly (engine enforces STRICT <40)
        "mom_2d":        [-4.0, -1.0],    # recent down-momentum
        "dma_200":       [None,  2.0],    # below / near 200-DMA
        "week_return":   [-10.0, -0.5],   # weak week
        "sector_week":   [None,  0.0],    # weak sector (engine enforces STRICT <0)
        "week_index_52": [20.0,  60.0],   # mid 52-week band
    },
    # cc#382 SELL_OVERBOUGHT_V3 (spec id=2912, replaces the never-firing V2). Dedicated handler
    # (_write_sell_overbought_v3_qualified). The 1 v8_metrics gate shown for reference; the live gates
    # (NIFTY RSI<=45, day HIGH>=R1, fall-from-2d-high 1-10%, day change<0) are handler-enforced and
    # rendered via so_funnel_detail. week_return here is the only v8_metrics-computable filter.
    "sell_overbought": {
        "week_return":  [-2.5, 0.0],   # mild weekly weakness
    },
    # buy_s1_bounce: 7 filters (1 gate + 6 stages). Reference cols only.
    "buy_s1_bounce": {
        "week_return":  [0.0,  2.5],   # cc#358 V2: cap 2.5 (was 3.0)
        "vol_ratio":    [1.5, None],
        "dma_50":       [0.0, None],
    },
}

# cc#378/380: SELL_REVERSAL_SL_MULT + SELL_MOMENTUM_SL_MULT retired — V5-D uses S1/S2 mirror, V3-N5 fixed +/-3%.

BASKET_META = {
    "buy_reversal":    {"side": "BUY",  "target": "R1",                        "win_pct": "62-64%", "signals_per_day": "~35-55/yr"},
    "buy_momentum":    {"side": "BUY",  "target": "+3.0% fixed",               "win_pct": "67% live", "signals_per_day": "~2/day"},
    "sell_reversal":   {"side": "SELL", "target": "S1/S2 dynamic",             "win_pct": "73% (12mo bt)", "signals_per_day": "~0.5/day"},
    "sell_momentum":   {"side": "SELL", "target": "-3.0% fixed",               "win_pct": "64% (1yr bt)", "signals_per_day": "~0.5/day"},
    "sell_overbought": {"side": "SELL", "target": "-2.0% fixed",               "win_pct": "59% (1yr bt)", "signals_per_day": "~0.4/day"},
    "buy_s1_bounce":   {"side": "BUY",  "target": "+2.0% fixed",               "win_pct": "73.9%", "signals_per_day": "~0.3/day"},
}

# ── cc#158 V2.1 candidate filters — hourly + w52 + fall_from_day_high ────────
# Refinement layer over the LOCKED baskets (specs id 1263-1268). Applied as a
# HARD GATE *after* the existing score-gate — NEVER folded into the score/
# threshold math — so a disabled group reverts each basket to exact locked
# behavior. Per-basket enable state lives in DB table v8_filter_state (read
# live each tick); kill-switches (v8_filter_killswitch.py) flip enabled=false
# and alert, never auto-re-enable.
#
# NULL-pass: a metric of None (hourly first hour / <12 fut bars, or missing
# w52/fall) never blocks a signal the locked logic already approved.
#
# sell_momentum week_index_52 is a MODIFY of a locked score-gate filter
# (<=20 -> <=30 when enabled), NOT a hard-gate add — keyed "_modify" and
# handled in the score-gate, so it is skipped by the hard-gate pass below.
V21_FILTERS = {
    # cc#360: buy_reversal V2.1 bands removed — buy_reversal is now V3 (dedicated strict-AND
    # handler that never calls the V2.1 hard gate). Absence is handled gracefully by every reader
    # (all use .get(basket,{}) or `basket in V21_FILTERS`), so no v21_pass is computed for it.
    "buy_momentum":    {"hourly_pct": {"min": 0.2, "max": 1.5},
                        "week_index_52": {"min": 60.0, "max": 100.0}},
    "buy_s1_bounce":   {"hourly_pct": {"min": 0.0, "min_excl": True, "max": 1.0},
                        "week_index_52": {"min": 50.0, "max": 90.0}},
    # cc#378: sell_reversal V2.1 bands removed — sell_reversal is now V5-D (dedicated strict-AND
    # handler, RAW: no market gate, no kill-switch, never calls the V2.1 hard gate). Absence is
    # handled by every reader (.get(basket,{}) / `basket in V21_FILTERS`), so no v21_pass for it.
    "sell_momentum":   {"hourly_pct": {"max": 0.0, "max_excl": True},
                        "week_index_52_modify": {"max": 30.0}},
    "sell_overbought": {"fall_from_day_high": {"max": -1.5}},
}

# Locked-spec WR baselines (per specs 1263-1268) for the WR kill-switch.
V21_BASELINE_WR = {
    "buy_reversal": 63.0, "buy_momentum": 67.0, "buy_s1_bounce": 73.9,  # cc#354/359 V2/V3 honest baselines
    "sell_reversal": 73.0, "sell_momentum": 64.0, "sell_overbought": 59.0,  # cc#378/380/382 honest baselines
}


def _v21_cond_pass(value, cond: dict) -> bool:
    """One V2.1 band check. None value NULL-passes. Supports exclusive
    floor/cap (min_excl / max_excl) for the strict >0 and <0 conditions."""
    if value is None:
        return True
    v = float(value)
    mn = cond.get("min"); mx = cond.get("max")
    if mn is not None:
        if cond.get("min_excl"):
            if v <= mn: return False
        elif v < mn:
            return False
    if mx is not None:
        if cond.get("max_excl"):
            if v >= mx: return False
        elif v > mx:
            return False
    return True


def v21_hard_gate_pass(basket: str, metrics: dict, enabled: bool, backtest: bool = False) -> bool:
    """cc#158 hard-gate layer. True if the stock passes this basket's ENABLED
    V2.1 refinement bands. Disabled -> always True (no-op = locked behavior).
    Keys ending in '_modify' are score-gate modifications, not hard gates, and
    are skipped here.

    cc#324 (founder-locked): backtest=True = BT7 BACKTEST mode — apply the EOD
    week_index_52 conditions ONLY; the live-only intraday refinements (hourly_pct,
    fall_from_day_high) are POLICY-SKIPPED (not merely NULL-passed by data absence),
    because 5yr hourly history does not exist and must never gate a historical
    backtest. backtest=False (default) = live + PARITY replays apply V2.1 in full."""
    if not enabled:
        return True
    for metric, cond in V21_FILTERS.get(basket, {}).items():
        if metric.endswith("_modify"):
            continue
        if backtest and metric != "week_index_52":
            continue   # cc#324: BACKTEST = w52 only; skip live-only intraday refinements
        if not _v21_cond_pass(metrics.get(metric), cond):
            return False
    return True


def _load_filter_state(conn) -> dict:
    """cc#164: per-basket V2.1 enable state (v8_filter_state), read live so the
    dashboard reflects a kill-switch trip immediately -- same fail-safe pattern
    as v8_signal_writer.py's _load_filter_state. On any error, return {} so
    every basket's hard gate displays as DISABLED (locked behavior), never
    accidentally-on."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT basket, enabled FROM v8_filter_state")
            return {b: bool(e) for b, e in cur.fetchall()}
    except Exception:
        return {}


def _load_v21_live_metrics(conn, symbols: list) -> dict:
    """cc#164: hourly_pct (fyers_fut 5m: last close vs 12-bars-ago) + fall_from_day_high
    (fyers_eq: live close vs today's high) -- read-only reproduction of the same live
    computation v8_signal_writer.py feeds into v21_hard_gate_pass, for dashboard display
    only. Returns {symbol: {"hourly_pct": float|None, "fall_from_day_high": float|None}}."""
    out = {s: {"hourly_pct": None, "fall_from_day_high": None} for s in symbols}
    if not symbols:
        return out
    today = date.today()
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
                FROM ranked WHERE rn IN (1, 13) GROUP BY symbol
            """, (today, symbols))
            for sym, last_close, close_12_ago in cur.fetchall():
                if last_close is not None and close_12_ago and float(close_12_ago) > 0:
                    out[sym]["hourly_pct"] = (float(last_close) / float(close_12_ago) - 1) * 100
    except Exception:
        pass
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol,
                       (SELECT close FROM intraday_prices i2
                        WHERE i2.symbol = ip.symbol AND i2.ts::date = %s AND i2.source = 'fyers_eq'
                        ORDER BY ts DESC LIMIT 1)              AS live_close,
                       MAX(high) FILTER (WHERE ts::date = %s) AS day_high
                FROM intraday_prices ip
                WHERE symbol = ANY(%s) AND ts::date = %s AND source = 'fyers_eq'
                GROUP BY symbol
            """, (today, today, symbols, today))
            for sym, live_close, day_high in cur.fetchall():
                if live_close is not None and day_high and float(day_high) > 0:
                    out[sym]["fall_from_day_high"] = (float(live_close) - float(day_high)) / float(day_high) * 100
    except Exception:
        pass
    return out


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
    # cc#354/355 V3: no Nifty-regime overrides — buy_reversal V3 uses fixed absolute gates.
    _, nifty_1m = _get_nifty_regime()
    return dict(FILTER_CONFIG["buy_reversal"]), "V3", nifty_1m


def _get_buy_momentum_target(regime: str) -> str:
    return "+3.0% fixed"   # cc#359 V2: fixed +/-3.0% exits (regime R2/R1 targeting retired)


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
    # cc#360: buy_reversal V3 is strict-AND — its NEAR_MISS watchlist must pass ALL 4 daily gates
    # (daily_rsi<=40, dma_200>=0, gvm>=6.5, mom_2d 0-3), not the n-1 fallback (which surfaced
    # hot-RSI names that are the opposite of a dip). Other baskets keep the n-1 near-miss.
    need      = n_filters if basket == "buy_reversal" else max(n_filters - 1, 1)

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
            if cmp and pv.get("r1"):   # cc#360: room-to-R1 % for the V3 buy_reversal column
                r["room_r1_pct"] = round((pv["r1"] - cmp) / cmp * 100, 2)
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
                SELECT p.symbol, p.entry_price, p.target, p.stop_loss, p.qty, p.entry_ts,
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


# cc#326: basket-tab 3-status taxonomy (OPEN / SIGNAL·reason / NEAR_MISS). Founder-locked
# 08-Jul: tabs show only actionable truth. SLOT_FULL is folded into SIGNAL·slots; a symbol
# whose trade already closed today (traded_today) is REMOVED — it is spent until tomorrow.
def _ist_today_sql() -> str:
    return "(NOW() AT TIME ZONE 'Asia/Kolkata')::date"   # cc#325: closed_at/exit_ts are naive IST


def _load_closed_today(basket: str) -> set:
    """Symbols on this basket's side whose paper trade CLOSED today (IST) — spent for today.
    Side-scoped, not basket-scoped: the engine's traded_today exclusion is symbol+side."""
    side = "LONG" if basket.startswith("buy") else "SHORT"
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(f"""
                SELECT DISTINCT symbol FROM v8_paper_trades
                WHERE side=%s AND COALESCE(closed_at, exit_ts)::date = {_ist_today_sql()}
            """, (side,))
            return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


def _load_conflict_syms(basket: str) -> set:
    """Symbols with an OPEN position on the OPPOSITE side (incl prior-day) -> SIGNAL·conflict."""
    opp = "SHORT" if basket.startswith("buy") else "LONG"
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM v8_paper_positions WHERE status='OPEN' AND side=%s", (opp,))
            return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


def _load_missed_reasons(basket: str) -> dict:
    """Engine missed-reasons for today (IST) -> {symbol: raw_reason}. Canonical SIGNAL source."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(f"""SELECT symbol, reason FROM v8_paper_missed
                            WHERE basket=%s AND miss_date = {_ist_today_sql()}""", (basket,))
            return {r[0]: r[1] for r in cur.fetchall()}
    except Exception:
        return {}


_MISS_REASON_MAP = {
    'slot_full': 'slots', 'slots': 'slots', 'slot_burst': 'slots',
    'blackout': 'blackout', 'earnings': 'blackout',
    'conflict': 'conflict', 'opposite_open': 'conflict', 'has_open': 'conflict',
    'after_cutoff': 'cutoff', 'cutoff': 'cutoff',
}


def _signal_reason(sym, signal_ts, slot_full, missed, conflict_syms) -> str:
    """Resolve the gate that blocked entry for a today-qualified, non-OPEN, non-traded-today row.
    Priority: conflict (opposite-side open) > explicit engine missed-reason > slots > cutoff > slots."""
    if sym in conflict_syms:
        return 'conflict'
    raw = missed.get(sym)
    if raw and raw in _MISS_REASON_MAP:
        return _MISS_REASON_MAP[raw]
    if sym in slot_full:
        return 'slots'
    try:
        if signal_ts is not None and hasattr(signal_ts, 'hour'):
            if signal_ts.hour > 15 or (signal_ts.hour == 15 and signal_ts.minute >= 20):
                return 'cutoff'
    except Exception:
        pass
    return 'slots'   # slots is the dominant gate; a valid signal that didn't enter was slot-gated


def _enrich_with_status(stocks: list, basket: str, open_pos: dict, slot_full: set,
                        closed_today: set = None, conflict_syms: set = None,
                        missed: dict = None) -> list:
    closed_today  = closed_today  or set()
    conflict_syms = conflict_syms or set()
    missed        = missed        or {}
    out = []
    for s in stocks:
        sym = s.get("symbol", "")
        pos = open_pos.get(sym)
        if pos:
            s["status"]       = "OPEN"
            # cc#253: SINCE for an OPEN row is the position's real entry time, NOT a
            # v8_qualified.signal_ts — a held position re-qualifies every day it still passes
            # the gate, so signal_ts drifts to the latest/first qual day, not the entry moment.
            if pos.get("entry_ts") is not None:
                s["signal_ts"] = pos["entry_ts"]
            s["entry_price"]  = float(pos["entry_price"]) if pos.get("entry_price") else None
            s["open_pnl_pct"] = float(pos["pnl_pct"])    if pos.get("pnl_pct")     else None
            s["open_target"]  = float(pos["target"])      if pos.get("target")      else None
            s["open_stop"]    = float(pos["stop_loss"])   if pos.get("stop_loss")   else None
            out.append(s)
            continue
        # cc#326: traded+closed today on this side -> spent, drop entirely (day log / trades is its home)
        if sym in closed_today:
            continue
        if s.get("status") == "NEAR_MISS":
            out.append(s)
            continue
        # everything surviving here qualified today but did not enter -> SIGNAL, gated
        s["status"] = "SIGNAL"
        s["signal_reason"] = _signal_reason(sym, s.get("signal_ts"), slot_full, missed, conflict_syms)
        out.append(s)
    return out


def _inject_open_positions(cur, rows: list, basket: str, open_pos: dict) -> list:
    """cc#240: every OPEN v8_paper_positions row must render on its basket tab, ALWAYS —
    even if it qualified on a prior day (its v8_qualified row is dated to entry day, so the
    signal_date=CURRENT_DATE endpoints never select it). For each open symbol not already in
    today's set, inject a DISPLAY row built from TODAY's live data: CMP from cmp_prices, pivots
    pp/r1/s1 from the latest v8_paper_pivots, technicals from v8_metrics at MAX(score_date).
    entry_price / open_pnl / target / stop are filled by _enrich_with_status from the position.
    Purely additive + read-only — never writes a v8_qualified row (EOD no-requal rule intact).
    Dedup: a symbol that also qualifies today stays as its today row (that row wins)."""
    present = {r.get("symbol") for r in rows}
    missing = [s for s in open_pos if s not in present]
    if not missing:
        return rows
    cur.execute("""
        SELECT u.symbol, m.gvm_score, m.day_1d, m.dma_50, m.dma_200,
               m.rsi_month, m.rsi_weekly, m.week_return, m.month_return, m.mom_2d,
               m.week_index_52, m.vol_ratio, m.sector_week, m.sector_month,
               g.segment, c.cmp, p.pp, p.r1, p.s1, fs.first_seen
        FROM unnest(%s::text[]) AS u(symbol)
        LEFT JOIN v8_metrics m ON m.symbol=u.symbol
            AND m.score_date=(SELECT MAX(score_date) FROM v8_metrics)
        LEFT JOIN gvm_scores g ON g.symbol=u.symbol
        LEFT JOIN cmp_prices c ON c.symbol=u.symbol
        LEFT JOIN v8_paper_pivots p ON p.symbol=u.symbol
            AND p.pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)
        LEFT JOIN (SELECT symbol, MIN(signal_ts) AS first_seen
                   FROM v8_qualified WHERE basket=%s GROUP BY symbol) fs ON fs.symbol=u.symbol
    """, (missing, basket))
    cols = [d[0] for d in cur.description]
    for r in cur.fetchall():
        row = dict(zip(cols, r))
        pos = open_pos.get(row["symbol"], {})
        if row.get("cmp") is None and pos.get("cmp") is not None:
            row["cmp"] = pos["cmp"]
        row["entry"]     = row.get("cmp")          # sell_overbought renderer keys on 'entry'
        row["source"]    = "open_position"         # cc#240: held position, not a fresh qual
        # cc#253: SINCE = real entry_ts (fallback first_seen if somehow missing). _enrich_with_status
        # re-affirms this, but set it here too so the injected row is correct independent of order.
        row["signal_ts"] = pos.get("entry_ts") or row.get("first_seen")
        row["status"]    = "OPEN"
        row["segment"]   = _seg_override(row["symbol"], row.get("segment"))
        rows.append(row)
    return rows


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
    # cc#417 fix_2: latest adr_daily row on a TRADING day — defensively exclude any weekend row
    # (Sat/Sun) even if one slipped in, so the mood gate never reads a phantom 0-ADR weekend row.
    cur.execute("""SELECT advances, declines, unchanged, adr, price_date FROM adr_daily
                   WHERE EXTRACT(DOW FROM price_date) BETWEEN 1 AND 5
                   ORDER BY price_date DESC LIMIT 1""")
    r = cur.fetchone()
    if r:
        adv, dec, unc = r[0] or 0, r[1] or 0, r[2] or 0
        adr = round(float(r[3]), 3) if r[3] is not None else 1.0
        return adv, dec, unc, adr, "adr_daily", str(r[4])
    return 0, 0, 0, 1.0, "no_data", str(date.today())


_ADR_CACHE = {"ts": 0.0, "data": None}

def _read_adr_cached(cur, ttl=60):
    """market_mood hits ADR (heavy intraday query) on every call — cache 60s."""
    now = time.time()
    if _ADR_CACHE["data"] is not None and (now - _ADR_CACHE["ts"]) < ttl:
        return _ADR_CACHE["data"]
    data = _read_adr(cur)
    _ADR_CACHE["ts"] = now
    _ADR_CACHE["data"] = data
    return data


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


@router.get("/market_mood")
def market_mood():
    try:
        with _conn() as conn, conn.cursor() as cur:
            advances, declines, unchanged, adr, breadth_source, adr_date = _read_adr_cached(cur)
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
            # cc#221 (display-only): live used-count for each ring-fenced pool so the Market
            # Gate card can show Sell Overbought + Buy S1 Bounce as used/cap. Each pool is
            # single-sided (SO=SHORT, S1B=LONG), so basket-only count == the engine's count.
            cur.execute("""SELECT basket, COUNT(*) FROM v8_paper_positions
                           WHERE status='OPEN' AND basket IN ('sell_overbought','buy_s1_bounce')
                           GROUP BY basket""")
            _pool_used = {r[0]: int(r[1]) for r in cur.fetchall()}
            so_used  = _pool_used.get("sell_overbought", 0)
            s1b_used = _pool_used.get("buy_s1_bounce", 0)
            return {
                "checked_at": str(date.today()), "checks": checks,
                "fails": fails, "mood": mood,
                "buy_slots": buy_slots, "sell_slots": sell_slots,
                "so_slots": so_slots, "s1b_slots": s1b_slots, "total_slots": total_slots,
                "so_pool":  {"cap": so_slots,  "used": so_used},    # cc#221 display-only
                "s1b_pool": {"cap": s1b_slots, "used": s1b_used},   # cc#221 display-only
                "slot_note": "so_slots ring-fenced for sell_overbought; s1b_slots ring-fenced for buy_s1_bounce -- never compete with standard pools",
                "breadth_source": breadth_source, "nifty_source": nifty_source,
                "adr_detail": {"advances": advances, "declines": declines,
                               "unchanged": unchanged, "adr_date": adr_date,
                               "source": breadth_source},
            }
    except Exception as e:
        raise HTTPException(500, f"market_mood failed: {e}")


def _offhours_metrics_all(cur):
    """cc#418 (extends cc#417 fix_3 from index-only to ALL symbols): last-session finals for the Raw
    Data intraday columns (hour%, fall%, Intra%/day_ret, Rec2D%, WkLow%). Anchored to the market's
    last session (all symbols share it off-market). Same formulas as the live path. '--' remains only
    for symbols with no recent session bars. Called only when CURRENT_DATE has no fyers_eq bars."""
    cur.execute("SELECT MAX(ts::date) FROM intraday_prices WHERE source='fyers_eq' AND ts >= NOW() - INTERVAL '12 days'")
    r0 = cur.fetchone()
    ls = r0[0] if r0 else None
    if not ls:
        return {}
    cur.execute("""
        WITH td AS (
            SELECT symbol,
                (array_agg(open  ORDER BY ts ASC ))[1]  AS day_open,
                (array_agg(close ORDER BY ts DESC))[1]  AS live_close,
                MAX(high) AS day_high, MIN(low) AS today_low,
                (array_agg(close ORDER BY ts DESC))[13] AS close_12_ago
            FROM intraday_prices WHERE ts::date = %s AND source = 'fyers_eq' GROUP BY symbol
        ),
        hist AS (
            SELECT symbol, MIN(low) FILTER (WHERE rn<=2) AS lo_2d, MIN(low) FILTER (WHERE rn<=5) AS lo_5d
            FROM (SELECT symbol, low, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
                  FROM raw_prices WHERE price_date < %s) x WHERE rn<=5 GROUP BY symbol
        )
        SELECT td.symbol, td.day_open, td.live_close, td.day_high, td.today_low, td.close_12_ago, h.lo_2d, h.lo_5d
        FROM td LEFT JOIN hist h ON h.symbol = td.symbol
    """, (ls, ls))

    def _ff(v):
        try: return float(v) if v is not None else None
        except (TypeError, ValueError): return None
    out = {}
    for r in cur.fetchall():
        cmpv = _ff(r[2])
        if cmpv is None:
            continue
        op, dh, tlow, c12, lo2, lo5 = (_ff(r[1]), _ff(r[3]), _ff(r[4]), _ff(r[5]), _ff(r[6]), _ff(r[7]))
        wl_cand = [x for x in (lo5, tlow) if x is not None]
        week_low = min(wl_cand) if wl_cand else None
        out[r[0]] = {
            "hourly_pct": (cmpv / c12 - 1) * 100 if (c12 and c12 > 0) else None,
            "fall_from_day_high": (cmpv - dh) / dh * 100 if (dh and dh > 0) else None,
            "day_ret": (cmpv - op) / op * 100 if (op and op > 0) else None,
            "recovery_2d": (cmpv - lo2) / lo2 * 100 if (lo2 and lo2 > 0) else None,
            "week_low_pct": (cmpv - week_low) / week_low * 100 if (week_low and week_low > 0) else None,
        }
    return out


@router.get("/metrics/all")
def metrics_all():
    """Flat array of every stock's latest v8_metrics + segment.
    Powers the dashboard Master tab (Top/Bottom Sectors). Same universe build
    as /scan: same query, same float cleanup, same _seg_override."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT m.symbol, m.gvm_score, m.mom_2d, m.day_1d, m.eod_chg,
                   m.week_return, m.month_return, m.year_return,
                   m.dma_20, m.dma_50, m.dma_200,
                   m.rsi_weekly, m.rsi_month, m.daily_rsi,
                   m.vol_ratio, m.week_index_52,
                   -- cc#231/#232: month_index + ma9_vs_ma21 kept; 4 dead range/BB
                   -- metrics dropped (cc#232).
                   m.month_index, m.ma9_vs_ma21,
                   m.sector_week, m.sector_month,
                   g.segment, g.verdict
            FROM v8_metrics m
            LEFT JOIN gvm_scores g ON g.symbol = m.symbol
            WHERE m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
            -- cc#298: g.verdict (Excellent/Good/Average/Weak) joins alongside the existing
            -- m.gvm_score (verified equal to g.gvm_score) for the sector-detail GVM column.
            -- gvm_scores is a single-snapshot table (1 row/symbol) so this LEFT JOIN never fans out.
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        # cc#233: live-join hourly_pct + cc#235 fall_from_day_high (fyers_fut path, not
        # v8_metrics columns). NULL before ~10:15 IST (needs 12 fut bars), not an error.
        v21 = _load_v21_live_metrics(conn, [s["symbol"] for s in rows])
        for s in rows:
            s["hourly_pct"]         = v21.get(s["symbol"], {}).get("hourly_pct")
            s["fall_from_day_high"] = v21.get(s["symbol"], {}).get("fall_from_day_high")  # cc#235: free
        # cc#235: recovery_2d / day_ret / week_low_pct — S1B/SO filter inputs. Single-pass
        # CTE + formulas copied from the ENGINE (_load_intraday_bars: fyers_eq pinned per
        # cc#140, array_agg first/last; writer recovery/day_ret/week_low math) for
        # tab==engine parity. NOTE: _s1b_funnel_stages' td CTE is source-UNFILTERED (mixes
        # eq/fut/yahoo) — a latent funnel bug; the tab intentionally matches the engine.
        cur.execute("""
            WITH td AS (
                SELECT symbol,
                    (array_agg(open  ORDER BY ts ASC ))[1] AS day_open,
                    (array_agg(close ORDER BY ts DESC))[1] AS live_close,
                    MIN(low) AS today_low
                FROM intraday_prices
                WHERE ts::date = CURRENT_DATE AND source = 'fyers_eq'
                GROUP BY symbol
            ),
            hist AS (
                SELECT symbol,
                    MIN(low) FILTER (WHERE rn<=2) AS lo_2d,
                    MIN(low) FILTER (WHERE rn<=5) AS lo_5d
                FROM (SELECT symbol, low,
                        ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
                      FROM raw_prices WHERE price_date < CURRENT_DATE) x
                WHERE rn<=5 GROUP BY symbol
            )
            SELECT td.symbol, td.day_open, td.live_close, td.today_low, h.lo_2d, h.lo_5d
            FROM td LEFT JOIN hist h ON h.symbol = td.symbol
        """)
        s1b_live = {r[0]: r for r in cur.fetchall()}

    def _f(v):
        try: return float(v) if v is not None else None
        except (TypeError, ValueError): return None
    for s in rows:
        d = s1b_live.get(s["symbol"])
        # SELECT order: symbol[0], day_open[1], live_close[2], today_low[3], lo_2d[4], lo_5d[5]
        op   = _f(d[1]) if d else None   # day_open
        cmpv = _f(d[2]) if d else None   # live_close (this is the live cmp)
        tlow = _f(d[3]) if d else None   # today_low
        lo2  = _f(d[4]) if d else None   # lo_2d (raw_prices daily)
        lo5  = _f(d[5]) if d else None   # lo_5d (raw_prices daily)
        s["recovery_2d"] = ((cmpv - lo2) / lo2 * 100) if (cmpv and lo2 and lo2 > 0) else None
        s["day_ret"]     = ((cmpv - op) / op * 100)   if (cmpv and op and op > 0)   else None
        wl_cand = [x for x in (lo5, tlow) if x is not None]
        week_low = min(wl_cand) if wl_cand else None
        s["week_low_pct"] = ((cmpv - week_low) / week_low * 100) if (cmpv and week_low and week_low > 0) else None

    # cc#417 fix_3 / cc#418: off-market (no CURRENT_DATE fyers_eq bars) -> anchor the intraday-derived
    # columns (hour%, fall%, Intra%, Rec2D%, WkLow%) to each symbol's last-session finals for EVERY
    # symbol so Raw Data shows Friday's values, not blanks. During market hours (incl the pre-10:15
    # warmup) today HAS bars, so this is skipped and live values / warmup '--' are preserved.
    with _conn() as _c2, _c2.cursor() as _cur2:
        _cur2.execute("SELECT EXISTS(SELECT 1 FROM intraday_prices WHERE ts::date=CURRENT_DATE AND source='fyers_eq')")
        _today_has_bars = _cur2.fetchone()[0]
        _off = {} if _today_has_bars else _offhours_metrics_all(_cur2)
    if _off:
        for s in rows:
            fb = _off.get(s["symbol"])
            if not fb:
                continue
            for k in ("hourly_pct", "fall_from_day_high", "day_ret", "recovery_2d", "week_low_pct"):
                if s.get(k) is None and fb.get(k) is not None:
                    s[k] = fb[k]

    for s in rows:
        s["segment"] = _seg_override(s["symbol"], s.get("segment"))
        for k, v in list(s.items()):
            if k not in ("symbol", "segment", "verdict") and v is not None:   # cc#298: verdict stays a string
                try: s[k] = float(v)
                except (TypeError, ValueError): pass
    return rows


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
                         "dynamic": False})
        # cc#354/355: V3 has no Nifty regime; 4 daily-metric gates shown here, 4 live/pivot gates
        # (true_weekly_rsi>=60, hourly 0.1-1.0 from 09:20, CMP>PP, room-to-R1>2%) enforced live.
        return {
            "basket": basket, "filters": rows, "count": len(rows),
            "regime": "V3", "nifty_1m_return": round(nifty_1m, 2),
            "live_gates": ["true_weekly_rsi >= 60 (true calendar weekly)", "hourly_pct 0.1-1.0 (from 09:20)",
                           "CMP > PP", "room-to-R1 > 2%"],
            "entry_exit": "entry live CMP, target R1 only, 1:1 mirror stop, max hold 15d",
            "backtest": {"signals": "~35-55/yr", "wr_pct": "62-64", "ev_per_trade": "1.0-1.2"},
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
            "target": "+3.0% fixed", "target_rule": "cc#359 V2: fixed +3.0% / -3.0% (1:1), frozen at entry",
            "stop": "-3.0% fixed",
            "regime_rules": {
                "BULL":    {"condition": "Nifty 1M > +2%", "slots": 15},
                "NEUTRAL": {"condition": "-2% to +2%",     "slots": 12},
                "BEAR":    {"condition": "Nifty 1M < -2%", "slots": 8},
            },
            "backtest": {"note": "Live 30d: 12 trades, 67% WR at 1:1; V2 fixed-3% replay: 81.8%"},
            **BASKET_META.get(basket, {})
        }
    if basket == "sell_reversal":
        # cc#378 SELL_REVERSAL_V5D (spec id=2894). 3 v8_metrics gates + 3 live/pivot gates.
        rows = []
        for metric, bounds in FILTER_CONFIG["sell_reversal"].items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            rows.append({"metric": metric, "min": mn, "max": mx,
                         "min_display": "" if mn is None else mn,
                         "max_display": "" if mx is None else mx})
        rows += [{"metric": "true_weekly_rsi", "condition": "<= 40 (TRUE calendar weekly)"},
                 {"metric": "cmp_lt_pp",        "condition": "CMP < PP"},
                 {"metric": "room",             "condition": "target (S1 or S2) >= 2% from entry"}]
        return {
            "basket": basket, "filters": rows, "count": len(rows),
            "target": "S1/S2 dynamic",
            "target_formula": "S1 if (CMP-S1)/CMP >= 2% else S2 (never beyond S2); no signal if <2%",
            "stop": "1:1 mirror = entry + (entry - target)",
            "gate_note": "V5-D (id=2894): strict AND of 6, RAW — no market gate, no auto kill-switch (founder-locked 11-Jul).",
            "backtest": {"trades": 126, "wr_pct": 73.3, "window": "12mo episode-level 1:1 (+2.00%/trade)"},
            **BASKET_META.get(basket, {})
        }
    if basket == "sell_momentum":
        # cc#380 SELL_MOMENTUM_V3 (N5, spec id=2901). 6 v8_metrics gates + 3 live/pivot gates.
        rows = []
        for metric, bounds in FILTER_CONFIG["sell_momentum"].items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            rows.append({"metric": metric, "min": mn, "max": mx,
                         "min_display": "" if mn is None else mn,
                         "max_display": "" if mx is None else mx})
        rows += [{"metric": "true_weekly_rsi", "condition": "<= 45 (TRUE calendar weekly)"},
                 {"metric": "cmp_lt_pp",        "condition": "CMP < PP"},
                 {"metric": "s2_clearance",     "condition": "(CMP-S2)/CMP >= 3%"}]
        return {
            "basket": basket, "filters": rows, "count": len(rows),
            "target": "-3.0% fixed", "target_formula": "entry * 0.97 (frozen at entry)",
            "stop": "+3.0% fixed = entry * 1.03 (true 1:1)",
            "gate_note": "V3-N5 (id=2901): strict AND of 9, dedicated handler; fixed +/-3% exits (true 1:1).",
            "backtest": {"trades": 183, "wr_pct": 64.0, "window": "1yr episode-level EOD (+0.80/trade, +147pts)"},
            **BASKET_META.get(basket, {})
        }
    if basket == "sell_overbought":
        # cc#382 SELL_OVERBOUGHT_V3 (spec id=2912). Fresh same-day R1 rejection short.
        return {
            "basket": basket,
            "principle": "Fresh same-day R1 rejection short (S1-Bounce mirror) in a bear regime",
            "filters": [
                {"metric": "nifty_rsi",   "condition": "<= 45 (NIFTY daily RSI market gate)"},
                {"metric": "high_ge_r1",  "condition": "day session HIGH >= R1 (touched TODAY)"},
                {"metric": "fall_2d",     "condition": "fall from 2-day high 1% to 10%"},
                {"metric": "day_red",     "condition": "day change < 0"},
                {"metric": "week_return", "condition": "-2.5% to 0%"},
            ],
            "count": 5,
            "target": "-2.0% fixed", "target_formula": "entry * 0.98 (frozen at entry)",
            "stop": "+2.0% fixed",   "stop_formula":   "entry * 1.02 (true 1:1)",
            "slot_architecture": {"strong_bullish": 4, "bullish": 4, "neutral": 4, "bearish": 3,
                                  "note": "Ring-fenced -- never competes with standard sell pool"},
            "backtest": {"trades": 112, "wr_pct": 59.0, "window": "1yr episode-level EOD (+0.36/trade, +40pts)"},
            **BASKET_META.get(basket, {})
        }
    if basket == "buy_s1_bounce":
        return {
            "basket": basket,
            "principle": "Bounce from pivot S1 support -- 7 filters (1 gate + 6 stages)",
            "filters": [
                {"metric": "nifty_rsi (market gate)", "condition": ">= 55"},
                {"metric": "week_return",             "condition": "0% to 2.5%"},
                {"metric": "dma_50",                  "condition": "> 0%"},
                {"metric": "vol_ratio",               "condition": ">= 1.5x"},
                {"metric": "recovery_2d",             "condition": "2% to 8%"},
                {"metric": "day_ret",                 "condition": "> 0.5% (implies close > open)"},
                {"metric": "week_low_vs_s1",          "condition": "week_low <= pivot S1"},
            ],
            "count": 7,
            "note": "close_vs_open is implied by day_ret>0.5% -- not a separate filter.",
            "target": "+2.0% fixed from entry", "stop": "-2.0% fixed from entry",
            "slot_architecture": {"strong_bullish": 3, "bullish": 3, "neutral": 3, "bearish": 2,
                                  "note": "Ring-fenced -- never competes with standard buy pool"},
            "backtest": {"signals": 88, "wr_pct": 73.9, "expected_value": 0.716},
            **BASKET_META.get(basket, {})
        }
    rows = []
    for metric, bounds in FILTER_CONFIG[basket].items():
        mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
        rows.append({"metric": metric, "min": mn, "max": mx,
                     "min_display": "" if mn is None else mn,
                     "max_display": "" if mx is None else mx})
    return {"basket": basket, "filters": rows, "count": len(rows), **BASKET_META.get(basket, {})}


# ── V10 ATM-option enrichment (task 51) ──────────────────────────────────────
def _nearest_nse_expiry(today: date) -> date:
    """Nearest NSE monthly expiry = last Tuesday of the month (next month if passed)."""
    def last_tue(y: int, m: int) -> date:
        nxt  = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        last = nxt - timedelta(days=1)
        return last - timedelta(days=(last.weekday() - 1) % 7)   # Tuesday == weekday 1
    exp = last_tue(today.year, today.month)
    if exp < today:
        ny = today.year + (1 if today.month == 12 else 0)
        nm = 1 if today.month == 12 else today.month + 1
        exp = last_tue(ny, nm)
    return exp


def _enrich_atm_options(rows: list, cur) -> list:
    """Add cPx, pPx, hVol, ivp, atm_strike, dte to each qualified row (task 51)."""
    if not rows:
        return rows
    symbols = list({r["symbol"] for r in rows if r.get("symbol")})
    if not symbols:
        return rows

    hvol_map = {}
    cur.execute("""
        WITH dr AS (
            SELECT symbol,
                   LN(close::numeric / LAG(close::numeric)
                       OVER (PARTITION BY symbol ORDER BY price_date)) AS lr
            FROM raw_prices
            WHERE price_date >= CURRENT_DATE - 35 AND symbol = ANY(%s)
        )
        SELECT symbol, ROUND((STDDEV(lr) * SQRT(252))::numeric, 4) AS hvol
        FROM dr WHERE lr IS NOT NULL GROUP BY symbol
    """, (symbols,))
    for sym, hv in cur.fetchall():
        if hv is None:
            continue
        hv = float(hv)
        if hv < 0.05 or hv > 2.0:
            print(f"[v10] hVol out of range for {sym}: {hv} -- capping to [0.05, 2.0]")
            hv = min(max(hv, 0.05), 2.0)
        hvol_map[sym] = hv

    ivp_map = {}
    cur.execute("""
        WITH lr AS (
            SELECT symbol, price_date,
                   LN(close::numeric / LAG(close::numeric)
                       OVER (PARTITION BY symbol ORDER BY price_date)) AS lr
            FROM raw_prices
            WHERE price_date >= CURRENT_DATE - 270 AND symbol = ANY(%s)
        ),
        hv AS (
            SELECT symbol, price_date,
                   STDDEV(lr) OVER (PARTITION BY symbol ORDER BY price_date
                         ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) * SQRT(252) AS hv
            FROM lr WHERE lr IS NOT NULL
        ),
        ranked AS (
            SELECT symbol, hv, price_date,
                   PERCENT_RANK() OVER (PARTITION BY symbol ORDER BY hv) AS pr
            FROM hv WHERE hv IS NOT NULL
        ),
        latest AS (
            SELECT DISTINCT ON (symbol) symbol, ROUND((pr * 100)::numeric) AS ivp
            FROM ranked ORDER BY symbol, price_date DESC
        )
        SELECT symbol, ivp FROM latest
    """, (symbols,))
    for sym, ivp in cur.fetchall():
        ivp_map[sym] = int(ivp) if ivp is not None else None

    today       = _ist_now().date()
    nearest_exp = _nearest_nse_expiry(today)
    dte         = max((nearest_exp - today).days, 0)

    cur.execute("SELECT MIN(expiry) FROM option_chain WHERE expiry >= CURRENT_DATE")
    oc_row = cur.fetchone()
    oc_exp = oc_row[0] if oc_row else None

    for r in rows:
        sym  = r["symbol"]
        r["hVol"] = hvol_map.get(sym)
        r["ivp"]  = ivp_map.get(sym)
        r["dte"]  = dte
        r["cPx"]  = None
        r["pPx"]  = None
        try:
            spot = float(r.get("cmp")) if r.get("cmp") is not None else None
        except (TypeError, ValueError):
            spot = None
        r["atm_strike"] = round(spot / 50) * 50 if spot else None

    if oc_exp:
        for r in rows:
            atm = r.get("atm_strike")
            if atm is None:
                continue
            cur.execute("""
                SELECT option_type, ltp FROM option_chain
                WHERE underlying = %s AND strike = %s AND expiry = %s
                  AND ts = (SELECT MAX(ts) FROM option_chain
                            WHERE underlying = %s AND expiry = %s)
            """, (r["symbol"], atm, oc_exp, r["symbol"], oc_exp))
            for ot, ltp in cur.fetchall():
                if ltp is None:
                    continue
                if ot == "CE":   r["cPx"] = float(ltp)
                elif ot == "PE": r["pPx"] = float(ltp)
    return rows


def _enrich_qualified_result(res: dict) -> dict:
    """Wrap any qualified-style result and enrich its 'stocks' with ATM option fields."""
    try:
        stocks = res.get("stocks") if isinstance(res, dict) else None
        if stocks:
            with _conn() as conn, conn.cursor() as cur:
                _enrich_atm_options(stocks, cur)
    except Exception as ex:
        print(f"[v10] ATM enrichment failed: {ex}")
    return res


@router.get("/qualified/{basket}")
def qualified(basket: str, response: Response, limit: int = 50):
    response.headers["Cache-Control"] = "max-age=300"   # 5-min — matches signal cadence
    basket = basket.lower()
    if basket == "sell_overbought": return _enrich_qualified_result(sell_overbought(limit=limit))
    if basket == "buy_s1_bounce":   return _enrich_qualified_result(buy_s1_bounce_qualified(limit=limit))
    if basket not in FILTER_CONFIG: raise HTTPException(404, f"Unknown basket: {basket}")
    try:
        open_pos      = _load_open_positions(basket)
        slot_full     = _load_slot_full(basket)
        closed_today  = _load_closed_today(basket)      # cc#326
        conflict_syms = _load_conflict_syms(basket)     # cc#326
        missed        = _load_missed_reasons(basket)    # cc#326
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT q.symbol, q.gvm_score, q.cmp,
                    q.dma_50, q.dma_200, q.rsi_month, q.rsi_weekly, q.daily_rsi,
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
                if r.get("cmp") and r.get("r1"):   # cc#360: room-to-R1 % for the V3 column
                    r["room_r1_pct"] = round((float(r["r1"]) - float(r["cmp"])) / float(r["cmp"]) * 100, 2)
                r["status"] = r.pop("stored_status", None) or "QUALIFIED"
        for r in rows:
            r['segment'] = _seg_override(r['symbol'], r.get('segment'))
        with _conn() as conn, conn.cursor() as cur:      # cc#240: inject prior-day OPEN positions
            rows = _inject_open_positions(cur, rows, basket, open_pos)
        rows = _enrich_with_status(rows, basket, open_pos, slot_full,
                                   closed_today, conflict_syms, missed)   # cc#326
        extra = {}
        if basket == "buy_momentum":
            regime, nifty_1m = _get_nifty_regime()
            extra = {"regime": regime, "nifty_1m": round(nifty_1m, 2),
                     "target": _get_buy_momentum_target(regime)}
        elif basket == "sell_reversal":
            # cc#378 V5-D: dynamic S1/S2 target + 1:1 mirror stop (frozen at entry); no V4 SL_MULT.
            extra = {"target": "S1/S2 dynamic",
                     "target_formula": "S1 if (CMP-S1)/CMP >= 2% else S2 (never beyond S2)",
                     "stop_formula": "1:1 mirror = entry + (entry - target)"}
        elif basket == "sell_momentum":
            # cc#380 V3-N5: fixed -/+3% exits (frozen at entry); no V2 SL_MULT.
            extra = {"target": "-3.0% fixed", "target_formula": "entry * 0.97",
                     "stop_formula": "+3.0% fixed = entry * 1.03 (true 1:1)"}
        return _enrich_qualified_result({"basket": basket, "count": len(rows), "stocks": rows,
                "source": source_note, **BASKET_META.get(basket, {}), **extra})
    except Exception as e:
        raise HTTPException(500, f"qualified failed: {e}")


def _basket_cmp(cur):
    cur.execute("SELECT symbol, cmp FROM cmp_prices WHERE cmp IS NOT NULL")
    return {r[0]: float(r[1]) for r in cur.fetchall()}


@router.get("/funnel/{basket}")
def funnel_counts(basket: str):
    basket = basket.lower()
    if basket == "buy_s1_bounce":   return s1b_funnel_counts()
    if basket == "sell_overbought": return so_funnel_counts()
    if basket not in FILTER_CONFIG: raise HTTPException(404, f"Unknown basket: {basket}")
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT symbol, gvm_score, dma_50, dma_200, dma_20,
                       rsi_month, rsi_weekly, daily_rsi, month_return, week_return,
                       year_return, mom_2d, week_index_52, ma9_vs_ma21, vol_ratio,
                       sector_week, sector_month FROM v8_metrics
                       WHERE score_date=(SELECT MAX(score_date) FROM v8_metrics)""")
            cols = [d[0] for d in cur.description]
            all_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            # cc#164: V2.1 enable state + live hourly/fall metrics, read fresh on every
            # call regardless of the precomputed/live-fallback branch below -- display
            # only, never written back to v8_funnel_counts (that write stays inside
            # v8_signal_writer.py, untouched).
            v21_enabled = _load_filter_state(conn).get(basket, False)
            v21_metrics = _load_v21_live_metrics(conn, [r["symbol"] for r in all_rows])
            cur.execute("SELECT counts FROM v8_funnel_counts WHERE basket=%s AND score_date=CURRENT_DATE ORDER BY computed_at DESC LIMIT 1", (basket,))
            row = cur.fetchone()
        v21_pass = None
        if basket in V21_FILTERS:
            v21_pass = sum(1 for s in all_rows
                           if v21_hard_gate_pass(basket, {**s, **v21_metrics.get(s["symbol"], {})}, v21_enabled))
        # cc#354/355: buy_reversal V3 is a dedicated-handler basket — the writer no longer
        # precomputes its funnel, so ignore any stale V2 v8_funnel_counts row and always compute
        # the V3 daily-gate funnel live.
        if row and basket != "buy_reversal":
            counts = row[0] if isinstance(row[0], dict) else {}
            counts = {**counts, "_v21_enabled": v21_enabled, "_v21_pass": v21_pass}
            return {"basket": basket, "score_date": str(date.today()), "counts": counts, "source": "precomputed"}
        filters = FILTER_CONFIG[basket]; universe = all_rows[:]; counts = {}
        for metric, bounds in filters.items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            universe = [s for s in universe if _passes_filter(s.get(metric), mn, mx)]
            counts[metric] = len(universe)
        counts["_v21_enabled"] = v21_enabled
        counts["_v21_pass"] = v21_pass
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
    # cc#378: s2 added (not in WHERE, so no symbol drops) for sell_reversal V5-D's S1/S2 target.
    cur.execute("""SELECT symbol, pp, r1, s1, s2 FROM v8_paper_pivots
        WHERE pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)
          AND pp IS NOT NULL AND r1 IS NOT NULL""")
    return {r[0]: {"pp": float(r[1]), "r1": float(r[2]), "s1": float(r[3]),
                   "s2": float(r[4]) if r[4] is not None else None} for r in cur.fetchall()}


def _sr_dynamic_target(cmp, s1, s2):
    """cc#378: sell_reversal V5-D dynamic target — S1 if (cmp-S1)/cmp >= 2% else S2 (never beyond
    S2); no valid target if even S2 is < 2% away. Returns (target, room_pct) or (None, None)."""
    if cmp is None or s1 is None or cmp <= 0:
        return None, None
    room_s1 = (cmp - s1) / cmp * 100.0
    if room_s1 >= 2.0:
        return s1, round(room_s1, 3)
    if s2 is not None:
        room_s2 = (cmp - s2) / cmp * 100.0
        if room_s2 >= 2.0:
            return s2, round(room_s2, 3)
    return None, None


# cc#357: buy_reversal V3 is a dedicated strict-AND basket; 4 of its 8 gates are live/pivot-based
# (not v8_metrics columns), so the funnel is CAPTURED by the writer (_write_buy_reversal_v3_qualified
# -> v8_funnel_counts) and reshaped here — never recomputed from FILTER_CONFIG. Static stage order +
# display labels (labels carry spaces so the dashboard renders them verbatim via metric.replace).
_BR_V3_STAGES = [
    ("daily_rsi",       "daily RSI",             "",       "<= 40"),
    ("dma_200",         "dma 200",               ">= 0",   ""),
    ("gvm_score",       "gvm score",             ">= 6.5", ""),
    ("mom_2d",          "mom 2d",                ">= 0",   "<= 3"),
    ("hourly_pct",      "hourly % (from 09:20)", ">= 0.1", "<= 1.0"),
    ("cmp_gt_pp",       "CMP > PP",              "",       ""),
    ("room_r1",         "room to R1",            "> 2%",   ""),
    ("true_weekly_rsi", "true weekly RSI",       ">= 60",  ""),
]

def br_funnel_detail():
    """cc#357/364: 8-stage funnel for buy_reversal V3, reshaped from the handler-written
    v8_funnel_counts row (score_date=today). cc#364 FOUNDER DIRECTIVE 11-Jul: rows are INDEPENDENT
    per-filter pass counts across the universe (buy_momentum convention), NOT cumulative survivors —
    each of the 7 cheap gates is passes/fails vs the whole universe; true_weekly_rsi (stage 8) is
    passes vs the stocks that cleared all 7 cheap gates. Final still = strict-AND of all 8.
    Empty stages (all 0) until the first live tick writes."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT counts FROM v8_funnel_counts WHERE basket='buy_reversal' "
                        "AND score_date=CURRENT_DATE ORDER BY computed_at DESC LIMIT 1")
            row = cur.fetchone()
        counts   = (row[0] if row and isinstance(row[0], dict) else {}) or {}
        universe = int(counts.get("_universe", 0) or 0)
        stage7   = counts.get("_stage7_survivors")
        stage7   = int(stage7) if stage7 is not None else None
        final    = int(counts.get("_score_qualified", 0) or 0)
        stages = []
        for key, label, cmin, cmax in _BR_V3_STAGES:
            passes = int(counts.get(key, 0) or 0)
            # true_weekly_rsi (stage 8) is only evaluated on the 7-gate survivors -> its denominator
            # is that survivor count, not the full universe (the heavy read is skipped for the rest).
            denom = (stage7 if stage7 is not None else universe) if key == "true_weekly_rsi" else universe
            fails = max(denom - passes, 0)
            stage = {"metric": label, "condition_min": cmin, "condition_max": cmax,
                     "passes": passes, "fails": fails,
                     "survivors": passes, "killed": fails,
                     "pass_pct": round(passes / denom * 100, 1) if denom else 0}
            if key == "true_weekly_rsi":
                stage["denominator"] = denom
                stage["note"] = f"of {denom} stocks passing all 7 cheap gates"
            stages.append(stage)
        return {
            "basket": "buy_reversal", "score_date": str(date.today()),
            "universe": universe, "final": final,
            "filter_count": 8, "n_filters": 8,
            "stage7_survivors": stage7,
            "gate_type": "independent per-filter counts; final = strict AND of all 8",
            "score_qualified": final, "pivot_pass": final,
            "stages": stages, **BASKET_META.get("buy_reversal", {}),
        }
    except Exception as e:
        raise HTTPException(500, f"br_funnel_detail failed: {e}")


# cc#364: buy_reversal V3 pass-count gates — the 4 cheap daily-metric gates (spec id=2818).
# The other 4 of the 8 (hourly_pct, cmp>pp, room-to-R1, true_weekly_rsi) are live/pivot/heavy and
# computed inline in br_stock_passcount, mirroring _write_buy_reversal_v3_qualified exactly.
_BR_V3_PASSCOUNT_GATES = [
    ("daily_rsi", None, 40.0),   # (a) cold short-term dip
    ("dma_200",   0.0,  None),   # (c) long-term uptrend
    ("gvm_score", 6.5,  None),   # (d) quality
    ("mom_2d",    0.0,  3.0),     # (e) 2-day momentum turned up
]

def br_stock_passcount():
    """cc#364: buy_reversal V3 pass-count = n/8 (V3 parity, spec id=2818), cheap-first.
    The 4 daily gates + 3 cheap live checks (hourly_pct, CMP>PP, room-to-R1) are evaluated for ALL
    stocks; true_weekly_rsi (stage 8, DB-heavy) is computed ONLY for stocks that clear the first 7 —
    every other stock caps at <=7/8 with true_weekly_rsi in the failed/skipped list. Off-market or
    missing pivot/CMP: the live checks NULL-pass gracefully (same exemption as the writer's hourly
    09:15-09:20 rule). Display only — mirrors _write_buy_reversal_v3_qualified, never qualifies."""
    from v8_signal_writer import _load_hourly_fut_v3, _true_weekly_rsi
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
            pivots   = _basket_pivots(cur)
            cmp_map  = _basket_cmp(cur)
            hourly_v3 = _load_hourly_fut_v3(conn, [r["symbol"] for r in all_rows])
            out = []
            for s in all_rows:
                sym = s["symbol"]
                passed, failed = [], []
                # 4 cheap daily gates
                for metric, mn, mx in _BR_V3_PASSCOUNT_GATES:
                    (passed if _passes_filter(s.get(metric), mn, mx) else failed).append(metric)
                # 3 cheap live checks — NULL-pass when the live datum is unavailable (off-market / no pivot)
                cmp = cmp_map.get(sym)
                pv  = pivots.get(sym)
                pp  = pv.get("pp") if pv else None
                r1  = pv.get("r1") if pv else None
                hourly, n_bars = hourly_v3.get(sym, (None, 0))
                hourly_ok = (n_bars == 0) or (hourly is not None and 0.1 <= hourly <= 1.0)
                cmp_ok    = (cmp is None or pp is None) or (cmp > pp)
                room_pct  = ((r1 - cmp) / cmp * 100.0) if (cmp and r1) else None
                room_ok   = (room_pct is None) or (room_pct > 2.0)
                (passed if hourly_ok else failed).append("hourly_pct")
                (passed if cmp_ok   else failed).append("cmp_gt_pp")
                (passed if room_ok  else failed).append("room_r1")
                # stage 8 true_weekly_rsi — heavy read, ONLY for stocks that cleared all 7 cheap gates
                if len(passed) == 7:
                    twr = _true_weekly_rsi(conn, sym, cmp)
                    (passed if (twr is not None and twr >= 60.0) else failed).append("true_weekly_rsi")
                else:
                    failed.append("true_weekly_rsi")   # skipped — not evaluated, caps at <=7/8
                out.append({"symbol": sym, "passed": len(passed), "total": 8,
                            "passed_filters": passed, "failed_filters": failed,
                            "gvm_score": s.get("gvm_score"), "mom_2d": s.get("mom_2d"),
                            "v21_pass": None})
        out.sort(key=lambda x: (x["passed"], x["gvm_score"] if x["gvm_score"] is not None else -1), reverse=True)
        return {"basket": "buy_reversal", "score_date": str(date.today()),
                "universe": len(out), "filter_count": 8, "stocks": out,
                "v21_enabled": False, **BASKET_META.get("buy_reversal", {})}
    except Exception as e:
        raise HTTPException(500, f"br_stock_passcount failed: {e}")


@router.get("/br_stock_detail/{symbol}")
def br_stock_detail(symbol: str):
    """cc#366: per-stock 8-filter breakdown for buy_reversal V3 (spec id=2818) — ACTUAL value vs
    REQUIRED bound + PASS/FAIL for each of the 8 gates, computed LIVE for ONE symbol. Powers the
    pass-count click-through modal so engine decisions can be verified by hand. Pass logic mirrors
    br_stock_passcount / _write_buy_reversal_v3_qualified EXACTLY (incl. the stage-8 rule that
    true_weekly_rsi only counts once all 7 cheap gates pass), so the green-row count equals the
    n/8 on the box. true_weekly_rsi is always computed here (one stock, cheap) so the row is never
    blank — even when the engine would have skipped it. Display only, never qualifies."""
    from v8_signal_writer import _load_hourly_fut_v3, _true_weekly_rsi
    sym = symbol.upper()
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT gvm_score, dma_200, mom_2d, daily_rsi FROM v8_metrics
                WHERE symbol=%s AND score_date=(SELECT MAX(score_date) FROM v8_metrics)""", (sym,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"No v8_metrics for {sym}")
            gvm, dma200, mom2d, drsi = [float(x) if x is not None else None for x in row]
            cur.execute("""SELECT pp, r1 FROM v8_paper_pivots WHERE symbol=%s
                AND pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)""", (sym,))
            pv = cur.fetchone()
            pp = float(pv[0]) if pv and pv[0] is not None else None
            r1 = float(pv[1]) if pv and pv[1] is not None else None
            cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s AND cmp IS NOT NULL", (sym,))
            cr = cur.fetchone()
            cmp = float(cr[0]) if cr else None
            hourly, n_bars = _load_hourly_fut_v3(conn, [sym]).get(sym, (None, 0))
            twr = _true_weekly_rsi(conn, sym, cmp)   # one stock — always compute so the row is never blank

        def _fmt(v, d):
            return "--" if v is None else f"{v:.{d}f}"
        room = ((r1 - cmp) / cmp * 100.0) if (cmp and r1) else None

        # pass booleans — identical to br_stock_passcount (NULL-pass when the live datum is absent)
        p_drsi = _passes_filter(drsi, None, 40.0)
        p_dma  = _passes_filter(dma200, 0.0, None)
        p_gvm  = _passes_filter(gvm, 6.5, None)
        p_mom  = _passes_filter(mom2d, 0.0, 3.0)
        p_hr   = (n_bars == 0) or (hourly is not None and 0.1 <= hourly <= 1.0)
        p_cmp  = (cmp is None or pp is None) or (cmp > pp)
        p_room = (room is None) or (room > 2.0)
        cleared = all([p_drsi, p_dma, p_gvm, p_mom, p_hr, p_cmp, p_room])   # all 7 cheap gates
        p_twr  = cleared and (twr is not None and twr >= 60.0)             # stage-8 engine rule

        hr_actual = "no fut bars yet (exempt)" if n_bars == 0 else _fmt(hourly, 2)
        rows = [
            {"filter": "daily_rsi",       "required": "<= 40",      "actual": _fmt(drsi, 1),          "pass": p_drsi},
            {"filter": "dma_200",         "required": ">= 0",       "actual": _fmt(dma200, 2) + "%",  "pass": p_dma},
            {"filter": "gvm_score",       "required": ">= 6.5",     "actual": _fmt(gvm, 1),           "pass": p_gvm},
            {"filter": "mom_2d",          "required": "0 to 3",     "actual": _fmt(mom2d, 2) + "%",   "pass": p_mom},
            {"filter": "hourly_pct",      "required": "0.1 to 1.0", "actual": hr_actual,              "pass": p_hr},
            {"filter": "cmp_gt_pp",       "required": "CMP > PP",   "actual": f"{_fmt(cmp, 2)} vs {_fmt(pp, 2)}", "pass": p_cmp},
            {"filter": "room_r1",         "required": "> 2%",       "actual": _fmt(room, 2) + "%",    "pass": p_room},
            {"filter": "true_weekly_rsi", "required": ">= 60",      "actual": _fmt(twr, 1),           "pass": p_twr},
        ]
        if not cleared:
            rows[7]["note"] = "engine evaluates true weekly RSI only after all 7 cheap gates pass"
        passed = sum(1 for r in rows if r["pass"])
        return {"symbol": sym, "cmp": cmp, "pp": pp, "r1": r1,
                "room_r1_pct": round(room, 2) if room is not None else None,
                "passed": passed, "total": 8, "rows": rows,
                "spec": "BUY_REVERSAL_V3 id=2818"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"br_stock_detail failed: {e}")


# cc#378: sell_reversal V5-D funnel stages (spec id=2894) — SELL mirror of _BR_V3_STAGES. Cheap-first,
# true_weekly_rsi last (heavy, only on cheap-intersection survivors). Labels carry spaces for verbatim render.
_SR_V5D_STAGES = [
    ("daily_rsi",       "daily RSI",       ">= 60", ""),
    ("rsi_month",       "monthly RSI",     "",      "< 50"),
    ("dma_200",         "dma 200",         "",      "<= 0"),
    ("cmp_lt_pp",       "CMP < PP",        "",      ""),
    ("room",            "room to S1/S2",   ">= 2%", ""),
    ("true_weekly_rsi", "true weekly RSI", "",      "<= 40"),
]

def sr_funnel_detail():
    """cc#378: 6-stage funnel for sell_reversal V5-D, reshaped from the handler-written
    v8_funnel_counts row. INDEPENDENT per-filter pass counts across the universe (buy_momentum
    convention); true_weekly_rsi (stage 6) is passes vs the stocks clearing all 5 cheap gates.
    Final = strict-AND of all 6. Empty until the first live tick writes."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT counts FROM v8_funnel_counts WHERE basket='sell_reversal' "
                        "AND score_date=CURRENT_DATE ORDER BY computed_at DESC LIMIT 1")
            row = cur.fetchone()
        counts   = (row[0] if row and isinstance(row[0], dict) else {}) or {}
        universe = int(counts.get("_universe", 0) or 0)
        stage5   = counts.get("_stage5_survivors")
        stage5   = int(stage5) if stage5 is not None else None
        final    = int(counts.get("_score_qualified", 0) or 0)
        stages = []
        for key, label, cmin, cmax in _SR_V5D_STAGES:
            passes = int(counts.get(key, 0) or 0)
            denom = (stage5 if stage5 is not None else universe) if key == "true_weekly_rsi" else universe
            fails = max(denom - passes, 0)
            stage = {"metric": label, "condition_min": cmin, "condition_max": cmax,
                     "passes": passes, "fails": fails, "survivors": passes, "killed": fails,
                     "pass_pct": round(passes / denom * 100, 1) if denom else 0}
            if key == "true_weekly_rsi":
                stage["denominator"] = denom
                stage["note"] = f"of {denom} stocks passing all 5 cheap gates"
            stages.append(stage)
        return {
            "basket": "sell_reversal", "score_date": str(date.today()),
            "universe": universe, "final": final, "filter_count": 6, "n_filters": 6,
            "stage5_survivors": stage5,
            "gate_type": "independent per-filter counts; final = strict AND of all 6",
            "score_qualified": final, "pivot_pass": final,
            "stages": stages, **BASKET_META.get("sell_reversal", {}),
        }
    except Exception as e:
        raise HTTPException(500, f"sr_funnel_detail failed: {e}")


# cc#378: sell_reversal V5-D pass-count cheap daily-metric gates (>=60 / <=0). rsi_month<50 (strict),
# CMP<PP, room-to-S1/S2, true_weekly_rsi are handled inline in sr_stock_passcount.
_SR_V5D_PASSCOUNT_GATES = [
    ("daily_rsi", 60.0, None),   # (a) hot bounce
    ("dma_200",   None, 0.0),    # (d) below the 200-DMA
]

def sr_stock_passcount():
    """cc#378: sell_reversal V5-D pass-count = n/6 (spec id=2894), cheap-first (mirror cc#364).
    5 cheap gates for ALL stocks; true_weekly_rsi (stage 6, heavy) only for stocks clearing the first 5.
    Off-market / missing pivot|CMP: the CMP<PP + room checks NULL-pass. Display only — mirrors the
    handler, never qualifies."""
    from v8_signal_writer import _true_weekly_rsi
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
            pivots   = _basket_pivots(cur)
            cmp_map  = _basket_cmp(cur)
            out = []
            for s in all_rows:
                sym = s["symbol"]
                passed, failed = [], []
                for metric, mn, mx in _SR_V5D_PASSCOUNT_GATES:
                    (passed if _passes_filter(s.get(metric), mn, mx) else failed).append(metric)
                rm = s.get("rsi_month")
                (passed if (rm is not None and float(rm) < 50.0) else failed).append("rsi_month")
                cmp = cmp_map.get(sym)
                pv  = pivots.get(sym)
                pp  = pv.get("pp") if pv else None
                s1  = pv.get("s1") if pv else None
                s2  = pv.get("s2") if pv else None
                cmp_ok = (cmp is None or pp is None) or (cmp < pp)
                tgt, _ = _sr_dynamic_target(cmp, s1, s2)
                room_ok = (cmp is None or s1 is None) or (tgt is not None)
                (passed if cmp_ok  else failed).append("cmp_lt_pp")
                (passed if room_ok else failed).append("room")
                if len(passed) == 5:
                    twr = _true_weekly_rsi(conn, sym, cmp)
                    (passed if (twr is not None and twr <= 40.0) else failed).append("true_weekly_rsi")
                else:
                    failed.append("true_weekly_rsi")
                out.append({"symbol": sym, "passed": len(passed), "total": 6,
                            "passed_filters": passed, "failed_filters": failed,
                            "gvm_score": s.get("gvm_score"), "mom_2d": s.get("mom_2d"),
                            "v21_pass": None})
        out.sort(key=lambda x: (x["passed"], x["gvm_score"] if x["gvm_score"] is not None else -1), reverse=True)
        return {"basket": "sell_reversal", "score_date": str(date.today()),
                "universe": len(out), "filter_count": 6, "stocks": out,
                "v21_enabled": False, **BASKET_META.get("sell_reversal", {})}
    except Exception as e:
        raise HTTPException(500, f"sr_stock_passcount failed: {e}")


@router.get("/sr_stock_detail/{symbol}")
def sr_stock_detail(symbol: str):
    """cc#378: per-stock 6-filter breakdown for sell_reversal V5-D (spec id=2894) — ACTUAL vs REQUIRED
    + PASS/FAIL. Mirrors sr_stock_passcount / _write_sell_reversal_v5d_qualified so the green-row count
    equals n/6. true_weekly_rsi always computed here (one stock) so the row is never blank."""
    from v8_signal_writer import _true_weekly_rsi
    sym = symbol.upper()
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT dma_200, rsi_month, daily_rsi FROM v8_metrics
                WHERE symbol=%s AND score_date=(SELECT MAX(score_date) FROM v8_metrics)""", (sym,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"No v8_metrics for {sym}")
            dma200, rmon, drsi = [float(x) if x is not None else None for x in row]
            cur.execute("""SELECT pp, s1, s2 FROM v8_paper_pivots WHERE symbol=%s
                AND pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)""", (sym,))
            pv = cur.fetchone()
            pp = float(pv[0]) if pv and pv[0] is not None else None
            s1 = float(pv[1]) if pv and pv[1] is not None else None
            s2 = float(pv[2]) if pv and pv[2] is not None else None
            cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s AND cmp IS NOT NULL", (sym,))
            cr = cur.fetchone()
            cmp = float(cr[0]) if cr else None
            twr = _true_weekly_rsi(conn, sym, cmp)

        def _fmt(v, d):
            return "--" if v is None else f"{v:.{d}f}"
        tgt, room = _sr_dynamic_target(cmp, s1, s2)

        p_drsi = _passes_filter(drsi, 60.0, None)
        p_rm   = rmon is not None and rmon < 50.0
        p_dma  = _passes_filter(dma200, None, 0.0)
        p_cmp  = (cmp is None or pp is None) or (cmp < pp)
        p_room = (cmp is None or s1 is None) or (tgt is not None)
        cleared = all([p_drsi, p_rm, p_dma, p_cmp, p_room])   # 5 cheap gates
        p_twr  = cleared and (twr is not None and twr <= 40.0)

        tgt_lbl = "S1" if (tgt is not None and s1 is not None and abs(tgt - s1) < 1e-6) else ("S2" if tgt is not None else "--")
        rows = [
            {"filter": "daily_rsi",       "required": ">= 60",  "actual": _fmt(drsi, 1),        "pass": p_drsi},
            {"filter": "rsi_month",       "required": "< 50",   "actual": _fmt(rmon, 1),        "pass": p_rm},
            {"filter": "dma_200",         "required": "<= 0",   "actual": _fmt(dma200, 2) + "%","pass": p_dma},
            {"filter": "cmp_lt_pp",       "required": "CMP < PP","actual": f"{_fmt(cmp, 2)} vs {_fmt(pp, 2)}", "pass": p_cmp},
            {"filter": "room",            "required": ">= 2%",  "actual": (f"{_fmt(room, 2)}% -> {tgt_lbl}" if room is not None else "--"), "pass": p_room},
            {"filter": "true_weekly_rsi", "required": "<= 40",  "actual": _fmt(twr, 1),         "pass": p_twr},
        ]
        if not cleared:
            rows[5]["note"] = "engine evaluates true weekly RSI only after all 5 cheap gates pass"
        passed = sum(1 for r in rows if r["pass"])
        return {"symbol": sym, "cmp": cmp, "pp": pp, "s1": s1, "s2": s2,
                "target": tgt, "room_pct": room, "passed": passed, "total": 6, "rows": rows,
                "spec": "SELL_REVERSAL_V5D id=2894"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"sr_stock_detail failed: {e}")


# cc#380: sell_momentum V3-N5 funnel stages (spec id=2901). Cheap-first, true_weekly_rsi last (heavy,
# only on the 8-cheap-gate intersection). Labels carry spaces for verbatim render.
_SM_V3_STAGES = [
    ("rsi_month",       "monthly RSI",     "",       "< 40"),
    ("mom_2d",          "mom 2d",          ">= -4",  "<= -1"),
    ("dma_200",         "dma 200",         "",       "<= 2"),
    ("week_return",     "week return",     ">= -10", "<= -0.5"),
    ("sector_week",     "sector week",     "",       "< 0"),
    ("week_index_52",   "52w index",       ">= 20",  "<= 60"),
    ("cmp_lt_pp",       "CMP < PP",        "",       ""),
    ("s2_clearance",    "S2 clearance",    ">= 3%",  ""),
    ("true_weekly_rsi", "true weekly RSI", "",       "<= 45"),
]

def sm_funnel_detail():
    """cc#380: 9-stage funnel for sell_momentum V3-N5, reshaped from the handler-written
    v8_funnel_counts row. INDEPENDENT per-filter pass counts across the universe (buy_momentum
    convention); true_weekly_rsi (stage 9) is passes vs the stocks clearing all 8 cheap gates.
    Final = strict-AND of all 9. Empty until the first live tick writes."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT counts FROM v8_funnel_counts WHERE basket='sell_momentum' "
                        "AND score_date=CURRENT_DATE ORDER BY computed_at DESC LIMIT 1")
            row = cur.fetchone()
        counts   = (row[0] if row and isinstance(row[0], dict) else {}) or {}
        universe = int(counts.get("_universe", 0) or 0)
        stage8   = counts.get("_stage8_survivors")
        stage8   = int(stage8) if stage8 is not None else None
        final    = int(counts.get("_score_qualified", 0) or 0)
        stages = []
        for key, label, cmin, cmax in _SM_V3_STAGES:
            passes = int(counts.get(key, 0) or 0)
            denom = (stage8 if stage8 is not None else universe) if key == "true_weekly_rsi" else universe
            fails = max(denom - passes, 0)
            stage = {"metric": label, "condition_min": cmin, "condition_max": cmax,
                     "passes": passes, "fails": fails, "survivors": passes, "killed": fails,
                     "pass_pct": round(passes / denom * 100, 1) if denom else 0}
            if key == "true_weekly_rsi":
                stage["denominator"] = denom
                stage["note"] = f"of {denom} stocks passing all 8 cheap gates"
            stages.append(stage)
        return {
            "basket": "sell_momentum", "score_date": str(date.today()),
            "universe": universe, "final": final, "filter_count": 9, "n_filters": 9,
            "stage8_survivors": stage8,
            "gate_type": "independent per-filter counts; final = strict AND of all 9",
            "score_qualified": final, "pivot_pass": final,
            "stages": stages, **BASKET_META.get("sell_momentum", {}),
        }
    except Exception as e:
        raise HTTPException(500, f"sm_funnel_detail failed: {e}")


# cc#380: sell_momentum V3-N5 pass-count cheap v8_metrics gates via _passes_filter (inclusive).
# rsi_month<40 + sector_week<0 (strict), CMP<PP, S2-clearance, true_weekly_rsi handled inline.
_SM_V3_PASSCOUNT_GATES = [
    ("mom_2d",        -4.0,  -1.0),
    ("dma_200",       None,   2.0),
    ("week_return",   -10.0, -0.5),
    ("week_index_52",  20.0,  60.0),
]

def sm_stock_passcount():
    """cc#380: sell_momentum V3-N5 pass-count = n/9 (spec id=2901), cheap-first (mirror cc#364).
    8 cheap gates for ALL stocks; true_weekly_rsi (stage 9, heavy) only for stocks clearing the first 8.
    Off-market / missing pivot|CMP: the CMP<PP + S2-clearance checks NULL-pass. Display only."""
    from v8_signal_writer import _true_weekly_rsi
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
            pivots   = _basket_pivots(cur)
            cmp_map  = _basket_cmp(cur)
            out = []
            for s in all_rows:
                sym = s["symbol"]
                passed, failed = [], []
                for metric, mn, mx in _SM_V3_PASSCOUNT_GATES:
                    (passed if _passes_filter(s.get(metric), mn, mx) else failed).append(metric)
                rm = s.get("rsi_month")
                (passed if (rm is not None and float(rm) < 40.0) else failed).append("rsi_month")
                sw = s.get("sector_week")
                (passed if (sw is not None and float(sw) < 0.0) else failed).append("sector_week")
                cmp = cmp_map.get(sym)
                pv  = pivots.get(sym)
                pp  = pv.get("pp") if pv else None
                s2  = pv.get("s2") if pv else None
                cmp_ok = (cmp is None or pp is None) or (cmp < pp)
                s2c = ((cmp - s2) / cmp * 100.0) if (cmp and s2 is not None) else None
                s2c_ok = (cmp is None or s2 is None) or (s2c is not None and s2c >= 3.0)
                (passed if cmp_ok else failed).append("cmp_lt_pp")
                (passed if s2c_ok else failed).append("s2_clearance")
                if len(passed) == 8:
                    twr = _true_weekly_rsi(conn, sym, cmp)
                    (passed if (twr is not None and twr <= 45.0) else failed).append("true_weekly_rsi")
                else:
                    failed.append("true_weekly_rsi")
                out.append({"symbol": sym, "passed": len(passed), "total": 9,
                            "passed_filters": passed, "failed_filters": failed,
                            "gvm_score": s.get("gvm_score"), "mom_2d": s.get("mom_2d"),
                            "v21_pass": None})
        out.sort(key=lambda x: (x["passed"], x["gvm_score"] if x["gvm_score"] is not None else -1), reverse=True)
        return {"basket": "sell_momentum", "score_date": str(date.today()),
                "universe": len(out), "filter_count": 9, "stocks": out,
                "v21_enabled": False, **BASKET_META.get("sell_momentum", {})}
    except Exception as e:
        raise HTTPException(500, f"sm_stock_passcount failed: {e}")


@router.get("/sm_stock_detail/{symbol}")
def sm_stock_detail(symbol: str):
    """cc#380: per-stock 9-filter breakdown for sell_momentum V3-N5 (spec id=2901). Mirrors
    sm_stock_passcount / the handler so the green-row count equals n/9. true_weekly_rsi always
    computed here so the row is never blank."""
    from v8_signal_writer import _true_weekly_rsi
    sym = symbol.upper()
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT rsi_month, mom_2d, dma_200, week_return, sector_week, week_index_52
                FROM v8_metrics WHERE symbol=%s AND score_date=(SELECT MAX(score_date) FROM v8_metrics)""", (sym,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"No v8_metrics for {sym}")
            rmon, mom2d, dma200, wret, swk, w52 = [float(x) if x is not None else None for x in row]
            cur.execute("""SELECT pp, s2 FROM v8_paper_pivots WHERE symbol=%s
                AND pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)""", (sym,))
            pv = cur.fetchone()
            pp = float(pv[0]) if pv and pv[0] is not None else None
            s2 = float(pv[1]) if pv and pv[1] is not None else None
            cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s AND cmp IS NOT NULL", (sym,))
            cr = cur.fetchone()
            cmp = float(cr[0]) if cr else None
            twr = _true_weekly_rsi(conn, sym, cmp)

        def _fmt(v, d):
            return "--" if v is None else f"{v:.{d}f}"
        s2c = ((cmp - s2) / cmp * 100.0) if (cmp and s2 is not None) else None

        p_rm   = rmon is not None and rmon < 40.0
        p_mom  = _passes_filter(mom2d, -4.0, -1.0)
        p_dma  = _passes_filter(dma200, None, 2.0)
        p_wret = _passes_filter(wret, -10.0, -0.5)
        p_sw   = swk is not None and swk < 0.0
        p_w52  = _passes_filter(w52, 20.0, 60.0)
        p_cmp  = (cmp is None or pp is None) or (cmp < pp)
        p_s2c  = (cmp is None or s2 is None) or (s2c is not None and s2c >= 3.0)
        cleared = all([p_rm, p_mom, p_dma, p_wret, p_sw, p_w52, p_cmp, p_s2c])   # 8 cheap gates
        p_twr  = cleared and (twr is not None and twr <= 45.0)

        rows = [
            {"filter": "rsi_month",       "required": "< 40",     "actual": _fmt(rmon, 1),        "pass": p_rm},
            {"filter": "mom_2d",          "required": "-4 to -1", "actual": _fmt(mom2d, 2) + "%", "pass": p_mom},
            {"filter": "dma_200",         "required": "<= 2",     "actual": _fmt(dma200, 2) + "%","pass": p_dma},
            {"filter": "week_return",     "required": "-10 to -0.5","actual": _fmt(wret, 2) + "%","pass": p_wret},
            {"filter": "sector_week",     "required": "< 0",      "actual": _fmt(swk, 2),         "pass": p_sw},
            {"filter": "week_index_52",   "required": "20 to 60", "actual": _fmt(w52, 1),         "pass": p_w52},
            {"filter": "cmp_lt_pp",       "required": "CMP < PP", "actual": f"{_fmt(cmp, 2)} vs {_fmt(pp, 2)}", "pass": p_cmp},
            {"filter": "s2_clearance",    "required": ">= 3%",    "actual": _fmt(s2c, 2) + "%",   "pass": p_s2c},
            {"filter": "true_weekly_rsi", "required": "<= 45",    "actual": _fmt(twr, 1),         "pass": p_twr},
        ]
        if not cleared:
            rows[8]["note"] = "engine evaluates true weekly RSI only after all 8 cheap gates pass"
        passed = sum(1 for r in rows if r["pass"])
        return {"symbol": sym, "cmp": cmp, "pp": pp, "s2": s2,
                "s2_clearance_pct": round(s2c, 2) if s2c is not None else None,
                "passed": passed, "total": 9, "rows": rows,
                "spec": "SELL_MOMENTUM_V3_N5 id=2901"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"sm_stock_detail failed: {e}")


@router.get("/funnel_detail/{basket}")
def funnel_detail(basket: str):
    basket = basket.lower()
    if basket == "buy_s1_bounce":   return s1b_funnel_detail()
    if basket == "sell_overbought": return so_funnel_detail()
    if basket == "buy_reversal":    return br_funnel_detail()
    if basket == "sell_reversal":   return sr_funnel_detail()   # cc#378 V5-D
    if basket == "sell_momentum":   return sm_funnel_detail()   # cc#380 V3-N5
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
            # cc#164: V2.1 hard-gate visibility -- read-only, computed against this
            # SAME loaded universe. Never touches _write_qualified / the signal engine.
            v21_enabled = _load_filter_state(conn).get(basket, False)
            v21_metrics = _load_v21_live_metrics(conn, [r["symbol"] for r in all_rows])
        total   = len(all_rows)
        filters = _get_buy_reversal_live_filters()[0] if basket == "buy_reversal" else FILTER_CONFIG[basket]
        n       = len(filters)
        side    = "BUY" if basket.startswith("buy") else "SELL"
        stages = []
        for metric, bounds in filters.items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            passes = sum(1 for s in all_rows if _passes_filter(s.get(metric), mn, mx))
            stage = {"metric": metric, "min": mn, "max": mx,
                           "passes": passes, "fails": total - passes,
                           "survivors": passes, "killed": total - passes,
                           "pass_pct": round(passes / total * 100, 1) if total else 0,
                           "dynamic": basket == "buy_reversal" and metric in ("week_return", "rsi_month", "sector_week")}
            # cc#164: sell_momentum's V2.1 change is a score-gate MODIFY of this exact
            # stage (week_index_52 <=20 -> <=30 when enabled), not a hard-gate add --
            # shown here on the existing stage rather than as a separate one.
            if basket == "sell_momentum" and metric == "week_index_52":
                stage["v21_enabled"] = v21_enabled
                if v21_enabled:
                    eff_mx = V21_FILTERS["sell_momentum"]["week_index_52_modify"]["max"]
                    eff_passes = sum(1 for s in all_rows if _passes_filter(s.get(metric), mn, eff_mx))
                    stage["v21_modified_max"] = eff_mx
                    stage["v21_passes"] = eff_passes
                    stage["v21_note"] = f"V2.1 enabled: threshold relaxed {mx} -> {eff_mx}"
                else:
                    stage["v21_note"] = "V2.1 disabled (locked threshold in effect)"
            stages.append(stage)
        # cc#164: V2.1 hard-gate stage -- buy_reversal / buy_momentum / sell_reversal
        # only (sell_momentum's V2.1 is the MODIFY handled above, not a hard-gate add).
        # v21_hard_gate_pass() itself returns True unconditionally when disabled, so
        # survivors==total automatically when off -- same fail-open behavior as the
        # live signal engine.
        if basket in V21_FILTERS and basket != "sell_momentum":
            v21_pass = sum(1 for s in all_rows
                           if v21_hard_gate_pass(basket, {**s, **v21_metrics.get(s["symbol"], {})}, v21_enabled))
            band = V21_FILTERS[basket]
            cond_desc = ", ".join(f"{k}[{c.get('min', '-')}..{c.get('max', '-')}]" for k, c in band.items())
            stages.append({
                "metric": "v2.1_hard_gate", "min": None, "max": None,
                "condition_min": cond_desc,
                "condition_max": "enabled" if v21_enabled else "disabled",
                "passes": v21_pass, "fails": total - v21_pass,
                "survivors": v21_pass, "killed": total - v21_pass,
                "pass_pct": round(v21_pass / total * 100, 1) if total else 0,
                "v21_enabled": v21_enabled,
                "v21_note": "V2.1 hard gate (hourly_pct + week_index_52)" if v21_enabled
                            else "V2.1 hard gate DISABLED (locked behavior in effect)",
            })
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
            if "condition_min" in st:
                continue
            mn, mx = st.get("min"), st.get("max")
            st["condition_min"] = f">= {mn}" if mn is not None else "-"
            st["condition_max"] = f"<= {mx}" if mx is not None else "-"
        return {"basket": basket, "score_date": str(date.today()),
                "universe": total, "n_filters": n, "filter_count": n, "final": pivot_pass,
                "score_threshold": score_threshold, "score_qualified": score_qualified,
                "pivot_pass": pivot_pass, "stages": stages, "v21_enabled": v21_enabled,
                **BASKET_META.get(basket, {})}
    except Exception as e:
        raise HTTPException(500, f"funnel_detail failed: {e}")


@router.get("/stock_passcount/{basket}")
def stock_passcount(basket: str):
    basket = basket.lower()
    if basket == "buy_s1_bounce":   return s1b_stock_passcount()
    if basket == "sell_overbought": return so_stock_passcount()
    if basket == "buy_reversal":    return br_stock_passcount()
    if basket == "sell_reversal":   return sr_stock_passcount()   # cc#378 V5-D
    if basket == "sell_momentum":   return sm_stock_passcount()   # cc#380 V3-N5
    if basket not in FILTER_CONFIG: raise HTTPException(404, f"Unknown basket: {basket}")
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
            v21_enabled = _load_filter_state(conn).get(basket, False)
            v21_metrics = _load_v21_live_metrics(conn, [r["symbol"] for r in all_rows])
        filters = _get_buy_reversal_live_filters()[0] if basket == "buy_reversal" else FILTER_CONFIG[basket]
        n_filters = len(filters); out = []
        for s in all_rows:
            passed_list, failed_list = [], []
            for metric, bounds in filters.items():
                mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
                if _passes_filter(s.get(metric), mn, mx): passed_list.append(metric)
                else: failed_list.append(metric)
            # cc#164: V2.1 hard-gate pass/fail per stock, display only.
            v21_pass = (v21_hard_gate_pass(basket, {**s, **v21_metrics.get(s["symbol"], {})}, v21_enabled)
                        if basket in V21_FILTERS else None)
            out.append({"symbol": s["symbol"], "passed": len(passed_list), "total": n_filters,
                        "passed_filters": passed_list, "failed_filters": failed_list,
                        "gvm_score": s.get("gvm_score"), "mom_2d": s.get("mom_2d"),
                        "v21_pass": v21_pass})
        out.sort(key=lambda x: (x["passed"], x["gvm_score"] if x["gvm_score"] is not None else -1), reverse=True)
        return {"basket": basket, "score_date": str(date.today()),
                "universe": len(out), "filter_count": n_filters, "stocks": out,
                "v21_enabled": v21_enabled,
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


# ── Sell Overbought dedicated funnel ─────────────────────────────────────────
_SO_COMMON_SQL = """
    WITH pivots AS (
        SELECT symbol, price_date,
            AVG((high+low+close)/3.0) OVER w AS pp,
            MAX(high) OVER w AS h5, MIN(low) OVER w AS l5
        FROM raw_prices WHERE price_date >= CURRENT_DATE - INTERVAL '14 days'
        WINDOW w AS (PARTITION BY symbol ORDER BY price_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)
    ),
    latest_pivot AS (
        SELECT symbol, 2*pp-h5 AS s1, 2*pp-l5 AS r1, pp+(h5-l5) AS r2
        FROM pivots WHERE price_date=(SELECT MAX(price_date) FROM pivots p2 WHERE p2.symbol=pivots.symbol)
    ),
    hi3d AS (SELECT symbol, MAX(high) AS max_high_3d FROM raw_prices
        WHERE price_date>=CURRENT_DATE-INTERVAL '4 days' AND price_date<=CURRENT_DATE GROUP BY symbol),
    hi5d AS (SELECT symbol, MAX(high) AS max_high_5d FROM raw_prices
        WHERE price_date>=CURRENT_DATE-INTERVAL '7 days' AND price_date<=CURRENT_DATE GROUP BY symbol),
    latest_close AS (
        SELECT DISTINCT ON (symbol) symbol, close
        FROM raw_prices WHERE price_date<=CURRENT_DATE ORDER BY symbol, price_date DESC
    )
    SELECT m.symbol, m.rsi_weekly, m.rsi_month, m.sector_week, m.gvm_score,
           lp.r1, lp.r2, h5d.max_high_5d, h3d.max_high_3d, lc.close
    FROM v8_metrics m
    JOIN futures_universe f ON f.symbol=m.symbol AND f.is_active=TRUE
    LEFT JOIN latest_pivot lp ON lp.symbol=m.symbol
    LEFT JOIN hi3d h3d ON h3d.symbol=m.symbol
    LEFT JOIN hi5d h5d ON h5d.symbol=m.symbol
    LEFT JOIN latest_close lc ON lc.symbol=m.symbol
    WHERE m.score_date=(SELECT MAX(score_date) FROM v8_metrics)
"""


def _so_enrich(rows):
    def _f(v):
        try: return float(v) if v is not None else None
        except (TypeError, ValueError): return None
    for r in rows:
        r1 = _f(r.get("r1")); r2 = _f(r.get("r2"))
        h5d = _f(r.get("max_high_5d")); h3d = _f(r.get("max_high_3d"))
        close = _f(r.get("close"))
        r["week_high_vs_pivot"] = bool(
            h5d is not None and (
                (r1 is not None and h5d > 0.9 * r1) or
                (r2 is not None and h5d > 0.9 * r2)
            )
        )
        r["fall_3d"] = ((close - h3d) / h3d * 100) if (close and h3d and h3d > 0) else None
    return rows


# cc#382: sell_overbought V3 funnel stages (spec id=2912) — reshaped from the handler-written
# v8_funnel_counts row. All 5 cheap/live (no heavy stage). (The V2 _so_funnel_stages that live-computed
# wRSI>=80/mRSI>=70 was removed — those V2 thresholds never fired and are gone from the SO path.)
_SO_V3_STAGES = [
    ("nifty_rsi",   "NIFTY daily RSI",   "",       "<= 45"),
    ("high_ge_r1",  "day high >= R1",    "",       ""),
    ("fall_2d",     "fall from 2d high", ">= 1%",  "<= 10%"),
    ("day_red",     "day change < 0",    "",       ""),
    ("week_return", "week return",       ">= -2.5","<= 0"),
]


def so_funnel_detail():
    """cc#382: 5-stage funnel for sell_overbought V3, reshaped from the handler-written
    v8_funnel_counts row (independent per-filter counts; final = strict AND of all 5)."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT counts FROM v8_funnel_counts WHERE basket='sell_overbought' "
                        "AND score_date=CURRENT_DATE ORDER BY computed_at DESC LIMIT 1")
            row = cur.fetchone()
        counts = (row[0] if row and isinstance(row[0], dict) else {}) or {}
        universe = int(counts.get("_universe", 0) or 0)
        final = int(counts.get("_score_qualified", 0) or 0)
        stages = []
        for key, label, cmin, cmax in _SO_V3_STAGES:
            passes = int(counts.get(key, 0) or 0)
            fails = max(universe - passes, 0)
            stages.append({"metric": label, "condition_min": cmin, "condition_max": cmax,
                           "passes": passes, "fails": fails, "survivors": passes, "killed": fails,
                           "pass_pct": round(passes / universe * 100, 1) if universe else 0})
        return {
            "basket": "sell_overbought", "score_date": str(date.today()),
            "universe": universe, "final": final, "filter_count": 5, "n_filters": 5,
            "gate_type": "independent per-filter counts; final = strict AND of all 5",
            "score_qualified": final, "pivot_pass": final, "stages": stages,
            **BASKET_META.get("sell_overbought", {})
        }
    except Exception as e:
        raise HTTPException(500, f"so_funnel_detail failed: {e}")


def so_funnel_counts():
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT counts FROM v8_funnel_counts WHERE basket='sell_overbought' "
                        "AND score_date=CURRENT_DATE ORDER BY computed_at DESC LIMIT 1")
            row = cur.fetchone()
        counts = (row[0] if row and isinstance(row[0], dict) else {}) or {}
        return {"basket": "sell_overbought", "score_date": str(date.today()),
                "counts": counts, "source": "handler_v3"}
    except Exception as e:
        raise HTTPException(500, f"so_funnel_counts failed: {e}")


_SO_V3_PASSCOUNT_GATES = [("week_return", -2.5, 0.0)]   # only v8_metrics gate; rest are live/pivot

def so_stock_passcount():
    """cc#382: sell_overbought V3 pass-count = n/5 (spec id=2912). NIFTY-RSI market gate + live session
    HIGH>=R1 + fall-from-2d-high 1-10% + day change<0 + week_return -2.5..0. Display only."""
    from v8_signal_writer import _get_nifty_rsi
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
            pivots   = _basket_pivots(cur)
            cmp_map  = _basket_cmp(cur)
            nifty_rsi = _get_nifty_rsi(conn)
            nifty_ok  = nifty_rsi is not None and nifty_rsi <= 45.0
            syms = [r["symbol"] for r in all_rows]
            sess_high, prev_high = {}, {}
            cur.execute("""SELECT symbol, MAX(high) FROM intraday_prices WHERE source='fyers_eq'
                AND timeframe='5m' AND ts::date=CURRENT_DATE AND symbol=ANY(%s) GROUP BY symbol""", (syms,))
            for sym, h in cur.fetchall():
                if h is not None: sess_high[sym] = float(h)
            cur.execute("""SELECT DISTINCT ON (symbol) symbol, high FROM raw_prices
                WHERE symbol=ANY(%s) AND price_date < CURRENT_DATE ORDER BY symbol, price_date DESC""", (syms,))
            for sym, h in cur.fetchall():
                if h is not None: prev_high[sym] = float(h)
        out = []
        for s in all_rows:
            sym = s["symbol"]
            passed, failed = [], []
            (passed if nifty_ok else failed).append("nifty_rsi")
            cmp = cmp_map.get(sym)
            pv  = pivots.get(sym)
            r1  = pv.get("r1") if pv else None
            sh  = sess_high.get(sym)
            ph  = prev_high.get(sym)
            (passed if (sh is not None and r1 is not None and sh >= r1) else failed).append("high_ge_r1")
            h2 = max([x for x in (sh, ph) if x is not None], default=None)
            fall = ((h2 - cmp) / h2 * 100.0) if (h2 and cmp) else None
            (passed if (fall is not None and 1.0 <= fall <= 10.0) else failed).append("fall_2d")
            d1 = s.get("day_1d")
            (passed if (d1 is not None and float(d1) < 0) else failed).append("day_red")
            (passed if _passes_filter(s.get("week_return"), -2.5, 0.0) else failed).append("week_return")
            out.append({"symbol": sym, "passed": len(passed), "total": 5,
                        "passed_filters": passed, "failed_filters": failed,
                        "gvm_score": s.get("gvm_score"), "mom_2d": s.get("mom_2d"), "v21_pass": None})
        out.sort(key=lambda x: (x["passed"], x["gvm_score"] if x["gvm_score"] is not None else -1), reverse=True)
        return {"basket": "sell_overbought", "score_date": str(date.today()),
                "universe": len(out), "filter_count": 5, "stocks": out,
                "v21_enabled": False, **BASKET_META.get("sell_overbought", {})}
    except Exception as e:
        raise HTTPException(500, f"so_stock_passcount failed: {e}")


@router.get("/so_stock_detail/{symbol}")
def so_stock_detail(symbol: str):
    """cc#382: per-stock 5-filter breakdown for sell_overbought V3 (spec id=2912). Mirrors
    so_stock_passcount / the handler so the green-row count equals n/5."""
    from v8_signal_writer import _get_nifty_rsi
    sym = symbol.upper()
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT day_1d, week_return FROM v8_metrics
                WHERE symbol=%s AND score_date=(SELECT MAX(score_date) FROM v8_metrics)""", (sym,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"No v8_metrics for {sym}")
            d1, wret = [float(x) if x is not None else None for x in row]
            cur.execute("""SELECT r1 FROM v8_paper_pivots WHERE symbol=%s
                AND pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)""", (sym,))
            pv = cur.fetchone()
            r1 = float(pv[0]) if pv and pv[0] is not None else None
            cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s AND cmp IS NOT NULL", (sym,))
            cr = cur.fetchone()
            cmp = float(cr[0]) if cr else None
            cur.execute("""SELECT MAX(high) FROM intraday_prices WHERE source='fyers_eq' AND timeframe='5m'
                AND ts::date=CURRENT_DATE AND symbol=%s""", (sym,))
            sh = cur.fetchone()[0]; sh = float(sh) if sh is not None else None
            cur.execute("""SELECT high FROM raw_prices WHERE symbol=%s AND price_date < CURRENT_DATE
                ORDER BY price_date DESC LIMIT 1""", (sym,))
            ph = cur.fetchone(); ph = float(ph[0]) if ph and ph[0] is not None else None
            nifty_rsi = _get_nifty_rsi(conn)

        def _fmt(v, d):
            return "--" if v is None else f"{v:.{d}f}"
        nifty_ok = nifty_rsi is not None and nifty_rsi <= 45.0
        h2 = max([x for x in (sh, ph) if x is not None], default=None)
        fall = ((h2 - cmp) / h2 * 100.0) if (h2 and cmp) else None

        p_nifty = nifty_ok
        p_high  = sh is not None and r1 is not None and sh >= r1
        p_fall  = fall is not None and 1.0 <= fall <= 10.0
        p_day   = d1 is not None and d1 < 0
        p_wret  = _passes_filter(wret, -2.5, 0.0)
        rows = [
            {"filter": "nifty_rsi",   "required": "<= 45",     "actual": _fmt(nifty_rsi, 1) + " (mkt)", "pass": p_nifty},
            {"filter": "high_ge_r1",  "required": "HIGH >= R1", "actual": f"{_fmt(sh, 2)} vs R1 {_fmt(r1, 2)}", "pass": p_high},
            {"filter": "fall_2d",     "required": "1% to 10%",  "actual": _fmt(fall, 2) + "%",  "pass": p_fall},
            {"filter": "day_red",     "required": "< 0",        "actual": _fmt(d1, 2) + "%",    "pass": p_day},
            {"filter": "week_return", "required": "-2.5 to 0",  "actual": _fmt(wret, 2) + "%",  "pass": p_wret},
        ]
        passed = sum(1 for r in rows if r["pass"])
        return {"symbol": sym, "cmp": cmp, "r1": r1, "session_high": sh, "nifty_rsi": nifty_rsi,
                "fall_2d_pct": round(fall, 2) if fall is not None else None,
                "passed": passed, "total": 5, "rows": rows, "spec": "SELL_OVERBOUGHT_V3 id=2912"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"so_stock_detail failed: {e}")


# ── Buy S1 Bounce dedicated funnel ────────────────────────────────────────────
def _s1b_funnel_stages():
    with _conn() as conn, conn.cursor() as cur:
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
        cur.execute("""
            WITH td AS (
                SELECT symbol,
                    -- cc#236: fyers_eq pinned + array_agg first/last, mirroring the engine
                    -- (_load_intraday_bars, cc#140). Was source-UNFILTERED -> mixed eq/fut/yahoo,
                    -- so the funnel disagreed with what the writer actually qualified.
                    (array_agg(open  ORDER BY ts ASC ))[1] AS day_open,
                    (array_agg(close ORDER BY ts DESC))[1] AS live_close,
                    MIN(low) AS today_low
                FROM intraday_prices WHERE ts::date=CURRENT_DATE AND source='fyers_eq' GROUP BY symbol
            ),
            hist AS (
                SELECT symbol,
                    MIN(low) FILTER (WHERE rn<=2) AS lo_2d,
                    MIN(low) FILTER (WHERE rn<=5) AS lo_5d
                FROM (SELECT symbol, low,
                        ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
                      FROM raw_prices WHERE price_date < CURRENT_DATE) x WHERE rn<=5 GROUP BY symbol
            )
            SELECT m.symbol, m.week_return, m.dma_50, m.vol_ratio, m.gvm_score, m.week_index_52,
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
        # cc#164: V2.1 hard-gate inputs (hourly_pct is live-only, not in v8_metrics).
        v21_enabled = _load_filter_state(conn).get("buy_s1_bounce", False)
        v21_metrics = _load_v21_live_metrics(conn, [r["symbol"] for r in rows])
        for r in rows:
            r["hourly_pct"] = v21_metrics.get(r["symbol"], {}).get("hourly_pct")

    total = len(rows)
    gate_open = nifty_rsi is not None and nifty_rsi >= 55.0

    def _f(v):
        try: return float(v) if v is not None else None
        except (TypeError, ValueError): return None

    for r in rows:
        cmp = _f(r.get("live_close")); op = _f(r.get("day_open"))
        lo2 = _f(r.get("lo_2d")); lo5 = _f(r.get("lo_5d")); tlow = _f(r.get("today_low"))
        r["recovery_2d"] = ((cmp - lo2) / lo2 * 100) if (cmp and lo2 and lo2 > 0) else None
        r["day_ret"]     = ((cmp - op) / op * 100) if (cmp and op and op > 0) else None
        wl_candidates = [x for x in (lo5, tlow) if x is not None]
        r["week_low"] = min(wl_candidates) if wl_candidates else None

    stages = []
    survivors_list = rows[:]
    prev = total

    def _stage(label, cond, cmin, cmax):
        nonlocal survivors_list, prev
        survivors_list = [s for s in survivors_list if cond(s)]
        n = len(survivors_list)
        stages.append({"metric": label, "condition_min": cmin, "condition_max": cmax,
                       "survivors": n, "killed": prev - n})
        prev = n

    if not gate_open:
        rsi_disp = f"{nifty_rsi:.1f}" if nifty_rsi is not None else "n/a"
        stages.append({"metric": "nifty_rsi (market gate)",
                       "condition_min": ">= 55", "condition_max": f"CLOSED ({rsi_disp})",
                       "survivors": 0, "killed": total})
        return stages, nifty_rsi, gate_open, total

    stages.append({"metric": "nifty_rsi (market gate)",
                   "condition_min": ">= 55", "condition_max": f"OPEN ({nifty_rsi:.1f})",
                   "survivors": total, "killed": 0})
    _stage("gvm_score",   lambda s: _passes_filter(s.get("gvm_score"), 7.0, None), ">= 7.0", "-")   # cc_task #76 4d
    _stage("week_return", lambda s: _passes_filter(s.get("week_return"), 0.0, 2.5), ">= 0%", "<= 2.5%")
    _stage("dma_50",      lambda s: _passes_filter(s.get("dma_50"), 0.0, None),     "> 0%",   "-")
    _stage("vol_ratio",   lambda s: _passes_filter(s.get("vol_ratio"), 1.5, None),  ">= 1.5x", "-")
    _stage("recovery_2d", lambda s: _passes_filter(s.get("recovery_2d"), 2.0, 8.0), ">= 2%", "<= 8%")
    _stage("day_ret",     lambda s: _passes_filter(s.get("day_ret"), 0.5, None),    "> 0.5%", "close>open")
    _stage("week_low_vs_s1",
           lambda s: s.get("week_low") is not None and s.get("s1") is not None
                     and float(s["week_low"]) <= float(s["s1"]),
           "week_low", "<= S1")

    # cc#164: V2.1 hard-gate stage (hourly_pct >0..1.0 + week_index_52 50..90),
    # chained onto the survivors above -- this dedicated funnel has no separate
    # v8_qualified read, so "final" must include this gate to accurately reflect
    # what the live engine (v8_signal_writer.py) qualifies.
    n_before = len(survivors_list)
    survivors_list = [s for s in survivors_list
                      if v21_hard_gate_pass("buy_s1_bounce", s, v21_enabled)]
    stages.append({
        "metric": "v2.1_hard_gate", "condition_min": "hourly_pct(0..1.0) + week_index_52(50..90)",
        "condition_max": "enabled" if v21_enabled else "disabled",
        "survivors": len(survivors_list), "killed": n_before - len(survivors_list),
        "v21_enabled": v21_enabled,
        "v21_note": "V2.1 hard gate (hourly_pct + week_index_52)" if v21_enabled
                    else "V2.1 hard gate DISABLED (locked behavior in effect)",
    })
    return stages, nifty_rsi, gate_open, total


def s1b_funnel_detail():
    try:
        stages, nifty_rsi, gate_open, total = _s1b_funnel_stages()
        final_qualified = stages[-1]["survivors"] if stages else 0
        v21_enabled = stages[-1].get("v21_enabled", False) if stages else False
        return {
            "basket": "buy_s1_bounce", "score_date": str(date.today()),
            "universe": total, "final": final_qualified,
            "filter_count": 8, "n_filters": 8,
            "gate_type": "strict AND (all must pass)",
            "market_gate": {"metric": "nifty_rsi", "threshold": 55.0,
                            "value": round(nifty_rsi, 1) if nifty_rsi is not None else None,
                            "open": gate_open},
            "score_qualified": final_qualified, "pivot_pass": final_qualified,
            "stages": stages, "v21_enabled": v21_enabled,
            **BASKET_META.get("buy_s1_bounce", {})
        }
    except Exception as e:
        raise HTTPException(500, f"s1b_funnel_detail failed: {e}")


def s1b_funnel_counts():
    try:
        stages, nifty_rsi, gate_open, total = _s1b_funnel_stages()
        counts = {st["metric"]: st["survivors"] for st in stages}
        counts["_market_gate_open"] = gate_open
        counts["_nifty_rsi"] = round(nifty_rsi, 1) if nifty_rsi is not None else None
        counts["_final_qualified"] = stages[-1]["survivors"] if stages else 0
        counts["_v21_enabled"] = stages[-1].get("v21_enabled", False) if stages else False
        return {"basket": "buy_s1_bounce", "score_date": str(date.today()),
                "counts": counts, "source": "live_7filter"}
    except Exception as e:
        raise HTTPException(500, f"s1b_funnel_counts failed: {e}")


def s1b_stock_passcount():
    try:
        with _conn() as conn, conn.cursor() as cur:
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
            gate_open = nifty_rsi is not None and nifty_rsi >= 55.0
            cur.execute("""
                WITH td AS (
                    SELECT symbol,
                        -- cc#236: fyers_eq pinned + array_agg first/last (engine parity,
                        -- mirrors _load_intraday_bars cc#140). Was source-UNFILTERED.
                        (array_agg(open  ORDER BY ts ASC ))[1] AS day_open,
                        (array_agg(close ORDER BY ts DESC))[1] AS live_close,
                        MIN(low) AS today_low
                    FROM intraday_prices WHERE ts::date=CURRENT_DATE AND source='fyers_eq' GROUP BY symbol
                ),
                hist AS (
                    SELECT symbol,
                        MIN(low) FILTER (WHERE rn<=2) AS lo_2d,
                        MIN(low) FILTER (WHERE rn<=5) AS lo_5d
                    FROM (SELECT symbol, low,
                            ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
                          FROM raw_prices WHERE price_date < CURRENT_DATE) x
                    WHERE rn<=5 GROUP BY symbol
                )
                SELECT m.symbol, m.week_return, m.dma_50, m.vol_ratio, m.gvm_score, m.week_index_52,
                       td.day_open, td.live_close, td.today_low, h.lo_2d, h.lo_5d, p.s1
                FROM v8_metrics m
                JOIN futures_universe f ON f.symbol=m.symbol AND f.is_active=TRUE
                LEFT JOIN td ON td.symbol=m.symbol
                LEFT JOIN hist h ON h.symbol=m.symbol
                LEFT JOIN v8_paper_pivots p ON p.symbol=m.symbol
                    AND p.pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)
                WHERE m.score_date=(SELECT MAX(score_date) FROM v8_metrics)
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            v21_enabled = _load_filter_state(conn).get("buy_s1_bounce", False)
            v21_metrics = _load_v21_live_metrics(conn, [r["symbol"] for r in rows])

        def _f(v):
            try: return float(v) if v is not None else None
            except (TypeError, ValueError): return None

        out = []
        for r in rows:
            cmp = _f(r.get("live_close")); op = _f(r.get("day_open"))
            lo2 = _f(r.get("lo_2d")); lo5 = _f(r.get("lo_5d")); tlow = _f(r.get("today_low"))
            rec2d  = ((cmp - lo2) / lo2 * 100) if (cmp and lo2 and lo2 > 0) else None
            dayret = ((cmp - op) / op * 100) if (cmp and op and op > 0) else None
            wl_cand = [x for x in (lo5, tlow) if x is not None]
            week_low = min(wl_cand) if wl_cand else None
            s1 = _f(r.get("s1"))
            r["hourly_pct"] = v21_metrics.get(r["symbol"], {}).get("hourly_pct")
            checks = {
                "nifty_rsi":      gate_open,
                "gvm_score":      _passes_filter(r.get("gvm_score"), 7.0, None),
                "week_return":    _passes_filter(r.get("week_return"), 0.0, 2.5),
                "dma_50":         _passes_filter(r.get("dma_50"), 0.0, None),
                "vol_ratio":      _passes_filter(r.get("vol_ratio"), 1.5, None),
                "recovery_2d":    _passes_filter(rec2d, 2.0, 8.0),
                "day_ret":        _passes_filter(dayret, 0.5, None),
                "week_low_vs_s1": (week_low is not None and s1 is not None and week_low <= s1),
            }
            passed = [k for k, ok in checks.items() if ok]
            failed = [k for k, ok in checks.items() if not ok]
            v21_pass = v21_hard_gate_pass("buy_s1_bounce", r, v21_enabled)
            out.append({"symbol": r["symbol"], "passed": len(passed), "total": 8,
                        "passed_filters": passed, "failed_filters": failed,
                        "gvm_score": r.get("gvm_score"),
                        "recovery_2d": round(rec2d, 2) if rec2d is not None else None,
                        "day_ret": round(dayret, 2) if dayret is not None else None,
                        "v21_pass": v21_pass})
        out.sort(key=lambda x: (x["passed"], x["gvm_score"] if x["gvm_score"] is not None else -1), reverse=True)
        return {"basket": "buy_s1_bounce", "score_date": str(date.today()),
                "universe": len(out), "filter_count": 8, "stocks": out,
                "nifty_rsi": round(nifty_rsi, 1) if nifty_rsi is not None else None,
                "gate_open": gate_open, "v21_enabled": v21_enabled,
                **BASKET_META.get("buy_s1_bounce", {})}
    except Exception as e:
        raise HTTPException(500, f"s1b_stock_passcount failed: {e}")


@router.get("/buy_s1_bounce")
def buy_s1_bounce_qualified(limit: int = 50):
    try:
        open_pos      = _load_open_positions("buy_s1_bounce")
        slot_full     = _load_slot_full("buy_s1_bounce")
        closed_today  = _load_closed_today("buy_s1_bounce")      # cc#326
        conflict_syms = _load_conflict_syms("buy_s1_bounce")     # cc#326
        missed        = _load_missed_reasons("buy_s1_bounce")    # cc#326
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
                    (q.metrics->>'filter_score')::numeric AS filter_score,
                    (q.metrics->>'filter_total')::numeric AS filter_total,
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
        rows = [r for r in rows if (r.get('gvm_score') or 0) >= 7.0]   # cc_task #76 4d: S1B GVM>=7.0 hard gate
        for r in rows:
            r['segment'] = _seg_override(r['symbol'], r.get('segment'))
            r['status']  = r.pop('stored_status', None) or 'QUALIFIED'
        with _conn() as conn, conn.cursor() as cur:      # cc#240: inject prior-day OPEN positions
            rows = _inject_open_positions(cur, rows, "buy_s1_bounce", open_pos)
        rows = _enrich_with_status(rows, "buy_s1_bounce", open_pos, slot_full,
                                   closed_today, conflict_syms, missed)   # cc#326
        return {
            "basket": "buy_s1_bounce", "count": len(rows),
            "target": "+2.0% fixed from entry", "stop": "-2.0% fixed from entry",
            "slot_architecture": "Dedicated ring-fenced: 3 (Strong Bull/Bull/Neutral) / 2 (Bearish)",
            "win_pct": "73.9%", "ev_per_trade": "+0.716%", "stocks": rows,
        }
    except Exception as e:
        raise HTTPException(500, f"buy_s1_bounce_qualified failed: {e}")


@router.get("/sell_overbought")
def sell_overbought(limit: int = 50):
    try:
        open_pos      = _load_open_positions("sell_overbought")
        slot_full     = _load_slot_full("sell_overbought")
        closed_today  = _load_closed_today("sell_overbought")      # cc#326
        conflict_syms = _load_conflict_syms("sell_overbought")     # cc#326
        missed        = _load_missed_reasons("sell_overbought")    # cc#326
        with _conn() as conn, conn.cursor() as cur:
            # cc#382 V3: qualifiers come from the dedicated handler (v8_qualified today), NOT a V2
            # inline recompute. Display FIXED -/+2% levels off the live CMP; enrich with pivots for context.
            # cc#379: also select the canonical basket-table fields (cmp, mom_2d, dma_50, dma_200,
            # s1, day_1d) so sell_overbought renders the SAME unified column set as the other baskets.
            # Display-only enrichment — the V3 qualification handler is untouched.
            cur.execute("""
                SELECT q.symbol,
                    ROUND(COALESCE(c.cmp, q.cmp)::numeric,2)          AS entry,
                    ROUND(COALESCE(c.cmp, q.cmp)::numeric,2)          AS cmp,
                    ROUND((COALESCE(c.cmp, q.cmp)*0.98)::numeric,2)   AS target,
                    ROUND((COALESCE(c.cmp, q.cmp)*1.02)::numeric,2)   AS stop,
                    -2.0 AS tgt_pct, 2.0 AS sl_pct,
                    ROUND(q.daily_rsi::numeric,1)   AS daily_rsi,
                    ROUND(q.rsi_month::numeric,1)   AS rsi_month,
                    ROUND(q.week_return::numeric,2) AS week_return,
                    ROUND(q.sector_week::numeric,2) AS sector_week,
                    ROUND(q.gvm_score::numeric,2)   AS gvm_score,
                    ROUND(q.mom_2d::numeric,2)      AS mom_2d,
                    ROUND(q.dma_50::numeric,2)      AS dma_50,
                    ROUND(q.dma_200::numeric,2)     AS dma_200,
                    ROUND(m.day_1d::numeric,2)      AS day_1d,
                    ROUND(p.r1::numeric,2) AS r1, ROUND(p.pp::numeric,2) AS pp,
                    ROUND(p.s1::numeric,2) AS s1,
                    q.signal_ts, q.metrics
                FROM v8_qualified q
                LEFT JOIN cmp_prices c ON c.symbol=q.symbol
                LEFT JOIN v8_metrics m ON m.symbol=q.symbol
                    AND m.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                LEFT JOIN v8_paper_pivots p ON p.symbol=q.symbol
                    AND p.pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)
                WHERE q.basket='sell_overbought' AND q.signal_date=CURRENT_DATE
                ORDER BY q.signal_ts DESC LIMIT %s
            """, (min(max(limit, 1), 200),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows: r["status"] = "QUALIFIED"
        with _conn() as conn, conn.cursor() as cur:      # cc#240: inject prior-day OPEN positions
            rows = _inject_open_positions(cur, rows, "sell_overbought", open_pos)
        rows = _enrich_with_status(rows, "sell_overbought", open_pos, slot_full,
                                   closed_today, conflict_syms, missed)   # cc#326
        return {"basket": "sell_overbought", "count": len(rows),
                "target": "-2.0% fixed", "stop": "+2.0% fixed",
                "slot_architecture": "Dedicated ring-fenced: 4 (Bull/Neutral) / 3 (Bearish)",
                "win_pct": "59% (1yr bt)", "ev_per_trade": "+0.36%", "stocks": rows}
    except Exception as e:
        raise HTTPException(500, f"sell_overbought failed: {e}")


@router.get("/forthcoming-results")
def forthcoming_results():
    """cc#226: upcoming quarterly results for tradeable NSE stocks — cap-tiered, with V8-universe
    + blackout flags. Read-only calendar view (earnings_calendar JOIN screener_raw, NSE-only);
    NOT coupled to the paper engine. Cap tier = global market_cap rank (<=100 Large / <=250 Mid /
    else Small, same as Quant Basket). Blackout = in active futures_universe AND ex_date within the
    engine window (today / today+1). Sorted ex_date ASC, market_cap DESC."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH ranked AS (
                    SELECT nse_code,
                           ROW_NUMBER() OVER (ORDER BY market_cap DESC NULLS LAST) AS mc_rank
                    FROM screener_raw WHERE nse_code IS NOT NULL AND nse_code <> ''
                )
                SELECT ec.ticker, ec.company_name, ec.ex_date,
                       CASE WHEN r.mc_rank <= 100 THEN 'Large'
                            WHEN r.mc_rank <= 250 THEN 'Mid' ELSE 'Small' END AS cap_tier,
                       ROUND(sr.market_cap::numeric, 0) AS market_cap_cr,
                       (fu.symbol IS NOT NULL) AS in_v8_universe,
                       (fu.symbol IS NOT NULL AND ec.ex_date IN (CURRENT_DATE, CURRENT_DATE + 1)) AS blackout
                FROM earnings_calendar ec
                JOIN screener_raw sr ON UPPER(sr.nse_code) = UPPER(ec.ticker)   -- NSE-only: BSE-only tickers have no nse_code match
                JOIN ranked r ON r.nse_code = sr.nse_code
                LEFT JOIN futures_universe fu ON UPPER(fu.symbol) = UPPER(ec.ticker) AND fu.is_active = TRUE
                WHERE ec.ex_date >= CURRENT_DATE
                ORDER BY ec.ex_date ASC, sr.market_cap DESC NULLS LAST
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        results = [{
            "symbol":         r["ticker"],
            "company":        r["company_name"],
            "result_date":    str(r["ex_date"]) if r["ex_date"] else None,
            "cap_tier":       r["cap_tier"],
            "market_cap_cr":  float(r["market_cap_cr"]) if r["market_cap_cr"] is not None else None,
            "in_v8_universe": bool(r["in_v8_universe"]),
            "blackout":       bool(r["blackout"]),
        } for r in rows]
        return {"count": len(results), "results": results}
    except Exception as e:
        raise HTTPException(500, f"forthcoming_results failed: {e}")


@router.get("/adr")
def adr_only():
    try:
        with _conn() as conn, conn.cursor() as cur:
            adv, dec, unc, adr, source, adr_date = _read_adr(cur)
        return {"price_date": adr_date, "adr": adr, "advances": adv,
                "declines": dec, "unchanged": unc, "pass": adr >= 1.0, "source": source}
    except Exception as e:
        raise HTTPException(500, f"adr failed: {e}")


@router.get("/v9_pairs_sectors")
def v9_pairs_sectors():
    """cc#385: dynamic sector list for the V9 Sector-Pairs concept tab. Every futures-universe
    sector (GVM segment) holding >=4 active stocks — the pool the long-short pairs engine will draw
    from once the backfills land. Read-only display data; NO engine, no signal computation."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT ir.gvm_segment AS segment, COUNT(*) AS stock_count
                FROM futures_universe fu
                JOIN input_raw ir ON UPPER(ir.nse_code)=UPPER(fu.symbol)
                WHERE fu.is_active=TRUE AND ir.gvm_segment IS NOT NULL AND ir.gvm_segment<>''
                GROUP BY ir.gvm_segment HAVING COUNT(*)>=4
                ORDER BY COUNT(*) DESC, ir.gvm_segment
            """)
            rows = [{"segment": r[0], "stock_count": int(r[1])} for r in cur.fetchall()]
        return {"sector_count": len(rows),
                "total_stocks": sum(r["stock_count"] for r in rows),
                "min_stocks": 4, "status": "DESIGN",
                "sectors": rows}
    except Exception as e:
        raise HTTPException(500, f"v9_pairs_sectors failed: {e}")


@router.get("/ohol")
def ohol():
    """cc#388: Open=High (bearish) / Open=Low (bullish) live scanner over the active futures universe.
    Open reference = the 09:15 5-min bar high/low. From 09:35 IST every tick: OPEN=HIGH list = session
    running HIGH still <= open_ref_high*1.001 (0.1% tol) AND day% < 0 AND CMP < PP; OPEN=LOW mirror
    (session LOW >= open_ref_low*0.999 AND day% > 0 AND CMP > PP). A name drops off once its level
    breaks. DAY% uses the cc#373 pair (CMP spot / last raw close before the CMP's session). Empty
    pre-09:35 on a live day; off-market shows the last-session final snapshot with its date."""
    def _ff(v):
        try:
            return float(v) if v is not None else None
        except Exception:
            return None
    now = _ist_now()
    live = _market_open()
    if live and now.time() < now.replace(hour=9, minute=35, second=0, microsecond=0).time():
        return {"open_high": [], "open_low": [], "live": True,
                "as_of_ts": now.strftime("%Y-%m-%d %H:%M:%S IST"), "session_date": str(now.date()),
                "note": "OH-OL populates from 09:35 IST — waiting for the opening range to settle."}
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH sess AS (SELECT MAX(ts::date) d FROM intraday_prices
                              WHERE source='fyers_eq' AND timeframe='5m'),
                fut AS (SELECT UPPER(symbol) sym FROM futures_universe WHERE is_active=TRUE),
                bars AS (
                    SELECT i.symbol, i.ts, i.high, i.low, i.close
                    FROM intraday_prices i, sess
                    WHERE i.source='fyers_eq' AND i.timeframe='5m' AND i.ts::date = sess.d
                      AND UPPER(i.symbol) IN (SELECT sym FROM fut)
                ),
                openb AS (SELECT DISTINCT ON (symbol) symbol, high AS oref_high, low AS oref_low
                          FROM bars ORDER BY symbol, ts ASC),
                agg AS (SELECT symbol, MAX(high) sess_high, MIN(low) sess_low,
                               (ARRAY_AGG(close ORDER BY ts DESC))[1] last_close
                        FROM bars GROUP BY symbol)
                SELECT a.symbol, a.sess_high, a.sess_low, a.last_close,
                       o.oref_high, o.oref_low, cp.cmp, pc.prev_close, pv.pp
                FROM agg a
                JOIN openb o ON o.symbol = a.symbol
                LEFT JOIN LATERAL (SELECT close AS cmp FROM intraday_prices
                    WHERE symbol=a.symbol AND source<>'fyers_fut' ORDER BY ts DESC LIMIT 1) cp ON true
                LEFT JOIN LATERAL (SELECT close AS prev_close FROM raw_prices
                    WHERE symbol=a.symbol AND price_date < (SELECT d FROM sess)
                    ORDER BY price_date DESC LIMIT 1) pc ON true
                LEFT JOIN LATERAL (SELECT pp FROM v8_paper_pivots
                    WHERE symbol=a.symbol ORDER BY pivot_date DESC LIMIT 1) pv ON true
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.execute("SELECT MAX(ts::date) FROM intraday_prices WHERE source='fyers_eq' AND timeframe='5m'")
            sess = cur.fetchone()[0]
    except Exception as e:
        raise HTTPException(500, f"ohol failed: {e}")
    oh, ol = [], []
    for r in rows:
        cmp_v = _ff(r.get("cmp")) or _ff(r.get("last_close"))
        prevc, pp = _ff(r.get("prev_close")), _ff(r.get("pp"))
        sh, sl = _ff(r.get("sess_high")), _ff(r.get("sess_low"))
        orh, orl = _ff(r.get("oref_high")), _ff(r.get("oref_low"))
        if None in (cmp_v, prevc, pp, sh, sl, orh, orl) or prevc == 0 or orh == 0 or orl == 0:
            continue
        day_pct = round((cmp_v / prevc - 1) * 100, 2)
        dist_pp = round((cmp_v - pp) / pp * 100, 2)
        if sh <= orh * 1.001 and day_pct < 0 and cmp_v < pp:
            oh.append({"symbol": r["symbol"], "cmp": round(cmp_v, 2), "day_pct": day_pct,
                       "dist_to_pp": dist_pp, "open_ref_high": round(orh, 2),
                       "session_high": round(sh, 2), "hold_pct": round((orh - sh) / orh * 100, 2)})
        if sl >= orl * 0.999 and day_pct > 0 and cmp_v > pp:
            ol.append({"symbol": r["symbol"], "cmp": round(cmp_v, 2), "day_pct": day_pct,
                       "dist_to_pp": dist_pp, "open_ref_low": round(orl, 2),
                       "session_low": round(sl, 2), "hold_pct": round((sl - orl) / orl * 100, 2)})
    oh.sort(key=lambda x: x["day_pct"])       # most bearish first
    ol.sort(key=lambda x: -x["day_pct"])      # most bullish first
    return {"open_high": oh, "open_low": ol, "live": bool(live), "snapshot": (not live),
            "as_of_ts": now.strftime("%Y-%m-%d %H:%M:%S IST"),
            "session_date": str(sess) if sess else None}


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


@router.get("/daylog")
def v8_daylog():
    """Day-wise aggregated performance table. Capital base Rs.50,00,000. Brokerage Rs.500/closed trade."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH all_dates AS (
                    SELECT DISTINCT entry_ts::date AS d FROM v8_paper_positions
                    UNION SELECT DISTINCT entry_ts::date FROM v8_paper_trades
                    UNION SELECT DISTINCT COALESCE(closed_at::date, exit_ts::date) FROM v8_paper_trades
                ),
                opened AS (
                    SELECT entry_ts::date AS d,
                        COUNT(*) FILTER (WHERE side='LONG') AS long_opened,
                        COUNT(*) FILTER (WHERE side='SHORT') AS short_opened,
                        COUNT(*) AS total_opened
                    FROM (SELECT entry_ts, side FROM v8_paper_positions
                          UNION ALL SELECT entry_ts, side FROM v8_paper_trades) e
                    GROUP BY entry_ts::date
                ),
                closed AS (
                    SELECT COALESCE(closed_at::date, exit_ts::date) AS d,
                        COUNT(*) FILTER (WHERE side='LONG') AS long_closed,
                        COUNT(*) FILTER (WHERE side='SHORT') AS short_closed,
                        COUNT(*) AS total_closed,
                        ROUND(SUM(pnl)::numeric,2) AS gross_pnl,
                        ROUND(AVG(pnl)::numeric,2) AS avg_pnl,
                        COUNT(*)*500 AS brokerage,
                        ROUND((SUM(pnl)-COUNT(*)*500)::numeric,2) AS net_pnl
                    FROM v8_paper_trades GROUP BY COALESCE(closed_at::date, exit_ts::date)
                ),
                cumulative AS (
                    SELECT ad.d,
                        COALESCE(o.total_opened,0) AS opened,
                        COALESCE(o.long_opened,0) AS long_open,
                        COALESCE(o.short_opened,0) AS short_open,
                        COALESCE(c.total_closed,0) AS closed,
                        COALESCE(c.long_closed,0) AS long_closed,
                        COALESCE(c.short_closed,0) AS short_closed,
                        COALESCE(c.gross_pnl,0) AS gross_pnl,
                        c.avg_pnl,
                        COALESCE(c.brokerage,0) AS brokerage,
                        COALESCE(c.net_pnl,0) AS net_pnl
                    FROM all_dates ad
                    LEFT JOIN opened o ON o.d=ad.d
                    LEFT JOIN closed c ON c.d=ad.d
                )
                SELECT d AS date, opened, long_open, short_open, closed, long_closed, short_closed,
                    gross_pnl, avg_pnl, brokerage, net_pnl,
                    SUM(opened) OVER (ORDER BY d ROWS UNBOUNDED PRECEDING)
                      - SUM(closed) OVER (ORDER BY d ROWS UNBOUNDED PRECEDING) AS net_open,
                    ROUND((net_pnl/5000000.0*100)::numeric,2) AS return_pct
                FROM cumulative ORDER BY d DESC
            """)
            cols = [d[0] for d in cur.description]
            rows = []
            for r in cur.fetchall():
                row = dict(zip(cols, r))
                row['date']       = str(row['date'])
                row['gross_pnl']  = float(row['gross_pnl'])  if row['gross_pnl']  is not None else 0.0
                row['avg_pnl']    = float(row['avg_pnl'])    if row['avg_pnl']    is not None else None
                row['net_pnl']    = float(row['net_pnl'])    if row['net_pnl']    is not None else 0.0
                row['net_open']   = int(row['net_open'])     if row['net_open']   is not None else 0
                row['return_pct'] = float(row['return_pct']) if row['return_pct'] is not None else 0.0
                row['brokerage']  = int(row['brokerage'])    if row['brokerage']  is not None else 0
                rows.append(row)
        total_gross = sum(r['gross_pnl'] for r in rows)
        total_brok  = sum(r['brokerage']  for r in rows)
        total_net   = sum(r['net_pnl']    for r in rows)
        return {
            "days": rows,
            "summary": {
                "total_opened":       sum(r['opened'] for r in rows),
                "total_closed":       sum(r['closed'] for r in rows),
                "total_gross_pnl":    round(total_gross, 2),
                "total_brokerage":    total_brok,
                "total_net_pnl":      round(total_net, 2),
                "net_open":           rows[0]['net_open'] if rows else 0,
                "overall_return_pct": round(total_net / 5_000_000 * 100, 2),
            },
            "capital_base": 5_000_000,
        }
    except Exception as e:
        raise HTTPException(500, f"v8_daylog failed: {e}")


@router.get("/global_indices")
def v8_global_indices():
    """cc#264: Global Indices tab data (grouped by category). cc#281: any symbol present in
    global_intraday is overlaid with its latest live 5-min close (chg_pct recomputed vs the
    daily prev_close); symbols absent from global_intraday keep the once-daily global_indices
    snapshot. Today only the 6 commodity/crypto symbols are live there; cc#282 adds the 7
    index/currency/VIX symbols to the same feed, at which point they overlay automatically —
    no further endpoint change. `data_ts` = the freshest underlying live data timestamp (IST),
    so a stalled feed is honestly reflected in the tab's Last-updated label rather than masked."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            # cc#414: last daily snapshot PER SYMBOL regardless of date, so no tile vanishes on
            # weekends (previously quote_date=MAX(...) returned only symbols updated today — i.e.
            # just 24/7 crypto). Each symbol keeps its own latest close; freshness is decided per tile.
            cur.execute("""
                SELECT symbol, name, category, price, prev_close, chg_pct, quote_date FROM (
                    SELECT DISTINCT ON (symbol) symbol, name, category, price, prev_close, chg_pct, quote_date
                    FROM global_indices ORDER BY symbol, quote_date DESC
                ) t ORDER BY category, symbol
            """)
            cols = [d[0] for d in cur.description]
            base = [dict(zip(cols, r)) for r in cur.fetchall()]
            # cc#414: latest intraday close PER SYMBOL over the 7-day rolling window (was CURRENT_DATE
            # only, which never fired on weekends). Age is checked per tile below — a stale tick still
            # overlays as the true last print and flips the tile to PREV CLOSE with its age stamp.
            cur.execute("""
                SELECT DISTINCT ON (symbol) symbol, close, ts
                FROM global_intraday
                WHERE ts >= NOW() - INTERVAL '7 days'
                ORDER BY symbol, ts DESC
            """)
            live = {r[0]: (float(r[1]) if r[1] is not None else None, r[2]) for r in cur.fetchall()}
            # cc#349: server IST-now to age each intraday bar — a tile is "live" only if its last bar
            # is <=15 min old; older bars still overlay (as the true last close) but flip to PREV CLOSE.
            cur.execute("SELECT (NOW() AT TIME ZONE 'Asia/Kolkata')")
            now_ist = cur.fetchone()[0]
        STALE_MIN = 15
        qd = None
        latest_ts = None
        rows = []
        for row in base:
            qd = str(row['quote_date'])
            row['quote_date'] = str(row['quote_date'])
            row['price']      = float(row['price'])      if row['price']      is not None else None
            row['prev_close'] = float(row['prev_close']) if row['prev_close'] is not None else None
            row['chg_pct']    = float(row['chg_pct'])    if row['chg_pct']    is not None else None
            lv = live.get(row['symbol'])
            if lv and lv[0] is not None:
                # cc#349: overlay the latest intraday close regardless of age (it IS the last real
                # print), but 'live' is honest — True only while the bar is fresh (<=15 min). A stale
                # bar (market closed / feed frozen) keeps its ts so the tile reads PREV CLOSE · <time>.
                age_min = (now_ist - lv[1]).total_seconds() / 60.0 if now_ist else 0
                row['price'] = lv[0]
                row['ts']    = str(lv[1])                          # tile shows ITS OWN last-tick time
                row['live']  = age_min <= STALE_MIN
                pc = row['prev_close']
                row['chg_pct'] = round((lv[0] - pc) / pc * 100, 2) if pc else row['chg_pct']
                if row['live'] and (latest_ts is None or lv[1] > latest_ts):
                    latest_ts = lv[1]                              # data_ts reflects only genuinely-live tiles
            else:
                # cc#291: daily tile has no intraday bar — frontend shows its trading date
                # (row['quote_date']) instead of a misleading time, so live vs daily freshness is
                # visually obvious per tile.
                row['ts']   = None
                row['live'] = False
            rows.append(row)
        # cc#414: markets_closed = no non-crypto tile is currently live (drives the header suffix)
        markets_closed = not any(r.get("live") and r.get("category") != "crypto" for r in rows)
        return {"quote_date": qd, "data_ts": (str(latest_ts) if latest_ts else None),
                "instruments": rows, "count": len(rows), "markets_closed": markets_closed}
    except Exception as e:
        raise HTTPException(500, f"v8_global_indices failed: {e}")


@router.get("/indiavix_intraday")
def v8_indiavix_intraday():
    """cc#266/318: INDIAVIX at TRUE 5-MIN resolution (every bar 09:15-15:30) across the most
    recent 5 TRADING days (rolling window, auto-advances daily) for the Master Dashboard VIX line
    chart. cc#318 dropped the old xx:15 hourly filter so a missing :15 bar can no longer make the
    inline value / chart last-point lag behind the freshest tick; the last element is always the
    latest bar. Missing bars are simply absent, never interpolated."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH days AS (
                    SELECT DISTINCT ts::date AS d
                    FROM intraday_prices
                    WHERE symbol='INDIAVIX' AND source='fyers_eq'
                    ORDER BY d DESC LIMIT 5
                )
                SELECT ts, close
                FROM intraday_prices
                WHERE symbol='INDIAVIX' AND source='fyers_eq'
                  AND ts::date IN (SELECT d FROM days)
                  AND EXTRACT(HOUR FROM ts) BETWEEN 9 AND 15
                ORDER BY ts ASC
            """)
            points = []
            for ts, close in cur.fetchall():
                if close is None:
                    continue
                points.append({"ts": str(ts), "vix": float(close)})
        return {"points": points, "count": len(points)}
    except Exception as e:
        raise HTTPException(500, f"v8_indiavix_intraday failed: {e}")


# ── cc#319 / cc#337: NIFTY 50 Sectors — curated Scorr THEME groups over the 50 constituents ────
# Reuses the EXACT sector-aggregate math the Top/Bottom Sector cards use (avg day_1d/week_return/
# month_return from v8_metrics), scoped to the 50 NIFTY-50 symbols. cc#337: grouped by the curated
# nifty50_constituents.theme (12 Scorr themes) instead of the raw NSE industry (15 groups, which
# produced 3 single-stock sectors + incoherent pairs). The .industry column stays untouched as
# provenance. Theme membership comes from nifty50_constituents (populated + verified summing to 50).

@router.get("/nifty50_sectors")
def v8_nifty50_sectors():
    """One card per curated Scorr theme across the NIFTY 50: avg 1D/1W/1M return + stock count,
    sorted 1D% desc. Same field names the frontend sectorCards() consumes."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT nc.theme,
                       COUNT(*) FILTER (WHERE m.day_1d IS NOT NULL)      AS n,
                       AVG(m.day_1d)      FILTER (WHERE m.day_1d IS NOT NULL),
                       AVG(m.week_return) FILTER (WHERE m.week_return IS NOT NULL),
                       AVG(m.month_return)FILTER (WHERE m.month_return IS NOT NULL)
                FROM nifty50_constituents nc
                LEFT JOIN v8_metrics m ON m.symbol = nc.symbol
                     AND m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
                GROUP BY nc.theme
                HAVING COUNT(*) FILTER (WHERE m.day_1d IS NOT NULL) > 0
                ORDER BY AVG(m.day_1d) FILTER (WHERE m.day_1d IS NOT NULL) DESC NULLS LAST
            """)
            sectors = []
            for theme, n, d1, wk, mo in cur.fetchall():
                sectors.append({
                    "segment": theme, "count": int(n),
                    "avg_day_1d": round(float(d1), 2) if d1 is not None else None,
                    "sector_week": round(float(wk), 2) if wk is not None else None,
                    "sector_month": round(float(mo), 2) if mo is not None else None,
                })
        return {"sectors": sectors, "count": len(sectors)}
    except Exception as e:
        raise HTTPException(500, f"v8_nifty50_sectors failed: {e}")


@router.get("/nifty50_sectors/{theme}/holdings")
def v8_nifty50_sector_holdings(theme: str):
    """The NIFTY-50 stocks in one curated Scorr theme, each with gvm_score + 1D/1W/1M — same
    shape as the existing sector-detail modal, for the card click-through. cc#337: filter by theme."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT nc.symbol, nc.company_name, m.gvm_score, m.day_1d, m.week_return, m.month_return
                FROM nifty50_constituents nc
                LEFT JOIN v8_metrics m ON m.symbol = nc.symbol
                     AND m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
                WHERE nc.theme = %s
                ORDER BY m.day_1d DESC NULLS LAST
            """, (theme,))
            holdings = []
            for sym, company, gvm, d1, wk, mo in cur.fetchall():
                holdings.append({
                    "symbol": sym, "company": company,
                    "gvm_score": round(float(gvm), 2) if gvm is not None else None,
                    "day_1d": round(float(d1), 2) if d1 is not None else None,
                    "week_return": round(float(wk), 2) if wk is not None else None,
                    "month_return": round(float(mo), 2) if mo is not None else None,
                })
        return {"theme": theme, "holdings": holdings, "count": len(holdings)}
    except Exception as e:
        raise HTTPException(500, f"v8_nifty50_sector_holdings failed: {e}")


# ── cc#338: Sector Cards regrouped by curated futures_universe.theme (22 Scorr themes) ─────────
# DISPLAY ONLY. Companion to cc#337. Equal-weight simple average of constituent day_1d/week/month
# from v8_metrics — a breadth signal for the 1-lot-per-name futures book (NOT mcap-weighted, which
# would let RELIANCE dominate Energy). This is a NEW, forked read path: it does NOT touch the GVM
# segment peer-averages (v8_metrics.sector_week/day/month) the locked basket filters are calibrated
# to — the engine still runs on GVM segments, completely unaffected. Indices (NIFTY/BANKNIFTY, seg
# 'F&O', theme NULL) are excluded; any OTHER active null-theme symbol is a genuine new F&O entrant
# and is surfaced under a "NEW ENTRANTS" card (never silently dropped) until Claude-web themes it.
_INDEX_EXCLUDE_SQL = "fu.symbol NOT LIKE '%%NIFTY%%' AND fu.symbol NOT LIKE '%%SENSEX%%'"

@router.get("/theme_sectors")
def v8_theme_sectors():
    """One card per curated Scorr theme across the active futures universe: equal-weight avg
    1D/1W/1M + stock count, sorted 1D% desc. Same fields the frontend sectorCards() consumes."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(f"""
                SELECT COALESCE(fu.theme, 'NEW ENTRANTS') AS theme,
                       COUNT(*) FILTER (WHERE m.day_1d IS NOT NULL)       AS n,
                       AVG(m.day_1d)       FILTER (WHERE m.day_1d IS NOT NULL),
                       AVG(m.week_return)  FILTER (WHERE m.week_return IS NOT NULL),
                       AVG(m.month_return) FILTER (WHERE m.month_return IS NOT NULL)
                FROM futures_universe fu
                LEFT JOIN v8_metrics m ON m.symbol = fu.symbol
                     AND m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
                WHERE fu.is_active = TRUE AND {_INDEX_EXCLUDE_SQL}
                GROUP BY COALESCE(fu.theme, 'NEW ENTRANTS')
                HAVING COUNT(*) FILTER (WHERE m.day_1d IS NOT NULL) > 0
                ORDER BY AVG(m.day_1d) FILTER (WHERE m.day_1d IS NOT NULL) DESC NULLS LAST
            """)
            sectors = []
            for theme, n, d1, wk, mo in cur.fetchall():
                sectors.append({
                    "segment": theme, "count": int(n),
                    "avg_day_1d": round(float(d1), 2) if d1 is not None else None,
                    "sector_week": round(float(wk), 2) if wk is not None else None,
                    "sector_month": round(float(mo), 2) if mo is not None else None,
                })
        return {"sectors": sectors, "count": len(sectors)}
    except Exception as e:
        raise HTTPException(500, f"v8_theme_sectors failed: {e}")


@router.get("/theme_sectors/{theme}/holdings")
def v8_theme_sector_holdings(theme: str):
    """The active futures-universe stocks in one Scorr theme, each with gvm_score + 1D/1W/1M,
    sorted 1D% desc. 'NEW ENTRANTS' resolves to active null-theme non-index symbols."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            if theme == "NEW ENTRANTS":
                theme_pred = "fu.theme IS NULL"
                params = ()
            else:
                theme_pred = "fu.theme = %s"
                params = (theme,)
            cur.execute(f"""
                SELECT fu.symbol, m.gvm_score, m.day_1d, m.week_return, m.month_return
                FROM futures_universe fu
                LEFT JOIN v8_metrics m ON m.symbol = fu.symbol
                     AND m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
                WHERE fu.is_active = TRUE AND {_INDEX_EXCLUDE_SQL} AND {theme_pred}
                ORDER BY m.day_1d DESC NULLS LAST
            """, params)
            holdings = []
            for sym, gvm, d1, wk, mo in cur.fetchall():
                holdings.append({
                    "symbol": sym,
                    "gvm_score": round(float(gvm), 2) if gvm is not None else None,
                    "day_1d": round(float(d1), 2) if d1 is not None else None,
                    "week_return": round(float(wk), 2) if wk is not None else None,
                    "month_return": round(float(mo), 2) if mo is not None else None,
                })
        return {"theme": theme, "holdings": holdings, "count": len(holdings)}
    except Exception as e:
        raise HTTPException(500, f"v8_theme_sector_holdings failed: {e}")
