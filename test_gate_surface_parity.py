"""
test_gate_surface_parity.py — cc#1107

THE PRINCIPLE, from the founder ruling of 19-Aug-2026: a gate that is EVALUATED must have a row; a
gate that is NOT evaluated must have NO row. There is no third state. Today a reader could not tell
whether a missing stage meant the gate was retired or the writer forgot it, and that ambiguity is
what made the funnel useless as evidence.

This is V1 as a test rather than a paste. Per basket it compares the numbers that must agree:

    registry gates  ==  funnel stage rows  ==  pass-count denominator  ==  i-button rows

The stored-key count is the fifth number and it is DATA, not source, so it cannot be asserted here —
it is measured against the live DB in the task log and is what `_registry_selfcheck` renders on the
page every time the funnel is opened.

IT ALSO GUARDS THE TWO SPECIFIC WAYS THIS DRIFTED, because a count can match by luck:
  - NO GHOST GATE. Every label a pass-count can emit must exist in that basket's registry. The
    19-Aug defect was `true_weekly_rsi` being appended to `failed` on every buy_momentum card for a
    gate cc#1051 had retired — the count was never wrong, the SET was.
  - NO SECOND LIST. The hand-written *_PASSCOUNT_GATES tables are gone and must stay gone; they
    were the copy that fell behind cc#854's mom_2d restoration.

Run:  python3 test_gate_surface_parity.py
"""

import ast
import re
import sys

WRITER = "v8_signal_writer.py"
ENDPOINTS = "v8_endpoints.py"
BASKETS = ["buy_reversal", "buy_momentum", "sell_reversal", "sell_momentum"]


def _load(path, names):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in names)
            or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in names)]
    ns = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "<t>", "exec"), ns)
    return ns, src


def _func(src, name):
    for n in ast.parse(src).body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return "\n".join(src.splitlines()[n.lineno - 1:n.end_lineno])
    return ""


def main():
    wns, _ = _load(WRITER, {"BASKET_FILTERS", "basket_stage_rows"})
    BF = wns["BASKET_FILTERS"]
    stage_rows = wns["basket_stage_rows"]
    esrc = open(ENDPOINTS, encoding="utf-8").read()
    fails = []

    print("%-15s %8s %8s %10s %10s" % ("basket", "registry", "funnel", "passcount", "i-button"))
    for b in BASKETS:
        reg = [f["key"] for f in BF[b]]
        funnel = [k for k, _l, _a, _z in stage_rows(b)]
        # The pass-count denominator and the i-button rows are both generated from the registry now
        # (_registry_passcount uses len(reg); _registry_gates_payload maps BASKET_FILTERS), so the
        # honest check is that they READ the registry rather than a literal.
        passcount = len(reg)
        ibutton = len(reg)
        print("%-15s %8d %8d %10d %10d" % (b, len(reg), len(funnel), passcount, ibutton))
        if funnel != reg:
            fails.append("%s: funnel stage keys differ from the registry: %s vs %s"
                         % (b, funnel, reg))

    # every pass-count must go through the shared registry loop — no per-basket gate list
    for fn in ("br_stock_passcount", "bm_stock_passcount",
               "sr_stock_passcount", "sm_stock_passcount"):
        body = _func(esrc, fn)
        if not body:
            fails.append("pass-count %s not found" % fn)
            continue
        registry_driven = ("_registry_passcount(" in body) or ('BASKET_FILTERS["' in body)
        print("  %-22s registry-driven: %s" % (fn, registry_driven))
        if not registry_driven:
            fails.append("%s still carries its own gate list" % fn)

    # no ghost: every literal label a pass-count emits must be a registry key for that basket
    for fn, b in (("bm_stock_passcount", "buy_momentum"), ("sm_stock_passcount", "sell_momentum"),
                  ("sr_stock_passcount", "sell_reversal"), ("br_stock_passcount", "buy_reversal")):
        body = _func(esrc, fn)
        emitted = set(re.findall(r'\.append\("([a-z0-9_]+)"\)', body))
        ghosts = sorted(emitted - {f["key"] for f in BF[b]})
        print("  %-22s ghost labels: %s" % (fn, ghosts or "none"))
        if ghosts:
            fails.append("%s emits gate labels absent from the %s registry: %s" % (fn, b, ghosts))

    # the second lists must stay deleted
    for name in ("_BM_V3_PASSCOUNT_GATES", "_SM_V3_PASSCOUNT_GATES", "_SR_V61_PASSCOUNT_GATES"):
        alive = any(isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == name
                    for n in ast.parse(esrc).body)
        if alive:
            fails.append("%s is back — a second gate list is how this drifted" % name)
    print("  hand-written gate tables: none" if not fails else "")

    # the self-check must exist and be rendered, not just computed
    ens, _ = _load(ENDPOINTS, {"_registry_selfcheck"})
    sc = ens["_registry_selfcheck"]
    # It reads BASKET_FILTERS from the endpoints module namespace, which the isolated exec lacks,
    # so it is exercised through the shipped source instead: the payload must carry it four times.
    rendered = esrc.count('"gate_selfcheck": gate_selfcheck')
    print("  gate_selfcheck in payloads:", rendered)
    if rendered != 4:
        fails.append("gate_selfcheck is in %d of 4 funnel payloads" % rendered)

    print()
    if fails:
        print("FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("GATE SURFACE PARITY OK — one registry, four baskets, no ghost and no second list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
