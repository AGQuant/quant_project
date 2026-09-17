"""cc#2198: fetch_live_quotes(budget_sec=...) is a HARD overall budget -- whatever answered inside
it is returned, the rest is cancelled and absent (never fabricated). No network: _fetch_one is
replaced by a coroutine that takes a fixed time per symbol under the same semaphore."""
import asyncio
import time

import yahoo_live_quote as ylq


def _slow(delay):
    async def fake_fetch_one(client, sem, nse_sym, fetched_at):
        async with sem:
            await asyncio.sleep(delay)
            return nse_sym, {"price": 100.0, "prev_close": 99.0, "open": 99.5, "high": 101.0,
                             "low": 99.0, "chg_pct": 1.01, "asof": fetched_at, "source": ylq.SOURCE_TAG}
    return fake_fetch_one


def test_budget_returns_partial_and_stops_on_time(monkeypatch):
    monkeypatch.setattr(ylq, "_fetch_one", _slow(0.3))
    syms = ["A", "B", "C", "D", "E", "F", "G", "H", "I"]      # 3-wide: 3 answer by 0.3 s, 6 by 0.6 s ...
    t0 = time.monotonic()
    out = ylq.fetch_live_quotes(syms, budget_sec=0.5)
    took = time.monotonic() - t0
    assert set(out) == {"A", "B", "C"}, out.keys()
    assert took < 1.5, took                                     # the budget bounds the wall, the list length does not
    assert all(q["price"] == 100.0 for q in out.values())


def test_no_budget_returns_everything(monkeypatch):
    monkeypatch.setattr(ylq, "_fetch_one", _slow(0.01))
    out = ylq.fetch_live_quotes(["A", "B", "C", "D"])
    assert set(out) == {"A", "B", "C", "D"}


def test_empty_list_short_circuits():
    assert ylq.fetch_live_quotes([], budget_sec=0.1) == {}
