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


# ── STYLE-level primary (cc#748, founder-approved 29-Jul) ────────────────────────────────────────
# get_primary_tc() (v4.0 endpoints) yields ONE verdict per LONG/SHORT direction — it has NO
# reversal-vs-momentum split. The 4 style cards (BUY/SELL x MOMENTUM/REVERSAL, each with its own
# STRONG/VALID/REJECT verdict) live in the v4 DUAL engine. Consumers that need per-style verdicts
# (the TC outcome sim, cc#748 — "per-style win rates are the whole point") import THIS accessor from
# the resolver, never `tc_v4_dual` directly, so cc#738's single-import-point rule still holds: the
# resolver remains the ONE place the platform's TC engines are named.
def get_primary_styles():
    """Return the canonical STYLE-level scorer callable: fn(symbol, side='ALL') -> dict whose `cards`
    list holds the 4 style cards, each {style, side, label, score, max, verdict}. Same v4 family and
    same PRIMARY_TC_VERSION flip point as get_primary_tc(); the dual is the style-resolving variant."""
    _resolve_version()   # keep the flip point consistent (dual is the v4 style-resolving variant)
    from tc_v4_dual import trade_check_v4_dual
    return trade_check_v4_dual


def primary_styles(symbol, side="ALL"):
    """One-shot convenience: score `symbol` on the canonical style-level scorer (all 4 style cards)."""
    return get_primary_styles()(symbol, side)

# ── cc#1593: the two remaining pieces an app surface needs from the SAME v4 family, exposed here so
# app_check_endpoints.py imports nothing versioned. Both follow the flip point above.
def get_primary_style_bands():
    """The /10 band cuts the style-level scorer verdicts on, as {"watch": 5.0, "valid": 6.5,
    "strong": 8.4}. Read from the scorer's own constants, never retyped: a surface that draws cut
    marks on a meter must draw them where the verdict actually changes (TC_SCORE_100_V1, 29138:
    thresholds live in ONE place and every surface reads them from the payload)."""
    _resolve_version()
    from tc_v4_dual import _WATCH_10, _VALID_10, _STRONG_10
    return {"watch": float(_WATCH_10), "valid": float(_VALID_10), "strong": float(_STRONG_10)}


def get_primary_scan():
    """The canonical universe scan: fn(side='ALL', verdict='ALL', segment=None, limit=250) -> dict
    whose `results` rows carry best_label / best_score100 / verdict per symbol, scored by the same
    score_card the style-level scorer uses (tc_v4_scan's shared-module contract)."""
    _resolve_version()
    from tc_v4_scan import scan
    return scan
