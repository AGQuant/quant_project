# cc#2110 — chart card timeframe control: native `<select>` replaces pill row (P0)

## The bug, as reported
Founder screenshot: in the portrait/modal chart view, the timeframe row showed only "ALL" — the
other four pills (1D/1W/1M/1Y) were not visible. The same markup, same data, rendered correctly in
the fullscreen/maximized view (all 5 pills visible). This is `scorr_chart_card.js`'s `_paintChrome()`
— a different component from `mobile/v8.html`'s own chart chrome (cc#2101/cc#2103 fixed that one
separately) — used by the peer/company chart card modal (`_buildModal()`/`#scorrChartTfs`).

## What I could and could not reproduce — stated honestly
I rebuilt the exact pre-fix code (cc#2103's flex-wrap fix, still live before this card) in a
real-cascade Playwright harness and rendered it at 375px portrait with realistic content
(BANKBARODA, +2.4%, BREAKOUT). **It did not reproduce the bug** — all 5 pills rendered correctly,
contradicting the founder's screenshot. I tried this because cc#2108's screenshot rule now applies
to every UI card, and I wanted to see the actual failure before changing anything, the same way
cc#2106/cc#2107 found their real root cause by looking rather than assuming.

I could not pin down why my Chromium render differs from the founder's real device — candidates are
real mobile Safari/Chrome flexbox quirks, device font metrics, or a narrower real viewport than I
tested — but none of those are things a Playwright/desktop-Chromium harness can settle from here.
Rather than keep chasing an exact repro, I went with the founder's own explicitly prescribed fix:
replace the pill row with a native `<select>`. This is not a workaround for an unconfirmed
mechanism — a `<select>` has a fixed, small rendered footprint regardless of the number of options,
the surrounding header width, or any per-browser flex-wrap quirk, so it is structurally immune to
the entire bug *class* (squeeze/wrap/clip on a multi-button row) independent of which exact
mechanism caused this specific report.

## The fix
In `scorr_chart_card.js`, `_paintChrome()`'s `TF_ORDER.forEach(...)` loop — which built one
`<button data-tf="k">` pill per timeframe — is replaced with a single `<select class="scorr-tf-sel">`
holding one `<option>` per `TF_ORDER` entry (same label lookup, `TF_LABEL[k] || k`), the current
`_tf` marked `selected`, and `onchange` calling the same `_load(tfSel.value)` the pills called on
click. The disabled/title-tooltip behaviour for the 5m/non-futures case is preserved as an option
`title` (a `disabled`-style greyed pill doesn't exist as a native `<option>` state that reads well,
so this keeps the informational tooltip cc#2043 established — 1D is never actually blocked, `_load`
already routes a non-futures symbol through the Yahoo on-demand path regardless).

The old 3Y-depth-gate branch (`disabled`/`y3` handling) was **not** ported forward — confirmed via
`git log` that TF_ORDER has carried only `["5m","1W","1M","1Y","ALL"]` since cc#2103 removed 3Y
entirely, so that branch has been dead code on every real path since then.

Nothing else in `_paintChrome()` changed: the Pivots/Fib/Channel/GVM overlay-toggle buttons, the
GVM pillar sub-row, and `_buildModal()`'s header layout (including the pre-existing flex-wrap /
flex-basis:100% structure from cc#2103) are all untouched.

## Verification

**Syntax**: `node --check scorr_chart_card.js` → clean.

**Theme ratchets**, computed directly against `origin/main` content (not the MCP snapshot):
- Fallback ratchet (`.js` is in scope): `scorr_chart_card.js` 1→1, delta 0, level.
- Raw ratchet does not gate `.js` files at all (confirmed via `theme_validator.gate()`'s own
  `.css`/`.html`-only check) — not applicable.

**`git diff --stat`**: only `scorr_chart_card.js` changed.

**Real-code Playwright harness** — extracted `TF`, `TF_ORDER`, `PIV_TFS`, `_pal()`, `_buildModal()`,
`_paintChrome()` verbatim (brace-matched) from the edited file, with a **real, non-stub** `_load(k)`
(`_tf = k; _paintChrome();`) so the select's `onchange` wiring is exercised end-to-end, not just its
initial render. Realistic content matches the founder's own reported repro exactly: CANBK, -5.0%,
H 133, L 122, WEAK verdict, `_tf = "ALL"` on load. 24/24 assertions pass across two widths (390px
portrait, matching the reported-broken case; 844px "fullscreen-width", matching the
reported-working case) plus a post-interaction check:
- `select.scorr-tf-sel` present, exactly 5 options in order with correct labels
  (`5m`→"1D", `1W`→"1W", `1M`→"1M", `1Y`→"1Y", `ALL`→"ALL").
- Selected value is `"ALL"` on load, matching the founder's exact repro state.
- Select's computed width is the fixed 56px at both widths (the structural point of this fix).
- Select's right edge and all 4 overlay-toggle buttons' right edges stay inside the viewport at
  both widths.
- Header height stays under 120px at both widths (not the old cc#2103-era multi-row squeeze).
- After `select_option("1M")`: `_load` fires, `_tf` updates, `_paintChrome()` re-renders — the
  select now shows `1M` selected, still has all 5 correct options, and the overlay buttons survive
  the re-render (`host.innerHTML = ""` + rebuild, so a real re-render was confirmed, not a stale
  DOM read).

**Screenshots — looked at directly, per cc#2108's VISUAL_VERIFY_GATE_V1**:
- `cc2110_portrait.png` (390×700, the founder's reported-broken width): header shows
  "CANBK · Price -5.0% · H 133 · L 122 [WEAK]" on the first row; second row shows "ALL ▾" (the new
  select, with its native chevron) followed by all four toggle chips — Pivots (greyed/disabled,
  correct: ALL is outside `PIV_TFS`), Fib (blue/on), Channel (grey/off), GVM (grey/off) — all on
  one line, nothing clipped, nothing overlapping, nothing run off the right edge.
- `cc2110_fullscreen.png` (844×390, the founder's reported-working width): same content, same
  layout shape, comfortably wider — confirms no regression at the width that already worked.
- `cc2110_after_change.png` (post `select_option("1M")`): select now reads "1M ▾", and — correctly
  — Pivots has flipped to blue/enabled (1M is inside `PIV_TFS`, ALL was not), confirming the
  re-render is live and timeframe-dependent state recalculates correctly, not just the label.
- **One pre-existing layout note, not a regression from this card**: in both screenshots, the
  maximize (⛶) and close (×) icons render on their own third row below the TF/toggle row, rather
  than inline with the title. Read `_buildModal()`'s HTML directly to confirm why: those two
  buttons sit in DOM/flex order *after* `#scorrChartTfs`, which carries `flex-basis:100%` (a
  cc#2103 change, untouched by this card) — a 100%-basis flex item always claims its own full line,
  which pushes anything after it in document order to the next line regardless of available width.
  This is the same in both screenshots and would have been the same with the old pill row too — it
  is not something cc#2110 introduced or something this card's scope covers.

## What did NOT change
`_buildModal()`'s HTML structure, the Pivots/Fib/Channel/GVM overlay-toggle logic, the GVM pillar
sub-row, `TF`/`TF_ORDER`/`PIV_TFS`/`TF_LABEL`, and every other function in the file.

## Landing evidence (rule 15)
`git ls-tree origin/main -- scorr_chart_card.js` blob sha `6d76873...` matches the local working
tree's `git hash-object` exactly. `github_read` of the same path against `main` independently
reports the identical sha (`6d768730045bee087e569947364c2e3b4a2df194`) and byte size (122152), and
its returned content contains the new `scorr-tf-sel` select markup. Both checks agree — the commit
is on `main`, not just pushed to the branch. Commit `125f2bd`.

## One unrelated pre-existing dead-code note, found while confirming `data-tf` had no other live
users — not caused by this card, not fixed here
Grepped the whole file for `data-tf` after the edit, to make sure nothing else still expected pill
markup. One hit: `_apply3Y()` (line ~1230) does `host.querySelector('[data-tf="3Y"]')` to grey out
the 3Y pill once a depth probe (`_probe3Y()`, called unconditionally on every chart open, line
~1848) comes back short. **This has been fully dead since cc#2103 removed "3Y" from `TF_ORDER`**
— no button carrying `data-tf="3Y"` has existed since then, pill-based or (now) select-based, so
`_apply3Y` has already been a silent no-op for every chart opened since that card, and this change
does not alter that. What this change does NOT alter, but is worth flagging separately: the
unconditional `_probe3Y(_sym)` network call (`GET /api/candles/{sym}?tf=3Y&probe=1`) still fires on
every chart open regardless — its result has been discarded since cc#2103 too. Small, pre-existing,
not a regression from this card, and out of this card's scope to fix silently — filing a separate
low-priority cleanup cc_task rather than bundling an unrelated fix into a P0 UI card.
