# cc#2038 — Web /options page (OPT sprint 3/5)

Built from Fable's design ref (`previews/option_strategy_builder.html` @ ac7cc21, founder-approved)
+ the DOM/API contract (`session_log 45192`), wired to cc#2037's five endpoints. NAV-COMPLETE per
rule 8.

## The one real design decision this card required: which token contract

The design ref's own `:root` fallback uses `--field/--ink/--muted/--brand/--win/--loss` on
`data-theme="goldnight"` — the **app's** gold/black contract. `WEB_THEME_TOKEN_LOCK_V1`
(`session_log 29075`, founder ruling) is explicit and still standing: *"APP IS NOT WEB ...
Nothing here touches them"* — one navy-dark/white-light contract (`scorr_web_tokens.css`) for
every web page, a different one for the app. Nothing in cc#2038's own spec names 29075 as
superseded, and the Supersession process (rule 12) requires an explicit, logged permission before a
locked rule stops applying — silence isn't that. `session_log 45192`'s "contract tokens" line is
read as this feature's **geometry/role vocabulary** (space/type/radius scale, which colour role
does which job), not a licence to relight a web page gold — so every **colour** role maps onto
`scorr_web_tokens.css`'s real names (`--bg/--panel/--panel2/--txt/--mut/--line/--line2/--grn/--red/
--blu/--on-accent`), and the ref's own **spacing/type/radius scale** (pure geometry, not something
the colour lock governs) is kept verbatim as this page's own local custom properties, so the exact
layout the founder already approved renders unchanged, just correctly re-skinned. No `<link>` or
`:root` colour block of this page's own — `scorr_web_tokens.css` + the theme boot are auto-injected
by main.py's middleware once `/options` is in `_PWA_INJECT_PATHS` (this card's own item 5), the
same way every other web page gets them.

## Files

- **`scorr_options.html`** — the page: header, chain/spot strip, underlying+expiry selects, Build/
  Readymade tabs, legs table (`#optLegs`, side/kind pills, strike select, premium input, lots,
  remove), Readymade grid with view/sub chips, result panel (`#optBE/#optMaxP/#optMaxL`, chart,
  per-leg table). Every dom id matches `session_log 45192`'s `dom_ids_shared_web_and_app` exactly.
  Zero inline JS beyond the one `<script src="/static/option_strategy.js">` tag.
- **`option_strategy.js`** (repo root, served at `/static/option_strategy.js` — matching this
  repo's own convention that `/static/*` is a URL namespace over root-level files, never a real
  `static/` directory; confirmed by reading `scorr_appshell.js`'s own serving route before writing
  anything) — the full behaviour list: meta/chain load, tab switching, leg CRUD (side/kind
  pill toggles, chain-fill on strike pick with the `.pf` mark removed on manual edit, 10-leg cap),
  Readymade grid + view/sub chip filtering, template-tap → resolve → build tab → payoff, chart SVG
  (path, zero line, win/loss shading, BE dots, spot dashed line, axis labels), per-leg table with
  `.be` rows, `Substantial` reveal-on-tap for a `bounded_by_zero` cell. **Shared, not duplicated**:
  written once now so cc#2039 (app) loads this SAME file rather than the usual build-then-extract
  churn — its own scope already asks for exactly this ("one JS file... page files only differ in
  shell").
- **`option_strategy_engine.py`** (small, backward-compatible addition to cc#2036's own file):
  `curve()`/`curve_per_leg()` gained an `extra` parameter merging exact points (breakevens, each
  leg's own strike) into the regular spot±15% grid, and `price_strategy()` now passes both in
  automatically. Why: this card's own item 4 asks for a **7-row per-leg table** at "range ends,
  each strike, each BE" — a breakeven (e.g. 23,323.3) is essentially never on the regular 50-wide
  grid, so without this a BE row's Net would be close to but not exactly zero. Verified directly:
  the Iron Condor's own BE rows now sum to **exactly** 0 across all 4 legs (confirmed both by a
  direct Python check and inside the Chromium test below) — and this is additive only (existing
  points are never removed), so cc#2036's own 34 golden tests and cc#2037's own endpoint harness
  were re-run afterward and still pass unchanged.
- **`option_strategy_endpoints.py`**: `/options` now serves the real page (was cc#2037's one-line
  placeholder); `/m/options` is untouched, still cc#2039's placeholder.
- **`pwa_endpoints.py`**: NAV array entry (`/options`, unflagged — desktop top bar + mobile More
  sheet, same as Screeners) placed next to Screeners/Invest Scan; a new `/static/option_strategy.js`
  serving route, read-from-disk-once exactly like `scorr_appshell.js`'s own.
- **`main.py`**: `/options` added to `_PWA_INJECT_PATHS` and `PROTECTED` (gate + no-store, gets the
  auto-injected web tokens + theme boot), `NAV_REGISTRY["/options"]` entry, and
  `option_strategy.js` added to the existing JS cache-stamp loop (the cc#1066 fix this repo already
  has for `scorr_appshell.js` — an unstamped `/static/*.js` on an unchanging URL is exactly last
  time's "deploy doesn't reach my phone" bug, pre-empted here rather than repeated).

## A card instruction that didn't match the live data — checked, not assumed

Item 5 says add `/options` "under group Tools/research (match the group Wall of Trades uses)".
Queried `app_route_map` directly: Wall of Trades' own row is `route_group='engine'`,
**`in_nav=false`** — it isn't even a NAV entry. The other half of the same instruction
("group research") **does** match the real `route_group` every comparable standalone research/tool
page here actually uses (`/screeners`, `/inv-scanner`, both `'research'`). Used `'research'` — the
value that matches real sibling pages, not the named-but-incorrect comparison — stated here and in
the `app_route_map` row's own `source` note rather than silently picking either one.

## Verify

`ast.parse` clean on all four `.py` files; `node --check` clean on `option_strategy.js` and on the
full extracted `pwa.js` body (the NAV array edit, checked as real JS, not just valid-looking Python
string content). `theme_validate`'s own check (`theme_validator.count_raw`, run directly — the MCP
tool itself only reads the deployed app's filesystem, unreachable pre-push) found **1 raw primitive**
on the first pass (`color:#fff` on the Calculate button) and **0** after fixing it to
`var(--on-accent)` (`scorr_web_tokens.css`'s own "text on an accent fill" token, found by reading the
file rather than guessing) — matching this card's own verify line exactly.

Real headless Chromium loaded the actual `scorr_options.html` + `option_strategy.js` +
`scorr_web_tokens.css`, with `/api/options/*` intercepted via `page.route` — `meta`/`chain`/
`templates` fixtures use real numbers already pulled from the live DB this session, and the
`resolve`/`payoff` route handlers call the **actual shipped `option_strategy_engine.price_strategy()`**
directly rather than fabricating a response, so the page is tested against genuine engine output,
not a hand-picked number. **38/38 checks pass**:

- Meta wiring: spot/ATM/lot/expiry/as-of/stale-badge all render the real fetched values.
- Legs: 10-cap enforced (button disables, label reads "10 of 10"); every row carries all six
  controls; a freshly-added leg at ATM is chain-filled (`.pf`, real premium); side pill toggles
  BUY/SELL, kind pill cycles CE→PE→FUT→CE; every leg removable.
- Readymade: chip-filtered fetch (bullish default → neutral → range sub-filter down to Iron
  Condor only); tapping a template resolves onto real strikes `[23300,23400,23600,23700]` and real
  chain premiums `[156.25,191.55,171.95,130.55]` — matching `golden_30.json`'s own Iron Condor
  exactly (cc#2037 already proved this fixture is the same live tick) — switches back to Build,
  and auto-runs payoff.
- Result: breakevens/max profit/max loss match the real engine's own numbers exactly; neither cell
  wrongly shows `Substantial` (a defined-risk Iron Condor); chart renders a real path + exactly 2
  breakeven dots; the per-leg table carries exactly 2 `.be`-marked rows whose Net column is exactly
  `0` (the extra-points engine fix, confirmed live in the browser, not just in isolation).
- Editing a chain-filled premium removes its `.pf` mark (behaviour list item 2); Calculate re-runs
  cleanly afterward; zero console errors across the entire interaction.

`app_route_map` row inserted and confirmed by direct query (`route='/options'`,
`serving_file='option_strategy_endpoints.py'`, `handler='options_web_page'`,
`template='scorr_options.html'`, `route_group='research'`, `in_nav=true`, `nav_position=43`).

## Not done here (explicitly deferred)

`/m/options` (cc#2039) — untouched, still cc#2037's placeholder. Home grid tile / web Home link
(cc#2040 — gated on both 2038 and 2039 deploying first, per that card's own instruction). A live
render on scorr.in — no route to prod from this container; the 38/38 real-Chromium pass plus the
`theme_validate` 0-raw-primitives check are the evidence available here.
