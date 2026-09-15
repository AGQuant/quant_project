# cc#2103 — Chart Card: trim TF row, default 1M, rolling 100-bar 1D, fix TF-row clipping

## Ground truth first (item 4)
The card's own instruction was to confirm via code whether the founder's screenshot was a genuine
overflow/wrap bug or an artifact of some other container's `overflow:hidden`, not to assume the
TF-count trim alone would fix it. Before changing anything, `_buildModal()`'s real HTML template and
`_paintChrome()`'s real button-rendering loop were extracted verbatim (brace-matched, not retyped)
into a Playwright harness and measured at real phone widths (360/390/412px), both with an empty
title/H-L/verdict (a freshly-opened card) and with realistic populated content.

**Finding: it is a genuine flex-squeeze/wrap bug, not `overflow:hidden` clipping unrelated content.**
`#scorrChartTfs` is one flex container holding the TF pills **and** the Pivots/Fib/Channel/GVM toggle
buttons (11 elements pre-trim) as a single cluster. It shares a `nowrap` row (`#scorrChartHead`) with
the title/H-L/verdict/maximize/close. On a nowrap row that whole cluster gets squeezed into whatever
width is left after the rest of the row claims its own — measured as little as **65px wide** — and
its own `flex-wrap` then folds the 11 buttons into as many as **11 cramped internal rows** (header
height up to **312px**, vs. a normal ~40-50px) instead of one clean row. Measuring the **trim alone**
(TF_ORDER 7→5, item 1, with the container unchanged) still produced 6-9 rows squeezed into a 77-107px
column — confirming the container had to change regardless of button count, which is why item 4 is a
layout fix on top of, not instead of, item 1.

## What shipped

### Item 1: TF row trimmed to 5m(1D)/1W/1M/1Y/ALL
Dropped `3M` and `3Y` from `TF`, `TF_ORDER`, and the `PIV_TFS` pivot-eligibility map (grep confirmed
`PIV_TFS` never had a `3Y` entry to begin with). `TF_3Y_REASON`/`_3yCache`/`_probe3Y`/`_apply3Y` and
the 3Y depth-gate branch in the EOD URL builder are left in place, now unreachable (no button can set
`_tf` to `"3Y"` any more) — the card's own scope note allows this as optional cleanup, not required,
and `_apply3Y`'s `querySelector('[data-tf="3Y"]')` already null-guards, so nothing breaks.

### Item 2: `open()` defaults to 1M
`_tf = "3M"` → `_tf = "1M"` in `open()`. The module-level `var _tf = ... "3M"` initializer was also
updated to `"1M"` for consistency — confirmed inert until `open()` runs (grepped every `_tf` read in
the file; the only entry point is `open()`, which unconditionally overwrites it before any paint or
load happens), so this is a same-intent consistency fix, not a second behavioural change.

### Item 3: genuine rolling 100-bar window, both intraday paths
Neither backend endpoint exposes a bar-count param — checked directly, not assumed:
`/api/intraday/{symbol}` (`gvm_market_endpoints.py`) only takes `sessions`/`days`; `/api/chart/oneday/
{symbol}` (`chart_oneday_endpoints.py`) is a deliberately single-purpose "latest session, Yahoo only"
endpoint whose own docstring says no caller grows its own params. Per do_not_touch's own preference,
**client-side trim** was used for both, applied at the one point the two sources already converge:
```js
data = (rows || []).map(...).filter(function (d) { return isFinite(d.close) && isFinite(d.time); })
  .slice(-100);
```
The Fyers fetch URL also changed `?sessions=5` → `?sessions=3` (real DB check below: still >2x the
100-bar target in the normal case, while cutting the fetch nearly in half). The Yahoo path's own
session-filtered payload is normally ~72-75 bars (one NSE session), so the same `.slice(-100)` is a
safe no-op there in the ordinary case and only trims defensively if it's ever fed more.

Confirmed pan/zoom auto-backfill (`_backfillCheck`, cc#1492) cannot be affected: it already bails
unconditionally on 5m (`if (span == null || ...) return;`, `TF["5m"] === null`) — read directly, not
assumed — so the 100-bar cap has no interaction with backfill at all.

**Real evidence, not assumed** — RELIANCE, queried directly against `intraday_prices`:

| Check | Real result |
|---|---|
| `sessions=3` bar count (today 15-Sep + 11-Sep + 10-Sep, weekend correctly skipped) | **217 bars** |
| "last 100" window (= what `.slice(-100)` on the ASC array produces) | **2026-09-11 12:55 → 2026-09-15 15:10** |
| Sessions spanned by that 100-bar window | **2** (a genuine rolling window, not aligned to session boundaries) |

A caption bug directly on the line being touched for the bar-count wording was also fixed: the
message hardcoded `"(F&O feed)"` unconditionally, which was already wrong for a Yahoo-sourced (non-
futures-universe) symbol — confirmed by grepping `viaYahoo`, already computed in scope. Now reads
`"5-min · last 100 bars · IST (Yahoo)"` or `"... (F&O feed)"` correctly per source.

### Item 4: TF-row clipping fixed at the root cause
`#scorrChartHead` gains `flex-wrap:wrap` (was `nowrap`); `#scorrChartTfs` gains `flex-basis:100%`.
The instant the button cluster cannot share the first line with the title/H-L/verdict, it now drops
cleanly to its own full-width second line instead of being squeezed into a narrow column — standard
behaviour once the parent allows wrapping at all. Verified by **re-measuring the real, now-edited
`_buildModal`/`_paintChrome` code**, extracted from the committed file a second time (not the
before-edit copy), across 360/390/412px and both content states — **30/30 checks pass**:
- header collapses from up to 11 internal rows / 312px tall to a consistent **2 rows / ≤159px tall**
- the button cluster spans the full **~294-357px** card width instead of a squeezed 65-107px column
- **zero clipped buttons** in every width × content-state combination tested
- Pivots tooltip text updated `"1D–3M"` → `"1D–1M"` to match the new `PIV_TFS` range — verified live:
  shows the normal description on a pivot-eligible TF (1M) and the updated blocked-range text on a
  pivot-blocked one (ALL), not the stale range.

## Verification
- `node --check` clean on the full file.
- Theme fallback ratchet (`.js` is in scope): origin/main=1 → local=1, **delta 0**. Raw-primitive
  ratchet does not gate `.js` files at all (`theme_validator.gate()`: only `.css`/`.html`) — confirmed
  by reading the gate function directly, not assumed; informational count unchanged anyway (30→30).
- **Real-browser test (Playwright) against the actual committed code**, extracted verbatim via
  brace-matching both before writing the fix (to reproduce the bug) and after (to prove it):
  36 total assertions, **36/36 pass** — TF_ORDER is exactly `5m/1W/1M/1Y/ALL` with no `3M`/`3Y`
  button anywhere in the DOM, exactly the `1M` button renders active (matching `open()`'s new
  default), every surviving TF button keeps a real click handler, header/cluster geometry measured
  clean at 3 widths and 2 content states.
- **Real production data (`intraday_prices`, RELIANCE)**: confirmed `sessions=3` and the 100-bar
  trim produce a genuine, correctly-ordered rolling window spanning 2 real sessions (table above).

## What did NOT change
`/api/candles`/`/api/intraday` endpoints themselves — client-side trim only, per do_not_touch.
GVM overlay logic, Fib/Channel toggle behaviour, the peer pane (cc#2102, separate) — only the shared
layout *container* they render inside changed (flex-wrap/flex-basis), never their colours, click
handlers, availability rules, or the GVM pillar sub-row beneath them. `_probe3Y`/`_apply3Y`/
`TF_3Y_REASON`/`_3yCache` — left in place, deliberately unreachable, per the card's own optional-
cleanup note.

## Flagged, not fixed (do_not_touch's own note)
`v8_dashboard.html` has its own separate `qaChart` implementation with its own TF row — untouched,
explicitly out of scope, and it will now diverge from this card's 5-item row and clipping fix unless
a follow-up task updates it too.

## Live checks still needed (Arpit)
Open the chart card on a real phone: TF row should read as one clean strip (5m/1W/1M/1Y/ALL) with no
overlap or cramped stacking, land on 1M by default, and the 1D pill should visibly show a tighter,
faster-loading window than before.
