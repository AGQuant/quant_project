# cc#2136 — Intel: out of the main top navbar, into the V8 Dashboard's own sub-nav row

## Clarify first (as the card asked): Intel and Index Intel are DIFFERENT pages
- **Intel** = `/intel` → `main.py intel_page()` → `_page("scorr_news.html")` (cc#1754: the news +
  research feed, standalone under the canonical nav, "full screen like GVM"). `/news` 301s to it.
- **Index Intel** = the `#pane-index` desk INSIDE `v8_dashboard.html` (`showV8Pane('index')`,
  `idxBoot()`), the buildup / OI / Custom Screen Builder pane. Two routes, two renderers, two
  data paths. Not merged (out of scope, and not the same thing).

## How the V8 sub-nav row is built (task 1)
A hardcoded row in `v8_dashboard.html`: `<div class="v8toptabs" id="v8toptabs">` of
`<button class="v8toptab" data-pane="…" onclick="showV8Pane('…')">` buttons (V8 · Index Intel ·
[V14 hidden] · TC Scanner · [V6 BT hidden] · Digest · Wall of Trades · Alerts · Check · Model
Portfolio), driven by nothing shared. `showV8Pane()` swaps panes and marks the active button by
`data-pane`.

## Change (tasks 2–3), the cc#2132 pattern
1. **Row entry added** right after Index Intel: `<a class="v8toptab" href="/intel">Intel</a>` — a
   real link that NAVIGATES to the existing page, not a pane (cc#1754 delinked Intel from the V8
   chrome on the founder's instruction, and a pane would undo that; cc#853's Digest entry is the
   precedent for a navigating entry in this row). Styled by the same `.v8toptab` rule as its
   sibling buttons; no `data-pane`, so it never takes the active state.
2. **NAV array** (`pwa_endpoints.py PWA_JS`): the `['/intel', …, 'Intel']` stop is removed, with
   a comment recording why and what was there. The evaluated array now has 42 entries and no
   `/intel`; `/screeners` and every other stop are untouched. This one array feeds both the
   desktop top bar and the mobile "More" sheet, so Intel leaves both automatically.
3. **NAV_REGISTRY** (`main.py`): `/intel` kind `nav` → `tab` ("V8 sub-nav entry (removed from top
   nav)"), the same treatment `/v12` (cc#557) and `/qb/universe2` (cc#2132) carry.
4. **PROTECTED and `_PWA_INJECT_PATHS`: kept, deliberately** — the same stated deviation as
   cc#2132. Removing `/intel` from PROTECTED would un-gate a logged-in page; removing it from
   `_PWA_INJECT_PATHS` would strip the injected canonical nav, which after this change is the
   page's only way back. The route and `scorr_news.html` are untouched (do_not_touch).

## Other references (task 4)
`/dashboard#intel` still client-redirects to `/intel` (v8_dashboard.html, two hash handlers,
cc#1754) — a held link keeps working. No other page, tour or onboarding flow links `/intel`
(grepped every html/js/py outside main.py/pwa_endpoints.py; `mobile/home.html`'s News Intel tile
is `/m/intel`, the app screen, unaffected). `app_route_map` (Fable's registry table) still says
`/intel · in_nav true · nav_position …` — it mirrors the NAV array as of 12-Sep and is Fable's to
restate; flagged in the room, not written by CC.

## Verify
`ast`/`py_compile` clean on `main.py`, `pwa_endpoints.py`; `node --check` clean on the `PWA_JS`
string and all 8 dashboard inline blocks; 0 new literal fallbacks. Playwright on the real
dashboard with the real web tokens, dark and light: visible row = V8 · Index Intel · **Intel** ·
TC Scanner · Digest · Wall of Trades · Alerts · Check · Model Portfolio; the entry is an `<a
href="/intel">` with no pane; computed font 13px / 700 / 9px 18px / colour / 2px border identical
to a sibling button, no underline; clicking it navigates to `/intel`. Row screenshot looked at
(the Intel label reads brighter in the capture only because the pointer was resting on it —
`:hover`; the computed-colour check above is the parity evidence).

## Not touched
Every other main-nav and sub-nav item; `/intel` and `scorr_news.html`; the Index Intel pane.
