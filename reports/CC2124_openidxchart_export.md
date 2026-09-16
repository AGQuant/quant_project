# cc#2124 — openIdxChart dead in Index Intel pane (P1 UI BUG)

## Confirmed against the real file first
Same bug class as cc#2118, found during that card's own audit. Confirmed directly in
`v8_dashboard.html` before touching anything: the index-tape header (`rIdxSpark()`'s per-index
card builder, ~line 6649) emits `<div onclick="openIdxChart('${tapeKey}')" ...>` — a literal
inline handler, which resolves in **global** scope. `openIdxChart` itself (~line 8811) is declared
inside the Index Intel pane's module IIFE (opens line 6103, cc#542) and was never exported —
only a differently-named wrapper was: `window.iiIpChart = function(k){ openIdxChart(k); };`
(line 8801). That wrapper is not what the onclick calls, so the click threw `ReferenceError:
openIdxChart is not defined` and did nothing. `typeof window.openIdxChart` is `undefined` before
this fix — matches the originating cc#2118-audit finding exactly.

## Fix
One line, placed next to the existing `iiIpChart` export inside the same IIFE, with a comment
stating why (`v8_dashboard.html`):
```js
window.openIdxChart = openIdxChart;   // cc#2124: the inline onclick resolves in global scope, not this IIFE's
```
`window.iiIpChart` itself is untouched.

## Verify

**Syntax**: extracted the real inline `<script>` block (the one containing `openIdxChart`, 195KB)
verbatim and ran `node --check` — clean.

**Theme ratchet** (`theme_validate`, scoped to `v8_dashboard.html`): `ok: true`, zero raw-primitive
regressions for this file. The tool's global `fallbacks` metric separately flagged pre-existing
regressions in three files this card never touches (`pwa_endpoints.py` +2, `mobile/v8.html` +14,
`scorr_bell.js` +5) — checked `pwa_endpoints.py` specifically since cc#2123 touched it this session:
every `var(--x, literal)` line in that file is CSS text far from the cc#2123 NAV-array diff (a pure
JS array line + comments, zero CSS) — confirmed not caused by this session's work. Flagged here for
whoever picks it up; not investigated or fixed, out of scope for this card.

**Real-code, before/after Playwright harness** — the actual bug reproduced, then disproved, not
just asserted: extracted the real Index Intel module script twice, once from `git HEAD` (pre-fix)
and once from the working tree (post-fix), stubbed only the true external dependency
(`window.ScorrChartCard.open`, capturing calls — the real chart-card module is a separate,
already-shared component per cc#803/cc#1059, out of this fix's scope), and clicked the REAL literal
onclick markup extracted verbatim from the source (`${tapeKey}`/`${name}` hand-resolved to the real
tapeKeys NIFTY/BANKNIFTY named in this card's own spec):
- **Before (git HEAD)**: `typeof window.openIdxChart` is `"undefined"`; clicking the real markup
  throws a real page error, `openIdxChart is not defined` — the exact real bug, reproduced.
- **After (working tree)**: `typeof window.openIdxChart` is `"function"`; `typeof window.iiIpChart`
  is still `"function"`; the real click now correctly reaches the stub with `tapeKey: "NIFTY"`,
  `opts: {index: true, label: "NIFTY"}` — matching `openIdxChart`'s real body
  (`ScorrChartCard.open(tapeKey,{index:true,label})`) exactly; a direct `iiIpChart('BANKNIFTY')`
  call also still reaches the stub correctly (`tapeKey: "BANKNIFTY"`) — proves the pre-existing
  wrapper is unchanged, not just that the new export exists; zero page errors.

**VISUAL_VERIFY_GATE_V1**: screenshotted the fixed run and looked at it directly. It shows an
honest capture of the real invocation (not a fabricated chart-card UI — the real `ScorrChartCard`
module needs live API data this harness does not mock, so faking its visual would be dishonest;
showing the real captured call is not): a `typeof` readout confirming both functions exist, and the
real JSON call log with two entries (`NIFTY` from the actual click, `BANKNIFTY` from the direct
`iiIpChart` call), each carrying `index: true` and the correct label — plain, correct, legible.

## What did NOT change
`openIdxChart`'s own body, `window.iiIpChart`, `mountIdxTapes()`, the tape-header markup, and every
other one of the ~37 `window.iiXxx` exports in this module — pure one-line addition, confirmed by
reading the diff (3 lines: the export + a 2-line comment).
