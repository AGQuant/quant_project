# SCORR APP REPORT — APP_QA_R4 · **EXECUTABLE**
**Date:** 17-Aug-2026 · **Author:** Fable · **Supersedes:** R3 (chain: R1–R3 kept) · **Execution ticket:** cc#1077
**Sprint 1 package: 12 ordered pushes.** One commit per push, numbered `R4-P1`…`R4-P12` in commit messages. Report DONE only when all 12 land and P12's results file is filled.

---

## §A · CC BRIEFING — READ BEFORE PUSH 1 (the operating system changed)
1. **REPORT_DRIVEN_EXECUTION_V1** (session_log 23991): consolidated reports replace task streams. You execute the whole package from this file, top to bottom, reporting per push in cc#1077's result. Individual cc_tasks now exist only for P0 breakage (and in-flight engine items like cc#1076, which stays a separate claim).
2. **APP_MODUS_OPERANDI_V1** (24081): founder builds the WEBSITE; **Fable owns the app** as a smart retail replica — GenZ, sporty, intuitive. Every page change is backed by a design-ref in `design_refs/` BEFORE build. You build to the ref, never past it.
3. **Phase 2 is open:** full AI-native app build. Fable = detail + design + verification, you = execution, founder = ideas + fund. **Production mode is ON**: claim and push immediately, market hours included (nothing here touches `worker/**`).
4. Standing rules unchanged: BUG_FIRST, PUSH_WHENEVER_POSSIBLE, NEW_TASK_PER_COMMENT, NAV-COMPLETE (2987), commit sha = claim until Fable verifies diff+DB.

## §B · REFERENCES (read each before its push)
- `design_refs/scorr_home_themes_R1.html` @ **80da8c6** — the 3-theme token layer + switch. THE source of truth for P1/P2/P10/P11 tokens, verbatim.
- `reports/APP_QA_R1.md` §2–3 — surface map + findings F1–F8 (context only).
- Live pages: `scorr_v10_signal.html` (ab9fad3), `scorr_digest_mobile.html` (1d73e4f), `scorr_gvm_fightcard.html`, `v10_page_endpoints.py` (ea35475), `mobile/home.html` (f19df63).

## §C · THE 12 PUSHES

**P1 — Theme token layer as a shared asset.** Create `static/scorr_themes.css` (served like mobile_app.css; if static/ isn't the pattern, repo root + route like other assets — state which). Content: the three `body[data-theme=…]` token blocks from the ref @ 80da8c6, byte-faithful (dark/goldday/goldnight), plus a comment header naming the mapping rules (P&L never re-skins; label=index-name colour; brand=structure+glow). No page consumes it yet.
*Verify:* file deployed and fetchable; tokens diff-identical to the ref.

**P2 — Home consumes the layer.** `mobile/home.html`: link the css; add `data-theme` to `<body>` (default `dark`); add the header switch (◆◇◈ pattern from the ref, 34×30px buttons, right of wordmark); persistence via `localStorage('scorr_theme')` read at parse time (before first paint — no flash). Map the THEMED sections' colors to the new vars where home already uses vars; hardcoded legacy hex in sections NOT yet migrated stays (that is WS4's job, not P2's).
*Verify:* switch renders; choice survives reload; dark theme byte-identical rendering to today (screenshot parity); no console errors.

**P3 — App shell on /m/v10 (Index Intel).** Add to `scorr_v10_signal.html`: top-left Scorr wordmark (44px target, tap→`/m/home`) beside the page title; standard 5-slot bottom nav (no tab highlighted — tool page); consume `static/scorr_themes.css` + honor stored theme.
*Verify:* wordmark navigates; nav renders; loaders untouched (diff shows shell + css link only).

**P4 — App shell on /m/digest.** Same treatment for `scorr_digest_mobile.html`.
*Verify:* same as P3.

**P5 — App shell on /m/gvm2 + route relocation.** Shell for `scorr_gvm_fightcard.html` (GVM tab highlighted). Move the `/m/gvm2` route from `scheduler_health_endpoints.py` into `v10_page_endpoints.py` (the mobile page router), deleting the parked route with a pointer comment.
*Verify:* `/m/gvm2` serves from the new router; scheduler_health diff is deletion-only.

**P6 — /m/digest single owner (F1).** Grep `mobile_endpoints.py` + `main.py` for the old `/m/digest` route serving `mobile/digest.html`. If it exists and wins registration order: remove/disable it so `v10_page_endpoints` owns the route. **Paste the grep finding into cc#1077's result either way** — if the new route already wins, say so and change nothing.
*Verify:* exactly one `GET /m/digest` across the codebase; `/m/digest` serves scorr_digest_mobile.html.

**P7 — Naming: Index Intel everywhere (F3).** `mobile/home.html`: INDEX SIGNALS card sub-line → `TAP FOR INDEX INTEL ›`. Bottom-nav labels untouched (**Intel tab rename is PARKED — founder DECIDE, do not touch /m/intel or its label**).
*Verify:* grep "V10 VIEW" = 0 in mobile/.

**P8 — Tools tiles app-first (F4).** `mobile/home.html` tools grid: `V10` tile → href `/m/v10`, label `Index Intel`; the old `/dashboard#index` tile relabels `Index Intel · Web` (kept — Fable decision on the open DECIDE, founder can prune in review).
*Verify:* grep shows `/m/v10` tile present; no dead hrefs.

**P9 — cc#1074 absorbed: two-colour headings.** Execute cc#1074's own spec (aqua eyebrows + chalk titles, purple out of all headings) across app surfaces; mark cc#1074 done pointing here.
*Verify:* per cc#1074's verify block.

**P10 — cc#1075 absorbed: LABEL_AMBER on home.** Execute cc#1075's spec exactly (3 CSS tokens in mobile/home.html → `#FF9F45` — note: under goldday/goldnight the token layer's `--label` supersedes visually; the amber applies to the legacy-var path so DARK renders amber today). Mark cc#1075 done pointing here.
*Verify:* per cc#1075's verify block.

**P11 — NAV registry mirror (rule 2987).** `pwa_endpoints.py`: NAV array + `_PWA_INJECT_PATHS` + `PROTECTED` reflect `/m/digest` and `/m/v10` as first-class app pages (labels `Daily Digest`, `Index Intel`) so injected navs and auth cover them. NAV_REGISTRY mirrored.
*Verify:* both routes in all three structures; auth_gate serves both.

**P12 — Results file.** Create `reports/APP_QA_R4_RESULTS.md`: table Push | SHA | Files | Verify-output | Notes, filled for P1–P11, including the P6 grep finding verbatim. This is the artifact Fable verifies against.

## §D · DO_NOT_TOUCH (package-wide)
Engines, `worker/**`, all `/api/*` payloads, data wiring inside the three R5 pages, `/m/intel` and its nav label, DECIDE-gated colours from cc#1072's scan, cc#1076 (separate claim).

## §E · SEQUENCING
Strictly P1→P12. If any push fails its verify: stop, log, continue is forbidden past a broken dependency (P2+ depend on P1; P6 independent; P9/P10 independent).

## §F · FOUNDER REVIEW (after P12)
Founder reviews on device: three themes on home, shells on the three pages, naming. Open DECIDEs carried: Intel tab fate · web-tile keep/prune · theme count final (3 shipped, founder may cut).
