# SCORR APP REPORT — APP_QA_R2
**Date:** 17-Aug-2026 · **Author:** Fable · **Status:** DRAFT — supersedes R1 (`reports/APP_QA_R1.md`, kept for chain)
**New in R2:** APP_MODUS_OPERANDI_V1 folded in (session_log 24081); WS6 added; design-ref requirement now binding on every workstream.

---

## 0. Modus operandi (binding on this and every future app report)
Arpit builds the website. **Fable builds the app as a smart replica of the web for retail** — intuitive, GenZ, sporty. Numbers identical to web (16915); depth and presentation differ. Big tasks split into workstream pieces with detailed specs, and **every new page or page modification ships with an HTML design-ref first** (design_refs/, numbered chain). CC executes only from an EXECUTABLE report revision; Fable verifies diff + DB + founder screenshot.

## 1. Executive summary
Unchanged from R1: the app is mid-migration between two design systems, its newest pages are flow dead-ends, one route may be shadowed, and naming is inconsistent. R2 adds the retail-replica direction and the home theme switcher. Six workstreams; no engine, worker, or DB write touched anywhere.

## 2. Surface map
As R1 §2 (19 surfaces). Unchanged.

## 3. Findings register
As R1 §3, F1–F8. Unchanged. F1 verdict still owed (thumb-test: Digest tile — old page or new aqua-eyebrow page?).

## 4. Workstreams

### WS1 — APP SHELL (F2, F6) — as R1, plus:
Header/nav design-ref required BEFORE build: `design_refs/scorr_appshell_R1.html` showing wordmark header + 5-slot nav + back-chevron pattern in R5 tokens. Fable produces; founder eyeballs; then CC applies to /m/v10, /m/digest, /m/gvm2.

### WS2 — ROUTING & NAMING (F1, F3, F4, F8) — as R1 verbatim.
No design-ref needed (no pixels change beyond label strings).

### WS3 — R5 THEME COMPLETION (ruled items) — as R1 verbatim (absorbs cc#1074/1075 if open).

### WS4 — LEGACY PAGE MIGRATION, retail-replica pass (F5, F7) — amended:
Order as R1 (v8 → gvm → intel → check → trade_wall → qb → results → sector → screeners → holdings → positions → models → fpc → login), but each page is now a **replica pass, not just a token swap**: (a) R5 tokens; (b) shell per WS1; (c) retail simplification — GenZ/sporty presentation of the SAME numbers (no depth added, no numbers changed); (d) **one design-ref per page before its build** (`design_refs/scorr_<page>_mobile_R1.html`). Cadence: Fable ships the ref → founder comment round → CC builds → founder screenshot → next page. One commit per page.

### WS5 — PHASE 2 GATE — amended:
BGW (23980) relationship to the new theme plan is a founder DECIDE (§6): the B&W theme of WS6 may precede, absorb, or replace BGW. No build either way until WS4 completes.

### WS6 — HOME THEME SWITCHER (new, founder 17-Aug)
**Scope:** /m/home gains a theme toggle (persistent per device) with two themes: **Theme 1 "MONO" — black & white clean minimal** (field near-black, chalk white, greys; P&L keeps green/red — money truth never goes mono); **Theme 2 "AQUA" — aqua on blue** (Aquaman direction; Telemetry-adjacent: petrol field, aqua structure, volt/heat semantics). Implementation: one token layer (CSS variables) + a `data-theme` attribute switch; NO per-theme markup forks. Home first; other pages inherit only after home is approved.
**Design-ref first:** `design_refs/scorr_home_themes_R1.html` — the same home hero rendered in both themes with the switch control visible.
**Verify:** toggle persists across reloads (no localStorage in refs — real page may use it, ref demos in-memory); zero layout shift between themes; P&L colours identical in both.
**DECIDE (§6):** founder wrote "blank and white" — read as **black & white**; confirm. Confirm switch placement (header vs settings row).

## 5. Sequencing & gates
WS1 → WS2 → WS3 → WS6 (home switcher lands before the long WS4 march so the token layer is theme-ready) → WS4 (paged, per-page refs) → WS5 (gated). Worker/** untouched throughout.

## 6. FOUNDER OBSERVATIONS *(to fill — R3 incorporates)*
- F1 thumb verdict (Digest tile: old or new page?): ______
- "blank and white" = black & white mono? ______
- Theme switch placement (header toggle vs settings): ______
- BGW phase-2: absorbed by WS6 themes / stays as third theme / dropped: ______
- Nav "Intel" tab fate + web Index-Intel tile (carried from R1): ______
- WS4 order changes / anything missed: ______

## 7. Changelog
- R2 17-Aug: modus operandi §0; WS4 became replica pass with per-page refs; WS6 theme switcher added; WS5 reframed as DECIDE; sequencing updated.
- R1 17-Aug: initial draft. Findings F1–F8, WS1–WS5.
