"""cc#2036 OPT sprint 1/5 — option strategy payoff engine, expiry payoff only (v1).

Pure functions, no DB, no imports beyond the standard library — same discipline as
deriv_metrics.py's _bs_* functions. Ported from reports/CC2035_ref/engine.py (Fable's own
prototype, validated against the founder's 2012 Excel workbook + re-checked by hand for the one
sheet Excel got wrong — see that file's own inline notes). Behaviour is the contract per cc#2036's
own instruction: this file changes NOTHING about breakevens()/max_profit_loss()'s actual numbers —
same piecewise-linear root-finding, same left-tail-bounded-at-zero/right-tail-only-Unlimited rule,
same loss-reported-as-positive convention. The two additions this port makes are curve_per_leg()
(session_log 45192's own API contract needs a per-leg breakdown alongside the net curve; the
reference only exposed leg_payoff() for a single S, so this is the same S-range walk curve()
already does, reused rather than duplicated — ONE_REGISTRY_ONE_DERIVATION_V1) and bounded_by_zero()
(cc#2036 item 3's explicit ask: "Add bounded_by_zero flags").

at_expiry_only_v1 (session_log 45180): this is the ONLY payoff model v1 computes. No Greeks, no
Black-Scholes, no intraday exercise-before-expiry P&L — those are explicitly out of scope here
(deriv_metrics.py's own Greeks, cc#2034, are a separate, already-shipped concern for a different
surface: the live option chain, not the strategy builder).
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

Number = Union[int, float]


@dataclass
class Leg:
    kind: str       # "CE" | "PE" | "FUT"
    side: str       # "BUY" | "SELL"
    strike: float   # for FUT: entry price
    premium: float  # for FUT: 0
    qty: int = 1


def leg_payoff(S: Number, l: Leg, lot: int) -> float:
    """Net payoff of a single leg at index level S, at expiry. Loss is negative here (net_payoff's
    and the public max_profit_loss()'s "loss reported as positive" convention is applied only at
    the aggregate/reporting layer below, never inside this per-point building block)."""
    sgn = 1 if l.side == "BUY" else -1
    if l.kind == "CE":
        intrinsic = max(S - l.strike, 0) - l.premium
    elif l.kind == "PE":
        intrinsic = max(l.strike - S, 0) - l.premium
    else:  # FUT
        intrinsic = S - l.strike
    return sgn * intrinsic * l.qty * lot


def net_payoff(S: Number, legs: List[Leg], lot: int) -> float:
    return sum(leg_payoff(S, l, lot) for l in legs)


def kinks(legs: List[Leg]) -> List[float]:
    """The strikes where the piecewise-linear net payoff can change slope — every CE/PE strike,
    deduplicated. A pure-FUT combination has no kinks (payoff is one straight line everywhere)."""
    return sorted({l.strike for l in legs if l.kind in ("CE", "PE")})


def tail_slopes(legs: List[Leg], lot: int) -> Tuple[float, float]:
    """Slope of net payoff per 1 point of S, as S -> -inf (lo) and S -> +inf (hi). FUT legs
    contribute their full signed qty*lot to BOTH tails (a future's payoff is one line, no kink);
    CE only ever affects the right tail, PE only the left — matching each option's one-sided
    intrinsic-value kink."""
    lo = hi = 0.0
    for l in legs:
        sgn = (1 if l.side == "BUY" else -1) * l.qty * lot
        if l.kind == "CE":
            hi += sgn
        elif l.kind == "PE":
            lo += -sgn
        else:
            lo += sgn
            hi += sgn
    return lo, hi


def breakevens(legs: List[Leg], lot: int) -> List[float]:
    """Roots of the piecewise-linear net payoff, between adjacent kinks plus both tails. A pure-FUT
    book has exactly one breakeven (its qty-weighted average entry price) computed directly, since
    there are no kinks to bracket a root between."""
    ks = kinks(legs)
    if not ks:
        p = [l for l in legs if l.kind == "FUT"]
        q = sum(l.qty * (1 if l.side == "BUY" else -1) for l in p)
        return [round(sum(l.strike * l.qty * (1 if l.side == "BUY" else -1) for l in p) / q, 2)] if q else []
    lo, hi = tail_slopes(legs, lot)
    pts = [ks[0] - 1.0] + ks + [ks[-1] + 1.0]
    bes = []
    for a, b in zip(pts[:-1], pts[1:]):
        fa, fb = net_payoff(a, legs, lot), net_payoff(b, legs, lot)
        if fa == 0:
            bes.append(a)
        if fa * fb < 0:
            bes.append(a + (b - a) * (-fa) / (fb - fa))
    f0 = net_payoff(ks[0], legs, lot)
    if lo != 0 and f0 * lo > 0:
        bes.append(ks[0] - f0 / lo)
    fn = net_payoff(ks[-1], legs, lot)
    if hi != 0 and fn * hi < 0:
        bes.append(ks[-1] - fn / hi)
    bes = [round(b, 2) for b in bes if b > 0]
    return sorted(set(bes))


def max_profit_loss(legs: List[Leg], lot: int) -> Tuple[Union[str, float], Union[str, float]]:
    """(max_profit, max_loss). Each is either a positive number or the literal string "Unlimited".
    Left tail is bounded at S=0 (an index cannot go negative) so ONLY the right tail (hi) can ever
    make a side "Unlimited" — a left-tail-only extreme instead shows up as a large-but-finite number
    (see bounded_by_zero() below, which flags exactly this case). Loss is reported as a positive
    number throughout, matching this file's one established convention."""
    ks = kinks(legs)
    lo, hi = tail_slopes(legs, lot)
    vals = [net_payoff(k, legs, lot) for k in ks] + [net_payoff(0, legs, lot)]
    mp: Union[str, float] = max(vals)
    ml: Union[str, float] = min(vals)
    if hi > 0:
        mp = "Unlimited"
    if hi < 0:
        ml = "Unlimited"
    if isinstance(mp, (int, float)):
        mp = round(mp, 2)
    if isinstance(ml, (int, float)):
        ml = round(-ml, 2)  # loss reported as positive number
    return mp, ml


def bounded_by_zero(legs: List[Leg], lot: int) -> dict:
    """cc#2036 item 3: flag a max_profit/max_loss that is finite ONLY because the index physically
    cannot go below S=0 -- e.g. a naked short put's worst case is theoretically "S keeps falling
    forever" but the true floor is S=0, giving a huge yet finite number (session_log 45180's
    "Substantial (index to zero)" display rule). Deliberately mirrors max_profit_loss()'s own
    hi>0/hi<0 "Unlimited" gates and its own `vals` list so the two functions can never disagree
    about which side is numeric vs "Unlimited" -- computed once here, not re-derived a second,
    possibly-drifting way.

    True exactly when: (a) that side is still a number (not already "Unlimited" via the right
    tail), (b) the S=0 evaluation point is the actual extremum, and (c) the LEFT tail slope is
    non-zero -- i.e. the payoff was still sloping linearly when truncated at S=0, not already
    flat there from the option structure's own limited-loss/profit floor (a long call's max loss
    is flat at -premium well before S=0 and is correctly NOT flagged; see the module docstring's
    golden-fixture cross-check in tests/test_option_strategy_engine.py)."""
    ks = kinks(legs)
    if not ks:
        return {"max_profit": False, "max_loss": False}
    lo, hi = tail_slopes(legs, lot)
    vals = [net_payoff(k, legs, lot) for k in ks] + [net_payoff(0, legs, lot)]
    z = net_payoff(0, legs, lot)
    mp_is_num = not (hi > 0)
    ml_is_num = not (hi < 0)
    return {
        "max_profit": bool(mp_is_num and lo != 0 and z == max(vals)),
        "max_loss": bool(ml_is_num and lo != 0 and z == min(vals)),
    }


def _grid_points(spot: Number, pct: float, step: int, extra=None) -> List[float]:
    """The shared S-axis curve()/curve_per_leg() both walk: the regular spot+/-pct grid at `step`,
    UNION any `extra` values that fall inside that range (cc#2038: a leg's own strike or an exact
    breakeven is very often NOT on the regular grid -- e.g. a breakeven of 23,323.3 vs a 50-wide
    grid anchored on multiples of 50 -- so a caller that snapped to the nearest grid point instead
    of asking for the exact one would show a "breakeven" row whose net is not actually zero. Adding
    the real point here means every row a caller asks for is a genuinely computed value, never an
    approximation dressed up as one)."""
    import math
    lo_s = math.floor(spot * (1 - pct) / step) * step
    hi_s = math.ceil(spot * (1 + pct) / step) * step
    points = set()
    S = lo_s
    while S <= hi_s:
        points.add(S)
        S += step
    for e in (extra or []):
        if e is not None and lo_s <= e <= hi_s:
            points.add(round(e, 2))
    return sorted(points)


def curve(spot: Number, legs: List[Leg], lot: int, pct: float = 0.15, step: int = 50, extra=None) -> List[Tuple[float, float]]:
    """[(S, net_payoff)] across spot +/-pct at the given strike step (plus any `extra` exact points
    merged in -- see _grid_points). session_log 45180: range is spot +/-15%, step = the
    underlying's own strike interval (50 NIFTY, 100 BANKNIFTY)."""
    return [(S, round(net_payoff(S, legs, lot), 2)) for S in _grid_points(spot, pct, step, extra)]


def curve_per_leg(spot: Number, legs: List[Leg], lot: int, pct: float = 0.15, step: int = 50, extra=None) -> List[list]:
    """[[S, leg1_payoff, leg2_payoff, ...]] over the SAME S-axis curve() walks (session_log 45192's
    per_leg contract) -- reuses _grid_points() rather than re-deriving it a second time, so the two
    curves can never disagree on their S axis."""
    return [[S] + [round(leg_payoff(S, l, lot), 2) for l in legs] for S in _grid_points(spot, pct, step, extra)]


def net_premium(legs: List[Leg], lot: int) -> float:
    """Net premium paid (positive) / received (negative) opening the combination -- BUY pays,
    SELL receives, FUT has none (its premium is always 0 per the Leg contract)."""
    total = 0.0
    for l in legs:
        sgn = 1 if l.side == "BUY" else -1
        total += sgn * l.premium * l.qty * lot
    return round(total, 2)


def price_strategy(spot: Number, legs: List[Leg], lot: int) -> dict:
    """The full session_log 45192 POST /api/options/payoff contract in one call -- the shape
    cc#2037's endpoint hands back verbatim (plus whatever request-echo fields the endpoint itself
    adds, e.g. underlying).

    cc#2038: `curve`/`per_leg` also force-include every leg's own strike and every breakeven as
    exact points (via _grid_points' `extra`) -- the web/app per-leg table (session_log 45192's own
    "show 7 rows: range ends, each strike, each BE") needs a REAL row at each of those, not the
    nearest regular-grid neighbour. Backward compatible: this only adds points to the two arrays,
    it does not remove or change any point already there, so nothing already consuming the regular
    grid (nothing does yet) is affected."""
    mp, ml = max_profit_loss(legs, lot)
    bes = breakevens(legs, lot)
    extra = list(bes) + [l.strike for l in legs if l.kind in ("CE", "PE")]
    return {
        "curve": [list(pt) for pt in curve(spot, legs, lot, extra=extra)],
        "per_leg": curve_per_leg(spot, legs, lot, extra=extra),
        "breakevens": bes,
        "max_profit": mp,
        "max_loss": ml,
        "bounded_by_zero": bounded_by_zero(legs, lot),
        "net_premium": net_premium(legs, lot),
    }
