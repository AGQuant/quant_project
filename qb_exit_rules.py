"""
qb_exit_rules.py -- cc#2127: Quant Basket Exit Rules V1.

Six EOD exit conditions for open Quant-Basket-Builder positions, each independently toggleable,
combined via the SAME AND/OR combinator cc#2126 built (imported, not duplicated). Founder words,
16-Sep: "hard stop loss user can define 10-15% from entry. Second exit rule trailing stop loss --
fall from peak after entry the stock made high, so if 10% fall from the peak that is also exit.
Rating exit if GVM rating falls below this. Momentum rating exit if momentum rating falls below
this. And monthly RSI exit and EMA crossover exit. AND or OR, both -- for now use limited
filters, we will add up later on."

TWO EXISTING-ENGINE RECONCILIATIONS FOUND, before building (data-source gate, Role Split: "CC
greps and answers before the build proceeds") -- the card's own spec flagged both, verified here
rather than assumed:

1. cc#2093 (trailing_peak_pct/rank_fall_y computed-but-never-enforced) -- VERIFIED LANDED. Read
   v12_backtest.py's run_backtest() directly: `keep`/`forced_exit_reason` now genuinely drop a
   position that breached its trailing peak or rank_fall_y threshold (the dead-code bug is fixed,
   commit 0e0e84a, confirmed on main via git ancestry). This card does not need to fix or re-fix
   that enforcement.

2. trailing_stop_pct is a NAMED, DELIBERATE FORMULA DIFFERENCE from v12_backtest.py's own
   peak_since_entry, not a duplicate to avoid. Read v12_backtest.py directly: its peak_since_entry
   tracks the peak CLOSE while held (`pc = series.as_of(sym, d); if pc > peak_since_entry.get(sym,
   0): peak_since_entry[sym] = pc` -- a close-price series). This card's founder brief explicitly
   says "the stock made high... fall from the peak" and its own scope item (b) is explicit:
   "peak_since_entry = MAX(raw_prices.high)... the actual traded HIGH, not the close, since
   raw_prices carries a high column and a peak is a high-water mark." These are two different,
   independently-specified formulas (close-peak vs. high-peak), not one feature with an accidental
   second implementation -- so trailing_stop_pct below is genuinely new code, built to the FOUNDER'S
   explicit high-based formula (per this card, the newer and more specific instruction), not a copy
   of v12_backtest.py's close-based one. The divergence is named here, not swept under "reuse."
   True code-sharing between a full backtest WALK's incremental state and a standalone per-position
   READ (what every other condition in this file, and every cc#2126 condition, is shaped as) would
   need v12_backtest.py's own internals refactored -- out of scope for this card, left to cc#2129
   or a dedicated card, per the same "no v12_backtest.py splice yet" scope this card shares with
   cc#2126.

SCOPE FOR THIS PASS, stated before writing code (cc_task_logs task 2127), mirroring cc#2126's own
precedent exactly: builds and hand-verifies the six exit conditions, the combinator (reused), and
persistence. Does NOT splice these into v12_backtest.py's live exit_def dict, does NOT expose
trailing_peak_pct in the V12 wizard, and does NOT build a UI -- all assigned to cc#2129 ("wire
cc#2123/2126/2127/2128 into the EXISTING v12_backtest.py engine") once cc#2128 also exists. Does
NOT touch qb_eod_checker.py or session_log 124's live production exit rules (Hard Stop 1 -20%,
Hard Stop 2 -10% vs Nifty50, Filter Exit) -- those govern real money on live baskets; this is a
separate, new, customizable engine for baskets built through the V12/Quant-Basket-Builder path,
per the card's own explicit ruling. FLAG, not built: two hard-stop systems will exist side by
side once this ships (the old -20%/-10% on legacy baskets, the new founder-configurable one on
V12 baskets) -- worth knowing before either is used to reason about the other.
"""
import json
from datetime import date
from typing import Optional, Dict, Any

from qb_entry_rules import (   # cc#2127: reuse, not duplicate -- the same functions cc#2126 built
    _ist_now, monthly_rsi_check, ema_crossover, combine_conditions,
)

__all__ = [
    "hard_stop_pct", "trailing_stop_pct", "gvm_rating_exit", "momentum_rating_exit",
    "monthly_rsi_exit", "ema_crossover_exit", "combine_conditions", "save_exit_ruleset",
]


def ensure_schema(conn):
    """CREATE TABLE only (rule 10). A SEPARATE table from cc#2126's qb_entry_rulesets, not an
    ALTER on it -- MAINTENANCE_LOCK_RULE (rule 10) names ALTER TABLE explicitly as Railway-
    console-only/propose-first, so a same-row entry+exit unification (ADD COLUMN on
    qb_entry_rulesets, or a rename to a shared qb_rulesets) is PROPOSED for cc#2129 to run, not
    executed here, even though a nullable ADD COLUMN would in practice be fast. Same shape as
    qb_entry_rulesets deliberately, so a future merge is a straight column copy, not a reshape."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS qb_exit_rulesets (
              id SERIAL PRIMARY KEY,
              basket_name TEXT NOT NULL,
              ruleset_name TEXT NOT NULL DEFAULT 'default',
              conditions JSONB NOT NULL,
              combinator TEXT NOT NULL DEFAULT 'OR',
              created_at TIMESTAMPTZ DEFAULT NOW(),
              updated_at TIMESTAMPTZ DEFAULT NOW(),
              UNIQUE (basket_name, ruleset_name)
            )""")
        conn.commit()


# ── the six exit conditions ───────────────────────────────────────────────────────────────────

def hard_stop_pct(conn, symbol: str, entry_price: float, as_of: Optional[date] = None,
                   threshold_pct: float = 10) -> Dict[str, Any]:
    """Condition (a), genuinely new. EOD close only, never intraday (founder's own basis for
    every condition in this file and cc#2126's). Fires when the close has fallen threshold_pct%
    or more below entry_price. threshold_pct is founder-editable (10-15%+ named as the range, a
    default not a fixed value, per "we will add up later on")."""
    d = as_of or _ist_now().date()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT price_date, close FROM raw_prices
            WHERE symbol = %s AND price_date <= %s
            ORDER BY price_date DESC LIMIT 1
        """, (symbol, d))
        row = cur.fetchone()
    if not row or not entry_price:
        return {"passes": False, "no_data": True, "cmp": None, "pct_from_entry": None,
                "threshold_pct": threshold_pct}
    price_date, cmp_ = row
    cmp_ = float(cmp_)
    pct = (cmp_ - entry_price) / entry_price * 100.0
    return {"passes": bool(pct <= -threshold_pct), "no_data": False, "cmp": cmp_,
            "cmp_date": str(price_date), "pct_from_entry": round(pct, 2),
            "threshold_pct": threshold_pct}


def trailing_stop_pct(conn, symbol: str, entry_date, as_of: Optional[date] = None,
                       threshold_pct: float = 10) -> Dict[str, Any]:
    """Condition (b), genuinely new -- see module docstring's reconciliation note 2 for why this
    is not a reuse of v12_backtest.py's own (close-based) peak_since_entry. peak_since_entry here
    = MAX(raw_prices.high) over [entry_date, latest EOD date] -- the actual traded HIGH, per the
    card's own explicit formula. Recompute-on-read (scope item 4's own recommendation, chosen
    here): an open position's holding period is at most a few hundred EOD rows, so a single
    MAX(high) aggregate over an indexed (symbol, price_date) range is cheap -- no new mutable
    per-position state, matching "keep the server light" more literally than an incrementally
    maintained cache would. Reports the peak value AND its date, not just the exit boolean, per
    the card's own verify requirement."""
    d = as_of or _ist_now().date()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT price_date, high FROM raw_prices
            WHERE symbol = %s AND price_date BETWEEN %s AND %s
            ORDER BY high DESC LIMIT 1
        """, (symbol, entry_date, d))
        peak_row = cur.fetchone()
        cur.execute("""
            SELECT price_date, close FROM raw_prices
            WHERE symbol = %s AND price_date <= %s
            ORDER BY price_date DESC LIMIT 1
        """, (symbol, d))
        cmp_row = cur.fetchone()
    if not peak_row or not cmp_row:
        return {"passes": False, "no_data": True, "peak": None, "peak_date": None,
                "cmp": None, "pct_from_peak": None, "threshold_pct": threshold_pct}
    peak_date, peak = peak_row
    cmp_date, cmp_ = cmp_row
    peak, cmp_ = float(peak), float(cmp_)
    pct = (cmp_ - peak) / peak * 100.0 if peak else 0.0
    return {"passes": bool(peak and pct <= -threshold_pct), "no_data": False,
            "peak": peak, "peak_date": str(peak_date), "cmp": cmp_, "cmp_date": str(cmp_date),
            "pct_from_peak": round(pct, 2), "threshold_pct": threshold_pct}


def gvm_rating_exit(conn, symbol: str, threshold: float) -> Dict[str, Any]:
    """Condition (c), genuinely new. gvm_history.gvm_score, latest score_date, independent of
    whatever gate the position entered under -- an exit-time re-check, not the entry gate
    reapplied (the card's own distinction)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT gvm_score, score_date FROM gvm_history
            WHERE symbol = %s AND gvm_score IS NOT NULL
            ORDER BY score_date DESC LIMIT 1
        """, (symbol,))
        row = cur.fetchone()
    if not row:
        return {"passes": False, "value": None, "score_date": None, "no_data": True,
                "threshold": threshold}
    value, score_date = row
    v = float(value)
    return {"passes": bool(v < threshold), "value": v, "score_date": str(score_date),
            "threshold": threshold, "no_data": False}


def momentum_rating_exit(conn, symbol: str, threshold: float) -> Dict[str, Any]:
    """Condition (d), genuinely new. gvm_history.m_score, latest score_date -- a stock can fail on
    momentum alone while its overall GVM score still holds, so this is independent of
    gvm_rating_exit (the card's own explicit requirement), not a corollary of it."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT m_score, score_date FROM gvm_history
            WHERE symbol = %s AND m_score IS NOT NULL
            ORDER BY score_date DESC LIMIT 1
        """, (symbol,))
        row = cur.fetchone()
    if not row:
        return {"passes": False, "value": None, "score_date": None, "no_data": True,
                "threshold": threshold}
    value, score_date = row
    v = float(value)
    return {"passes": bool(v < threshold), "value": v, "score_date": str(score_date),
            "threshold": threshold, "no_data": False}


def monthly_rsi_exit(conn, symbol: str, threshold: float = 30) -> Dict[str, Any]:
    """Condition (e). Reuses cc#2126's monthly_rsi_check (renamed from monthly_rsi_above when
    this card needed the same read path in the opposite direction) with direction='below' -- the
    same v8_metrics.rsi_month query, no second implementation."""
    return monthly_rsi_check(conn, symbol, direction="below", threshold=threshold)


def ema_crossover_exit(conn, symbol: str, pair: str = "5_20",
                        as_of: Optional[date] = None, window_days: int = 21) -> Dict[str, Any]:
    """Condition (f). Reuses cc#2126's ema_crossover with direction='bear' -- the bearish mirror
    (ema5_under_ema20 / ema20_under_ema50), the SAME crossover-detection function, not a second
    implementation, per the card's own explicit instruction."""
    return ema_crossover(conn, symbol, pair=pair, direction="bear", as_of=as_of,
                          window_days=window_days)


# ── persistence ───────────────────────────────────────────────────────────────────────────────

def save_exit_ruleset(conn, basket_name: str, conditions: dict, combinator: str = "OR",
                       ruleset_name: str = "default") -> Dict[str, Any]:
    """Persists an exit rule set -- same shape and pattern as cc#2126's save_ruleset. A future
    cc#2129 unification (one ruleset row carrying both entry and exit) needs a proposed, not
    executed here, ALTER TABLE ADD COLUMN on qb_entry_rulesets (or a rename to a shared
    qb_rulesets) -- MAINTENANCE_LOCK_RULE names ALTER TABLE explicitly as propose-first."""
    with conn.cursor() as cur:
        ensure_schema(conn)
        cur.execute("""
            INSERT INTO qb_exit_rulesets (basket_name, ruleset_name, conditions, combinator, updated_at)
            VALUES (%s, %s, %s::jsonb, %s, NOW())
            ON CONFLICT (basket_name, ruleset_name) DO UPDATE SET
                conditions=EXCLUDED.conditions, combinator=EXCLUDED.combinator, updated_at=NOW()
            RETURNING id
        """, (basket_name, ruleset_name, json.dumps(conditions), combinator))
        rid = cur.fetchone()[0]
        conn.commit()
    return {"id": rid, "basket_name": basket_name, "ruleset_name": ruleset_name}
