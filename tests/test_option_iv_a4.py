"""cc#2031 A4: the pure re-solve (a4_resolve_rows) recovers the true surface from stored closes when
the stored iv came from the old spot-based solve, reports the stats the gate reads, and never
fabricates a value for a row without a price. No DB, no network."""
from datetime import date

import numpy as np

import option_iv_history as oih


def _surface(d, exp, spot, carry, strikes, sig_of_k):
    """Closes priced by Black-76 off F = spot*e^(carry*T) with a smile; 'old' iv = the spot-based
    solve (drift R_FREE) -- wrong whenever carry != R_FREE."""
    T = (exp - d).days / 365.0
    F = spot * np.exp(carry * T)
    rows = []
    for K in strikes:
        for ot in ("CE", "PE"):
            sig = sig_of_k(K)
            close = float(oih._b76_price_vec(np.array([F]), np.array([K]), np.array([T]), np.array([sig]), np.array([ot == "CE"]))[0])
            old = float(oih._bs_iv_vec(np.array([close]), np.array([spot]), np.array([K]), np.array([T]), np.array([ot == "CE"]))[0])
            rows.append((d, exp, K, ot, close, spot, old))
    return rows


def test_resolve_recovers_true_surface_and_closes_atm_gap():
    d, exp = date(2026, 9, 11), date(2026, 9, 29)
    strikes = [23000 + 50 * i for i in range(21)]
    smile = lambda K: 0.12 + 0.000004 * abs(K - 23500)
    rows = _surface(d, exp, 23500.0, 0.004, strikes, smile)          # real carry 0.4%, not 7%
    iv_new, st = oih.a4_resolve_rows(rows)
    true = np.array([smile(r[2]) for r in rows])
    assert np.nanmax(np.abs(iv_new - true)) < 1e-6, np.nanmax(np.abs(iv_new - true))
    assert st["parity_groups"] == 1 and st["fallback_groups"] == 0 and st["rows"] == 42
    assert st["atm_gap_before_median"] > 0.5                          # the old solve splits CE/PE at the same strike
    assert st["atm_gap_after_median"] == 0.0 and st["atm_gap_after_p95"] == 0.0
    assert st["rows_changed"] == 42 and st["null_flips"] == 0
    assert st["inside_to_outside"] == 0                                # nothing correct gets pushed out of the band
    assert st["band_hits_after"] == 0 and st["band_hits_before"] >= st["outside_to_inside"]   # the old solve floored a deep leg; the re-solve brings it back in


def test_already_black76_rows_do_not_change_and_null_price_stays_null():
    d, exp = date(2026, 9, 15), date(2026, 9, 29)
    strikes = [56000 + 100 * i for i in range(5)]
    rows = _surface(d, exp, 56200.0, 0.003, strikes, lambda K: 0.15)
    iv_new, _ = oih.a4_resolve_rows(rows)
    rows2 = [(r[0], r[1], r[2], r[3], r[4], r[5], float(iv_new[i])) for i, r in enumerate(rows)]   # stored = B76 already
    rows2.append((d, exp, 57000.0, "CE", None, 56200.0, None))                                      # no price
    iv2, st2 = oih.a4_resolve_rows(rows2)
    assert st2["rows_changed"] == 0 and st2["null_flips"] == 0
    assert np.isnan(iv2[-1]) and st2["rows_with_iv_after"] == 10


def test_carry_fallback_when_no_strike_has_both_legs():
    d, exp = date(2026, 9, 15), date(2026, 9, 29)
    T = (exp - d).days / 365.0
    spot = 2900.0
    rows = []
    for K in (2800.0, 2900.0, 3000.0):
        close = float(oih._b76_price_vec(np.array([spot * np.exp(oih.R_FREE * T)]), np.array([K]), np.array([T]), np.array([0.2]), np.array([True]))[0])
        rows.append((d, exp, K, "CE", close, spot, None))                                            # calls only, iv never stored
    iv_new, st = oih.a4_resolve_rows(rows)
    assert st["parity_groups"] == 0 and st["fallback_groups"] == 1
    assert np.allclose(iv_new, 0.2, atol=1e-6)                                                      # carry forward = the old solve's own forward
    assert st["null_flips"] == 3 and st["rows_changed"] == 0
