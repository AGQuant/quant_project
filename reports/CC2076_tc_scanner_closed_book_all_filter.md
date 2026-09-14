# cc#2076 — TC Scanner Closed Book: All/Long/Short filter + per-tab stats

Founder ask (screenshot, 14-Sep-2026): the Long/Short filter buttons on the Closed Book don't
filter the table; only two buttons exist, need a third — All, Long, Short, All active by default;
the three stat cards must recompute per selected tab.

## Step 1 gate — which file, and which of the four claims is actually current

The evidence cites "web /check page." Traced this precisely rather than assuming: `/check`
(`main.py:1401`) serves `scorr_check.html`, which contains **zero** matches for "Closed Book",
"REALISED", "ACCURACY" or "closed_by_exit" — it is not the file. `v8_dashboard.html`'s own "check"
TAB (`#pane-check`) is confirmed to be a bare `<iframe id="checkFrame">`, not an independent
implementation. Repo-wide search for the same terms turned up exactly one live UI implementation:
`v8_dashboard.html`'s `.tc13-wrap` TC Scanner section (the file already worked on this session —
cc#2073/2074) — its exact capsule labels (`Realised`/`Accuracy`/`Avg Profit` via `tc13Cap()`) and
row shape match the screenshot's numbers precisely. `mobile/tcscan.html` (a candidate by name) was
checked and ruled out — it shows read-only "Long record"/"Short record" summary cells and "Net
pts", not the three named capsules or tappable filter chips at all. Conclusion: this card
describes `v8_dashboard.html`'s own TC13 tab; "/check page" in the evidence is a mislabel (most
likely confusing the dashboard's own "Check" hash-tab with the "TC Scanner" hash-tab it sits
beside in the same shell).

**Re-read `closedTable()`/`setTc13ClosedSide()`/`render()` fresh, line by line, against the four
claims specifically:**
- **"Only two buttons, need three (All/Long/Short)"** — TRUE, confirmed by reading: `_tc13ClosedSide`
  defaults to `'ALL'` and IS reachable (tap the active chip again to toggle back), but there was
  never a visible, dedicated **third chip** for it. Genuine, current gap.
- **"Filter buttons do not filter the table when clicked"** and **"stats don't change with side
  selection"** — reading `closedTable(rows, range, lastClosure)` shows `fRows` filtered by
  `_tc13ClosedSide` BEFORE `kpi=closedKpis(fRows)` and `realised=bookRs(closed)` are computed —
  both the table body and all three capsules already read the filtered set. This contradicted the
  report, so it earned a materially more rigorous check than a re-read: real **DOM click events**
  dispatched on the actual chip elements (not direct JS calls), the gap every prior test on this
  file had left — see Verify. Confirmed working correctly. The most likely reconciliation: the
  undiscoverable "no All chip" gap (confirmed real, above) is what actually produced the founder's
  report — tapping the active filter again with no visual "All" chip landing makes the interaction
  read as broken even though the underlying filter/recompute is correct.

**Scope respected**: the card's own scope_note limits this to the Closed Book; the Open Book has
the identical toggle-only pattern (shares `_tc13SideTag`) but was not reported and is left alone,
per "do not touch... unless... confirm first" — not confirmed broken, not touched.

## The fix

One addition: a third chip, `_tc13SideTag('All','ALL',_tc13ClosedSide,'setTc13ClosedSide')`,
placed before Long/Short. `setTc13ClosedSide()` itself needed **zero changes** — its existing
toggle rule (`_tc13ClosedSide===v ? 'ALL' : v`) already sets `'ALL'` correctly on an explicit call,
so this is a plain third option through the exact same shared component, not a new mechanism.

## Verify

`node --check` clean (all 8 inline `<script>` blocks). Real headless Chromium, `closedTable()`/
`render()`/`setTc13ClosedSide()` extracted verbatim from the committed file — **20/20 checks
pass**, deliberately built around **real DOM click and keyboard events on the actual chip
elements** (`.click()` / a real `Enter` keypress after `.focus()`), not direct function calls —
the one gap this specific founder report exposed in this session's prior testing of the same code:
- Default state: three chips (All/Long/Short) in order, All active, all 5 rows (2 BUY + 3 SELL)
  shown, Realised capsule = the combined total.
- A **real click** on Long: table narrows to the 2 BUY rows only, Long becomes active, and the
  Realised capsule recomputes to the BUY-only total — cross-checked against an independently
  computed expected value in the test itself, and confirmed to genuinely differ from the
  unfiltered total (proves recomputation, not a relabel).
- A **real click** on Short: 3 SELL rows only, capsules differ from both prior states.
- A **real click** on the already-active Short chip: toggles back to All (pre-existing behaviour
  preserved, not broken by the new chip).
- A **real click** directly on the new All chip from a filtered state: returns to unfiltered.
- A **real keyboard Enter** on the Long chip filters identically to a click (the pre-existing
  accessibility path stays intact).
- Zero page errors throughout.

Re-ran cc#2072's (17/17) and cc#2073's (38/38) full suites against the current file afterward —
both still pass clean, confirming this addition caused no regression in the Open Book or the
Closed Book's date-range work landed earlier today.

**FOUNDER-ONLY, not done here** (this container is network-blocked from scorr.in): confirming
on-glass that the live Closed Book now shows a discoverable All chip and that tapping it/Long/
Short visibly updates the table and the three stat cards together.
