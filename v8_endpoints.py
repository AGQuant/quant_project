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
        # V4 LOCKED 16-Jun-2026. 5 strict AND in Neutral/Bear.
        # Mood relaxation 18-Jun-2026: 4/5 in Strong Bullish + Bullish.
        "rsi_weekly":   [None, 45.0],
        "mom_2d":       [None, -3.0],
        "sector_week":  [None, -1.5],
        "dma_200":      [None,  2.0],
        "week_return":  [-10.0, -0.5],
    },
    "sell_momentum": {
        # V2 LOCKED 16-Jun-2026. 6 strict AND in Neutral/Bear.
        # Mood relaxation 18-Jun-2026: 5/6 in Strong Bullish + Bullish.
        "dma_200":       [None,  -2.0],
        "rsi_month":     [None,  38.0],
        "rsi_weekly":    [None,  38.0],
        "week_index_52": [None,  20.0],
        "sector_week":   [None,  -2.0],
        "mom_2d":        [None,  -1.5],
    },
    # sell_overbought: 2 pivot-based filters computed live. 3 RSI/sector here for reference.
    "sell_overbought": {
        "rsi_weekly":   [80.0, None],
        "rsi_month":    [70.0, None],
        "sector_week":  [None,  0.0],
    },
    # buy_s1_bounce: 7 filters (1 gate + 6 stages). Reference cols only.
    "buy_s1_bounce": {
        "week_return":  [0.0,  2.5],   # cc#358 V2: cap 2.5 (was 3.0)
        "vol_ratio":    [1.5, None],
        "dma_50":       [0.0, None],
    },
}

SELL_REVERSAL_SL_MULT  = 0.5
SELL_MOMENTUM_SL_MULT  = 0.5

BASKET_META = {
    "buy_reversal":    {"side": "BUY",  "target": "R1",                        "win_pct": "62-64%", "signals_per_day": "~35-55/yr"},
    "buy_momentum":    {"side": "BUY",  "target": "+3.0% fixed",               "win_pct": "67% live", "signals_per_day": "~2/day"},
    "sell_reversal":   {"side": "SELL", "target": "S2",                        "win_pct": "79.3%", "signals_per_day": "~0.6/day"},
    "sell_momentum":   {"side": "SELL", "target": "S2",                        "win_pct": "71.9%", "signals_per_day": "~0.4/day"},
    "sell_overbought": {"side": "SELL", "target": "S1",                        "win_pct": "81.5%", "signals_per_day": "~0.4/day"},
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
    "buy_reversal":    {"hourly_pct": {"min": 0.2, "max": 1.0},
                        "week_index_52": {"min": 40.0, "max": 80.0}},
    "buy_momentum":    {"hourly_pct": {"min": 0.2, "max": 1.5},
                        "week_index_52": {"min": 60.0, "max": 100.0}},
    "buy_s1_bounce":   {"hourly_pct": {"min": 0.0, "min_excl": True, "max": 1.0},
                        "week_index_52": {"min": 50.0, "max": 90.0}},
    "sell_reversal":   {"hourly_pct": {"max": 0.0, "max_excl": True},
                        "week_index_52": {"min": 20.0, "max": 60.0}},
    "sell_momentum":   {"hourly_pct": {"max": 0.0, "max_excl": True},
                        "week_index_52_modify": {"max": 30.0}},
    "sell_overbought": {"fall_from_day_high": {"max": -1.5}},
}

# Locked-spec WR baselines (per specs 1263-1268) for the WR kill-switch.
V21_BASELINE_WR = {
    "buy_reversal": 63.0, "buy_momentum": 67.0, "buy_s1_bounce": 73.9,  # cc#354/359 V2/V3 honest baselines
    "sell_reversal": 79.3, "sell_momentum": 71.9, "sell_overbought": 81.5,
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
    cur.execute("SELECT advances, declines, unchanged, adr, price_date FROM adr_daily ORDER BY price_date DESC LIMIT 1")
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
            "gate_note": "Strict AND in Neutral/Bear (5/5). 1 miss allowed (4/5) in Strong Bullish + Bullish (18-Jun-2026).",
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
            "gate_note": "Strict AND in Neutral/Bear (6/6). 1 miss allowed (5/6) in Strong Bullish + Bullish (18-Jun-2026).",
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
            "principle": "Bounce from pivot S1 support -- 7 filters (1 gate + 6 stages)",
            "filters": [
                {"metric": "nifty_rsi (market gate)", "condition": ">= 55"},
                {"metric": "week_return",             "condition": "0% to 3%"},
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
            extra = {"target": "S2", "target_formula": "S2 = PP - (H5 - L5)",
                     "stop_formula": f"PP + {SELL_REVERSAL_SL_MULT}*(R1-PP)", "sl_mult": SELL_REVERSAL_SL_MULT}
        elif basket == "sell_momentum":
            extra = {"target": "S2", "target_formula": "S2 = PP - (H5 - L5)",
                     "stop_formula": f"PP + {SELL_MOMENTUM_SL_MULT}*(R1-PP)", "sl_mult": SELL_MOMENTUM_SL_MULT}
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
    cur.execute("""SELECT symbol, pp, r1, s1 FROM v8_paper_pivots
        WHERE pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)
          AND pp IS NOT NULL AND r1 IS NOT NULL""")
    return {r[0]: {"pp": float(r[1]), "r1": float(r[2]), "s1": float(r[3])} for r in cur.fetchall()}


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
    """cc#357: 8-stage cumulative funnel for buy_reversal V3, reshaped from the handler-written
    v8_funnel_counts row (score_date=today). Empty stages (all 0) until the first live tick writes."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT counts FROM v8_funnel_counts WHERE basket='buy_reversal' "
                        "AND score_date=CURRENT_DATE ORDER BY computed_at DESC LIMIT 1")
            row = cur.fetchone()
        counts   = (row[0] if row and isinstance(row[0], dict) else {}) or {}
        universe = int(counts.get("_universe", 0) or 0)
        stages, prev = [], universe
        for key, label, cmin, cmax in _BR_V3_STAGES:
            surv = counts.get(key)
            surv = prev if surv is None else int(surv)   # missing -> no kill (defensive)
            stages.append({"metric": label, "condition_min": cmin, "condition_max": cmax,
                           "survivors": surv, "killed": max(prev - surv, 0)})
            prev = surv
        final = int(counts.get("_score_qualified", prev) or 0)
        return {
            "basket": "buy_reversal", "score_date": str(date.today()),
            "universe": universe, "final": final,
            "filter_count": 8, "n_filters": 8,
            "gate_type": "strict AND (all 8 must pass)",
            "score_qualified": final, "pivot_pass": final,
            "stages": stages, **BASKET_META.get("buy_reversal", {}),
        }
    except Exception as e:
        raise HTTPException(500, f"br_funnel_detail failed: {e}")


@router.get("/funnel_detail/{basket}")
def funnel_detail(basket: str):
    basket = basket.lower()
    if basket == "buy_s1_bounce":   return s1b_funnel_detail()
    if basket == "sell_overbought": return so_funnel_detail()
    if basket == "buy_reversal":    return br_funnel_detail()
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


def _so_funnel_stages():
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(_SO_COMMON_SQL)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        raise HTTPException(500, f"_so_funnel_stages DB error: {e}")
    rows = _so_enrich(rows)
    total = len(rows)
    survivors_list = rows[:]
    prev = total
    stages = []

    def _stage(label, cond, cmin, cmax):
        nonlocal survivors_list, prev
        survivors_list = [s for s in survivors_list if cond(s)]
        n = len(survivors_list)
        stages.append({"metric": label, "condition_min": cmin, "condition_max": cmax,
                       "survivors": n, "killed": prev - n})
        prev = n

    _stage("week_high_vs_pivot", lambda s: s.get("week_high_vs_pivot") is True, "hi5d", "> 0.9*R1/R2")
    _stage("fall_3d", lambda s: s.get("fall_3d") is not None and float(s["fall_3d"]) < -3.0, "< -3%", "-")
    _stage("rsi_weekly", lambda s: _passes_filter(s.get("rsi_weekly"), 80.0, None), ">= 80", "-")
    _stage("rsi_month",  lambda s: _passes_filter(s.get("rsi_month"), 70.0, None),  ">= 70", "-")
    _stage("sector_week", lambda s: s.get("sector_week") is not None and float(s["sector_week"]) < 0, "< 0", "-")

    # cc#164: V2.1 hard-gate stage (fall_from_day_high <= -1.5), chained onto the
    # survivors of the 5 locked filters above -- this dedicated funnel has no
    # separate v8_qualified read, so "final" below must include this gate to
    # accurately reflect what the live engine (v8_signal_writer.py) qualifies.
    v21_enabled = False
    try:
        with _conn() as conn:
            v21_enabled = _load_filter_state(conn).get("sell_overbought", False)
            v21_metrics = _load_v21_live_metrics(conn, [r["symbol"] for r in rows])
    except Exception:
        v21_metrics = {}
    for r in rows:
        r["fall_from_day_high"] = v21_metrics.get(r["symbol"], {}).get("fall_from_day_high")
    n_before = len(survivors_list)
    survivors_list = [s for s in survivors_list
                      if v21_hard_gate_pass("sell_overbought", s, v21_enabled)]
    stages.append({
        "metric": "v2.1_hard_gate", "condition_min": "fall_from_day_high", "condition_max": "<= -1.5",
        "survivors": len(survivors_list), "killed": n_before - len(survivors_list),
        "v21_enabled": v21_enabled,
        "v21_note": "V2.1 hard gate (fall_from_day_high)" if v21_enabled
                    else "V2.1 hard gate DISABLED (locked behavior in effect)",
    })
    return stages, total


def so_funnel_detail():
    try:
        stages, total = _so_funnel_stages()
        final = stages[-1]["survivors"] if stages else 0
        v21_enabled = stages[-1].get("v21_enabled", False) if stages else False
        return {
            "basket": "sell_overbought", "score_date": str(date.today()),
            "universe": total, "final": final, "filter_count": 5, "n_filters": 5,
            "gate_type": "strict AND (all must pass)",
            "score_qualified": final, "pivot_pass": final, "stages": stages,
            "v21_enabled": v21_enabled,
            **BASKET_META.get("sell_overbought", {})
        }
    except Exception as e:
        raise HTTPException(500, f"so_funnel_detail failed: {e}")


def so_funnel_counts():
    try:
        stages, total = _so_funnel_stages()
        counts = {st["metric"]: st["survivors"] for st in stages}
        counts["_final_qualified"] = stages[-1]["survivors"] if stages else 0
        counts["_v21_enabled"] = stages[-1].get("v21_enabled", False) if stages else False
        return {"basket": "sell_overbought", "score_date": str(date.today()),
                "counts": counts, "source": "live_5filter"}
    except Exception as e:
        raise HTTPException(500, f"so_funnel_counts failed: {e}")


def so_stock_passcount():
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(_SO_COMMON_SQL)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            v21_enabled = _load_filter_state(conn).get("sell_overbought", False)
            v21_metrics = _load_v21_live_metrics(conn, [r["symbol"] for r in rows])
        rows = _so_enrich(rows)
        out = []
        for r in rows:
            r["fall_from_day_high"] = v21_metrics.get(r["symbol"], {}).get("fall_from_day_high")
            checks = {
                "week_high_vs_pivot": r.get("week_high_vs_pivot") is True,
                "fall_3d":     r.get("fall_3d") is not None and float(r["fall_3d"]) < -3.0,
                "rsi_weekly":  _passes_filter(r.get("rsi_weekly"), 80.0, None),
                "rsi_month":   _passes_filter(r.get("rsi_month"), 70.0, None),
                "sector_week": r.get("sector_week") is not None and float(r["sector_week"]) < 0,
            }
            passed = [k for k, ok in checks.items() if ok]
            failed = [k for k, ok in checks.items() if not ok]
            v21_pass = v21_hard_gate_pass("sell_overbought", r, v21_enabled)
            out.append({"symbol": r["symbol"], "passed": len(passed), "total": 5,
                        "passed_filters": passed, "failed_filters": failed,
                        "gvm_score": r.get("gvm_score"),
                        "fall_3d": round(float(r["fall_3d"]), 2) if r.get("fall_3d") is not None else None,
                        "v21_pass": v21_pass})
        out.sort(key=lambda x: (x["passed"], x["gvm_score"] if x["gvm_score"] is not None else -1), reverse=True)
        return {"basket": "sell_overbought", "score_date": str(date.today()),
                "universe": len(out), "filter_count": 5, "stocks": out,
                "v21_enabled": v21_enabled,
                **BASKET_META.get("sell_overbought", {})}
    except Exception as e:
        raise HTTPException(500, f"so_stock_passcount failed: {e}")


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
    _stage("week_return", lambda s: _passes_filter(s.get("week_return"), 0.0, 3.0), ">= 0%", "<= 3%")
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
                "week_return":    _passes_filter(r.get("week_return"), 0.0, 3.0),
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
        with _conn() as conn, conn.cursor() as cur:      # cc#240: inject prior-day OPEN positions
            rows = _inject_open_positions(cur, rows, "sell_overbought", open_pos)
        rows = _enrich_with_status(rows, "sell_overbought", open_pos, slot_full,
                                   closed_today, conflict_syms, missed)   # cc#326
        return {"basket": "sell_overbought", "count": len(rows),
                "target": "S1", "stop": "R2",
                "slot_architecture": "Dedicated ring-fenced: 4 (Bull/Neutral) / 3 (Bearish)",
                "win_pct": "81.5%", "ev_per_trade": "+1.56%", "stocks": rows}
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
            cur.execute("""
                SELECT symbol, name, category, price, prev_close, chg_pct, quote_date
                FROM global_indices
                WHERE quote_date = (SELECT MAX(quote_date) FROM global_indices)
                ORDER BY category, symbol
            """)
            cols = [d[0] for d in cur.description]
            base = [dict(zip(cols, r)) for r in cur.fetchall()]
            # cc#281: latest live intraday close per symbol (today), if present.
            cur.execute("""
                SELECT DISTINCT ON (symbol) symbol, close, ts
                FROM global_intraday
                WHERE ts::date = CURRENT_DATE
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
        return {"quote_date": qd, "data_ts": (str(latest_ts) if latest_ts else None),
                "instruments": rows, "count": len(rows)}
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
