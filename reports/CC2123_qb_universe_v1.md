# cc#2123 — QB Universe Builder V1 (pool selection + GVM/component-score filters)

## Scope decision, stated before any code was written
The spec's full ambition is 7 filter categories / ~40 filters across 6 symbol pools. That is too
large to build and verify honestly in one sitting. Logged to `cc_task_logs` before writing code
(mirroring the cc#2120 Part A/Part B precedent): **V1 ships pool selection (6 pools, real
scored counts) + CAT_1 (GVM & Component Scores — 6 filters: gvm/g/v/m score range, verdict,
segment rank) + the click-through results table.** CAT_2 through CAT_7 (Sector & Segment, Price &
Momentum, Change & Delta, Quarterly Results YoY, Alpha & Risk, Quality/Valuation/Size/Ownership —
34 remaining filters) ship as visible "COMING" sections on the page, not hidden and not built.
Mobile/app-native card parity (a `mobile/*.html` idiom matching `alerts.html`) is also explicit
follow-up, not attempted this push — this V1 is a responsive WEB page, confirmed error-free down
to 360px, not a native app card view.

The card's own spec carried two conflicting instructions from different phases: an early
`do_not_touch` ("ship no code, `v12_endpoints.py`/`scorr_v12.html` are off-limits") and a later,
explicitly-dated founder ruling ("FOUNDER_RULING_16SEP_STOP_ASKING_BUILD_IT... CC builds V1").
Resolution: the FILE-level protection of the old v12 files stands (neither was touched — confirmed
zero diff below); the "ship nothing" framing is superseded by the dated ruling. Built entirely new
files instead (rule 5): `qb_universe_builder.py` + `scorr_qb_universe.html`.

## Data-quality finding, caught by smoke-testing real SQL before wiring the UI
A pool's "size" is not its raw source-table row count — a real fraction of every pool has no
current `gvm_scores` row (GVM is a stock-quality score; some pool members are not stocks or lack
coverage):

| Pool | Raw count | Scored count | Gap reason |
|---|---|---|---|
| Large Cap | 100 | 100 | — |
| Mid Cap | 149 | 149 | — |
| Small Cap | 748 | 744 | 4 symbols, no current GVM score |
| Micro Cap | 761 | 732 | 29 symbols, no current GVM score |
| All F&O Stocks | 208 | 205 | 3 index futures (BANKNIFTY/NIFTY/NIFTY50) — GVM does not apply to an index |
| Nifty 500 (top-500 mcap) | 500 | 497 | 3 symbols, no current GVM score |

Every pool's displayed count is the SCORED count (what CAT_1 filters can actually act on), with the
raw-vs-scored gap stated as a visible note, never silently dropped. `/pools` and `/preview` share
the identical scored-count query so the two endpoints can never disagree with each other.

**Nifty 500 is additionally stale**: `built_at = 2026-06-02 17:27:52+00:00` — ranked over three
months ago and not rebuilt since, while the underlying market caps move daily. Surfaced as an
amber "STALE SINCE 2026-06-02" badge plus a visible explanatory line (not hover-only — see Bug 1
below). This may be the same underlying frozen-ranking mechanism as cc#2125's
`mcap_rank`/`screener_raw.market_cap` finding (103 band changes) — not investigated further here,
flagged as a lead for whoever picks up cc#2125.

## Real, hand-verified filter arithmetic (before shipping, not after)
Ran the actual combined SQL directly against production data for "All F&O Stocks, `gvm_score>=7.5`
AND `seg_rank<=3`" — the exact scenario the Playwright harness replays. First draft of the test
fixture guessed a count of 9 without running the query; caught before finalizing, re-ran for real:
**true count is 11**, not 9. Ran each filter alone to find the true binding filter:
`gvm_min` alone → 11 rows; `seg_rank_max` alone → 128 rows. `gvm_min` is the real binding filter
(the tighter one), not `seg_rank_max` as first assumed. The 11 real rows (GLENMARK highest at
8.00 down to ADANIPORTS at 7.56) are what the test fixture and the screenshot below both show.

## What was built
**`qb_universe_builder.py`** (new file, `include_router`ed in `main.py`):
- `GET /api/qb/universe2/pools` — 6 pools (`cap_large/mid/small/micro`, `fo`, `nifty500`), each
  with raw count, scored count, and a note when they differ; `nifty500` additionally carries
  `stale`/`built_at`/`stale_note`.
- `GET /api/qb/universe2/preview` — CAT_1 filters (gvm/g/v/m score min/max, verdict list,
  segment-rank min/max) against a pool; returns `pool_count` (scored, same query as `/pools`),
  `count` (post-filter), the matching rows, and `binding_filter` (each applied filter re-run in
  isolation, the tightest one reported with its own cut count).
- `GET /qb/universe2` — serves `scorr_qb_universe.html`.

**`scorr_qb_universe.html`** (new file): pool selector, CAT_1 filter builder (add/remove filter
rows, min/max or checkbox-list inputs, a free-text note field per filter), CAT_2–CAT_7 rendered as
visible "COMING" headers with their real filter counts, a count bar (`pool → N pass`, binding
filter line, EOD as-of date), and a click-to-open results table with dynamic columns (Symbol/
Company/Segment plus only the columns for filters actually applied).

**Wiring (rule 8, confirmed against the current file state, not recalled)**:
`main.py:43` imports the router, `main.py:850` includes it, `main.py:282` adds `/qb/universe2` to
`_PWA_INJECT_PATHS`, `main.py:302` adds it to `PROTECTED`, `main.py:1644` adds it to
`NAV_REGISTRY` as `"QB Universe builder (cc#2123 V1)"`. `pwa_endpoints.py:404` adds
`['/qb/universe2', '⬚', 'QB Universe']` to the live `NAV` array inside `PWA_JS` (verified by
importing the module and running `node --check` on the real extracted string, not just a static
read — the file carries a documented `#` bundle-breaking warning that does not apply here since
every use is inside a `//` comment, already used dozens of times elsewhere in the same array).
**Label: "QB Universe" · URL: `/qb/universe2`.**

## Two bugs found by Playwright, fixed, and re-verified
1. **Stale-ranking explanation was hover-only.** `poolRow()` put `p.stale_note` only inside the
   `.badge-stale` span's `title=` attribute — invisible without a mouse hover, and never part of
   the page's real text. Fixed: `poolRow()` now joins `p.note` and `p.stale_note` into the same
   visible `.pool-note` div already used for the raw-vs-scored explanation (kept the `title=` too,
   as a bonus tooltip). Confirmed visually in the screenshot below — the Nifty 500 row now shows
   both explanations as real page text.
2. **Results table fought its own click handler.** `renderResults()` unconditionally set
   `#resultsWrap.style.display = ''` at the end of every successful preview run, so the table was
   already open before any click — meaning the count's `onclick="toggleResults()"` actually closed
   it, backwards from the spec's "click to open" design. Fixed: removed the unconditional show,
   leaving the table's static `display:none` default and `toggleResults()` (bound to `.count-num`)
   as the sole visibility control.

Both were caught by an automated check that was itself run against real production data (see
Verify below), not by inspection.

## Verify

**Syntax**: `ast.parse` + `py_compile` clean on `qb_universe_builder.py` and `main.py`. `node
--check` clean on the page's inline `<script>` block (extracted verbatim from the actual `.html`
file, not retyped) and on the live `PWA_JS` string extracted from the running `pwa_endpoints`
module.

**Real-data Playwright harness** (`scorr_qb_universe.html` extracted verbatim, only
`fetchWithTimeout` stubbed to return the real `/pools` and `/preview` JSON captured this session
via direct SQL, not fabricated) — full run, all checks pass after the two fixes:
- 6 pool rows, real scored counts (Large 100, Mid 149, Small 744-not-748, Micro 732-not-761, F&O
  205-not-208, Nifty500 497-not-500), each gap's note text present.
- Nifty 500 stale badge ("stale since 2026-06-02") AND the frozen-ranking explanation both present
  as real page text (bug 1, re-verified after fix).
- Selecting the F&O pool, adding GVM Score (≥7.5) and Rank within Segment (≤3): count bar reads
  "205 → 11 pass", binding filter line reads "gvm_min cuts to 11" — matches the independently
  hand-verified SQL exactly.
- Clicking the count opens the results table (bug 2, re-verified after fix); columns are exactly
  Symbol/Company/Segment/GVM Score/Rank within Segment (case-insensitive match — `thead th` is
  intentionally `text-transform:uppercase`, the same convention used elsewhere on this page; a
  case-sensitive assertion in the test itself was the bug there, not the page — fixed the test).
- 11 real rows render, first GLENMARK (8.00), last ADANIPORTS (7.56) — matches the hand-verified
  SQL row-for-row.
- All 6 CAT_2–7 categories visible with "Coming" tags and their real filter counts.
- Removing a filter drops the active count back correctly.
- Zero page errors at 1200px and at 360/375/390px.

**VISUAL_VERIFY_GATE_V1** — three screenshots taken and looked at directly with vision, not just
asserted on:
- **Desktop, pool list**: clean page, honest V1-scope sentence in the intro ("pool selection + GVM/
  component-score filters are live. The other six categories... are shown below... not wired to
  real filtering yet"). Every pool with a raw/scored gap shows its note as real visible text; Nifty
  500 shows both the amber stale badge and the combined note sentence beneath it. All 6 "Coming"
  categories listed with real filter counts and a "COMING" tag, not hidden.
- **Desktop, results open**: F&O pool selected (highlighted), 2 active filters shown with their
  values and Remove links, count bar "205 → 11 pass" with the binding-filter line, EOD as-of date,
  and the results table open beneath showing exactly 11 rows with the 5 expected columns — matches
  the hand-verified data exactly, nothing fabricated.
- **Mobile 375px**: page reflows to a single column, all notes and the stale badge wrap legibly,
  filter-add buttons wrap into rows, no horizontal overflow or clipped text, "Coming" categories
  section remains fully visible.

## What shipped vs. what's named follow-up
**Shipped**: pool selection (6 pools, real scored counts + honest gap notes), CAT_1 (6 filters:
gvm/g/v/m score range, verdict, segment rank), count bar with binding-filter explanation,
click-through results table with dynamic columns, NAV registration.

**Explicit follow-up, visible on the page as "Coming", not built this push**: CAT_2 Sector &
Segment (5 filters), CAT_3 Price & Momentum (5), CAT_4 Change & Delta (7), CAT_5 Quarterly Results
YoY (6), CAT_6 Alpha & Risk (5), CAT_7 Quality/Valuation/Size/Ownership (6) — 34 filters total.
Mobile/app-native card parity (a dedicated `mobile/*.html` idiom) is also not attempted this push;
the current page is a responsive web page only.

## What did NOT change
`v12_endpoints.py`, `scorr_v12.html`, `_UNI_COLS`, V13 vocabulary, and every existing QB preset —
confirmed zero diff (new files only, nothing in either old file touched). No writes to any table —
`/pools` and `/preview` are read-only against `gvm_scores`, `futures_universe`, `nifty500_universe`,
and `input_raw`.
