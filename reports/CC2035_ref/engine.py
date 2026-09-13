"""Option strategy payoff engine prototype (expiry payoff, piecewise-linear exact).
Fable reference for cc#2035. CC: port into option_strategy_engine.py; behaviour is the contract.
Left tail is bounded at S=0 (index cannot go below zero) -> only the right tail can be 'Unlimited'.
"""
from dataclasses import dataclass
import math

@dataclass
class Leg:
    kind: str      # CE | PE | FUT
    side: str      # BUY | SELL
    strike: float  # for FUT: entry price
    premium: float # for FUT: 0
    qty: int = 1

def leg_payoff(S, l: Leg, lot):
    sgn = 1 if l.side == "BUY" else -1
    if l.kind == "CE":   intrinsic = max(S - l.strike, 0) - l.premium
    elif l.kind == "PE": intrinsic = max(l.strike - S, 0) - l.premium
    else:                intrinsic = S - l.strike
    return sgn * intrinsic * l.qty * lot

def net_payoff(S, legs, lot):
    return sum(leg_payoff(S, l, lot) for l in legs)

def kinks(legs):
    return sorted({l.strike for l in legs if l.kind in ("CE","PE")})

def tail_slopes(legs, lot):
    """slope of net payoff for S -> -inf and S -> +inf (per 1 point of S)."""
    lo = hi = 0.0
    for l in legs:
        sgn = (1 if l.side=="BUY" else -1) * l.qty * lot
        if l.kind == "CE": hi += sgn
        elif l.kind == "PE": lo += -sgn
        else: lo += sgn; hi += sgn
    return lo, hi

def breakevens(legs, lot):
    ks = kinks(legs)
    if not ks:  # pure futures
        p = [l for l in legs if l.kind=="FUT"]
        q = sum(l.qty*(1 if l.side=="BUY" else -1) for l in p)
        return [round(sum(l.strike*l.qty*(1 if l.side=="BUY" else -1) for l in p)/q, 2)] if q else []
    lo, hi = tail_slopes(legs, lot)
    pts = [ks[0] - 1.0] + ks + [ks[-1] + 1.0]
    bes = []
    for a, b in zip(pts[:-1], pts[1:]):
        fa, fb = net_payoff(a, legs, lot), net_payoff(b, legs, lot)
        if fa == 0: bes.append(a)
        if fa * fb < 0:
            bes.append(a + (b - a) * (-fa) / (fb - fa))
    f0 = net_payoff(ks[0], legs, lot)
    if lo != 0 and f0 * lo > 0:
        bes.append(ks[0] - f0/lo)
    fn = net_payoff(ks[-1], legs, lot)
    if hi != 0 and fn * hi < 0:
        bes.append(ks[-1] - fn/hi)
    bes = [round(b, 2) for b in bes if b > 0]
    return sorted(set(bes))

def max_profit_loss(legs, lot):
    ks = kinks(legs)
    lo, hi = tail_slopes(legs, lot)
    vals = [net_payoff(k, legs, lot) for k in ks] + [net_payoff(0, legs, lot)]
    mp = max(vals); ml = min(vals)
    if hi > 0: mp = "Unlimited"
    if hi < 0: ml = "Unlimited"
    if isinstance(mp,(int,float)): mp = round(mp,2)
    if isinstance(ml,(int,float)): ml = round(-ml,2)   # loss reported as positive number
    return mp, ml

def curve(spot, legs, lot, pct=0.15, step=50):
    lo_s = math.floor(spot*(1-pct)/step)*step; hi_s = math.ceil(spot*(1+pct)/step)*step
    S = lo_s; out = []
    while S <= hi_s:
        out.append((S, round(net_payoff(S, legs, lot),2))); S += step
    return out

if __name__ == "__main__":
    def run(name, legs, lot, exp_be, exp_mp, exp_ml):
        be = breakevens(legs, lot); mp, ml = max_profit_loss(legs, lot)
        ok = be == exp_be and mp == exp_mp and ml == exp_ml
        print(("PASS" if ok else "FAIL"), name, "BE", be, "maxP", mp, "maxL", ml)
        if not ok: print("   expected", exp_be, exp_mp, exp_ml)
    # Golden cases from the founder's 2012 Excel (values re-verified by Fable 13-Sep-2026).
    run("Covered Call", [Leg("CE","SELL",5000,160), Leg("FUT","BUY",5150,0)], 50, [4990.0], 500.0, 249500.0)
    run("Long Straddle", [Leg("CE","BUY",4500,122), Leg("PE","BUY",4500,100)], 50, [4278.0,4722.0], "Unlimited", 11100.0)
    run("Bull Call Spread", [Leg("CE","BUY",4100,170.45), Leg("CE","SELL",4400,35.4)], 50, [4235.05], 8247.5, 6752.5)
    # Excel showed BEs 3874.45/2325.55 for this one -- WRONG. Correct values asserted here.
    run("Long Call Butterfly", [Leg("CE","BUY",3100,141.55,5), Leg("CE","SELL",3200,98,10), Leg("CE","BUY",3300,64,5)], 20, [3109.55,3290.45], 9045.0, 955.0)
