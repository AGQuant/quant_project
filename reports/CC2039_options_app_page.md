# cc#2039 — App /m/options page (OPT sprint 4/5)

Built from the same design ref + DOM/API contract as cc#2038, wrapped in the app shell
(`.as-hdr`/`.bnav`, `scorr_appshell.css`/`.js`) instead of the web shell. NAV-COMPLETE per rule 8.

## Shared logic, not duplicated

`static/option_strategy.js` (cc#2038's own file) is loaded verbatim — same script tag, same dom
ids, same behaviour. No second copy of the fetch/state/render logic exists for this shell. Two
small, additive, backward-compatible extensions were needed in that one file for the app's own
mobile idiom (item 3's own ask):

- **Strike picker**: `legRowHtml()` now renders a tappable `<button class="strike-btn">` instead of
  a native `<select>` **only when the page provides a `#optStrikeSheet` element** (detected via
  `document.getElementById`, not a hardcoded shell flag) — tapping it opens a `.ov`/`.ov-box` bottom
  sheet (byte-for-byte the pattern `mobile/trade_wall.html` already uses) listing the live chain's
  strikes; picking one sets the leg's strike + chain premium, exactly as the native `<select>`'s
  `change` handler already did. `scorr_options.html` (cc#2038) has no such element, so it keeps the
  native `<select>` already verified there — confirmed by re-running cc#2038's own 38-check harness
  after this change: unaffected, still 38/38.
- **Substantial reveal**: `substantialCell()` now opens a second `.ov`/`.ov-box` sheet
  (`#optSubstSheet`) when present, showing the real rupee figure there instead of the in-place
  text swap the web page still uses. Same detection pattern, same non-regression on web.

## `mobile/options.html`

Contract tokens only — `scorr_themes.css`'s real `goldnight` set (`--field/--panel/--hi/--edge/
--ink/--muted/--brand/--win/--loss`) plus its own geometry scale (`--space-N/--type-N/--radius-N/
--bw-N/--track-emN`), **all already present in that one shared file** — unlike the web page, no
local geometry `:root` block was even needed here. The design ref's own CSS carries over almost
verbatim (same class names, same layout), since the ref's assumed contract (`goldnight`) genuinely
**is** the app's real one — confirmed by reading `scorr_themes.css` directly before writing
anything, not assumed from the ref looking plausible. Bottom nav is the 5 canonical slots
(Home/GVM/Check/WoT/Models), copied verbatim from `mobile/alerts.html` (this session's own most
recent app page) rather than an older file, with none marked `.on` — Option Strategy isn't one of
the 5 slots, and `do_not_touch` forbids reordering them.

## Verify

`ast.parse` clean on the three `.py` files touched; `node --check` clean on `option_strategy.js`
and on the re-extracted, full `pwa.js` body (the second NAV-array edit this sprint, checked as real
JS again, not just valid Python string content). `theme_validator.count_raw`, run directly (same
method as cc#2038 — the MCP tool only reads the deployed app): found **5** raw primitives on the
first pass (four `rgba()` literals reused across the stale banner / chip-on / breakeven-row tints,
plus the `.ov` backdrop, all copied in from the ref/trade_wall.html without a token wrapper) →
declared each as a **named local custom property** (`--ov-backdrop`, `--brand-bg-14`, etc. — custom
property declarations are exempt from the count; only a literal used directly in a themed property
counts) and referenced via `var()` everywhere → **0**, matching this card's own verify line.

Real headless Chromium at **390px** (this card's own stated width) loaded the actual
`mobile/options.html` + `option_strategy.js` + `scorr_themes.css`, `/api/options/*` intercepted the
same way as cc#2038 (`resolve`/`payoff` route handlers run the real shipped
`option_strategy_engine.price_strategy()`, not a fabricated response). **19/19 checks pass**
(one console message, `net::ERR_CONNECTION_RESET` on the Google Fonts request, confirmed via a
dedicated `requestfailed` listener to be exactly that one request — this sandbox has no egress to
`fonts.googleapis.com`, not a page defect; the page's own `font-family:Sora,-apple-system,sans-serif`
fallback is the same real-world degrade path, and it was excluded from the pass/fail count only
after being independently confirmed, not assumed):

- App shell renders (header with the right title, 5-item bottom nav, none marked active).
- Tapping the Iron Condor template resolves 4 real legs.
- **This card's own explicit verify list**: no horizontal overflow at 390px with 4 legs
  (`scrollWidth` 390, at the viewport edge, never beyond it); the legs table, result panel and
  chart all measure inside the 390px viewport; the result cells show real, legible text matching
  the actual engine's numbers (breakevens/max profit/max loss, same real Iron Condor case
  cc#2037/cc#2038 already proved against live chain data).
- App-only interactions: the strike bottom sheet opens, lists the real chain strikes, and picking
  one applies the real chain premium; a naked short put (a genuine `bounded_by_zero` case) shows
  `Substantial`, tapping it opens the bottom sheet with the real rupee figure, and closing it works.

`app_route_map` row inserted and confirmed by direct query (`route='/m/options'`,
`surface='mobile'`, `nav_flag='m'`, `route_group='research'`, `template='mobile/options.html'`).

## A verify line that doesn't match where the wiring actually lives — checked, not assumed

`grep -c '/m/options' pwa_endpoints.py -> >=4` reads 1 in that file alone (the NAV array line) —
`PROTECTED`/`NAV_REGISTRY` live in `main.py`, not `pwa_endpoints.py`, confirmed by reading both
files directly (the identical mismatch cc#2038's own analogous line had, for the identical reason:
those two sets are declared in `main.py` site-wide, and every sibling `/m/*` page's own wiring is
split the same way — checked against `/m/trades` and `/m/alerts` before writing anything). All four
pieces exist and are correct (1 in `pwa_endpoints.py` + 3 in `main.py`); `_PWA_INJECT_PATHS` is
deliberately **not** one of them, matching the established rule that an app page carries its own
bottom nav and injecting the desktop one would double it (confirmed against every other `/m/*`
page's own wiring, e.g. `/m/trades`, `/m/alerts`).

## Not done here (explicitly deferred)

Home grid tile + web Home link (cc#2040 — gated on both 2038 and 2039 deploying, per that card's
own instruction). A live render on a real phone — no route to prod from this container; the 19/19
real-Chromium pass at the card's own stated 390px width is the evidence available here.
