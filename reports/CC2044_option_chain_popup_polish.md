# cc#2044 — option chain popup: pop-out detail panel, more spacing, drop the Black-Scholes line

Founder screenshot 13-Sep-2026 of the live NIFTY option chain popup (cc#2034's own detail panel):
tapping a strike appended the CALL/PUT detail below the 21-row grid and the legend, inside the same
scrolling sheet — so it read as more list content to scroll down to, not something that popped out.
Also asked for more breathing room in the grid/detail panel, and for the Black-Scholes/realised-vol
commentary line to be dropped from the detail panel entirely.

Confirmed before touching anything: `_dcRenderChainGrid`/`_dcChainDetailHtml` in
`scorr_cockpit_card.js` are the SAME functions behind both surfaces this card names — the D-cockpit's
own section 06 and the standalone chain-only popup Home opens (`ScorrCockpitCard.openChain`) — so one
fix reaches both, same pattern as this session's other shared-component fixes.

## What changed

**Item 1 — pop out, not append-and-scroll.** Chose the card's own option (b): the moment the detail
panel renders, it's brought into view with `scrollIntoView({block:'nearest'})`, and it carries a
visually distinct elevated treatment (a blue-accent border in place of the sheet's usual `--c-bd`
gray, a real box-shadow, and a brief fade/scale-in) so it reads as something that popped up rather
than silent list content. Went with (b) over pinning it sticky/fixed: `.dc-sheet` is a *centered*
modal (`max-height:88vh`, not a full-viewport bottom sheet), so a fixed-position panel would need its
own resize-aware positioning math against the sheet's bounds; `scrollIntoView` needs none and works
identically regardless of how much content sits above the chain (the multi-section D-cockpit) or how
little (the standalone popup). Gated on a small per-symbol `_dcLastPop` guard so an unrelated
re-render with the same strike still selected — tapping the (i) info toggle, say — doesn't replay the
scroll/pop animation; it still fires again if the user closes the panel and reopens the same strike.

**Item 2 — spacing.** Grid cell padding: `5px 6px` / `5px 8px` (row) and `4px 6px` / `4px 8px`
(header) → `9px 11px` / `9px 12px` uniformly, the STRIKE column keeping its original relative extra
horizontal room. Detail-panel field lines (Premium/IV/Fair value/OI/.../Greeks), previously plain
stacked divs with zero margin, now get `margin-bottom:5px` via one scoped rule
(`.dc-detail-pop>div>div`) rather than hand-adding inline margins to eight separate lines.

**Item 3 — drop the Black-Scholes line.** Removed the `Fair (Black-Scholes, σ=20d realised vol) ...`
div from `_dcChainDetailHtml`'s `leg()` output. `o.fair` stays exactly as computed server-side in the
API payload — display-only removal, per the card's own instruction. cc#1859's own IVP fair value/tag
line (the concrete rupee number the founder does want) is untouched. Two stale comments describing
the now-removed line as intentionally kept were updated so they don't mislead a future reader.

**Untouched, per `do_not_touch`:** the chain-grid data source (`option_chain_grid.py`,
`deriv_metrics.py`), cc#1859's fair_value/tag computation, the Greeks maths (cc#2034), wall/max-pain
row colours, the (i) info toggle, the legend, and the D-cockpit's sections 01-05. The pre-fetch
placeholder copy elsewhere in the file (shown before any fetch, unrelated to the detail panel) still
says "Black-Scholes fair" — the card's own verify line scopes the removal to `_dcChainDetailHtml`'s
rendered output specifically, so that line was deliberately left alone rather than guessed at.

## Verify

`node --check` clean. Real headless Chromium, the actual `scorr_cockpit_card.js` +
`scorr_card_common.js` loaded unmodified, a 21-strike NIFTY chain-grid fixture (matching the
founder's own screenshot) served through the real `ScorrCockpitCard.openChain('NIFTY')` entry point
at a 400×700 viewport — **17/17 checks pass**:

- The fixture grid is confirmed genuinely taller than `.dc-sheet`'s visible scrollport (842px content
  vs 644px visible) — the "no manual scrolling needed" assertion below is a real test, not trivially
  true because everything already fit.
- Grid cell padding measured via `getComputedStyle` at 9px (was 4-5px).
- Tapping a strike renders `#dcChainDetail` and its bounding rect ends up **fully inside** the sheet's
  visible viewport with no further scrolling — the exact founder complaint, verified geometrically,
  not just "an element exists somewhere in the DOM."
- The panel carries the `dc-detail-pop` class with a real box-shadow and the blue accent border.
- `"Black-Scholes"` and `"realised vol"` do not appear anywhere in the detail panel's rendered text;
  the IVP Fair value line and the full Greeks section are confirmed still present (not collaterally
  removed).
- Detail-panel field lines now measure a real 5px `margin-bottom` (was 0).
- Tap-to-close (tapping the same row again) still works; selecting a different strike after closing
  still pops a fresh panel.
- Toggling the (i) info panel while a strike stays selected does **not** replay the scroll/pop
  (confirmed by calling `dcChainToggleInfo` in-page directly, since `page.click()` on an off-screen
  button would itself trigger Playwright's own pre-click auto-scroll and confound the assertion).
- Zero console/page errors across the whole interaction.

Not done here, and not needed for this card: a live screenshot from scorr.in itself (this container
has no route to the deployed site, the same structural limitation noted on every UI card this
session) — the geometric in-viewport assertion above is the closest verifiable substitute for "no
manual scrolling needed," and the founder's own live check is still the final word per the card's own
verify list.
