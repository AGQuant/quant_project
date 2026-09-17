# cc#2143 — QB Universe builder: multi-select pool chips, compact grid, no per-item commentary

Founder direct, 17-Sep: multi-select in the pool section, all pool names compact in a row
(~5 across, wrapping), no descriptive commentary per pool. Step 1 of the QB Universe sprint.

## Task 7 first — the endpoint's callers
`grep` for `universe2/preview` and `universe2/pools` across every .py/.html/.js: exactly one
caller each, `scorr_qb_universe.html` (lines 149 and 265). cc#2123's docstring is still true, so
the parameter shape could change without a compatibility shim — one was kept anyway (below).

## Backend (`qb_universe_builder.py`)
- `/api/qb/universe2/preview` takes the selection as **repeated `pools=` params**
  (`pools=cap_large&pools=nifty500`) — my call, stated: it is what `Query(List[str])` parses
  natively and what the page's own `buildQuery()` already does for `verdict=`. A comma inside one
  value is tolerated; the legacy single `pool=` is folded in (`_pool_keys`), so any held link keeps
  working. No keys → `{"error": "select at least one pool"}`; an unknown key names itself.
- Population = **`SELECT DISTINCT symbol FROM ((pool1) UNION ALL (pool2) …) u`**
  (`_pool_union_sql`), never a sum or a concatenation. One key = that pool's own `_POOL_SQL`,
  byte-for-byte as before. The `_POOL_SQL` definitions and `/pools` are untouched (do_not_touch).
- Response adds `pools` (keys) and `pool_labels`; `pool` / `pool_label` keep their names, joined
  (`"Large Cap + Nifty 500 (top-500 mcap)"`), so no reader ever sees the first pool alone.
- **Dedup proven on the live tables** with the exact SQL the helper builds: Large Cap + Nifty 500
  → raw union 500, **scored `pool_count` 490** = the solo Nifty 500 count, not 100 + 490 = 590.
  Second pair for good measure: F&O + Mid Cap → 264, not 352.

## Page (`scorr_qb_universe.html`)
- The three group sub-headers and the one-per-line radio rows are gone. The Pool section is a
  **flat wrapping grid of checkbox chips**: name + the pool's own scored count as a badge.
  `repeat(auto-fill, minmax(190px, 1fr))` gives **5 per row** on the 1100 px page (6 pools → 5 + 1),
  **2 per row** under 560 px. Any number of chips can be on; each toggle re-fetches `/preview`
  with the current set, and `pool_count` / `count` / `per_filter_counts` update exactly as before,
  keyed on the union.
- **Data honesty moved, not deleted (task 3):** one caption above the grid — "Cap bands and
  Nifty 500 ranked as of 2026-09-16 (screener CSV load)" — built from every rank-derived pool's
  `ranked_as_of`; they all read `MAX(rank_date)` so one date is expected, and if they ever
  differ the page prints all of them with an amber "ranking dates differ" flag rather than picking
  one. The stale badge + `stale_note` from `/pools` render in the same caption when `stale=true`.
  The raw-vs-scored gap sentence moved behind an **(i)** on each chip that has a gap (5 of 6;
  Large Cap has none): a `title` tooltip on desktop, and a tap writes it under the grid for a phone
  (`.pool-note`), without toggling the chip.
- Nifty 500's served label "Nifty 500 (top-500 mcap)" keeps its qualifier visible as a small muted
  suffix on the chip (the cc#2123 honesty label — our own top-500 by mcap, not index membership)
  instead of forcing a four-line chip on a phone.
- Zero chips selected is an explicit state: "— select at least one pool", no request sent, the
  expression / binding / as-of lines cleared.
- Assumptions flagged by the spec, as built: pools **combine as a union**; the group sub-headers
  are dropped for one flat list. Both easy to revisit if Arpit wants otherwise.

## Verify
- `ast`/`py_compile` clean; helper unit checks (`_pool_keys` order/dedup/comma/legacy,
  `_pool_union_sql` single vs many); `node --check` clean on the page; **0 literal fallbacks** added.
- Playwright on the real page with the real theme boot + web tokens, `/pools` stubbed in the
  endpoint's own shape from today's live counts (100/100, 147/150, 734/750, 792/865, 205/208,
  490/500, rank_date 2026-09-16), `/preview` stubbed with the measured union numbers: dark 1280,
  light 1280, dark 375. Each: 6 chips, **5 grid columns** at 1280 / **2** at 375, zero legacy
  rows/captions/headers, caption present, (i) only on the five gap chips and its text carries only
  the gap sentence, initial prompt; toggle Large Cap → "100 stocks of 100 in Large Cap"; toggle
  Nifty 500 → request `pools=cap_large&pools=nifty500` and **"490 stocks of 490 in Large Cap +
  Nifty 500 (top-500 mcap)"**; CAT_1 filter (GVM ≥ 7) against the pair → request keeps both pools,
  "98 of 490"; (i) tap on Micro Cap writes "865 in the raw pool; 73 have no current GVM score…" and
  does not toggle the chip; deselect both → the prompt and no request; no page errors, no
  horizontal overflow. Screenshots looked at (VISUAL_VERIFY_GATE_V1).
- **Founder on glass**: `/qb/universe2` (also inside `/quant-basket`'s Build-a-Basket step 1).

## Not touched
CAT_1 filter logic and UI; the Categories 2–7 placeholders; `/api/qb/universe2/pools`;
`_POOL_SQL`; `mcap_rank_daily`.
