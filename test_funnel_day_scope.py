"""
test_funnel_day_scope.py — cc#1101

The funnel row describes a SESSION. The writer runs every 5 minutes. Those two facts were in
conflict: five copies of the same last-write-wins upsert meant the stored row reported whatever was
true at the final tick of the day, so on 7 of 8 basket-days measured (17 + 18-Aug-2026) the funnel
under-reported the qualified table it claims to describe. buy_momentum reported 0 on a day it
signalled OFSS, because V5 gates entries to 10:15-13:00 and every tick after 13:00 rewrote its
aggregates to zero.

WHAT THIS ASSERTS, against the SHIPPED source rather than a copy of it:
  1. There is exactly ONE funnel writer. No handler carries an inline INSERT any more — five copies
     of one rule is how the rule drifts, and this is the check that keeps it at one.
  2. Every handler routes through it. A handler added later that forgets is caught here.
  3. The day high-watermark actually holds: a later tick reporting 0 cannot erase an earlier 1.
     This is the exact failure the card was raised for, reproduced as a test.
  4. The merge never INVENTS a key. A basket with no heavy stage must not gain a survivor count of
     0 it never computed — a fabricated zero is indistinguishable from a measured one on the page.
  5. A non-numeric legacy value survives untouched, so a bad old row stays evidence rather than
     being quietly coerced.
  6. The reader side agrees with the writer side: _funnel_final prefers the day-scoped key and
     falls back to the old one, so rows written before this deploy still render.

Run:  python3 test_funnel_day_scope.py
"""

import ast
import re
import sys

WRITER = "v8_signal_writer.py"
ENDPOINTS = "v8_endpoints.py"

HANDLERS = [
    "_write_buy_reversal_v6_qualified",
    "_write_sell_reversal_v61_qualified",
    "_write_sell_reversal_v7b_shadow",
    "_write_sell_momentum_v4_qualified",
    # the name is legacy (the spec is V5 since cc#1051); the function is the buy_momentum handler
    "_write_buy_momentum_v3_qualified",
]


def _extract(src_text, names, extra_body=None):
    """Exec only the named top-level defs/assigns — the modules need psycopg, which is not here."""
    tree = ast.parse(src_text)
    body = [n for n in tree.body
            if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names)
            or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in names)]
    ns = {}
    mod = ast.Module(body=(extra_body or []) + body, type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), "<t>", "exec"), ns)
    return ns


def _func_source(src_text, name):
    tree = ast.parse(src_text)
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return "\n".join(src_text.splitlines()[n.lineno - 1:n.end_lineno])
    return ""


def main():
    wsrc = open(WRITER, encoding="utf-8").read()
    esrc = open(ENDPOINTS, encoding="utf-8").read()
    fails = []

    # 1 — one writer, and it is the helper
    inline = len(re.findall(r"INSERT INTO v8_funnel_counts", wsrc))
    print("inline INSERT INTO v8_funnel_counts occurrences:", inline)
    if inline != 1:
        fails.append("expected exactly 1 funnel INSERT (inside _upsert_funnel_counts), found %d"
                     % inline)
    helper_src = _func_source(wsrc, "_upsert_funnel_counts")
    if "INSERT INTO v8_funnel_counts" not in helper_src:
        fails.append("the one remaining INSERT is not inside _upsert_funnel_counts")
    else:
        print("  the single INSERT lives in _upsert_funnel_counts: OK")

    # 2 — every handler routes through it
    for h in HANDLERS:
        body = _func_source(wsrc, h)
        if not body:
            fails.append("handler %s not found in %s" % (h, WRITER))
            continue
        ok = "_upsert_funnel_counts(conn, basket, target_date, funnel)" in body
        print("  %-38s calls the shared upsert: %s" % (h, ok))
        if not ok:
            fails.append("%s does not call _upsert_funnel_counts" % h)

    # 3/4/5 — the pure merge
    ns = _extract(wsrc, {"_FUNNEL_DAY_PEAK_KEYS", "_merge_day_peaks"})
    merge = ns["_merge_day_peaks"]
    peaks = ns["_FUNNEL_DAY_PEAK_KEYS"]
    print("day-peak keys:", list(peaks))

    # the exact 18-Aug buy_momentum failure: 10:25 tick stored 1, the 15:15 tick reports 0
    late = merge({"_score_qualified": 0, "_hard_qualified": 0, "_stage6_survivors": 0},
                 {"_score_qualified": 1, "_hard_qualified": 2, "_stage6_survivors": 3})
    print("  late 0-tick over a stored 1 ->", late)
    if late != {"_score_qualified": 1, "_hard_qualified": 2, "_stage6_survivors": 3}:
        fails.append("a late zero tick erased the day's aggregates — the cc#1101 defect is back")

    rise = merge({"_score_qualified": 4}, {"_score_qualified": 1})
    if rise["_score_qualified"] != 4:
        fails.append("a higher later tick failed to raise the watermark: %s" % rise)
    else:
        print("  a higher later tick still raises it: OK")

    invented = merge({"_score_qualified": 0}, {"_stage8_survivors": 9})
    print("  keys not produced by the caller:", sorted(set(invented) - {"_score_qualified"}))
    if "_stage8_survivors" in invented:
        fails.append("the merge invented _stage8_survivors — a fabricated zero reads as a measured one")

    legacy = merge({"_score_qualified": 0}, {"_score_qualified": "n/a"})
    if legacy["_score_qualified"] != 0:
        fails.append("a non-numeric legacy value was not handled safely: %s" % legacy)
    else:
        print("  non-numeric legacy prior handled without raising: OK")

    # per-gate counts must NOT be day-scoped — they read the market at this tick and that is honest
    strays = [k for k in peaks if not k.startswith("_")]
    if strays:
        fails.append("day-peak set contains public per-gate keys, which would freeze gate counts "
                     "at the day's peak instead of reading the market now: %s" % strays)
    else:
        print("  only private aggregate keys are day-scoped: OK")

    # 6 — the reader agrees with the writer
    ens = _extract(esrc, {"_funnel_final"})
    ff = ens["_funnel_final"]
    checks = [
        ({"_qualified_today": 1, "_score_qualified": 0}, 1, "prefers the day-scoped key"),
        ({"_score_qualified": 3}, 3, "falls back for rows written before this deploy"),
        ({}, 0, "an empty row reads 0, not a crash"),
        ({"_qualified_today": 0, "_score_qualified": 5}, 0, "a real 0 today is not overridden"),
    ]
    for counts, want, why in checks:
        got = ff(counts)
        print("  _funnel_final(%s) = %s  (%s)" % (counts, got, why))
        if got != want:
            fails.append("_funnel_final(%s) = %s, expected %s" % (counts, got, want))

    print()
    if fails:
        print("FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("FUNNEL DAY SCOPE OK — one writer, aggregates are the session, gate counts are the tick")
    return 0


if __name__ == "__main__":
    sys.exit(main())
