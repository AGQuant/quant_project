# cc#1978 items 5/8 — star + DMA universe-scope evaluation, shared-helper refactor

Per `v8_marker_ticks.py`'s own docstring constraint ("a universe-scoped writer for STARS/DMA needs
new, separately-scoped evaluation logic — reusing the same condition math via a shared helper, not
a flag flip and not a duplicated copy") and the authorization already on record
(`FOUNDER_WORD_10SEP_2205_STARS_UNIVERSE` in cc#1978's own spec, reconfirmed 12-Sep log 6382,
"continue items 2-9 of the card").

## What this lands

`v8_pivot_star.py` — pure refactor + two new functions, **zero behaviour change** to anything that
already exists:

- `_fetch_star_support(cur, d, syms)` / `_score_stars(cands, piv, live, eod, met, touch)` —
  extracted verbatim from `evaluate()`'s own former inline body (fetch queries and the star
  condition math, byte-for-byte). `evaluate()` itself is rewritten to call these two helpers but
  its candidate-set query, `EVAL_SCOPE` handling, and external contract (a plain list of fired
  stars) are **completely untouched** — `session_log 18052`'s book-scope lock is not touched by
  this push.
- `evaluate_universe(conn, target_date=None)` — new. Same two helpers, candidate set is
  `SELECT symbol FROM futures_universe WHERE is_active` instead of the open book. Returns the same
  shape `evaluate()` does (a plain list of fired stars).
- `evaluate_universe_with_state(conn, target_date=None)` — new. Same as above but also returns
  `evaluated_syms` (the set of symbols that cleared every data gate — had a pivot, metrics and a
  cmp — regardless of whether a star fired). This is what the marker-ticks writer needs to post
  real `fired=false` rows (the card's own verify V2: "fired=false rows must exist and outnumber
  fired=true") — a symbol skipped for **missing data** never reaches a real check and is correctly
  excluded from this set too (no row at all for that tick, matching `persist_chan_ticks`'s own
  "no fit, no row" convention).
- `_score_stars`'s return signature changed from a single list to `(fired, evaluated_syms)` — safe
  because it is a function this session created earlier today; both of its only two callers
  (`evaluate()`, `evaluate_universe()`) were updated in the same push to unpack and discard the
  second value, preserving their own single-list return contract exactly.

Same pattern for DMA state: `_fetch_dma_state_support` / `_score_dma_state` extracted from
`evaluate_dma_state()` verbatim; `evaluate_dma_state()` rewritten to call them with **zero change**
to its own hardcoded open-book candidate query (this function never read `EVAL_SCOPE` to begin
with — confirmed by re-reading it before touching anything); new `evaluate_dma_state_universe()`
calls the same two helpers against the registry candidate set.

## Verify

- `ast.parse` clean.
- Isolated test (`test_v8_pivot_star_refactor.py`, scratchpad, not committed) against the REAL
  committed module (`psycopg2`/`pytz`/`fastapi`/`v8_book_canon`/`cmp_resolver` stubbed so the file
  imports without those packages installed here):
  1. `evaluate()` forced to `EVAL_SCOPE="universe"` (temporarily, restored after — asserted back
     to `"positions"` when the test ends) produces **byte-identical** output to
     `evaluate_universe()` on the same synthetic data — direct proof the refactor preserved
     behaviour and the new function is wired correctly, not just "it ran without an exception."
  2. Expected stars fire on hand-designed synthetic data (one BLUE, one RED, two symbols with
     full data that correctly get no star).
  3. `evaluate_universe_with_state()` reports all 4 synthetic symbols as `evaluated` (2 fired, 2
     did not) — confirming the fired=false evidence path works before it is wired into a writer.
  4. `evaluate_dma_state_universe()` fires `DMA_ABOVE`/`DMA_BELOW` correctly on synthetic
     rising/falling close series; the 5DMA/20DMA values match an independent hand computation.

7/7 assertions pass (5 named cases, several assertions each).

## What this push does NOT include yet

The actual `v8_marker_ticks.py` writer functions (`persist_star_ticks`, `persist_dma_ticks`,
`persist_act_ticks`) that call these new evaluators and insert rows — plus wiring the already-built
ACT batch functions (`_ad_21d_batch`/`eod_rvol_pair_batch`/`r6_read_batch`, sha `9071240`) into an
`evaluate_activity_universe()`-style path, retention/purge for the new families, and the
`scheduler.py` dispatch. This report lands the evaluation layer, verified in isolation; the writer
is the direct next step, fully unblocked by what's here.

Nothing under `worker/**`. `EVAL_SCOPE`, `evaluate()`, `evaluate_dma_state()`,
`evaluate_activity()` all behave exactly as before this push — confirmed by test, not just by
inspection. Card not done — Fable verifies.
