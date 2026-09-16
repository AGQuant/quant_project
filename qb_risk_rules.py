"""
qb_risk_rules.py -- cc#2128: Quant Basket Risk Management V1.

Three risk controls governing how a qualifying, entered, held position is SIZED and CAPPED within
a basket -- not whether it qualifies/enters/exits (that is cc#2126/cc#2127's job, untouched here).
Founder words, 16-Sep: "basket cap, maximum stock. sector cap in terms of weightage, maximum
sector can be capped. and allocation -- if there is cash left, allocation in cash, gold, silver
ETF, anywhere, user can define in percentage. These three basic things for risk management as of
now, we will add up later. After that, backtesting."

WHAT ALREADY EXISTS, VERIFIED BEFORE BUILDING (data-source gate, Role Split: "CC greps and answers
before the build proceeds") -- the card's own spec named all three, checked here rather than
trusted verbatim:

1. BASKET CAP is already live. Read qb_config.py and quant_basket_registry directly: large_cap=15,
   mid_cap=20, small_cap=20, breakout_52w=10, contra_value=10, alpha_multicap=15 -- matches the
   card's own QB_CAP_AMENDMENT_V1 citation exactly. qb_config.basket_params(conn, basket_name) is
   the ONE real read path (also covers finz_* and model_portfolio, not named by this card but
   confirmed present). get_basket_cap_default() below calls it directly -- reused, not reimplemented.

2. SECTOR CAP does not exist anywhere -- confirmed, no sector-weight ceiling in any selection
   engine (qb_composite_select.py, qb_smallcap_select.py, qb_alpha_select.py all grepped, zero
   segment-weight logic in any of them). The one genuinely new control. apply_sector_cap() below
   is new code.

3. RESIDUAL ALLOCATION is partially live. Read qb_rebalance.py directly: compute_position_sizing()
   /fix_basket_overdeployment() already route leftover capital into NIFTYBEES only, CASH_CAP_PCT
   = 0.05 (5%). This card generalises to a founder-defined percentage split across CASH/GOLDBEES/
   SILVERBEES/NIFTYBEES -- NIFTYBEES stays available as a fourth sleeve, not removed.

SCOPE FOR THIS PASS, stated before writing code (cc_task_logs task 2128), mirroring cc#2126/
cc#2127's own precedent exactly: builds and hand-verifies the three risk primitives + persistence.
Does NOT splice these into qb_rebalance.py's live position-sizing path (still NIFTYBEES-only,
untouched) or v12_backtest.py, and does NOT build a V12 wizard control -- both deferred to cc#2129
("wire cc#2123/2126/2127/2128 into the EXISTING v12_backtest.py engine") per this card's own
"next: backtesting" sequencing. Does NOT touch qb_rebalance.py's Rs 5L capital-per-basket
convention or its fill-to-cap logic (reused via qb_config, never rewritten), and does NOT alter
the live production baskets' current caps (15/20/10) -- this card adds the editable CONTROL for
V12-built baskets, it does not retroactively change what is running today.

PERSISTENCE NOTE (third time this exact flag has been raised, cc#2126 -> cc#2127 -> here):
qb_risk_rulesets is a THIRD separate table, same shape convention as qb_entry_rulesets/
qb_exit_rulesets, deliberately not merged into either via ALTER TABLE -- MAINTENANCE_LOCK_RULE
(rule 10) names ALTER TABLE explicitly as Railway-console-only/propose-first. cc#2129 now has
three sibling tables to reconcile into one basket-definition object (or leave as three joined by
basket_name) -- a decision this card does not make unilaterally.
"""
import json
from typing import Dict, Any, List

_ALLOWED_SLEEVES = {"cash", "GOLDBEES", "SILVERBEES", "NIFTYBEES"}


def ensure_schema(conn):
    """CREATE TABLE only (rule 10)."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS qb_risk_rulesets (
              id SERIAL PRIMARY KEY,
              basket_name TEXT NOT NULL,
              ruleset_name TEXT NOT NULL DEFAULT 'default',
              max_stocks INTEGER,
              sector_cap_pct NUMERIC,
              residual_split JSONB,
              created_at TIMESTAMPTZ DEFAULT NOW(),
              updated_at TIMESTAMPTZ DEFAULT NOW(),
              UNIQUE (basket_name, ruleset_name)
            )""")
        conn.commit()


# ── 1. basket cap ─────────────────────────────────────────────────────────────────────────────

def get_basket_cap_default(conn, basket_name: str) -> Dict[str, Any]:
    """Reuses qb_config.basket_params -- the ONE real read path for a basket's production
    max_stocks/capital (rule: reused, not rewritten). A basket with no existing production row
    (a new, V12-only basket) is a valid, honest case -- not an error -- so the LookupError
    basket_params raises by design is caught and reported plainly, not propagated."""
    import qb_config
    try:
        p = qb_config.basket_params(conn, basket_name)
        return {"has_production_default": True, "max_stocks": p["max_stocks"],
                "capital": p["capital"], "source": p["source"]}
    except LookupError:
        return {"has_production_default": False, "max_stocks": None, "capital": None,
                "source": None,
                "note": f"{basket_name} has no existing production cap -- a new V12-only basket, "
                        "the founder sets max_stocks directly with no default to match"}


# ── 2. sector cap (genuinely new) ─────────────────────────────────────────────────────────────

def apply_sector_cap(candidates: List[Dict[str, Any]], max_pct: float, capital: float,
                      max_stocks: int, segment_key: str = "segment") -> Dict[str, Any]:
    """Walks rank-ordered candidates (candidates[0] = highest ranked), filling equal-weight slots
    (slot = capital / max_stocks, the SAME fill-to-cap-in-rank-order convention qb_config.
    size_slots already uses -- not a new sizing rule). A candidate whose entry would push its
    segment's cumulative slot value over max_pct% of capital is SKIPPED (never force-included at
    a trimmed weight) -- the next-ranked candidate is evaluated for the same still-open slot.
    Every skip records the segment and the % it would have reached, not just a boolean, per the
    card's own verify requirement."""
    slot = round(capital / max_stocks, 2) if max_stocks else 0.0
    seg_capital: Dict[str, float] = {}
    filled: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for c in candidates:
        if len(filled) >= max_stocks:
            break
        seg = c.get(segment_key) or "Unknown"
        would_be = seg_capital.get(seg, 0.0) + slot
        pct = (would_be / capital * 100.0) if capital else 0.0
        if pct > max_pct + 1e-9:
            skipped.append({"symbol": c.get("symbol"), "segment": seg,
                             "would_reach_pct": round(pct, 2), "cap_pct": max_pct,
                             "reason": "sector_cap"})
            continue
        seg_capital[seg] = would_be
        filled.append({"symbol": c.get("symbol"), "segment": seg, "slot_value": slot})
    return {"filled": filled, "skipped": skipped, "slot_value": slot,
            "max_pct": max_pct, "max_stocks": max_stocks, "capital": capital}


# ── 3. residual allocation split ──────────────────────────────────────────────────────────────

def validate_residual_split(split: Dict[str, float]) -> Dict[str, Any]:
    """Percentages must sum to 100 OF THE RESIDUAL, not the whole basket. Rejected (not silently
    normalised) with the shortfall/excess stated, per the card's own explicit instruction."""
    if not split:
        return {"valid": False, "total_pct": 0.0, "reason": "empty split"}
    unknown = set(split) - _ALLOWED_SLEEVES
    if unknown:
        return {"valid": False, "total_pct": None,
                "reason": f"unknown sleeve(s): {sorted(unknown)} -- allowed: {sorted(_ALLOWED_SLEEVES)}"}
    total = round(sum(float(v) for v in split.values()), 6)
    if abs(total - 100.0) > 1e-6:
        diff = round(total - 100.0, 4)
        word = "over" if diff > 0 else "short of"
        return {"valid": False, "total_pct": total,
                "reason": f"sums to {total}%, {abs(diff)}% {word} 100%"}
    return {"valid": True, "total_pct": total, "reason": None}


def compute_residual_allocation(residual_capital: float, split: Dict[str, float]) -> Dict[str, Any]:
    """Rupee allocation per sleeve. The LAST sleeve (by insertion order) absorbs the rounding
    remainder so the allocation sums to residual_capital exactly, to the rupee -- per the card's
    own verify requirement, never off by a paisa of drift."""
    v = validate_residual_split(split)
    if not v["valid"]:
        return {"ok": False, **v, "allocation": None}
    keys = list(split.keys())
    allocation: Dict[str, float] = {}
    running = 0.0
    for i, k in enumerate(keys):
        if i == len(keys) - 1:
            amt = round(residual_capital - running, 2)
        else:
            amt = round(residual_capital * float(split[k]) / 100.0, 2)
            running += amt
        allocation[k] = amt
    return {"ok": True, "allocation": allocation, "residual_capital": residual_capital,
            "split": split}


# ── persistence ───────────────────────────────────────────────────────────────────────────────

def save_risk_ruleset(conn, basket_name: str, max_stocks: int, sector_cap_pct: float,
                       residual_split: Dict[str, float], ruleset_name: str = "default") -> Dict[str, Any]:
    """Persists the three risk settings together on one row -- 'visible together when that basket
    is reopened, not three separate forms that can drift out of sync' (the card's own verify
    requirement). Validates the residual split before writing -- an invalid split is rejected
    here too, not just in compute_residual_allocation."""
    v = validate_residual_split(residual_split)
    if not v["valid"]:
        return {"ok": False, "reason": v["reason"]}
    with conn.cursor() as cur:
        ensure_schema(conn)
        cur.execute("""
            INSERT INTO qb_risk_rulesets
                (basket_name, ruleset_name, max_stocks, sector_cap_pct, residual_split, updated_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, NOW())
            ON CONFLICT (basket_name, ruleset_name) DO UPDATE SET
                max_stocks=EXCLUDED.max_stocks, sector_cap_pct=EXCLUDED.sector_cap_pct,
                residual_split=EXCLUDED.residual_split, updated_at=NOW()
            RETURNING id
        """, (basket_name, ruleset_name, max_stocks, sector_cap_pct, json.dumps(residual_split)))
        rid = cur.fetchone()[0]
        conn.commit()
    return {"ok": True, "id": rid, "basket_name": basket_name, "ruleset_name": ruleset_name}
