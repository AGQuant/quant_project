"""
guards.py — cc#217 Phase 2: canonical entry-gate + guard primitives, single-sourced.
=================================================================================
Consolidates logic that was duplicated verbatim across the three v8_signal_writer
auto-entry functions (_auto_paper_entry / _so / _s1b): the trading-day gate, the
09:15-15:20 entry window, and the earnings-blackout / same-side-open / traded-today
guard queries.

DESIGN — behavior-identical AND sim-aware (cc#218):
  • every helper that touches a date takes it as a parameter, so the caller passes
    _today(sim_ts) — byte-identical in live (sim_ts=None) and under the BT7 frozen
    clock. NONE of these use CURRENT_DATE (which would break the sim as-of replay).
  • the SQL is a verbatim lift of the writer's pre-consolidation inline queries — no
    policy change. Certified zero-diff by the BT7 harness on the golden day.

NOTE: v8_paper keeps its own _has_open/_traded_today/_blackout for now (paper_tick is
outside the BT7-certified path); folding those into this module is a separate, verified
follow-up rather than an unverified change riding this push.
"""

from datetime import date, datetime

import nse_holidays

# auto-entry window (IST): open 09:15, hard cut 15:20 — the writer stops opening at 15:20.
ENTRY_OPEN_HM = (9, 15)
ENTRY_CUT_HM  = (15, 20)


def is_trading_day(d: date) -> bool:
    """Canonical NSE trading-day test (cc#211 write-gate). Single source = nse_holidays."""
    return nse_holidays.is_trading_day(d)


def in_entry_window(now_ist: datetime, open_hm=ENTRY_OPEN_HM, cut_hm=ENTRY_CUT_HM) -> bool:
    """True iff now_ist is within [09:15, 15:20] IST. Replaces the three identical
    market-hours blocks in the writer entry fns (mkt_open <= now <= mkt_cut)."""
    lo = now_ist.replace(hour=open_hm[0], minute=open_hm[1], second=0, microsecond=0)
    hi = now_ist.replace(hour=cut_hm[0],  minute=cut_hm[1],  second=0, microsecond=0)
    return lo <= now_ist <= hi


def blackout(conn, sym: str, d: date) -> bool:
    """Earnings blackout: True if `sym` has an ex_date on d or d+1. Sim-aware — the writer
    passes _today(sim_ts) (identical to the old inline `ex_date IN (%s, %s + INTERVAL
    '1 day')` with _today(sim_ts))."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM earnings_calendar
            WHERE UPPER(ticker)=%s AND ex_date IN (%s, %s + INTERVAL '1 day')
            LIMIT 1
        """, (sym.upper(), d, d))
        return cur.fetchone() is not None


def has_open(conn, sym: str, side: str) -> bool:
    """True if an OPEN position exists for symbol/side (verbatim of the writer's inline
    same-side-open guard)."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM v8_paper_positions WHERE symbol=%s AND side=%s AND status='OPEN'",
                    (sym, side))
        return cur.fetchone() is not None


def traded_today(conn, sym: str, side: str, d: date, basket: str = None) -> bool:
    """One entry per symbol/side/day — blocks zone re-entry after TARGET/SL.

    basket=None (standard baskets): checks BOTH closed trades and open positions for the
      day on symbol/side — verbatim of _auto_paper_entry's two inline checks.
    basket set (SO / S1B dedicated pools): checks OPEN positions for the day scoped to that
      basket only — verbatim of the SO/_s1b inline check (positions-only, basket-filtered).
    """
    with conn.cursor() as cur:
        if basket is None:
            cur.execute("SELECT 1 FROM v8_paper_trades WHERE symbol=%s AND side=%s AND entry_ts::date=%s LIMIT 1",
                        (sym, side, d))
            if cur.fetchone():
                return True
            cur.execute("SELECT 1 FROM v8_paper_positions WHERE symbol=%s AND side=%s AND entry_ts::date=%s LIMIT 1",
                        (sym, side, d))
            return cur.fetchone() is not None
        cur.execute("""SELECT 1 FROM v8_paper_positions
                       WHERE symbol=%s AND side=%s AND basket=%s AND entry_ts::date=%s LIMIT 1""",
                    (sym, side, basket, d))
        return cur.fetchone() is not None
