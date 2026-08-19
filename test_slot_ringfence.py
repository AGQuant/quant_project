"""
test_slot_ringfence.py — cc#1100 (replaces test_v7b_slot_neutrality.py)

The V7-B shadow basket lasted about eight hours: the founder ruled 19-Aug that V7-B REPLACES the
V6.1 filter set on the LIVE sell_reversal basket, so there is no `sell_reversal_v7b` tag left to
ring-fence. That made the old test's premise false, and a test asserting a withdrawn design is
worse than no test — it fails for the right reason on the wrong day.

What still needs guarding is the thing that test was really protecting: the slot branch every live
basket passes through. So this asserts the surviving rule.

  1. All four LIVE baskets enter the slot branch — buy_reversal, sell_reversal, buy_momentum,
     sell_momentum. sell_reversal matters most here: it now carries V7-B and it MUST still respect
     the mood cap and the shared SELL pool, because "same slots, same book" was the ruling.
  2. s1_reclaim_obs (cc#714) is still exempt, and is the ONLY exemption.
  3. The standard pool's COUNT excludes exactly the exempt set — the "both ways" half of the
     ring-fence, and the half that is easy to forget.
  4. Guard and SQL are driven by the SAME set. A second literal would let the two halves drift,
     which is precisely how a ring-fence leaks.
  5. The withdrawn shadow is gone everywhere, not just from the dispatch list.

Run:  python3 test_slot_ringfence.py
"""

import ast
import sys

SRC = "v8_signal_writer.py"
LIVE_BASKETS = ["buy_reversal", "sell_reversal", "buy_momentum", "sell_momentum"]
EXEMPT_EXPECTED = {"s1_reclaim_obs"}


def _load_constants(src_text):
    """Evaluate only the basket-name constants, without importing the module (it needs psycopg)."""
    tree = ast.parse(src_text)
    ns = {}
    keep = {"S1REC_BASKET", "_SLOT_EXEMPT_BASKETS"}
    body = [n for n in tree.body
            if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in keep]
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "<t>", "exec"), ns)
    return ns


def main():
    src = open(SRC, encoding="utf-8").read()
    exempt = _load_constants(src)["_SLOT_EXEMPT_BASKETS"]
    fails = []

    print("exempt set:", sorted(exempt))

    for b in LIVE_BASKETS:
        gated = b not in exempt
        print("  %-16s enters the slot branch: %s" % (b, gated))
        if not gated:
            fails.append("LIVE BASKET %s IS EXEMPT — it would ignore the mood cap" % b)

    if exempt != EXEMPT_EXPECTED:
        fails.append("exempt set is %s, expected %s" % (sorted(exempt), sorted(EXEMPT_EXPECTED)))
    else:
        print("  s1_reclaim_obs is the only exemption: OK")

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

    # 5 — the withdrawn shadow must not survive as a basket NAME anywhere. Prose about the
    # withdrawal is fine and is why this checks the quoted tag rather than the bare word.
    stray = [ln.strip() for ln in src.splitlines() if '"sell_reversal_v7b"' in ln]
    if stray:
        fails.append("the withdrawn shadow basket tag is still present: %s" % stray[:2])
    else:
        print("  withdrawn shadow tag appears nowhere as a basket name: OK")

    print()
    if fails:
        print("FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("SLOT RING-FENCE OK — four live baskets gated, one observation basket exempt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
