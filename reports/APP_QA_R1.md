# SCORR APP REPORT — APP_QA_R1
**Date:** 17-Aug-2026 · **Author:** Fable · **Status:** DRAFT — awaiting founder observations
**Process:** REPORT_DRIVEN_EXECUTION_V1 (session_log 23981). CC executes NOTHING from a DRAFT report. Founder observations land in §6; Fable issues R2 marked EXECUTABLE; CC then works the workstreams top to bottom, one commit per workstream, and reports back per workstream.
**Audit basis:** repo source truth (APP_QA_AUDIT_V1, session_log 23979). Live-render checks marked THUMB are founder-verified on device.

---

## 1. Executive summary
The app is mid-migration between two design systems, its two newest pages are flow dead-ends, one route may be shadowed, and the product name "Index Intel" is not yet consistent. Backend and engines are sound — everything below is presentation, routing, and flow. Five workstreams fix all of it; no engine, worker, or DB write is touched anywhere in this report.

## 2. Surface map (19 app surfaces)
- **Nav (5):** /m/home · /m/gvm · /m/check · /m/intel (polished news) · /m/models
- **Tools → app:** /m/qb · /m/screeners · /m/sector · /m/results · /m/digest · /m/fpc · /m/trades
- **Tools → web (16915 exception):** /dashboard · /cio · /health · /dashboard#index · /v9 · /v15 · /v10 · /intraday
- **Sub-pages:** /m/holdings · /m/positions · /m/v8 · /m/v10 (INDEX INTEL) · /m/digest (new) · /m/gvm2 (fight card) · /m/login

## 3. Findings register
| ID | Sev | Finding | Root cause |
|---|---|---|---|
| F1 | P0-VERIFY | /m/digest may be shadowed: old mobile/digest.html route vs new scorr_digest_mobile.html (ea35475). Whichever router registers first in main.py wins. | Fable added the route without grepping for an existing owner (cc#847 lesson, repeated). THUMB test decides; fix in WS2 either way (one owner). |
| F2 | P1 | /m/v10, /m/digest, /m/gvm2 have no bottom nav, no wordmark, no back affordance — dead ends. | New R5 shells built standalone. |
| F3 | P1 | Naming: nav tab "Intel" = news; product "Index Intel" = /m/v10; home card says "TAP FOR THE V10 VIEW". | Name migration never specced. |
| F4 | P1 | Tools tiles web-first where app pages exist: V10 tile → /v10 (retiring web page), Index Intel tile → /dashboard#index. | Tiles predate the app pages. |
| F5 | P1 | Two live design systems: legacy Sora/IBM-Plex (13 pages) vs R5 Telemetry (3 pages+home partials). | Migration in flight, page order unruled. |
| F6 | P2 | Scorr wordmark absent on R5 pages. | Same as F2. |
| F7 | P2 | 9 small shells (holdings, positions, qb, results, sector, screeners, models, fpc, login) unaudited for back buttons / 44px targets / theme. | THUMB pass owed. |
| F8 | P2 | Route hygiene: /m/gvm2 still lives on scheduler_health_endpoints (parked, cc#1065 note). | Known debt. |

## 4. Workstreams

### WS1 — APP SHELL: one header + nav for the R5 pages (fixes F2, F6)
**Scope:** Add to scorr_v10_signal.html, scorr_digest_mobile.html, scorr_gvm_fightcard.html: (a) top header — Scorr wordmark left (tap → /m/home, 44px target), page title beside it; (b) the standard 5-slot bottom nav, current section highlighted volt where applicable (gvm2 highlights GVM; v10/digest highlight nothing — they are tool pages); (c) pages keep their R5 tokens exactly.
**Verify:** each page renders header + nav; wordmark tap navigates; no visual regression to card content (founder screenshot).
**DO_NOT_TOUCH:** page data wiring, loaders, refresh timers.

### WS2 — ROUTING & NAMING: Index Intel everywhere, one route owner (fixes F1, F3, F4, F8)
**Scope:** (1) /m/digest single owner: remove/disable the mobile/digest.html route in mobile_endpoints (grep first, state the finding in the commit); new page is the owner. (2) mobile/home.html: INDEX SIGNALS card sub-line → "TAP FOR INDEX INTEL ›"; V10 tools tile → /m/v10 label "Index Intel" (delete the old /dashboard#index Index Intel tile OR keep as "Index Intel · Web" — founder DECIDE §6). (3) Relocate /m/gvm2 route into v10_page_endpoints.py (the mobile page router) with a one-line redirect note. (4) NAV registry mirrors (pwa_endpoints NAV array + _PWA_INJECT_PATHS + PROTECTED) updated for any renamed labels per NAV-COMPLETE rule 2987.
**Verify:** grep shows exactly one GET /m/digest; /m/gvm2 serves from new router; home grep for "V10 VIEW" returns 0; NAV array diff shown.
**DO_NOT_TOUCH:** /m/intel news page itself (tab rename is DECIDE §6, not assumed).

### WS3 — R5 THEME COMPLETION on ruled items only (fixes part of F5)
**Scope:** Execute cc#1074 (aqua eyebrows + chalk titles, purple out of headings) and cc#1075 (LABEL_AMBER_V1) if still open when this report goes EXECUTABLE — those two in-flight tasks are absorbed here, not duplicated. Nothing from the cc#1072 DECIDE rows is touched (still founder-gated).
**Verify:** per cc#1074/1075 own verify blocks.

### WS4 — LEGACY PAGE MIGRATION ORDER (fixes rest of F5, F7)
**Scope:** Migrate the 13 legacy pages to R5 tokens in THIS order (traffic-weighted): v8 → gvm → intel → check → trade_wall → qb → results → sector → screeners → holdings → positions → models → fpc → login. Each page: token swap + shell audit (back chevron on sub-pages, 44px, as-of stamp) — one commit per page, founder screenshot per page before the next starts.
**Verify:** per page — grep 0 hardcoded legacy hex outside tokens; THUMB screenshot.
**DO_NOT_TOUCH:** any page logic/data wiring; the enhancer files (source-level only, cc#1068 pattern).

### WS5 — PHASE 2 GATE (BGW)
Black & Golden White (spec 23980) opens ONLY after WS4 completes and founder rules the three BGW DECIDEs. No CC work exists here yet. Listed so the sequence is on record.

## 5. Sequencing & gates
WS1 → WS2 → WS3 → WS4 (paged) → WS5 (gated). WS1+WS2 are one morning of work. Worker/** untouched throughout, so market-hours pushes are clean under PUSH_WHENEVER_POSSIBLE. BUG_FIRST_RULE applies inside every workstream.

## 6. FOUNDER OBSERVATIONS *(to fill — R2 incorporates)*
- F1 thumb-test verdict (old vs new digest page): ______
- Nav "Intel" tab: rename to News? keep? remap to Index Intel? ______
- Keep a web "Index Intel · Web" tile alongside /m/v10? ______
- WS4 page order changes: ______
- Anything the audit missed: ______

## 7. Changelog
- R1 17-Aug-2026: initial draft (Fable). Findings F1–F8, WS1–WS5.
