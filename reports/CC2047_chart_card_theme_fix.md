# cc#2047 — chart card (C button) always painted dark, even on a light theme

Founder report: on the light/white app theme, tapping the C (Chart) button opens the shared chart
popup in black/dark colours instead of matching the light theme. The card's own two hypotheses:
`_detectTheme()` mis-detecting, or a stray CSS override (`mobile_app.css`'s `:root` blocks)
defeating the probe. **Step 1 (the card's own read-only gate) ruled out both** and found the real,
third cause before any code changed.

## Step 1 — reproduce and report, before touching anything

Real headless Chromium, the actual `scorr_themes.css` + the actual `mobile_app.css`
(`MOBILE_CSS` in `mobile_endpoints.py`) loaded together — the real cascade every `/m/*` page runs:

- `document.body.getAttribute('data-theme')` is set correctly by the app's own before-paint boot
  script (confirmed against the byte-identical script every `/m/*` page carries); `<html>` never
  carries the attribute, so `_detectTheme()`'s own `documentElement`-then-`body` fallback works.
- `getComputedStyle(document.body)`'s `--panel` resolves **correctly** for every theme tested —
  `aquawhite`/`goldday`/`silvergold` → `#FFFFFF` (light), `goldnight`/`dark`/`ainight` → their real
  dark hex. `mobile_app.css`'s five `:root{--panel:#121A33...}` blocks (its own "cc#963/964 ROOT
  CAUSE" comments) do **not** win the cascade against `scorr_themes.css`'s
  `body[data-theme=X]{--panel:...}` rule — confirmed empirically, and consistent with plain CSS
  specificity (an element+attribute selector outranks a bare `:root`).
- `_detectTheme()`'s own exact source, replayed against that same live DOM, returned the **correct**
  dark/light for all six themes tested. Detection was never broken.

Neither of the card's own named candidates held up. **Grepping the whole codebase** for every
literal `theme:'dark'`/`theme:'light'` passed to any `.open()` call found exactly **one**:
`mobile/gvm.html:292` — `window.ScorrChartCard.open(STATE.sym, {theme:'dark', gvm:true})`. This is
a third mechanism the card didn't name: a **caller-side hardcoded override** that bypasses
`_theme = opts.theme || _detectTheme()` entirely, unconditionally forcing dark for every chart
opened from the GVM page, regardless of the app's real theme. No other real call site
(`v8_dashboard.html`, `v10_dashboard.html`, `scorr_holdings.html`, `scorr_result_corner.html`,
`scorr_card_strip.js`'s own dispatcher) passes an explicit theme — every one of them already relies
on the confirmed-correct auto-detection. No comment near the GVM line explains the hardcode; most
likely a stale leftover from before the app's real light-theme rollout, stated as a guess, not a
fact I could verify from the code alone.

## The fix

One line, `mobile/gvm.html`'s own `openChart()`: `theme:'dark'` removed, `gvm:true` kept.
`scorr_chart_card.js` was read to confirm `opts.gvm` only toggles an unrelated internal overlay
flag (`_ov.gvm`, the GVM score line on the chart, cc#1591) with zero interaction with `opts.theme`
— removing one cannot affect the other. `_detectTheme()` and `_pal()` are untouched, because they
were never the defect.

**Scope boundary, stated per the card's own item 3:** this is the card's "(a) minimal fix" —
correct theme resolution, achieved here by removing the one thing that was defeating already-
correct detection. The card's "(b) fuller fix" — retheming `_pal()` to read each of the ~15 live
theme's own tokens instead of a generic two-palette light/dark approximation — is **not** done
here and is flagged as a separate, larger follow-up: detection itself was never broken, and (b)
would be a materially larger change touching every chart-card consumer site-wide for a
nice-to-have, not the confirmed defect.

**Untouched, per `do_not_touch`:** `scorr_card_strip.js` and the C·A·R·D dispatch/availability
logic, chart data/candlesticks/timeframe pills/overlays/peers, and `scorr_themes.css`'s theme
contract (every theme already declares `--panel`/`--field` correctly — no missing declaration was
found, so there was nothing to patch there).

## Verify

`node --check` clean. Real headless Chromium, the actual `scorr_chart_card.js` +
`scorr_card_common.js`, real `scorr_themes.css` + `mobile_app.css`, calling the exact fixed line —
extracted directly from the committed `mobile/gvm.html`, not retyped — **12/12 checks pass**:

- The real file's `openChart()` no longer contains `theme:'dark'` in its actual call (confirmed by
  stripping comment lines first, so the check couldn't be fooled by this report's own prose); still
  passes `gvm:true`.
- `#scorrChartBoxWrap`'s real painted background colour, read via `getComputedStyle` after a real
  `.open()` call: **`aquawhite` → `#ffffff`, `goldday` → `#ffffff`** — the exact fix. **`goldnight` →
  `#1c2536`, `dark` → `#1c2536`, `ainight` → `#1c2536`** — the three existing dark themes render
  identically to before, confirming no regression on the case that already worked.
- Zero console/page errors across all five theme runs.

Not done here, and not needed for this card: the founder's own live check on scorr.in (this
container has no route to the deployed site, the same structural limitation on every UI card this
session). The measured, real background colours above are the closest verifiable substitute.
