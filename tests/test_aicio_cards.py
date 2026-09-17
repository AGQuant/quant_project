"""cc#2171 -- AICIO V1 cards rail (GET /api/mobile/aicio_app/cards).

What is pinned here, with every source stubbed (no database, no network):
  * the registry is the one list, in order, and every entry has a callable builder + an /m/ href;
  * build_cards isolates a builder that raises (available:false + the reason, the other cards untouched)
    and one that overruns the timeout;
  * the three real builders map their source payloads to the card shape (headline, two lines, as-of);
  * the endpoint payload counts available/total and carries the feed state.
"""
import os
import sys
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

aam = pytest.importorskip("aicio_app_mobile")

MOOD = {"checked_at": "2026-09-17", "mood": "Neutral", "fails": 2, "buy_slots": 12, "sell_slots": 8,
        "nifty_source": "live_intraday",
        "checks": [{"filter": "ADR", "value": 3.98, "pass": True, "indeterminate": False},
                   {"filter": "Nifty Day", "value": 0.36, "pass": True},
                   {"filter": "Nifty Week", "value": -0.4, "pass": False},
                   {"filter": "Nifty Month", "value": -1.2, "pass": False}],
        "adr_detail": {"advances": 167, "declines": 42, "unchanged": 5, "adr_date": "2026-09-17"}}
SIG = {"NIFTY50": {"status": "ok", "symbol": "NIFTY50", "as_of": "2026-09-17 12:30:00", "price": 23347.0,
                   "st_dir": "up", "signal": "FLAT", "gate_zone": "buy"},
       "BANKNIFTY": {"status": "ok", "symbol": "BANKNIFTY", "as_of": "2026-09-17 12:30:00", "price": 56337.8,
                     "st_dir": "up", "signal": "FLAT", "gate_zone": "buy"}}
CANON = {"long": {"n": 9}, "short": {"n": 3}, "unrealised": -12450.5, "realised": 284310.0,
         "win_rate": 58.3, "decided": 96, "trades": 104}


class _Cur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def cursor(self):
        return _Cur()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub_sources(monkeypatch):
    monkeypatch.setitem(sys.modules, "v8_endpoints", types.SimpleNamespace(market_mood=lambda: MOOD))
    monkeypatch.setitem(sys.modules, "v10_endpoints", types.SimpleNamespace(v10_signal=lambda sym="NIFTY50": SIG[sym]))
    monkeypatch.setitem(sys.modules, "mobile_endpoints", types.SimpleNamespace(
        _conn=lambda: _Conn(), _guard=lambda r: None, _json_safe=lambda f: f, _page=lambda n: None,
        rail_state=lambda last, cad, now, td: {"state": "LIVE" if td else "CLOSED", "age_min": 3.0, "why": "tick 3 min ago"}))
    monkeypatch.setitem(sys.modules, "v8_book_canon", types.SimpleNamespace(book_canon=lambda conn, era="fresh": CANON))
    monkeypatch.setitem(sys.modules, "v8_era", types.SimpleNamespace(era_block=lambda cur: {"era_label": "Fresh era since 18 Jul 2026"}))
    monkeypatch.setitem(sys.modules, "nse_holidays", types.SimpleNamespace(is_trading_day=lambda d: True))
    monkeypatch.setattr(aam, "_guard", lambda r: None)


def test_registry_is_one_ordered_list():
    assert [c["key"] for c in aam.AICIO_CARDS] == ["market_mood", "index_intel", "v8_book"]
    assert all(callable(c["builder"]) and c["href"].startswith("/m/") and c["title"] and c["source"] for c in aam.AICIO_CARDS)


def test_inr_is_indian_grouped_and_signed():
    assert aam._inr(579000) == "₹5,79,000"
    assert aam._inr(-1234567.4) == "−₹12,34,567"
    assert aam._inr(950) == "₹950"
    assert aam._inr(None) == "—"
    assert aam._inr("x") == "—"


def test_build_cards_isolates_a_failing_and_a_slow_builder():
    def ok():
        return {"headline": "Neutral", "lines": ["a", "b"], "as_of": "x"}

    def boom():
        raise RuntimeError("feed dead")

    def slow():
        time.sleep(2)
        return {"headline": "late"}

    reg = [{"key": "a", "title": "A", "builder": ok, "href": "/m/home", "source": "s"},
           {"key": "b", "title": "B", "builder": boom, "href": "/m/v10", "source": "s"},
           {"key": "c", "title": "C", "builder": slow, "href": "/m/v8", "source": "s"}]
    cards = aam.build_cards(reg, timeout_s=0.5)
    assert [c["key"] for c in cards] == ["a", "b", "c"], "registry order is kept"
    assert cards[0]["available"] and cards[0]["headline"] == "Neutral" and "built_ms" in cards[0]
    assert not cards[1]["available"] and cards[1]["reason"] == "RuntimeError: feed dead" and cards[1]["href"] == "/m/v10"
    assert not cards[2]["available"] and cards[2]["reason"].startswith("timed out after")


def test_builders_map_their_sources(monkeypatch):
    _stub_sources(monkeypatch)
    mm = aam._card_market_mood()
    assert mm["headline"] == "Neutral"
    assert mm["lines"] == ["2 of 4 checks passed · ADR 3.98", "12 buy / 8 sell slots · A/D 167/42"]
    assert "live intraday" in mm["as_of"]
    ii = aam._card_index_intel()
    assert ii["headline"] == "NIFTY 23,347 · up"
    assert ii["lines"][0] == "NIFTY50 23,347 · trend up · signal FLAT"
    assert ii["lines"][1].startswith("BANKNIFTY 56,338")
    assert ii["as_of"].startswith("2026-09-17 12:30")
    vb = aam._card_v8_book()
    assert vb["headline"] == "−₹12,450"
    assert vb["lines"] == ["12 open · unrealised −₹12,450", "realised ₹2,84,310 · win rate 58.3% of 96 decided"]
    assert vb["raw"]["open"] == 12 and "Fresh era" in vb["as_of"]


def test_endpoint_counts_available_cards(monkeypatch):
    _stub_sources(monkeypatch)
    out = aam.mobile_aicio_cards(None)
    assert out["total"] == 3 and out["available"] == 3
    assert [c["key"] for c in out["cards"]] == ["market_mood", "index_intel", "v8_book"]
    assert out["feed"]["state"] == "LIVE" and out["as_of"].endswith("IST") and out["version"].startswith("V1")

    def boom():
        raise RuntimeError("feed dead")

    monkeypatch.setitem(aam.AICIO_CARDS[0], "builder", boom)
    out2 = aam.mobile_aicio_cards(None)
    assert out2["available"] == 2 and out2["total"] == 3
    assert not out2["cards"][0]["available"] and out2["cards"][0]["reason"] == "RuntimeError: feed dead"
    assert out2["cards"][1]["available"] and out2["cards"][2]["available"]
