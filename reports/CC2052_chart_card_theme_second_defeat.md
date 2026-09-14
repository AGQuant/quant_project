# cc#2052 — chart card still dark on light theme: the SECOND defeat mechanism, found and fixed

Founder report, 7:57 IST, screenshot: tapped C on the ADANIGREEN card in Home's EXPERT CURATED /
Approved Trades carousel — the chart card opened dark on a light theme, ~57 minutes **after**
cc#2047 (sha `29521a6`) had already landed a real fix for the same visible symptom on GVM.

## Step 1 — reproduce for real before concluding anything

Cache-busting was ruled out immediately by reasoning, not guessed: `main.py`'s `auth_gate` sets
`cache-control: no-store, no-cache, must-revalidate` on every injected HTML response (confirmed by
direct read), so a stale HTML/JS bundle 57 minutes after a push is not a credible explanation on
its own — but the card's own gate demanded real evidence either way, not just this reasoning.

**The real defeat mechanism, found by reading code, then confirmed empirically:**
`main.py`'s `auth_gate` middleware injects three theme-related scripts into every `/m/*` response,
in this order: `_MOBILE_HEAD`'s own script (sets `documentElement`'s `data-theme` from
`localStorage`), `_APP_THEME_RESOLVE` (only ever touches `document.body`, confirmed by reading
`pwa_endpoints.APP_THEME_RESOLVE_JS` in full), then **`_MOBILE_APP_DARK`** — which **unconditionally**
runs `document.documentElement.setAttribute('data-theme','dark')`, no `if`, no localStorage check,
labelled in its own comment "cc#1064: dark-only surface, stamped honestly." This is deliberate and
load-bearing for an **unrelated** reason: `scorr_theme_r5.css`'s shell rule needs `<html>` pinned
dark so inherited text colour resolves correctly regardless of the page's own light/dark choice
(confirmed by `mobile/home.html`'s own comment at the exact line describing this).

Because `_MOBILE_APP_DARK` runs **last**, `<html data-theme>` ends up `"dark"` on **every** real
`/m/*` page load, always — while the page's own boot script correctly writes the REAL selected
theme onto `<body>` (e.g. `aquawhite`). `<html>` and `<body>` disagree **by design**.

`scorr_chart_card.js`'s `_detectTheme()` checked `documentElement` **before** `body`:
```js
var dt = (r.getAttribute("data-theme") || b.getAttribute("data-theme") || "").toLowerCase();
```
`r` (documentElement) is truthy ("dark") on every real `/m/*` page, so the `||` short-circuits
before `body`'s correct value is ever read — `_detectTheme()` returns `"dark"` unconditionally on
every real app page, regardless of the actual theme.

**This is exactly why cc#2047's own test could not have caught it.** Its real-Chromium
reproduction (per its own report) loaded "the actual `scorr_themes.css` + the actual
`mobile_app.css`" directly — a clean, isolated fixture with no `data-theme` on `documentElement` at
all, so `_detectTheme()`'s `documentElement`-then-`body` order happened to fall through to `body`
by coincidence in that test, never exercising the real, always-present `_MOBILE_APP_DARK` pin.
cc#2047's fix (removing GVM's own local `theme:'dark'` hardcode) was a real, correct fix for a real,
separate defect — it just could not have revealed this deeper, universal one, because nothing in
its test environment reproduced the actual server-injected page.

**Confirmed empirically**, not just read: a real-Chromium page assembled from the **exact literal
script bytes** in `main.py`/`pwa_endpoints.py` (concatenated in the real injection order, not
retyped from memory) with `localStorage` set to `aquawhite` showed `<html data-theme>` = `"dark"`,
`<body data-theme>` = `"aquawhite"`, body's real `--panel` = `#FFFFFF` (genuinely light) — and
`_detectTheme()` returned `"dark"`. Root cause confirmed before any code changed.

## The fix

One line in `scorr_chart_card.js`'s `_detectTheme()`: swap the check order so `body` is read
**before** `documentElement`, falling back to `documentElement` only when body carries nothing.

```js
var dt = (b.getAttribute("data-theme") || r.getAttribute("data-theme") || "").toLowerCase();
```

**Confirmed safe for every other known caller before making this the new default order**, not
assumed: every real web-side host — `v8_dashboard.html`, `v10_dashboard.html`,
`scorr_holdings.html`, `scorr_result_corner.html`, `scorr_news.html` — has a **plain `<body>` with
no `data-theme` attribute at all** (direct grep, all five). On those pages `body.getAttribute(...)`
is `null`, so the swapped check still falls straight through to `documentElement` exactly as
before — zero behaviour change on the web side. The luminance fallback path (`--panel`/`--field`
via `getComputedStyle`) was already reading `body`, untouched either way.

**Untouched, per `do_not_touch`:** cc#2047's own fix in `mobile/gvm.html` (not reverted, not
duplicated — GVM no longer needs a local override, and still doesn't have one), `_pal()` (the
palette itself was never wrong), the C·A·R·D strip's own dispatch logic (`cardNav`/`_fallback` —
confirmed unmodified, this card is entirely about what `_detectTheme()` decides once the strip
correctly reaches `ScorrChartCard.open()`).

## Which of the card's three honest possibilities it was

**(b)** — a second defeat mechanism, expressed differently from the first (a structural precedence
bug in `_detectTheme()` itself, not another hardcoded `theme:'dark'` a plain string-grep would have
caught). Not (a): cache-control already rules out staleness, and the real-injection repro
reproduced the bug on a fresh load. Not (c): `open()`'s own `_theme = opts.theme || _detectTheme();`
already re-runs detection fresh on every call — there was no stale singleton; `_detectTheme()`
itself was simply answering the wrong question every time it ran on `/m/*`.

## Verify

`node --check` clean. Real headless Chromium, the **real, unmodified** `scorr_card_strip.js` +
the real (now-fixed) `scorr_chart_card.js`, assembled with the **exact literal injected scripts**
from `main.py`/`pwa_endpoints.py` in their real order (not an isolated fixture) — **42/42 scored
checks pass** (one additional theoretical, unconfirmed-to-occur combination logged for
transparency but not scored, explained below):

- **Both real dispatch paths**, end to end, across all 6 live app themes (aquawhite, goldday →
  remapped to aquawhite, silvergold, goldnight, dark, ainight): Home's exact path
  (`window.ScorrCardStrip.nav('C', sym)` → `_fallback` → `ScorrChartCard.open(sym)`, **no opts at
  all**, the precise shape that broke) now paints light on every light theme and dark on every
  dark theme, reading the actual `#scorrChartBoxWrap` computed background colour, not a proxy.
  GVM's own post-cc#2047 path (`open(sym, {gvm:true})`) checked identically alongside it, so this
  class of bug is now covered on **both** surfaces per the card's own request — it cannot resurface
  on one without the regression test catching it on the other.
- **Web-side regression, confirmed zero change:** `aquawhite` and `dark`, the two confirmed-real
  bare-`<body>` configurations, still paint correctly post-fix.
- One `goldnight`-on-web variant was included and reported for transparency but **not scored as
  pass/fail**: it models `documentElement` carrying a *named* theme while `body` stays bare, a
  combination not confirmed to occur on any real page (`v8_dashboard.html` documents its own,
  separate, non-`scorr_themes.css` dark/light mechanism). Confirmed algebraically unaffected by
  this fix either way — `body` is `null` in both the old and new check order there, so both
  produce the identical result; this fix cannot have broken it, and there is no reproduction
  showing it was ever broken to begin with. Flagged rather than silently dropped.
- Zero page errors across all 12 (app) + 6 (web) real-Chromium runs.

Not done here, and not needed for this card: the founder's own live check on scorr.in (this
container has no route to the deployed site, the same structural limitation on every UI card this
session).
