"""test_tc_resolver_guard.py — cc#1549: automated guard for the tc_resolver.py single-import-point rule.

THE RULE (tc_resolver.py's own docstring, cc#738, founder rule 28-Jul): every Python consumer of
Trade Check imports the scorer from tc_resolver (get_primary_tc / get_primary_styles / primary_tc /
primary_styles), never a versioned tc_* module directly. "A direct `from tc_v4_endpoints import
trade_check_v4` (or any versioned tc_* import) in a new call site is a code-review failure."

That rule existed but nothing enforced it — cc#1540 imported native_trade_check directly into
v8_pivot_star.py and nobody caught it until cc#1548 traced a live discrepancy back to it. This test
is the enforcement: an AST scan of every .py file in the repo root for a direct import of one of the
five scoring ENTRYPOINTS (not the shared low-level helpers _f/_r/_rsi/_derive/score_card/best_card/
etc that tc_v4_dual.py and tc_v4_scan.py themselves legitimately reuse) from one of the five known
TC modules, outside tc_resolver.py itself.

Two import shapes are both caught:
  - `from tc_v4_dual import trade_check_v4_dual`                      (ImportFrom)
  - `import tc_v4_dual` ... `tc_v4_dual.trade_check_v4_dual(sym, ...)`  (Import + attribute call)

KNOWN EXCEPTIONS (do not silently re-add a bad import elsewhere and expect this list to cover it —
each entry here is a SPECIFIC pre-existing file:name pair, flagged to the founder in cc#1549's
completion log as a genuine consumer of a NON-PRIMARY engine version, awaiting an explicit product
decision rather than a silent engine swap):
  - check_endpoint.py       imports native_trade_check.compute_trade_check / compute_single_rule.
                             Mounted at POST /api/check + GET /api/check/rule/{rule} (legacy v3.4
                             composite-card shape). scorr_check.html (the live /check page) calls
                             /api/trade-check/v4/dual instead — this route has no confirmed live UI
                             caller today, but migrating it would silently change its response SHAPE
                             (v3.4 Tier1+Tier2 card vs v4's shape), which is a behaviour change, not
                             import hygiene. Flagged, not migrated.
  - tc_intraday.py           imports native_trade_check as ntc, calls ntc.compute_trade_check. Its
                             own docstring: "Standalone, manually triggerable. Scheduler wiring
                             deferred to phase 1.5" — not mounted in main.py, not scheduled. Dormant
                             prototype, engine choice was deliberate at build time. Flagged.
  - native_router.py         imports native_trade_check.native_trade_check for the Ask Scorr chat's
                             Layer 0a "Trade Check v3.3" trigger — a LIVE, user-facing consumer that
                             answers chat queries ("check RELIANCE long") on the OLD v3.3 engine
                             while every dashboard shows v4. Highest-priority flag of the four below;
                             migrating it changes which verdict a live chat user sees, so it needs an
                             explicit founder call, not a silent audit fix.
  - trade_check_v34_endpoints.py  imports trade_check_v34.trade_check, trade_check_v36.trade_check_v36
                             (plus native_trade_check and tc_intraday, both already listed above).
                             Its OWN docstring is "Trade Check v3.4 endpoints" — a deliberately
                             version-scoped legacy API surface (`/api/trade-check/v34`, `/v36`), not a
                             consumer that drifted. Mounted in main.py; no live UI page calls it today
                             (grepped). Migrating it would defeat the endpoint's purpose (querying the
                             v3.4/v3.6 engine BY NAME), so it is flagged, not touched.

Excluded entirely (founder-approved OPTION B, 12-Jul chat — standalone binary buckets, NOT the TC V4
scoring engine, per tc_scanner_endpoints.py's own docstring):
  - tc_scanner_endpoints.py
  - intraday_scanner_endpoints.py

Run:  python3 test_tc_resolver_guard.py   OR   pytest test_tc_resolver_guard.py
"""
import ast
import os

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# module -> the scoring ENTRYPOINT names that must be resolver-routed (not the shared helpers).
GUARDED_ENTRYPOINTS = {
    "native_trade_check": {"compute_trade_check", "compute_single_rule", "native_trade_check"},
    "trade_check_v34": {"trade_check"},
    "trade_check_v36": {"trade_check_v36"},
    "tc_v4_endpoints": {"trade_check_v4"},
    "tc_v4_dual": {"trade_check_v4_dual"},
}

# tc_resolver.py IS the resolver — its own internal imports of the versioned modules are the one
# sanctioned place these entrypoints get named.
EXEMPT_FILES = {"tc_resolver.py"}

# Founder-approved standalone engines — not the TC V4 R1-R16 scoring engine, out of scope entirely.
EXCLUDED_FILES = {"tc_scanner_endpoints.py", "intraday_scanner_endpoints.py"}

# file -> set of (module, name) pairs that are a KNOWN, already-flagged exception (see docstring
# above). Anything NOT in this set that the scan finds is a NEW violation and fails the test.
KNOWN_EXCEPTIONS = {
    "check_endpoint.py": {("native_trade_check", "compute_trade_check"),
                          ("native_trade_check", "compute_single_rule")},
    "tc_intraday.py": {("native_trade_check", "compute_trade_check")},
    "native_router.py": {("native_trade_check", "native_trade_check")},
    "trade_check_v34_endpoints.py": {("trade_check_v34", "trade_check"),
                                      ("trade_check_v36", "trade_check_v36"),
                                      ("native_trade_check", "compute_trade_check")},
}


def _repo_py_files():
    for name in sorted(os.listdir(REPO_ROOT)):
        if name.endswith(".py") and os.path.isfile(os.path.join(REPO_ROOT, name)):
            yield name


def _scan_file(fname):
    """Return a set of (module, name) violations found in this one file."""
    path = os.path.join(REPO_ROOT, fname)
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src, filename=fname)

    violations = set()

    # module-alias -> real module name, for `import tc_v4_dual` / `import tc_v4_dual as x` shapes
    module_alias = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in GUARDED_ENTRYPOINTS:
            entrypoints = GUARDED_ENTRYPOINTS[node.module]
            for alias in node.names:
                if alias.name in entrypoints:
                    violations.add((node.module, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in GUARDED_ENTRYPOINTS:
                    module_alias[alias.asname or alias.name] = alias.name

    if module_alias:
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                real_module = module_alias.get(node.value.id)
                if real_module and node.attr in GUARDED_ENTRYPOINTS[real_module]:
                    violations.add((real_module, node.attr))

    return violations


def find_new_violations():
    """Scan the repo; return {filename: {(module, name), ...}} for anything not already known."""
    new = {}
    for fname in _repo_py_files():
        if fname in EXEMPT_FILES or fname in EXCLUDED_FILES:
            continue
        found = _scan_file(fname)
        if not found:
            continue
        allowed = KNOWN_EXCEPTIONS.get(fname, set())
        unexpected = found - allowed
        if unexpected:
            new[fname] = unexpected
    return new


def test_no_new_direct_tc_imports():
    new = find_new_violations()
    assert not new, (
        "Direct import of a versioned TC scoring entrypoint outside tc_resolver.py — route through "
        "tc_resolver.get_primary_tc() / get_primary_styles() instead, or add a documented exception "
        "to KNOWN_EXCEPTIONS in this file if it is genuinely intentional (cc#1549):\n" +
        "\n".join(f"  {f}: {sorted(v)}" for f, v in sorted(new.items()))
    )


if __name__ == "__main__":
    new = find_new_violations()
    if new:
        print("FAIL — new direct TC imports found outside tc_resolver.py:")
        for f, v in sorted(new.items()):
            print(f"  {f}: {sorted(v)}")
        raise SystemExit(1)
    print("OK — no direct TC scoring-entrypoint imports outside tc_resolver.py "
          "beyond the documented KNOWN_EXCEPTIONS.")
