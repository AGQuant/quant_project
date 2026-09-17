# cc#2187 — /m/options: result values readable on the light theme; a +/- lots stepper per leg

Founder 17-Sep 14:53 IST (voice + screenshot of `/m/options` after Calculate payoff, light theme): the three summary tiles and the payoff table's first column were unreadable; typing lots is clumsy. Built 17-Sep-2026 from 15:02 IST server time (the Started line); checks up to 15:09 IST. The landing time is in the task row. One file: `mobile/options.html` (page CSS + one page-local script). The shared `option_strategy.js`, the payoff maths, the chain source, the card layout, the chart and the table are untouched.

## Diagnosis (evidence before the fix)

`theme_validate` on `mobile/options.html`: 0 raw primitives, 0 literal fallbacks (file unmeasured, no baseline) — so not a literal. Reproduced instead with the page served exactly as production serves it: the template, then main.py's `_MOBILE_HEAD` block injected before `</head>` (the legacy `/static/mobile.css`, `scorr_theme_r5.css`, the shared scripts), on aquawhite with a stubbed chain and payoff. Computed styles after Calculate:

| element | light theme before | matching colour rules |
|---|---|---|
| `#optBE`, `#optMaxP`, `#optMaxL` (the tiles) | `rgb(234, 240, 250)` = `--txt` (chalk) | none on the element — inherited |
| `.tr span:first-child` (the price column) | `rgb(234, 240, 250)` | none — inherited |
| `.tr .l` (a per-leg loss cell) | `rgb(192, 39, 58)` = `--loss` | the page's own `.tr .l` |
| `.leg .lots` (an input) | `rgb(14, 26, 32)` = `--ink` | the page's own `.in{color:var(--ink)}` |

So every element the page colours explicitly is right, and every element that inherits gets `--txt`: the injected legacy stylesheet (loaded after the page's `<style>`) paints the document with `--txt`, a token only the dark sets define (`.screen,.bnav{--txt:#E9EEFB…}` in the legacy sheet, `--txt:var(--r5-chalk)` at `:root` in R5); the app's light sets (`aquawhite`, `blush`, `duskday` …) define `--ink` but no `--txt`, so the inherited text fell through to the dark sets' chalk on a pale cell (contrast about 1.1:1). Same family as cc#2166 / cc#2182: a dark-only token reached through inheritance. The spot value at the top (`.spot .optv`) had the same fault.

## The fix

1. **Text colour, tokens only**: `.pg{color:var(--ink)}` on the page root, and explicit `color:var(--ink)` on `.res` (the result card), `.cell .optv` (the three tiles), `.tr` and `.tr span:first-child` (the table, its price column; the header row keeps `--muted`) and `.spot .optv`. No literal; no change to the win/loss cells.
2. **Lots stepper per leg**: the shared script keeps rendering its `<input class="in lots" min=1 max=50>`; a page-local script wraps each fresh input (MutationObserver on `#optLegs`, since the shared script re-renders the legs on every edit) in `[−] value [+]`. The buttons change the value and dispatch the same `input` + `change` events the shared handler already listens to, so the lots rule stays in one place (1..50, `option_strategy.js`); minus is disabled at 1, plus at 50; the value stays typeable, and a typed value outside 1..50 settles to the clamped number on change. The legs grid columns widen the Lots column (`40 40 1fr 62 98 18` px) so the stepper fits at 375 px; the header cell reads "Lots / 1 lot = 75", filled from the page's own `#optLot` (the existing lot-size hint). Calculate payoff stays the trigger — nothing recalculates on a step. The web page (`scorr_options.html`) shares the script and is unchanged: it has no `#optLegs` observer, so it keeps its plain number field.

## Harness (Playwright, real Chromium, `scratchpad/cc2187_test.py`) — ALL PASS at 375×812, aquawhite + goldnight, page served as production serves it

Stubs: `/api/options/meta` (NIFTY, lot 75, ATM 22,450), `/chain` (three strikes), `/templates`, `/payoff` (returns max profit 11,250 × total lots, max loss 20,150 × total lots, break-evens 23,140 / 23,760 — a stub, so the tiles can prove they scale with the lots sent). Two legs added, Calculate tapped.

- Light and dark: the three tiles and the price column compute to `--ink` (`#0E1A20` light / `#F5F2EA` dark); contrast 15.7:1 (tile on its cell fill) and 17.7:1 (price on the card) on light, 15.3:1 / 16.6:1 on dark.
- A stepper on each of the two legs, both at 1; buttons 44 × 44 px; minus disabled at 1; the hint "1 lot = 75" in the Lots header; the stepper inside the card, no sideways overflow (row 341 px).
- Tap + three times on leg 1 → the input reads 4 and the shared state's `legs[0].qty` is 4; minus → 3. Typing 7 on leg 2 (committed on change, as a keyboard does) → qty 7; 50 → plus disabled; a typed 60 → 50 in both the state and the input.
- Calculate with lots 3 + 1: the payoff request carries `qty` 3 and 1; the tiles read ₹45,000 / ₹80,600 (= 4 × the stub's per-lot figures) — the numbers scale with the stepped lots.
- No page errors on either theme.

**Screenshots looked at** (`scratchpad/cc2187_*.png`): `375_light_before` — the founder's picture: the BREAK-EVEN / MAX PROFIT / MAX LOSS tiles with their numbers invisible on the pale cells, the Spot value faint, the lots as plain "1" fields. `375_light_after` — "23,140.0 and 23,760.0", "₹45,000", "₹80,600" in dark ink, the Spot value dark, each leg with − 1 + in aqua on the light theme, the Lots header carrying "1 lot = 75". `375_dark_after` — the same on goldnight with gold buttons and chalk numbers. `375_light_legs` — the legs card close-up: BUY · CE · 22,450 ▾ · 150 · [−] [+] · ×, the premium in the chain-filled brand colour.

## Checks

- `node --check` on both inline scripts: OK. Literal `var(--x, #hex)` fallbacks: 0; the only hex in the file is the pre-existing `<meta name="theme-color">`. `theme_validate` runs on the deployed file after landing — its result is in the task row. No NAV change.

## Live check (Fable — the sandbox cannot reach scorr.in)

`https://scorr.in/m/options` on a phone, light theme: two legs, Calculate → the three tiles and the NIFTY-at-expiry column readable; tap + on a leg → lots go up one at a time, the Calculate button recomputes with the new lots. Dark theme unchanged apart from the stepper.

## Out of scope (by spec)

Payoff maths, chain premium source, card layout and chart.
