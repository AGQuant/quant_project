# cc#2141 — Index Intel tile (Home grid, Market → /m/v10) "not clickable / not responding": diagnosis

## Step 1 — the tile, confirmed
`mobile/home.html` `G_GROUPS` → Market group → `{ href: '/m/v10', label: 'Index Intel', svg: '<span class="gglyph">◐</span>' }`
(line ~2008), rendered by `gtileR1()` as a plain `<a class="gtile" href="/m/v10">` — no onclick, no JS
needed to navigate. `app_route_map` agrees: `/m/v10 · Index Intel (mobile) · home_grid_group Market ·
handler v10_page_endpoints.m_v10_signal`. Not in `HIDDEN_GRID_TILES` (`'/v10'` there is the web
route, exact-match only). Route serves `scorr_v10_signal.html` from disk; PROTECTED like every other
`/m/` screen (standard login gate, nothing extra).

## Step 2 — which failure mode? Tested, not guessed. NONE of the three reproduces.
**(a) dead link / no handler — NO.** Real `mobile/home.html` loaded in Playwright with every script
`main.py` injects on `/m/*` (`_MOBILE_HEAD`'s 13 files incl. the two served from `pwa_endpoints.py`
strings, r5 theme, `mobile_app.css`), the tile is present, `pointer-events auto`, nothing on top of
it (`elementFromPoint` = the tile's own glyph), and a tap navigates to `/m/v10` in every emulation
tried: mouse click; touch `tap()` in an iPhone context; touch `tap()` in an Android 10 / Chrome 152
context at 360px (the founder's UA per `perf_request_log`). Sibling tiles (News Intel, GVM) behave
identically. Every inline `onclick` handler on Home is a global function (runtime `typeof` check of
all 30 — the cc#2118/cc#2124 bug class is absent). No document-level click/touch delegate on the
page or in the shared scripts cancels an anchor tap outside its own targets (checked
`scorr_card_common.js`'s two capture handlers, `results_card.js`'s five, `scorr_mobile_cards.js`,
`scorr_appshell.js`, `mobile_tables.js`). Every full-screen overlay (`.tpo` ×3, `.pfveil`,
`.deriv-sheet-ov`, `.c-scrim`, bell) is `display:none` unless open and opaque when open. The
service worker is network-first for navigations (`fetch(req).catch(() => caches.match(req))`),
nothing intercepts `/m/v10`.
**(b) navigates but /m/v10 errors/blank — NO.** `scorr_v10_signal.html` parses (2 inline blocks,
`node --check` clean), paints its header + "Loading the desk…" immediately while its 15 boot
fetches are in flight, renders honest empty states on empty payloads with zero page errors. And
the founder's own phone proves the live path: `perf_request_log` shows Android-Chrome hitting
`/m/v10` at **06:38:36 IST 16-Sep, 200 in 56 ms**, then all 15 boot calls (market_mood, adr, pcr,
maxpain, strike_oi, vix, live_metrics, signal, positions, pivots, divergence, buildup,
quality-bullish-basis) 200 within 3 s, then `approved_map` and `volume-flow` — which `boot()` only
issues **after** `render()` returned, so the page rendered. He stayed 64 s and went back to Home.
**(c) deliberately disabled / gated — NO.** Only the standard `PROTECTED` login gate; not hidden;
the glyph is brand-coloured like the SVG tiles, not greyed.

## What the log does show (the symptom is real, and it is client-side)
Founder's evening session 16-Sep (same UA): Home 21:10 IST → CHAIN popup on the Max Pain card
(cc#2116, works) → Investment Scanner → Screeners → Home → Trade Check → V8 → Option Strategy →
Planning → My Alerts → app relaunch (`/` 302 → Home 21:28:25) → **Daily Digest tile 21:28:54
(the other `.gglyph` tile, right next to Index Intel — works)** → Home 21:29:06 → then no page
request at all until the Home auto-poll at 21:48 (page still open) and nothing after. **Not one
`/m/v10` request after 06:38 IST**, all day. So whatever happened, the tap never became a
navigation request — consistent with "nothing happens", inconsistent with a server or page fault
(a 5xx or a broken page would have logged the request). I cannot reproduce that on any emulated
device, mouse or touch, with the real page and all real shared scripts.

## Not changed
No code change on this card: there is no evidenced defect to fix in the tile, the route, or the
page, and the card says (c)-style states are reported, not silently re-enabled. Changing the tile
blind would be exactly the guess the card forbids.

## What would settle it (ask on the card)
1. Founder: which "Index Intel" — the Market-group **tile** (◐ icon, 3rd in the row) or the
   **INDEX INTEL →** bevel button under the Max Pain card on the hero deck? And what is seen after
   the tap: nothing at all, a blank dark page, "Loading the desk…" that never fills, or an error
   page? Any of these separates the remaining candidates cleanly.
2. If it recurs: one line of instrumentation — a `navigator.sendBeacon` on grid-tile tap — would
   put the tap itself in `perf_request_log` next to the navigation it should cause. Proposed as a
   follow-up card, not done here.

## Siblings (task 4)
News Intel, Sector Intel, Daily Digest, Results, Learn use the identical `gtileR1()` anchor
pattern; Digest shares the text-glyph icon. Digest and News Intel are proven working on the
founder's phone in the same log (08:53 and 21:28 IST 16-Sep). Nothing to flag.

## Found on the way (separate card, not a silent fix)
`mobile_tables.js` (served from `pwa_endpoints.MOBILE_TABLES_JS`, injected in `<head>` on every
`/m/*` page) ends with `new MutationObserver(scanSoon).observe(document.body, …)` executed at
script time, when `document.body` is still null → `TypeError: parameter 1 is not of type 'Node'`
on every app page load (reproduced with production injection order). The initial scan still
runs (it is DOMContentLoaded-guarded), but tables added after load never get the mobile-table
wiring. Harmless to navigation; filed as its own cc_task.
