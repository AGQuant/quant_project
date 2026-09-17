# cc#2158 -- Screeners app PUSH 3/5: Custom Screener backend (10 button-only filters, EOD, own file)

New file `custom_screener_app.py` (own router). `main.py` diff = one import + one `include_router`, nothing
else. **No write, no table, no scheduler row: an on-demand EOD query, so ENGINE_LIVENESS_RULE does not
apply.** Not the TC score (founder ruling 17-Sep). `grep -ci "tick\|tc_" custom_screener_app.py` = 0.

## 1. Shape

- **Registry** = the locked list from the spec / cc#1199 log 6913, as code constants: 10 filters, 3 buttons
  each (30 buttons). Every button is ONE SQL predicate over the scored CTE; predicates are constants, no
  request text ever reaches the SQL; every predicate is false on NULL (cc#1822).
- **Scored CTE** (copied shape, not imported): `gvm_scores` at `MAX(score_date)` LEFT JOIN `mcap_rank_daily`
  (cap_category at MAX(rank_date)), `sector_ratings` (verdict at MAX(score_date), on segment),
  `investment_check_v2_scores` (score10 + band at MAX(score_date)), `universe_technicals` (dma_50, dma_200,
  week_index_52, year_return at MAX(score_date)); `has_tech` marks a technicals row.
- `GET /api/mobile/custom_screener/meta` -> `{filters[10]{key,label,rule_text,source,technicals,
  buttons[{key,label,alone}]}, universe, technicals_missing, as_of{gvm,invest,technicals,rank,sector,
  price_basis}, query_ms, basis}`. ONE query: 30 `COUNT(*) FILTER` in a single pass.
- `GET /api/mobile/custom_screener/run?size=large,mid&gvm=good&...` (repeated or comma values, any subset
  of the 10 keys, case-insensitive, duplicates dropped) -> `{count, rows[<=300], universe, as_of,
  applied[{key,label,buttons_chosen,buttons_label,rule_text}], row_cap, query_ms, basis}` plus
  `excluded_no_technicals` only when a technicals-backed filter (trend / range52 / ret1y) is on = names
  that passed every other filter but have no technicals row today. ONE query: `base` (non-technicals
  filters) -> `matched` (technicals filters) -> universe count, full match count, dropped count, the five
  dates and the first 300 rows as JSON. Sort `invest_score DESC NULLS LAST, gvm DESC, symbol`. `count` is
  always the full count.
- Row: `symbol, company_name, segment, cap, gvm, verdict, g, v, m, invest_score, invest_band, price,
  year_return, w52`. `price` = `gvm_scores.price` = the EOD close at the GVM date (`as_of.price_basis`).
- Unknown key -> `{error: "unknown filter 'x'", known}`; unknown button -> `{error: "unknown button 'x'
  for 'size'", known}`. Nothing selected -> the query runs with `LIMIT 0`: `count` = universe, `rows: []`,
  `note: "pick at least one filter"`. Never dumps the universe.

## 2. Unit tests -- `scratchpad/cc2158_unit.py`, `=== ALL cc#2158 UNIT TESTS PASS ===`

Parser (comma + repeated + case + spaces + dupes + empty, registry order for keys, input order for
buttons, both error shapes), WHERE builder (OR inside, AND across, technicals split, `TRUE` when empty),
the SQL text (`LIMIT 300` vs `LIMIT 0`, `base`/`matched`, the sort), and both endpoints on a fake cursor
(10 filters x <=3 buttons with `alone`; run: count 32 from the canned row, applied order, sort held,
`excluded_no_technicals` only with trend on, empty -> note, both errors, the basis line). Source grep for
`tc_` / tick = 0.

## 3. Live runs on production (the module's exact SQL, 17-Sep-2026 12:05 IST)

| Call | Result |
|---|---|
| `/meta` | universe 1773; technicals_missing **0** today (technicals date 2026-09-17 covers 1773/1773; the spec's 46 was measured on an earlier technicals date); alone counts: size large 100 / mid 147 / small&micro 1526; sector good 33 / average 1343 / weak 397; GVM good 366 / average 572 / weak 835; growth strong 581 / steady 556 / slow 636; valuation reasonable 497 / fair 759 / expensive 517; momentum strong 454 / neutral 181 / weak 1138; invest high 363 / medium 388 / low 1022; trend up 486 / mixed 392 / down 744; 52-wk near high 325 / middle 727 / near low 523; 1-yr up 524 / flat 326 / down 725 |
| `/meta` timing | `EXPLAIN (ANALYZE)`: **execution 10.06 ms**, planning 3.1 ms (spec ceiling 400 ms); hash joins over 1,773 rows, index scans on the date indexes |
| as-of dates | gvm 2026-09-16, rank 2026-09-16, sector 2026-09-16, invest 2026-09-16, technicals 2026-09-17 |
| demo 1: `size=large,mid&gvm=good&momentum=strong&invest=high` | **count 32** (spec 32), 32 rows; first GLENMARK invest 8.96 STRONG_BUY, then NAVINFLUOR 8.80, IPCALAB 8.72, POLICYBZR 8.72, WELCORP 8.52 ... last AUROPHARMA 6.54, OFSS 6.52, GRASIM 6.52 -- invest_score never rises down the list |
| demo 2: `size=small&valuation=reasonable&invest=high&trend=up` | **count 88** (spec 88), 88 rows, excluded_no_technicals 0, first row invest 8.12, last 7.66 |
| `trend=up` | count 486, rows capped at 300, `excluded_no_technicals` present = 0 (the live missing count) |
| `gvm=good` | key `excluded_no_technicals` absent (no technicals filter on) |
| no params | count 1773 = universe, rows `[]`, note |

Bands reconcile with the spec's evidence: GVM Good+Excellent 318+48 = 366; Invest STRONG_BUY+ACCUMULATE 43+320 = 363;
mid is 147 today (spec 150 -- live figure).

## 4. Verify list

- `/meta`: 10 filters, <=3 buttons each, every button with `alone`, the as-of dates present -- yes (section 3)
- `/run` demo combos 32 and 88, first row's invest_score >= every later row -- yes
- `/run?trend=up` -> `excluded_no_technicals` = the measured missing count (0 today) -- yes; `/run?gvm=good` omits the key -- yes
- `/run` with no params -> rows `[]`, count 1773, the note -- yes
- no `tc_*` / `tc_universe_ticks` in the file -- grep 0
- `main.py` diff = one import + one include_router -- yes (2 lines)

## 5. Live check after deploy

- https://scorr.in/api/mobile/custom_screener/meta
- https://scorr.in/api/mobile/custom_screener/run?size=large,mid&gvm=good&momentum=strong&invest=high -> count 32
- https://scorr.in/api/mobile/custom_screener/run?size=small&valuation=reasonable&invest=high&trend=up -> count 88
- https://scorr.in/api/mobile/custom_screener/run -> count 1773, rows [], note
