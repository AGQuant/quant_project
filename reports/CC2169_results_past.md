# cc#2169 — Results app PUSH 7/7: Past results button → full-screen view, size chips on top, table of filed date / sales YoY / profit YoY / GVM

Sprint RESULTS_APP_V3 (added 17-Sep 11:41). Built 17-Sep-2026 from 13:58 IST server time; the landing time is in the task row.

## What shipped

| File | Change |
|---|---|
| `results_app_mobile.py` | Additive. Every `/api/mobile/results_app/companies` row gains `size` from `mcap_rank_daily.cap_category` at the latest rank date (`_size_map()`, the same source push 6 uses for the calendar); `None` when the symbol is outside the ranked universe. The existing `tier` field is untouched for other readers. The `?segment=` rows carry it too. |
| `mobile/results.html` | A wide "Past results" button card at the end of the Upcoming section (icon, "Past results", "{reported} companies filed this season", chevron) → `/m/results?view=past`. New `renderPast()`: title "Past results", back arrow to `/m/results`, chips All / Large / Mid / Small / Micro on top with counts, then the page's sortable table with Name (symbol bold + company small + basis dot) · Filed (dd Mon) · Sales YoY · Profit YoY · GVM (2dp + verdict small). Rows = `/companies` rows with a filed date; default sort Filed descending, NULLs last; tap a header to sort, again to flip; chip filter client-side on `size`. Footer "{n} of {total} shown · tap a header to sort · dash = not filed or CSV-basic", plus the CSV-basic line once when such rows are in view. Chip and sort state in the URL (`?view=past&size=large&sort=pat_yoy&dir=desc`), honoured on a cold open. `/m/results?view=companies` is untouched. |
| `tests/test_results_companies_segment.py` | Extended: `size` on every companies row from the ranked universe, `None` when absent; the no-param payload's keys unchanged. 3 passed. |
| `reports/CC2169_results_past.md` | This report. |

Four size chips, not three: the spec's own reasoning stands — a name cannot be micro in Upcoming and small in Past results — so Micro stays; one line from the founder overrules it.

## Counts by size on production (spec verify item 1), 17-Sep 13:56 IST server time

The companies list as `result_corner_v2()` assembles it (same-quarter reporters, detailed + CSV-basic, plus names reported in the last 45 days), joined to `mcap_rank_daily` at its latest rank date:

| size | companies | with a filed date |
|---|---|---|
| micro | 747 | 670 |
| small | 729 | 676 |
| mid | 143 | 133 |
| large | 99 | 92 |
| unranked | 0 | — |

So the live view shows 1,571 rows under All and every row carries a size.

## Harness (Playwright, real Chromium, `scratchpad/cc2169_test.py`) — ALL PASS at 375×812, goldnight + aquawhite

Fixture: the 31 Shipping & Maritime / Retail - Mid company rows from cc#2163 with their real sizes and filed dates, plus the 13 Private Banks rows replicated the same way (4 large, 6 mid, 3 small; FEDERALBNK and KARURVYSYA without a filed date) — 44 companies, 42 filed: large 4 / mid 5 / small 18 / micro 15.

- Season page: the button card sits directly after the Upcoming list, 58 px tall, full width, "Past results · 1,712 companies filed this season", href `/m/results?view=past`. Tap → the view; back → the season page.
- The view: title "Past results", back arrow to `/m/results`, as-of "Season Q1 FY27 · 42 filed of 44"; chips All 42 / Large 4 / Mid 5 / Small 18 / Micro 15, All pressed; columns Name · Filed ▾ · Sales YoY · Profit YoY · GVM; 42 rows newest first (KMEW, SHREEJISPG, KITEX, LUXIND on 14 Aug, then 13 Aug …); CSV-basic rows show a dash for Profit YoY with the muted dot, and the footer carries the cc#1192 line once; footer "42 of 44 shown · tap a header to sort · dash = not filed or CSV-basic"; the table scrolls inside `.scroll`, the page never overflows sideways.
- Large → AXISBANK, HDFCBANK, ICICIBANK, KOTAKBANK only; URL `?view=past&size=large`; footer "4 of 44 shown".
- Tap Profit YoY → KOTAKBANK, AXISBANK, HDFCBANK, ICICIBANK (desc), URL `sort=pat_yoy&dir=desc`; tap again → ascending.
- `?view=past&size=mid&sort=gvm&dir=asc` cold-opens the Mid chip sorted by GVM ascending (INDUSINDBK, IDFCFIRSTB, YESBANK, RBLBANK, AUBANK).
- No page errors. The push-3, push-4 and push-6 harnesses were rerun after this change: ALL PASS.

**Screenshots looked at** (`scratchpad/cc2169_*.png`): `375_dark_button` — under the Upcoming list, the wide card "Past results / 1,712 companies filed this season" with the icon tile and gold chevron. `375_dark_all` — "PAST RESULTS" header with the back arrow, "Season Q1 FY27 · 42 filed of 44", the five chips with All 42 pressed, the table with Filed ▾ marked, KMEW first with +472.7%, dashes on the CSV-basic rows, GVM with the verdict word. `375_light_large` — the light theme with Large 4 pressed and the four bank rows, footer "4 of 44 shown".

## For cc#2165 (the closing report)

The sprint's screenshot set now also covers pushes 6 and 7: `cc2168_375_dark_all`, `cc2168_375_light_sheet`, `cc2168_375_dark_large_empty`, `cc2169_375_dark_button`, `cc2169_375_dark_all`, `cc2169_375_light_large`.

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. `https://scorr.in/api/mobile/results_app/companies` → every row carries `size`; counts over rows with a `reported` date ≈ micro 670 / small 676 / mid 133 / large 92.
2. `https://scorr.in/m/results?view=past` → newest first; tap Large → only large rows; tap Profit YoY → sorted; no sideways scroll outside the table. The button on the season page lands there; back returns.
