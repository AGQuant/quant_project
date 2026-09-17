"""cc#2203: the cash-leg outage gate is closed once continuous cash trading has ended (15:15 IST,
the CAS window) -- the fyers_eq source is idle by design then, while fyers_eq_auction carries the
leg. No DB: _leg_ages is replaced."""
from datetime import datetime

import yahoo_live_quote as ylq


def _at(h, m, age, monkeypatch, weekday_date="2026-09-17"):
    monkeypatch.setattr(ylq, "_leg_ages", lambda cur, now: {"fyers_eq": age, "fyers_fut": 0.3})
    y, mo, d = (int(x) for x in weekday_date.split("-"))
    return ylq.fyers_eq_outage(None, datetime(y, mo, d, h, m, 0))


def test_stale_inside_continuous_session_is_an_outage(monkeypatch):
    assert _at(14, 50, 12.0, monkeypatch) == (True, 12.0)


def test_fresh_inside_continuous_session_is_not(monkeypatch):
    assert _at(15, 10, 5.0, monkeypatch) == (False, 5.0)


def test_auction_window_is_never_an_outage(monkeypatch):
    assert _at(15, 22, 12.0, monkeypatch) == (False, 12.0)      # 17-Sep's 15:20 case
    assert _at(15, 29, 18.0, monkeypatch) == (False, 18.0)


def test_last_continuous_minute_still_gates(monkeypatch):
    assert _at(15, 15, 11.0, monkeypatch) == (True, 11.0)       # 15:15:00 is still continuous


def test_weekend_and_after_close_are_not(monkeypatch):
    assert _at(15, 22, 40.0, monkeypatch, weekday_date="2026-09-20") == (False, None)   # Sunday
    assert _at(15, 45, 40.0, monkeypatch) == (False, None)
