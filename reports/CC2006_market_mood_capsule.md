# cc#2006 — Market Mood/PCR card: "(whole chain)" becomes a tappable capsule

Founder direction 12-Sep-2026 ~08:20 IST, on a Home Market Mood/PCR card screenshot. Depends on
cc#2003 (the option-chain popup), which had landed (`2cfb9c0`) before this card was claimed.

## What this lands

`mobile/home.html`, `pcrMood(hero)` (the Market Mood/PCR card renderer):
- The plain-text `(whole chain)` qualifier next to the PCR value is now `<span class="pcrchip"
  onclick="chainPopupOpen('NIFTY')" role="button" ...>whole chain</span>` — parentheses dropped per
  the spec ("the capsule shape carries the meaning").
- Tapping it opens the SAME popup cc#2003 wires to the Max Pain chart tap (`chainPopupOpen` →
  `scorr_cockpit_card.js`'s cc#2004 chain-grid component) — one implementation, a second entry
  point, per item 3's explicit "do not create a second."
- Symbol hardcoded to `NIFTY`: this card carries no per-symbol toggle (unlike `heroCard3`'s
  `IBK.sym`), and the whole-chain PCR it shows is the market-wide headline reading — the same index
  every other "market mood" figure on this page leads with. Stated as a real assumption in the code
  comment, not verified against a second underlying in `mobile_home2.py`'s own composer — a one-line
  fix if wrong.
- `.pcrbasis` (the old plain-text class) is retired, not left orphaned — its one caller now reads
  `.pcrchip`.

## Item 4 — "reuse the existing capsule/pill pattern," honestly

Checked what already exists before writing new CSS: `.tr2 .pill` (used for LONG/SHORT and BUY/SELL
badges) is the closest real "pill" in this file — same font/weight/letter-spacing/padding idiom, and
the same `color-mix()` background technique — but it is scoped to `.tr2` (would resolve to nothing
outside that ancestor) and coloured green/red for a direction, neither of which fits here. `.chip`
(used for the Retry button) turned out to be a full 44px-min-height tap target from the shared
mobile stylesheet — too big for "keep it SMALL ... must not compete with the PCR number ... do not
reflow onto two lines." Built `.pcrchip` borrowing `.tr2 .pill`'s font/padding/color-mix RECIPE,
recoloured to `var(--mut)` (the spec's own explicit "secondary token colour," not a bullish/bearish
hue), with a border and a fully-rounded radius added on top of that borrowed recipe because the
spec's own words are "capsule ... rounded, bordered" and `.tr2 .pill` itself has neither. Stated
plainly in the CSS comment that this is a recombination of an established idiom, not a byte-for-byte
reuse of one existing selector — no exact drop-in fit existed, so the closest real one was adapted
rather than a new shape invented from nothing.

## Verify

- `node --check` clean (home.html's inline scripts extracted and checked together).
- Grepped the whole file: `.pcrbasis` has zero remaining live references (only this task's own
  explanatory comment mentions the retired name).
- Playwright, against the real file, at 360px (this app's own narrowest reference width, matching
  `tools/render_check.py`'s convention) — the capsule renders exactly once, reads "whole chain" with
  no parentheses, carries `onclick="chainPopupOpen('NIFTY')"`; the gauge SVG, the PCR headline
  number, the (i) button (`pcrRead`) and the DAILY DIGEST link are all still present and unchanged.
- The `.pcite` row measures 20px tall against an ~18px single line-height (`wrapped: false`) —
  confirmed NOT reflowing to two lines at the narrowest width, per the spec's own verify item.
- Tapping the capsule opens `#dcOv` with `hasVolumeSection: false` (the minimal popup, not the full
  D-cockpit) for `NIFTY` specifically; the popup's own close button closes it; `page.url()` is
  unchanged throughout (no route change, no hash) — the spec's own DOM-probe requirement.
- A pre-existing 4px document-level horizontal overflow shows up in this test environment
  regardless of this card's content — reproduced with `hero: {}` (zero Market Mood markup rendered
  at all, `pmood` element count 0) and the overflow was still exactly 4px. Confirmed NOT caused by
  this change before writing this line, not assumed; almost certainly this page's own swipe-deck
  mechanism or an artifact of the synthetic test harness's incomplete mock data, out of this card's
  scope either way.
- Zero console/page errors from this file's own code.

## What this does NOT include

The gauge rendering, PCR computation, mood-label thresholds, the (i) explainer and the DAILY DIGEST
button are all untouched, per do_not_touch. No change to PCR maths or the whole-chain vs near-month
definition, per out_of_scope. Founder glass-check still outstanding — this session's synthetic-data
structural verification is as far as it can go. Card not done — Fable verifies.

## cc#2019 addendum (12-Sep, Sonnet 5 seat) — capsule text, LTP decimals, grid alignment

Founder direction on the Home option-chain popup (screenshots, NIFTY and BANKNIFTY, ~09:28 IST):
the capsule's own text duplicated what the popup it opens already says; LTP rendered at 2 decimals;
the strike-chain table mixed three different column alignments.

**Capsule text (`mobile/home.html`, `pcrMood()`).** The `.pcrchip` span's visible text changes from
`whole chain` to `full chain &rarr;` — a tap hint, not a PCR-basis qualifier (that duplication was
the whole reason for the change: "the popup this opens already states it is the whole chain").
`aria-label` changes from `"Open full option chain, whole chain PCR basis"` to `"Open full option
chain"` — reusing, verbatim, the aria-label this same file already gives the Max Pain chart's own
tap target into the identical popup (`.oib`, line ~3292), rather than inventing new wording for the
same action. Span class, `onclick="chainPopupOpen('NIFTY')"`, and the tap affordance: untouched.

**LTP at one decimal (`scorr_cockpit_card.js`).** `_dcLtpTxt(leg)` now returns
`Number(leg.ltp).toFixed(1)` instead of the raw stored value; the Premium line in
`_dcChainDetailHtml`'s per-leg `leg()` closure gets the identical `Number(o.ltp).toFixed(1)`, so LTP
reads at one decimal everywhere in this component — the grid and the tap-to-detail panel alike.
Display-only: `deriv_metrics._price_rows()`'s own 2-decimal rounding is untouched, per do_not_touch.

**Grid alignment (`scorr_cockpit_card.js`, `_dcRenderChainGrid`).** Every header cell (`hcol` for
OI/CE LTP, the inline STRIKE/PE LTP/OI header cells) and every row `<td>` (OI, CE LTP, STRIKE, PE
LTP, OI) is now `text-align:center` — was right/right/center/left/left. Markup and cell content
unchanged; column order unchanged.

**Verify, this session:**
- `node --check scorr_cockpit_card.js` — clean.
- `grep` inside `_dcRenderChainGrid`'s own header + row templates (lines 458–491): zero remaining
  `text-align:left` / `text-align:right` — every one is `center`.
- The real, shipped `_dcLtpTxt` — extracted from the file itself and run in Node, not
  re-implemented: `279.75` → `"279.8"`, `0` → `"0.0"`, a `null`/absent leg → `"&mdash;"` (all
  correct `toFixed(1)` rounding on the actual function).
- Exact-string match confirms the Premium line's formatting call landed as written.
- `github_read` against `main` post-push resolves both changed files (`PUSH_IS_NOT_DEPLOY_V1`).

**Not done here (the card's own FOUNDER-ONLY item):** a live screenshot of the Home popup —
`CC's container is network-blocked from scorr.in`, stated in the card itself, not worked around.
