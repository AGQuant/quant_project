"""
tc_resolver.py — cc#738: the ONE canonical Trade Check resolver.

Every PYTHON consumer of Trade Check imports the scorer from HERE (get_primary_tc / primary_tc),
never a versioned tc_* module directly. Flipping the platform's primary TC scorer then becomes a
one-line change to PRIMARY_TC_VERSION below (or, later, a DB/config flag) that propagates to every
consumer at once — the founder rule (28-Jul): when the TC version changes it must update EVERYWHERE,
no consumer left calling an old scorer.

    NEW TC CONSUMERS MUST IMPORT THIS RESOLVER. A direct `from tc_v4_endpoints import trade_check_v4`
    (or any versioned tc_* import) in a new call site is a code-review failure.

────────────────────────────────────────────────────────────────────────────────────────────────────
DISCREPANCY FLAGGED FOR FOUNDER DECISION (cc#737 + cc#738):
  STOCK_VIEWS_FRAMEWORK_V1 / registry id=10062 names "TC v3.3.2" as PRIMARY, but there is NO v3.3.2
  CODE module — per trade_check_v34.py's header, "v3.3 was conversational-only (spec session_log
  id=143)". The scorers that actually ship are:
      - trade_check_v34.py        VERSION v3.5   (weighted Tier-1 + core-gate)
      - trade_check_v36.py        VERSION v3.6   (Tier-1 process gate, side-by-side)
      - native_trade_check.py     native v3.4    (unified single-tier)
      - tc_v4_endpoints.py        VERSION v4.0   (22-pt Tier1+Tier2 + verdict)  <-- de-facto PRIMARY
      - tc_v4_dual.py             v4 dual-style  (LONG+SHORT in one pass; different return shape)
  Every LIVE (symbol, direction) -> verdict consumer today calls tc_v4_endpoints.trade_check_v4.
  To honour cc#738's "No scoring-logic changes anywhere — pure wiring/indirection", the resolver's
  default is therefore the DE-FACTO production primary, v4.0 — NOT a non-existent v3.3.2 module.
  Founder to reconcile the registry "v3.3.2" label against the real shipped versions, then set the
  true primary by editing PRIMARY_TC_VERSION (one line) — no consumer edits needed.

  Flip mechanism: a single module constant (implemented). A DB-flag option (e.g. app_config
  tc_primary_version) can be layered on later by making _resolve_version() read the row and fall back
  to this constant — kept as a constant first per cc#738 ("implement constant first, note DB-flag").
────────────────────────────────────────────────────────────────────────────────────────────────────

Contract: get_primary_tc() returns a callable  fn(symbol: str, direction: str="LONG") -> dict
with the canonical Trade Check shape (keys incl. final_verdict, tier1{score,max}, tier2{score,max},
cmp). Consumers must not assume anything version-specific beyond that shape.
"""

# Single source of truth for the platform-wide primary TC scorer. Flip HERE to change everyone.
PRIMARY_TC_VERSION = "v4.0"

# Versioned scorers that remain importable EXPLICITLY (side-by-side validation surfaces only) — never
# import these directly in a new consumer; go through get_primary_tc() unless you are specifically
# building a version-comparison surface.
_KNOWN_VERSIONS = ("v4.0",)


def _resolve_version() -> str:
    """The active primary version. A constant today; swap the body for an app_config read later to
    make the flip a DB change instead of a deploy (cc#738 DB-flag option)."""
    return PRIMARY_TC_VERSION


def get_primary_tc():
    """Return the canonical Trade Check scorer callable: fn(symbol, direction='LONG') -> dict."""
    v = _resolve_version()
    if v == "v4.0":
        from tc_v4_endpoints import trade_check_v4
        return trade_check_v4
    # Unknown/unshipped version (e.g. the registry's "v3.3.2" placeholder) -> fall back to the
    # de-facto production primary so a mislabel never breaks live consumers. The discrepancy is
    # documented above for founder reconciliation.
    from tc_v4_endpoints import trade_check_v4
    return trade_check_v4


def primary_tc(symbol, direction="LONG"):
    """One-shot convenience: score `symbol`/`direction` on the canonical scorer."""
    return get_primary_tc()(symbol, direction)
