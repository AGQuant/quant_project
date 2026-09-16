# cc#2106 — V8 Positions deck: chart-glyph button forced to 44px by a site-wide tap-target rule

## Diagnosis (item 1) — genuine CSS regression, not a stale build

The card's own instruction was to confirm the live cause before assuming one. Read every single
stylesheet `/m/v8` actually loads — `mobile_app.css` (`mobile_endpoints.py`'s `MOBILE_CSS`,
123KB), `scorr_themes.css`, `scorr_appshell.css`, `mobile.css` (`pwa_endpoints.py`'s own separate
`MOBILE_CSS`, 25.8KB), `scorr_theme_r5.css`, `theme_mobile.css`, plus every JS-injected style block
— and `mobile/v8.html`'s own `#v8p .kw`/`#v8p .kw-c` rule in isolation, which reads entirely
correct on its own (`position:relative` + `position:absolute;top:4px;right:4px;width:18px;
height:18px`). A text grep for `.kw-c`/`.kw{`/`button{` found nothing competing anywhere.

**That grep was the wrong tool for this bug — confirmed by asking the real browser instead.**
Rendered `kpiWell()`'s real, unmodified output against the real, complete cascade (all six
stylesheets, in a real Chromium instance) and asked the browser itself which CSS rules match the
button (`el.matches(rule.selectorText)`, walking into `@media` blocks). **The button measured
44px × 44px, not 18px** — and the actual matching rule was `mobile.css`'s own locked
`MOBILE_UX_STANDARD_V1` (cc#336) contract:
```css
@media(max-width:767px){
  button,a.btn,.btn,.chip,.tab,.toggle,select,
  th[onclick],[role=button],.week-nav button,.book-toggle{min-height:var(--mux-tap);}
  button,a.btn,.btn,.chip{min-width:var(--mux-tap);}   /* --mux-tap:44px */
}
```
`.kw-c` **is** a bare `<button>`, so it matches this rule too. `min-width`/`min-height` act as a
**floor** on `width`/`height` — that relationship is not decided by selector specificity at all
(they are different properties that combine via `max()`), which is exactly why a text search for
a competing `.kw-c` declaration never finds it: there isn't one. The only thing catching this
button is a bare, unscoped `button` selector in a rule that predates cc#2097 by a long way and was
never meant to touch it.

**Verdict, stated plainly per the card's own instruction: a genuine CSS regression, introduced the
moment cc#2097 gave `.kw-c` a real `<button>` element without an escape hatch from this pre-existing,
locked, site-wide rule.** Not a stale/cached build — reproduced from this repo's current, freshly-
pushed code, in a fresh browser context with no cache involved.

## Fix (item 2)
`#v8p .kw{position:relative}` is untouched. `#v8p .kw-c` gains `min-width:0;min-height:0`, which
wins back over `mobile.css`'s floor via ordinary specificity (these ARE the same two properties
now competing on the same element, so cascade order/specificity applies normally) — the button
renders at its real, designed 18×18px again.

**Also addressed, not asked for but inseparable from the root cause:** `MOBILE_UX_STANDARD_V1` is
explicitly "LOCKED... every future UI task MUST satisfy... 44px tap targets" — shrinking `.kw-c`'s
`min-width`/`min-height` to 0 opts it out of that accessibility floor entirely, which would be
quietly reintroducing the exact class of gap that locked rule exists to close, for a brand new
button, the moment after diagnosing that the rule's own reach is what broke it. Added
`#v8p .kw-c::before{content:'';position:absolute;top:50%;left:50%;width:var(--mux-tap);
height:var(--mux-tap);transform:translate(-50%,-50%)}` — an invisible pseudo-element centered on
the real 18px circle, restoring a genuinely tappable 44px hit-zone (`var(--mux-tap)`, the same
variable the locked rule itself defines, so the two never drift apart) without growing anything
visible. Confirmed harmless to the box's own whole-area tap: `kpiWell(..., {chartBtn:true, chart:
'all', ...})` already puts the SAME `data-kw-chart` on the whole `.kw` box (cc#2097's own "every kpi
box becomes a tap target for its chart"), so wherever the invisible hit-zone reaches into the label
below it, a tap there already resolved to the identical `dlChartOpen()` call before this card
existed — nothing about what a tap resolves to changes.

## Verification
- `node --check` clean on the extracted inline scripts (CSS-only change; script content is
  byte-identical, checked as a sanity confirmation, not because logic was touched).
- Theme ratchets computed directly against `origin/main`: fallback 41→41, raw 179→179, both delta
  0 (this change touches `min-width`/`min-height`/`::before` geometry, none of which the ratchet's
  `FAMILY` regex even inspects — no color or themeable-length literal introduced either way).
- `git diff --stat`: only `mobile/v8.html` changed.
- **Real-browser measurement (Playwright), against the ACTUAL, complete CSS cascade this page
  loads** — not a synthetic snippet — both before and after the fix, to prove the diagnosis and
  then the fix, in that order: **18/18 assertions pass** post-fix, including: the button's
  computed `width`/`height` is exactly `18px` (was `44px`, reproduced first); it sits in the box's
  top-right corner; it does **not** overlap the label's or the value's *actual rendered text*
  (measured via `Range.getBoundingClientRect()` on the text content itself — the label `<div>`'s
  own box spans the row's full width by ordinary block layout regardless of how short its text is,
  so a naive box-vs-box check would have produced a false failure here; this is the same technique
  `scorr_position_row.js`'s own `trkFixLabels()` already uses in this codebase, for the identical
  reason); the LONG and SHORT boxes still carry no chart button at all; the invisible `::before`
  hit-zone is exactly `44px × 44px` and genuinely paints nothing (`background: transparent`,
  `border: none`).
- **Functionality**: `data-kw-chart`/`data-kw-chart-kind`/`data-kw-chart-info` on the button are
  untouched — this card made zero changes to any `<script>` content, confirmed by diff and by
  `node --check` against the unmodified extracted scripts, so `dlChartOpen`'s wiring cannot have
  changed.

## Live check still needed (Arpit) — per the card's own instruction
CC is network-blocked from scorr.in and cannot self-verify visually. Please confirm with a fresh
screenshot of the UNREALISED box after this deploys: the chart icon should sit as a small circle
in the box's top-right corner with no overlap on "UNREALISED" or the rupee figure beneath it, and
tapping it (or tapping anywhere else in the box) should still open the same P&L chart as before.

## What did NOT change
`CHART_ICON_SVG`'s own glyph and `dlChartOpen()`'s chart-opening behaviour — untouched.
`mobile.css`'s locked `MOBILE_UX_STANDARD_V1` rule itself — untouched; every OTHER button on the
site keeps its real 44px minimum exactly as that contract requires, only `.kw-c` now opts out (with
its own invisible replacement) since it is a small decorative/informational affordance the box's
own whole-area tap already backs up, not a lone or hard-to-reach control. The swipe-deck mechanics
and cc#2097's "one chart button per pane, on the leading box only" decision — untouched, and
re-confirmed still true (LONG/SHORT carry no button).
