# cc#2162 — Results app PUSH 2/5: sector table with Top / Under toggle, size chips, five rows, View more sheet

Sprint RESULTS_APP_V3. Built 17-Sep-2026, landed on main 13:35 IST (times corrected to the git commit clock).

## What shipped

| File | Change |
|---|---|
| `results_app_mobile.py` | Additive. Each `/api/mobile/results_app/season` sector row gains `size` (Large / Mid / Small from `avg_mcap` on `investment_check.CAP_LARGE_MIN` / `CAP_MID_MIN`, imported inside `sector_size()`, never retyped; `None` when there is no average) and `pat_n` (= `pat_n_detailed`, passed through). New top-level `sectors_unsized` = rows with no size. Nothing else in the payload changes. |
| `mobile/results.html` | "Sectors this season" replaces the "Sector by sector" ladder (the `.lad` / `.lr` CSS and markup and the "Every company that filed, sortable" line are gone). Row 1: segmented toggle Top performers / Underperformers (brand fill on the active one). Row 2: All / Large / Mid / Small chips, one at a time, each with its eligible count. Then five rows, a "View more" line, and a bottom sheet. State mirrored to `?perf=top|under&size=large|mid|small` (defaults omitted). |
| `tests/test_results_sector_size.py` | The band helper on the imported cuts (boundaries inclusive at the cut), `None` for no average, and a guard that neither 20000 nor 5000 is typed in `results_app_mobile.py`. 3 passed. |
| `reports/CC2162_results_sector_table.md` | This report. |

## Rules, stated on the section

- Ranking basis line under the heading: "ranked by median profit growth vs last year · sectors with 3+ filings". Eligible = `pat_n >= 3`. Top = `pat_yoy` descending, Under = ascending.
- Row (grid `minmax(0,1fr) 70px 70px`): sector name (bold, ellipsis) + "N filings · GVM {verdict}" (+ THIN tag when N is 3–4); Profit YoY bold in win/loss colour; Sales YoY muted. Each row links to `/m/results?view=companies&segment=<sector>` until the push-3 companies sheet exists.
- "View more": "{K} more sectors[ in {size}] · View all ›", hidden when K = 0. It opens a bottom sheet (fixed, max-height 78vh, drag handle, backdrop tap, Escape and the back button close it; history entry pushed before the scroll lock, the cc#2159 lesson) headed "{Top performers|Underperformers} · {All sizes|Large|Mid|Small} · {N} sectors", listing every eligible sector in the current toggle + size with the same row renderer and order.
- Empty states, measured: fewer than five eligible → the rows it has plus "Only {n} {Size} sectors have 3+ filings this season."; zero → "No {Size} sector has 3+ filings this season." alone. The chips still show 0 for that size.
- Size chips read the payload's `size`; a row with no size never matches a size chip and is counted in `sectors_unsized` (0 today).

## Counts (spec verify item 1)

Production replicate of the payload's sector rows (same method as `result_corner_v2()`: same-quarter reporters, detailed + CSV-basic, per-segment medians, `n_used` = profit readings; average market cap over the sector's full universe membership, `COALESCE(screener_raw.market_cap, gvm_scores.market_cap)`), 17-Sep ≈13:31 IST:

| Eligible (3+ profit readings) | Large (≥ 20,000 Cr avg) | Mid (≥ 5,000) | Small | unsized |
|---|---|---|---|---|
| 87 | 47 | 32 | 8 | 0 |

These equal the card's own evidence (87 → 47 / 32 / 8). With the cuts confirmed from `investment_check` (CAP_LARGE_MIN 20000, CAP_MID_MIN 5000), Fable's "pending CC confirming" note is closed. Small is 8, so today the live page shows five Small rows plus "3 more sectors in Small · View all ›" rather than the thin-state sentence; the sentence is proven on a fixture variant (below).

## Harness (Playwright, real Chromium, `scratchpad/cc2162_test.py`) — ALL PASS at 375×812, goldnight + aquawhite

Fixture = the 87 eligible sectors from the replicate with their sizes and GVM verdicts (`scratchpad/cc2162_sectors.json`).

- Top / All: five rows = Commodity & Chlor-Alkali Chemicals +275.9% (3, THIN), Shipping & Maritime +117.3% (4, THIN), Organic Chemicals - Small +102.0% (4, THIN), Aluminium & Non Ferrous +90.9% (5), IT - Small +89.5% (5); sub-lines "N filings · GVM {verdict}"; chips All 87 / Large 47 / Mid 32 / Small 8, All pressed, 40 px; "82 more sectors · View all ›"; every row ≥ 44 px, links to the companies view with its segment; profit column coloured; number columns stay inside the row; no ladder markup, no "Every company that filed" line.
- Under / All: Defence - Small −35.5%, Cement - Large & Mid −27.1%, Refineries & Exploration - Large −24.6%, Holding Companies −20.9%, Broadcasting & OTT −4.3%; URL `?perf=under`.
- Under / Small: five rows + "3 more sectors in Small"; URL `?perf=under&size=small`.
- Top / Large: five rows + "42 more sectors in Large"; View all → sheet "Top performers · Large · 47 sectors", exactly 47 rows in the same order (first five = the five above), body scroll-locked, sheet ≤ 78vh, no row's numbers outside it; back button closes it and unlocks the page.
- `?perf=under&size=mid` cold-opens to Under + Mid: Defence - Small, Holding Companies, Broadcasting & OTT, Fertilizers, IT - Mid.
- Variant with 3 eligible Small sectors → the 3 rows + "Only 3 Small sectors have 3+ filings this season.", View more hidden. Variant with 0 → "No Small sector has 3+ filings this season." alone, chip "Small 0".
- No sideways scroll, no page errors on either theme.

**Screenshots looked at** (`scratchpad/cc2162_*.png`): `375_dark_top_all` — the gold Top performers pill, All 87 pressed, the five rows with THIN tags on the first three, "82 more sectors · View all ›". `375_light_under_all` — Underperformers, red profit column, "Refineries & Exploration - Large" now ellipsises with +34.4% fully visible (the first run clipped it: grid columns defaulted to `min-width:auto`; fixed with `minmax(0,1fr)` and `min-width:0`). `375_dark_sheet_top_large` — the sheet over the dimmed page: "Top performers · Large · 47 sectors", the basis line, rows from Aluminium & Non Ferrous +90.9% down. `375_dark_small_thin_variant` — Small 3 pressed, three rows and the sentence "Only 3 Small sectors have 3+ filings this season."

## Live check (Fable — the sandbox cannot reach scorr.in)

1. `https://scorr.in/api/mobile/results_app/season` → every `sectors[]` row carries `size` and `pat_n`; count `size` over rows with `pat_n >= 3` → Large 47 / Mid 32 / Small 8; `sectors_unsized` 0.
2. `https://scorr.in/m/results`: Top / All five rows sorted by profit; Underperformers; Small chip → five + "3 more"; View all → the sheet with the Large count; `?perf=under&size=mid` reopens that view.

## Out of scope (by spec)

Companies sheet (push 3, cc#2163) — the rows and the sheet rows will point at it once it exists.
