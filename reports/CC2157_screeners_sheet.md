# cc#2157 -- Screeners app PUSH 2/5: tap a screen card, its names open in a sheet on the same page

Founder: cards must be "clickable to display holdings". A tap used to leave for `/m/screeners?screen=<id>`
(the full sortable table). Now a tap opens a bottom sheet on the SAME page with every name in that screen;
the sheet's footer still links to the full table. No backend change; `screeners_app_mobile.py` untouched.

## 1. What changed (`mobile/screeners.html` only)

- **One shared component**: `renderNamesSheet(title, meta, rows, footerHref)`. `rows` = the names (rendered
  in the order given, never re-sorted), `null` = "Loading names…", `false` = "Could not load…", `[]` = an
  honest "No names in this screen on the last run." `meta` = `{count, last_run, rule}`. Push 4 (custom
  screener results) calls the same function with its own rows; `nameRow(r)` is the shared row renderer.
- **Sheet**: fixed, bottom 0, `max-height 78vh`, header (drag handle, screen name, `N names · Last run
  16 Sep 2026`, the rule in plain words), scrollable body, footer `Full table, sortable ›` ->
  `/m/screeners?screen=<id>`. Backdrop above the bottom nav (z 59/60; nav is z 20, shell menu z 70).
- **Row**: symbol (bold) + `sector · ₹mcap Cr` under it; right side GVM 2dp (brand colour from 8.00) with
  `G 7.2 · V 6.8 · M 8.1` under it.
- **Open**: card tap (delegated on `#body`; `data-id`/`data-name` on the card; modifier clicks and middle
  clicks keep the plain link) -> `openScreenSheet(id, name)` -> loading state shown at once -> fetch
  `/api/mobile/screeners_app/screen?id=<id>` (existing endpoint, unchanged) -> rows. A response that
  arrives after the sheet was closed is dropped (sequence counter).
- **Close**: backdrop tap, handle tap, ESC, and the back button. On open the page does
  `history.pushState({skSheet:1})` BEFORE locking the body (so the history entry keeps the page's real scroll
  position) and sets `history.scrollRestoration = 'manual'` while open; `popstate` hides the sheet; the
  other close paths call `history.back()` so the history stays clean.
- **Scroll lock**: `body.sk-lock` (`position:fixed; top:-scrollY`) while open, restored on close with
  `scrollTo(0, scrollY)`. The rails are their own scroll containers, so their position is untouched.
- Copy in the note: "tap a screen to see its names right here". Contract tokens only; `theme_validate`
  raw 0 / no new fallbacks.

## 2. Checks -- `scratchpad/cc2157_test.py`, `=== ALL cc#2157 CHECKS PASS ===`

375x812, touch, dark (goldnight) and light (aquawhite). The `/screen` stubs are the REAL endpoint function
run over the REAL production rows (SQL 17-Sep 11:53 IST: Quality Compounders 30 rows, Future Giants 1 row)
through a fake cursor, so name/rule/count/last_run and every row value are what production returns.

| Check | Result |
|---|---|
| tap Quality Compounders (id 12) | sheet opens, 30 rows, title `Quality Compounders`, header `30 names · Last run 16 Sep 2026`, rule `ROCE ≥ 18 · G ≥ 8 · GVM ≥ 7.5 · 3-yr return ≥ 15%` |
| footer | `/m/screeners?screen=12`, text `Full table, sortable` |
| order | CUPID, MODISONLTD, AEROFLEX … NPST -- the endpoint's rank order, not re-sorted |
| brand colour | 12 of 30 rows have GVM >= 8 and carry `.gd`, matching the payload |
| geometry | sheet top 179 -> bottom 812 of an 812 viewport (633px = 78vh), body scrolls inside, z-index 60 |
| body lock | `body.sk-lock` while open, URL still `/m/screeners` |
| backdrop tap | closes, lock released, `scrollY` restored to 259 (where the page was) |
| Future Giants (id 10) | 1 row (NYKAA), header `1 name · Last run 16 Sep 2026`, no empty-state text, no padding rows |
| handle tap / ESC | close |
| rail scroll intact | quality rail scrolled to card 2 (scrollLeft 333) -> tap card 2 opens `Market Leaders` -> backdrop close -> 333; reopen -> `page.go_back()` -> sheet closed, still `/m/screeners`, three rails, scrollLeft 333 |
| loading state | with the fetch delayed 600 ms in-page, the sheet shows `Loading names…` at +120 ms, then the 30 rows |
| error state | endpoint 500 -> `Could not load. Try again, or open the full table below.`, footer link still `/m/screeners?screen=12` |
| full table page | `/m/screeners?screen=12` still renders 30 table rows, title `Quality Compounders` (untouched) |
| page errors | none |

Screenshots looked at (VISUAL_VERIFY_GATE_V1): `cc2157_sheet_30_dark.png`, `cc2157_sheet_30_light.png`
(sheet over the dimmed page, handle, title, `30 names · Last run 16 Sep 2026`, rule line, rows with symbol /
sector · mcap on the left and GVM + G·V·M on the right, brand-coloured GVMs, footer link),
`cc2157_sheet_1_dark.png` (one row, no filler), `cc2157_sheet_error_dark.png` (the error line inside the sheet).

## 3. Live check after deploy

- https://scorr.in/m/screeners -- tap Quality Compounders: sheet with 30 names; tap the backdrop: closes;
  Android back with the sheet open: closes the sheet, stays on the page
- https://scorr.in/m/screeners?screen=12 -- unchanged full table
