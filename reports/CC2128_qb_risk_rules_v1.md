# cc#2128 — Quant Basket Risk Management V1 (basket cap / sector cap / residual split)

## Scope decision, stated before writing code
Mirrors cc#2126/cc#2127's own precedent exactly: builds and hand-verifies the three risk
primitives plus persistence. Does **not** splice these into `qb_rebalance.py`'s live
position-sizing path (still NIFTYBEES-only, untouched) or `v12_backtest.py`, and does **not**
build a V12 wizard control — both deferred to cc#2129 ("wire cc#2123/2126/2127/2128 into the
EXISTING v12_backtest.py engine") per this card's own "next: backtesting" sequencing. Does not
touch `qb_rebalance.py`'s Rs 5L capital-per-basket convention or its fill-to-cap logic (reused via
`qb_config`, never rewritten), and does not alter the live production baskets' current caps
(15/20/10) — this adds the editable *control* for V12-built baskets, not a retroactive change to
what's running today.

## What already exists, verified rather than trusted
**Basket cap — already live.** Read `qb_config.py` and `quant_basket_registry` directly:
large_cap=15, mid_cap=20, small_cap=20, breakout_52w=10, contra_value=10, alpha_multicap=15 —
matches the card's QB_CAP_AMENDMENT_V1 citation exactly. `qb_config.basket_params()` is confirmed
the one real read path (also covers `finz_*` baskets and `model_portfolio`, not named by this card
but present). Reused directly, not reimplemented.

**Sector cap — genuinely new, confirmed absent.** Grepped `qb_composite_select.py`,
`qb_smallcap_select.py`, `qb_alpha_select.py` — zero segment-weight logic anywhere. A basket can
concentrate unlimited weight into one segment today as long as individual names pass their
stock-level filters. `apply_sector_cap()` is new code.

**Residual allocation — partially live.** Read `qb_rebalance.py` directly:
`compute_position_sizing()`/`fix_basket_overdeployment()` already route leftover capital into
NIFTYBEES only, `CASH_CAP_PCT = 0.05`. This card generalises to a founder-defined percentage split
across CASH/GOLDBEES/SILVERBEES/NIFTYBEES — NIFTYBEES stays available as a fourth sleeve, not
removed.

## What was built
**`qb_risk_rules.py`** (new file):
- `get_basket_cap_default(conn, basket_name)` — calls `qb_config.basket_params()` directly. A
  basket with no production row (a new, V12-only basket) is a valid, honest case, not an error —
  the `LookupError` `basket_params` raises by design is caught and reported plainly.
- `apply_sector_cap(candidates, max_pct, capital, max_stocks, segment_key="segment")` — walks
  rank-ordered candidates, filling equal-weight slots (`slot = capital / max_stocks`, the same
  convention `qb_config.size_slots` already uses). A candidate whose entry would push its
  segment's cumulative slot value over `max_pct`% is **skipped, never force-included at a trimmed
  weight** — the next-ranked candidate is evaluated for the same still-open slot. Every skip
  records the segment and the % it would have reached.
- `validate_residual_split(split)` / `compute_residual_allocation(residual_capital, split)` —
  percentages must sum to 100 of the residual; a split that doesn't is **rejected with the
  shortfall/excess stated**, never silently normalised. The last sleeve absorbs the rounding
  remainder so the allocation sums to the residual exactly, to the rupee.
- `save_risk_ruleset(conn, basket_name, max_stocks, sector_cap_pct, residual_split, ruleset_name)`
  — persists all three settings on **one row**, so they stay visible together and can't drift out
  of sync, per the card's own explicit requirement. Validates the split before writing.

**Persistence note (raised a third time, consistently, across cc#2126→cc#2127→here)**:
`qb_risk_rulesets` is a third separate table, same shape convention as `qb_entry_rulesets`/
`qb_exit_rulesets`, deliberately not merged via `ALTER TABLE` — MAINTENANCE_LOCK_RULE (rule 10)
names `ALTER TABLE` explicitly as Railway-console-only/propose-first. cc#2129 now has three
sibling tables to reconcile into one basket-definition object (or leave joined by `basket_name`)
— a decision this card does not make unilaterally.

## Verify

**Syntax**: `ast.parse` + `py_compile` clean.

**Basket cap**: `quant_basket_registry` queried directly — large_cap max_stocks=15, capital=500000
confirmed, matching production and the card's own default requirement.

**Sector cap**, tested against a real ranked large_cap universe (12 real symbols by `gvm_score`
DESC, real segments from `gvm_history`, richer than the card's own "two candidates" minimum — this
set has a real 3-way Auto OEM collision): `max_pct=20`, `capital=500000`, `max_stocks=12` (slot
41,666.67 = 8.33% each). Result: **TVSMOTOR (rank 11, Auto OEM) correctly SKIPPED** — segment
already held 2 slots (16.67%), a 3rd would reach **25.0%**, over the 20% cap — logged with the
segment and exact %, not a bare boolean. The next-ranked candidate (GVT&D, Electronics) correctly
filled the slot instead. Final basket: 11 of 12 filled (one slot legitimately left open, no
lower-ranked same-segment substitute forced in). Cross-checked every filled segment's final total
— all ≤20%, zero breaches, confirmed programmatically not just for the one skip case.

**Residual split**, all four required cases run for real: 100% cash → ₹123,456.78 exactly; 100%
GOLDBEES → same; a real 40/30/30 split → ₹49,382.71 / ₹37,037.03 / ₹37,037.04, **summing to
₹123,456.78 exactly, to the rupee** (verified by direct addition, not assumed); an invalid split
(40/30/25 = 95%) **rejected** with `"sums to 95.0%, 5.0% short of 100%"` — not silently normalised.

**All three settings on one row**: saved a real ruleset (`large_cap`/`default`: max_stocks=15
matching production, sector_cap_pct=20, residual_split 40/30/30 cash/gold/silver) and read it back
unchanged — one row, not three forms that could drift.

## What did NOT change
`qb_rebalance.py` (NIFTYBEES-only residual routing, `compute_position_sizing`,
`fix_basket_overdeployment`, the Rs 5L capital convention, fill-to-cap logic) — zero diff,
confirmed via `git diff --stat`. `qb_config.py` — read-only, reused. `qb_eod_checker.py` and
cc#2127's exit engine — no overlap, no shared code path touched, per the card's own instruction.
Live production basket caps (15/20/10) — unchanged; this card adds the editable control for V12
baskets only. `v12_backtest.py`/`v12_endpoints.py` — not touched this pass.
