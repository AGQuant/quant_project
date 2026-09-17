# cc#2177 — /m/tcscan position rail: CMP and ENTRY labels no longer overlap when the two prices sit close

Founder screenshot 17-Sep 12:23 IST: the 360ONE SHORT row printed "CMP 1053ENTRY 1065.20". Built 17-Sep-2026 from 14:30 IST server time (the Started line); the checks below ran up to 14:33 IST server time. The landing time is in the task row. One file: `mobile/tcscan.html` (+9 lines). Bar geometry, colours, the P&L numbers, the row order, the sort chips and the hero (cc#2176): untouched.

## Root cause

The rail's labels are absolutely positioned at their price fractions, so ENTRY and CMP collide whenever the two prices sit within a label's width of each other. The shared module `scorr_position_row.js` already carries the collision rule for exactly this — `trkFixLabels()`, the cc#1726 pass, verbatim from `mobile/v8.html`: it measures the rendered ENTRY and CMP boxes, drops ENTRY **below** the rail (`.entry.below`) when the two would touch (6 px gap), then nudges the dropped ENTRY clear of the SL / TGT end labels with min/max clamps inside the rail. `mobile/v8.html` calls it after every render (its line 1010). `/m/tcscan` built its rails on the same geometry (cc#2101) and even names the pass in a comment, but **never called it** — so every close pair collided. On the live book at 14:20 IST that was 8 of the 9 open rows (RELIANCE, 360ONE, IEX, IRFC, TCS, BANKINDIA and the two synthetic rows in the harness).

## The change

1. `draw()` now runs `window.ScorrPositionRow.trkFixLabels(document.getElementById('body'))` right after the body is rendered — every open and closed row, on every redraw (side toggle, sort chip, date chip). One rule shared with V8, not a second one written here (`scorr_card_common.js` line 1984 states the two rails share the rule).
2. A rotation changes the rail width and with it where the labels land, so a 150 ms-debounced `resize` listener redraws from the cached payload and the pass runs again on the new geometry. State (side, sort, date, the hero rail position) is preserved across that redraw.

No price is ever truncated: the labels keep `white-space: nowrap`, and the pass moves them instead of clipping. TGT / SL keep their places at the rail ends; the dropped ENTRY is what moves when it would touch them.

## Harness (Playwright, real Chromium, `scratchpad/cc2177_test.py`) — ALL PASS at 375×812 and 360×780, goldnight + aquawhite, before and after

Fixture: the live open book of cc#2176 (7 SELL rows) with 360ONE at the founder's own prices (entry 1065.20, cmp 1053.30, tgt 1033.24, sl 1097.16), plus two synthetic long rows: **SAME** (cmp == entry, 100.00 / 100.00, tgt 103, sl 97) and **NEAREND** (entry 100.50 on the SL end, sl 100.00, cmp 100.60, tgt 110). The "before" page is `git show HEAD:mobile/tcscan.html` (the page as cc#2176 left it), served to the same fixture.

- **Before, every viewport and theme**: 8 overlapping label pairs across the 10 rows, ENTRY × CMP on RELIANCE, 360ONE, IEX, IRFC, NEAREND, SAME, TCS, BANKINDIA — the founder's defect reproduced (360ONE: "ENTRY 1065.20 x CMP 1053.30").
- **After**: zero overlapping label bounding boxes on any row (pairwise `Range` rects of all `.lbl` elements); every label inside its row (nothing clipped); four labels on every row. 360ONE: ENTRY dropped below the rail (`ENTRY 1065.20` at bottom, between TGT and SL), CMP stays above. SAME: ENTRY below, all four shown. NEAREND: ENTRY below and nudged to the right of "SL 100.00" (ENTRY box starts 7 px after the SL box ends). Rows whose labels never touched are not moved.
- Rotated (viewport swapped to 812 / 780 px wide): the debounced redraw runs, still no overlapping or clipped labels.
- No page errors on any run. The cc#2176 harness (76 checks) rerun on the changed page: ALL PASS.

**Screenshots looked at** (`scratchpad/cc2177_*.png`): `375_dark_360ONE_before` — the founder's picture: "CMP 1053.30" in gold and "ENTRY 1065.20" in grey printed over each other above the rail. `375_dark_360ONE_after` — CMP alone above the rail, ENTRY below it between "TGT 1033.24" and "SL 1097.16", every character readable. `375_dark_SAME_after` — CMP above, ENTRY below at the same x. `360_light_page_after` — the open list at 360 px on the light theme: IRFC, NEAREND, SAME, TCS, BANKINDIA each with CMP above and ENTRY below, the NEAREND row showing "SL 100.00" then "ENTRY 100.50" side by side with a gap.

## Checks

- `node --check` on the inline script: OK. Literal `var(--x, #hex)` fallbacks: 0. No NAV change.
- `theme_validate` is run on the deployed file after landing; its result is in the task row.

## Live check (Fable — the sandbox cannot reach scorr.in)

`https://scorr.in/m/tcscan` on a phone: on any open row where cmp sits within about 1% of entry (RELIANCE, IEX, 360ONE today), CMP reads above the rail and ENTRY below it, both complete; TGT and SL stay at the ends; rotate the phone and the labels re-settle.

## Out of scope (by spec)

The P&L numbers, row order, sort chips, the hero (cc#2176), `scorr_position_row.js` itself (the rule was already right; the page just never ran it).
