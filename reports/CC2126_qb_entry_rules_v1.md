# cc#2126 — Quant Basket Entry Rules V1 (TC score / monthly RSI / EMA crossover)

## Scope decision, stated before writing code
This card's own correction note says: *"DO NOT build a parallel entry/exit engine. Wire every
condition on this card into that SAME entry/exit dict vocabulary (v12_backtest.py +
v12_endpoints.py)... See cc#2129 for the consolidated wiring task and the full reconciliation."*
Logged to `cc_task_logs` before coding: this pass builds and hand-verifies the three condition
evaluators, the EOD write job, the combinator, and a persistence table — all real, all tested
against live data, all returning plain dicts shaped as a precise contract for integration. It does
**not** splice these into `v12_backtest.py`'s live `_passes_gates()`/entry dict, and does **not**
build a UI. cc#2129's own title — "wire cc#2123/2126/2127/2128 into the EXISTING v12_backtest.py
engine, then run a first real backtest" — already owns that integration once cc#2127 (exit) and
cc#2128 (risk) also exist; splicing a third of the picture into a shared engine file now, then
revisiting it twice more, is worse than the one clean integration 2129 already owns.

## Correction found before building (data-source gate)
The spec says start writing `v8_tc_score_daily`. Checked first: that table has **zero rows,
ever**, and `v8_pivot_star.py`'s own schema comment explains why — *"the amendment-superseded
daily table, exists empty in the live DB from the first cut of this card — flagged for a weekend
console DROP, never written."* It was abandoned in favour of `v8_tc_score_ticks` (cc#1540) before
this card was even written — independently confirmed by three prior CC investigations (cc#1979 API
census, cc#1983 TC consolidation plan, cc#2095 custom_alerts, which hit the identical trap once
already). `v8_tc_score_ticks` isn't a fit either — it scores only the OPEN BOOK (a position
health-check), not the full F&O universe this card's real source, `tc_universe_ticks`, actually
covers. Writing into a table three independent audits already flagged for DROP would revive a
deliberately-abandoned design and risks vanishing under that cleanup with no warning. Built a new,
distinctly-named table instead: **`tc_scanner_score_daily`**. The spec's SOURCE table
(`tc_universe_ticks`) is real, healthy, and correctly named — only the destination was wrong.

**Second correction, table design.** `tc_universe_ticks` carries FOUR rows per symbol per tick
(side BUY/SELL × bucket REV/MOM), not the two the spec's literal target schema implied.
Collapsing REV/MOM at write time would destroy information under an unstated rule, so
`tc_scanner_score_daily` keeps the real `(symbol, score_date, side, bucket)` grain. `tc_score_rise_1w`
states its own collapse rule explicitly instead: "the TC score" = `MAX(score100)` across the four
rows for that symbol/day — the strongest signal active in any direction, matching the founder's
own single-number framing without silently discarding data.

## What was built
**`qb_entry_rules.py`** (new file, rule 5):
- `ensure_schema()` — `CREATE TABLE IF NOT EXISTS` only (rule 10) for `tc_scanner_score_daily` and
  `qb_entry_rulesets`.
- `compute_tc_scanner_score_daily(conn, run_date=None)` — the one new EOD job. Last tick of the
  IST trading day per `(symbol, side, bucket)`, upserted. Timezone-safe day boundary
  (`(ts AT TIME ZONE 'Asia/Kolkata')::date`), not a session-timezone-dependent `ts::date`.
- `tc_score_rise_1w(conn, symbol, ...)` — condition (a). Reports the actual low date/value found
  in the trailing window, not just a boolean; states "insufficient history, N of 5 sessions
  collected" before a full week exists.
- `monthly_rsi_above(conn, symbol, threshold=70)` — condition (b), `v8_metrics.rsi_month`
  (confirmed 331 dates back to 2025-06-02, the deeper source per the card's own instruction).
- `ema_crossover(conn, symbol, pair, window_days=21)` — condition (c), modelled on
  `evaluate_dma_cross_window()`'s pattern (cc#2115) in its own function; reuses only `_ema()`
  (the shared pure EMA-math helper) so the DMA marker itself is never touched, per the card's
  `do_not_touch`. Fires on the session the relationship flips **up** within the window, not every
  day it holds.
- `combine_conditions(results, combinator)` — the toggle+AND/OR combinator. An excluded condition
  contributes nothing (no ghost term), by construction.
- `save_ruleset(conn, basket_name, conditions, combinator, ruleset_name)` — persistence. Attaches
  to a new `qb_entry_rulesets` table, not to cc#2123's universe object: cc#2123 is preview-only/
  stateless (checked — no saved-filter mechanism exists there to attach to), so a new table is the
  honest choice, not a forced reuse of an object that persists nothing today.

**`scheduler.py`**: `tc_scanner_score_daily` chained inside `_bg_gvm()`, immediately after
`gvm_coverage_guard`, same reasoning and same guarded/recorded pattern as `screeners_eod` and
`gvm_coverage_guard` (a snapshot failure never marks the GVM run bad; `record_run` called
explicitly since a chained job never passes through `_spawn`).

**`scheduler_master.py`**: `tc_scanner_score_daily` added to `_CHAINED_JOBS` (rule 9 —
registry-derived enumeration, never a hand-maintained name list; a chained job invisible to
`enumerate_scheduler_jobs()` that isn't listed here gets auto-retired as "vanished from code" by
the drift audit while it keeps running — the exact cc#1095 P2 lesson this list exists to prevent).

## Verify

**Syntax**: `ast.parse` + `py_compile` clean on `qb_entry_rules.py`, `scheduler.py`,
`scheduler_master.py`. Diffs to the two existing files are pure additions (22 and 7 lines).

**ENGINE_LIVENESS_RULE first-run evidence, not just built-and-registered**: ran the EOD job's
exact SQL for real, today — **828 rows written, 207 symbols × 4 (side, bucket) combos, score_date
2026-09-16.** Spot-checked RELIANCE/TCS/INFY: sensible real scores and verdicts (e.g. TCS
SELL-MOM score100=84.2, verdict=STRONG). `scheduler_master.tc_scanner_score_daily` now shows a
real `last_run_at`/`last_status='ok'` from this manual trigger, noted as such (tonight's actual
chained run records its own timing on top of it).

**tc_score_rise_1w**: traced against the real row just written — every symbol today has exactly 1
of 5 sessions of history, so every call correctly returns `insufficient_history=True,
sessions_collected=1` right now, exactly matching the card's own explicit verify requirement
("states plainly... rather than silently excluding every stock"). The rule activates for real
once a week of history accrues from today's real first row.

**monthly_rsi_above**, spot-checked against 6 real symbols (more than the 3 the card asks for):
RELIANCE (7.70), TCS (8.12), INFY (9.02) all correctly fail @70; DIVISLAB (95.67), NYKAA (90.02),
SOLARINDS (82.72) all correctly pass. Cross-checked the underlying column isn't mis-scaled first —
full `v8_metrics` distribution on the latest date is min 0 / max 95.67 / avg 37.36 across 206
symbols, confirming the single-digit values are genuine extremes, not a bug.

**ema_crossover**, hand-verified against 3 real symbols (RELIANCE, TCS, SOLARINDS) × both pairs —
real `raw_prices` closes fed through the exact production code (`_ema()` copied verbatim, the
crossover loop copied verbatim from `qb_entry_rules.py`) in a standalone script, no DB dependency,
same math: RELIANCE and TCS both show a real bear cross (5/20) on 2026-09-08/09-07, matching their
visible recent downtrends; SOLARINDS shows no cross on either pair even through a real ~14% single
day drop (09-11→09-15) because the prior rally left too large a gap for one day to flip the sign —
correctly no false positive, a genuine edge case this check was worth running.

**Combinator OR ≥ AND**, proven on real data: SOLARINDS (`monthly_rsi_above`=True,
`ema_crossover(5_20)`=False) — `combine_conditions(..., "ALL")` → False, `combine_conditions(...,
"ANY")` → True. RELIANCE and TCS fail both ways (no flip). OR-count (1) ≥ AND-count (0), as the
combinator's own construction (`all(L) ⟹ any(L)`) guarantees.

**Toggle-off removes ghost terms**: `combine_conditions` filters to `included` conditions before
building `passes_list`/`per_condition`/`active_conditions` — an excluded condition cannot appear
in any of the three, by construction, not by a separate check.

**Persistence**: saved a real, sensible ruleset (`large_cap`/`default`, all three conditions ON,
combinator `ANY`, matching the founder's own stated default) and read it back unchanged.

## What shipped vs. explicit follow-up
**Shipped**: the EOD job (real, running, first-run evidence), all three condition evaluators
(hand-verified), the combinator, persistence.

**Explicit follow-up, not attempted this push**: splicing these into `v12_backtest.py`'s live
`_passes_gates()`/entry dict, and any UI (checkbox/combinator picker) — both assigned to cc#2129
per this card's own correction note, once cc#2127 (exit) and cc#2128 (risk) also exist.

## What did NOT change
`v8_tc_score_daily` (left alone, still empty, still flagged for its own weekend DROP — not
touched, not revived). `v8_tc_score_ticks`, `evaluate_dma_cross_window()`, `evaluate_dma_state()`
(cc#1682/cc#2115, this card's own `do_not_touch`) — zero diff. `tc_universe_ticks` — read-only
source, never altered. `v12_backtest.py`/`v12_endpoints.py` — not touched this pass, per the scope
decision above.
