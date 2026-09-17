# cc#2168 — Results app PUSH 6/7: Upcoming results with Large / Mid / Small / Micro chips, 30-day sheet, no bare BSE codes

Sprint RESULTS_APP_V3 (added 17-Sep 11:41 from the founder's second screenshot). Built 17-Sep-2026 from 13:52 IST server time; the landing time is in the task row.

## What shipped

| File | Change |
|---|---|
| `results_app_mobile.py` | Additive. The season payload's `upcoming` rows now come from a 30-day window (`UPCOMING_DAYS = 30`, returned as `upcoming_days`), joined to `mcap_rank_daily.cap_category` at `MAX(rank_date)` (`size`: large / mid / small / micro, `null` when the ticker is not in the ranked universe) and to the latest `gvm_scores` (`gvm`, `null` if absent); rows keep ex-date order, limit 200. `upcoming_row()` maps each row; a purely numeric ticker (a BSE scrip code) gets `display` = company name and `bse_code` = the code, so it is never rendered as an NSE symbol. `earnings_calendar` is read only. |
| `mobile/results.html` | "Upcoming results" replaces the "Next 10 days" three-column grid (that CSS is gone). Chips All / Large / Mid / Small / Micro, each with its count over the window; below, the next 10 matching rows grouped by date ("17 Sep" header, then name, company small, GVM 2dp on the right); rows with no size appear only under All with an "unranked" tag; "{K} more · View all ›" opens the shared sheet with every row in the window for the chosen chip; an empty chip says "No {Size} results on the calendar in the next 30 days." Chip state lives in the URL as `?up=large|mid|small|micro`, alongside the sector table's `?perf=&size=` (one URL sync carries all three). The upcoming chips have their own class (`up-chip`, same look as the sector chips) so nothing selects them by the sector table's class. |
| `tests/test_results_upcoming.py` | The row mapper: a ranked row keeps its symbol, size and GVM; an unranked row has neither; a BSE scrip code shows by company name with `bse_code` set; the window is 30 days. 4 passed. |
| `reports/CC2168_results_upcoming.md` | This report. |

## Production rows (spec verify item 1), 17-Sep 13:52 IST server time

The season query's own join, replicated on production (`earnings_calendar` rows with `ex_date` in the next 30 days and `verified <> 'false'`, joined to the ranked universe and the latest GVM):

| ex-date | ticker | display | size | GVM |
|---|---|---|---|---|
| 17 Sep | 543435 | Clara Industries (`bse_code` 543435) | unranked | — |
| 17 Sep | ORISSAMINE | ORISSAMINE | micro | 5.36 |
| 17 Sep | SKYWAYS | SKYWAYS | micro | — |
| 18 Sep | HTEL | HTEL | micro | — |
| 18 Sep | SUPREMEENG | SUPREMEENG | unranked | — |
| 18 Sep | TOYAMSL | TOYAMSL | unranked | — |
| 21 Sep | AUGMONT | AUGMONT | small | — |
| 21 Sep | ELITECON | ELITECON | micro | 5.78 |
| 21 Sep | LUMINO | LUMINO | micro | — |
| 21 Sep | SYMBIOTEC | SYMBIOTEC | small | — |
| 17 Oct | INDIACEM | INDIACEM | small | 4.22 |

11 rows in 30 days (10 within 10 days), 8 ranked (micro 5, small 3, large 0, mid 0) + 3 unranked including the numeric 543435 — exactly the card's evidence. So today the Large and Mid chips read 0 and show the empty sentence; the Micro chip carries 5.

## Harness (Playwright, real Chromium, `scratchpad/cc2168_test.py`) — ALL PASS at 375×812, goldnight + aquawhite

Fixture = the 11 rows above in the shape `upcoming_row()` emits, plus the cc#2162 sector rows for the rest of the page.

- All: chips All 11 / Large 0 / Mid 0 / Small 3 / Micro 5 (size chips 8 + 3 unranked = the All count), All pressed; the next 10 rows grouped under "17 Sep", "18 Sep", "21 Sep"; "1 more · View all ›"; the first row reads "Clara Industries" with "BSE code 543435" and the unranked tag, no symbol link, and no row shows "543435" as a name; the unranked tag sits on exactly 543435, SUPREMEENG and TOYAMSL; GVM 5.36 on ORISSAMINE, a dash where absent; symbol rows link to `/m/results?sym=`; rows ≥ 44 px; the section hint "next 30 days · 11 on the calendar"; it is the last section on the page.
- View all → the shared sheet "Upcoming results · All sizes · 11" with all 11 rows across "17 Sep", "18 Sep", "21 Sep", "17 Oct" (the sheet count = the window total), body locked; back closes it.
- Micro → the 5 micro rows only, no unranked row, no View more, URL `?up=micro`. Large → "No Large results on the calendar in the next 30 days.", never blank.
- The URL carries `?perf=under` and `?up=large` together after tapping both controls. `?up=mid` cold-opens the Mid chip (0 today → the sentence); `?up=small` cold-opens the three small rows across two dates.
- No sideways scroll; no page errors; the old grid and its CSS are gone from the file. The push-1, push-2, push-3 and push-4 harnesses were rerun after this change: ALL PASS (push-4's section-order expectation now ends in "Upcoming results").

**Screenshots looked at** (`scratchpad/cc2168_*.png`): `375_dark_all` — "UPCOMING RESULTS · next 30 days · 11 on the calendar", the five chips with All 11 pressed, the list grouped under gold date headers, "Clara Industries / BSE code 543435 UNRANKED" first with a dash for GVM, ORISSAMINE with 5.36, "1 more · View all ›" under the list. `375_light_sheet` — the sheet on the light theme, "Upcoming results · All sizes · 11", four date groups down to 17 Oct with INDIACEM 4.22 last. `375_dark_large_empty` — the Large chip pressed and the one-line sentence in the list panel.

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. `https://scorr.in/api/mobile/results_app/season` → `upcoming_days` 30; every `upcoming[]` row carries `size`, `gvm`, `display`, `bse_code`; the 543435 row has `display` "Clara Industries" and `bse_code` "543435".
2. `https://scorr.in/m/results` → chips with counts summing to All (unranked only under All); View all → the sheet with the window total; `?up=mid` reopens filtered.

## Out of scope

Anything on the web /result-corner page; Past results (push 7, cc#2169).
