# cc#2131 — hide the legacy 5>20 / 5<20 tag pills from the web V8 Dashboard Open Positions row

## Where (task 1)
`v8_dashboard.html`, inside `renderMaster()` → the marker quick-filter group `markerGroup` (line
~3733): `mchip('dmaUp','■','#0a9e63','5>20',…)` and `mchip('dmaDn','■','#f87171','5<20',…)`, the
two DMA-STATE chips cc#1682 split out of the one 'dma cross' chip, sitting between Volume confirm
and the cc#2115 5M>20 / 5M<20 window-cross pair.

## Are they only a display filter? (task 2) — yes, and the logic stays
`dmaUp` / `dmaDn` appear in exactly three places on the page: `markerKeysFor()` (the filter key a
row carries, from `PIVOT_DMA` = the engine's `dma_state` rows via `/api/v8/pivot_star`),
`_mkCounts` (the chip counts) and the two `mchip` calls. `MASTER_MARKERS` is an in-memory `Set`
with no persistence, and a key can only enter it through its own chip. Nowhere else — no alert
rule, no scheduler job — reads these keys; the engine-side DMA tagging (`PIVOT_DMA`,
`ScorrMarkerFlagFilter`) is shared and untouched. `mobile/v8.html` (`/m/v8`) carries its **own
separate copy** of this row (its own `chip()` and family list, lines ~886–917) — the component is
NOT shared, so per the card it is left alone.

## Change (task 3) — display only, hidden not deleted
- `const MKQ_HIDDEN = new Set(['dmaUp','dmaDn'])` next to `MASTER_MARKERS` (with the why).
- `mchip()` returns `''` for a hidden key. The two `mchip('dmaUp'…)` / `mchip('dmaDn'…)` calls,
  the counts and `markerKeysFor()` are byte-identical. Removing a key from the Set brings its chip
  back. 9 added lines, 0 removed; TC strong, S1 reversal, R1 mirror, Volume confirm, 5M>20, 5M<20
  untouched.

Before → after on the row: `★TC strong · ★S1 reversal · ★R1 mirror · ⚡Volume confirm · ■5>20 ·
■5<20 · ↑5M>20 · ↓5M<20` → `★TC strong · ★S1 reversal · ★R1 mirror · ⚡Volume confirm · ↑5M>20 ·
↓5M<20`.

## Verify
`node --check` clean on all 8 inline blocks; 0 new literal fallbacks. Playwright on the REAL
`v8_dashboard.html` with the real web tokens and the **real paper book** (`/api/paper/status`, 12
open positions; the marker endpoints stubbed empty, so every chip count reads 0 — the counts are
not what this card changes), dark and light: after = the six chips in order, no text contains
5>20/5<20; clearing `MKQ_HIDDEN` and re-rendering on the same page restores all 8 with the two in
their old 5th/6th place (the calls are intact); the Open Positions count is unchanged by the hide
(12 → 12); the surviving 5M>20 chip still toggles (`aria-pressed`, "0 of 12"); `MASTER_MARKERS`
holds no hidden key (nothing filters invisibly); no page errors from the pane. Screenshots of the
row before/after looked at (VISUAL_VERIFY_GATE_V1).

## Not touched
Any other chip/pill class (`.ticks`, `.ib`, `.oiib`, the basket chips), the builder logic and
results table, the MATCH badge, `/m/v8`.
