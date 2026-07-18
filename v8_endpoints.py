"""
V8 endpoints -- Quant Long-Short Basket Strategy

ADR (14-Jun-2026): _read_adr gates the live tiers behind _market_open().
ADR (11-Jun-2026): market_mood reads adr_intraday primary, falls back to adr_daily.

cc#502 V8 SUITE REBUILD (18-Jul-2026): sell_overbought and buy_s1_bounce RETIRED entirely
(handlers, ring-fenced slot pools, and all dedicated display-layer functions removed). The suite
is now exactly FOUR dedicated strict-AND handlers in v8_signal_writer.py -- the generic
FILTER_CONFIG score-gate loop is retired (kept dormant/unreachable in a couple of fallback code
paths only). Each handler's FINAL heavy stage is the shared true_weekly_rsi() (wRSI) -- NEVER the
synthetic v8_metrics.rsi_weekly column (cc#353: ~16pt off).
  buy_reversal   BUY_REVERSAL_V5   (_write_buy_reversal_v5_qualified):   7 filters -- S1-touch
    (prior-4d raw_prices low OR today's live day_low <= S1), mom_2d>=-0.5, week_return>=-2,
    rsi_month[60,90], sector_week>0, month_return<5, FINAL wRSI>=70. Fixed +/-3.0% exits.
  buy_momentum   BUY_MOMENTUM_V3   (_write_buy_momentum_v3_qualified): TWO independent layers --
    6 HARD gates (dma_50[5,12], dma_20>0, week_index_52>=75, gvm_score>=7, day_1d>0,
    hourly_pct>0&NOT NULL) + FINAL heavy wRSI[70,85], PLUS SCORE>=7-of-10 fixed-threshold V2 bands
    (gvm/dma_50/dma_200/rsi_month/week_return/month_return/mom_2d/sector_week/sector_month + wRSI
    reused at [60,85]). Fixed +/-3.0% exits.
  sell_reversal  SELL_REVERSAL_V6.1 (_write_sell_reversal_v61_qualified): 10 filters -- R1-touch
    (last 3 days), day_1d[-2,0], dma_20/50/200<0, week_index_52<50, sector_week<0, mom_2d[-4,-1],
    month_return>=-10, room-to-S1/S2 dynamic target, FINAL wRSI<=45. Dynamic S1/S2 target, 1:1
    mirror stop.
  sell_momentum  SELL_MOMENTUM_V4  (_write_sell_momentum_v4_qualified): 9 filters -- rsi_month<40,
    mom_2d[-4,-2], dma_200<=2, week_return[-10,-0.5], sector_week<0, week_index_52[20,60],
    CMP<PP, S2-clearance>=3%, FINAL wRSI<=40. Dynamic S2 target, 1:1 mirror stop.
Generic funnel_detail stages emit survivors/killed (dashboard aliases for passes/fails).
Slot architecture (cc#502, 18-Jul-2026) SLOT_ARCHITECTURE_V3.0.0: ring-fenced SO/S1B pools removed,
standard pool only, 20 total slots:
  Strong Bullish 15B/5S | Bullish 14B/6S | Neutral 12B/8S | Bearish 8B/13S
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
    # cc#502 V8 SUITE REBUILD (18-Jul-2026): sell_overbought + buy_s1_bounce entries REMOVED
    # (both baskets retired entirely). All four remaining baskets are dedicated strict-AND
    # handlers in v8_signal_writer.py -- this dict is DISPLAY/ENDPOINT REFERENCE ONLY (the
    # generic score-gate loop that used to read it live is retired). Each entry carries the
    # v8_metrics-computable subset of that basket's filters; live/pivot/price-history gates and
    # the heavy true_weekly_rsi (wRSI) FINAL stage are handler-enforced only and noted in comments
    # -- wRSI is NEVER the shared synthetic v8_metrics.rsi_weekly (cc#353: ~16pt off), always the
    # basket-call-local _true_weekly_rsi().
    "buy_reversal": {
        # BUY_REVERSAL_V5 (replaces the V3 inverse-sandwich). Live/pivot gates NOT shown here:
        # S1-touch (prior-4-day raw_prices low OR today's live day_low <= S1) and the FINAL heavy
        # stage true_weekly_rsi>=70 -- both handler-enforced in _write_buy_reversal_v5_qualified.
        "mom_2d":       [-0.5, None],
        "week_return":  [-2.0, None],
        "rsi_month":    [60.0, 90.0],
        "sector_week":  [0.0,  None],   # engine enforces STRICT >0
        "month_return": [None, 5.0],
    },
    "buy_momentum": {
        # BUY_MOMENTUM_V3: TWO independent layers, both handler-enforced. The 9 bands below are
        # the SCORE layer (SCORE>=7-of-10 fixed threshold, no mood-dependent n/n-1; the 10th band
        # is wRSI 60-85 via true_weekly_rsi, not a stored column). A SEPARATE, stricter HARD-gate
        # layer (all strict-AND, not shown here) also applies: dma_50 in [5,12] (tighter than the
        # score band below), dma_20>0, week_index_52>=75, gvm_score>=7, day_1d>0, hourly_pct>0 AND
        # NOT NULL (blocks entries before ~10:15), FINAL heavy true_weekly_rsi in [70,85].
        "gvm_score":    [7.0,  10.0],
        "dma_50":       [8.0,  25.0],
        "dma_200":      [8.0,  40.0],
        "rsi_month":    [70.0, 100.0],
        "month_return": [2.0,  30.0],
        "week_return":  [0.5,  12.0],
        "mom_2d":       [0.0,   6.0],
        "sector_week":  [0.0,   6.0],
        "sector_month": [0.0,   6.0],
    },
    "sell_reversal": {
        # SELL_REVERSAL_V6.1 (replaces V5-D). Live/pivot gates NOT shown here: R1-touch last 3
        # days (per-day high vs that day's r1) and the FINAL heavy stage true_weekly_rsi<=45 --
        # both handler-enforced in _write_sell_reversal_v61_qualified. RAW: no market gate, no
        # kill-switch. Target S1-or-S2 dynamic (room>=2%), stop 1:1 mirror (handler-computed).
        "day_1d":        [-2.0,  0.0],
        "dma_20":        [None,  0.0],
        "dma_50":        [None,  0.0],
        "dma_200":       [None,  0.0],
        "week_index_52": [None, 50.0],
        "sector_week":   [None,  0.0],   # engine enforces STRICT <0
        "mom_2d":        [-4.0, -1.0],
        "month_return":  [-10.0, None],
    },
    "sell_momentum": {
        # SELL_MOMENTUM_V4 (renamed from V3): twr tightened <=45 -> <=40, mom_2d tightened
        # [-4,-1] -> [-4,-2] (both cc#502). Live/pivot gates NOT shown here: CMP<PP,
        # S2-clearance>=3%, FINAL heavy true_weekly_rsi<=40 -- handler-enforced in
        # _write_sell_momentum_v4_qualified.
        "rsi_month":     [None, 40.0],    # weak monthly (engine enforces STRICT <40)
        "mom_2d":        [-4.0, -2.0],    # recent down-momentum
        "dma_200":       [None,  2.0],    # below / near 200-DMA
        "week_return":   [-10.0, -0.5],   # weak week
        "sector_week":   [None,  0.0],    # weak sector (engine enforces STRICT <0)
        "week_index_52": [20.0,  60.0],   # mid 52-week band
    },
}

# cc#378/380: SELL_REVERSAL_SL_MULT + SELL_MOMENTUM_SL_MULT retired — V5-D uses S1/S2 mirror, V3-N5 fixed +/-3%.

# cc#502 SUITE REBUILD (18-Jul-2026): sell_overbought + buy_s1_bounce entries removed (baskets
# retired). Remaining four are the new dedicated-handler specs (V5/V3/V6.1/V4) -- win_pct/
# signals_per_day for the rebuilt specs are TBD pending live/BT7 evidence, left honest rather than
# carrying forward the old (now-wrong) V3/V5-D/V3-N5 backtest numbers.
BASKET_META = {
    "buy_reversal":    {"side": "BUY",  "target": "+3.0% fixed",   "win_pct": "TBD (cc#502 rebuild)", "signals_per_day": "TBD"},
    "buy_momentum":    {"side": "BUY",  "target": "+3.0% fixed",   "win_pct": "TBD (cc#502 rebuild)", "signals_per_day": "TBD"},
    "sell_reversal":   {"side": "SELL", "target": "S1/S2 dynamic", "win_pct": "TBD (cc#502 rebuild)", "signals_per_day": "TBD"},
    "sell_momentum":   {"side": "SELL", "target": "-3.0% fixed",   "win_pct": "TBD (cc#502 rebuild)", "signals_per_day": "TBD"},
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
    # cc#360: buy_reversal V2.1 bands removed — buy_reversal is now a dedicated strict-AND
    # handler (V3, then V5 per cc#502) that never calls the V2.1 hard gate. Absence is handled
    # gracefully by every reader (all use .get(basket,{}) or `basket in V21_FILTERS`), so no
    # v21_pass is computed for it.
    # cc#502: sell_overbought + buy_s1_bounce entries removed (baskets retired entirely).
    # buy_momentum/sell_momentum entries below are ORPHANED as of cc#502 (both are now dedicated
    # strict-AND handlers that never call v21_hard_gate_pass) but left in place -- funnel_counts()
    # still reads them for an informational v21_pass comparison column, and every reader is
    # membership-guarded so their presence is harmless.
    "buy_momentum":    {"hourly_pct": {"min": 0.2, "max": 1.5},
                        "week_index_52": {"min": 60.0, "max": 100.0}},
    # cc#378: sell_reversal V2.1 bands removed — sell_reversal is now a dedicated strict-AND
    # handler (V5-D, then V6.1 per cc#502, RAW: no market gate, no kill-switch, never calls the
    # V2.1 hard gate). Absence is handled by every reader (.get(basket,{}) / `basket in
    # V21_FILTERS`), so no v21_pass for it.
    "sell_momentum":   {"hourly_pct": {"max": 0.0, "max_excl": True},
                        "week_index_52_modify": {"max": 30.0}},
}

# Locked-spec WR baselines (per specs 1263-1268) for the WR kill-switch.
# cc#502: sell_overbought + buy_s1_bounce removed (baskets retired). buy_reversal/buy_momentum/
# sell_reversal/sell_momentum baselines are STALE (pre-rebuild V3/V3/V5-D/V3-N5 numbers) -- kept
# only because the WR kill-switch reads this dict; TBD pending fresh cc#502 live/BT7 evidence.
V21_BASELINE_WR = {
    "buy_reversal": 63.0, "buy_momentum": 67.0,
    "sell_reversal": 73.0, "sell_momentum": 64.0,
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
    # cc#502 V5: no Nifty-regime overrides — buy_reversal uses fixed absolute gates.
    _, nifty_1m = _get_nifty_regime()
    return dict(FILTER_CONFIG["buy_reversal"]), "V5", nifty_1m


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


def _last_session_sql(source: str = "fyers_eq", timeframe: str = "5m") -> str:
    """cc#446 fix_3: the ONE shared 'as-of last tick' anchor. Returns a SQL scalar subquery that
    yields the most recent trading-session date present in intraday_prices for the given feed.
    Every read surface that must FREEZE at the last available session off-market (weekend / holiday
    / after 15:30 -> last session's 15:30 final, per the cc#424 convention) routes through this, so
    the class of "blank/zero field off-market" bug dies by default and every FUTURE field inherits
    freeze-at-last-tick for free. During live hours MAX(ts::date) is today, so the same expression
    also resolves the live session — one anchor, both regimes. `source`/`timeframe` are internal
    constants (never user input), so string interpolation here is safe.
    Usage: cur.execute(f"... WHERE ts::date = {_last_session_sql()} ...", params)."""
    return (f"(SELECT MAX(ts::date) FROM intraday_prices "
            f"WHERE source='{source}' AND timeframe='{timeframe}')")


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
        # cc#508: NEAR_MISS branch removed -- /qualified/{basket} (this function's only caller)
        # never feeds it a NEAR_MISS row anymore (_live_qualified_fallback unwired + deleted).
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
        row["entry"]     = row.get("cmp")          # dashboard renderer keys on 'entry'
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
            # cc#502 SUITE REBUILD: ring-fenced SO/S1B pools removed -- standard pool only,
            # 20 total slots. so_slots/s1b_slots/so_pool/s1b_pool retired with the baskets.
            total_slots = buy_slots + sell_slots
            return {
                "checked_at": str(date.today()), "checks": checks,
                "fails": fails, "mood": mood,
                "buy_slots": buy_slots, "sell_slots": sell_slots, "total_slots": total_slots,
                "slot_note": "standard pool only (cc#502) -- no ring-fenced pools",
                "breadth_source": breadth_source, "nifty_source": nifty_source,
                "adr_detail": {"advances": advances, "declines": declines,
                               "unchanged": unchanged, "adr_date": adr_date,
                               "source": breadth_source},
            }
    except Exception as e:
        raise HTTPException(500, f"market_mood failed: {e}")


def _wilder_rsi(closes, period=14):
    """Wilder RSI on a close series (engine-identical to tc_v4 _rsi). None if < period+1 points."""
    closes = [c for c in closes if c is not None]
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


_TWR_CACHE = {"ts": 0.0, "data": None}

def _true_weekly_rsi_all(cur, ttl=300):
    """cc#419: TRUE calendar-weekly RSI-14 per symbol (last close of each ISO week -> Wilder RSI),
    matching the engine's buy_reversal/sell gate. Cached 5 min (weekly RSI barely moves intraday)."""
    now = time.time()
    if _TWR_CACHE["data"] is not None and (now - _TWR_CACHE["ts"]) < ttl:
        return _TWR_CACHE["data"]
    cur.execute("""
        SELECT symbol, close FROM (
            SELECT symbol, close, price_date,
                ROW_NUMBER() OVER (PARTITION BY symbol, EXTRACT(ISOYEAR FROM price_date), EXTRACT(WEEK FROM price_date)
                                   ORDER BY price_date DESC) AS rn
            FROM raw_prices WHERE price_date >= CURRENT_DATE - INTERVAL '400 days'
        ) x WHERE rn = 1 ORDER BY symbol, price_date
    """)
    series = {}
    for sym, close in cur.fetchall():
        if close is not None:
            series.setdefault(sym, []).append(float(close))
    out = {sym: _wilder_rsi(cl) for sym, cl in series.items()}
    _TWR_CACHE["ts"] = now
    _TWR_CACHE["data"] = out
    return out


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
            "_cmp": cmpv, "_day_high": dh,   # cc#419: for vs-PP / ROOM / R1-touch off-market
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
        # cc#235: recovery_2d / day_ret / week_low_pct — originally S1B/SO filter inputs (both
        # retired cc#502); kept as general Raw Data / Master-tab display columns. Single-pass CTE
        # + formulas copied from the ENGINE (_load_intraday_bars: fyers_eq pinned per cc#140,
        # array_agg first/last; writer recovery/day_ret/week_low math).
        cur.execute("""
            WITH td AS (
                SELECT symbol,
                    (array_agg(open  ORDER BY ts ASC ))[1] AS day_open,
                    (array_agg(close ORDER BY ts DESC))[1] AS live_close,
                    MIN(low) AS today_low, MAX(high) AS day_high
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
            SELECT td.symbol, td.day_open, td.live_close, td.today_low, h.lo_2d, h.lo_5d, td.day_high
            FROM td LEFT JOIN hist h ON h.symbol = td.symbol
        """)
        _live_bars = {r[0]: r for r in cur.fetchall()}

    def _f(v):
        try: return float(v) if v is not None else None
        except (TypeError, ValueError): return None
    for s in rows:
        d = _live_bars.get(s["symbol"])
        # SELECT order: symbol[0], day_open[1], live_close[2], today_low[3], lo_2d[4], lo_5d[5], day_high[6]
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
        s["_cmp"] = cmpv                         # cc#419: CMP + day-high for vs-PP / ROOM / R1-touch
        s["_day_high"] = _f(d[6]) if d else None

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
            if s.get("_cmp") is None: s["_cmp"] = fb.get("_cmp")
            if s.get("_day_high") is None: s["_day_high"] = fb.get("_day_high")

    # cc#419: surface 5 more basket-filter fields — RSI(D) (daily_rsi, already selected), tRSI(W)
    # (true calendar weekly, engine formula), vs-PP%, ROOM% (side-aware), R1-touch flag. Pivots from
    # v8_paper_pivots; CMP/day-high from the same as-of source as the intraday cols above.
    with _conn() as _c3, _c3.cursor() as _cur3:
        _cur3.execute("""SELECT DISTINCT ON (symbol) symbol, pp, r1, s1, r2, s2
                         FROM v8_paper_pivots ORDER BY symbol, pivot_date DESC""")
        _piv = {r[0]: {"pp": _f(r[1]), "r1": _f(r[2]), "s1": _f(r[3]), "r2": _f(r[4]), "s2": _f(r[5])} for r in _cur3.fetchall()}
        _twr = _true_weekly_rsi_all(_cur3)
    for s in rows:
        s["true_weekly_rsi"] = _twr.get(s["symbol"])
        p = _piv.get(s["symbol"]) or {}
        cmpv, dh = s.get("_cmp"), s.get("_day_high")
        pp, r1, s1, s2 = p.get("pp"), p.get("r1"), p.get("s1"), p.get("s2")
        s["vs_pp"] = ((cmpv - pp) / pp * 100) if (cmpv and pp and pp > 0) else None
        # ROOM% — side-aware room to the nearest target (names the level used for the tooltip)
        room, lvl = None, None
        if cmpv and pp:
            if cmpv > pp:
                if r1 and r1 > 0: room, lvl = (r1 - cmpv) / cmpv * 100, ("R1", r1)
            else:
                # below PP -> room to S1; if already below S1, room to S2 (sell_rev/sell_mom semantics)
                if s1 and cmpv > s1 and s1 > 0: room, lvl = (cmpv - s1) / cmpv * 100, ("S1", s1)
                elif s2 and s2 > 0: room, lvl = (cmpv - s2) / cmpv * 100, ("S2", s2)
                elif s1 and s1 > 0: room, lvl = (cmpv - s1) / cmpv * 100, ("S1", s1)
        s["room_pct"] = room
        s["room_level"] = (f"{lvl[0]} {round(lvl[1],1)}" if lvl else None)
        s["r1_touch"] = bool(dh and r1 and dh >= r1)
        s.pop("_cmp", None); s.pop("_day_high", None)

    for s in rows:
        s["segment"] = _seg_override(s["symbol"], s.get("segment"))
        for k, v in list(s.items()):
            if k not in ("symbol", "segment", "verdict") and v is not None:   # cc#298: verdict stays a string
                try: s[k] = float(v)
                except (TypeError, ValueError): pass
    return rows


@router.get("/segment_day")
def segment_day():
    """cc#429: mcap-weighted DAY change per V8 futures segment (gvm_scores.segment taxonomy — same as
    SEC WK%/SEC MO% in Raw Data), derived from member v8_metrics.day_1d weighted by gvm_scores.market_cap.
    Anchored to MAX(score_date) so off-market it serves the last session's finals (cc#424 convention).
    Returns {segment: {day_pct, n, top_mover, top_day}} for the Open Positions 'Sector Day %' column."""
    # cc#432 fix_1: root cause of the "--" everywhere — the cc#429 version called _f() (a helper that
    # only exists as a NESTED function elsewhere, not at module scope) -> NameError -> 500 -> the
    # dashboard job cached null -> every row rendered "--". Use a local float converter instead.
    def _num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH mem AS (
                    SELECT g.segment, m.symbol, m.day_1d::numeric AS day_1d, g.market_cap::numeric AS mcap
                    FROM v8_metrics m
                    JOIN gvm_scores g ON g.symbol = m.symbol
                    WHERE m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
                      AND g.segment IS NOT NULL AND m.day_1d IS NOT NULL
                      AND g.market_cap IS NOT NULL AND g.market_cap > 0
                )
                SELECT segment,
                       ROUND(SUM(day_1d * mcap) / NULLIF(SUM(mcap), 0), 2) AS day_pct,
                       COUNT(*) AS n,
                       (array_agg(symbol ORDER BY day_1d DESC))[1] AS top_mover,
                       ROUND(MAX(day_1d), 2) AS top_day
                FROM mem GROUP BY segment
            """)
            out = {}
            for seg, day_pct, n, top_mover, top_day in cur.fetchall():
                out[seg] = {"day_pct": _num(day_pct), "n": int(n),
                            "top_mover": top_mover, "top_day": _num(top_day)}
        return {"segments": out}
    except Exception as e:
        raise HTTPException(500, f"segment_day failed: {e}")


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
        # cc#502 V5: no Nifty regime; 5 daily-metric gates shown here, 2 live/pivot gates
        # (S1-touch, FINAL heavy true_weekly_rsi>=70) enforced live in the dedicated handler.
        return {
            "basket": basket, "filters": rows, "count": len(rows),
            "regime": "V5", "nifty_1m_return": round(nifty_1m, 2),
            "live_gates": ["S1-touch (prior-4-day low OR today's live day_low <= S1)",
                           "true_weekly_rsi >= 70 (true calendar weekly, FINAL heavy stage)"],
            "entry_exit": "entry live CMP, target +3.0% fixed, stop -3.0% fixed (true 1:1)",
            "backtest": {"note": "TBD (cc#502 rebuild, pending live/BT7 audit)"},
            **BASKET_META.get(basket, {})
        }
    if basket == "buy_momentum":
        rows = []
        for metric, bounds in FILTER_CONFIG["buy_momentum"].items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            rows.append({"metric": metric, "min": mn, "max": mx,
                         "min_display": "" if mn is None else mn,
                         "max_display": "" if mx is None else mx,
                         "dynamic": False})
        return {
            "basket": basket, "filters": rows, "count": len(rows),
            "score_threshold": "score >= 7 of 10 (fixed, no mood-dependent n/n-1)",
            "target": "+3.0% fixed", "target_rule": "cc#502 V3: fixed +3.0% / -3.0% (1:1), frozen at entry",
            "stop": "-3.0% fixed",
            "hard_gates": ["dma_50 in [5,12]", "dma_20 > 0", "week_index_52 >= 75", "gvm_score >= 7",
                           "day_1d > 0", "hourly_pct > 0 AND NOT NULL (blocks entries before ~10:15)",
                           "FINAL heavy true_weekly_rsi in [70,85]"],
            "gate_note": "cc#502 V3: TWO independent layers -- 6 HARD gates + FINAL heavy wRSI, "
                         "PLUS SCORE>=7-of-10 (the 9 bands above + wRSI[60,85] reused as the 10th).",
            "backtest": {"note": "TBD (cc#502 rebuild, pending live/BT7 audit)"},
            **BASKET_META.get(basket, {})
        }
    if basket == "sell_reversal":
        # cc#502 SELL_REVERSAL_V6.1. 8 v8_metrics gates + 3 live/pivot gates.
        rows = []
        for metric, bounds in FILTER_CONFIG["sell_reversal"].items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            rows.append({"metric": metric, "min": mn, "max": mx,
                         "min_display": "" if mn is None else mn,
                         "max_display": "" if mx is None else mx})
        rows += [{"metric": "r1_touch",        "condition": "day session HIGH >= R1 in the last 3 days"},
                 {"metric": "room",             "condition": "target (S1 or S2) >= 2% from entry"},
                 {"metric": "true_weekly_rsi",  "condition": "<= 45 (TRUE calendar weekly, FINAL heavy stage)"}]
        return {
            "basket": basket, "filters": rows, "count": len(rows),
            "target": "S1/S2 dynamic",
            "target_formula": "S1 if (CMP-S1)/CMP >= 2% else S2 (never beyond S2); no signal if <2%",
            "stop": "1:1 mirror = entry + (entry - target)",
            "gate_note": "cc#502 V6.1: strict AND of 10, RAW -- no market gate, no auto kill-switch.",
            "backtest": {"note": "TBD (cc#502 rebuild, pending live/BT7 audit)"},
            **BASKET_META.get(basket, {})
        }
    if basket == "sell_momentum":
        # cc#502 SELL_MOMENTUM_V4 (renamed from V3-N5). 6 v8_metrics gates + 3 live/pivot gates.
        rows = []
        for metric, bounds in FILTER_CONFIG["sell_momentum"].items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            rows.append({"metric": metric, "min": mn, "max": mx,
                         "min_display": "" if mn is None else mn,
                         "max_display": "" if mx is None else mx})
        rows += [{"metric": "true_weekly_rsi", "condition": "<= 40 (TRUE calendar weekly, FINAL heavy stage)"},
                 {"metric": "cmp_lt_pp",        "condition": "CMP < PP"},
                 {"metric": "s2_clearance",     "condition": "(CMP-S2)/CMP >= 3%"}]
        return {
            "basket": basket, "filters": rows, "count": len(rows),
            "target": "-3.0% fixed", "target_formula": "entry * 0.97 (frozen at entry)",
            "stop": "+3.0% fixed = entry * 1.03 (true 1:1)",
            "gate_note": "cc#502 V4: strict AND of 9, dedicated handler; fixed +/-3% exits (true 1:1). "
                         "twr tightened 45->40, mom_2d tightened [-4,-1]->[-4,-2].",
            "backtest": {"note": "TBD (cc#502 rebuild, pending live/BT7 audit)"},
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
            # cc#508 (18-Jul-2026, founder-directed): NEAR_MISS score-gate fallback rows were
            # flooding the basket tabs (12+ near-miss rows drowning the actionable OPEN/SIGNAL
            # rows). Extends the cc#326 3-status doctrine (OPEN / SIGNAL only) -- zero
            # v8_qualified rows today means an honest empty list, not a near-miss watchlist.
            # _inject_open_positions (below, unconditional) still surfaces any OPEN position on
            # this basket even when nothing qualified today. _live_qualified_fallback /
            # _fallback_miss_reasons / _MISS_LABEL deleted outright (confirmed zero remaining
            # callers -- the STOCK PASS COUNT / funnel views compute their own independent
            # passed/failed-filter breakdowns and never called these).
            rows = []
            source_note = 'empty_no_qualified'
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
        # cc#517 Part D: F&O ban chip (display-only) -- the real entry-skip gate lives in
        # v8_signal_writer.py's _auto_paper_entry. Table-exists-safe (no-op before cc#517's first run).
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name='fo_ban'")
                if cur.fetchone():
                    cur.execute("SELECT symbol FROM fo_ban WHERE d=(SELECT MAX(d) FROM fo_ban)")
                    banned = {r[0] for r in cur.fetchall()}
                    for r in rows:
                        r["banned"] = r["symbol"] in banned
                else:
                    for r in rows:
                        r["banned"] = False
        except Exception:
            for r in rows:
                r["banned"] = False
        extra = {}
        if basket == "buy_momentum":
            # cc#502 BUY_MOMENTUM_V3: fixed +3.0%/-3.0% (1:1), frozen at entry -- no Nifty-regime
            # target anymore (was R2/R1-based display-only text; live entry already used R1/mirror
            # exits before this rebuild, so this is the first REAL exit-mechanism change too).
            extra = {"target": "+3.0% fixed", "target_formula": "entry * 1.03",
                     "stop_formula": "-3.0% fixed = entry * 0.97 (true 1:1)"}
        elif basket == "buy_reversal":
            # cc#502 BUY_REVERSAL_V5: fixed +3.0%/-3.0% (1:1), frozen at entry (replaces R1-target/mirror).
            extra = {"target": "+3.0% fixed", "target_formula": "entry * 1.03",
                     "stop_formula": "-3.0% fixed = entry * 0.97 (true 1:1)"}
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
def _latest_funnel_counts(cur, basket: str):
    """cc#424: as-of funnel-count row for a basket. Off-market there is no
    score_date=CURRENT_DATE row, so serve the most recent session's precomputed counts
    (last session, frozen-15:30 convention cc#417/418) instead of a blank UNIVERSE-0 funnel.
    Monday's first live tick writes today's row and it wins automatically.
    Returns (counts_dict, score_date|None)."""
    cur.execute("SELECT counts, score_date FROM v8_funnel_counts WHERE basket=%s "
                "ORDER BY score_date DESC, computed_at DESC LIMIT 1", (basket,))
    row = cur.fetchone()
    if not row:
        return {}, None
    counts = row[0] if isinstance(row[0], dict) else {}
    return (counts or {}), row[1]


def funnel_counts(basket: str):
    basket = basket.lower()
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
            # cc#424: anchor to the last session's counts off-market (was score_date=CURRENT_DATE
            # -> blank on weekends/holidays). Monday's first live tick overwrites today's row.
            _fc_counts, _fc_asof = _latest_funnel_counts(cur, basket)
        v21_pass = None
        if basket in V21_FILTERS:
            v21_pass = sum(1 for s in all_rows
                           if v21_hard_gate_pass(basket, {**s, **v21_metrics.get(s["symbol"], {})}, v21_enabled))
        # cc#502: all four baskets are dedicated-handler baskets now (writer precomputes every
        # tick) -- prefer the precomputed row whenever one exists; live_fallback below only fires
        # before the first live tick after a fresh deploy.
        if _fc_counts:
            counts = {**_fc_counts, "_v21_enabled": v21_enabled, "_v21_pass": v21_pass}
            return {"basket": basket, "score_date": str(_fc_asof or date.today()), "counts": counts, "source": "precomputed"}
        filters = FILTER_CONFIG[basket]; universe = all_rows[:]; counts = {}
        for metric, bounds in filters.items():
            mn, mx = bounds if isinstance(bounds, list) else (bounds[0], bounds[1])
            universe = [s for s in universe if _passes_filter(s.get(metric), mn, mx)]
            counts[metric] = len(universe)
        counts["_v21_enabled"] = v21_enabled
        counts["_v21_pass"] = v21_pass
        return {"basket": basket, "score_date": str(_fc_asof or date.today()), "counts": counts, "source": "live_fallback"}
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


def _basket_day_low_today(cur):
    """cc#502 BUY_REVERSAL_V5 S1-touch leg 2: today's live session low per symbol, straight from
    intraday_prices -- mirrors v8_signal_writer.py's bar['low'] source (fyers_eq, 5m)."""
    cur.execute("""SELECT symbol, MIN(low) FROM intraday_prices
        WHERE source='fyers_eq' AND timeframe='5m' AND ts::date=CURRENT_DATE
        GROUP BY symbol""")
    return {r[0]: float(r[1]) for r in cur.fetchall() if r[1] is not None}


def _basket_prior4_low(cur, symbols):
    """cc#502 BUY_REVERSAL_V5 S1-touch leg 1: MIN(prior-4-trading-day raw_prices low) per symbol,
    mirrors _write_buy_reversal_v5_qualified's ranked query exactly."""
    cur.execute("""
        WITH ranked AS (
            SELECT symbol, low, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
            FROM raw_prices WHERE symbol = ANY(%s) AND price_date < CURRENT_DATE
        )
        SELECT symbol, MIN(low) FROM ranked WHERE rn <= 4 GROUP BY symbol
    """, (symbols,))
    return {r[0]: float(r[1]) for r in cur.fetchall() if r[1] is not None}


def _basket_r1_touch_3d(cur, symbols):
    """cc#502 SELL_REVERSAL_V6.1 R1-touch: symbols where ANY of the last 3 trading days had that
    day's raw_prices HIGH >= that SAME day's v8_paper_pivots r1, mirrors
    _write_sell_reversal_v61_qualified's query exactly."""
    cur.execute("""
        WITH days AS (
            SELECT DISTINCT price_date AS d FROM raw_prices
            WHERE price_date < CURRENT_DATE ORDER BY d DESC LIMIT 3
        )
        SELECT DISTINCT rp.symbol
        FROM raw_prices rp
        JOIN v8_paper_pivots pv ON pv.symbol = rp.symbol AND pv.pivot_date = rp.price_date
        WHERE rp.price_date IN (SELECT d FROM days) AND rp.symbol = ANY(%s)
          AND pv.r1 IS NOT NULL AND rp.high >= pv.r1
    """, (symbols,))
    return {r[0] for r in cur.fetchall()}


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


# cc#502 BUY_REVERSAL_V5 (replaces V3 inverse-sandwich entirely): the funnel is CAPTURED by the
# writer (_write_buy_reversal_v5_qualified -> v8_funnel_counts) and reshaped here — never
# recomputed from FILTER_CONFIG. Static stage order + display labels (labels carry spaces so the
# dashboard renders them verbatim via metric.replace).
_BR_V5_STAGES = [
    ("s1_touch",        "S1 touch (prior-4d low or today's low)", "<= S1",   ""),
    ("mom_2d",          "mom 2d",                                 ">= -0.5", ""),
    ("week_return",     "week return",                            ">= -2",   ""),
    ("rsi_month",       "monthly RSI",                            ">= 60",   "<= 90"),
    ("sector_week",     "sector week",                            "> 0",     ""),
    ("month_return",    "month return",                           "",        "< 5"),
    ("true_weekly_rsi", "true weekly RSI",                        ">= 70",   ""),
]

def br_funnel_detail():
    """cc#502: 7-stage funnel for BUY_REVERSAL_V5, reshaped from the handler-written
    v8_funnel_counts row (score_date=today). INDEPENDENT per-filter pass counts across the
    universe (buy_momentum convention), NOT cumulative survivors — each of the 6 cheap gates is
    passes/fails vs the whole universe; true_weekly_rsi (stage 7) is passes vs the stocks that
    cleared all 6 cheap gates. Final still = strict-AND of all 7. Empty stages (all 0) until the
    first live tick writes."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            counts, _asof = _latest_funnel_counts(cur, "buy_reversal")   # cc#424: last-session as-of
        universe = int(counts.get("_universe", 0) or 0)
        stage6   = counts.get("_stage6_survivors")
        stage6   = int(stage6) if stage6 is not None else None
        final    = int(counts.get("_score_qualified", 0) or 0)
        stages = []
        for key, label, cmin, cmax in _BR_V5_STAGES:
            passes = int(counts.get(key, 0) or 0)
            # true_weekly_rsi (stage 7) is only evaluated on the 6-gate survivors -> its denominator
            # is that survivor count, not the full universe (the heavy read is skipped for the rest).
            denom = (stage6 if stage6 is not None else universe) if key == "true_weekly_rsi" else universe
            fails = max(denom - passes, 0)
            stage = {"metric": label, "key": key, "condition_min": cmin, "condition_max": cmax,
                     "passes": passes, "fails": fails,
                     "survivors": passes, "killed": fails,
                     "pass_pct": round(passes / denom * 100, 1) if denom else 0}
            if key == "true_weekly_rsi":
                stage["denominator"] = denom
                stage["note"] = f"of {denom} stocks passing all 6 cheap gates"
            stages.append(stage)
        return {
            "basket": "buy_reversal", "score_date": str(_asof or date.today()),
            "universe": universe, "final": final,
            "filter_count": 7, "n_filters": 7,
            "stage6_survivors": stage6,
            "gate_type": "independent per-filter counts; final = strict AND of all 7",
            "score_qualified": final, "pivot_pass": final,
            "stages": stages, **BASKET_META.get("buy_reversal", {}),
        }
    except Exception as e:
        raise HTTPException(500, f"br_funnel_detail failed: {e}")


# cc#502: BUY_REVERSAL_V5 pass-count gates — the 3 cheap daily-metric gates. sector_week (strict
# >0) + month_return (strict <5) + s1_touch (prior-4d low / today's live low) are handled inline
# in br_stock_passcount, mirroring _write_buy_reversal_v5_qualified exactly.
_BR_V5_PASSCOUNT_GATES = [
    ("mom_2d",      -0.5, None),   # (2)
    ("week_return", -2.0, None),   # (3)
    ("rsi_month",   60.0, 90.0),   # (4)
]

def br_stock_passcount():
    """cc#502: BUY_REVERSAL_V5 pass-count = n/7, cheap-first. S1-touch + 3 cheap daily gates +
    sector_week + month_return are evaluated for ALL stocks; true_weekly_rsi (stage 7, DB-heavy)
    is computed ONLY for stocks that clear the first 6 — every other stock caps at <=6/7 with
    true_weekly_rsi in the failed/skipped list. Display only — mirrors
    _write_buy_reversal_v5_qualified, never qualifies."""
    from v8_signal_writer import _true_weekly_rsi
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
            pivots   = _basket_pivots(cur)
            cmp_map  = _basket_cmp(cur)
            syms     = [r["symbol"] for r in all_rows]
            prior4_low = _basket_prior4_low(cur, syms)
            today_low  = _basket_day_low_today(cur)
            out = []
            for s in all_rows:
                sym = s["symbol"]
                passed, failed = [], []
                actuals = {}
                for metric, mn, mx in _BR_V5_PASSCOUNT_GATES:
                    actuals[metric] = s.get(metric)
                    (passed if _passes_filter(s.get(metric), mn, mx) else failed).append(metric)
                mr = s.get("month_return")
                actuals["month_return"] = mr
                (passed if (mr is not None and float(mr) < 5.0) else failed).append("month_return")
                sw = s.get("sector_week")
                actuals["sector_week"] = sw
                (passed if (sw is not None and float(sw) > 0.0) else failed).append("sector_week")
                pv   = pivots.get(sym)
                s1   = pv.get("s1") if pv else None
                p4lo = prior4_low.get(sym)
                tlo  = today_low.get(sym)
                s1_ok = s1 is not None and ((p4lo is not None and p4lo <= s1) or (tlo is not None and tlo <= s1))
                # cc#514: actual = the 5d/today low tested against S1 (whichever cleared it, else the
                # 5d low as the representative value shown in the funnel-row expansion).
                actuals["s1_touch"] = (p4lo if (p4lo is not None and s1 is not None and p4lo <= s1)
                                        else (tlo if (tlo is not None and s1 is not None and tlo <= s1)
                                              else (p4lo if p4lo is not None else tlo)))
                (passed if s1_ok else failed).append("s1_touch")
                # heavy true_weekly_rsi — ONLY for stocks that cleared all 6 cheap gates
                if len(passed) == 6:
                    cmp = cmp_map.get(sym)
                    twr = _true_weekly_rsi(conn, sym, cmp)
                    actuals["true_weekly_rsi"] = twr
                    (passed if (twr is not None and twr >= 70.0) else failed).append("true_weekly_rsi")
                else:
                    failed.append("true_weekly_rsi")   # skipped — not evaluated, caps at <=6/7
                out.append({"symbol": sym, "passed": len(passed), "total": 7,
                            "passed_filters": passed, "failed_filters": failed,
                            "gvm_score": s.get("gvm_score"), "mom_2d": s.get("mom_2d"),
                            "v21_pass": None, "actuals": actuals})
        out.sort(key=lambda x: (x["passed"], x["gvm_score"] if x["gvm_score"] is not None else -1), reverse=True)
        return {"basket": "buy_reversal", "score_date": str(date.today()),
                "universe": len(out), "filter_count": 7, "stocks": out,
                "v21_enabled": False, **BASKET_META.get("buy_reversal", {})}
    except Exception as e:
        raise HTTPException(500, f"br_stock_passcount failed: {e}")


@router.get("/br_stock_detail/{symbol}")
def br_stock_detail(symbol: str):
    """cc#502: per-stock 7-filter breakdown for BUY_REVERSAL_V5 — ACTUAL value vs REQUIRED bound
    + PASS/FAIL for each of the 7 gates, computed LIVE for ONE symbol. Powers the pass-count
    click-through modal so engine decisions can be verified by hand. Pass logic mirrors
    br_stock_passcount / _write_buy_reversal_v5_qualified EXACTLY (incl. the stage-7 rule that
    true_weekly_rsi only counts once all 6 cheap gates pass), so the green-row count equals the
    n/7 on the box. true_weekly_rsi is always computed here (one stock, cheap) so the row is
    never blank. Display only, never qualifies."""
    from v8_signal_writer import _true_weekly_rsi
    sym = symbol.upper()
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT gvm_score, mom_2d, week_return, rsi_month, sector_week, month_return
                FROM v8_metrics WHERE symbol=%s AND score_date=(SELECT MAX(score_date) FROM v8_metrics)""", (sym,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"No v8_metrics for {sym}")
            gvm, mom2d, wret, rmon, swk, mret = [float(x) if x is not None else None for x in row]
            cur.execute("""SELECT pp, s1 FROM v8_paper_pivots WHERE symbol=%s
                AND pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)""", (sym,))
            pv = cur.fetchone()
            pp = float(pv[0]) if pv and pv[0] is not None else None
            s1 = float(pv[1]) if pv and pv[1] is not None else None
            cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s AND cmp IS NOT NULL", (sym,))
            cr = cur.fetchone()
            cmp = float(cr[0]) if cr else None
            p4lo = _basket_prior4_low(cur, [sym]).get(sym)
            tlo  = _basket_day_low_today(cur).get(sym)
            twr  = _true_weekly_rsi(conn, sym, cmp)   # one stock — always compute so the row is never blank

        def _fmt(v, d):
            return "--" if v is None else f"{v:.{d}f}"

        s1_ok = s1 is not None and ((p4lo is not None and p4lo <= s1) or (tlo is not None and tlo <= s1))
        p_mom  = _passes_filter(mom2d, -0.5, None)
        p_wret = _passes_filter(wret, -2.0, None)
        p_rm   = _passes_filter(rmon, 60.0, 90.0)
        p_sw   = swk is not None and swk > 0.0
        p_mret = mret is not None and mret < 5.0
        cleared = all([s1_ok, p_mom, p_wret, p_rm, p_sw, p_mret])   # all 6 cheap gates
        p_twr  = cleared and (twr is not None and twr >= 70.0)     # stage-7 engine rule

        low_lbl = f"prior4d {_fmt(p4lo,2)} / today {_fmt(tlo,2)} vs S1 {_fmt(s1,2)}"
        rows = [
            {"filter": "s1_touch",        "required": "<= S1",    "actual": low_lbl,             "pass": s1_ok},
            {"filter": "mom_2d",          "required": ">= -0.5",  "actual": _fmt(mom2d, 2) + "%","pass": p_mom},
            {"filter": "week_return",     "required": ">= -2",    "actual": _fmt(wret, 2) + "%", "pass": p_wret},
            {"filter": "rsi_month",       "required": "60 to 90", "actual": _fmt(rmon, 1),       "pass": p_rm},
            {"filter": "sector_week",     "required": "> 0",      "actual": _fmt(swk, 2),        "pass": p_sw},
            {"filter": "month_return",    "required": "< 5",      "actual": _fmt(mret, 2) + "%", "pass": p_mret},
            {"filter": "true_weekly_rsi", "required": ">= 70",    "actual": _fmt(twr, 1),        "pass": p_twr},
        ]
        if not cleared:
            rows[6]["note"] = "engine evaluates true weekly RSI only after all 6 cheap gates pass"
        passed = sum(1 for r in rows if r["pass"])
        return {"symbol": sym, "cmp": cmp, "pp": pp, "s1": s1,
                "passed": passed, "total": 7, "rows": rows,
                "spec": "BUY_REVERSAL_V5 cc#502"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"br_stock_detail failed: {e}")


# cc#502 SELL_REVERSAL_V6.1 (replaces V5-D) funnel stages — SELL mirror pattern. Cheap-first,
# true_weekly_rsi last (heavy, only on cheap-intersection survivors). Labels carry spaces for
# verbatim render.
_SR_V61_STAGES = [
    ("r1_touch",        "R1 touch (last 3 days)", "",       ""),
    ("day_1d",          "day change",             ">= -2",  "<= 0"),
    ("dma_20",           "dma 20",                "",       "< 0"),
    ("dma_50",           "dma 50",                "",       "< 0"),
    ("dma_200",         "dma 200",                "",       "< 0"),
    ("week_index_52",   "52w index",              "",       "< 50"),
    ("sector_week",     "sector week",            "",       "< 0"),
    ("mom_2d",          "mom 2d",                 ">= -4",  "<= -1"),
    ("month_return",    "month return",           ">= -10", ""),
    ("room",            "room to S1/S2",          ">= 2%",  ""),
    ("true_weekly_rsi", "true weekly RSI",        "",       "<= 45"),
]

def sr_funnel_detail():
    """cc#502/514: 11-stage funnel for SELL_REVERSAL_V6.1, reshaped from the handler-written
    v8_funnel_counts row. INDEPENDENT per-filter pass counts across the universe (buy_momentum
    convention); true_weekly_rsi (stage 11) is passes vs the stocks clearing all 10 cheap gates.
    Final = strict-AND of all 11. Empty until the first live tick writes.
    cc#514: the "room" gate (engine funnel["room"], AND'd into `surv` in v8_signal_writer.py's
    sell_reversal handler) was missing from _SR_V61_STAGES entirely -- the funnel showed only 9
    cheap stages though sr_stock_passcount/sr_stock_detail already counted 10 (cc#509). Added here
    so the funnel-row click-through (cc#514) reconciles with the pass-count box on every basket,
    not just the 3 already fixed by cc#509. The `_stage9_survivors` DB key name is an engine-side
    log/naming quirk (already noted out-of-scope in cc#509) -- its VALUE is the correct 10-cheap-
    gate survivor count; only this display list was missing the stage."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            counts, _asof = _latest_funnel_counts(cur, "sell_reversal")   # cc#424: last-session as-of
        universe = int(counts.get("_universe", 0) or 0)
        stage10  = counts.get("_stage9_survivors")   # cc#514: value is correct (10 cheap gates); key name is a legacy quirk
        stage10  = int(stage10) if stage10 is not None else None
        final    = int(counts.get("_score_qualified", 0) or 0)
        stages = []
        for key, label, cmin, cmax in _SR_V61_STAGES:
            passes = int(counts.get(key, 0) or 0)
            denom = (stage10 if stage10 is not None else universe) if key == "true_weekly_rsi" else universe
            fails = max(denom - passes, 0)
            stage = {"metric": label, "key": key, "condition_min": cmin, "condition_max": cmax,
                     "passes": passes, "fails": fails, "survivors": passes, "killed": fails,
                     "pass_pct": round(passes / denom * 100, 1) if denom else 0}
            if key == "true_weekly_rsi":
                stage["denominator"] = denom
                stage["note"] = f"of {denom} stocks passing all 10 cheap gates"
            stages.append(stage)
        return {
            "basket": "sell_reversal", "score_date": str(_asof or date.today()),
            "universe": universe, "final": final, "filter_count": 11, "n_filters": 11,
            "stage9_survivors": stage10,
            "gate_type": "independent per-filter counts; final = strict AND of all 11",
            "score_qualified": final, "pivot_pass": final,
            "stages": stages, **BASKET_META.get("sell_reversal", {}),
        }
    except Exception as e:
        raise HTTPException(500, f"sr_funnel_detail failed: {e}")


# cc#502: SELL_REVERSAL_V6.1 pass-count cheap daily-metric gates. sector_week<0 (strict),
# R1-touch, room-to-S1/S2, true_weekly_rsi are handled inline in sr_stock_passcount.
_SR_V61_PASSCOUNT_GATES = [
    ("day_1d",       -2.0,  0.0),
    ("dma_20",        None, 0.0),
    ("dma_50",        None, 0.0),
    ("dma_200",       None, 0.0),
    ("week_index_52", None, 50.0),
    ("mom_2d",        -4.0, -1.0),
    ("month_return",  -10.0, None),
]

def sr_stock_passcount():
    """cc#502/509: SELL_REVERSAL_V6.1 pass-count = n/11 (10 cheap gates -- 7 metric gates +
    sector_week + r1_touch + room -- plus the true_weekly_rsi heavy stage). 10 cheap gates for
    ALL stocks; true_weekly_rsi (stage 11, heavy) only for stocks clearing all 10. cc#509 fix:
    the heavy-stage trigger was `len(passed)==9`, an off-by-one against these 10 cheap labels --
    a stock failing exactly one cheap gate still got true_weekly_rsi evaluated and could display
    max/max with a failed gate listed (FORCEMOT); a true full qualifier also capped at the same
    max, indistinguishable from a one-gate-short near miss. Off-market / missing pivot|CMP: the
    room check NULL-passes. Display only — mirrors the handler, never qualifies."""
    from v8_signal_writer import _true_weekly_rsi
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
            pivots   = _basket_pivots(cur)
            cmp_map  = _basket_cmp(cur)
            syms     = [r["symbol"] for r in all_rows]
            r1_touch = _basket_r1_touch_3d(cur, syms)
            out = []
            for s in all_rows:
                sym = s["symbol"]
                passed, failed = [], []
                actuals = {}
                for metric, mn, mx in _SR_V61_PASSCOUNT_GATES:
                    actuals[metric] = s.get(metric)
                    (passed if _passes_filter(s.get(metric), mn, mx) else failed).append(metric)
                sw = s.get("sector_week")
                actuals["sector_week"] = sw
                (passed if (sw is not None and float(sw) < 0.0) else failed).append("sector_week")
                actuals["r1_touch"] = sym in r1_touch
                (passed if sym in r1_touch else failed).append("r1_touch")
                cmp = cmp_map.get(sym)
                pv  = pivots.get(sym)
                s1  = pv.get("s1") if pv else None
                s2  = pv.get("s2") if pv else None
                tgt, room = _sr_dynamic_target(cmp, s1, s2)
                actuals["room"] = room
                room_ok = (cmp is None or s1 is None) or (tgt is not None)
                (passed if room_ok else failed).append("room")
                if len(passed) == 10:   # cc#509: was ==9, off-by-one against these 10 cheap labels
                    twr = _true_weekly_rsi(conn, sym, cmp)
                    actuals["true_weekly_rsi"] = twr
                    (passed if (twr is not None and twr <= 45.0) else failed).append("true_weekly_rsi")
                else:
                    failed.append("true_weekly_rsi")
                out.append({"symbol": sym, "passed": len(passed), "total": 11,
                            "passed_filters": passed, "failed_filters": failed,
                            "gvm_score": s.get("gvm_score"), "mom_2d": s.get("mom_2d"),
                            "v21_pass": None, "actuals": actuals})
        out.sort(key=lambda x: (x["passed"], x["gvm_score"] if x["gvm_score"] is not None else -1), reverse=True)
        return {"basket": "sell_reversal", "score_date": str(date.today()),
                "universe": len(out), "filter_count": 11, "stocks": out,
                "v21_enabled": False, **BASKET_META.get("sell_reversal", {})}
    except Exception as e:
        raise HTTPException(500, f"sr_stock_passcount failed: {e}")


@router.get("/sr_stock_detail/{symbol}")
def sr_stock_detail(symbol: str):
    """cc#502/509: per-stock 11-filter breakdown for SELL_REVERSAL_V6.1 (10 cheap gates + the
    true_weekly_rsi heavy stage) — ACTUAL vs REQUIRED + PASS/FAIL. Mirrors sr_stock_passcount /
    _write_sell_reversal_v61_qualified so the green-row count equals n/11 and the card and modal
    always agree. true_weekly_rsi always computed here (one stock) so the row is never blank."""
    from v8_signal_writer import _true_weekly_rsi
    sym = symbol.upper()
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT day_1d, dma_20, dma_50, dma_200, week_index_52, sector_week,
                mom_2d, month_return FROM v8_metrics
                WHERE symbol=%s AND score_date=(SELECT MAX(score_date) FROM v8_metrics)""", (sym,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"No v8_metrics for {sym}")
            d1d, dma20, dma50, dma200, w52, swk, mom2d, mret = [float(x) if x is not None else None for x in row]
            cur.execute("""SELECT pp, s1, s2 FROM v8_paper_pivots WHERE symbol=%s
                AND pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)""", (sym,))
            pv = cur.fetchone()
            pp = float(pv[0]) if pv and pv[0] is not None else None
            s1 = float(pv[1]) if pv and pv[1] is not None else None
            s2 = float(pv[2]) if pv and pv[2] is not None else None
            cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s AND cmp IS NOT NULL", (sym,))
            cr = cur.fetchone()
            cmp = float(cr[0]) if cr else None
            r1_ok = sym in _basket_r1_touch_3d(cur, [sym])
            twr = _true_weekly_rsi(conn, sym, cmp)

        def _fmt(v, d):
            return "--" if v is None else f"{v:.{d}f}"
        tgt, room = _sr_dynamic_target(cmp, s1, s2)

        p_d1d  = _passes_filter(d1d, -2.0, 0.0)
        p_d20  = _passes_filter(dma20, None, 0.0)
        p_d50  = _passes_filter(dma50, None, 0.0)
        p_d200 = _passes_filter(dma200, None, 0.0)
        p_w52  = _passes_filter(w52, None, 50.0)
        p_sw   = swk is not None and swk < 0.0
        p_mom  = _passes_filter(mom2d, -4.0, -1.0)
        p_mret = _passes_filter(mret, -10.0, None)
        p_room = (cmp is None or s1 is None) or (tgt is not None)
        cleared = all([r1_ok, p_d1d, p_d20, p_d50, p_d200, p_w52, p_sw, p_mom, p_mret, p_room])   # 10 cheap gates
        p_twr  = cleared and (twr is not None and twr <= 45.0)

        tgt_lbl = "S1" if (tgt is not None and s1 is not None and abs(tgt - s1) < 1e-6) else ("S2" if tgt is not None else "--")
        rows = [
            {"filter": "r1_touch",        "required": "last 3 days", "actual": "touched" if r1_ok else "not touched", "pass": r1_ok},
            {"filter": "day_1d",          "required": "-2 to 0",   "actual": _fmt(d1d, 2) + "%",   "pass": p_d1d},
            {"filter": "dma_20",          "required": "< 0",       "actual": _fmt(dma20, 2) + "%", "pass": p_d20},
            {"filter": "dma_50",          "required": "< 0",       "actual": _fmt(dma50, 2) + "%", "pass": p_d50},
            {"filter": "dma_200",         "required": "< 0",       "actual": _fmt(dma200, 2) + "%","pass": p_d200},
            {"filter": "week_index_52",   "required": "< 50",      "actual": _fmt(w52, 1),         "pass": p_w52},
            {"filter": "sector_week",     "required": "< 0",       "actual": _fmt(swk, 2),         "pass": p_sw},
            {"filter": "mom_2d",          "required": "-4 to -1",  "actual": _fmt(mom2d, 2) + "%", "pass": p_mom},
            {"filter": "month_return",    "required": ">= -10",    "actual": _fmt(mret, 2) + "%",  "pass": p_mret},
            {"filter": "room",            "required": ">= 2%",     "actual": (f"{_fmt(room, 2)}% -> {tgt_lbl}" if room is not None else "--"), "pass": p_room},
            {"filter": "true_weekly_rsi", "required": "<= 45",     "actual": _fmt(twr, 1),         "pass": p_twr},
        ]
        if not cleared:
            rows[-1]["note"] = "engine evaluates true weekly RSI only after all 10 cheap gates pass"
        passed = sum(1 for r in rows if r["pass"])
        return {"symbol": sym, "cmp": cmp, "pp": pp, "s1": s1, "s2": s2,
                "target": tgt, "room_pct": room, "passed": passed, "total": 11, "rows": rows,
                "spec": "SELL_REVERSAL_V6.1 cc#502"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"sr_stock_detail failed: {e}")


# cc#502 SELL_MOMENTUM_V4 (renamed from V3): twr<=45->40, mom_2d[-4,-1]->[-4,-2], else unchanged.
# Cheap-first, true_weekly_rsi last (heavy, only on the 8-cheap-gate intersection). Labels carry
# spaces for verbatim render.
_SM_V3_STAGES = [
    ("rsi_month",       "monthly RSI",     "",       "< 40"),
    ("mom_2d",          "mom 2d",          ">= -4",  "<= -2"),
    ("dma_200",         "dma 200",         "",       "<= 2"),
    ("week_return",     "week return",     ">= -10", "<= -0.5"),
    ("sector_week",     "sector week",     "",       "< 0"),
    ("week_index_52",   "52w index",       ">= 20",  "<= 60"),
    ("cmp_lt_pp",       "CMP < PP",        "",       ""),
    ("s2_clearance",    "S2 clearance",    ">= 3%",  ""),
    ("true_weekly_rsi", "true weekly RSI", "",       "<= 40"),
]

def sm_funnel_detail():
    """cc#502: 9-stage funnel for SELL_MOMENTUM_V4, reshaped from the handler-written
    v8_funnel_counts row. INDEPENDENT per-filter pass counts across the universe (buy_momentum
    convention); true_weekly_rsi (stage 9) is passes vs the stocks clearing all 8 cheap gates.
    Final = strict-AND of all 9. Empty until the first live tick writes."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            counts, _asof = _latest_funnel_counts(cur, "sell_momentum")   # cc#424: last-session as-of
        universe = int(counts.get("_universe", 0) or 0)
        stage8   = counts.get("_stage8_survivors")
        stage8   = int(stage8) if stage8 is not None else None
        final    = int(counts.get("_score_qualified", 0) or 0)
        stages = []
        for key, label, cmin, cmax in _SM_V3_STAGES:
            passes = int(counts.get(key, 0) or 0)
            denom = (stage8 if stage8 is not None else universe) if key == "true_weekly_rsi" else universe
            fails = max(denom - passes, 0)
            stage = {"metric": label, "key": key, "condition_min": cmin, "condition_max": cmax,
                     "passes": passes, "fails": fails, "survivors": passes, "killed": fails,
                     "pass_pct": round(passes / denom * 100, 1) if denom else 0}
            if key == "true_weekly_rsi":
                stage["denominator"] = denom
                stage["note"] = f"of {denom} stocks passing all 8 cheap gates"
            stages.append(stage)
        return {
            "basket": "sell_momentum", "score_date": str(_asof or date.today()),
            "universe": universe, "final": final, "filter_count": 9, "n_filters": 9,
            "stage8_survivors": stage8,
            "gate_type": "independent per-filter counts; final = strict AND of all 9",
            "score_qualified": final, "pivot_pass": final,
            "stages": stages, **BASKET_META.get("sell_momentum", {}),
        }
    except Exception as e:
        raise HTTPException(500, f"sm_funnel_detail failed: {e}")


# cc#502: SELL_MOMENTUM_V4 pass-count cheap v8_metrics gates via _passes_filter (inclusive).
# rsi_month<40 + sector_week<0 (strict), CMP<PP, S2-clearance, true_weekly_rsi handled inline.
_SM_V3_PASSCOUNT_GATES = [
    ("mom_2d",        -4.0,  -2.0),
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
                actuals = {}
                for metric, mn, mx in _SM_V3_PASSCOUNT_GATES:
                    actuals[metric] = s.get(metric)
                    (passed if _passes_filter(s.get(metric), mn, mx) else failed).append(metric)
                rm = s.get("rsi_month")
                actuals["rsi_month"] = rm
                (passed if (rm is not None and float(rm) < 40.0) else failed).append("rsi_month")
                sw = s.get("sector_week")
                actuals["sector_week"] = sw
                (passed if (sw is not None and float(sw) < 0.0) else failed).append("sector_week")
                cmp = cmp_map.get(sym)
                pv  = pivots.get(sym)
                pp  = pv.get("pp") if pv else None
                s2  = pv.get("s2") if pv else None
                cmp_ok = (cmp is None or pp is None) or (cmp < pp)
                s2c = ((cmp - s2) / cmp * 100.0) if (cmp and s2 is not None) else None
                s2c_ok = (cmp is None or s2 is None) or (s2c is not None and s2c >= 3.0)
                actuals["cmp_lt_pp"] = cmp
                actuals["s2_clearance"] = s2c
                (passed if cmp_ok else failed).append("cmp_lt_pp")
                (passed if s2c_ok else failed).append("s2_clearance")
                if len(passed) == 8:
                    twr = _true_weekly_rsi(conn, sym, cmp)
                    actuals["true_weekly_rsi"] = twr
                    (passed if (twr is not None and twr <= 40.0) else failed).append("true_weekly_rsi")
                else:
                    failed.append("true_weekly_rsi")
                out.append({"symbol": sym, "passed": len(passed), "total": 9,
                            "passed_filters": passed, "failed_filters": failed,
                            "gvm_score": s.get("gvm_score"), "mom_2d": s.get("mom_2d"),
                            "v21_pass": None, "actuals": actuals})
        out.sort(key=lambda x: (x["passed"], x["gvm_score"] if x["gvm_score"] is not None else -1), reverse=True)
        return {"basket": "sell_momentum", "score_date": str(date.today()),
                "universe": len(out), "filter_count": 9, "stocks": out,
                "v21_enabled": False, **BASKET_META.get("sell_momentum", {})}
    except Exception as e:
        raise HTTPException(500, f"sm_stock_passcount failed: {e}")


@router.get("/sm_stock_detail/{symbol}")
def sm_stock_detail(symbol: str):
    """cc#502: per-stock 9-filter breakdown for SELL_MOMENTUM_V4 (renamed from V3, twr<=45->40,
    mom_2d[-4,-1]->[-4,-2]). Mirrors sm_stock_passcount / the handler so the green-row count
    equals n/9. true_weekly_rsi always computed here so the row is never blank."""
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
        p_mom  = _passes_filter(mom2d, -4.0, -2.0)
        p_dma  = _passes_filter(dma200, None, 2.0)
        p_wret = _passes_filter(wret, -10.0, -0.5)
        p_sw   = swk is not None and swk < 0.0
        p_w52  = _passes_filter(w52, 20.0, 60.0)
        p_cmp  = (cmp is None or pp is None) or (cmp < pp)
        p_s2c  = (cmp is None or s2 is None) or (s2c is not None and s2c >= 3.0)
        cleared = all([p_rm, p_mom, p_dma, p_wret, p_sw, p_w52, p_cmp, p_s2c])   # 8 cheap gates
        p_twr  = cleared and (twr is not None and twr <= 40.0)

        rows = [
            {"filter": "rsi_month",       "required": "< 40",     "actual": _fmt(rmon, 1),        "pass": p_rm},
            {"filter": "mom_2d",          "required": "-4 to -2", "actual": _fmt(mom2d, 2) + "%", "pass": p_mom},
            {"filter": "dma_200",         "required": "<= 2",     "actual": _fmt(dma200, 2) + "%","pass": p_dma},
            {"filter": "week_return",     "required": "-10 to -0.5","actual": _fmt(wret, 2) + "%","pass": p_wret},
            {"filter": "sector_week",     "required": "< 0",      "actual": _fmt(swk, 2),         "pass": p_sw},
            {"filter": "week_index_52",   "required": "20 to 60", "actual": _fmt(w52, 1),         "pass": p_w52},
            {"filter": "cmp_lt_pp",       "required": "CMP < PP", "actual": f"{_fmt(cmp, 2)} vs {_fmt(pp, 2)}", "pass": p_cmp},
            {"filter": "s2_clearance",    "required": ">= 3%",    "actual": _fmt(s2c, 2) + "%",   "pass": p_s2c},
            {"filter": "true_weekly_rsi", "required": "<= 40",    "actual": _fmt(twr, 1),         "pass": p_twr},
        ]
        if not cleared:
            rows[8]["note"] = "engine evaluates true weekly RSI only after all 8 cheap gates pass"
        passed = sum(1 for r in rows if r["pass"])
        return {"symbol": sym, "cmp": cmp, "pp": pp, "s2": s2,
                "s2_clearance_pct": round(s2c, 2) if s2c is not None else None,
                "passed": passed, "total": 9, "rows": rows,
                "spec": "SELL_MOMENTUM_V4 cc#502"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"sm_stock_detail failed: {e}")


# cc#502 BUY_MOMENTUM_V3 funnel stages -- 7 HARD gates (dedicated handler, mirrors cc#364
# cheap-first convention), true_weekly_rsi last (heavy, only on the 6-cheap-gate intersection).
# A stage-8 SCORE row (>=7 of 10 V2 bands, fixed threshold) is appended separately below since it
# is a second independent layer, not part of the 7 hard gates.
_BM_V3_STAGES = [
    ("dma_50",         "dma 50",          ">= 5",   "<= 12"),
    ("dma_20",         "dma 20",          "> 0",    ""),
    ("week_index_52",  "52w index",       ">= 75",  ""),
    ("gvm_score",      "gvm score",       ">= 7",   ""),
    ("day_1d",         "day change",      "> 0",    ""),
    ("hourly_pct",     "hourly % (from ~10:15)", "> 0", "NOT NULL"),
    ("true_weekly_rsi","true weekly RSI", ">= 70",  "<= 85"),
]

def bm_funnel_detail():
    """cc#502: BUY_MOMENTUM_V3 8-stage funnel, reshaped from the handler-written v8_funnel_counts
    row. INDEPENDENT per-filter pass counts across the universe for the 6 cheap hard gates;
    true_weekly_rsi (stage 7, DB-heavy) is passes vs the stocks clearing all 6. NOTE: hard-gate
    pass is NECESSARY but not SUFFICIENT to qualify -- final qualification also requires
    SCORE>=7-of-10 V2 bands (stage 8, denominator = hard-qualified count)."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            counts, _asof = _latest_funnel_counts(cur, "buy_momentum")
        universe = int(counts.get("_universe", 0) or 0)
        stage6   = counts.get("_stage6_survivors")
        stage6   = int(stage6) if stage6 is not None else None
        hard_qualified = int(counts.get("true_weekly_rsi", 0) or 0)
        final    = int(counts.get("_score_qualified", 0) or 0)
        stages = []
        for key, label, cmin, cmax in _BM_V3_STAGES:
            passes = int(counts.get(key, 0) or 0)
            denom = (stage6 if stage6 is not None else universe) if key == "true_weekly_rsi" else universe
            fails = max(denom - passes, 0)
            stage = {"metric": label, "key": key, "condition_min": cmin, "condition_max": cmax,
                     "passes": passes, "fails": fails, "survivors": passes, "killed": fails,
                     "pass_pct": round(passes / denom * 100, 1) if denom else 0}
            if key == "true_weekly_rsi":
                stage["denominator"] = denom
                stage["note"] = f"of {denom} stocks passing all 6 cheap hard gates"
            stages.append(stage)
        stages.append({
            # cc#514: not a passed_filters/failed_filters entry -- the funnel-row click-through
            # special-cases this key to list stocks by score_qualified/score instead.
            "metric": "score >= 7 of 10 (V2 bands)", "key": "_score_band", "condition_min": "fixed threshold", "condition_max": "",
            "passes": final, "fails": max(hard_qualified - final, 0),
            "survivors": final, "killed": max(hard_qualified - final, 0),
            "pass_pct": round(final / hard_qualified * 100, 1) if hard_qualified else 0,
            "denominator": hard_qualified,
            "note": f"of {hard_qualified} stocks passing all HARD gates (incl. wRSI[70,85])",
        })
        return {
            "basket": "buy_momentum", "score_date": str(_asof or date.today()),
            "universe": universe, "final": final, "filter_count": 8, "n_filters": 8,
            "stage6_survivors": stage6, "hard_qualified": hard_qualified,
            "gate_type": "HARD gates (6) + FINAL heavy wRSI + SCORE>=7-of-10 V2 bands",
            "score_qualified": final, "pivot_pass": final,
            "stages": stages, **BASKET_META.get("buy_momentum", {}),
        }
    except Exception as e:
        raise HTTPException(500, f"bm_funnel_detail failed: {e}")


# cc#502: BUY_MOMENTUM_V3 pass-count cheap range gates. dma_20>0, day_1d>0, hourly_pct>0&NOT NULL
# are strict-positive checks handled inline (mirror sr_stock_passcount's sector_week convention).
_BM_V3_PASSCOUNT_GATES = [
    ("dma_50",        5.0,  12.0),
    ("week_index_52", 75.0, None),
    ("gvm_score",      7.0, None),
]

# cc#502: SCORE_BANDS mirror of _write_buy_momentum_v3_qualified's V2 bands (fixed >=7-of-10
# threshold). true_weekly_rsi[60,85] is the 10th band, scored separately since it reuses the same
# wRSI value computed for the hard gate rather than a fresh v8_metrics column.
_BM_SCORE_BANDS = [
    ("gvm_score",    7.0,  10.0),
    ("dma_50",       8.0,  25.0),
    ("dma_200",      8.0,  40.0),
    ("rsi_month",    70.0, 100.0),
    ("week_return",  0.5,  12.0),
    ("month_return", 2.0,  30.0),
    ("mom_2d",       0.0,  6.0),
    ("sector_week",  0.0,  6.0),
    ("sector_month", 0.0,  6.0),
]

def bm_stock_passcount():
    """cc#502: BUY_MOMENTUM_V3 pass-count = n/7 HARD gates, cheap-first (mirror cc#364). 6 cheap
    gates for ALL stocks; true_weekly_rsi (stage 7, heavy) only for stocks clearing the first 6.
    A stock clearing all 7 hard gates ALSO carries a separate score/10 (SCORE_BANDS, fixed >=7
    threshold) since real qualification requires BOTH layers -- score is null for stocks that
    don't clear the hard gates. Display only -- mirrors _write_buy_momentum_v3_qualified, never
    qualifies."""
    from v8_signal_writer import _true_weekly_rsi
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
            cmp_map  = _basket_cmp(cur)
            syms     = [r["symbol"] for r in all_rows]
            v21_metrics = _load_v21_live_metrics(conn, syms)
            out = []
            for s in all_rows:
                sym = s["symbol"]
                passed, failed = [], []
                actuals = {}
                for metric, mn, mx in _BM_V3_PASSCOUNT_GATES:
                    actuals[metric] = s.get(metric)
                    (passed if _passes_filter(s.get(metric), mn, mx) else failed).append(metric)
                d20 = s.get("dma_20")
                actuals["dma_20"] = d20
                (passed if (d20 is not None and float(d20) > 0.0) else failed).append("dma_20")
                d1d = s.get("day_1d")
                actuals["day_1d"] = d1d
                (passed if (d1d is not None and float(d1d) > 0.0) else failed).append("day_1d")
                hp = v21_metrics.get(sym, {}).get("hourly_pct")
                actuals["hourly_pct"] = hp
                (passed if (hp is not None and float(hp) > 0.0) else failed).append("hourly_pct")
                score = None
                if len(passed) == 6:
                    cmp = cmp_map.get(sym)
                    twr = _true_weekly_rsi(conn, sym, cmp)
                    actuals["true_weekly_rsi"] = twr
                    if twr is not None and 70.0 <= twr <= 85.0:
                        passed.append("true_weekly_rsi")
                        score = sum(1 for metric, mn, mx in _BM_SCORE_BANDS
                                    if _passes_filter(s.get(metric), mn, mx))
                        if 60.0 <= twr <= 85.0:
                            score += 1
                    else:
                        failed.append("true_weekly_rsi")
                else:
                    failed.append("true_weekly_rsi")   # skipped -- not evaluated, caps at <=6/7
                out.append({"symbol": sym, "passed": len(passed), "total": 7,
                            "passed_filters": passed, "failed_filters": failed,
                            "gvm_score": s.get("gvm_score"), "mom_2d": s.get("mom_2d"),
                            "score": score, "score_total": 10,
                            "score_qualified": bool(score is not None and score >= 7),
                            "v21_pass": None, "actuals": actuals})
        out.sort(key=lambda x: (x["passed"], x["score"] if x["score"] is not None else -1,
                                 x["gvm_score"] if x["gvm_score"] is not None else -1), reverse=True)
        return {"basket": "buy_momentum", "score_date": str(date.today()),
                "universe": len(out), "filter_count": 7, "stocks": out,
                "v21_enabled": False, **BASKET_META.get("buy_momentum", {})}
    except Exception as e:
        raise HTTPException(500, f"bm_stock_passcount failed: {e}")


@router.get("/bm_stock_detail/{symbol}")
def bm_stock_detail(symbol: str):
    """cc#502: per-stock breakdown for BUY_MOMENTUM_V3 -- 7 HARD gates (ACTUAL vs REQUIRED +
    PASS/FAIL, computed LIVE for one symbol, true_weekly_rsi only after all 6 cheap gates clear)
    PLUS a separate SCORE/10 breakdown (SCORE_BANDS, fixed >=7 threshold, wRSI band reuses the
    same true_weekly_rsi value) once all 7 hard gates pass. Qualification requires BOTH 7/7 hard
    gates AND score>=7. Mirrors bm_stock_passcount / _write_buy_momentum_v3_qualified exactly.
    Display only, never qualifies."""
    from v8_signal_writer import _true_weekly_rsi
    sym = symbol.upper()
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT gvm_score, dma_50, dma_20, dma_200, week_index_52, day_1d,
                rsi_month, week_return, month_return, mom_2d, sector_week, sector_month
                FROM v8_metrics WHERE symbol=%s AND score_date=(SELECT MAX(score_date) FROM v8_metrics)""", (sym,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"No v8_metrics for {sym}")
            (gvm, dma50, dma20, dma200, w52, d1d,
             rmon, wret, mret, mom2d, swk, smon) = [float(x) if x is not None else None for x in row]
            cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s AND cmp IS NOT NULL", (sym,))
            cr = cur.fetchone()
            cmp = float(cr[0]) if cr else None
            hp = _load_v21_live_metrics(conn, [sym]).get(sym, {}).get("hourly_pct")
            twr = _true_weekly_rsi(conn, sym, cmp)   # one stock -- always compute so the row is never blank

        def _fmt(v, d):
            return "--" if v is None else f"{v:.{d}f}"

        p_dma50 = _passes_filter(dma50, 5.0, 12.0)
        p_dma20 = dma20 is not None and dma20 > 0.0
        p_w52   = _passes_filter(w52, 75.0, None)
        p_gvm   = _passes_filter(gvm, 7.0, None)
        p_d1d   = d1d is not None and d1d > 0.0
        p_hp    = hp is not None and hp > 0.0
        cleared = all([p_dma50, p_dma20, p_w52, p_gvm, p_d1d, p_hp])   # 6 cheap gates
        p_twr   = cleared and (twr is not None and 70.0 <= twr <= 85.0)

        rows = [
            {"filter": "dma_50",          "required": "5 to 12",  "actual": _fmt(dma50, 2) + "%", "pass": p_dma50},
            {"filter": "dma_20",          "required": "> 0",      "actual": _fmt(dma20, 2) + "%", "pass": p_dma20},
            {"filter": "week_index_52",   "required": ">= 75",    "actual": _fmt(w52, 1),          "pass": p_w52},
            {"filter": "gvm_score",       "required": ">= 7",     "actual": _fmt(gvm, 1),          "pass": p_gvm},
            {"filter": "day_1d",          "required": "> 0",      "actual": _fmt(d1d, 2) + "%",    "pass": p_d1d},
            {"filter": "hourly_pct",      "required": "> 0",      "actual": _fmt(hp, 2) + "%",     "pass": p_hp},
            {"filter": "true_weekly_rsi", "required": "70 to 85", "actual": _fmt(twr, 1),          "pass": p_twr},
        ]
        if not cleared:
            rows[6]["note"] = "engine evaluates true weekly RSI only after all 6 cheap gates pass"
        passed = sum(1 for r in rows if r["pass"])

        score, score_rows = None, None
        if p_twr:
            metric_vals = {"gvm_score": gvm, "dma_50": dma50, "dma_200": dma200, "rsi_month": rmon,
                            "week_return": wret, "month_return": mret, "mom_2d": mom2d,
                            "sector_week": swk, "sector_month": smon}
            score, score_rows = 0, []
            for metric, mn, mx in _BM_SCORE_BANDS:
                v = metric_vals.get(metric)
                ok = _passes_filter(v, mn, mx)
                if ok:
                    score += 1
                score_rows.append({"filter": metric, "required": f"{mn} to {mx}",
                                    "actual": _fmt(v, 2), "pass": ok})
            twr_ok = twr is not None and 60.0 <= twr <= 85.0
            if twr_ok:
                score += 1
            score_rows.append({"filter": "true_weekly_rsi (score band)", "required": "60 to 85",
                                "actual": _fmt(twr, 1), "pass": twr_ok})

        return {"symbol": sym, "cmp": cmp, "passed": passed, "total": 7, "rows": rows,
                "score": score, "score_total": 10, "score_rows": score_rows,
                "score_qualified": bool(score is not None and score >= 7),
                "spec": "BUY_MOMENTUM_V3 cc#502"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"bm_stock_detail failed: {e}")


@router.get("/funnel_detail/{basket}")
def funnel_detail(basket: str):
    basket = basket.lower()
    if basket == "buy_reversal":    return br_funnel_detail()   # cc#502 V5
    if basket == "sell_reversal":   return sr_funnel_detail()   # cc#502 V6.1
    if basket == "sell_momentum":   return sm_funnel_detail()   # cc#502 V4
    if basket == "buy_momentum":    return bm_funnel_detail()   # cc#502 V3 (NEW dedicated handler)
    if basket not in FILTER_CONFIG: raise HTTPException(404, f"Unknown basket: {basket}")
    # cc#502: unreachable — all four FILTER_CONFIG baskets are dispatched to a dedicated
    # function above. Legacy generic score-gate funnel body kept dormant below only in case a
    # future basket is added back into the loop; never exercised today.
    try:
        with _conn() as conn, conn.cursor() as cur:
            all_rows = _basket_universe(cur)
            pivots   = _basket_pivots(cur)
            cmp_map  = _basket_cmp(cur)
            # cc#446 fix_2: anchor to the last session the basket qualified (freeze-at-last-tick), so
            # the qualified/final counts don't collapse to 0 off-market while the sibling threshold
            # (below) already shows the last session. MAX(signal_date)=today during live hours.
            cur.execute("""SELECT COUNT(*) FROM v8_qualified WHERE basket=%s
                AND signal_date=(SELECT MAX(signal_date) FROM v8_qualified WHERE basket=%s)""", (basket, basket))
            score_qualified = int(cur.fetchone()[0])
            cur.execute("""SELECT counts->>'_score_threshold' FROM v8_funnel_counts
                WHERE basket=%s ORDER BY score_date DESC, computed_at DESC LIMIT 1""", (basket,))  # cc#424: last-session as-of
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
                WHERE q.basket=%s AND q.signal_date=(SELECT MAX(signal_date) FROM v8_qualified WHERE basket=q.basket)
            """, (basket,))   # cc#446 fix_2: anchor pivot-room rows to last session (freeze-at-last-tick)
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
    if basket == "buy_reversal":    return br_stock_passcount()   # cc#502 V5
    if basket == "sell_reversal":   return sr_stock_passcount()   # cc#502 V6.1
    if basket == "sell_momentum":   return sm_stock_passcount()   # cc#502 V4
    if basket == "buy_momentum":    return bm_stock_passcount()   # cc#502 V3 (NEW dedicated handler)
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


@router.get("/adr_history")
def adr_history(days: int = 5):
    """cc#443: last N TRADING-day ADR values from adr_daily (weekend rows excluded, matching the
    cc#417 guard) for the mood-panel rolling-trend sparkline. Oldest -> newest."""
    try:
        n = max(1, min(days, 30))
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT price_date, adr FROM adr_daily
                           WHERE adr IS NOT NULL AND EXTRACT(DOW FROM price_date) BETWEEN 1 AND 5
                           ORDER BY price_date DESC LIMIT %s""", (n,))
            rows = [{"date": str(r[0]), "adr": round(float(r[1]), 2)} for r in cur.fetchall()][::-1]
        return {"points": rows}
    except Exception as e:
        raise HTTPException(500, f"adr_history failed: {e}")


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
            cur.execute(f"SELECT {_last_session_sql()}")   # cc#446: canonical last-session anchor
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
def v8_daylog(era: str = "fresh"):
    """Day-wise aggregated performance table. Capital base Rs.50,00,000. Brokerage Rs.500/closed trade.
    cc#510: defaults to the REBUILT-SUITE era only -- every CTE is restricted to
    entry_ts >= app_config.v8_paper_rebuild_cutover_ts (cc#504's cutover). This excludes the
    SUITE_REBUILD flatten rows (old entry_ts -- an administrative close of pre-rebuild positions,
    not fresh-era performance) and all pre-rebuild history. History is never deleted (cc#504
    doctrine); pass ?era=all to see the full legacy+fresh book unfiltered."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cutover_ts = None
            if era != "all":
                cur.execute("SELECT value FROM app_config WHERE key='v8_paper_rebuild_cutover_ts'")
                row = cur.fetchone()
                cutover_ts = row[0] if row and row[0] else None
            cur.execute("""
                WITH all_dates AS (
                    SELECT DISTINCT entry_ts::date AS d FROM v8_paper_positions
                    WHERE %(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp
                    UNION SELECT DISTINCT entry_ts::date FROM v8_paper_trades
                    WHERE %(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp
                    UNION SELECT DISTINCT COALESCE(closed_at::date, exit_ts::date) FROM v8_paper_trades
                    WHERE %(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp
                ),
                opened AS (
                    SELECT entry_ts::date AS d,
                        COUNT(*) FILTER (WHERE side='LONG') AS long_opened,
                        COUNT(*) FILTER (WHERE side='SHORT') AS short_opened,
                        COUNT(*) AS total_opened
                    FROM (SELECT entry_ts, side FROM v8_paper_positions
                          WHERE %(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp
                          UNION ALL SELECT entry_ts, side FROM v8_paper_trades
                          WHERE %(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp) e
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
                    FROM v8_paper_trades
                    WHERE %(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp
                    GROUP BY COALESCE(closed_at::date, exit_ts::date)
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
            """, {"cut": cutover_ts})
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
            "era": "all" if era == "all" else "fresh",
            "rebuild_cutover_ts": str(cutover_ts) if cutover_ts else None,
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
