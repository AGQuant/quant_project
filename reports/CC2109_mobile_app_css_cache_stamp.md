# cc#2109 — mobile_app.css build-stamping gap (P0)

## Item 1 — the real Cache-Control header, confirmed not assumed
`mobile_endpoints.mobile_app_css()` (the actual route serving `/static/mobile_app.css`):
```python
return Response(MOBILE_CSS, media_type="text/css", headers={"Cache-Control": "no-store"})
```
**This is `no-store`, not `max-age=86400`** as the card's own evidence section assumed by analogy
with its siblings. Stated here precisely because it changes what this fix can honestly claim: a
compliant HTTP cache should never have held a stale copy of this specific file to begin with —
`no-store` is a stronger instruction than even `no-cache`, forbidding any storage of the response
at all, anywhere in the chain.

## Items 2-3 — the fix, and the full audit
Found the existing mechanism: `main.py`'s global `@app.middleware("http")` (the same one that
already stamps `scorr_themes.css`/`scorr_appshell.css`) does a body-text replace of
`href="/static/<name>.css"` → `href="/static/<name>.css?v=<build>"` for a short, explicit tuple of
filenames. `mobile_app.css` was never in that tuple. Added it:
```python
for _css in (b"scorr_themes.css", b"scorr_appshell.css", b"mobile_app.css"):
```
**Full audit, not assumed** — grepped every file in the repo for the tag: **exactly 30**
`mobile/*.html` templates link `mobile_app.css` directly, each with **exactly one**, byte-identical
`<link rel="stylesheet" href="/static/mobile_app.css">` tag and no pre-existing `?v=` anywhere
(confirmed via `grep -c` per file). Because the fix lives in the one shared response middleware
that already runs on every page (this is how `scorr_themes.css`/`scorr_appshell.css` already reach
every consumer without a per-template edit), one change covers all 30 without touching any of the
individual template files. **Verified, not assumed**: ran the exact committed replace logic
(copied verbatim from the file, not retyped) against all 30 real template bodies — every one gets
`?v=<build>` appended, the old unstamped tag is fully gone, and byte-length growth matches exactly
what stamping that page's own `mobile_app.css` + `scorr_themes.css` + `scorr_appshell.css` tags
should add (no other content touched). Full list: aicio, alerts, check, dash, digest, fpc, gvm,
health, holdings, home, intel, invscan, learn, login, mf, models, myalerts, myportfolio,
mywatchlist, options, positions, qb, qb_holdings, qbbuilder, results, screeners, sector, tcscan,
trade_wall, v8.

## Item 4 — does an already-cached phone need anything beyond loading a page?
Given the header is genuinely `no-store`, a real browser should not have been holding a cached
copy of this file at all, stamped or not — so this specific fix is unlikely to be "the moment it
starts working" for anyone, because nothing here should have been broken by HTTP caching to begin
with. Framed against the actual, weaker risk this class of fix defends against (a caching layer
that does not fully respect `no-store` — a misconfigured intermediate proxy, or a rare browser
quirk): the answer is still **yes, loading any page after this deploys is sufficient**, because
the fix works by changing the *URL itself* on every deploy, not by asking any cache to expire
correctly. A URL a browser has never requested cannot be served from its cache regardless of what
that cache did or didn't honor previously.

## What this fix does NOT explain — stated honestly, not glossed over
The card's own evidence cites specific symptoms: "the founder's live screenshot shows [a pill/badge
treatment on `.cell .v`] and [`text-transform:uppercase` on `.sec h2`]" on `mobile/tcscan.html`.
Checked directly: **`mobile_app.css`'s real, current content contains zero `.cell` rules and zero
`.sec h2` rules** (grepped the live `MOBILE_CSS` string in `mobile_endpoints.py` — neither selector
exists anywhere in that file). `.cell`/`.sec h2` are declared entirely inside `mobile/tcscan.html`'s
own inline `<style>` block — the exact rules I edited myself in cc#2105 (dropped `.sec h2`'s
`text-transform:uppercase`; flattened `.cell` from a bordered/boxed grid cell to a single-row-with-
dividers strip). **A stale copy of `mobile_app.css`, however stale, cannot produce either symptom
described** — those two selectors are not in that file.

Checked the other two candidate mechanisms directly rather than stopping at `mobile_app.css`:
- **The HTML document itself** (`mobile/tcscan.html`, served via `mobile_endpoints._page()`) is
  *also* `Cache-Control: no-store`, with an explicit docstring: "No caching — these change per
  deploy and a stale shell is the cc#867 class."
- **The service worker** (`pwa_endpoints.SW_JS`) does not cache either resource: its `SHELL` list
  (the only paths it cache-first-serves) is `['/pwa.js', '/static/manifest.json',
  '/static/icon-192.png', '/static/icon-512.png']` — neither `mobile_app.css` nor `/m/tcscan` is in
  it. Its `fetch` handler is network-first for navigations (falls back to cache only when the
  network request itself fails) and does not call `respondWith()` at all for anything outside
  `/api/`, `/pwa.js`, navigations, or `SHELL` — meaning requests for `mobile_app.css` fall straight
  through to the browser's normal network fetch, which respects `no-store`.

**Conclusion, stated plainly per this card's own instruction not to force a speculative fix**: I
could not find a caching mechanism in this codebase that would produce the two specific symptoms
cited. The build-stamping gap on `mobile_app.css` was real and worth closing regardless (defense-
in-depth, and consistency with every sibling shared asset already gets) — done, verified above.
But it is very unlikely to be what the founder's screenshot actually showed. The most ordinary
explanation that would fit — an already-open browser tab or installed PWA window from *before*
cc#2105 deployed, simply never reloaded — needs no code fix at all, only a fresh page load. Worth
asking Arpit directly whether the screenshot came from a freshly-opened page/tab or one that had
already been sitting open; that answer would settle it without further guessing.

## Verification
- `ast.parse`/`py_compile` clean on `main.py`.
- Theme fallback ratchet (`.py` is in scope): `main.py` 12→12, delta 0. (Raw ratchet does not gate
  `.py` files at all — confirmed via `theme_validator.gate()`'s own `.css`/`.html`-only check.)
- Real byte-level simulation of the exact, committed middleware logic against all 30 real
  `mobile/*.html` template bodies — 30/30 pass (tag stamped, old tag gone, no corruption).
- No screenshot for this card: it is a pure delivery/cache-busting change to a middleware
  (`main.py`), the stylesheet's own content is untouched (do_not_touch), and nothing about how any
  page renders changes as a result — there is nothing new to look at.

## What did NOT change
`mobile_app.css`'s own CSS rules — untouched, per do_not_touch. `mobile/tcscan.html`'s own
`.cell`/`.sec h2` rules from cc#2105 — already correct, confirmed again here, untouched.
