# cc#2107 — V8 Open cards: mockup reference (items 1-2, no action) + sort-button clipping investigation (item 3)

## Items 1 and 2 — confirmed reference-only, no action taken
Read `reports/CC2107_v8_open_mockup_reference.html` and diffed it against the real, committed
`posRow()`/`mkTc()`/`mkFiredChips()`/`trkLabels()`/`kpiWell()` in `mobile/v8.html`. The card's own
framing is correct: the position-card layout and the Unrealised/Long/Short stat row already match
the mockup almost line for line — confirmed by reading the actual functions, not assumed from the
card's own evidence section. No change made for either item, per the card's own instruction.

## Item 3 — sort-button/chip-row clipping

### What was confirmed real (same class of bug as cc#2106, found the same way)
`.sort-btn` is a bare `<button>` (`<button type="button" class="sort-btn" ...>⇅</button>`).
`mobile.css`'s own locked `MOBILE_UX_STANDARD_V1` (cc#336) — the exact rule cc#2106 traced the
V8 chart-icon bug to — matches every bare `button` at mobile widths and sets
`min-width`/`min-height:var(--mux-tap)` (44px). `min-width`/`min-height` are a floor on
`width`/`height` regardless of selector specificity, so `.sort-btn`'s own explicit `width:32px;
height:32px` was silently overridden. **Confirmed by measurement, not assumption**: rendered the
real `renderFilters()` output against the real, complete 6-stylesheet cascade (the same assembled
cascade cc#2106 built and verified) with the real open-book basket data (`v8_paper_positions`,
`status='OPEN'`: LONG buy_reversal=4, SHORT sell_momentum=12, SHORT sell_reversal=2 — 18 real
positions, 6 real chips including "Buy Rev 4", matching the founder's own screenshot evidence of a
truncated "Buy Rev..." chip) — `.sort-btn` measured **44×44px**, not 32×32px, before this fix.

### What was NOT reproducible, stated plainly per the card's own explicit allowance
The reported symptom is severe — "the sort button visibly cut in half at the screen edge... the
last filter chip... truncated with no visible way to reach it." Tested this directly: with the
real 6-chip data, at every realistic phone width from 320px (iPhone SE, the narrowest common
target) up through 412px, **the sort button's right edge stayed inside the viewport in every
run, including with the pre-fix 44px-oversized button.** Stress-tested further with 6 synthetic
baskets / 30 rows (double the real chip count) at 360px — still no clipping. The mechanism
responsible, `#open-filter-bar .filter-row1 .filters{flex:1;min-width:0}` + `#v8p .filters{
overflow-x:auto}` (already on file, untouched by this card), is doing exactly what it is designed
to do: absorb any amount of chip-row overflow into its own internal horizontal scroll rather than
pushing its `flex:none` sibling off-screen. This is not a theory — it is what got measured, at the
narrowest realistic width and under a stress load, both before and after this fix.

**Conclusion, stated honestly rather than forced**: there is one real, confirmed, fixed defect
(`.sort-btn`'s 44px oversizing) that is real and worth fixing regardless, but it does not by
itself explain the full clipping severity in the founder's screenshot. The most likely remaining
explanations — none of which an isolated component test can confirm from here — are the ones the
card's own item 3 already named: a stale/cached client build, or a page-level interaction (some
other element affecting the row's available width) that only shows up on the full live page. CC is
network-blocked from scorr.in and cannot check either directly.

## Fix applied
`#open-filter-bar .sort-btn` gains `min-width:0;min-height:0` (wins back over the locked floor via
ordinary specificity — same two properties competing on the same element) restoring its real
32×32px size, plus an invisible `::before` hit-zone (`var(--mux-tap)`, 44px, centered on the
button) so it keeps meeting the site's own locked accessibility standard without visually growing
— the identical pattern cc#2106 used for `.kw-c`, for the identical reason.

## Verification
- `node --check` clean on the extracted inline scripts (CSS-only change).
- Theme ratchets computed directly against `origin/main`: fallback 41→41, raw 179→179, both delta 0.
- `git diff --stat`: only `mobile/v8.html` changed.
- **Real-browser measurement (Playwright) against the real, complete cascade**, real basket data,
  4 realistic widths (320/360/390/412px) plus a stress case — **21/21 assertions pass**: `.sort-btn`
  computed width/height is 32px at every width (was 44px); the button's right edge never exceeds
  the viewport, before or after the fix, real data or stress data; all 6 real chips render with
  the real "Sell Rev 2" (sell_reversal, n=2) as the last one; the 44px invisible hit-zone is
  present and genuinely paints nothing.

## What did NOT change
`posRow()`/`mkTc()`/`mkFiredChips()`/`trkLabels()`/`kpiWell()`, the 3-pane swipe deck
(`posTrack`/`posDots`), `.chip`'s own CSS (its `min-height:32px` override already existed before
this card and was already correct; its `min-width` is also floored to 44px by the same locked rule
but every real chip's own content already exceeds that width today, so it is currently a no-op —
left alone rather than fixed pre-emptively for a case that isn't happening). cc#2106's own
chart-glyph fix — separate task, not duplicated here.

## Live check still needed (Arpit)
CC is network-blocked from scorr.in. After this deploys, please confirm with a fresh screenshot
whether the sort button and the last filter chip are now both fully visible and reachable. If the
clipping is fully gone, this card is done. If it persists even with the button now at its real
32px, that confirms something else — outside what CC could see or reproduce from code and an
isolated render — is still involved, and it would need a fresh screenshot plus (if possible) a
hard-refresh check to rule out a stale client cache before the next investigation step.
