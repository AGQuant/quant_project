# cc#2137 — Screeners: out of the main top navbar, into the V8 Dashboard's own sub-nav row

Founder brief (16-Sep-2026): "shift screeners from main nav to V8 nav just like intel". This is
the cc#2136 treatment applied to `/screeners`, nothing more.

## What Screeners is (so nothing is confused)
- **Screeners** = `/screeners` → `scorr_screeners.html` (cc#824): the EOD predefined screens
  (`v13_presets` / `v13_screen_results`), served by `screeners_endpoints.py`. A standalone web page
  under the canonical nav.
- **Model Portfolio** (`#pane-model` inside the dashboard) has a QUANT SCREENERS launcher that opens
  the same stored screens as a read-only table and deep-links each row to `/screeners#<id>`
  (`scorr_model_portfolio.js`). Not the same surface, and not touched.
- **/m/screeners** is the app screen (`mobile/screeners.html`), its own NAV entry, unaffected.

## Change, the cc#2136 pattern exactly
1. **Row entry added** (`v8_dashboard.html #v8toptabs`) right before Model Portfolio:
   `<a class="v8toptab" href="/screeners">Screeners</a>` — a real link that NAVIGATES to the
   existing page, not a pane. Styled by the same `.v8toptab` rule as its sibling buttons; no
   `data-pane`, so it never takes the active state. Placed next to Model Portfolio because both are
   the list-of-lists entries (stored screens / registry baskets).
2. **NAV array** (`pwa_endpoints.py PWA_JS`): the `['/screeners', '☷', 'Screeners']` stop is
   removed, with a comment recording why and what was there. The evaluated array now has
   **41 entries**, no `/screeners` and no `/intel`; `/m/screeners` and every other stop are
   untouched. One array feeds both the desktop top bar and the mobile "More" sheet, so Screeners
   leaves both automatically.
3. **NAV_REGISTRY** (`main.py`): `/screeners` kind `nav` → `tab` ("V8 sub-nav entry (removed from
   top nav)"), the same treatment `/intel` (cc#2136), `/v12` (cc#557) and `/qb/universe2`
   (cc#2132) carry.
4. **PROTECTED and `_PWA_INJECT_PATHS`: kept, deliberately** — the same stated deviation as
   cc#2136/cc#2132. Removing `/screeners` from PROTECTED would un-gate a logged-in page; removing
   it from `_PWA_INJECT_PATHS` would strip the injected canonical nav, which after this change is
   the page's only way back. The route and `scorr_screeners.html` are untouched.

## Other references (all deep links to the page; all still work because the route is unchanged)
- `scorr_model_portfolio.js` — Model Portfolio pane rows → `/screeners#<screen_id>`.
- `mobile/screeners.html` — app "Custom screen" card → `/screeners#custom` (opens the web builder).
- `previews/v8_lower_redesign.html:85` — a preview screen (dummy, not live).
- `/dashboard` hash handlers: no `#screeners` alias exists (none needed; there was never a pane).
- `app_route_map` (Fable's registry table) still says `/screeners · in_nav true` — it mirrors the
  NAV array and is Fable's to restate (flagged in the room, not written by CC), same as cc#2136.

## Verify
`ast`/`py_compile` clean on `main.py`, `pwa_endpoints.py`; `node --check` clean on the `PWA_JS`
string and all 8 dashboard inline blocks; 0 new literal fallbacks. Playwright on the real
dashboard with the real web tokens, dark and light: visible row = V8 · Index Intel · Intel ·
TC Scanner · Digest · Wall of Trades · Alerts · Check · **Screeners** · Model Portfolio; the entry
is an `<a href="/screeners">` with no pane; computed font / weight / padding / colour / border
identical to a sibling button, no underline; clicking it navigates to `/screeners`. Row screenshots
looked at, dark and light (VISUAL_VERIFY_GATE_V1) — the Screeners label reads brighter in the
capture only because the pointer was resting on it after the click check (`:hover`); the
computed-colour check is the parity evidence.

## Not touched
Every other main-nav and sub-nav item; `/screeners`, `scorr_screeners.html`,
`screeners_endpoints.py`; the Model Portfolio pane and its launcher; `/m/screeners`.
