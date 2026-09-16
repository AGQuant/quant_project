# cc#2114 — chart card timeframe select relocated next to the symbol title (P2)

## The bug, as reported
Follow-up on cc#2110 (native `<select>` for the TF row). Founder screenshot: the select — 56px
wide — was still sharing `#scorrChartTfs`'s row with the four overlay-toggle buttons (Pivots/Fib/
Channel/GVM), and on a real device it rendered clipped/barely visible at that row's left edge. My
own cc#2110 Playwright verification (390px/844px, desktop Chromium) did not reproduce this — the
same class of real-device-vs-Playwright gap cc#2110's own report already flagged honestly for the
*original* pill-row bug. Rather than chase the exact mechanism again, the founder's own fix is
structural: give the select its own spot next to the symbol, so it never has to compete with the
four toggle buttons for width in the same row at all.

## Fix
`scorr_chart_card.js`:
- `_buildModal()`'s header template gains a new `<span id="scorrChartTfHost">` immediately after
  `#scorrChartTitle`, before `#scorrChartHL`.
- `_paintChrome()`: the `<select>`-building block now targets `tfHost` (`#scorrChartTfHost`)
  instead of `host` (`#scorrChartTfs`). The leading `<span>` separator that used to sit between the
  select and the Pivots button — its only job was dividing those two groups within one shared row —
  is dropped; there is nothing left in that row to divide from. `#scorrChartTfs` now holds only the
  four overlay-toggle buttons, unchanged in every other respect (same order, same styling, same
  click handlers).
- `_setTab()`: the existing Peers-tab hide logic (`if (tfs) tfs.style.display = ...`) already hid
  `#scorrChartTfs` when switching away from the Chart tab, with its own comment explaining why
  ("Timeframe pills belong to the chart... the peer columns are fixed Day/Week/Month"). That
  reasoning applies identically to the relocated select, so `_setTab()` gains the matching
  `if (tfHost) tfHost.style.display = ...` line — otherwise the select would keep showing next to
  the title on the Peers tab while the (now select-less) toggle row correctly hid, which would be a
  real, if minor, regression this card would have introduced silently.

Nothing about the select's own options, values, `TF_LABEL` lookup, or `onchange`/`_load()` wiring
changed — only its DOM position, per the card's own `do_not_touch`.

## Verification

**Syntax**: `node --check scorr_chart_card.js` → clean.

**Theme ratchet**: fallback (`.js` in scope) 1→1, delta 0. Raw ratchet does not gate `.js` files.

**`git diff --stat`**: only `scorr_chart_card.js` changed (23 insertions/10 deletions).

**Real-code Playwright harness**: freshly extracted `TF`/`TF_ORDER`/`PIV_TFS`/`_pal()`/
`_buildModal()`/`_paintChrome()`/`_setTab()` verbatim from the edited file (same real `_load(k)` —
`_tf = k; _paintChrome();` — as cc#2110's harness, so the select's wiring is exercised end-to-end).
Same founder repro state as cc#2110 (CANBK, `_tf="ALL"`, -5.0%, H133/L122, WEAK). 25/25 assertions
pass across two widths (390px, 844px) plus interaction and tab-switch checks:
- The select is now found inside `#scorrChartTfHost` and confirmed **absent** from `#scorrChartTfs`
  at both widths.
- The select sits on the same row as the title (vertical overlap within 10px) and to its right —
  i.e., genuinely "next to the symbol," not just somewhere else on the page.
- The select's right edge stays inside the viewport at both widths (not clipped).
- `#scorrChartTfs` now holds exactly 4 children, all `<button>`, in order Pivots/Fib/Channel/GVM —
  no select, no leftover separator — and every one of those buttons' right edge stays inside the
  viewport too.
- Selected value is `"ALL"` on load, matching the founder's own repro state.
- After `select_option("1M")`: the select (still in its new host) re-renders showing `1M`, and the
  overlay-toggle row survives the re-render intact.
- **Peers-tab check** (the regression this card could have introduced silently): calling
  `_setTab("peers")` hides both `#scorrChartTfHost` (new) and `#scorrChartTfs` (pre-existing) via
  `getComputedStyle(...).display === "none"`; calling `_setTab("chart")` afterward shows the host
  again. Confirms the relocated control was wired into the existing tab-visibility logic, not left
  behind.

**Screenshots — looked at directly, per cc#2108's VISUAL_VERIFY_GATE_V1**:
- `cc2114_portrait.png` (390×700, the reported-broken width): first line reads "CANBK · Price
  [ALL ▾] -5.0% · H 133 · L 122" — the select sits cleanly next to the title, fully readable, native
  chevron visible, nothing clipped. The WEAK verdict badge wraps to its own second line (see honest
  note below). The Pivots/Fib/Channel/GVM row now renders alone on its own line, comfortably
  right-aligned, with no select and no stray leading divider.
- `cc2114_fullscreen.png` (844×390): everything — title, select, H/L%, verdict badge — fits on one
  line; the toggle row sits alone below it. Confirms no regression at the wider width.
- `cc2114_after_change.png` (post `select_option("1M")`): select now reads "1M ▾" in the same new
  position, and Pivots has correctly flipped to blue/enabled (1M is pivot-eligible, ALL was not) —
  the same live-recalculation cc#2110 already verified, now confirmed unaffected by the relocation.
- **Honest note, not glossed over**: at the narrower 390px width, adding the ~56px select to the
  title's own line leaves less room on that line, and the WEAK badge — which used to share that
  line — now wraps to its own row instead (three header rows total before the divider, instead of
  two). Confirmed via the fullscreen screenshot that this is purely a width-driven flex-wrap
  consequence (same header, same flex-wrap the code already relies on for the toggle row), not a
  clipping or overlap bug — nothing is cut off, nothing overlaps, and the badge is still fully
  readable on its own line. Not fixed further, since the spec's own scope is limited to relocating
  the select and this doesn't clip or hide anything; worth knowing about if the header is revisited.

## What did NOT change
The select's own `TF_ORDER`-driven options/labels and `onchange` → `_load(tf)` wiring. The
Pivots/Fib/Channel/GVM buttons' own behaviour, styling and order. `_buildModal()`'s other elements
(maximize/close, tabs, chart box, peer pane). Every other function in the file.
