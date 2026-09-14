# cc#2050 — /m/learn: Knowledge Galaxy banner, reusing the web's shared galaxy_map.js

Founder ask: the web Intel page's Learn tab has a Knowledge Galaxy — build the same for the app,
shown as a banner at the top of `/m/learn`. Tapping it opens the full galaxy (all articles).
Otherwise the existing category chips + article-card list stay exactly as they are.

## What was ported, and from where

Everything star-map-related — rendering, physics, pan/zoom/tap — is `galaxy_map.js`'s own job.
That file is **untouched** (confirmed by `git diff --stat galaxy_map.js` being empty at push time);
this card only adds a second caller of its existing, documented, instance-agnostic public API
(`GalaxyMap.create(container, {nodes, categories, cacheKey}, {onTap}) -> {setDim, resize, destroy,
_nodes}`), exactly as `galaxy_endpoints.py`'s own docstring anticipates ("Knowledge Hub is instance
#1; Quant Basket universe is the planned instance #2").

From `scorr_news.html`'s Learn tab, ported into `mobile/learn.html`:
- `CAT_COLORS` — the exact same category→colour map, verbatim, so a category reads the same colour
  on both surfaces.
- `drawGalaxyThumb()` / `buildGalaxyConfig()` — same logic, re-pointed at this page's own `D`
  object (`D.articles` in place of web's `kbArticles`) instead of `kbArticles`/`kbTotal`. **No new
  fetch** — `D` is the exact object `load()` already populates from the existing
  `GET /api/knowledge/articles?limit=500` call.
- `loadGalaxyScript()` — verbatim: adds `<script src="/galaxy_map.js?v=203">` once, guarded on
  `window.GalaxyMap` already existing.
- `openGalaxy()` / `closeGalaxy()` / `openGalaxyArticle()` / `hideGalaxyArticle()` — same shape and
  lifecycle (the `GalaxyMap` instance is destroyed and recreated only on a fresh `openGalaxy()`
  call, never on every render, so pan/zoom isn't reset by anything else on the page).

## Where this port deliberately stops short of a 1:1 copy

- **No search box or difficulty pills inside the overlay.** The web instance has them because its
  parent Learn tab already has search + a Beginner/Pro filter to wire a "dim non-matching stars"
  behaviour to. This page's Learn tab has neither — only a category chip row — and the founder's
  own words were "opens the full galaxy (**all articles**)". Adding filter UI the spec never asked
  for, backed by state this page doesn't have, would be scope the card didn't request. Every star
  always renders at full brightness; the page's own category chip (`CAT`) is untouched and has no
  effect on what the galaxy shows.
- **The article's difficulty/category chips inside the in-page reader use new, small, explicitly-
  scoped CSS (`.ga-chips .gd-diff`, `.pro`/`.beg`)** rather than reusing this file's own
  `.art .tag`/`diffCls()` — those rules are scoped to `.art` descendants (the card list) and
  wouldn't apply inside `.ga-chips` at all. Colours mirror `galaxy_map.js`'s own semantic tier
  colours (pro = amber, beginner = green) for consistency with the shared component itself.
- **The overlay's article cache is `mobile/learn.html`'s own existing `MD` object and `renderMd()`
  — not a second, parallel cache.** `openGalaxyArticle()` reads/writes `MD[slug]` exactly the way
  `openArt()` already does (a rendered-HTML cache, not raw markdown — the two functions must agree
  on that contract to share the object safely). Practical effect, confirmed in testing: reading an
  article via the card list first and then tapping its star costs zero extra fetches, and vice
  versa.
- **A caught theming trap, not a copy-paste of web's CSS:** web's galaxy CSS uses
  `var(--field, #070b1a)`-style tokens with a dark fallback. On the app those *same* token names
  are redefined per theme — light on aquawhite/goldday/silvergold (confirmed this session, cc#2047)
  — so reusing that pattern here would have silently broken the founder's own "the starfield stays
  dark regardless of app theme" requirement the moment someone opened the galaxy on a light theme.
  All galaxy-specific CSS in this port uses plain hex, matching `galaxy_map.js`'s own internal
  palette, never a themed token.
- **No special z-index/hide-nav handling for the app's fixed header or bottom nav** — the overlay
  (`position:fixed;inset:0;z-index:1000`) already outranks both (`.as-hdr` z-index 10, `.bnav`
  z-index 50) purely by stacking, so neither needed to be hidden or reordered. Confirmed by direct
  measurement below, not just by comparing the numbers on paper.

## Verify

`node --check` clean (via extracted-inline-script check). Real headless Chromium, the real
`mobile/learn.html` + the real, unmodified `galaxy_map.js`, a 9-article/3-category stubbed
`/api/knowledge/articles` payload — **36/36 checks pass**:

- Banner renders above `#chips` every time `draw()` runs (confirmed after a chip click too),
  sourced from `D.total`/`D.category_counts` with no dedicated fetch; thumbnail canvas paints a
  non-empty starfield.
- Existing category-chip filtering and the existing card-list read-in-place (`openArt()`) are
  unaffected.
- Tapping the banner lazy-loads `/galaxy_map.js` **exactly once** (confirmed again across a second
  open — the script tag is never re-added), opens the overlay, and hands `GalaxyMap.create()`
  **all 9** articles as nodes with correctly-wired payloads (no silent drop) and a real, non-guessed
  count badge.
- Tapping a star (invoking the real `onTap` handler `galaxy_map.js` itself would call) opens the
  article in place: title, summary, category chip, difficulty chip, and the markdown body all
  render from the real fetch; re-tapping the same star does not re-fetch.
- **Cross-cache proof:** an article already read via the ordinary card list shows instantly inside
  the galaxy with zero additional fetch, and vice versa — the two views share one cache, not two.
- Closing the article panel returns to the star map (overlay stays open); closing the overlay
  returns to the normal Learn page and releases the scroll lock.
- **Stacking, measured directly:** with the overlay open, `elementFromPoint` at both the header
  strip and the bottom-nav strip resolves inside `.galaxy-overlay`, while `.as-hdr` and `.bnav`
  both still exist in the DOM (covered, not hidden or removed).
- `openGalaxy()` no-ops cleanly on a zero-article state (no crash).
- `galaxy_map.js` has zero diff against the committed version.
- Zero real console/page errors (favicon.ico's own auto-probe, this repo's `/static/*` assets not
  being served outside the real FastAPI app, and the two external CDN hosts blocked by this
  sandbox's own egress policy were each individually traced by URL and excluded as environmental,
  not code, noise — `renderMd()`'s documented no-`marked`-available fallback was directly confirmed
  rendering in cdnjs's place).

Not done here, and not needed for this card: the founder's own live tap-through on scorr.in (this
container has no route to the deployed site, the same structural limitation on every UI card this
session) — the card's own verify section marks that step FOUNDER-ONLY.
