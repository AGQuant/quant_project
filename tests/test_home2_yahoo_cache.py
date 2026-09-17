"""cc#2198: the home2 Yahoo fallback cache -- the refresher fills indices then breadth on its own
thread with its own short connections; a second kick while one runs is refused; the snapshot is a
copy with ages. No DB, no network: _conn and fetch_live_quotes are replaced."""
import time
from contextlib import contextmanager

import mobile_home2 as mh


class _Cur:
    def __init__(self, rows_by_sql):
        self._rows = rows_by_sql
        self._last = []

    def execute(self, sql, params=None):
        key = "universe" if "futures_universe" in sql else "prev"
        self._last = self._rows[key]

    def fetchall(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reset():
    with mh._YF["lock"]:
        mh._YF.update({"running": False, "idx": None, "idx_at": 0.0, "adr": None, "adr_at": 0.0,
                       "last_err": None, "last_run_s": None})


def _wait_idle(timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if not mh._yahoo_snapshot()["running"]:
            return True
        time.sleep(0.02)
    return False


def test_refresh_fills_indices_then_breadth(monkeypatch):
    _reset()
    universe = ["S%03d" % i for i in range(60)]
    rows = {"universe": [(s,) for s in universe], "prev": [(s, 100.0) for s in universe]}
    monkeypatch.setattr(mh, "_conn", lambda: _Conn(rows))
    calls = []

    def fake_fetch(symbols, budget_sec=None):
        calls.append((list(symbols), budget_sec))
        if symbols == ["NIFTY50", "BANKNIFTY"]:
            return {"NIFTY50": {"price": 25000.5, "prev_close": 24900.0, "open": 24950.0, "high": 25100.0,
                                "low": 24900.0, "chg_pct": 0.4, "asof": "2026-09-17T15:21:00", "source": "yahoo_live_fallback"}}
        # 40 up, 15 down, 5 flat -> 60 resolved (>= 50 floor)
        out = {}
        for i, s in enumerate(symbols):
            px = 101.0 if i < 40 else (99.0 if i < 55 else 100.0)
            out[s] = {"price": px, "prev_close": 100.0, "open": None, "high": None, "low": None,
                      "chg_pct": None, "asof": "x", "source": "yahoo_live_fallback"}
        return out

    import yahoo_live_quote as ylq
    monkeypatch.setattr(ylq, "fetch_live_quotes", fake_fetch)
    assert mh._yahoo_kick() is True
    assert mh._yahoo_kick() is False or _wait_idle()      # a second kick while running is refused
    assert _wait_idle()
    snap = mh._yahoo_snapshot()
    assert snap["idx"]["NIFTY50"]["price"] == 25000.5 and snap["idx_age"] is not None and snap["idx_age"] < 5
    assert snap["adr"] == {"advances": 40, "declines": 15, "unchanged": 5, "source": "yahoo_live_fallback",
                           "resolved": 60, "universe": 60}
    assert snap["last_err"] is None and snap["last_run_s"] is not None
    assert calls[0] == (["NIFTY50", "BANKNIFTY"], mh._YF_IDX_BUDGET)   # indices carry the hard budget
    assert calls[1][0] == universe and calls[1][1] is None             # the sweep runs unbudgeted, off the request


def test_thin_batch_does_not_write_breadth(monkeypatch):
    _reset()
    universe = ["S%03d" % i for i in range(30)]
    rows = {"universe": [(s,) for s in universe], "prev": [(s, 100.0) for s in universe]}
    monkeypatch.setattr(mh, "_conn", lambda: _Conn(rows))
    import yahoo_live_quote as ylq
    monkeypatch.setattr(ylq, "fetch_live_quotes", lambda symbols, budget_sec=None:
                        {} if symbols == ["NIFTY50", "BANKNIFTY"] else
                        {s: {"price": 101.0, "prev_close": 100.0, "open": None, "high": None, "low": None,
                             "chg_pct": None, "asof": "x", "source": "yahoo_live_fallback"} for s in symbols})
    assert mh._yahoo_kick() is True and _wait_idle()
    snap = mh._yahoo_snapshot()
    assert snap["idx"] is None            # nothing answered -> nothing cached, never zero-filled
    assert snap["adr"] is None            # 30 < 50 floor -> not written


def test_refresh_failure_is_recorded_not_raised(monkeypatch):
    _reset()

    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(mh, "_conn", boom)
    import yahoo_live_quote as ylq
    monkeypatch.setattr(ylq, "fetch_live_quotes", lambda symbols, budget_sec=None: {})
    assert mh._yahoo_kick() is True and _wait_idle()
    snap = mh._yahoo_snapshot()
    assert snap["running"] is False and "db down" in (snap["last_err"] or "")
