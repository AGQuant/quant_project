"""
qb_entry_rules.py -- cc#2126: Quant Basket Entry Rules V1.

Three EOD entry conditions for basket entry decisions, each independently toggleable, combined
via a per-ruleset AND/OR combinator. Founder words, 16-Sep: "TC score increase -- if TC score
rise above 80 from 75 in past one week. OR monthly RSI above 70. OR EMA crossover -- 20 over 50
or 5 over 20. Yes, OR is right, AND too, we will play around customization."

SCOPE FOR THIS PASS, stated before writing code (cc_task_logs task 2126): builds and hand-verifies
the three condition evaluators, the EOD write job feeding condition (a), the combinator, and a
persistence table for a saved ruleset -- all real, tested against live data. Does NOT splice these
into v12_backtest.py's live _passes_gates()/entry dict yet, and does NOT build a UI. The card's own
correction note assigns the full reconciliation across this card + cc#2127 (exit) + cc#2128 (risk)
+ cc#2123 (universe) to cc#2129 ("wire cc#2123/2126/2127/2128 into the EXISTING v12_backtest.py
engine, then run a first real backtest") -- a one-third-wired splice into a shared engine file now,
revisited twice more as 2127/2128 land, is worse than the one clean integration 2129 already owns.
Every function below returns a plain dict shaped for that integration to consume directly.

CORRECTION FOUND BEFORE BUILDING (data-source gate, Role Split: "CC greps and answers before the
build proceeds") -- THE EOD TABLE NAME IN THE SPEC IS DEAD. The spec says start writing
v8_tc_score_daily. Checked first: that table has zero rows, ever, and v8_pivot_star.py's own
schema comment says why -- "the amendment-superseded daily table, exists empty in the live DB from
the first cut of this card -- flagged for a weekend console DROP, never written." It was abandoned
in favour of v8_tc_score_ticks (cc#1540) BEFORE this card was written -- confirmed independently by
three prior CC investigations (cc#1979 API census, cc#1983 TC consolidation plan, cc#2095
custom_alerts, the same trap already hit once). v8_tc_score_ticks is not a fit either: it scores
only the OPEN BOOK (a position health-check), not the full F&O universe this card's
tc_universe_ticks source actually covers. Writing into a table three independent audits already
flagged for DROP would revive a deliberately-abandoned design and could vanish under that flagged
cleanup with no warning. Built a new, distinctly-named table instead: tc_scanner_score_daily. The
SOURCE the spec names, tc_universe_ticks, is real, healthy, and correctly named -- only the
destination was wrong.

TABLE DESIGN -- a second correction. tc_universe_ticks carries FOUR rows per symbol per tick
(side BUY/SELL x bucket REV/MOM), not the two (per side only) the spec's own literal target schema
implied. Collapsing REV/MOM into one number at WRITE time would destroy information under an
unstated rule. tc_scanner_score_daily keeps the real (symbol, score_date, side, bucket) grain;
tc_score_rise_1w below states its own collapse rule explicitly -- "the TC score" for entry-rule
purposes is MAX(score100) across all four rows for that symbol/day, the strongest signal active in
any direction, matching the founder's own single-number framing without silently discarding the
other three rows from storage.

EVALUATION IS EOD, ON-DEMAND (spec item 5) -- every function below reads a settled prior value
(tc_scanner_score_daily/v8_metrics/raw_prices, all EOD-stamped), none polls or auto-refreshes, and
none is called from a scheduled loop except compute_tc_scanner_score_daily itself.
"""
import os
import json
import logging
from datetime import date, datetime
from typing import Optional, Dict, Any, List

import psycopg
import pytz

from v8_pivot_star import _ema   # cc#2126: reuse the one EMA implementation, not a second one

log = logging.getLogger("qb_entry_rules")
IST = pytz.timezone("Asia/Kolkata")


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def ensure_schema(conn):
    """CREATE TABLE only -- MAINTENANCE_LOCK_RULE (rule 10): no ALTER anywhere in this module."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tc_scanner_score_daily (
              symbol TEXT NOT NULL,
              score_date DATE NOT NULL,
              side TEXT NOT NULL,
              bucket TEXT NOT NULL,
              score100 NUMERIC,
              verdict10 TEXT,
              cmp NUMERIC,
              computed_at TIMESTAMPTZ DEFAULT NOW(),
              PRIMARY KEY (symbol, score_date, side, bucket)
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tc_scanner_score_daily_sym_date "
                    "ON tc_scanner_score_daily(symbol, score_date DESC)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS qb_entry_rulesets (
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


# ── the new EOD write job ─────────────────────────────────────────────────────────────────────

def compute_tc_scanner_score_daily(conn=None, run_date: Optional[date] = None) -> Dict[str, Any]:
    """cc#2126: the ONE new EOD job this card adds. Writes one row per (symbol, side, bucket)
    active in tc_universe_ticks on run_date -- the LAST tick of the day per group, same "the
    15:20 tick IS the EOD value" convention as TC_NO_EOD_COMPUTE_V1 (session_log 42649). Chained
    after gvm_recompute (scheduler.py _bg_gvm), same reasoning screeners_eod/gvm_coverage_guard
    already chain there: by GVM's nightly run every tick for the trading day has long landed
    (market closes 15:30 IST), so chaining costs nothing and avoids a redundant new wall-clock
    slot. Idempotent -- re-running the same run_date overwrites via ON CONFLICT.
    Timezone-safe day boundary: (ts AT TIME ZONE 'Asia/Kolkata')::date, not ts::date, which would
    depend on the connection's session timezone (the same STABLE-vs-IMMUTABLE class of trap
    v8_marker_ticks.py already hit once on an index; harmless here in a WHERE clause but done
    explicitly rather than left to chance)."""
    own = conn is None
    conn = conn or _conn()
    try:
        d = run_date or _ist_now().date()
        with conn.cursor() as cur:
            ensure_schema(conn)
            cur.execute("""
                SELECT DISTINCT ON (symbol, side, bucket)
                       symbol, side, bucket, score100, verdict10, cmp
                FROM tc_universe_ticks
                WHERE (ts AT TIME ZONE 'Asia/Kolkata')::date = %s
                ORDER BY symbol, side, bucket, ts DESC
            """, (d,))
            rows = cur.fetchall()
            for symbol, side, bucket, score100, verdict10, cmp_ in rows:
                cur.execute("""
                    INSERT INTO tc_scanner_score_daily
                        (symbol, score_date, side, bucket, score100, verdict10, cmp, computed_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (symbol, score_date, side, bucket) DO UPDATE SET
                        score100=EXCLUDED.score100, verdict10=EXCLUDED.verdict10,
                        cmp=EXCLUDED.cmp, computed_at=NOW()
                """, (symbol, d, side, bucket, score100, verdict10, cmp_))
            conn.commit()
        return {"score_date": str(d), "rows_written": len(rows)}
    finally:
        if own:
            conn.close()


# ── the three entry conditions ────────────────────────────────────────────────────────────────

def tc_score_rise_1w(conn, symbol: str, as_of: Optional[date] = None,
                      threshold_hi: float = 80, threshold_lo: float = 75,
                      window_sessions: int = 5) -> Dict[str, Any]:
    """Condition (a). "TC score" for this purpose = MAX(score100) across the symbol's four
    (side, bucket) rows on a given day -- see module docstring's TABLE DESIGN note. Passes when
    today's TC score is >= threshold_hi AND was <= threshold_lo at some point in the trailing
    window_sessions -- reports the actual low date/value found, not just a boolean (same evidence
    pattern as cc#2115's DMA_CROSS_WINDOW_1M). Before a full window of history exists, states
    "insufficient history" plainly rather than silently excluding the symbol."""
    d = as_of or _ist_now().date()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT score_date, MAX(score100) AS day_max
            FROM tc_scanner_score_daily
            WHERE symbol = %s AND score_date <= %s
            GROUP BY score_date
            ORDER BY score_date DESC
            LIMIT %s
        """, (symbol, d, window_sessions))
        rows = cur.fetchall()   # most recent first
    if not rows:
        return {"passes": False, "insufficient_history": True, "sessions_collected": 0,
                "current": None, "low_in_window": None, "low_date": None,
                "note": f"insufficient history, 0 of {window_sessions} sessions collected"}
    current_date, current = rows[0]
    if len(rows) < window_sessions:
        return {"passes": False, "insufficient_history": True,
                "sessions_collected": len(rows),
                "current": float(current) if current is not None else None,
                "low_in_window": None, "low_date": None,
                "note": f"insufficient history, {len(rows)} of {window_sessions} sessions collected"}
    low_date, low_val = min(rows, key=lambda r: (r[1] if r[1] is not None else 10 ** 9))
    passes = (current is not None and float(current) >= threshold_hi
              and low_val is not None and float(low_val) <= threshold_lo)
    return {"passes": bool(passes), "insufficient_history": False,
            "sessions_collected": len(rows),
            "current": float(current) if current is not None else None,
            "current_date": str(current_date),
            "low_in_window": float(low_val) if low_val is not None else None,
            "low_date": str(low_date),
            "threshold_hi": threshold_hi, "threshold_lo": threshold_lo,
            "window_sessions": window_sessions}


def monthly_rsi_above(conn, symbol: str, threshold: float = 70) -> Dict[str, Any]:
    """Condition (b). v8_metrics.rsi_month, latest score_date -- deliberately NOT
    universe_technicals (shorter history, only from 2026-07-03; v8_metrics goes back to
    2025-06-02, per the card's own instruction to pick the deeper source -- confirmed, 331 dates)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT rsi_month, score_date FROM v8_metrics
            WHERE symbol = %s AND rsi_month IS NOT NULL
            ORDER BY score_date DESC LIMIT 1
        """, (symbol,))
        row = cur.fetchone()
    if not row:
        return {"passes": False, "value": None, "score_date": None, "no_data": True,
                "threshold": threshold}
    value, score_date = row
    return {"passes": bool(value is not None and float(value) >= threshold),
            "value": float(value) if value is not None else None,
            "score_date": str(score_date), "threshold": threshold, "no_data": False}


_EMA_PAIRS = {"5_20": (5, 20), "20_50": (20, 50)}


def ema_crossover(conn, symbol: str, pair: str = "5_20", as_of: Optional[date] = None,
                   window_days: int = 21) -> Dict[str, Any]:
    """Condition (c). Modelled on evaluate_dma_cross_window()'s pattern (cc#2115,
    v8_pivot_star.py) -- a CROSS, not a state: fires on the session the fast/slow EMA relationship
    FLIPS UP within the trailing window_days trading sessions, not every day it holds (same
    distinction cc#1682/cc#1539 drew for DMA), and reports the most recent such flip. Its own
    function, in this file -- reuses only _ema(), the shared pure EMA-math helper, never
    evaluate_dma_cross_window()/evaluate_dma_state() themselves, so the DMA marker is never at
    risk of a parameterised-reuse regression (the card's own do_not_touch). pair: '5_20' or
    '20_50', matching the founder's own two named pairs ("20 over 50 or 5 over 20")."""
    fast, slow = _EMA_PAIRS[pair]
    d = as_of or _ist_now().date()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT price_date, close FROM raw_prices
            WHERE symbol = %s AND price_date <= %s
            ORDER BY price_date DESC LIMIT %s
        """, (symbol, d, window_days + slow + 5))
        rows = cur.fetchall()   # most recent first
    rows = list(reversed(rows))   # oldest first -- _ema() expects closes in chronological order
    dates = [r[0] for r in rows]
    closes = [float(r[1]) for r in rows]
    if len(closes) < slow + 2:
        return {"passes": False, "insufficient_history": True, "cross_date": None,
                "direction": None, "pair": pair}
    start_i = max(slow, len(closes) - window_days - 1)
    series = []   # [(date, ema_fast, ema_slow)], oldest first, one entry per trading day in window
    for i in range(start_i, len(closes)):
        ef = _ema(closes[:i + 1], fast)
        es = _ema(closes[:i + 1], slow)
        if ef is None or es is None:
            continue
        series.append((dates[i], ef, es))
    cross_date, direction = None, None
    for i in range(1, len(series)):
        prev_diff = series[i - 1][1] - series[i - 1][2]
        cur_diff = series[i][1] - series[i][2]
        if prev_diff <= 0 and cur_diff > 0:
            cross_date, direction = series[i][0], "bull"
        elif prev_diff >= 0 and cur_diff < 0:
            cross_date, direction = series[i][0], "bear"
    return {"passes": cross_date is not None and direction == "bull",
            "insufficient_history": False,
            "cross_date": str(cross_date) if cross_date else None,
            "direction": direction, "pair": pair, "window_days": window_days}


# ── the combinator ────────────────────────────────────────────────────────────────────────────

def combine_conditions(results: List[Dict[str, Any]], combinator: str = "OR") -> Dict[str, Any]:
    """Condition (3), the COMBINATOR. `results` is a list of
    {"key": str, "included": bool, "passes": bool} -- one entry per condition the caller
    evaluated (any subset of tc_score_rise_1w / monthly_rsi_above / ema_crossover). A condition
    with included=False contributes NOTHING -- no ghost AND/OR term, matching the card's own
    verify requirement. combinator: 'ALL' (AND) or 'ANY' (OR) over the INCLUDED conditions only.
    Proof property the card's verify list asks for: running ANY always returns a pass count >=
    running ALL on the same 2+ conditions, since ALL is a subset of ANY over the same set."""
    active = [r for r in results if r.get("included")]
    if not active:
        return {"overall_pass": False, "active_conditions": [], "combinator": combinator,
                "note": "no condition included"}
    combinator = (combinator or "OR").upper()
    combinator = "ALL" if combinator in ("ALL", "AND") else "ANY"
    passes_list = [bool(r.get("passes")) for r in active]
    overall = all(passes_list) if combinator == "ALL" else any(passes_list)
    return {"overall_pass": overall, "combinator": combinator,
            "active_conditions": [r["key"] for r in active],
            "per_condition": {r["key"]: bool(r.get("passes")) for r in active}}


# ── persistence ───────────────────────────────────────────────────────────────────────────────

def save_ruleset(conn, basket_name: str, conditions: dict, combinator: str = "OR",
                  ruleset_name: str = "default") -> Dict[str, Any]:
    """Persists a rule set: which conditions are on, their thresholds, and the combinator --
    reproducible per the card's own requirement. Attaches to (basket_name, ruleset_name), a new
    table, not to cc#2123's universe object: cc#2123 is preview-only/stateless (no saved-filter
    mechanism exists there to attach to -- checked before choosing this table, not assumed), so a
    new table is the honest choice, not a forced reuse of an object that persists nothing today."""
    with conn.cursor() as cur:
        ensure_schema(conn)
        cur.execute("""
            INSERT INTO qb_entry_rulesets (basket_name, ruleset_name, conditions, combinator, updated_at)
            VALUES (%s, %s, %s::jsonb, %s, NOW())
            ON CONFLICT (basket_name, ruleset_name) DO UPDATE SET
                conditions=EXCLUDED.conditions, combinator=EXCLUDED.combinator, updated_at=NOW()
            RETURNING id
        """, (basket_name, ruleset_name, json.dumps(conditions), combinator))
        rid = cur.fetchone()[0]
        conn.commit()
    return {"id": rid, "basket_name": basket_name, "ruleset_name": ruleset_name}
