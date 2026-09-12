# cc#2005 — the frontend: Unrealised/Realised/Net swipe deck

Second push. First push (sha `38d552d`) landed the backend prerequisite (`v8_daylog`'s additive
`side` filter) and the recon + gross/net reasoning. This lands the actual UI in `mobile/v8.html`.

## What this lands

- **The 3-pane swipeable carousel** (`#posTrack`/`#posDots`), CSS scroll-snap + a plain `scroll`
  listener — the exact mechanism `mobile/home.html`'s hero deck already uses (no touch handlers,
  copied CSS shape), plus `bookDots()`'s tap-a-dot-to-jump. Pane 0 = Unrealised (existing
  `renderKpi()`, unchanged). Pane 1 = Realised — **new** REALISED/LONG/SHORT headline boxes (item
  1's own shape), canon totals same convention as pane 0 (never moves with the list's own filter
  chips). Pane 2 = **new** Net Total (unrealised + realised, all/long/short), computed entirely
  from data already on `STATE.book` — no new fetch.
- **Founder addition B (section merge)**: the old standalone "Closed" section (header, `#cl-kpi`,
  list, more-toggle) is gone from its old spot; `#cl-kpi` relocated into pane 1, `#sec-closed`/
  `#cl-more` relocated to sit with the open list. The WIN/LOSS + AVG WIN/LOSS boxes (item 1 doesn't
  ask for them in the swipeable strip — only REALISED/LONG/SHORT are) move to their own
  `#cl-extra-kpi`, still fully filter-responsive exactly as before — relocated, not deleted or
  changed, per the founder's own instruction.
- **Founder addition A (pane drives list)**: `posPaneChanged()` shows/hides the open filter bar +
  list (pane 0), the closed filter bar (`#cl-filter-bar`, the three `clSeg` rows moved out of
  `#cl-kpi`) + extra stats + list (pane 1), or both lists with a divider label, no filters (pane 2
  — the founder's own wording reads as a plain concatenation: "Fable's assumption, founder may
  override"). The header (`#pos-n`) shows "N open" / "N closed" / "N total" following the pane.
- **Founder addition (11-Sep 21:20, the (i) button)**: one (i) per pane (its leading box only),
  `kwInfoOpen(key)` — a small sheet with the GROSS → brokerage → NET working in plain words, same
  body-level-sheet shape `openSortSheetOpen()` already established in this file.
  - Unrealised: brokerage charged so far is **honestly ₹0**, with a note why (a position only
    pays the ₹500 fee when it closes) — not an invented anticipated-future fee. This is the
    resolution to the gross/net finding from the backend push: no number changes, the reasoning is
    just stated on tap.
  - Realised: gross → −brokerage (₹500 × N trades) → net, off the existing `bk.gross`/
    `bk.brokerage`/`bk.realised` fields.
  - Net: unrealised(net) + realised(net) = combined.
- **Item 3 (every box tappable)**: all 9 boxes (3 panes × 3) carry `data-kw-chart="all|long|short"`.
  Tapping any of them opens the SAME chart component regardless of which pane it was tapped from —
  "long" from pane 0, pane 1 or pane 2 all show the identical long-side chart, per item 4's own
  framing (one cumulative REALISED curve per slice, not nine different charts).
- **Item 4 (the chart)**: `dlChartOpen()`/`dlSeriesLoad()`/`dlChartSvg()`/`dlChartSheetHtml()`
  (the existing Day Log P&L chart, cc#1873) extended to be slice-aware —
  `DL_SERIES` is now `{all,long,short}` (one cache per slice, fetched via the new `?side=` param
  from the first push) instead of one global value. The live unrealised total for the tapped slice
  (read straight off `STATE.book`, no new fetch) is appended as ONE extra point — a dashed
  connector + a hollow "live" marker — matching item 4's own wording exactly: "realised curve;
  unrealised marked as the live end-point," not a fabricated historical unrealised series (which
  doesn't exist anywhere and isn't what was asked for). Passing no slice (the pre-existing Day Log
  sheet's own chart button) still shows the whole-book chart, now also carrying the live marker
  when `STATE.book` is loaded — a strict enhancement, not a behaviour change, of that existing
  button.
- **Dead-code cleanup** (flagged in the first push's report): `bk.brokerage_per_trade` was never a
  real payload key (`mobile_ext.py` ships `brokerage`, the running total, not a per-trade rate) —
  both references replaced with the real, founder-locked constant (₹500/closed trade, rule 13),
  stated directly rather than left silently dead.

## Verify

- `node --check` clean on both `<script>` blocks.
- **Playwright structural check** against the real, just-edited file (headless Chromium, the
  pre-installed one — `mobile/v8.html` served over local HTTP with `/api/mobile/v8book` and
  `/api/v8/daylog/series` intercepted with realistic synthetic payloads built from the confirmed
  `v8_book_canon.py`/`mobile_ext.py` field names; every other endpoint intentionally 500'd to
  exercise the page's own existing `catch(()=>null)` degradation path):
  - 3 panes, 3 dots, 9 tappable boxes, 3 (i) buttons — positive counts, not just "it rendered."
  - Tapping dot 1 (Realised): header → "N closed", open filter bar + list hide, closed filter bar
    + extra stats + list show, `#cl-kpi` shows the right REALISED/LONG/SHORT figures.
  - Tapping dot 2 (Net): header → "N total", BOTH lists visible with the divider, `#net-kpi`
    correctly sums unrealised + realised for all/long/short (checked against the synthetic
    numbers by hand).
  - The Net Total (i) sheet and the Unrealised (i) sheet both render the intended real numbers and
    copy, including the "brokerage ₹0, not owed yet" framing.
  - Tapping a LONG chart target opens the per-slice chart with exactly 2 circles in the SVG (the
    last real point + the new live marker) and the live-marker explanation line.
  - Dot round-trip back to pane 0 restores the original visibility state.
  - **Zero unexpected console errors or page errors** across the entire flow.
- **This verification pass itself caught and fixed two real bugs before they could ship**, neither
  of which `node --check`/`ast.parse`-style validation would have caught (both are DOM-wiring bugs,
  not syntax errors):
  1. `renderOpen()` still wrote to `#open-n` directly; the header rewrite had renamed that span to
     `#pos-n` without leaving a target for the existing function — threw
     `TypeError: Cannot set properties of null` on every render. Fixed by restoring a hidden
     `#open-n` (same pattern already used for `#cl-n`/`#cl-r`).
  2. The dot track's `onscroll="posDots()"` was an inline HTML attribute; `posDots` only exists
     inside this file's own strict-mode IIFE, which an inline attribute handler (executing in
     global scope) cannot see — every scroll threw `posDots is not a function` and NO pane switch
     ever actually applied, silently. Fixed by wiring the listener with `addEventListener` from
     inside the closure instead, matching how every other interactive element in this file is
     already wired (the shared `#v8p` click delegate, never an inline `onclick`).

## What this does NOT change

Nothing on the backend beyond the first push's additive `side` filter. No existing consumer of
`v8_daylog`/`v8_daylog_series` without `?side=` sees any different output. The Day Log tab, the
Sectors section, Day Performance, and the Funnel section are untouched (Sectors/Day-performance now
sit directly after the consolidated Positions block instead of being sandwiched between the old
Open and Closed sections — the natural, and only sensible, consequence of merging Open+Closed into
one unit, per founder addition B's own instruction).

## What still needs the founder

A founder glass-check on a real device is the one thing this session cannot do itself: swipe the
3 panes, tap all 9 boxes, confirm each opens the right chart, open the 3 (i) sheets, confirm the
combined pane-2 list reads correctly on a real phone screen. Everything above is verified as far as
this session can verify it (structurally, against real code, with synthetic-but-realistic data);
the glass-check is Fable's/the founder's own step, same as every other UI card on this board. Card
NOT done — Fable verifies.
