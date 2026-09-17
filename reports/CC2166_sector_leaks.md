# cc#2166 — /m/sector: the last two bare-selector leaks renamed (themes rail, detail values)

Built 17-Sep-2026; the landing time is in the task row. Page only: `mobile/sector.html`. `mobile_endpoints.MOBILE_CSS` and `sector_app_mobile.py` untouched (do_not_touch).

## What changed

- Emerging themes rail: `.tc .rk` → `.tc .si-rk`, `.tc .tg` → `.tc .si-tg` (CSS and the render string). The shared `.tg{min-height:48px;display:flex;…font-weight:800}` and `.rk{IBM Plex Mono…}` rules no longer reach the card.
- Detail view (`/m/sector?seg=`): `.pil` → `.si-pil` (its `.r/.l/.bar` children keep their names, scoped under `.si-pil`), the pillar value `.v` → `.si-pv` (in `bar()`), the strip's `.k/.v` → `.si-sk/.si-sv`. The shared `.v` boxed value pill (border, 15/19 px padding, `--panel2` fill, `::before` bar) and the bare `.pil` margin no longer apply.
- Values carry `color:var(--ink)` explicitly (tokens only; theme_validate stays clean).

## Harness (Playwright, real Chromium, `scratchpad/cc2166_test.py`) — ALL PASS at 360 and 390 px, goldnight + aquawhite

List view on the cc#2155 production fixture; detail view on a segment payload in the endpoint's shape.

- Rail card tagline: computed `min-height 0px`, `display block`, weight 400, no 13 px padding (was 48 px / flex / 800 from the leak); rank label font Sora (not IBM Plex Mono), weight 800; no `.tc .tg` / `.tc .rk` left; no sideways overflow.
- Detail: the 3 pillar values and the 4 strip values have no border, no padding, no fill and no `::before` bar; the pillar block keeps its 10 px margin; values 7.1 / 6.8 / 8.2 and 13 · ₹24.0L Cr · +1.2% · +4.5%; no leftover `.pil` / `.v` / `.k` selectors in the detail; no page errors.

**Screenshots looked at** (`scratchpad/cc2166_*.png`): `390_dark_rail` — COLD SEGMENTS ladder, then EMERGING THEMES: "THEME 1 · Pharma: India as World Pharmacy 2.0" with a plain two-line tagline and the three names, the second card peeking in. `360_light_detail` — PRIVATE BANKS: the scorecard with 7.42, the G/V/M bars with plain right-aligned values, the four-cell strip (NAMES 13 · MCAP ₹24.0L Cr · INST. FLOW +1.2% · QOQ PROFIT +4.5%) with no boxed pills, then the brief cards.

## Item 3 — the same bare names used as page-local classes elsewhere (a list, not a fix)

Grep over the other 29 `mobile/*.html` for `class="…"` carrying `.c .v .g .gv .rk .tg .more .big .pil .bar .row .tag .k .s .l` (counts of uses):

| Page | bare names in use |
|---|---|
| health.html | v 30, k 17, row 5, s 4, tag 2, c 1, l 1, bar 1, big 1, pil 1, g 1 |
| mf.html | v 12, k 8, row 3, c 2, more 1, big 1, pil 1, l 1, bar 1, g 1 (fund detail view; the list is `mf-` since cc#2184) |
| qbbuilder.html | v 12, k 6, row 4, c 1, big 1, tag 1 |
| results.html | k 8, v 8, s 2, g 1, big 1 |
| qb.html | v 4, k 3, more 2, big 2, l 1, bar 1, pil 1, s 1, g 1 (detail view; the landing footer is `qb-` since cc#2189) |
| fpc.html | k 5, v 3, big 2, more 2, tg 1 (steps 1–2; the output sheet is `fpc-` since cc#2190) |
| gvm.html | tag 8 |
| myportfolio.html | s 6, k 5, v 5, row 1 |
| home.html | c 6, s 3, k 3, l 2, v 2, row 1 |
| positions.html | k 4, v 4, tag 4, s 1, c 1 |
| holdings.html | k 3, v 3, tag 2, s 1, c 1 |
| digest.html | tag 4, c 3, s 2 |
| intel.html | tag 4, s 1, c 1 |
| screeners.html | k 4, v 3 |
| trade_wall.html | v 3, s 2, c 1, row 1 |
| myalerts.html | k 3, v 3, s 2 |
| v8.html | c 3, tag 3, l 2 |
| invscan.html | k 3, big 1 |
| options.html | k 6 |
| tcscan.html | tag 2, row 1, v 1, c 1 |
| learn.html | tag 2, c 1, more 1 |
| check.html | k 2, v 2 |
| models.html | s 1, tag 1 |
| alerts.html | l 1 |

A use is only a defect where the shared rule actually restyles the element (`.v`, `.tg`, `.rk`, `.pil`, `.c` are the ones with heavy shared rules; `.k`, `.s`, `.l`, `.tag` less so). No cards filed from this list today — the founder-visible pages fixed this week (mf, qb, fpc, results, invscan, tcscan, options) already use prefixed names on the parts he reviewed; Fable to pick which of the rest get a card.

## Live check (Fable — the sandbox cannot reach scorr.in)

`https://scorr.in/m/sector` → the theme cards' taglines sit tight under the name (no tall boxed line), rank labels in the page font; tap a segment → the G/V/M values and the four-cell strip are plain values, no pills.
