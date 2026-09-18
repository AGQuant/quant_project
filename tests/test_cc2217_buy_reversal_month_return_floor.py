"""cc#2217 BUY_REVERSAL V6.1 -> V6.2: month_return gains a STRICT floor (>0), turning the existing
<5 ceiling into a (0,5] band. filter_total stays 9 -- one bound on an existing gate, not a new gate.
No DB, no network: BASKET_FILTERS/BASKET_SPEC are plain module-level dicts and _reg_cond /
_passes_registry_band are pure functions, all importable and callable as-is."""
import v8_endpoints as e
import v8_signal_writer as sw


def _mr_row():
    return [f for f in sw.BASKET_FILTERS["buy_reversal"] if f["key"] == "month_return"][0]


def test_registry_row_gained_a_strict_floor_ceiling_untouched():
    row = _mr_row()
    assert row["min"] == 0.0
    assert row["max"] == 5.0
    assert row["cond_min"] == "> 0"
    assert row["cond_max"] == "< 5"          # unchanged text -- the ceiling itself never moved
    assert row["strict"] is True


def test_registry_row_count_unchanged_this_is_a_bound_not_a_new_gate():
    # 8 registry rows == "9 conditions" in the basket's own docstring counting (mom_2d's two bounds
    # count as 2 conditions on 1 row); cc#2217 adds a bound to month_return the same way, so the row
    # count -- and the snapshot's hardcoded filter_total -- both stay exactly where they were.
    assert len(sw.BASKET_FILTERS["buy_reversal"]) == 8


def test_basket_spec_version_bumped_to_v6_2():
    assert sw.BASKET_SPEC["buy_reversal"]["version"] == "V6.2"
    assert sw.BASKET_SPEC["buy_reversal"]["cc"] == "cc#2217"


def test_reg_cond_prints_0_to_5_for_the_ibutton_and_passcount_surfaces():
    # cc#2217's own verify step: "the funnel, the i-button, br_stock_passcount and br_stock_detail
    # all render the new band as '0 to 5' from the registry". _reg_cond ignores `strict` whenever
    # both mn and mx are set, so this holds regardless of the strict flag on the row.
    assert e._reg_cond(_mr_row()) == "0 to 5"


def test_passes_registry_band_boundary_matrix():
    row = _mr_row()
    # Floor is exclusive (the new cc#2217 behaviour): exactly 0 fails.
    assert e._passes_registry_band(-1.0, row) is False
    assert e._passes_registry_band(0.0, row) is False
    assert e._passes_registry_band(0.0001, row) is True
    assert e._passes_registry_band(2.5, row) is True
    # Ceiling: _passes_registry_band applies `strict` to BOTH bounds of a two-sided band, so on
    # THIS display/passcount surface exactly 5.0 now reads as a fail -- a narrow, documented
    # divergence from the live engine (which keeps the ceiling inclusive, see the registry row's
    # own comment and the two tests below). The schema has no strict_min/strict_max split to avoid
    # it; flagged on the card, not something this test should silently paper over.
    assert e._passes_registry_band(4.999, row) is True
    assert e._passes_registry_band(5.0, row) is False
    assert e._passes_registry_band(5.0001, row) is False


def _live_engine_gate6(month_return):
    """Mirrors _mr_gt0 + the _passes(v, None, 5.0) pair inside
    _write_buy_reversal_v6_qualified exactly (that function is DB-driven and its helpers are
    nested/non-exported, so the real live-money gate is re-created here from the same two
    module-level primitives -- _passes itself, and _mr_gt0's one-line body -- rather than
    re-implemented from scratch)."""
    v = month_return
    mr_gt0 = v is not None and float(v) > 0.0
    return mr_gt0 and sw._passes(v, None, 5.0)


def test_live_engine_gate_floor_strict_ceiling_inclusive():
    # This is the actual money-path behaviour, deliberately asymmetric: floor > 0 STRICT (per the
    # card), ceiling stays <= 5.0 INCLUSIVE ("no other change" to the pre-existing ceiling).
    assert _live_engine_gate6(-1.0) is False
    assert _live_engine_gate6(0.0) is False       # the one historical loser at exactly 0 (BANKINDIA)
    assert _live_engine_gate6(0.0001) is True
    assert _live_engine_gate6(5.0) is True         # ceiling inclusive -- unchanged from V6.1
    assert _live_engine_gate6(5.0001) is False
    assert _live_engine_gate6(None) is False
