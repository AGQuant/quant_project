"""cc#2036 item 4: 34 golden tests for option_strategy_engine.py (pytest node count matches the
card's own verify line, "pytest -q tests/test_option_strategy_engine.py -> 34 passed") --
the 4 Excel-derived cases from reports/CC2035_ref/engine.py's own __main__ block, plus all 30 rows
of reports/CC2035_ref/golden_30.json (breakevens, max_profit, max_loss, lot 65). Each of the 30
golden_30 tests ALSO cross-checks bounded_by_zero() -- a field this port adds that the reference
prototype never had -- against that same row's own max_profit_display/max_loss_display "Substantial
(index to zero)" marker, folded into the same 30 test nodes (not spawned as extra ones) so the
total stays exactly 34, not a silently-grown number. Run: pytest -q tests/test_option_strategy_engine.py
"""
import json
import os

import pytest

from option_strategy_engine import Leg, breakevens, bounded_by_zero, max_profit_loss

GOLDEN_PATH = os.path.join(os.path.dirname(__file__), "..", "reports", "CC2035_ref", "golden_30.json")
with open(GOLDEN_PATH) as _f:
    GOLDEN = json.load(_f)

SUBSTANTIAL = "Substantial (index to zero)"


# ---- the 4 Excel golden cases, verbatim from reports/CC2035_ref/engine.py's own __main__ ----
EXCEL_CASES = [
    ("Covered Call", [Leg("CE", "SELL", 5000, 160), Leg("FUT", "BUY", 5150, 0)], 50, [4990.0], 500.0, 249500.0),
    ("Long Straddle", [Leg("CE", "BUY", 4500, 122), Leg("PE", "BUY", 4500, 100)], 50, [4278.0, 4722.0], "Unlimited", 11100.0),
    ("Bull Call Spread", [Leg("CE", "BUY", 4100, 170.45), Leg("CE", "SELL", 4400, 35.4)], 50, [4235.05], 8247.5, 6752.5),
    ("Long Call Butterfly", [Leg("CE", "BUY", 3100, 141.55, 5), Leg("CE", "SELL", 3200, 98, 10), Leg("CE", "BUY", 3300, 64, 5)], 20, [3109.55, 3290.45], 9045.0, 955.0),
]


@pytest.mark.parametrize("name,legs,lot,exp_be,exp_mp,exp_ml", EXCEL_CASES, ids=[c[0] for c in EXCEL_CASES])
def test_excel_golden_case(name, legs, lot, exp_be, exp_mp, exp_ml):
    be = breakevens(legs, lot)
    mp, ml = max_profit_loss(legs, lot)
    assert be == exp_be, f"{name}: breakevens {be} != expected {exp_be}"
    assert mp == exp_mp, f"{name}: max_profit {mp} != expected {exp_mp}"
    assert ml == exp_ml, f"{name}: max_loss {ml} != expected {exp_ml}"


def _legs_from_golden(t):
    return [Leg(l["kind"], l["side"], l["strike"], l["premium"], l["qty"]) for l in t["legs"]]


@pytest.mark.parametrize("t", GOLDEN["templates"], ids=[t["name"] for t in GOLDEN["templates"]])
def test_golden_30_template(t):
    lot = GOLDEN["lot"]
    legs = _legs_from_golden(t)
    be = breakevens(legs, lot)
    mp, ml = max_profit_loss(legs, lot)
    assert be == t["breakevens"], f"{t['name']}: breakevens {be} != golden {t['breakevens']}"
    assert mp == t["max_profit"], f"{t['name']}: max_profit {mp} != golden {t['max_profit']}"
    assert ml == t["max_loss"], f"{t['name']}: max_loss {ml} != golden {t['max_loss']}"
    # bounded_by_zero is new (the reference prototype never had it) -- folded into this same test
    # node, not a 31st-60th parametrized one, so the collected count stays exactly 34. Cross-checked
    # against this row's own max_profit_display/max_loss_display marker, not self-verified only.
    bz = bounded_by_zero(legs, lot)
    assert bz["max_profit"] == (t.get("max_profit_display") == SUBSTANTIAL), \
        f"{t['name']}: bounded_by_zero.max_profit={bz['max_profit']} but max_profit_display={t.get('max_profit_display')!r}"
    assert bz["max_loss"] == (t.get("max_loss_display") == SUBSTANTIAL), \
        f"{t['name']}: bounded_by_zero.max_loss={bz['max_loss']} but max_loss_display={t.get('max_loss_display')!r}"


if __name__ == "__main__":
    # plain-python fallback (no pytest needed) -- runs the same 34 golden nodes, bounded_by_zero
    # folded into each golden_30 case exactly as above, prints PASS/FAIL per case like this repo's
    # other measure_* scripts, non-zero exit on any failure.
    n_pass = n_fail = 0
    for name, legs, lot, exp_be, exp_mp, exp_ml in EXCEL_CASES:
        be = breakevens(legs, lot)
        mp, ml = max_profit_loss(legs, lot)
        ok = be == exp_be and mp == exp_mp and ml == exp_ml
        print(("PASS" if ok else "FAIL"), "[excel]", name, "BE", be, "maxP", mp, "maxL", ml)
        n_pass, n_fail = (n_pass + 1, n_fail) if ok else (n_pass, n_fail + 1)
    for t in GOLDEN["templates"]:
        legs = _legs_from_golden(t)
        be = breakevens(legs, GOLDEN["lot"])
        mp, ml = max_profit_loss(legs, GOLDEN["lot"])
        bz = bounded_by_zero(legs, GOLDEN["lot"])
        ok = (be == t["breakevens"] and mp == t["max_profit"] and ml == t["max_loss"]
              and bz["max_profit"] == (t.get("max_profit_display") == SUBSTANTIAL)
              and bz["max_loss"] == (t.get("max_loss_display") == SUBSTANTIAL))
        print(("PASS" if ok else "FAIL"), "[golden_30]", t["name"], "BE", be, "maxP", mp, "maxL", ml, "bz", bz)
        n_pass, n_fail = (n_pass + 1, n_fail) if ok else (n_pass, n_fail + 1)
    print(f"\n{n_pass} passed, {n_fail} failed (34 total, bounded_by_zero folded into the golden_30 cases)")
    raise SystemExit(1 if n_fail else 0)
