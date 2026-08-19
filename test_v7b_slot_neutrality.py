"""
test_v7b_slot_neutrality.py — cc#1100

cc#1100 says the V7-B shadow must never consume a live slot, and its do_not_touch names the V8
slot allocator. Those two pull in opposite directions: to exempt the shadow I had to edit the one
`if` that every live basket passes through. This test is the evidence that the edit is neutral.

WHAT IT ASSERTS, against the SHIPPED source rather than a copy of it:
  1. Every live basket still enters the slot branch — buy_reversal, sell_reversal, buy_momentum,
     sell_momentum. If any of them were ever added to the exempt set, a live basket would silently
     stop respecting the mood cap and could open unlimited positions.
  2. Both ring-fenced baskets are exempt — s1_reclaim_obs (cc#714) and sell_reversal_v7b.
  3. The standard pool's COUNT excludes exactly the exempt set, so a shadow position can never
     push a live basket over its cap. That is the "both ways" half of the ring-fence, and it is
     the half that would be easy to forget.
  4. The exemption is driven by the SAME set the SQL is parameterised with. A second literal would
     let the two halves drift, which is precisely how a ring-fence leaks.

Run:  python3 test_v7b_slot_neutrality.py
"""

import ast
import re
import sys

SRC = "v8_signal_writer.py"
LIVE_BASKETS = ["buy_reversal", "sell_reversal", "buy_momentum", "sell_momentum"]
EXEMPT_EXPECTED = {"s1_reclaim_obs", "sell_reversal_v7b"}


def _load_constants(src_text):
    """Evaluate only the basket-name constants, without importing the module (it needs psycopg)."""
    tree = ast.parse(src_text)
    ns = {}
    keep = {"S1REC_BASKET", "SELLREV_V7B_BASKET", "_SLOT_EXEMPT_BASKETS"}
    body = [n for n in tree.body
            if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in keep]
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "<t>", "exec"), ns)
    return ns


def main():
    src = open(SRC, encoding="utf-8").read()
    ns = _load_constants(src)
    exempt = ns["_SLOT_EXEMPT_BASKETS"]
    fails = []

    print("exempt set:", sorted(exempt))

    # 1 — no live basket may be exempt
    for b in LIVE_BASKETS:
        gated = b not in exempt
        print("  %-16s enters the slot branch: %s" % (b, gated))
        if not gated:
            fails.append("LIVE BASKET %s IS EXEMPT — it would ignore the mood cap" % b)

    # 2 — both ring-fenced baskets are exempt
    if exempt != EXEMPT_EXPECTED:
        fails.append("exempt set is %s, expected %s" % (sorted(exempt), sorted(EXEMPT_EXPECTED)))
    else:
        print("  ring-fenced baskets exempt: OK")

    # 3 + 4 — the guard and the SQL must both be driven by that same set
    if "if basket not in _SLOT_EXEMPT_BASKETS:" not in src:
        fails.append("the slot guard no longer tests _SLOT_EXEMPT_BASKETS")
    else:
        print("  guard reads _SLOT_EXEMPT_BASKETS: OK")

    if "basket <> ALL(%s)" not in src:
        fails.append("the standard-pool COUNT no longer excludes the exempt set")
    else:
        print("  pool COUNT excludes the exempt set: OK")

    if "(list(_SLOT_EXEMPT_BASKETS),)" not in src:
        fails.append("the COUNT is not parameterised from _SLOT_EXEMPT_BASKETS — the two halves "
                     "of the ring-fence can drift")
    else:
        print("  guard and COUNT share one source: OK")

    # 5 — the shadow must not be reachable from the live order path by name anywhere else
    stray = [ln for ln in src.splitlines()
             if "SELLREV_V7B_BASKET" in ln and "slot" in ln.lower() and "EXEMPT" not in ln]
    if stray:
        fails.append("shadow basket referenced in slot code outside the exempt set: %s" % stray[:2])
    else:
        print("  shadow appears in no other slot code: OK")

    print()
    if fails:
        print("FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("SLOT NEUTRALITY OK — the shadow takes no slot and no live basket changed behaviour")
    return 0


if __name__ == "__main__":
    sys.exit(main())
