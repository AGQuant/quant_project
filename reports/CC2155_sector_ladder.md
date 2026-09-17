# cc#2155 -- /m/sector ladder: the boxed, cut-off delta; top 5 per band; View all; score/delta split

Founder screenshot 17-Sep 11:09 IST (light theme, /m/sector): every ladder row's right-side GVM/delta
box rendered as a bordered card that ran off the right edge. Display-only fix in `mobile/sector.html`.
No API, engine or scoring change.

## 1. Root cause -- two things, both real, both fixed

**(a) A shared-sheet class collision.** `/static/mobile_app.css` is not a file; it is the `MOBILE_CSS`
string in `mobile_endpoints.py`, and it carries BARE selectors that are cards on other app pages:

| Rule in `mobile_endpoints.py` (MOBILE_CSS) | Line | What it did to this page |
|---|---|---|
| `.c{position:relative;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:13px 13px 13px 17px;margin-bottom:9px;...}` + `.c::before{...width:3px...}` | 1122, 1124 | the delta `<div class="c">` became a bordered, padded card with a 3px left bar |
| `.v{position:relative;background:var(--panel2);border:1px solid var(--line2);border-radius:15px;padding:15px 15px 12px 19px;...}` + `.v::before` | 1305, 1307 | the key-metrics values (`.km .v`: 126 / 4 / 93 / 29) became dark bordered boxes |
| `.gv{font-size:11.5px;color:var(--mut);line-height:1.5}` | 1327 | the GVM score read in muted grey, not ink |
| `.rk{font-family:'IBM Plex Mono',...;min-width:22px}` | 1405 | the rank digit took the mono font |
| `.more{font-family:'IBM Plex Mono',...}` | 1848 | the "+N more" line took the mono font |

The page's own rules (`.lr .gv .c{font-size;margin-top;font-weight}`) only set three properties, so
the card's border, padding, margin and `::before` bar cascaded straight through. Same failure class as
cc#1113 and the reason cc#1591 moved the GVM page to `g-*` names. The sheet loads BEFORE the page's
`<style>`, but load order never mattered: the page never set those properties at all.

**(b) The grid column could not shrink.** `.lr{grid-template-columns:22px 1fr 58px}` with a `nowrap`
sub line. In CSS grid `1fr` means `minmax(auto,1fr)`: the middle column's minimum is its content's
min-content width, and the `overflow:hidden` on the child lines does not shrink the parent cell. A long
sub line (`G 6.6 · V 6.4 · M 8.3 · 10 names · DEEPINDS +0.3%`) grew the column past its share and pushed
the 58px score cell out of the row, where `.lad{overflow:hidden}` clipped it. That is the "cut off at
the right edge": rows 1-2 (short lines) fitted, rows 3-5 did not.

Measured on the shipped page (Playwright, real `mobile_app.css` + `scorr_themes.css` + `scorr_appshell.css`,
list payload built from the real 126 `sector_ratings` rows of 16-Sep):

| | 360px | 390px |
|---|---|---|
| rows whose score cell sat past the row's right edge | 7 of 10 (Oil Services & Small, Petrochemicals & Lubricants, Pharma - Formulations, Exchanges & Ratings - Mid, Aluminium & Non Ferrous, Digital Aggregators & E-Commerce, Organic Chemicals - Large) | 1 of 10 |
| delta cell computed style | `border 1px solid`, `padding 13px 13px 13px 17px`, `margin-bottom 9px`, `position relative`, `::before` 3px bar, radius 14px | same |
| key-metrics value computed style | `border 1px`, `padding-left 19px`, `::before` 3px bar | same |
| score colour vs name colour | muted `rgb(140,153,189)` vs ink | same |

## 2. What changed (`mobile/sector.html` only)

- Ladder row inner classes are page-local now: `.si-rk` (rank), `.si-gv` (score block), `.si-n` (score),
  `.si-d` (delta chip). The key-metrics value is `.si-v`. Nothing in `mobile_app.css` matches them.
- `.lr` grid is `22px minmax(0,1fr) auto`: the name column can shrink (ellipsis works), the score column
  sizes to its content and always stays inside the row.
- **Top 5** per size band: `rows.slice(0,5)` (was 10), on All / Large / Mid / Small alike. A band with
  fewer than 5 shows what it has.
- **View all control** replaces the "+N more · All 126 segments, sortable ›" text line: a full-width
  tappable card, `View all 126 segments` + a sub-line `Showing the top 5 of 30 large · sortable table`
  (or `All N shown` when the band has 5 or fewer), chevron on the right. Same href `/m/sector?view=all`.
- **Score and delta separated**: score 17px in ink with tabular digits; delta is its own small chip
  (`▲ +0.23` green / `▼ −0.23` red / `—` when there is no history) on a `var(--hi)` pill under it.
  The header hint `by GVM · Δ since first score` is unchanged.
- Cold segments use the same `ladderRow()`, so they carry the same fix (checked, section 3).
- Contract tokens only, no new literal fallbacks. `theme_validate` on the file: raw 0, baseline 0, clean.

## 3. Checks (harness `scratchpad/cc2155_test.py`, prints `=== ALL cc#2155 CHECKS PASS ===`)

Four runs: light (aquawhite) and dark (goldnight) at 360px and at 390px. Each run first measures the
shipped page (before), then the patched page (after).

- After: delta chip `border 0px`, `padding 2px 5px`, `margin-bottom 0`, `position static`, no `::before`;
  no row's score cell past the row edge; page `scrollWidth == innerWidth` (no horizontal scroll).
- Score colour equals the name colour (ink) in both themes. Rank font is Sora, not mono.
- Rows per tab: All 5 of 126, Large 5 of 30, Mid 5 of 30, Small 5 of 66 (band sizes from the stub's
  avg-mcap ranking, same LARGE<=30 / MID<=60 rule as `sector_endpoints._add_size_class`).
- View all control: href `/m/sector?view=all`, text `View all 126 segments`, sub-line names the band count.
- Cold segments: 5 rows, none boxed, none past the row edge, zero old class names left on the page.
- Key metrics: 4 `.si-v` values, border 0, padding 0, no bar.
- `/m/sector?view=all`: 126 table rows, title `All segments`, untouched.
- No page errors.

Screenshots looked at (VISUAL_VERIFY_GATE_V1): `cc2155_before_light_360.png` (the founder's bug
reproduced: boxed deltas, rows 3-5 cut at the right, dark boxed key-metrics), `cc2155_after_aquawhite_360.png`,
`cc2155_after_goldnight_360.png`, `cc2155_after_dark_390_full.png`: five clean rows, score in ink,
coloured delta chips, the View all card, cold rows in the same style, key-metrics plain numbers.

The stub's `change` values are SAMPLE (deterministic per segment name) -- the real ones come from
`/api/mobile/sector_app/list`; the layout does not depend on the value.

## 4. Found, not fixed (outside this card's scope) -- follow-up card cc#2166

The same bare-selector leak is still live elsewhere on this page:

| Element | Bare rule leaking in | Measured |
|---|---|---|
| Emerging themes rail tagline `.tc .tg` | `.tg{min-height:48px;display:flex;align-items:center;padding:0 13px;font-weight:800;...}` (line 1301) | `min-height 48px, display flex, font-weight 800` |
| Themes rail rank label `.tc .rk` | `.rk` mono font (line 1405) | `IBM Plex Mono` |
| Detail view (`?seg=`) `.pil .v` and `.strip .v` | `.v` card rule (line 1305) | not rendered in this harness; same selector, same effect |
| Detail view `.pil .bar` | `.bar{flex:1;...}` (line 1382) | harmless in a grid, listed for completeness |

The rail is named out of scope on the card and the detail view is on the do-not-touch list, so they are
filed, not fixed.

## 5. Live check after deploy

- https://scorr.in/m/sector -- light and dark: five rows per tab, no boxed delta, `View all 126 segments` card
- https://scorr.in/m/sector?view=all -- unchanged sortable table
