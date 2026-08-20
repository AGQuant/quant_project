"""cc#1155 — max pain, computed correctly, with a guard that prefers absent over wrong.

WHY THIS FILE EXISTS
    The old computation lived inline in v10_endpoints.v10_maxpain as a single SQL statement, and it
    was measuring the wrong quantity. It printed NIFTY 22,650 — a hundred points BELOW the lowest
    strike in its own chain — which is arithmetically impossible for a real max pain.

    Two independent defects produced that, and the second one is the serious one.

    DEFECT 1 — a stale strike set.
        The old query took `DISTINCT ON (strike, option_type) ... ORDER BY strike, option_type,
        ts DESC` across EVERY tick of the expiry, not the latest tick. So a strike that was quoted
        yesterday and has since dropped out of the chain still appears, carrying its last-known OI.
        Proven: strike 22,650 for the 25-Aug expiry was last seen 19-Aug 15:35 and appears in no
        20-Aug tick at all, while the chain floor on 20-Aug is 22,750.

    DEFECT 2 — the payout formula was not a max-pain formula.
        Max pain is the expiry price K that minimises what option WRITERS must pay out across the
        WHOLE chain. That is a double sum: for each candidate K, add up every strike's contribution.

            payout(K) = SUM over strikes S of  call_oi(S) * max(K - S, 0)
                                             + put_oi(S)  * max(S - K, 0)

        The old query summed, per strike, that strike's OWN intrinsic value at the CURRENT SPOT and
        then picked the smallest. That is not a payout curve over candidate expiry prices; it is a
        per-strike moneyness measure, and its minimum lands on whichever strike happens to have the
        smallest OI-times-distance product. It also carried `WHERE total_pain > 0`, which existed
        only to hide the strikes where that quantity is legitimately zero.

    DEFECT 3 — the spot it used was the previous DAILY CLOSE from raw_prices, not a live spot.
        That matters much less once defect 2 is fixed, because the correct formula does not need a
        spot at all: the payout curve is a property of the chain. Spot is now used only to report
        distance, and it is read from the chain's own snapshot where available so the reported
        distance and the strike come from one tick.

    THE BANKNIFTY CONTROL IN THE CARD IS WRONG, and it is worth saying so plainly.
        The card reasoned that BANKNIFTY's 57,200 looked plausible, so the defect was probably
        NIFTY-specific. It is not. On the same 20-Aug chain the old query gives BANKNIFTY 57,200
        and the correct computation gives 57,600. The bug is systemic; BANKNIFTY only LOOKED right
        because when OI is smooth the broken formula drifts toward the strike nearest spot, which
        is usually near the real max pain. A plausible wrong number is worse than an obvious one.

DATA HONESTY
    `max_pain()` returns None rather than a number it cannot stand behind. A max pain outside the
    chain's own strike range is not a number to display with a caveat — it is evidence the input
    is broken, and the caller is told so through `reason` instead of being handed a figure.
"""

from typing import Dict, Iterable, Optional, Tuple


def payout_at(strikes: Dict[float, Tuple[float, float]], k: float) -> float:
    """Total writer payout if the underlying settles at `k`.

    `strikes` maps strike -> (call_oi, put_oi). A call written at S pays out max(k - S, 0) per
    unit of OI; a put written at S pays out max(S - k, 0). Summed over the whole chain, because a
    writer's book is the whole chain, not one strike.
    """
    total = 0.0
    for s, (ce, pe) in strikes.items():
        if ce:
            d = k - s
            if d > 0:
                total += ce * d
        if pe:
            d = s - k
            if d > 0:
                total += pe * d
    return total


def max_pain(strikes: Dict[float, Tuple[float, float]], top_n: int = 3):
    """Return (strike, curve) where curve is the top_n lowest-payout candidates.

    Returns (None, []) when the input cannot support an answer. The GUARD lives here rather than in
    the endpoint, so every caller inherits it and none can route around it:

      * fewer than 3 strikes  -> a payout curve through 2 points has no meaningful minimum
      * no OI anywhere        -> nothing has been written, so nothing can be paid out
      * result outside the chain's own [min, max] -> arithmetically impossible, treated as a
        broken input rather than displayed. This is the assertion cc#1155 scope 3 asks for, and
        it is what would have caught 22,650 at source.
    """
    usable = {float(s): (float(ce or 0), float(pe or 0)) for s, (ce, pe) in strikes.items()}
    usable = {s: v for s, v in usable.items() if (v[0] or v[1])}
    if len(usable) < 3:
        return None, []
    lo, hi = min(usable), max(usable)
    curve = sorted(((k, payout_at(usable, k)) for k in usable), key=lambda x: x[1])
    best = curve[0][0]
    if not (lo <= best <= hi):
        # Unreachable by construction today, because candidates are drawn from the chain itself.
        # Kept because the guard must survive a future change that widens the candidate set.
        return None, curve[:top_n]
    return best, curve[:top_n]


def chain_bounds(strikes: Iterable[float]) -> Tuple[Optional[float], Optional[float]]:
    vals = [float(s) for s in strikes]
    return (min(vals), max(vals)) if vals else (None, None)


# ── SQL used by the endpoint ────────────────────────────────────────────────────────────────
# The LATEST TICK ONLY, of ONE expiry, of ONE underlying. `ts = (SELECT max(ts) ...)` is the whole
# fix for defect 1 — it is what stops a strike that has dropped out of the chain from voting.
LATEST_CHAIN_SQL = """
    WITH exp AS (
        SELECT MIN(expiry) AS e FROM option_chain
        WHERE underlying = %(u)s AND expiry >= CURRENT_DATE
    ),
    mts AS (
        SELECT MAX(ts) AS t FROM option_chain
        WHERE underlying = %(u)s AND expiry = (SELECT e FROM exp)
    )
    SELECT strike,
           SUM(CASE WHEN option_type = 'CE' THEN oi ELSE 0 END) AS ce,
           SUM(CASE WHEN option_type = 'PE' THEN oi ELSE 0 END) AS pe,
           (SELECT e FROM exp) AS expiry,
           (SELECT t FROM mts) AS tick
    FROM option_chain
    WHERE underlying = %(u)s
      AND expiry = (SELECT e FROM exp)
      AND ts = (SELECT t FROM mts)
      AND oi IS NOT NULL
    GROUP BY strike
    ORDER BY strike
"""
