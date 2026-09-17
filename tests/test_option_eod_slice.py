"""cc#2205 OPTION IV SOLVE-ON-READ: the ONE solver (option_iv_history.solve_iv) recovers a Black-76
surface from raw closes off the parity forward, falls back to carry, never fabricates a vol for a row
without a price; option_ivp solves the raw slice on read through a per-(symbol, max trade_date) cache,
applies the G3 band after the solve, and chain_tags does ONE fetch per request; the cc#1994 gap map
reads the same solve. No DB, no network -- a fake cursor stands in for psycopg."""
from datetime import date, timedelta

import numpy as np

import option_iv_history as oih
import option_ivp


def _b76(F, K, T, sig, is_call):
    return float(oih._b76_price_vec(np.array([F]), np.array([K]), np.array([T]), np.array([sig]), np.array([is_call]))[0])


def _session(d, exp, spot, carry, strikes, sig_of_k):
    """Raw rows in the solve_iv shape for ONE session: closes priced by Black-76 off F = spot*e^(carry*T)."""
    T = (exp - d).days / 365.0
    F = spot * np.exp(carry * T)
    rows = []
    for K in strikes:
        for ot in ("CE", "PE"):
            rows.append((d, exp, K, ot, _b76(F, K, T, sig_of_k(K), ot == "CE"), spot))
    return rows


def _history(n_sessions=70, n_each_side=5, step=50.0, base=23500.0):
    """n sessions of a drifting spot, one monthly expiry each, (2n+1) strikes around spot, a mild smile
    whose level wobbles session to session so percentiles are not degenerate."""
    rows, start = [], date(2026, 5, 4)
    for s in range(n_sessions):
        d = start + timedelta(days=s + (s // 5) * 2)          # skip weekends, roughly
        exp = d + timedelta(days=21)
        spot = base * (1 + 0.002 * np.sin(s / 3.0)) + 3.0 * s
        atm = round(spot / step) * step
        strikes = [atm + step * k for k in range(-n_each_side, n_each_side + 1)]
        level = 0.13 + 0.03 * np.sin(s / 7.0)
        rows += _session(d, exp, float(spot), 0.004, strikes, lambda K, lv=level, sp=spot: lv + 0.000004 * abs(K - sp))
    return rows


class FakeCur:
    """Answers MAX(trade_date) with `max_date` and every other SELECT with `rows`; records the SQL."""
    def __init__(self, max_date, rows):
        self.max_date, self.rows, self.executed = max_date, rows, []
    def execute(self, sql, params=None):
        self.executed.append(sql)
    def fetchone(self):
        return (self.max_date,)
    def fetchall(self):
        return self.rows


def test_solve_iv_recovers_the_surface_off_the_parity_forward():
    d, exp = date(2026, 9, 11), date(2026, 9, 29)
    strikes = [23000 + 50 * i for i in range(21)]
    smile = lambda K: 0.12 + 0.000004 * abs(K - 23500)
    rows = _session(d, exp, 23500.0, 0.004, strikes, smile)      # real carry 0.4 pct, not R_FREE
    iv, meta = oih.solve_iv(rows, with_meta=True)
    true = np.array([smile(r[2]) for r in rows])
    assert np.nanmax(np.abs(iv - true)) < 1e-6
    assert meta["parity_groups"] == 1 and meta["fallback_groups"] == 0 and len(meta["atm_pairs"]) == 1
    ce_i, pe_i = meta["atm_pairs"][0]
    assert rows[ce_i][2] == rows[pe_i][2] == 23500.0 and rows[ce_i][3] == "CE" and rows[pe_i][3] == "PE"
    assert abs(iv[ce_i] - iv[pe_i]) < 1e-9                        # the same strike solves to the same vol on both legs


def test_solve_iv_carry_fallback_and_no_price_stays_nan():
    d, exp = date(2026, 9, 15), date(2026, 9, 29)
    T = (exp - d).days / 365.0
    spot = 2900.0
    rows = [(d, exp, K, "CE", _b76(spot * np.exp(oih.R_FREE * T), K, T, 0.2, True), spot) for K in (2800.0, 2900.0, 3000.0)]
    rows.append((d, exp, 3100.0, "PE", None, spot))               # no price
    rows.append((d, exp, 3200.0, "PE", 0.0, spot))                # zero price
    iv, meta = oih.solve_iv(rows, with_meta=True)
    assert meta["parity_groups"] == 0 and meta["fallback_groups"] == 1
    assert np.allclose(iv[:3], 0.2, atol=1e-6)
    assert np.isnan(iv[3]) and np.isnan(iv[4])
    assert oih.solve_iv([]).shape == (0,)


def test_solved_rows_caches_per_symbol_and_max_date_and_never_caches_an_empty_slice():
    option_ivp.invalidate()
    rows = _history(3)
    cur = FakeCur(date(2026, 9, 16), rows)
    r1 = option_ivp.solved_rows(cur, "TESTSYM")
    assert len(r1) == len(rows) and len(cur.executed) == 2 and "option_eod_slice" in cur.executed[1]
    r2 = option_ivp.solved_rows(cur, "TESTSYM")
    assert r2 is r1 and len(cur.executed) == 3                    # hit: only the MAX(trade_date) lookup ran
    cur.max_date = date(2026, 9, 17)                               # the nightly tick landed a new date
    r3 = option_ivp.solved_rows(cur, "TESTSYM")
    assert r3 is not r1 and len(cur.executed) == 5
    empty = FakeCur(None, [])                                      # no rows for this symbol yet -> nothing cached, re-asked next time
    assert option_ivp.solved_rows(empty, "OTHER") == [] and option_ivp.solved_rows(empty, "OTHER") == []
    assert len(empty.executed) == 4 and "OTHER" not in option_ivp._SOLVED_CACHE
    option_ivp.invalidate("TESTSYM")
    assert "TESTSYM" not in option_ivp._SOLVED_CACHE


def test_band_applied_after_the_solve_and_chain_tags_one_fetch():
    option_ivp.invalidate()
    rows = _history(70)
    d_last = rows[-1][0]
    bad = (d_last, rows[-1][1], rows[-1][2] + 1000.0, "CE", 50000.0, rows[-1][5])   # a close no sane vol can produce -> above the ceiling
    solved = option_ivp.solved_from(rows + [bad], oih.solve_iv(rows + [bad]))
    banded = option_ivp._banded(solved)
    assert len(solved) == len(rows) + 1 and len(banded) == len(rows)
    assert all(option_ivp.IV_FLOOR <= t[3] <= option_ivp.IV_CEILING for t in banded)
    hist = option_ivp.atm_iv_history(None, "X", solved=solved)
    assert len(hist) == 70 and hist[0][0] < hist[-1][0]
    spot = rows[-1][5]
    strikes = sorted({r[2] for r in rows if r[0] == d_last})
    tags, fwd = option_ivp.chain_tags(None, "X", spot, strikes, 21, solved=solved)
    atm_k = min(strikes, key=lambda s: abs(s - spot))
    assert len(tags) == 22 and fwd == "carry_fallback"
    assert tags[(atm_k, "CE")]["tag"] in ("CHEAP", "FAIR", "EXPENSIVE") and tags[(atm_k, "CE")]["ivp"] is not None
    cur = FakeCur(d_last, rows)
    option_ivp.chain_tags(cur, "X", spot, strikes, 21)
    assert len(cur.executed) == 2                                  # MAX(trade_date) + ONE slice fetch for both helpers
    option_ivp.chain_tags(cur, "X", spot, strikes, 21)
    assert len(cur.executed) == 3                                  # warm: the lookup only


def test_latest_session_iv_feeds_the_gap_map():
    import deriv_metrics
    option_ivp.invalidate()
    rows = _history(2)
    cur = FakeCur(rows[-1][0], rows)
    asof, legs = option_ivp.latest_session_iv(cur, "GAPSYM")
    assert asof == rows[-1][0] and len(legs) == 22
    asof2, gap = deriv_metrics._stored_iv_gap_map(cur, "GAPSYM")
    assert asof2 == str(rows[-1][0]) and len(gap) == 11
    assert all(v["gap_vol_pts"] == round(v["put_iv"] - v["call_iv"], 1) for v in gap.values())
    assert max(abs(v["gap_vol_pts"]) for v in gap.values()) == 0.0   # one solve, one forward: both legs agree at every strike
