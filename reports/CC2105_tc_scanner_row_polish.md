# cc#2105 — TC Scanner row polish: combined header, sub-line capsule, mockup-directed polish

## Ground truth first
Read the founder-approved mockup's own raw HTML/CSS (not a screenshot description) via the
Artifact tool before writing anything, and diffed it against `mobile/tcscan.html`'s current
`positionRow()`/`draw()` and the shared `scorr_position_row.js`. Confirmed directly, not assumed:
- `record.BUY`/`record.SELL` already carry real `wins`/`closed`/`wr_pct` fields
  (`scanners_app_mobile.py`'s own `_record()`, the exact function `/api/mobile/tcscan` calls) — the
  mockup's "1 of 7" / "14.3%" / "60.9%" stat-cell figures turned out to be **real record numbers**
  as of when it was built, not invented, confirmed by re-deriving them from `tc_scanner_holds`
  directly (same SQL as `_record()`): BUY closed=7/wins=1/wr=14.3%, SELL closed=23/wins=14/wr=60.9% —
  exact match to the mockup.
- `.scorr-prow .v`/`.hd`/`.sub`/`.kg`/`.cell`/`.sec h2`/`.hero .eye`/`.chip` are ALL declared in
  `mobile/tcscan.html`'s OWN `<style>` block — only `scorr_position_row.js`'s own JS-injected
  `<style data-scorr="position-row">` (the `.trk`/`.fn-tc`/`.rowstrip` rules) is genuinely shared
  with `mobile/v8.html`. Confirmed before editing anything, so every CSS change below is provably
  scoped to this one page.
- `.chip` (the sort control AND the day-picker chips both use it) already renders in normal case
  with no `text-transform`, and out_of_scope explicitly protects the day-picker — so item 3c's
  `.chip` mention needed no change at all; stated here rather than invented one.

## What shipped

### Item 1: pct + rupee/lot on one line
`.scorr-prow .v` becomes `display:flex;align-items:baseline` (was two stacked block divs) — no
markup change needed, `.n`/`.c` already were two children of `.v`. Rupee/lot now wraps in parens,
`(+₹23,500/lot)`, matching the mockup's `(+₹27,500/lot)` shape exactly (was `+₹23,500 / lot`, no
parens).

### Item 2: TC capsule moves into the sub line
The sub line now leads with the CURRENT marker only — `cmp <b>157.45</b> · 11 Sep, 10:15` — instead
of restating `in 162.15 → now 157.45 · ...`. Reasoning, not a blind mockup copy: entry price is
**already on the bar** (`tcTrkLabels`' own ENTRY tick, untouched by this card, present both live and
closed), so repeating it in the sub line was the same number twice; dropping it matches the
mockup's own lean `cmp 156.65 · timestamp` shape for the reason the mockup itself had it lean. The
capsule (`SPR.fnTcCapsule(r.score)`) moves out of `mkrow` into `.sub`, wrapped in a `.tcw` span with
`margin-left:auto` (the mockup's own `.tcscore{margin-left:auto}` idiom) so it sits pinned right.
`mkrow` now carries only the exit-reason tag — unaffected, per do_not_touch.

**Closed-row case, stated explicitly (mockup never depicted it):** a closed row's sub line reads
`out <b>255.79</b> · 15 Sep, 14:10` (real VEDL TARGET exit, tested below) — same shape, `out` instead
of `cmp` since there is no live price on an exited position. Capsule and exit-reason tag both still
render (capsule in `.sub`, tag in `mkrow`, same as the open case).

### Item 3a: two-tone win/loss background tint on the trk bar
New `.zone.win`/`.zone.loss` elements, inserted first in `positionRow()`'s trk markup (before
`.tfill`), split at the entry fraction. `trkAxis()`'s own construction (`ax.lo/ax.hi` = exactly
`{target, sl}`) means the SL fraction is always exactly 0 or 1 by construction — confirmed with real
ASHOKLEY numbers (target 157.29, sl 167.01, entry 162.15 → entry sits at exactly the 50% mark),
never assumed. Colour: `color-mix(in srgb, var(--win) 14%, transparent)` /
`...var(--loss) 14%...` — the *exact same* idiom `scorr_position_row.js`'s own `.fn-tc-strong`
already uses for a pale token-derived tint, so no new hex literal and no new CSS pattern. Layered
behind `.tfill`/`.z`/`.cmp`/`.lbl` by DOM order alone (none of them carry a z-index, so earlier-in-
DOM paints first) — verified directly in the rendered DOM, not assumed. `tcTrkLabels()` itself —
SL/TGT/ENTRY/CMP labels — was never touched; only new elements were added around it.

### Item 3b: `.kg` stat cells flattened to single-row-with-dividers + win-rate bar
`.kg` is now one flex row with ONE outer background/border/radius (was `display:grid` with 3
individually-boxed `.cell`s); cells are `flex:1` with a `.cell+.cell{border-left}` divider instead
of a gap, matching the mockup's `.stats`/`.stat` shape. Each cell now shows a value line + a real
sub line (`wins of closed`, e.g. "1 of 7") + (Long/Short only) a thin win-rate bar filled to the
real `wr_pct`. Net pts gets a real sub too ("N signals", total closed both sides) but **no bar** —
a point total has no 0-100% ratio to visualize, per the card's own note. Net pts keeps the
**existing sign-aware `cls(net)` colouring** rather than the mockup's own hardcoded-green sample —
its one demo figure happened to be positive, so the mockup's CSS never had to handle a red Net pts;
porting that unconditionally would have been exactly the "mockup sample data taken as a rule"
mistake this card's own evidence section already warns about for the TC score scale.

### Item 3c: section labels normal-case, lighter weight
`.sec h2` / `.hero .eye` / `.cell .k` drop `text-transform:uppercase` (their HTML text was already
written in normal case — "Open", "Book", "Long record" — so removing the CSS transform is the whole
change) and go from `font-weight:800` to `700`/`600`, matching the mockup's un-bolded, un-capsed
`.open-head .title`/`.stat .lbl`. `.chip` needed no change (see Ground truth above).

## Verification
- `node --check` clean on the extracted inline scripts.
- Theme ratchets computed directly against `origin/main` (not the possibly-stale MCP snapshot):
  fallback 0→0 (delta 0), raw 1→1 (delta 0, `.html` files ARE raw-gated, unlike a `.js`-only change)
  — every colour introduced reuses an already-declared token, never a new one, so the separate
  theme set-completeness check cannot regress either.
- `git diff --stat`: **only `mobile/tcscan.html` changed** — `scorr_position_row.js` and
  `mobile/v8.html` are untouched, confirmed by the diff itself, not assumed.
- **Real-browser test (Playwright), real production data** — 3 open rows (ASHOKLEY/RELIANCE/ICICIGI,
  real entry/target/sl/score from `tc_scanner_holds`, real `cmp` from `intraday_prices`' latest close,
  real lot sizes from `futures_universe`, pnl_pct/pnl_rs computed with the exact formula
  `tc_scanner_endpoints.py`'s `_rs()`/pnl_pct use) + 1 real closed row (VEDL, real TARGET exit) + the
  real BUY/SELL record stats (re-derived from `tc_scanner_holds` with `_record()`'s own SQL) — run
  through the actual committed `positionRow()`/`statCell()`/`tcTagRes()` and the real
  `scorr_position_row.js`, extracted verbatim via brace-matching. **31/31 assertions pass**,
  including: pct+rupee/lot genuinely overlap on one baseline-aligned line; the real pnl_rs renders
  as `(+₹23,500/lot)`; the capsule is gone from `mkrow` and present in `.sub`, pushed right; the real
  score (89) renders via the unmodified `fnTcCapsule` as `89.0` (confirming the 0-100 scale was
  never touched); the closed VEDL row reads `out 255.79 · ...` with no `cmp`; the two zone elements
  sit at the geometrically-correct 0-50%/50-100% split and precede `.tfill` and every `.lbl` in DOM
  order; their computed alpha is exactly 0.14; the three stat cells share one background with a
  divider (not three boxes) and real win-rate bars at 14.3%/60.9%; Net pts has no bar; section/label
  text-transform is confirmed `none` in the rendered DOM, not just in the source.

## Flagged, not fixed (out_of_scope's own instruction)
The founder's original screenshot appeared to show larger, boxier sort-chip buttons; this file's
current `.chip`/`.sortbar` CSS already renders small pill chips (`padding:4px 9px`,
`border-radius:20px`) and this card made no change to that CSS at all. Stated here per the card's
own instruction rather than guessed at or silently changed — if live rendering on a phone still
looks boxier than this file's CSS implies, something else (a cache, an override elsewhere) is the
cause, worth its own look if it persists.

## What did NOT change
`scorr_position_row.js` and every page-agnostic primitive in it (`trkAxis`/`trkFrac`/`trkTickStyle`/
`trkFixLabels`/`fnTcBand`/`fnTcCapsule`/`cardStripToggle`) — `mobile/v8.html`, which shares that
module, is untouched by construction (confirmed via `git diff --stat`, not just by not having
opened the file). The real TC score scale/bands/thresholds. `tcTagRes`'s own rendering and its
placement in `mkrow`. The Book segmented control and the Closed-on day-picker chips. `tcTrkLabels()`
itself — SL/TGT/ENTRY/CMP label text, position and format are byte-for-byte the same code as before
this card.

## Live checks still needed (Arpit)
Open `/m/tcscan` on a real phone with a genuinely live position: the header should read as one
line, the sub line should show `cmp · timestamp` with the score capsule pinned to the right edge,
the price rail should show a soft green/red tint split at your entry price, and the three stat
cells at the top should read as one flat strip with thin dividers instead of three separate boxes.
