# cc#2159 -- Screeners app PUSH 4/5: the Custom Screener view (buttons only, live count, names in the sheet)

`/m/screeners?view=custom` -- a view inside the existing route, so no NAV change (NAV-COMPLETE untouched,
nothing mirrored). Page only (`mobile/screeners.html`); `custom_screener_app.py` consumed as shipped in
cc#2158, no payload change needed. The web `/screeners#custom` builder is untouched, just no longer linked
from the app.

## 1. What the view does

- **Header**: title `Custom screener`, back arrow -> `/m/screeners`. As-of line `Scored 16 Sep 2026 · EOD
  only · 1,773 names` from `/meta`; when `as_of.invest < as_of.gvm` it appends `Investment score is one
  day behind` (measured, never assumed; today the dates match so it does not show).
- **Ten rows** in registry order inside one panel card, hairline-separated: label (type-11, muted,
  uppercase), the rule in plain words (type-10), then <= 3 chips. Chip = 44px min-height button,
  `aria-pressed`, multi-select toggle, label over its `alone` count (`Good` / `366`). On-state = the app's
  chip idiom (16% brand tint, brand border, brand text) -- readable in both themes.
- **Sticky footer** above the bottom nav (its `bottom` = the nav's measured height, z 21): left `32 names
  match` (live), `Clear`, `Show names` (disabled at 0). Every tap -> `/run` after a 250 ms debounce with the
  previous fetch aborted; the number dims while loading and never blanks. Nothing selected -> `0 names
  match · pick at least one filter` (the endpoint's 1,773 is not shown as a match).
- **Excluded line**: when `/run` returns `excluded_no_technicals > 0` a dashed line under the panel reads
  `N names have no technicals today and are left out of trend / 52-week / 1-year filters.`; it disappears
  when the technicals filters are off.
- **Show names** opens the push-2 sheet with the rows `/run` already returned (no second fetch): title
  `Your screen`, meta line = applied filters in words (`Size: Large / Mid · GVM rating: Good · Momentum:
  Strong · Investment score: High`), the same row renderer plus two extras on the right (`Invest 8.96 ·
  Strong buy · 1-yr +16.0%`), a plain footer `EOD basis · GVM 16 Sep 2026 · Investment 16 Sep 2026 ·
  Technicals 17 Sep 2026` (+ the excluded line when > 0, + `Showing the first 300 of N.` when capped). No
  full-table link on this sheet.
- **Memory**: the chip selection is kept in `localStorage` `scorr_custom_screen_v1` as a per-viewer
  convenience (restored on open, wiped by Clear, never treated as a saved screen).
- **Entry**: the list view's Build-your-own card now points at `?view=custom`. It is rebuilt as a
  full-width `sk-card` (icon, `Custom screener`, `Ten filters, three buttons each. Tap what you want.`,
  `Live count as you tap · the names open right here`), which also removes the mono-font leak the old
  `.sc` card had from `mobile_app.css`'s bare `.sc` rule; the unused `.grid/.sc/.wide` CSS is gone.
- Shared-sheet additions: `meta.text` (a ready meta line) and `meta.foot` (a plain footer block) on
  `renderNamesSheet`; `nameRow` prints the invest extras only when the row carries `invest_score`.
  A `[hidden]` rule for the sheet footer pieces was needed: the author `display:flex` beat the `hidden`
  attribute, so the full-table link had stayed visible on the custom sheet -- caught on the screenshot,
  fixed, asserted on computed display.

## 2. Checks -- `scratchpad/cc2159_test.py`, `=== ALL cc#2159 CHECKS PASS ===`

375px and 390px, dark (goldnight) and light (aquawhite), touch. `/meta` and `/run` stubs are the REAL
module functions run over REAL production rows (today's 30 alone counts and dates; the demo combo's 32
real rows). The trend stub returns `excluded_no_technicals: 3` as a SAMPLE to exercise the line (live is 0
today).

| Check | Result |
|---|---|
| list view card | href `/m/screeners?view=custom`, full width (343 / 358), Sora font (no `.sc` leak), zero `.grid/.sc` left |
| custom view | title, back arrow to `/m/screeners`; 10 rows, 30 chips, min height 44, every row's chips on one line, all inside the viewport, `scrollWidth == innerWidth` |
| chips | first alone count `100`, `aria-pressed` follows the on-state |
| nothing selected | `0` + `names match · pick at least one filter`, Show names disabled |
| footer | bottom edge = the nav's top (58px), z-index 21 |
| as-of | `Scored 16 Sep 2026 · EOD only · 1,773 names`, no warning (dates equal) |
| demo taps (Large, Mid, Good, Strong, High) | count dims then reads `32`, Show names enabled, ONE `/run` request with `size=large,mid&gvm=good&momentum=strong&invest=high`, selection in localStorage |
| Show names | sheet 32 rows, `Your screen`, meta line in words, first row GLENMARK 8.52 with `Invest 8.96 · Strong buy · 1-yr +16.0%` and `Mid`, EOD footer line, full-table link hidden (computed display none) |
| trend Uptrend on | count `486`, the excluded line with N = 3; the sheet footer carries it plus `Showing the first 5 of 486.` |
| trend off | line gone, count back to `32` |
| reload | chips restored from localStorage, count re-fetched (`32`) |
| Clear | all chips off, `0`, Show names disabled, localStorage key removed |
| back arrow | list view, title `Screeners`, no footer left behind |
| page errors | none |

Regression: the cc#2157 sheet harness still passes; the cc#2156 rail harness passes once its old
`/screeners#custom` assertion accepts the new card target.

Screenshots looked at (VISUAL_VERIFY_GATE_V1): `cc2159_list_dark_375.png`, `cc2159_custom_goldnight_375.png`
and `cc2159_custom_aquawhite_375.png` (full page: ten rows, three chips per row with counts, the five chosen
chips tinted, footer `32 names match · Clear · Show names` sitting on the nav), `cc2159_sheet_dark_375.png`
(the sheet with the extras and the EOD footer line, no link).

## 3. Live check after deploy

- https://scorr.in/m/screeners -- the Build-your-own card opens https://scorr.in/m/screeners?view=custom
- tap Large + Mid, GVM Good, Momentum Strong, Investment High -> `32 names match`; Show names -> the sheet
- tap Trend Uptrend -> no excluded line today (technicals cover all 1,773)
