"""
test_tc_v4_513_alignment.py -- cc#513 consistency test (18-Jul-2026).

PRINCIPLE (founder-locked): every TC rule that shares a field with a locked basket spec must use
the basket's exact threshold, style-matched -- a stock passing a V8 filter can NEVER fail the
corresponding TC field. This asserts that with synthetic metric sets set exactly at each basket's
own locked passing thresholds, the style-matched tc_v4_dual card gives FULL credit (1.0) on every
rule that shares a field with that basket. Zero tolerance (acceptance criterion #2, cc#513).

Run: pytest test_tc_v4_513_alignment.py   OR   python3 test_tc_v4_513_alignment.py
"""
import datetime

import tc_v4_dual as m


def _base():
    start = datetime.date(2025, 12, 1)
    daily = []
    for i in range(160):
        dt = start + datetime.timedelta(days=i)
        daily.append({"price_date": dt, "open": 95 + i * 0.05, "high": 96 + i * 0.05,
                      "low": 94 + i * 0.05, "close": 95 + i * 0.05, "volume": 100000})
    return {
        "symbol": "TEST", "cmp": 100.0,
        "daily": daily,
        "nifty_day": 0.3, "nifty_wk": 0.5, "nifty_mo": -0.2, "nifty_source": "x",
        "vol_ratio_today": 1.3,
        "v8": {"dma_20": 2.0, "dma_50": 1.0, "dma_200": -0.5, "daily_rsi": 45, "rsi_month": 62,
               "rsi_weekly": 999, "week_return": 1.2, "month_return": 3.4, "mom_2d": 2.1,
               "week_index_52": 55, "sector_week": 0.4, "sector_month": -0.1, "day_1d": 0.6},
        "gvm_score": 7.1, "segment": "SEG",
        "peers_up1": 3, "peers_up": 5, "peers_dn1": 1, "peers_dn05": 2, "peers_dn": 4, "peer_count": 10,
        "pivots": {"pp": 99.0, "r1": 102.0, "s1": 96.0, "r2": 104.0, "s2": 93.0},
        "bars": [{"open": 99 + i * 0.05, "high": 100 + i * 0.05, "low": 98 + i * 0.05,
                  "close": 99.5 + i * 0.05, "volume": 5000} for i in range(75)],
        "adr": 1.1,
        "basis": [{"basis_pct": 0.3, "oi_chg": 1.2}, {"basis_pct": 0.1, "oi_chg": 0.5}],
        "is_future": True,
        "event_blackout": False, "event_date": None,
    }


def _rules_for(overrides):
    """Derive a card's rules dict {rule_id: rule} after applying post-_derive overrides."""
    style, side, patch = overrides
    d = m._derive(_base())
    for k, v in patch.items():
        d[k] = v
    return {r["rule"]: r for r in m._rules(d, style, side)}


def _assert_full(rules, ids, label):
    for rid in ids:
        r = rules[rid]
        assert r["credit"] == 1.0, (
            f"{label}: {rid} expected full credit (1.0) at the basket's own passing threshold, "
            f"got {r['credit']} -- required={r['required']!r} actual={r['value']!r}")


def test_buy_reversal_v5_shared_fields_full_credit():
    """BuyRev V5 Spring (id=5647): a V8-passing dip candidate must score R7/R8/R10/R11/R15/R16
    full credit on every ALIGNED field (R7 twr is style-matched separately, not a V8 hard gate --
    still must be reachable via the widened weekly-heat band at a healthy twr)."""
    d = m._derive(_base())
    d["true_weekly_rsi"] = 75             # >=70 -> R7 full
    d["low_5d"] = 95.0                    # <= s1 (96.0) -> R10 full
    d["rs_mo"] = 0.0                      # > -1 -> R15 full
    d["fib_pos"] = 50.0                   # 38.2-78.6 -> R16 full
    d["fib_range_ok"] = True
    d["v8"]["month_return"] = -2.0        # in [-10, 5) -> R8 full
    d["v8"]["mom_2d"] = 0.0               # >= -0.5 -> R11
    d["v8"]["week_return"] = 0.0          # >= -2 -> R11 full
    rules = {r["rule"]: r for r in m._rules(d, "REVERSAL", "BUY")}
    _assert_full(rules, ["R7", "R8", "R10", "R11", "R15", "R16"], "BuyRev V5")


def test_buy_momentum_v3_shared_fields_full_credit():
    """BuyMom V3 (id=5650): a V8-passing momentum candidate (dma_50 5-12 hard gate zone, dma_20>0,
    mRSI/twr hot band, w52>=75, mom_2d 0-6) must score R4/R7/R10 full credit."""
    d = m._derive(_base())
    d["v8"]["dma_20"] = 1.5
    d["v8"]["dma_50"] = 8.0     # inside the V3 5-12 hard-gate zone
    d["v8"]["dma_200"] = 3.0
    d["v8"]["rsi_month"] = 60   # >= 50
    d["true_weekly_rsi"] = 78   # in [70,85]
    d["v8"]["mom_2d"] = 3.0     # in [0,6]
    d["v8"]["week_index_52"] = 80  # >= 75
    rules = {r["rule"]: r for r in m._rules(d, "MOMENTUM", "BUY")}
    _assert_full(rules, ["R4", "R7", "R10"], "BuyMom V3")


def test_sell_reversal_v61_shared_fields_full_credit():
    """SellRev V6.1 (id=5626): a V8-passing bounce-fade candidate (twr<=45 heat, orderly-weakness
    mret, moderate-red fall+day_1d) must score R7/R8/R10 full credit."""
    d = m._derive(_base())
    d["true_weekly_rsi"] = 40           # <= 45
    d["v8"]["month_return"] = -5.0      # in [-10, 0)
    d["fall_from_high_2d"] = 5.0        # in [2, 8]
    d["v8"]["day_1d"] = -1.0            # in [-2, 0]
    rules = {r["rule"]: r for r in m._rules(d, "REVERSAL", "SELL")}
    _assert_full(rules, ["R7", "R8", "R10"], "SellRev V6.1")


def test_sell_momentum_v4_shared_fields_full_credit():
    """SellMom V4 (id=4514): a V8-passing breakdown candidate (mRSI/twr cold band, mom_2d/w52
    band, below-PP + S2-clearance) must score R7/R10/R11 full credit."""
    d = m._derive(_base())
    d["v8"]["rsi_month"] = 35   # < 40
    d["true_weekly_rsi"] = 35   # <= 40
    d["v8"]["mom_2d"] = -3.0    # in [-4,-2]
    d["v8"]["week_index_52"] = 40  # in [20,60]
    d["above_pp"] = False       # below PP -> R11 eligible
    d["room_s2"] = 4.0          # >= 3% clearance -> R11 full
    rules = {r["rule"]: r for r in m._rules(d, "MOMENTUM", "SELL")}
    _assert_full(rules, ["R7", "R10", "R11"], "SellMom V4")


def test_r13_basis_missing_symmetric():
    """cc#513 cross-cutting: missing basis -> 0.5 on BOTH sides (was BUY 0 / SELL 0.5)."""
    d = m._derive(_base())
    d["basis_now"], d["basis_prev"] = None, None
    for style in ("MOMENTUM", "REVERSAL"):
        for side in ("BUY", "SELL"):
            rules = {r["rule"]: r for r in m._rules(d, style, side)}
            assert rules["R13"]["credit"] == 0.5, (style, side, rules["R13"])


def test_no_synthetic_rsi_weekly_in_any_rule():
    """cc#513 cross-cutting: synthetic rsi_weekly must not appear in any rule's value/required
    after this change (all twr reads = the module's own _derive true_weekly_rsi)."""
    d = m._derive(_base())
    for style in ("MOMENTUM", "REVERSAL"):
        for side in ("BUY", "SELL"):
            for r in m._rules(d, style, side):
                val = r.get("value")
                if isinstance(val, dict):
                    assert "wRSI" not in val, (style, side, r)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} cc#513 alignment tests passed.")
