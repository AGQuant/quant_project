# cc#2117 — Custom Screen Builder picker chips: −25%

## COLLISION, stated first
cc#2079 (founder, 14-Sep) bumped this exact font 10.5px → 14.5px ("+2px per notch, +4px total"). A
25% cut on that font gives **10.9px — essentially back to the 10.5px cc#2079 replaced two days
earlier.** Implemented exactly as specced, and said plainly here so it gets a glass check: if the
bump had overshot on a wide screen this is what the founder wants; if he meant something else, it is
a one-line correction. The comment block above the rule now records the whole sequence
(10.5 → 14.5 → 10.9) instead of a bare number under a stale rationale.

## Interpretation taken (as the card itself directed)
"Button size", not font size → the chip's footprint: **font 14.5px → 10.9px, padding 5px 12px →
4px 9px, border-radius 8px → 6px; margin 0 6px 6px 0 unchanged** (inter-chip spacing, not chip size —
rows are relatively more generous now, the intended effect). Padding-only was rejected: it would
squeeze 14.5px text against the border and break the pill shape cc#2079 verified.

## Diff
ONE CSS rule + the comment above it, `v8_dashboard.html` (`#pane-index .csbchip`, ~line 982).
`.csbchip.on`, `.csblb`, `.csbrow` untouched. Zero JS lines in the diff (checked: no `function`/`=>`
in any changed line). Web only — `csbchip`/`csbrow`/"Custom Screen Builder" have zero hits in
`mobile/home.html`, `mobile_ext.py`, `mobile_endpoints.py`, so there is no app mirror to sync and none
was created.

## Measured, real page pieces (Playwright, both rules rendered side by side)
Harness = the page's own three rules (`.csblb`/`.csbrow`/`.csbchip` + `.on`) and its own
`BU_STATES` / `CSB_BASIS_BANDS` / `CSB_VOLR_BANDS` arrays and `csbChip/csbRow/csbBuildChips/
csbFlowChips/csbBandChips` renderers, sliced verbatim from `v8_dashboard.html` by marker, on the
real `scorr_web_tokens.css` (dark and `html[data-theme=light]`). Before = the cc#2079 rule; after =
this one.

| | before | after |
|---|---|---|
| computed font / padding / radius | 14.5px / 5px 12px / 8px | **10.9px / 4px 9px / 6px** |
| chip height (all four rows, one class) | 28px | **23px** (×0.82 — the 1px borders and line-height rounding do not scale) |
| Buildup row total width (5 chips) | 628px | **474px** (×0.755) |
| chip area | — | **×0.62** |
| card height, desktop 1280px | 278px | 258px |
| card height, 375px | 414px | **287px** |
| desktop lines per row (Buildup/Orderflow/Basis/VolR) | 1/1/1/1 | 1/1/1/1 |
| **375px wrap points** (lines per row) | **3/1/2/2** | **2/1/1/1** |
| longest labels wrap or clip inside their pill? | no | **no** — Long Build Up, Short Covering, Short Buildup, Long Unwinding, Beyond -1%, Below 1.0x all one line, no overflow |
| ON chip (blue fill) contrast | dark 5.12:1 · light 4.76:1 | **unchanged** 5.12 / 4.76 (bold, well above 3:1) |

Wrap points described (VISUAL_VERIFY_GATE_V1, screenshots looked at at both widths): at 375px the
Buildup row now breaks after "Short Covering" (Any · Long Build Up · Short Covering / Short Buildup
· Long Unwinding) instead of after "Long Build Up" with a third line for "Long Unwinding"; Basis band
and Volume R band each fit on one line where "Beyond -1%" and "1.5-2.0x · 2.0x+" used to spill to a
second. All four rows shrink together (one shared class, confirmed: identical chip height in every
row). Adjacent chips stay clearly separate — the 6px margin is now proportionally larger.

## Not touched
`.ticks`, `.ib`, `.oiib`, the marker chips, basket filter chips; the builder's logic, filters,
TC/100 column, sorting, `CSB_STORE_KEY` persistence, results table; the MATCH badge and the (i)
button.
