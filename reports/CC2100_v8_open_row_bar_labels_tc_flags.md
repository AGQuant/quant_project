# cc#2100 — V8 OPEN POSITION ROW: entry/CMP labels onto the bar, freed row → TC capsule + fired-flag chips

## What shipped

### Items 1-3: ENTRY (and CMP) price labels move onto the rail
Reintroduces `trkTickStyle`/`trkLabels`/`trkFixLabels` — the exact mechanism cc#1726 built and
cc#1868 item 6 later removed once the labels themselves were gone ("CMP is the marker itself...
Entry becomes a faint tick, unlabelled"). The founder is now asking for the labelled version again
directly — **not a regression of cc#1868**, a deliberate reintroduction, stated as such in the code.
Bodies restored **verbatim** from git history (`195732b`, `dd7a61b`) rather than re-derived, since
that collision-avoidance was already measured and proven:
- `trkTickStyle(f)`: positions a label at its fraction of the rail, anchoring to an edge instead of
  centering within 15% of either end so it can never bleed off the card.
- `trkFixLabels(root)`: runs once after render, measures the real rendered ENTRY/CMP label text
  boxes (via `Range`, not the element box — a CSS rule shrinking the box while text still overflows
  was the original cc#1726 bug) and drops ENTRY below the rail onto the SL/TGT row when it would
  touch CMP, then nudges it clear of whichever end label it would touch there too.
- The old "within 12% of track → one merged label" branch is **not** restored — cc#1726's own
  commit already replaced it with always-render-both + `trkFixLabels`, because a percentage
  threshold guesses text width and was the original overlap bug on the founder's screenshot.
- The `(last close)` qualifier (item 3) moves onto the CMP label itself, shortened to "LC" to fit
  the rail rather than silently disappearing with the removed price-text row.

**Item 2 (CMP also gets a label) — Fable's own default, flagged as such in the spec, not an
explicit founder instruction.** Implemented per that default (both ENTRY and CMP labelled) since
the spec states the reversion path if wrong ("if the founder only wants ENTRY labelled... this
reverts to entry-only") — noted here again for confirmation, per the card's own instruction.

### Items 4-6: the freed row — TC capsule + fired-flag chips
- **TC capsule**: `fnTcCapsule(mkTc(p).score_pct)` — reuses cc#2099's own capsule renderer and
  `fnTcBand` bands (STRONG/VALID/WATCH/FAIL, same locked thresholds) verbatim, fed by `mkTc(p)`
  (already defined, already reading `MK.tcRows` from `/api/trade-check/position-stars-v2`, no new
  fetch). Dash state when `mkTc(p)` returns null — same honesty rule as cc#2099.
- **Fired-flag chips**: `mkFiredChips(p)`, new, but built entirely on existing reads —
  `mkKeys(p)` (already computed for the flag glyph) filtered against a new shared array,
  `MK_CHIP_DEFS`. `renderMarkerChips()`'s own `chip()` closure was refactored to source its
  icon/colour/title from that **same** array instead of retyping them a second time — one
  vocabulary, two consumers, not a second definition (spec item 5's explicit requirement).
  Verified this refactor is behaviour-preserving (below).
- Item 6: `mkFlag(p)` (the glyph next to the symbol) and its tap target (`mkOpen`, the full
  marker-detail sheet) — untouched. The new chips are a glanceable summary, not a replacement.

## Token gate — caught before shipping (same discipline as cc#2099)
First draft of `.mkrow`'s `font-size:11px` (matching `.pmeta`'s own existing convention) was a raw
length the ratchet tracks. Fixed with `var(--type-11)` (already defined in `scorr_themes.css`,
already linked by this file). Re-verified delta 0 against `origin/main`.

## Verification
- `node --check` clean on the full extracted inline script.
- **Reuse, not duplication** (grep): `mkTc`, `mkFired`, `mkKeys` each have exactly one `function`
  definition; `mkTc`/`mkKeys` each now have two call sites (their pre-existing one plus this
  card's), never a second definition.
- **Raw-primitive + fallback ratchets**: computed directly with `theme_validator.count_raw`/
  `count_fallbacks` against `origin/main`'s real committed content — delta **0** on both.
- **Real-browser test (Playwright), real production data** — 6 real open positions
  (`v8_paper_positions` × `cmp_prices`, symbols 360ONE/ADANIPORTS/ASHOKLEY/BANKINDIA/BHARTIARTL/
  BOSCHLTD) run through the actual committed `trkAxis`/`trkFrac`/`trkTickStyle`/`trkLabels`/
  `trkFixLabels`, extracted verbatim (brace-matched from the committed file, not retyped):

  | Symbol | ENTRY/CMP collided? | Result |
  |---|---|---|
  | 360ONE | yes (real prices land close) | ENTRY correctly dropped below, no overlap |
  | BHARTIARTL | yes | ENTRY correctly dropped below, no overlap |
  | ADANIPORTS, ASHOKLEY, BANKINDIA, BOSCHLTD | no | both labels stayed above, no overlap |

  22/22 assertions pass, including two **real** collision cases the fix had to actually resolve,
  not contrived ones.
- **Real TC data cross-check**: ADANIPORTS's real `tc_position_stars_v2.best_any_score10` = 6.2 →
  `mkTc`'s own formula gives `score_pct` = 62.0 — which **exactly matches** the real
  `tc_universe_ticks.score100` = 62.0 for the same symbol found independently in cc#2099's own
  spot-check earlier this session. Rendered capsule: `fn-tc-watch`, "62.0" — correct band, correct
  number. BOSCHLTD (no real position-stars row available in this pass) correctly renders the plain
  dash, never a fabricated score.
- **Refactor equivalence**: the pre-cc#2100 hardcoded `chip()` calls and the post-refactor
  `MK_CHIP_DEFS`-sourced version produce **byte-identical** HTML for the same inputs (Node test) —
  confirms the filter-row's existing behaviour is unchanged. `mkFiredChips` tested against
  constructed flag-key fixtures (clearly labelled as constructed, since real `MK.stars/act/dma`
  data wasn't queried this pass): zero flags → zero chips (expected, not a bug), one flag → one
  chip, two flags → two chips in `MK_CHIP_DEFS` order, labels correctly HTML-escaped.

## What did NOT change
SL/TGT end labels and the bar's fill/colour-by-P&L-sign logic. `mkFlag`/`mkOpen`/
`ScorrMarkerDetail`. `mkTc`, `MK.tcRows`, `TC_CANON_V2_FINAL` — read-only reuse, no new endpoint.
`closedRow()` / CLOSED position rows — untouched, this card is OPEN positions only.

## Precedent tension (flagged, per the card's own instruction)
Same TC-on-a-non-Home-surface exception cc#2099 already got explicit founder direction for on this
page — flagged again since this is a second, separate spot, not assumed to inherit that exception
silently.

## Live checks still needed (Arpit)
Entry (and CMP) labels legible at 360-390px, no collision with SL/TGT. Old price-text row gone,
row visually shorter. TC capsule + fired chips render correctly for a few real rows. Confirm the
CMP-label default (item 2) is wanted, or say entry-only and it reverts.
