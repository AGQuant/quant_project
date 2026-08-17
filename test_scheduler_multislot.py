"""
test_scheduler_multislot.py — cc#1085 R6-P9 verify, as a test rather than a claim.

WHAT BROKE. classify_cadence() kept the FIRST `h ==` and the FIRST `m ==` in a cadence string and
discarded the rest. Six ACTIVE registry rows declare more than one exact-minute slot, so six jobs
were judged — for staleness AND for catch-up — on a fraction of their schedule.

WHY IT MATTERED MOST FOR bg_protocol_one. Its cadence is "15:40 ; 09:20", so the only slot the old
parser saw was 15:40. Before 15:40 on any day, expected_last_run() therefore answered "last Friday
15:40", which always sits BELOW today's period floor — and the catch-up sweep skips anything whose
expected slot predates the floor. So that job could never be caught up, on any day, at any hour.
That is a different failure from a deploy landing on the minute: the recovery path was closed.

Run: python3 test_scheduler_multislot.py   (stubs psycopg/fastapi; no DB)
"""

import sys
import types
from datetime import datetime, timedelta, timezone


def _stub():
    for n, a in [
        ("psycopg", {"connect": lambda *x, **k: None}),
        ("fastapi", {"APIRouter": lambda **k: types.SimpleNamespace(
            get=lambda *x, **k: (lambda f: f), post=lambda *x, **k: (lambda f: f)),
            "HTTPException": type("H", (Exception,), {}),
            "Query": lambda *x, **k: None, "Body": lambda *x, **k: None}),
        ("fastapi.responses", {"JSONResponse": object, "HTMLResponse": object,
                               "PlainTextResponse": object}),
    ]:
        try:
            __import__(n)
        except ImportError:
            m = types.ModuleType(n)
            for k, v in a.items():
                setattr(m, k, v)
            sys.modules[n] = m


_stub()
import scheduler_master as sm   # noqa: E402

IST = sm.IST

# The six real multi-slot rows, verbatim from scheduler_master where active and scheduler_loop.
REAL = [
    ("bg_protocol_one",
     "_is_trading_day(today) and h == 15 and (m == 40); _is_trading_day(today) and h == 9 and (m == 20)",
     [(9, 20), (15, 40)]),
    ("bg_oi_snapshot",
     "now.weekday() < 5 and _is_trading_day(now.date()) and (h == 9 and m == 20 or (h == 15 and m == 35))",
     [(9, 20), (15, 35)]),
    ("bg_yahoo_daily_sync",
     "h == 1 and m == 0; now.weekday() < 5 and h == 15 and (m == 35)", [(1, 0), (15, 35)]),
    ("bg_fetch_universe_reco_news", "h == 21 and m == 10; h == 3 and m == 10",
     [(3, 10), (21, 10)]),
    ("bg_nse_eod_ingest",
     "now.weekday() < 5 and h == 18 and (m == 30); now.weekday() < 5 and h == 19 and (m == 30); "
     "now.weekday() < 5 and h == 20 and (m == 30)", [(18, 30), (19, 30), (20, 30)]),
    ("bg_fetch_market_news",
     "_is_market_hours(now) and _is_trading_day(now.date()) and (m % 5 == 0) AND m % 15 == 0; h == 5 and m == 20",
     [(5, 20)]),
]

# Single-slot rows must be completely unaffected. An `m ==` with no `h` before it is an hourly
# loop, not a slot, and must NOT be promoted into one.
SINGLE = [
    ("bg_ca_daily_note", "h == 9 and m == 0", [(9, 0)]),
    ("bg_gvm_backfill", "h == 2 and m == 20; m == 25 and (not _is_session_live(now))", [(2, 20)]),
    ("bg_signal_writer", "_is_cash_continuous(now) and m % 5 == 0; _restart_requested AND _is_cash_continuous(now)", []),
]


def is_td(d):
    return d.weekday() < 5


def main() -> int:
    ok = True

    print("SLOT PARSING — six real multi-slot registry rows")
    for name, cad, want in REAL + SINGLE:
        got = sm.classify_cadence(cad).get("slots")
        good = got == want
        print("  %-28s %-22s %s" % (name, got, "OK" if good else "FAIL want %s" % (want,)))
        ok = ok and good

    # The regression that closed the recovery path. Monday 17-Aug 14:30 IST, before the 15:40 slot.
    print("\nTHE bg_protocol_one REGRESSION — catch-up eligibility at 14:30 IST on a Monday")
    now = datetime(2026, 8, 17, 14, 30, tzinfo=IST)
    cad = REAL[0][1]
    exp = sm.expected_last_run(cad, now, is_td)
    floor = now.replace(hour=0, minute=0, second=0, microsecond=0)
    print("  expected_last_run = %s" % exp)
    print("  period floor      = %s" % floor)
    checks = [
        ("resolves to TODAY's 09:20, not last Friday's 15:40",
         exp == datetime(2026, 8, 17, 9, 20, tzinfo=IST)),
        ("sits at or above the period floor, so catch-up can see it", exp is not None and exp >= floor),
    ]

    # After 15:40 the later slot must win, or the evening run would look due at the morning slot.
    later = sm.expected_last_run(cad, datetime(2026, 8, 17, 16, 0, tzinfo=IST), is_td)
    checks.append(("after 15:40 the LATER slot wins",
                   later == datetime(2026, 8, 17, 15, 40, tzinfo=IST)))

    # Before the first slot of the day it must fall back to the previous WEEKDAY's last slot -
    # never to a Saturday, and never forward into a slot that has not happened.
    early = sm.expected_last_run(cad, datetime(2026, 8, 17, 8, 0, tzinfo=IST), is_td)
    checks.append(("before 09:20 it falls back to Friday's 15:40",
                   early == datetime(2026, 8, 14, 15, 40, tzinfo=IST)))
    checks.append(("never returns a future instant",
                   all(x is None or x <= n for x, n in
                       [(exp, now), (later, datetime(2026, 8, 17, 16, 0, tzinfo=IST)),
                        (early, datetime(2026, 8, 17, 8, 0, tzinfo=IST))])))

    for label, passed in checks:
        print("    %-52s %s" % (label, "OK" if passed else "FAIL"))
        ok = ok and passed

    # Single-slot behaviour must be byte-for-byte what it was, or this fix has cost more than it won.
    print("\nSINGLE-SLOT JOBS UNCHANGED")
    for name, cad, _ in SINGLE[:2]:
        k = sm.classify_cadence(cad)
        e = sm.expected_last_run(cad, now, is_td)
        want = sm._prev_weekday_at(now, k["hh"], k["mm"])
        good = e == want
        print("  %-28s expected=%s  %s" % (name, e, "OK" if good else "FAIL want %s" % want))
        ok = ok and good

    # The whole registry, in one sweep: nothing may start returning a future instant.
    print("\nNO CADENCE RESOLVES INTO THE FUTURE")
    bad = []
    for name, cad, _ in REAL + SINGLE:
        for hour in (0, 6, 9, 12, 15, 18, 23):
            t = datetime(2026, 8, 17, hour, 30, tzinfo=IST)
            e = sm.expected_last_run(cad, t, is_td)
            if e is not None and e > t:
                bad.append((name, hour, e))
    print("  checked %d cadences x 7 times of day: %s"
          % (len(REAL + SINGLE), "none in the future" if not bad else "FAIL %s" % bad))
    ok = ok and not bad

    print("\nMULTI-SLOT OK" if ok else "\nMULTI-SLOT FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
