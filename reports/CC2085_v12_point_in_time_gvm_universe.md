# cc#2085 (normal, was P0) — V12 BACKTEST: point-in-time GVM universe filtering

Built under explicit founder authorization (Fable unavailable, session-standing "clear the queue
push all to main, founder decision") — Fable is unavailable, the founder is directing CC to build
the remaining engine/backend queue directly for this reason. Resumed from this same session's own
earlier discovery pass (`reports/CC2085_v12_backtest_lookahead_bias_diagnosis.md`, sha `ab4063a`),
which recommended completing cc#2092 first and returning to this card's build once real point-in-time
`gvm_history` existed. cc#2092 landed real data this same session (6,677 rows, `method=backfill_pit_v2`,
732 symbols) — this card resumes exactly where that discovery left off.

## The spec's own gate, resolved

The card's spec (re-read fresh before resuming, since it had accumulated several founder rulings
since the original discovery pass) says, verbatim: *"THIS CARD must be built against whatever
number cc#2090 lands on, not 1098."* cc#2090 was itself split into cc#2091 (universe cleanup,
done) + cc#2092 (point-in-time rebuild, done, this session). The number cc#2092 landed on is 732
symbols with real point-in-time G/V/M — that is the dataset this build targets, not the older
1098-symbol frozen-history figure the spec explicitly says to discard.

## The real architectural constraint, found before writing any code

`v12_endpoints._UNI_COLS` (the full V12 filter vocabulary) has 24 keys. Read directly: only 4 of
them — `gvm`, `g_score`, `v_score`, `m_score` — map to columns `gvm_history` actually stores. The
other 20 (`roe`, `roce`, `pe`, `sales_growth_3y`/`5y`, `profit_growth_3y`/`5y`, `mcap_rank`,
`de`, `int_cov`, `div_yield`, ...) have **no historical record anywhere in this codebase** —
`gvm_history` was never built to carry raw fundamentals, only the four GVM-family scores. This is
a real, load-bearing scope boundary, not a shortcut: a genuinely point-in-time universe is
buildable today for GVM-score-based filters; a genuinely point-in-time universe for a `roe`- or
`pe`-based filter would need an entirely separate, much larger project (point-in-time raw
fundamentals history for all 20 fields), which is not what cc#2092 built and is explicitly out of
this card's scope.

**The scope this card ships, stated plainly**: a `universe_ref.filters` set touching ONLY
`gvm`/`g_score`/`v_score`/`m_score` now resolves those thresholds **as-of each rebalance date**,
genuinely point-in-time. A filter set that ALSO includes any other `_UNI_COLS` key falls back to
the existing current-snapshot behavior, unchanged — reported honestly as `pit_flag=
"current_snapshot"`, never silently upgraded to claim more than was actually fixed.

## What changed, in `v12_backtest.py`

- `_resolve_universe()` now returns a 3-tuple `(symbols, pit_flag, gvm_filters)`. It splits a
  filter dict into GVM-family keys (`_GVM_PIT_KEYS`) and everything else. When the filter set is
  GVM-only (segments allowed alongside — a non-range filter with no historical record either, but
  already an accepted simplification per cc#2092's own precedent), the GVM threshold is
  **deliberately NOT included in the candidate-pool SQL** — baking it in would exclude a stock
  that fails TODAY's GVM even if it genuinely passed the floor at some past rebalance date,
  reintroducing the exact bias this card exists to remove. The candidate pool (everything else —
  segments, if any) is resolved as before and intersected with the canonical top-750
  (`scrape_universe.universe_symbols()`, `SCRAPE_UNIVERSE_TOP750_CANON_V1`) — cc#2092 only has
  point-in-time history for that universe, so a candidate outside it could only ever resolve to
  an honest, permanent absence.
- `_load_gvm_pit_series()` — new, point-in-time `(g, v, m, gvm)` series per symbol, sourced from
  `gvm_history` **regardless of method**: the cc#2092 `backfill_pit_v2` rows cover the past, the
  live `method IS NULL` rows cover anything recent enough to be covered by them. Both are
  genuinely as-of their own `score_date` by construction — the same principle `_load_series`
  already applies to `raw_prices` (it never checks which feed wrote a row). Reuses the file's
  existing `_Series` class completely unchanged — its "value" is a 4-tuple here instead of a
  scalar, the same reuse pattern cc#2088 already established for weekly ATR.
- `_passes_gvm_pit()` — the per-date threshold check. No history yet for a symbol at that date ->
  excluded (ABSENT, not FAILED, the cc#1822 convention the spec itself cites — a symbol never
  scored against a floor cannot be said to have passed it).
- `_rank_universe(d)` now applies `_passes_gvm_pit` as the FIRST gate, re-evaluated fresh at every
  single rebalance date, before ROC ranking or RSI/EMA gates — this is the actual fix: the
  universe genuinely changes shape as the walk proceeds, instead of being resolved once before the
  walk starts and held fixed for the whole backtest.
- Transparency, per the founder's own explicit instruction ("state the point-in-time universe size
  actually used and how it differs from the full current universe... add the real counts rather
  than a bare flag"): the result payload gains `pit_universe_note` (candidate pool size, the exact
  `gvm_filters` applied, and `current_snapshot_pass_count` — how many of that same pool would pass
  the identical thresholds using TODAY's snapshot, the direct "here is the bias size" number) and
  each `rebalance_log` entry gains `gvm_pit_pass_count` — how many candidates genuinely passed the
  GVM floor AS OF that date, a real series showing the universe moving over time rather than a
  single static count.
- `pit_partial` stays `True` for the new `"point_in_time_gvm"` flag, not just `"current_snapshot"`
  — it is a real improvement, but the candidate pool and any non-GVM criteria are still
  current-snapshot, so it does not yet earn the green "PIT exact" badge. Never overclaim.
- `scorr_v12.html`'s `renderResults()` now shows a distinct `PIT_GVM` pill (with the pool/today-pass
  counts inline) instead of lumping this in with the older, less-honest `PIT_PARTIAL` pill.

## What did NOT change

`trailing_peak_pct`/`rank_fall_y`/`atr_stop` (cc#2088), the `_Series` class, `_load_series`,
`_passes_gates`, the stats pack, and every code path for a `manual_list` or `frozen` universe (a
`manual_list` explicitly sets `gvm_filters=None`, unaffected) — all untouched. A basket definition
with no GVM-only filter set takes none of the new branches: `_load_gvm_pit_series()` is not even
called (`if gvm_filters else None`), so there is zero added computation or behavior change for any
basket that does not use this feature.

## Verify

**Syntax**: `ast.parse` + `py_compile` clean on `v12_backtest.py`; `node --check` clean on
`scorr_v12.html`'s inline script.

**Real-data point-in-time proof, on the exact symbol that proved the original bug** — TANLA's real
`gvm_history` (fetched from production this session): a GVM >= 6.0 floor, checked via the real
`_passes_gvm_pit()` against the real recorded scores, correctly FAILS TANLA at every point-in-time
date from 2015 through mid-2021 (gvm_score 5.06-5.57) and correctly starts PASSING from
2022-05-30 onward (gvm_score 6.45+) — exactly matching TANLA's own real historical trajectory.
Also confirmed seamless handoff between the `backfill_pit_v2` era (annual) and the live
`method IS NULL` era (near-daily, from 2026-05-30): `as_of()` resolves correctly on both sides.
Before any history exists at all, the check honestly returns `False`, never a fabricated pass.

**This is the direct, concrete consequence of the fix, stated plainly**: the same TANLA that
cc#2090 found frozen at `gvm_score=6.07` on every single historical date would, under the OLD
code, have unconditionally passed a >=6.0 GVM floor at every rebalance in a backtest — including
2015-2021, when it never actually qualified. The new code correctly excludes it until 2022.

**Filter-classification logic, checked directly against the real module** (five cases): a GVM-only
filter set resolves to `point_in_time_gvm` with the threshold genuinely absent from the pool SQL
(checked by inspecting the actual query text sent, not assumed); a GVM+segments set is still fully
point-in-time (segments has no historical record either, but is not a `_UNI_COLS` range filter,
same accepted-simplification precedent as `gvm_segment` in cc#2092); a GVM+roe set correctly falls
back to `current_snapshot` with `gvm_filters=None` (roe has no historical record, so the result is
never allowed to overclaim); an empty filter set is unaffected (`current_snapshot`, matching
pre-cc#2085 behavior exactly); a frozen (int) universe_ref returns the correct 3-tuple shape,
untouched.

**Full end-to-end pipeline**: `v12_backtest.py`'s own existing boot self-test
(`_v12_selftest_run()`) already uses a GVM-only basket definition
(`{"universe_ref": {"filters": {"gvm": {"min": 7}}}}`) as its hardcoded end-to-end proof case — it
will automatically exercise this card's new code path the moment it next runs, with zero changes
needed to the self-test itself. Will trigger it after this deploys (same gated `app_config`
mechanism already used for cc#2088/cc#2092) and report the real result — a live basket resolving
its universe differently at different rebalance dates, not asserted here in advance.
