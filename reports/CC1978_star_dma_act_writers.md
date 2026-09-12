# cc#1978 items 5/6/8 — STAR/DMA/ACT writers, scheduler wiring

Follows `reports/CC1978_universe_scope_refactor.md` (sha 616d1b1, evaluation layer for STAR/DMA)
and sha 9071240 (ACT batch primitives). This push is the part both of those reports named as the
direct next step: the actual `v8_marker_ticks.py` persistence functions, the ACT
evaluation-layer composition those batch primitives were still missing, and the `scheduler.py`
dispatch.

## What this lands

**`v8_pivot_star.py` — ACT gets the same shared-helper treatment STAR/DMA already got:**
- `_score_activity_one(r_v, p_v, d_v, ad_v, side)` — the Vol R/P/D/AD four-check tally extracted
  verbatim from `evaluate_activity()`'s own inline scoring loop. `evaluate_activity()` itself is
  rewritten to call it — zero change to its thresholds, side-awareness, or output shape (isolated
  test asserts it against an independent hand-copy of the pre-refactor formula across boundary and
  None-blackout cases).
- `_fetch_activity_support(cur, syms)` — composes the three batch primitives (`r6_read_batch`,
  `deliv_ratio_batch` — already batch, `_ad_21d_batch`) into the same 4-key shape
  `evaluate_activity()`'s own per-symbol loop builds by hand.
- `evaluate_activity_universe_with_state(conn, target_date=None)` — the universe-scoped,
  batched ACT evaluator. A universe symbol has no position side (`futures_universe` carries no
  long/short concept — that's `v8_paper_positions`' own field), so **both sides are scored per
  symbol**: the same compound-family shape this card's own CHAN family already uses
  (`chan_sell`/`chan_buy` — one compute, two independent per-tick truths that can co-occur).
  Returns `(fired_rows, all_rows)` — `all_rows` is every symbol×side pair (what the writer needs
  for real `fired=false` rows), `fired_rows` the passed≥2 subset.
- `evaluate_activity_universe(conn, target_date=None)` — thin filter over the above, matching
  `evaluate_activity()`'s own plain-list contract.

**`v8_marker_ticks.py` — three new persist functions, module docstring + distribution endpoint
updated:**
- `persist_star_ticks()` — calls `evaluate_universe_with_state()` (sha 616d1b1). One `star` row
  per symbol in `evaluated_syms`: `fired=true` with direction/colour/level when a star actually
  fired, `fired=false` (nulls) when the symbol cleared every data gate but didn't. A symbol
  outside `evaluated_syms` (missing pivot/metrics/CMP) gets no row — "never evaluated" stays
  distinct from "evaluated, no star".
- `persist_dma_ticks()` — calls `evaluate_dma_state_universe()`. DMA STATE has no "ran, no state"
  outcome for a symbol with enough history (every row is a definite GREEN or RED), so **every row
  here is written `fired=true`** — stated explicitly since it's the one family in this store where
  `fired` isn't a rare event. A symbol with insufficient history (or the rare exact tie) gets no
  row, same "no row = did not run" convention as everywhere else.
- `persist_act_ticks()` — calls the new `evaluate_activity_universe_with_state()`. Writes
  `act_buy`/`act_sell` per symbol every tick, fired or not — no data-gate exclusion for this
  family (a None-valued read just fails its own check, matching how the book-scoped
  `evaluate_activity()` already treats missing data; stated as a deliberate difference from
  STAR/DMA, not an oversight).
- All three follow the exact `persist_tcs_ticks()` shape: optional own-connection handling,
  `ON CONFLICT (symbol, ts, family) DO NOTHING`, `_purge()` gated the same way (`purge_now` or
  `_is_last_tick()`), a result dict with `ok`/counts/`purged`/`zero_tick`. No DDL change — `family`
  is a plain TEXT value, so four new values need no `ALTER TABLE` (MAINTENANCE_LOCK_RULE cc#351
  stays untouched).
- `marker_ticks_symbol`'s `families_covered` list and docstring updated to include
  `star`/`dma`/`act_buy`/`act_sell`.

**`scheduler.py`:**
- Three new wrappers — `_bg_marker_ticks_star`, `_bg_marker_ticks_dma`, `_bg_marker_ticks_act` —
  each its own `scheduler_master` row (job name is the function's own `__name__`, the same
  self-registering mechanism `_bg_marker_ticks_tcs` already relies on — no separate seed step).
  All three use the LOUD-FAILURE convention `_bg_pivot_star`/`_bg_channel_5m` established (cc#996:
  a non-ok result is re-raised so a real failure stamps `last_status='error'`, never a false
  `'ok'`) — **not** the pattern `_bg_marker_ticks_tcs` itself uses (log-only, no re-raise). Found
  that inconsistency while writing these three; flagged in `_bg_marker_ticks_tcs`'s own docstring
  as a note, not fixed — it's TCS's existing behaviour, out of this card's items, so it gets a
  comment, not a silent edit.
- Dispatched in the **same cash-continuous block as `_bg_pivot_star`/`_bg_channel_5m`** (right
  after `_bg_channel_5m`), not the `_is_market_hours` block `_bg_marker_ticks_tcs` uses. Same rule
  at universe scope must share the exact window the book-scoped marker uses (ends 15:15, not
  15:30) — using a looser window would let the universe and book-scoped readings of the identical
  condition disagree during 15:15–15:30 for a reason that has nothing to do with the rule itself.

## Verify

- `ast.parse` clean on all three files.
- Isolated tests against the REAL committed modules (deps stubbed the same way as the prior
  push): `test_v8_pivot_star_act_refactor.py` (6 cases — `_score_activity_one` matches an
  independent hand-copy of the pre-refactor inline formula across boundary/None cases;
  `evaluate_activity_universe_with_state()` scores both sides for every universe symbol with zero
  data-gate exclusions; a symbol with 3-of-3 real checks fires BOTH act_buy and act_sell — the
  intended CHAN-style co-occurrence; a symbol failing all three real checks fires neither even
  with one AD leg clear; a total-data-blackout symbol scores cleanly to 0/4 both sides with no
  crash; the plain `evaluate_activity_universe()` correctly filters+strips) and
  `test_v8_marker_ticks_writers.py` (4 cases, monkeypatching the three universe-evaluation
  functions to isolate the writer logic itself — STAR writes fired=true/false rows correctly with
  nulls on the false rows and respects the purge gate; DMA writes 100% fired=true rows with
  correct direction/colour per symbol; ACT writes exactly 4 rows for 2 symbols with fired flags and
  family (`act_buy`/`act_sell`) assigned correctly per side).
- Not re-verified again here: `evaluate_universe_with_state()`/`evaluate_dma_state_universe()`
  themselves — already proven byte-identical-behaviour-preserving in the prior push
  (`test_v8_pivot_star_refactor.py`, still passing, not re-run redundantly).

## What this push does NOT include / could not do

- **First-run evidence (item 8) is NOT posted here** — checked two ways before writing this:
  (1) today (12-Sep) is Saturday, a non-trading day, so the scheduler's own
  `_is_cash_continuous`/`_is_trading_day` gate will not dispatch these jobs regardless of what is
  deployed; (2) this container has no `DATABASE_URL` (confirmed empty), so there is no way to
  invoke the new functions against the real production DB directly from this session either, the
  way earlier ad-hoc verification in this card's history was done. Genuine first-run evidence
  needs BOTH a live trading day AND this code actually reaching `main` (still blocked on the
  unresolved main/deploy question, `cc_task_logs` 1199/6385) — stating this plainly rather than
  claiming liveness the data cannot yet back up (rule 9's own "badge follows the data, never
  precedes it").
- `scheduler_master` self-registration for the three new jobs is inferred from the exact same
  mechanism `_bg_marker_ticks_tcs` already relies on (job rows keyed by `record_run(fn.__name__,
  ...)`, called uniformly by `_spawn` on every dispatch) — not independently re-verified against a
  live row, since none of the three has run yet (see above).
- Card not done — Fable verifies. Once this reaches a trading day live, the card's own V1–V5
  verify block (universe symbol counts, fired=false outnumbering fired=true, the ~22 open-book
  match-check against `v8_pivot_star_log`, wall-clock ms, the timestamp check) is the next thing
  to run and report.
