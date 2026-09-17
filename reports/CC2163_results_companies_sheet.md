# cc#2163 — Results app PUSH 3/5: tap a sector to open its companies table (QoQ, YoY, GVM) in a scrollable sheet

Sprint RESULTS_APP_V3. Built 17-Sep-2026, 14:48–15:05 IST.

## What shipped

| File | Change |
|---|---|
| `results_app_mobile.py` | Additive. `GET /api/mobile/results_app/companies?segment=<name>` returns only that segment's rows (case-exact match on the string `result_corner_v2` emits) and adds `segment`, `size` (the cc#2162 rule), the sector's `reported` / `total`, and `season_medians {sales_yoy, pat_yoy, pat_n}` taken from the same sectors list. No param → the full list, exactly as before (keys `quarter`, `rows`, `count`). |
| `mobile/results.html` | Every sector row on the page — the hero strip rows, the five in "Sectors this season", and the rows inside the View more sheet — opens the same sheet with that sector's companies. The sheet keeps the push-2 component (fixed, 78vh, drag handle, backdrop, Escape and back close it, body scroll-locked); its open logic is now `stShow()`, shared by the sector list and the companies table. One sorter, `cmpBy(key, asc)` with NULLs last, now serves both the companies view's `tbl()` and the sheet. |
| `tests/test_results_companies_segment.py` | Stubbed endpoint tests: the segment filter + medians + size + total, case-exact matching (an unknown segment is an empty list with null medians, never an error), and the no-param payload's key set unchanged. 3 passed. |
| `reports/CC2163_results_companies_sheet.md` | This report. |

## The sheet

- **Header**: sector name; under it "{n} filed of {total} · median profit {x}% · sales {y}%" from the payload (dashes when NULL). While loading: "Loading…" and "Loading the companies…"; on failure: "Could not load" and "Could not load the companies. Try again", never blank.
- **Body**: a horizontally-scrollable table in the page's existing `.wrap` / `.scroll` / `table` CSS. Columns: Name (sticky first column; symbol bold, company small, a basis dot: brand = detailed filing, muted = CSV-basic) · Profit YoY · Profit QoQ · Sales YoY · Sales QoQ · GVM (score 2dp with the verdict word under it, brand colour at 8+). Default sort Profit YoY descending, NULLs last; tap a header to sort, tap again to flip. Each symbol links to `/m/results?sym=<symbol>`.
- **Footer**: "CSV-basic rows carry no profit YoY (cc#1192 rule) — sales still shown." only when the segment has such rows; then "All filings, sortable ›" → `/m/results?view=companies&segment=<name>` (push 4 makes that page honour the parameter; today it opens unfiltered).
- **No overflow**: the sheet and its body are `overflow-x: hidden`; only the `.scroll` wrapper scrolls sideways (asserted: sheet and body `scrollWidth == clientWidth`, wrapper `scrollWidth > clientWidth`, document never wider than the viewport).

## Harness (Playwright, real Chromium, `scratchpad/cc2163_test.py`) — ALL PASS at 375×812, goldnight + aquawhite

Fixture: the 87 sector rows from cc#2162 plus the company rows for two segments replicated on production at 14:48 IST with `result_corner_v2()`'s method (detailed rows from `fundamentals_history` with QoQ against the previous quarter and YoY against four quarters back; CSV-basic rows from `screener_raw` with no profit YoY): Shipping & Maritime (6 filed of 7: KMEW +472.7%, GESHIP +159.7%, SCI +74.9%, SHREEJISPG +18.9%, then JITFINFRA and SEAMECLTD as CSV-basic) and Retail - Mid (25 filed, 5 detailed + 20 basic, EMIL +450.0% first). The Shipping medians the sheet header shows (+117.3% / +40.6%) are the medians of exactly these rows.

1. Tap Shipping & Maritime in the hero strip → the sheet asks `/companies?segment=Shipping & Maritime`; header "Shipping & Maritime / 6 filed of 7 · median profit +117.3% · sales +40.6%" (= the tapped sector row); six rows in Profit YoY order with the two basic rows last, dashes in Profit YoY and muted dots; GESHIP 8.19 Excellent in brand colour; six headers with Profit YoY marked; footer line + "All filings, sortable ›" to the segment; KMEW links to `/m/results?sym=KMEW`; table scrolls sideways, sheet / body / page do not; body locked.
2. Tap the Sales YoY header → KMEW, GESHIP, SEAMECLTD, SCI, SHREEJISPG, JITFINFRA (desc); tap again → ascending.
3. Back → sheet closed, page unlocked. From the five rows: open, Android back → closed with the scroll position kept (914 → 914 px).
4. View all → the 87-sector sheet → tap Retail - Mid → the same sheet becomes its companies table: "25 filed of 25 · median profit +50.0% · sales +12.7%", 25 rows, 20 CSV-basic with dashes; the sheet scrolls down, the table sideways.
5. A segment with no CSV-basic rows (variant) → no explanation line, the All filings link stays.
6. Endpoint down → "Could not load the companies. Try again"; Try again → six rows.
7. Tap a symbol → `/m/results?sym=KMEW`.
8. No page errors on either theme. The push-1 and push-2 harnesses were rerun after the shared-sheet refactor: ALL PASS.

**Screenshots looked at** (`scratchpad/cc2163_*.png`): `375_dark_shipping` — the sheet over the dimmed page: "Shipping & Maritime", the medians line, the table with the sticky Name column (symbol, gold dot, company name under it), Profit YoY in green/red, dashes on the two CSV-basic rows with muted dots, GVM with the verdict word; the footer explanation and the All filings link. `375_dark_sorted_sales` — Sales YoY header marked ▴ with the rows in ascending sales order. `375_light_retail_mid` — the light theme, "Retail - Mid / 25 filed of 25 · median profit +50.0% · sales +12.7%", EMIL first, the long list scrolling inside the sheet.

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. `https://scorr.in/api/mobile/results_app/companies?segment=Shipping%20%26%20Maritime` → only that segment's rows, `season_medians.pat_yoy` +117.3 (as of the 17-Sep numbers), `total` 7; `…/companies` with no param → unchanged full list (`quarter`, `rows`, `count` only).
2. `https://scorr.in/m/results` → tap any sector row (strip, the five, or inside View all) → the sheet; header medians equal the row tapped; a symbol tap lands on `/m/results?sym=`; back closes the sheet.

## Out of scope (by spec)

The companies view honouring `?segment=` (push 4, cc#2164).
