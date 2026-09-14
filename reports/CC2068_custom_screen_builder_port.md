# cc#2068 — port the web Custom Screen Builder to /m/v10

Founder ask, 14-Sep-2026: on the WEB Index Intel tab, a custom table builder over OI/order flow/
volume exists and isn't in the app. Add it to the app, as the last section of the page.

## Step 1 gate — one correction found before touching anything

The card's own file path (`mobile/v10_signal.html`) does not exist. Confirmed the real, routed
file directly: `v10_page_endpoints.py` — `GET /m/v10 -> scorr_v10_signal.html` (repo root). This is
the same file cc#2060 already touched this session. Everything else in the card's own
pre-investigation (`CUSTOM_SCREEN_BUILDER_V1`/cc#1822 in `v8_dashboard.html`, `D.build`/`VF.data`
already loaded on the app page, `BU_STATES`/`buNum`/`_sortTh`/`_sortByState` already present) was
re-confirmed accurate by direct read.

## The port

New section `Custom Screen Builder`, inserted as the last section of `render()`'s template,
directly after `QUALITY BULLISH · FLOW + BASIS` (the section that was last before this card) — per
the founder's explicit placement instruction. `#csbwrap` lets picker/sort taps repaint just this
section (`csbRepaint()`), the same in-place pattern `#utrack`/`#qtrack`/`#ftrack` already use for
their own decks; `csbRestore()` runs once in `boot()` before the first `render()` so a returning
reader's picks are already applied on first paint; `vfPaint()` (fired when `VF.data` arrives) now
also calls `csbRepaint()`, since Orderflow depends on that same fetch — without this the CSB table
would show every row as flow-ABSENT until the next unrelated repaint.

**Logic ported near-verbatim from `v8_dashboard.html`'s `CSB` object** (`csbBackbone`/`csbPool`/
`CSB_BASIS_BANDS`/`CSB_VOLR_BANDS`/`csbSave`/`csbRestore`) — same AND-combining rule (a row must
pass every picker that is set; Any/no band picked never narrows), same ABSENT-never-guessed
handling for both Orderflow (a symbol on neither VF list) and the two numeric bands (a null
`basis_pct`/`rvol` cannot satisfy a band it has no value for). Confirmed against the read source,
not memory.

**Zero new fetches for the four filters** — Buildup and Orderflow read `D.build`/`VF.data`, the
same objects the Futures Buildup and Volume Flow decks above already fetch on this page; Basis and
Vol R read fields already inside `D.build`'s own rows (`basis_pct`, `rvol`).

**Column set, per scope item 3's own "at minimum" list**: SYMBOL, CMP (DAY% riding the cell, same
convention `buTable` already uses), OI Δ%, BASIS, Vol R, Vol P, Vol D, WK (shown only when
Orderflow is filtered — only then is every visible row guaranteed to carry a real week figure).
Tap-to-sort on every column via the app's own `_sortTh()`/`_sortByState()` — not a new sort
implementation.

## TC /100 column — scope decision (item 4), stated as asked

**Option (b) taken: not built in this pass.** Every other column here is free — already-loaded
data, no new network call. TC is the one genuine exception: web's own column needs `loadTc() ->
/api/v10/tc_screen -> tc_universe_ticks`, a fetch this app page does not otherwise make. Adding it
would be the only column in this port that costs a real new request, breaking the "additive only,
no new fetch" property everything else here keeps. Named explicitly as a known gap versus web (in
the code comment, the `(i)` guide sheet, and here) rather than silently dropped or silently
fetched — flagged for a follow-up card if confirmed worth the extra request.

## Persistence

Own key, `scorr_csb_filters_v10` — **deliberately not web's `scorr_csb_filters`**, so a founder
using both surfaces in the same browser never has app and web filter state cross-contaminate.
Same shape and same defensive-restore behaviour as web's precedent: a stored value that no longer
names a real state/band option is dropped, never trusted (verified directly — a garbage
`build`/`flow`/`basis` value in storage is confirmed ignored, not just assumed safe).

## Guide sheet and empty state

`csbGuide()` reuses web's `csbGuide()` text verbatim, through this page's own `vfSheet()` shell
(not web's `iiSheet`) — with the TC paragraph removed (not built) and one honesty line added
stating the gap plainly, so a reader of the guide is told the same thing the card asked to be
stated, not left to notice a missing column on their own. Empty-state copy matches web's own
wording exactly: *"No symbol matches this combination right now. Try Any on one picker, or a wider
band."*

## Class names — one deliberate deviation from web, explained

Web's `.csbchip` wraps (`display:inline-block`, no forced equal width) rather than using this
page's existing `.vfchips`/`.vfchip` (flexbox, `flex:1` per child) — checked directly, not assumed:
`.vfchips` already exists on this page for 2-4 SHORT equal-width options (the price-alert form's
KIND/DIRECTION/TRIGGER rows, the Volume Flow ticks filter), but Buildup alone has 5 options with
long labels ("Short Covering", "Long Unwinding") that would truncate forced into equal flex slots.
New `.csbrow`/`.csbchip` classes were added, matching web's own wrapping layout (the correct choice
for this content, proven necessary by web's own CSS) but with this page's own token names/colours
(`.csbchip.on` mirrors `.vfchip.on`'s `border-color:var(--aqua);color:var(--aqua)` exactly) — same
visual language, correct layout mechanism for the content shape.

Table column widths (`.csbtbl`) are proportional splits, stated honestly as such — not a
Chromium-measured-against-widest-real-value split the way `.b6tbl`/`.qbtbl`/`.vftbl` above
document theirs; SYMBOL/CMP mirror `.b6tbl`'s own 19%/25%→23% split since the CMP cell is the
identical shape, the remaining numeric columns split what's left evenly (5-way without WK, 6-way
with it).

**Do not touch, respected** (confirmed by diff after the fact, not just intent): the existing
Buildup/Volume Flow/Quality Bullish decks and their fetches are untouched — the diff shows exactly
two modified lines (the `render()` template's closing section and the `boot()` line), both edited
on purpose to wire this in; everything else in the 237-line diff is pure addition. Web's own
Custom Screen Builder (`v8_dashboard.html`) was only read, never edited.

## Verify

`node --check` clean (all three inline `<script>` blocks). Real headless Chromium, the actual
`CSB`/`csbPool`/`csbTable`/`csbSection`/`csbSave`/`csbRestore` code **extracted verbatim from the
committed file** (self-checked for the expected markers before running, to catch a slicing mistake
before it could produce a false result) plus the file's own real `BU_STATES`/`buNum`/`_sortTh`/
`_sortByState`/`esc`, run against a realistic `D.build`/`VF.data` fixture — **26/26 checks pass**:

- Backbone de-dupe (a symbol present in both `long_buildup` and `short_buildup`, as the real API
  does, counts once).
- A single Buildup filter narrows correctly; **a second filter (Basis band) genuinely AND-combines
  with it** — a symbol matching Buildup alone but failing the Basis band is correctly excluded, not
  OR'd in.
- Orderflow ABSENT is never guessed either way, checked on both sides (Bullish and Bearish).
- A null `basis_pct` cannot satisfy a band it has no value for.
- Clearing every filter restores the full unfiltered set.
- SYMBOL sort: correct ascending order, then a real reversal on a second tap.
- The WK column is absent with no Orderflow filter and present once one is set.
- **The TC /100 column is confirmed genuinely absent** from both the table markup and the section
  HTML — the scope decision was actually shipped, not just written down.
- An impossible filter combination shows the honest empty-state copy, not a blank or a loading
  state.
- The scanned-count footer line reads correctly off the real backbone/universe counts.
- **Persistence round-trips**: save under a realistic selection, simulate a fresh load, restore —
  the exact same picks come back. A garbage stored value is dropped, not trusted. The app's own key
  (`scorr_csb_filters_v10`) is confirmed distinct from web's.
- Zero page errors throughout.

**FOUNDER-ONLY, not done here** (this container cannot render or screenshot the app): confirming
on-glass that the new section appears at the bottom of `/m/v10` and behaves the same way as web's
Custom Screen Builder — the card's own verify section marks this FOUNDER-ONLY.
