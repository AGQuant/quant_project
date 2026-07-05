"""
sim_clock.py — cc#218 (BT7 parity harness): the ONE canonical injectable clock.
============================================================================
RULE 3 (CTO-locked): every now()/today()/CURRENT_DATE in the BT7-driven live path
(v8_signal_writer + v8_paper) resolves through _now(sim_ts). No monkeypatching, no
external clock interception — a plain parameter threaded through the real functions.

    sim_ts = None   -> live: datetime.now(IST) (naive IST) — BYTE-IDENTICAL to the
                       pre-cc#218 behavior. This is the invariant that makes the diff
                       safe to deploy: the live path is provably unchanged.
    sim_ts = <dt>   -> frozen clock: all time reads resolve to sim_ts, so the harness
                       can walk 09:15->15:30 in 5-min steps over a past (golden) day.

Point-in-time note (Rule 5): callers that read intraday bars add `AND ts <= _now(sim_ts)`.
In live this is a no-op (bars only exist up to now); in sim it is the as-of cutoff.
DB session is UTC; the writer/paper functions only run during market hours
(09:15-15:30 IST = 03:45-10:00 UTC, same calendar day), so a param bound to
_today(sim_ts) equals the SQL CURRENT_DATE they replace.
"""

from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def _now(sim_ts=None):
    """Naive-IST 'now': sim_ts when frozen, else the real wall clock (live default)."""
    return sim_ts if sim_ts is not None else datetime.now(IST).replace(tzinfo=None)


def _today(sim_ts=None):
    """Naive-IST 'today' date."""
    return _now(sim_ts).date()
