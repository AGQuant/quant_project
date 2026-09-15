# cc#2098 — V8 CLOSED WIN/LOSS box squeeze fix + C·A·R·D strip on OPEN and CLOSED position rows

## What shipped

### Item 1-2: #cl-extra-kpi 2-box squeeze, fixed
Root cause confirmed by reading the live source: `#v8p .kpi{grid-template-columns:1fr 1fr 1fr}`
(`mobile/v8.html:137`) is a shared rule used by `open-kpi`/`cl-kpi`/`net-kpi` (always 3 real boxes)
AND by the inner `.kpi` div `renderClosed()` builds inside `#cl-extra-kpi` for WIN/LOSS + AVG
WIN/LOSS — which is only ever 2 boxes. Inheriting the 3-column rule squeezed each into a 33%-wide
column, wrapping the labels across 3-4 lines exactly as the founder's screenshot showed.

Fix: one new scoped rule, `#v8p #cl-extra-kpi .kpi{grid-template-columns:1fr 1fr}` (line 138,
right after the shared rule). ID beats class in specificity regardless of source order, so this
wins for `#cl-extra-kpi`'s own boxes while `open-kpi`/`cl-kpi`/`net-kpi` keep the untouched shared
3-column rule. No content/copy change to either `kpiWell()` call — box width only, confirmed by
`grep`: exactly one new `grid-template-columns` selector added, the original 3-col rule unchanged.

### Items 3-6: shared C·A·R·D strip, tap-to-reveal, on both OPEN and CLOSED rows
Consumes the existing shared component (`window.ScorrCardStripHtml` / `ScorrCardNav`,
`scorr_card_strip.js`, cc#789/803/805 ground truth) exactly as GVM/Home/My Portfolio already do —
confirmed by grep that this was new ground: zero references to the strip anywhere in
`mobile/v8.html` before this card. Nothing about the strip itself (markup, CSS, the four letters'
meaning, futures-only gating for D) was touched or re-implemented.

**Correction to the spec's own precedent, found while reading the code, not assumed:** the spec
cited "the established A/D-sheet pattern (cc#1946 'row-expand C·A·R·D strip')" as the model to
follow. Reading `mobile/home.html` today shows that exact row-expand mechanism was later *removed*
(explicit comment at `mobile/home.html:417-418`: "removed: `.brr{cursor:pointer}`... the ROW is no
longer a tap target at all") and replaced by an always-visible per-row strip column (cc#1974). That
precedent no longer exists to copy. This does not change what cc#2098 itself asks for — the card's
own stated rationale (dense rows, an always-on 4-button row would not fit the founder's "clean" bar
here) stands on its own — so the tap-to-reveal mechanic below was designed fresh against that
rationale and the card's explicit collision rules, rather than against a pattern that is gone.

**Mechanism** (`mobile/v8.html`):
- `posRow()`/`closedRow()` each gain `data-card-strip="<symbol>"` on the row's own wrapper div, and
  a trailing `<div class="rowstrip" style="display:none"></div>` — present on every row, empty and
  hidden until first tap (lazy fill, not lazy visibility-only).
- `cardStripToggle(row)`: hides the strip if open; otherwise fills it once via
  `window.ScorrCardStripHtml(symbol, '')` (never re-implemented markup) and reveals it. If the
  shared file failed to load, it is a no-op — the row shows no strip, never four dead buttons
  (same guard cc#1070 established).
- Click delegate (`#v8p`'s existing single listener) gains two lines: a guard that returns early
  on a click landing inside `.scorr-card-strip` itself, then the `[data-card-strip]` toggle. The
  guard exists because the strip's own C/A/R/D pills use plain `onclick="ScorrCardNav(...)"` — that
  click still bubbles to `#v8p`'s delegate afterward, and without the guard it would immediately
  collapse the very strip the tap just opened. Placed after the pre-existing `data-mk-sym` check,
  so the marker-flag button (already `stopPropagation()`'d) keeps working exactly as today and is
  untouched.
- The SL/entry/CMP/target rail is decorative only (confirmed: not a tap target anywhere else in
  this file), so it is fair game for the new row-level tap, per the card's own scope.

## Verification

**Diff-shape checks** (grep, matching the card's own verify list):
- Exactly one new `grid-template-columns` rule added (`#v8p #cl-extra-kpi .kpi`); the shared
  `#v8p .kpi{...1fr 1fr 1fr}` rule is byte-unchanged.
- `window.ScorrCardStripHtml` is called from exactly one place (`cardStripToggle`); no new
  hand-written `.scorr-cs-b` / bare C-A-R-D button markup anywhere in the file.
- `node --check` clean on the full extracted inline script (120,079 bytes).

**Real-browser behavioural test** (Playwright/Chromium, not a plain unit test — this is
event-bubbling logic, which needs a real DOM): a harness loading the *actual* `scorr_card_strip.js`
verbatim plus the *actual* committed `cardStripToggle` function and the *actual* 3-line click-
delegate snippet (both grep-confirmed against the committed file immediately before the harness was
written), against two fixture rows built to the same markup contract `posRow`/`closedRow` emit.
15/15 assertions passed:

| Test | Result |
|---|---|
| Strip hidden on load | PASS |
| Tap on empty row area reveals the strip with real C/A/R/D pills | PASS |
| Second tap on the row collapses the strip again | PASS |
| Tapping the strip's own "C" pill dispatches to `ScorrChartCard.open(symbol)` | PASS |
| ...and does **not** also collapse the row (the guard this card added) | PASS |
| Tapping the marker-flag button calls `mkOpen(symbol, side)` | PASS |
| ...and does **not** toggle the strip (pre-existing `stopPropagation` still wins) | PASS |
| The CLOSED row's strip (no flag button) opens/closes independently | PASS |
| One row's strip state is unaffected by tapping a different row | PASS |

Fixtures are clearly constructed (labelled in the harness) — no real position/trade data was
needed to prove this interaction logic; the letters' own behaviour (chart/analysis/result/cockpit,
futures-only D) is cc#789/803/805's unchanged, already-proven code.

## What did NOT change
`scorr_card_strip.js` and the four letters' meanings/handlers — untouched (do_not_touch). The
marker-flag button and its `stopPropagation()` — untouched, still the first check in the delegate.
The SL/entry/CMP/target rail's geometry and P&L-coloured fill — visual only, unaffected. No other
page or surface touched — mobile/v8.html's OPEN and CLOSED position rows only.

## Live checks still needed (Arpit)
Per the card's own verify list: WIN/LOSS and AVG WIN/LOSS render on one or two lines with no
overlap on a real phone. Tapping a CLOSED row reveals a working strip; D is absent/disabled for a
non-futures symbol (e.g. PNBHOUSING) and present for a futures one — this depends on the real
`/api/v8/futures/list` response, which the automated test above did not exercise (cc#789's own
existing, unchanged fetch/cache logic). Tapping an OPEN row does the same, and the existing
marker-flag tap still opens its own detail sheet.
