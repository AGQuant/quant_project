# cc#2023 — D-cockpit: drop empty OI columns on a stock chain, shrink the FUTURES BASIS chip

Founder screenshots, 12-Sep-2026 10:12 IST, `/m/home`. (a) Option chain popup for LTM (a stock):
22 strike rows, both OI columns show an em-dash on every one — 44 empty cells, with the same "no
OI here" fact already stated three separate ways (header note, footer legend, the dashes
themselves). (b) D-cockpit section 04 FUTURES BASIS: the DISCOUNT/PREMIUM chip renders as a large
rounded pill roughly twice the height of section 05's own strength capsules directly beneath it.

## Item 1/2 — the two OI columns, gated on the one flag already in scope

`_dcRenderChainGrid()`'s row builder and `<thead>` both now emit the OI `<td>` only when
`d.oi_available` — the SAME flag `oiNote` (header line) and `_dcChainLegendHtml(d.oi_available)`
(footer) already consume; no second test introduced. A stock chain now renders three columns
(CE LTP · STRIKE · PE LTP); the index path (`d.oi_available === true`) is untouched — same five
columns, same wall/max-pain row backgrounds.

**Verify, real Chromium, the REAL `scorr_cockpit_card.js` driven through its own real
`dcFetchStrikes()` → `_dcRenderChainGrid()` path** (a stubbed `getJSON` returns fixture chain data
in place of a network call — no `_dcRenderChainGrid` reimplementation):

| | header | row `<td>` count | notes |
|---|---|---|---|
| STOCK fixture (`oi_available:false`) | `CE LTP · STRIKE · PE LTP` | 3 | "index only" note + legend both still present |
| INDEX fixture (`oi_available:true`) | `OI · CE LTP · STRIKE · PE LTP · OI` | 5 | unchanged; `PCR 1.28` note; call-wall/max-pain/put-wall row backgrounds all present |

16/16 checks pass, including the wall/max-pain colours (`color-mix(in srgb, var(--c-red)/#FF9F45/var(--c-grn) ...)`) rendering exactly as before on the index path.

## Item 3/4 — the FUTURES BASIS chip, retargeted onto `.lvv`

`scorr_cockpit_card.js`'s `_chipCls` now maps `b.tag_color` to `.lvv`'s own class vocabulary
(`grn`/`red`/`amb`) instead of the chip-only `''`/`dn`/`neu` shorthand — **the colour decision
itself is unchanged** (same server-driven `b.tag_color`, same PREMIUM/DISCOUNT fallback, cc#624
item_2 untouched), only which CSS class name carries it. The markup emits `class="lvv ${_chipCls}"`
in place of `class="chip ${_chipCls}"`. The now-unused `#dcOv .chip`/`.chip.dn`/`.chip.neu` rules
are removed from `_dcInjectStyle`'s CSS (grepped: `class="chip` had exactly one call site in this
file, now zero); the `flex-shrink:0` guard that used to read `.basis .chip,#dcOv .chip` now reads
`.basis .lvv` — scoped to the chip's own row only, so section 05's other `.lvv` badges (unaffected
by this card, per do-not-touch) keep their existing behaviour exactly.

**Verify, real Chromium**: built a live `.lvv.grn` element inside a real `#dcOv .basis` and read
`getComputedStyle` — `font-size: 9.5px` (was `.chip`'s 11px), `border-radius: 5px` (was 20px),
`padding-top: 2px` (was 5px) — the box now measures exactly as `.lvv` specifies, not as `.chip`
used to. `_dcInjectStyle`'s own CSS confirmed (comments stripped before checking) to carry no
`.chip{`/`.chip.dn{`/`.chip.neu{` rule any more.

## Item 5 — the `.basis` row's wrap, measured before AND after, not assumed

The chip shrink was expected to free horizontal room. Measured the REAL `_dcRender()` output (not
hand-built markup) at 390px, same representative fixture (`basis.value = -33.1, tag_color: 'red',
tag: 'DEEP'` — **not the founder's own live figures**, this container has no route to scorr.in;
same general shape: a negative two-digit-plus-decimal value with a DISCOUNT/DEEP tag), against
both the pre-cc#2023 committed file and the fixed one:

| | chip box | `.v` ("-33.1") box | line boxes (precise, `Range.getClientRects()`) |
|---|---|---|---|
| **before** (`.chip`) | 130.8 × 25 px | 37.2 × 84 px | **3** |
| **after** (`.lvv`) | 109.3 × 17 px | 43.2 × 56 px | **2** |

**The value still wraps at 390px** — improved from 3 lines to 2, not eliminated. Root cause (read,
not guessed): `.basis .v` has no `flex-grow`, so freeing space elsewhere in the row does not
enlarge it — flexbox only redistributes freed space to items that ask to grow. Separately,
`overflow-wrap:anywhere` (cc#621) combined with `.basis>*{min-width:0}` lets the browser's flex
sizing pass allocate `.v` far less than its unbroken-text width needs, since "breaking anywhere" is
treated as an available layout option during intrinsic sizing — this is *why* a narrow flex child
with these two properties wraps even when its siblings and the row overall have slack (measured:
row width 324px, the three children's boxes sum to ~264–300px depending on version — genuine spare
width exists, it simply isn't reachable by `.v` under the current flex rules). Per the card's own
instruction, **not fixed here** — reported with numbers, since "a second guard stacked on the
first is how that row got fragile" (cc#621's own history).

## Do-not-touch, confirmed

Index chain path byte-identical (5 columns, wall/max-pain colours, `MP_AMBER`); `_dcTagDot`, the
ivp tag banding, and `_dcChainDetailHtml` (still shows OI + its not-tracked lines even on a stock —
a per-leg detail view is a different question from a column of dashes) all untouched; `b.tag_color`
and the server-side tag derivation untouched — only the box the words sit in changed; sections 01,
02, 03, 05 and the cc#621 guards on other rows: untouched.

## Verify

`node --check scorr_cockpit_card.js` — clean. Two real-Chromium test harnesses, 20 checks total
(16 OI-gating + chip-metrics, 4 basis-row before/after measurements), all against the actual
shipped functions (`dcFetchStrikes`, `_dcRenderChainGrid`, `_dcRender`, `_dcInjectStyle`) — none
reimplemented.

## Not done here (the card's own FOUNDER-ONLY items)

A live open of the chain popup on a stock and on NIFTY, and the D-cockpit on a futures name — this
container has no route to scorr.in, stated in the card itself, not worked around.
