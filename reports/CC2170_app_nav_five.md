# cc#2170 — app bottom nav: fixed five, Home · GVM · AICIO · Check · Alerts, on every source

Founder 17-Sep-2026 12:04 IST ("in main nav hide V8, shift GVM to centre and AICIO in middle" + voice note: AICIO is the premium product) and 14:36 IST (screenshot of `/m/home` on his phone). Fable's RECO (room line 6981): the bar is FIXED, not variable — Home · GVM · AICIO · Check · Alerts, left to right; everything else leaves the bar and lives on the Home grid / More sheet; resolve the evidence conflict first, then converge EVERY source. Built 17-Sep-2026 from 14:45 IST server time (the Started line); checks up to 14:55 IST. The landing time is in the task row.

## The evidence conflict, resolved

My STOPPED log (07:11 UTC) said no page carries "Home · V8 · GVM · Check · Alerts". True of the markup and false of the phone, because a **fourth nav source** rewrites the bar at runtime:

- `scorr_card_common.js` (lines 436–470, appended by Fable under cc#897): "Every /m/* template carries a five-slot .bnav in its own markup; editing thirteen files for every nav change is the NAV CHURN trap. So the nav is now REBUILT here, in the one script every screen already loads — this block is the single owner of the mobile nav." Its slot list read Home `/m/home` · V8 `/m/v8` · GVM `/m/gvm` · Check `/m/check` · Alerts `/m/alerts` (the Alerts label from cc#1662), with a PARENT map lighting a slot for deeper screens. It runs when a `.bnav` exists and the path starts with `/m/`, replacing the page's own `<nav>` inner HTML on DOMContentLoaded.
- `main.py` `_MOBILE_HEAD` injects that script (build-stamped) into every PROTECTED `/m/` page, so the per-page `<nav class="bnav">` blocks (Home · GVM · Check · WoT · Models on 13 pages, · Dash on 16) have been dead markup, a no-JS fallback only.
- Production proof (`perf_request_log`, UTC): the founder's Android phone (UA "Linux; Android 10; K") `GET /m/home` 200 at 09:05:57 UTC = **14:35:57 IST**, followed within a second by `/static/mobile_app.css`, `/scorr_card_common.js`, `/scorr_bell.js`, `/static/scorr_appshell.js` … `/pwa.js`, `/service_worker.js`. The deployed build served it (the same app that runs the MCP tools, `VERSION 2.9.66` = main). Not a cached service-worker shell: navigations are network-first, `/m/` pages are `Cache-Control: no-store`, and the SW precaches only `pwa.js`, the manifest and the icons. Not a stale deploy: nothing on main or in git history renders that bar from markup — only the runtime owner does.

## What changed, source by source (32 files)

| Source | Before | After |
|---|---|---|
| `scorr_card_common.js` slot list (the live owner) | Home · V8 · GVM · Check · Alerts | **Home · GVM · AICIO · Check · Alerts**; AICIO → `/m/aicio` with the NAV array's own `/m/aicio` glyph `⊙` (`⊙`), distinct from GVM's `◈`. PARENT map: the V8 family (`/m/positions`, `/m/qb`, `/m/results`) and `/m/v8` itself now light Home (the cc#897 rule for a parent that is no longer a slot). |
| 29 × `mobile/*.html` `<nav class="bnav">` (no-JS fallback) | two variants (… WoT · Models / … WoT · Dash) | the same five on every page; `on` = the page's own slot (home, gvm, aicio, check, alerts) or Home for the pages the PARENT map lights (digest, fpc, holdings, intel, models, positions, qb, results, screeners, sector, v8), none otherwise. `mobile/login.html` has no bar. Per the RECO this overrides the card's item 3 (which assumed pwa.js was the owner). |
| `pwa_endpoints.py` `PRIMARY` (the bar pwa.js injects on legacy web pages at phone width) | `/m/home, /m/gvm, /m/check, /m/trades` + More | `/m/home, /m/gvm, /m/aicio, /m/check, /m/alerts` + More (the More sheet stays: on those pages it is what keeps nothing stranded, rule 2987); new `BAR_LABEL` map so the bar prints the one-word labels (cc#1662) while the NAV entries keep their descriptive More-sheet labels ("Trade Check (mobile)"). |
| `pwa_endpoints.py` NAV array | `['/m/aicio', '⊙', 'AI CIO (mobile)', 'm']` | label `'AICIO (mobile)'`; `/m/v8` entry unchanged (still in the More sheet). |
| `main.py` `NAV_REGISTRY` | `/m/v8` "nav-mobile"; `/m/aicio` "nav-mobile" (More sheet); `/m/alerts` "grid+more-sheet" | `/m/v8` → "grid+more-sheet" (off the bar; Home grid Analytics tile + More sheet); `/m/aicio` → "nav-mobile", bottom-bar slot 3; `/m/alerts` → "nav-mobile" (it has been slot 5 in the live owner since cc#1506/cc#1662 — the registry was stale, corrected). |
| `app_route_map` (DB) | `/m/v8` linked_from "nav; home grid; bottom nav"; `/m/aicio` "nav; home grid"; `/m/gvm` "nav; home grid" | The table carries `in_nav` (true for every NAV-array entry, i.e. the More sheet too), `nav_flag`, `nav_position` (the NAV array's own order, not the bar's) and `linked_from` — the bottom-bar membership lives in `linked_from`. Updated: `/m/v8` → "nav; home grid"; `/m/aicio` → "nav; home grid; bottom nav"; `/m/gvm` → "nav; home grid; bottom nav" (it was a slot all along). `in_nav` stays true on all three; `crawl` untouched; a cc#2170 note appended to `source`. |

Untouched, as the card requires: `mobile/home.html` `G_GROUPS` (the `/m/v8` "V8 Signals" Analytics tile, line 2039, HOME_GRID_R1), the `/m/v8` route and page, `_PWA_INJECT_PATHS` (no `/m/` path is in it by design — main.py lines 319–330 — so `/m/aicio` gets `_MOBILE_HEAD` through PROTECTED, where it already sits: `PROTECTED.add("/m/aicio")`, main.py 375).

## Harness (Playwright, real Chromium, `scratchpad/cc2170_test.py`) — 49 checks, ALL PASS at 375×812, goldnight + aquawhite

Pages served exactly as the server does: the template plus the `_MOBILE_HEAD` script tags (`scorr_card_common.js` first). `/m/home`, `/m/sector`, `/m/aicio`, `/m/v8`, and a stub web page with `/pwa.js` served from `PWA_JS` as the server evaluates it.

- Every app page: the rendered bar reads Home · GVM · AICIO · Check · Alerts with hrefs `/m/home /m/gvm /m/aicio /m/check /m/alerts`; the AICIO glyph `⊙` differs from GVM's `◈`; exactly one bar, no injected web bar; five tap targets ≥ 44 px (60 px), nothing clipped, the bar pinned to the viewport bottom, no sideways overflow.
- Lit slot: Home on `/m/home`, Home on `/m/sector` (PARENT), AICIO on `/m/aicio`, Home on `/m/v8` (PARENT).
- Tap AICIO on `/m/home` → `/m/aicio` (title "AI CIO · Scorr") with the AICIO slot lit.
- `/m/v8` still loads (title "V8 · Scorr"); its NAV-array entry and Home grid tile are unchanged (grep in the diff section).
- Web stub: `#pwa-mobile-nav` reads Home · GVM · AICIO · Check · Alerts · More, displayed at 375 px, six slots ≥ 44 px, nothing clipped.
- Static: all 29 page bars carry exactly the five hrefs in order; no bar left with `/m/trades`, `/m/models`, `/m/dash` or `/m/v8`.

**Screenshots looked at** (`scratchpad/cc2170_*.png`): `375_dark_m_home` — the Home page with the bar Home (lit, gold) · GVM · AICIO (⊙) · Check · Alerts. `375_dark_aicio_tapped` — the AI CIO page after the tap, AICIO lit. `375_light_m_v8` — the V8 page on the light theme, still loading as a page, Home lit on the five-slot bar. `375_dark_webbar` — the web stub's injected bar: Home · GVM · AICIO · Check · Alerts · More.

## Checks

- `node --check` on `scorr_card_common.js` and on `PWA_JS` as served: OK. `ast.parse` `main.py`, `pwa_endpoints.py`: OK. No colour literals added anywhere.
- `worker/**` untouched. `main.py` wiring only (registry strings).

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. On the phone, open `https://scorr.in/m/home` after the deploy (the script URL is build-stamped, so the next page load picks it up; no cache clearing needed): the bar reads Home · GVM · AICIO · Check · Alerts; tap AICIO → `/m/aicio`. Same bar on `/m/sector`, `/m/v8`, `/m/tcscan`.
2. `/m/v8` reachable from the Home grid's V8 Signals tile and, on a web page at phone width, from More → V8 (mobile).
3. `SELECT route, linked_from FROM app_route_map WHERE route IN ('/m/v8','/m/aicio','/m/gvm')` → as in the table above.

## Out of scope (by spec)

AICIO page content (cc#2171).
