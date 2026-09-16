# cc#2118 — TC Scanner Long/Short tags dead: export the two side-filter setters (P1)

## Root cause, confirmed in a real browser before touching anything
Per the card's own explicit instruction — "BROWSER CONSOLE FIRST, before the fix" — I did not
trust the spec's diagnosis on its own. Extracted `setTc13OpenSide`/`setTc13ClosedSide`/
`_tc13SideTag` verbatim from `v8_dashboard.html`, wrapped in the identical IIFE pattern, rendered
a real `_tc13SideTag('Long','BUY',...)` button, and clicked it in a real Playwright/Chromium page
with `page.on('pageerror', ...)` listening:

```
setTc13ClosedSide is not defined
```

**Exact match to the card's own required diagnosis.** `render()` was never invoked (a counter
stayed at 0), confirming the click is a complete no-op besides throwing.

Confirmed structurally too: the TC13 module is a genuine, self-contained IIFE —
`<script>(function(){ 'use strict'; var _booted=false; ...` opening at line 9646, closing
`})();` at line 10242 (pre-fix numbering) — everything inside, including both setters, is
private unless explicitly assigned to `window`. Grepped the whole IIFE body for every
`window.*` assignment: exactly three, `tc13Boot`, `tc13InfoOpen`, `tc13InfoClose` — matching the
card's own claim that the setters were never among them.

## Fix
Two lines added right next to the existing `window.tc13Boot=...` assignment, per the card's own
placement instruction, with a comment stating why the export is required (inline `onclick`/
`onkeydown` attributes resolve against global scope, not the enclosing closure) so a future
tidy-up doesn't delete them again:
```js
window.setTc13OpenSide=setTc13OpenSide;
window.setTc13ClosedSide=setTc13ClosedSide;
```
Re-ran the same minimal repro with the export applied — the `ReferenceError` is gone.

Nothing else touched: `closedTable()`, `closedKpis()`, `bookRs()`, `openTable()`, `tc13Cap()`,
`_tc13SideTag()`, the state vars and their toggle-back-to-`ALL` rule were all already correct —
confirmed by reading them directly. `closedTable()` already filters `fRows` first, then derives
`closed`/`kpi`/`realised` from that same filtered set (line ~9921 onward) — the "table + capsules
don't update" symptom needed no filtering logic change, exactly as the card predicted; it resolves
entirely once the setter is reachable.

## Audit (scope item 4) — other IIFE-scoped functions referenced from inline `on*` attributes
Static grep found 44 function names referenced from a literal `onclick`/`onkeydown`/etc. attribute
in the file with no matching `window.X=` export. Cross-checked all 44 in a real browser
(`typeof window[name]` after loading the live page's own scripts) rather than trusting the static
list at face value — 42 resolved to real global functions (reachable via mechanisms elsewhere in
the file I did not need to trace further), one (`esc`) was a false positive from my own regex (no
literal `onclick="esc(...)"` exists anywhere in the file — confirmed by direct grep, zero matches).

**One genuine additional instance of the same bug class, found and NOT fixed here per the card's
own instruction:** `openIdxChart` (declared ~line 8811, inside the separate "Index Intel pane
(V10 engine display bridge)" IIFE, cc#542, opening at line 6103). `typeof window.openIdxChart` is
`undefined` — only a *different*-named wrapper (`window.iiIpChart = function(k){ openIdxChart(k);
};`) is exported, not `openIdxChart` itself. Its own call site (line 6649,
`onclick="openIdxChart('${tapeKey}')"`) is inside the index-tape header card — live, founder-facing
markup last touched 24-Aug ("cc#1281, founder direct"), styled with `cursor:pointer` and a
`title="Open the {name} chart"` — reads as a genuine, currently-dead control, not orphaned/dead
code. **Recommending a follow-up P1/P2 card for this** rather than widening this diff, per the
card's own explicit instruction ("Do not fix them in this card... name it and file a follow-up").

## Verification

**Syntax**: `node --check` clean on all 8 inline `<script>` blocks in `v8_dashboard.html`.

**Theme ratchets**, computed directly against `origin/main`: fallback 59→59, raw 1141→1141, both
delta 0.

**`git diff --stat`**: only `v8_dashboard.html` changed, 8 insertions (comment + two export lines),
zero deletions.

**Real-code Playwright harness**: extracted every function `render()`'s call chain actually needs
— `render`, `openTable`, `closedTable`, `closedKpis`, `bookRs`, `tc13Cap`, `capBookTxt`/`capBookCls`,
`_tc13SideTag`, both setters, `tc13OpenRow`, and every display-formatting helper (`esc`, `n2`,
`cl2`, `rsTxt`, `pnlTxt`, `lotTxt`, `prox`, `dShort`, `eTime`, `daysHeldNum`, `proxPct`,
`reasonPill`) plus the shared `sortTbl`/`drawTbl`/`tblSort`/`SORTREG` — all verbatim from the
edited file, all module state vars included. 20/20 assertions pass:
- **Closed Book**: All shows all 9 real closed rows; clicking Long shows exactly the 7 real BUY
  rows (verified every visible row really is BUY, not just a count match); Realised/Accuracy
  capsules provably recompute (`+₹5,265`→`+₹7,615`, `66.7%`→`85.7%`, not stale); the `Closed <n>`
  chip matches the visible row count; clicking Long again toggles back to All (9 rows, All chip
  lit); clicking Short shows exactly the 2 real SELL rows (GODREJCP, TCS by symbol, not just count).
- **Empty state**: re-rendered with a payload carrying zero SELL closures while the Short filter
  is active — shows "No closed Short TC Scanner positions..." (the real sentence, real last-closure
  date), never a blank table.
- **Open Book independence**: filtering Open Book to Long (3 of 5 synthetic rows) leaves the Closed
  Book's own state (still Short-filtered, still showing its empty sentence from the prior step)
  completely untouched — direct proof the two books hold separate state vars, per the card's own
  explicit check.
- **Keyboard path**: focus a side tag, press Enter — filters identically to a click (2 rows), no
  page error.
- Zero uncaught page errors at any point in the sequence (checked after every interaction).

**Real data, not fabricated**: Closed Book test data is **9 real closed TC Scanner trades**
(`tc_intraday_trades` JOIN `futures_universe`, queried this session) — 7 LONG (6 wins, 1 loss:
COFORGE), 2 SHORT (both losses: GODREJCP, TCS). The capsule math is independently checkable by
hand: Long total `+₹7,615` + Short total `−₹2,350` = `+₹5,265`, exactly the All total — confirms
the filter is a true partition, not a coincidental re-render. `tc_intraday_positions` has **zero**
real open rows right now (checked live — genuinely empty, not a fetch failure), so Open Book test
rows (5, shaped identically to the real payload) are clearly-labeled synthetic fixtures, stated as
such in the harness and here — never presented as live figures.

**Screenshots — looked at directly, per cc#2108's VISUAL_VERIFY_GATE_V1**, exactly as the card's
own instruction specified ("a screenshot of one state alone proves nothing here"):
- `cc2118_closed_all.png`: All chip lit, Closed 9, Realised `+₹5,265`, Accuracy `66.7%` (6 of 9),
  Avg Profit `+₹585`, all 9 rows visible (7 green BUY, 2 red SELL).
- `cc2118_closed_long.png`: Long chip lit, Closed **7**, Realised **`+₹7,615`**, Accuracy
  **`85.7%`** (6 of 7), Avg Profit **`+₹1,088`** — every number visibly different from the All
  state — table shows only the 7 BUY rows, GODREJCP/TCS gone.
- `cc2118_closed_short.png`: Short chip lit, Closed **2**, Realised **`−₹2,350`** (red), Accuracy
  **`0%`** (0 of 2), Avg Profit **`−₹1,175`** (red) — table shows only GODREJCP and TCS.
- `cc2118_open_short.png`: Open Book's own Short filter, independently — Short chip lit, Open 2,
  Unrealised `+₹40`, table shows only WIPRO/ITC (the synthetic SELL rows) — confirms the Open Book
  side of the fix visually too, not just via DOM assertions.

One harness-only cosmetic note, not a real-page issue: the `SCROLL →`/badge/date-range row overlaps
slightly in these screenshots because my minimal harness CSS doesn't replicate the real page's full
`.sh`/`.badge`/`.tc13-datepick` layout rules (out of scope for a component-level harness) — the
capsules and table, which is what this fix touches, render correctly.

## What did NOT change
`closedTable()`, `closedKpis()`, `bookRs()`, `openTable()`, `tc13Cap()`, `_tc13SideTag()`, the
`_tc13ClosedSide`/`_tc13OpenSide` state vars and their toggle rule, the date-range controls, the
`(i)` info sheet, the P&L colour invariant, `tc_scanner_endpoints.py` (zero backend change — this
is a client-side filter over rows the endpoint already returns, per the card's own do_not_touch).
`openIdxChart` and every other audited name — found, not fixed, per scope item 4's own instruction.
