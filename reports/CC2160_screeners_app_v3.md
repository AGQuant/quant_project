# cc#2160 -- Screeners app PUSH 5/5: shareable custom-screen link, edge states, dead code out, closing report

Closing push of SCREENERS_APP_V3 (Fable-owned sprint, founder handover 17-Sep-2026). Five pushes, all on
`main` the same day: cc#2156 `2115984`, cc#2157 `28faa84`, cc#2158 `25d0097`, cc#2159 `43f663c`, and this one.

## 1. This push

- **URL state** (`mobile/screeners.html`): the chip selection is mirrored into the query string on every
  change with `history.replaceState` -- `/m/screeners?view=custom&size=small&valuation=reasonable&invest=high&trend=up`
  uses the same keys `/run` takes. Opening such a link pre-selects the chips and runs the count. A link's
  filters win; `localStorage` (push 4) restores only when the URL carries none, and the restored selection is
  written back into the URL so the address bar is always shareable. Clear empties the query.
- **Share** button next to Clear (enabled once something is picked): copies the URL with
  `navigator.clipboard.writeText`, shows `Link copied` for 1.5 s; when the clipboard refuses or does not
  exist it falls back to a `prompt` holding the URL.
- **Edge states, measured**: count 0 with filters on -> `0` + `No name passes all of these today`, Show
  names disabled; a button whose `alone` is 0 today still renders its chip with `0`; a failed `/meta`
  (500 or an error body) -> `Could not load the filters.` with a `Try again` button that reloads the view;
  never a blank panel.
- **Home tile**: `mobile/home.html` G_GROUPS Analytics tile `{href:'/m/screeners', label:'Screeners', svg}`
  has NO subtitle field (`gtileR1` renders icon + label only), so per the spec nothing changes there. No
  tile added or moved (HOME_GRID_R1, cc#1921).
- **Dead code**: the old 3-column `.grid/.sc/.wide` CSS and the pad-to-3 hidden-card loop were already
  removed by cc#2156/cc#2159 (the stale header comment is corrected here). The `"A TC score column is being
  added under cc#1881."` footnote string in `screeners_app_mobile.py`'s cc#1899 payload is deleted (founder
  ruling: TC has no place here); no page read `custom_builder.footnote` (grep). `grep "TC score"
  screeners_app_mobile.py` -> 0.

## 2. Checks -- `scratchpad/cc2160_test.py`, `=== ALL cc#2160 CHECKS PASS ===` (375px, dark + light, touch)

Stubs: `/meta` = the real module over today's counts (with one button's `alone` forced to 0 as a SAMPLE);
`/run` answers the shared-link combo with the LIVE count 88 (cc#2158's production run) and sample rows.

| Check | Result |
|---|---|
| cold open of the shared link (no localStorage) | four chips on (size small, valuation reasonable, invest high, trend up), count `88`, URL unchanged, Show names + Share enabled |
| alone = 0 | the chip renders with `0`, 30 chips present |
| tap GVM Good | URL becomes `...&trend=up&gvm=good` (replaceState) |
| Share | clipboard gets exactly `origin + current URL`, button reads `Link copied` then `Share` |
| copied link in a fresh page | reopens to the same five chips |
| clipboard refused | `prompt` fallback carries the URL |
| Clear | URL back to `?view=custom`, Share disabled, count 0 |
| sector Weak + GVM Good + Invest Low | `0` + `No name passes all of these today`, Show names disabled, Share allowed |
| `?view=custom` with a saved selection | chips restored from localStorage and the URL filled in |
| `/meta` 500 | `Could not load the filters.` + `Try again`; retry renders the 30 chips |
| demo link `size=large,mid&gvm=good&momentum=strong&invest=high` | `32`, five chips, sheet with 32 rows |
| page errors | none |

Regression on the final page: cc#2159, cc#2157 and cc#2156 harnesses all green.

## 3. The screenshot set (375px, dark = goldnight, light = aquawhite) -- all looked at

| Screen | Files | What is on it |
|---|---|---|
| list | `cc2160_list_goldnight.png`, `cc2160_list_aquawhite.png` | three snap rails, one card each with the next card peeking, three preview chips, dot pagers |
| sheet from a rail card | `cc2160_sheet_goldnight.png`, `cc2160_sheet_aquawhite.png` | Quality Compounders, 30 names, rule line, GVM + G/V/M per row, `Full table, sortable` |
| custom | `cc2160_custom_goldnight.png`, `cc2160_custom_aquawhite.png` | ten rows, three chips each with counts, the demo's five chips tinted, footer `32 names match · Clear · Share · Show names` |
| custom with sheet | `cc2160_custom_sheet_goldnight.png`, `cc2160_custom_sheet_aquawhite.png` | `Your screen · 32 names · Size: Large / Mid · GVM rating: Good · Momentum: Strong · Investment score: High`, rows with `Invest 8.96 · Strong buy · 1-yr +16.0%`, EOD basis footer |
| shared link open | `cc2160_shared_goldnight.png`, `cc2160_shared_aquawhite.png` | the four chips from the URL on, `88 names match`, the Sector rating Good chip showing `0` (sample) |
| meta failure | `cc2160_meta_fail_dark.png` | `Could not load the filters.` with `Try again` |

## 4. The three demo counts (production, 17-Sep-2026, cc#2158's exact SQL)

| Combo | Count |
|---|---|
| Large + Mid x GVM Good x Momentum Strong x Investment High | 32 |
| Small & micro x Valuation Reasonable x Investment High x Uptrend | 88 |
| nothing selected | 1,773 = the universe (rows `[]`, `pick at least one filter`) |

`theme_validate mobile/screeners.html`: raw 0 / baseline 0, 0 new `var(--x, literal)` fallbacks (grep 0).

## 5. What the founder should look at

Open https://scorr.in/m/screeners on the phone: swipe a rail, tap a card -- the names come up in a sheet
without leaving the page. Then the Build-your-own card: tap Large, Mid, GVM Good, Momentum Strong,
Investment High and watch the footer count settle on 32 (today's figure); Show names opens the same
sheet with Invest score and 1-yr return on every row. Tap Share and send yourself the link -- opening it
reproduces the screen. Two things worth a deliberate look: the chip on-state (a brand tint, not a solid
fill) in both themes, and the light-theme key-metrics boxes at the top of the list view, which are the
known `mobile_app.css` bare-`.v` leak filed as cc#2166 and not part of this sprint.

## 6. Out of scope, on purpose

Saved screens (no table), alerts on a custom screen, web parity -- separate cards after the review.
