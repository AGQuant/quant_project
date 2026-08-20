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
import v8_timing_rules   # cc#1138: V8_TIMING_RULES_V1, session_log 27321

# auto-entry window (IST): open 09:15, hard cut 15:15 — the writer stops opening at 15:15.
# cc#855: cut moved 15:20 -> 15:15. SEBI's Closing Auction Session (live 03-Aug-2026) ends
# CONTINUOUS trading for F&O-eligible cash stocks at 15:15; 15:15-15:30 is order collection only.
# A 15:20 entry therefore had no continuous market to execute against — it would have been priced
# off a dead tape (15:15 and 15:20 closes were byte-identical on NIFTY50/BANKNIFTY/FEDERALBNK/
# SONACOMS on both 03-Aug and 04-Aug, which never happened 20-Jul to 31-Jul) or, worse, off the
# auction print. Equity derivatives still trade to 15:40, but the basket entries are cash-referenced,
# so the binding constraint is the cash continuous close.
ENTRY_OPEN_HM = (9, 15)
ENTRY_CUT_HM  = (15, 15)


def is_trading_day(d: date) -> bool:
    """Canonical NSE trading-day test (cc#211 write-gate). Single source = nse_holidays."""
    return nse_holidays.is_trading_day(d)


def in_entry_window(now_ist: datetime, open_hm=ENTRY_OPEN_HM, cut_hm=ENTRY_CUT_HM) -> bool:
    """True iff now_ist is within [09:15, 15:15] IST AND at or after the 09:30 entry cool-off.

    cc#1138 rule 1 (session_log 27321, V8_TIMING_RULES_V1): no new entries before 09:30 IST.

    WIRED HERE BECAUSE THIS IS THE CHOKE POINT, and that is a grep result, not a guess. Every
    live automatic entry in the system reaches a position through
    v8_signal_writer._auto_paper_entry, which calls this function once at its top (line ~1379)
    before any basket branch runs — so one edit covers all four baskets and there is no fifth
    path to miss. The alternative, editing each basket's entry branch, is the shape of mistake
    cc#847 exists to prevent.

    THE WINDOW CONSTANT IS DELIBERATELY NOT MOVED. ENTRY_OPEN_HM stays (9,15): it describes when
    the market opens, which has not changed, and BT7's golden-day certification is keyed to it.
    The cool-off is a separate, later rule and is asked separately, so the two can be read — and
    reverted — independently.

    SIM-SAFE. now_ist is passed in by the caller, so under the BT7 frozen clock the cool-off is
    evaluated against simulated time exactly as the window already is. Nothing here reads a
    wall clock.

    EXITS ARE UNAFFECTED. This function is consulted only on entry paths; stop, target and gap
    monitoring never call it, so an open position is still watched from the first tick of the day.
    """
    lo = now_ist.replace(hour=open_hm[0], minute=open_hm[1], second=0, microsecond=0)
    hi = now_ist.replace(hour=cut_hm[0],  minute=cut_hm[1],  second=0, microsecond=0)
    return lo <= now_ist <= hi and v8_timing_rules.entries_allowed(now_ist)


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
