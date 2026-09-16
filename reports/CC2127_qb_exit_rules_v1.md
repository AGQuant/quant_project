# cc#2127 — Quant Basket Exit Rules V1 (hard stop / trailing stop / GVM / momentum / RSI / EMA)

## Scope decision, stated before writing code
Mirrors cc#2126's own precedent exactly, per this card's shared correction note ("See cc#2129 for
the consolidated wiring task"): this pass builds and hand-verifies the six exit conditions, reuses
cc#2126's combinator unchanged, and adds persistence. It does **not** splice these into
`v12_backtest.py`'s live `exit_def` dict, does **not** expose `trailing_peak_pct` in the V12
wizard, and does **not** build a UI — all assigned to cc#2129 once cc#2128 (risk management) also
exists. Does **not** touch `qb_eod_checker.py` or session_log 124's live production exit rules
(Hard Stop 1 -20%, Hard Stop 2 -10% vs Nifty50, Filter Exit) — those govern real money on live
baskets today; this is a separate, new, founder-configurable engine for V12/Quant-Basket-Builder
baskets, per the card's own explicit ruling.

**Flagged, not built**: once this ships, two hard-stop systems exist side by side — the legacy
-20%/-10% on production baskets, and this new founder-configurable one on V12 baskets. Worth
knowing before either is used to reason about the other (the card's own words).

## Two existing-engine reconciliations, verified rather than assumed
**cc#2093 (trailing_peak_pct/rank_fall_y computed-but-never-enforced) — checked, confirmed
LANDED.** Read `v12_backtest.py`'s `run_backtest()` directly: `keep`/`forced_exit_reason` now
genuinely drop a position that breached its trailing peak or `rank_fall_y` threshold — the
dead-code bug is fixed. Confirmed commit `0e0e84a` is on `main` via `git merge-base
--is-ancestor`. This card does not need to re-fix that enforcement.

**`trailing_stop_pct` is a named, deliberate formula difference, not a duplicate to avoid.** Read
`v12_backtest.py` directly: its `peak_since_entry` tracks the peak **close** while held
(`pc = series.as_of(sym, d); if pc > peak_since_entry.get(sym, 0): peak_since_entry[sym] = pc` —
a close-price series, confirmed by reading the surrounding code). This card's founder brief is
explicit the other way — *"the stock made high... fall from the peak"* — and its own scope item
(b) spells it out: *"the actual traded HIGH, not the close, since raw_prices carries a high
column and a peak is a high-water mark."* Two different, independently-specified formulas
(close-peak vs. high-peak), not one feature accidentally reimplemented. Built to the founder's
explicit high-based formula (the newer, more specific instruction), with the divergence from
`v12_backtest.py`'s own close-based version named here rather than silently swept under "reuse."
True code-sharing between a full backtest walk's incremental state and a standalone per-position
read (what this file and cc#2126 are both shaped as) would need `v12_backtest.py`'s own internals
refactored — out of scope here, left to cc#2129 or a dedicated card.

## What was built
**`qb_exit_rules.py`** (new file):
- `hard_stop_pct(conn, symbol, entry_price, threshold_pct=10)` — genuinely new, EOD close only.
- `trailing_stop_pct(conn, symbol, entry_date, threshold_pct=10)` — genuinely new (see
  reconciliation above), `MAX(raw_prices.high)` over `[entry_date, latest EOD]`, recompute-on-read
  (scope item 4's own recommendation, chosen here: an open position's holding period is at most a
  few hundred EOD rows, so a single indexed `MAX(high)` aggregate is cheap — no new mutable
  per-position state, matching "keep the server light" more literally than an incremental cache).
- `gvm_rating_exit(conn, symbol, threshold)` / `momentum_rating_exit(conn, symbol, threshold)` —
  genuinely new, `gvm_history.gvm_score`/`m_score`, latest `score_date`, independent of each other
  and of whatever gate the position entered under (an exit-time re-check, not the entry gate
  reapplied).
- `monthly_rsi_exit(conn, symbol, threshold=30)` — reuses cc#2126's RSI read path. That function
  (`monthly_rsi_above`) is **renamed to `monthly_rsi_check`** with a `direction` parameter
  (`"above"`/`"below"`) so entry and exit share one query, not two — checked before renaming that
  nothing outside `qb_entry_rules.py` called the old name yet (it was never wired into a live
  path), so this is a safe, zero-caller-impact rename.
- `ema_crossover_exit(conn, symbol, pair)` — thin wrapper calling cc#2126's `ema_crossover` with
  a new `direction="bear"` parameter (default `"bull"`, preserving cc#2126's exact tested
  behaviour for any existing caller) — the bearish mirror, same function, not a second
  implementation, per the card's own explicit instruction.
- `combine_conditions` — **imported directly from `qb_entry_rules.py`, zero duplication.**
- `save_exit_ruleset(conn, basket_name, conditions, combinator, ruleset_name)` — a new
  `qb_exit_rulesets` table, same shape as cc#2126's `qb_entry_rulesets`. **Not** merged into that
  table via `ALTER TABLE ADD COLUMN`: MAINTENANCE_LOCK_RULE (rule 10) names `ALTER TABLE`
  explicitly as Railway-console-only/propose-first, so the cleaner one-row-per-ruleset unification
  is **proposed here for cc#2129 to run**, not executed on this card, even though a nullable
  `ADD COLUMN` would in practice be a fast, non-blocking operation.

**`qb_entry_rules.py`** (edited, additive/renaming only): `monthly_rsi_above` → `monthly_rsi_check`
plus a `direction` param; `ema_crossover` gained a `direction` param (default `"bull"`) and now
reports both the requested `direction` and the `detected_direction` (previously one field did
both jobs); a stale docstring reference in `combine_conditions` updated to the new name.

## Verify

**Syntax**: `ast.parse` + `py_compile` clean on both files.

**Regression check on the two edited cc#2126 functions**: re-ran the exact edited `ema_crossover`
code (copied verbatim into a standalone script, same as cc#2126's own verification) against the
same three real symbols (RELIANCE, TCS, SOLARINDS) × both pairs, now for both `direction="bull"`
and `direction="bear"`. The `bull` results are byte-identical to what cc#2126 originally verified
(RELIANCE/TCS `passes=False`, SOLARINDS `passes=False`, same cross dates) — confirming the edit is
a clean, backward-compatible generalisation, not a silent behaviour change.

**hard_stop_pct / trailing_stop_pct**, hand-checked against a real RELIANCE position (entered
2026-08-07 at close 1334.80, a real recent local high) — the exact embedded SQL run directly:
peak `high` over 2026-08-07→2026-09-15 is **1337.00 on 2026-08-07 itself** (the entry day's own
high — RELIANCE never made a new high after this entry date, a real, meaningful edge case: the
peak IS the entry, not a later date); latest close 1235.30 on 2026-09-15. `pct_from_entry` =
-7.46% (threshold_pct=10 → no fire; threshold_pct=5 → fires, boundary confirmed both ways).
`pct_from_peak` = -7.61% (threshold_pct=10 → no fire; threshold_pct=7 → fires). Peak date is
reported, not just the exit boolean, per the card's own verify requirement.

**gvm_rating_exit / momentum_rating_exit**, checked against 4 real symbols straddling a threshold
(more than the 2-3 the card asks for) — latest `gvm_history`: RELIANCE 4.77, TCS 5.67, VEDL 5.88
all correctly fire at threshold=6; DIVISLAB 7.74 correctly does not. Momentum: RELIANCE 2.97, TCS
2.81, VEDL 3.44 all correctly fire at threshold=5; DIVISLAB 9.38 correctly does not.

**monthly_rsi_exit / ema_crossover_exit reuse, grep-confirmed**: `monthly_rsi_exit` calls
`monthly_rsi_check(..., direction="below", ...)` — one function, `ema_crossover_exit` calls
`ema_crossover(..., direction="bear", ...)` — one function. Grepped the whole repo for the old
`monthly_rsi_above` name after the rename: only the docstring's own explanatory reference and
cc#2126's already-shipped report remain (historical, correctly left as-is) — no live caller missed.

**Combinator OR ≥ AND**, proven on real EXIT data (not re-using cc#2126's entry-side proof):
RELIANCE with `hard_stop_pct`(10%)=False, `trailing_stop_pct`(10%)=False,
`gvm_rating_exit`(6)=True → `combine_conditions(..., "ALL")` = False,
`combine_conditions(..., "ANY")` = True. OR(1) ≥ AND(0).

**A position exits on the first firing condition, reason + measured value named**: every condition
function already returns its own measured value (`pct_from_entry`, `pct_from_peak` + `peak_date`,
`value` for GVM/momentum/RSI, `cross_date` for EMA) alongside `passes` — a caller combining them
has the full evidence for "trailing stop -7.6% from a peak of 1337.00 on 2026-08-14"-style
reporting, never a bare boolean. (Full first-condition-wins selection logic is the responsibility
of the integration cc#2129 owns, once these are spliced into `v12_backtest.py`'s exit walk.)

**No double-fire confirmed by construction, not by inspection**: this card wires nothing into any
live position or scheduled job. `qb_eod_checker.py`, session_log 124's rules, and every currently
running basket are untouched — zero diff, confirmed by `git diff --stat`. No basket is wired to
this new engine yet; that remains an explicit, separate founder step per the card's own ruling.

**Persistence**: saved a real, sensible exit ruleset (`large_cap`/`default`, all six conditions
ON, combinator `ANY`) and read it back unchanged.

## What did NOT change
`qb_eod_checker.py`, session_log 124's three rules, the live -20%/-10% stops on production
baskets — zero diff. `v12_backtest.py`/`v12_endpoints.py` — not touched this pass. `gvm_history`,
`v8_metrics`, `raw_prices` — read-only sources, unchanged. `evaluate_dma_cross_window()`/
`evaluate_dma_state()` (cc#1682/cc#2115) — untouched; `ema_crossover_exit` reuses only cc#2126's
own `ema_crossover()`, never those functions directly.
