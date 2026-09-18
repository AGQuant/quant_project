"""cc#2214 V8 LIVE TC: the bucket tabs' TC /100 comes from the resolver's scorer for every qualified row --
best of four on the cc#1033 ratio (the resolver's own best_card), side fields per tab, an explicit
tc_error when there is no honest number, one scorer call per symbol across the four basket calls,
failures never cached. No DB, no network -- a fake scorer stands in for tc_v4_dual."""
import pytest

import v8_live_tc as m
from tc_resolver import get_primary_best_card

BEST = get_primary_best_card()   # the real selection rule (score / max), through the resolver


def card(label, side, score, mx, score100, verdict="VALID", weighted=True):
    return {"style": label.split("-")[1], "side": side, "label": label, "score": score, "max": mx,
            "score100": score100, "verdict10": verdict, "score10_weighted": weighted}


RES = {"symbol": "X", "cards": [
    card("BUY-REV", "BUY", 12.0, 20.0, 60.0),          # ratio 0.60
    card("BUY-MOM", "BUY", 10.0, 21.0, 47.6, "REJECT"),  # 0.476
    card("SELL-REV", "SELL", 5.0, 11.0, 45.5, "REJECT"),  # 0.4545
    card("SELL-MOM", "SELL", 7.0, 9.0, 77.8, "STRONG"),   # 0.778  <- best by ratio
]}


@pytest.fixture(autouse=True)
def _fresh():
    m.clear_cache()
    yield
    m.clear_cache()


def test_best_of_four_by_ratio_and_side_fields_for_a_buy_tab():
    f = m.fields_from_result(RES, "BUY", BEST)
    assert f["tc_score"] == 77.8 and f["tc_bucket"] == "SELL-MOM" and f["tc_band"] == "STRONG"
    assert f["tc_opposes"] is True                      # strongest card is against a BUY tab
    assert f["tc_side_bucket"] == "BUY-REV" and f["tc_side_score"] == 60.0
    assert f["tc_error"] is None and f["tc_weighted"] is True


def test_same_result_seen_from_a_sell_tab_does_not_oppose():
    f = m.fields_from_result(RES, "SELL", BEST)
    assert f["tc_bucket"] == "SELL-MOM" and f["tc_opposes"] is False
    assert f["tc_side_bucket"] == "SELL-MOM" and f["tc_side_score"] == 77.8


def test_error_shapes_are_explicit_never_a_bare_null():
    assert m.fields_from_result({"error": "no metrics row"}, "BUY", BEST) == {"tc_score": None, "tc_error": "no metrics row"}
    assert m.fields_from_result(None, "BUY", BEST)["tc_error"] == "trade check returned no result"
    assert m.fields_from_result({"cards": []}, "BUY", BEST)["tc_error"] == "no card scored"
    f = m.fields_from_result({"cards": [card("BUY-REV", "BUY", 12.0, 20.0, None)]}, "BUY", BEST)
    assert f["tc_score"] is None and "BUY-REV" in f["tc_error"] and "12.0/20.0" in f["tc_error"]


def test_resolve_many_scores_each_symbol_once_and_isolates_a_raising_symbol():
    calls = []

    def scorer(sym, side):
        calls.append((sym, side))
        if sym == "BAD":
            raise RuntimeError("boom")
        return dict(RES, symbol=sym)

    out = m.resolve_many(["INFY", "BAD", "INFY", None], "SELL", scorer=scorer, best_card=BEST)
    assert calls == [("INFY", "ALL"), ("BAD", "ALL")]
    assert out["INFY"]["tc_score"] == 77.8 and out["INFY"]["tc_error"] is None
    assert out["BAD"]["tc_score"] is None and out["BAD"]["tc_error"] == "RuntimeError: boom"
    # a second basket (other side) reuses the cached raw result for INFY and retries BAD
    out2 = m.resolve_many(["INFY", "BAD"], "BUY", scorer=scorer, best_card=BEST)
    assert calls == [("INFY", "ALL"), ("BAD", "ALL"), ("BAD", "ALL")]
    assert out2["INFY"]["tc_opposes"] is True and out["INFY"]["tc_opposes"] is False


def test_attach_live_tc_mutates_rows_and_counts():
    rows = [{"symbol": "LTM"}, {"symbol": "NOPE"}, {"symbol": "MPHASIS"}]

    def scorer(sym, side):
        return {"error": "no data"} if sym == "NOPE" else dict(RES, symbol=sym)

    meta = m.attach_live_tc(rows, "sell_reversal", scorer=scorer, best_card=BEST)
    assert meta["resolved"] == 2 and meta["failed"] == 1 and meta["engine"] == m.ENGINE and meta["as_of"]
    assert rows[0]["tc_score"] == 77.8 and rows[0]["tc_source"] == "live" and rows[0]["tc_ts"] == meta["as_of"]
    assert rows[1]["tc_score"] is None and rows[1]["tc_error"] == "no data"
    assert m.attach_live_tc([], "buy_momentum") == {"resolved": 0, "failed": 0, "engine": m.ENGINE}


def test_side_for():
    assert m.side_for("sell_reversal") == "SELL" and m.side_for("sell_momentum") == "SELL"
    assert m.side_for("buy_reversal") == "BUY" and m.side_for("buy_momentum") == "BUY"
